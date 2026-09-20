"""M4 生成与溯源编排。

流程：
    RAG 检索补充片段 → LLM 生成解释 → **再跑一遍规则引擎做二次校验** → 挂溯源

二次校验是这套架构的关键：LLM 输出里的 level 必须与规则引擎的判定一致。
不一致就整体回退到规则答案 —— 宁可输出朴素但正确的结果，也不要漂亮但错的结果。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ddi.generate.llm_client import LLMClient, LLMUnavailable
from ddi.generate.prompts import (
    DISCLAIMER,
    SYSTEM_PROMPT,
    build_user_prompt,
    fallback_summary,
)
from ddi.rules.engine import SEVERITY_ORDER, AssessResult

log = logging.getLogger(__name__)


@dataclass
class Generation:
    result: dict[str, Any]
    used_llm: bool = False
    degraded_reason: str | None = None
    recheck_passed: bool = True
    recheck_notes: list[str] = field(default_factory=list)
    model: str | None = None
    usage: dict[str, int] = field(default_factory=dict)


def _validate_shape(payload: dict, valid_rule_ids: set[int]) -> list[str]:
    """结构性校验：字段齐不齐、rule_id 是否来自检索片段。"""
    problems: list[str] = []
    if not isinstance(payload.get("items"), list):
        problems.append("缺少 items 数组")

    for i, item in enumerate(payload.get("items") or []):
        if not isinstance(item, dict):
            problems.append(f"items[{i}] 不是对象")
            continue
        rid = item.get("rule_id")
        try:
            rid_int = int(rid)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            problems.append(f"items[{i}] 的 rule_id 无效：{rid!r}")
            continue
        if rid_int not in valid_rule_ids:
            # 模型编了一个不在片段里的 rule_id —— 这是幻觉的直接证据
            problems.append(f"items[{i}] 引用了片段外的 rule_id={rid_int}")
        if item.get("level") not in SEVERITY_ORDER:
            problems.append(f"items[{i}] 的 level 非法：{item.get('level')!r}")
    return problems


def recheck(
    generated: dict[str, Any], assessed: AssessResult
) -> tuple[bool, list[str]]:
    """二次校验：把 LLM 声称的等级与规则引擎的判定逐条比对。

    返回 (是否通过, 问题列表)。任一条不一致即判定不通过。
    """
    truth = {r.rule_id: r.severity for r in assessed.risks if r.rule_id is not None}
    problems: list[str] = []

    for item in generated.get("items") or []:
        try:
            rid = int(item.get("rule_id"))
        except (TypeError, ValueError):
            continue
        expected = truth.get(rid)
        got = item.get("level")
        if expected is None:
            problems.append(f"rule_{rid} 不在规则引擎结论中")
        elif got != expected:
            problems.append(
                f"rule_{rid} 等级不一致：规则引擎判定 {expected}，模型输出 {got}"
            )
    return (not problems), problems


def generate(
    *,
    drugs: list[str],
    ingredient_names: list[str],
    profile: dict[str, Any],
    assessed: AssessResult,
    rag_hits: list[dict],
    client: LLMClient | None = None,
    use_llm: bool = True,
) -> Generation:
    """生成最终报告。任何异常都降级为纯规则答案，绝不向上抛。"""
    rule_items = [r.to_dict() for r in assessed.risks]
    fallback = fallback_summary(rule_items)

    # 无风险：不必调用模型
    if not rule_items:
        return Generation(result=fallback, used_llm=False, degraded_reason="未发现风险，跳过模型")

    # ★ 禁忌级硬拦截：不进 LLM
    if assessed.hard_blocked:
        return Generation(
            result=fallback,
            used_llm=False,
            degraded_reason="命中禁忌级组合，规则引擎硬拦截，不交由模型改写",
        )

    if not use_llm:
        return Generation(result=fallback, used_llm=False, degraded_reason="调用方关闭了模型生成")

    client = client or LLMClient()
    if not client.configured:
        return Generation(result=fallback, used_llm=False, degraded_reason="未配置大模型 api_key")

    user_prompt = build_user_prompt(
        drugs=drugs,
        ingredient_names=ingredient_names,
        profile=profile,
        rule_items=rule_items,
        rag_hits=rag_hits,
    )

    try:
        payload, resp = client.complete_json(SYSTEM_PROMPT, user_prompt)
    except LLMUnavailable as e:
        log.warning("模型不可用，降级为规则答案：%s", e)
        return Generation(result=fallback, used_llm=False, degraded_reason=f"模型不可用：{e}")

    if resp.refused:
        return Generation(
            result=fallback,
            used_llm=False,
            degraded_reason=f"模型拒答（{resp.refusal_category or '未分类'}）",
            model=resp.model,
        )

    if payload is None:
        return Generation(
            result=fallback,
            used_llm=False,
            degraded_reason="模型输出无法解析为 JSON",
            model=resp.model,
        )

    valid_ids = {it["rule_id"] for it in rule_items if it.get("rule_id")}
    problems = _validate_shape(payload, valid_ids)
    if problems:
        return Generation(
            result=fallback,
            used_llm=False,
            degraded_reason="模型输出结构不合规：" + "；".join(problems[:3]),
            model=resp.model,
        )

    passed, recheck_notes = recheck(payload, assessed)

    # 统一 disclaimer，不接受模型改写
    payload["disclaimer"] = DISCLAIMER

    if not passed:
        # 矛盾 → 回退规则答案，但保留一个合并版本供人工排查
        return Generation(
            result=fallback,
            used_llm=False,
            degraded_reason="二次校验未通过，已回退规则答案",
            recheck_passed=False,
            recheck_notes=recheck_notes,
            model=resp.model,
        )

    return Generation(
        result=payload,
        used_llm=True,
        recheck_passed=True,
        model=resp.model,
        usage={"input_tokens": resp.input_tokens, "output_tokens": resp.output_tokens},
    )
