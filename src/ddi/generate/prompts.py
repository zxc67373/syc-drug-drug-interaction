"""Prompt 构造。

全部约束都指向同一件事：**让模型的每一条结论都能被检索片段证实**。
模型只负责把结构化规则翻译成人话，不负责发现新的相互作用。
"""

from __future__ import annotations

import json
from typing import Any

SYSTEM_PROMPT = """你是联合用药安全评估助手，服务于药师和医师的辅助判断。

严格遵守以下约束：
1. 只能基于【检索片段】中的内容作答，禁止使用任何外部知识补充结论。
2. 不得给出片段中未提及的机制、后果或建议。片段中没有的，就不要说。
3. 每条结论必须标注其依据的 rule_id；没有 rule_id 的内容不得写入 items。
4. 【规则引擎结论】是由确定性规则计算得出的，**不可推翻、不可改写、不可降级**。
   你的任务是解释它，不是重新判断它。
   items 里的 level 必须**逐字照抄**规则引擎给出的英文枚举值
   （contraindicated / caution / monitor），不得换算、不得"更保守一点"、
   不得因为患者情况看起来更严重就升一级。等级由规则决定，不由你决定。
5. 输出严格的 JSON，不要输出任何解释性文字、markdown 围栏或前后缀。

语气要求：客观、谨慎、不夸大。这是医疗辅助信息，措辞必须保守。"""

OUTPUT_SCHEMA = """{
  "summary": "一句话总体结论（不超过 60 字）",
  "items": [
    {
      "rule_id": 12,
      "level": "contraindicated | caution | monitor",
      "explanation": "结合机制与患者情况的通俗解释（不超过 120 字）",
      "suggestion": "可执行的建议（不超过 100 字）"
    }
  ],
  "patient_note": "针对该患者画像（年龄/肝肾功能/过敏史）的特别提示；无则填 null",
  "disclaimer": "固定文案，原样返回"
}"""

DISCLAIMER = "本结果仅供用药参考，不能替代医师诊断与处方。如有不适请及时就医。"


def build_user_prompt(
    drugs: list[str],
    ingredient_names: list[str],
    profile: dict[str, Any],
    rule_items: list[dict],
    rag_hits: list[dict],
) -> str:
    """组装用户消息。

    规则引擎的命中结果与 RAG 补充片段分两段列出 —— 模型对二者的权限不同：
    前者是既成结论，后者是可选素材。
    """
    profile_lines = []
    if profile.get("age") is not None:
        profile_lines.append(f"- 年龄：{profile['age']} 岁")
    if profile.get("sex"):
        profile_lines.append(f"- 性别：{profile['sex']}")
    for key, label in (("hepatic", "肝功能"), ("renal", "肾功能")):
        val = profile.get(key)
        if val and val != "normal":
            cn = {"mild": "轻度异常", "moderate": "中度异常", "severe": "重度异常"}.get(val, val)
            profile_lines.append(f"- {label}：{cn}")
    if profile.get("pregnancy"):
        profile_lines.append("- 妊娠期：是")
    allergies = profile.get("allergies") or []
    if allergies:
        profile_lines.append(f"- 过敏史：{'、'.join(map(str, allergies))}")
    if not profile_lines:
        profile_lines.append("- 未提供特殊人群信息")

    # 等级同时给出中文和英文枚举值。
    # 只给中文（"关注"）会逼模型自己做一次翻译才填得出 level 字段 ——
    # 实测这会稳定产生 关注→caution 这类映射错误，然后被二次校验打回，
    # 白白浪费一次调用。把枚举值直接摆出来，模型照抄即可。
    rule_block = "\n".join(
        f"[rule_{it['rule_id']}] {it['title']} —— {it['level_cn']}"
        f"（level 字段必须原样填 \"{it['level']}\"）。"
        f"机制：{it.get('mechanism') or '—'}；"
        f"后果：{it.get('consequence') or '—'}；"
        f"建议：{it.get('suggestion') or '—'}"
        if it["kind"] == "ddi"
        else f"[rule_{it['rule_id']}] {it['title']} —— {it['level_cn']}"
             f"（level 字段必须原样填 \"{it['level']}\"）。{it.get('note') or ''}"
        for it in rule_items
    ) or "（无）"

    rag_block = "\n".join(
        f"[rule_{h['rule_id']}] {h['text']}" for h in rag_hits
    ) or "（无补充片段）"

    return f"""患者画像：
{chr(10).join(profile_lines)}

用户输入的药品：{'、'.join(drugs)}
归一化后的成分：{'、'.join(ingredient_names) if ingredient_names else '—'}

【规则引擎结论】（已确定，不可改写）
{rule_block}

【检索片段】（仅可引用，不可外推）
{rag_block}

【输出 JSON Schema】
{OUTPUT_SCHEMA}

disclaimer 字段必须原样返回：{DISCLAIMER}

请只输出 JSON。"""


def fallback_summary(items: list[dict]) -> dict[str, Any]:
    """不经过 LLM 时的纯规则答案。LLM 不可用/输出不合规时回退到这里。"""
    if not items:
        return {
            "summary": "未发现已知的相互作用风险。",
            "items": [],
            "patient_note": None,
            "disclaimer": DISCLAIMER,
        }

    top = items[0]
    counts: dict[str, int] = {}
    for it in items:
        counts[it["level_cn"]] = counts.get(it["level_cn"], 0) + 1
    dist = "、".join(f"{k} {v} 项" for k, v in counts.items())

    return {
        "summary": f"共发现 {len(items)} 项风险（{dist}），最高为{top['level_cn']}级。",
        "items": [
            {
                "rule_id": it["rule_id"],
                "level": it["level"],
                "explanation": "；".join(
                    x for x in [it.get("mechanism"), it.get("consequence")] if x
                )
                or (it.get("note") or ""),
                "suggestion": it.get("suggestion") or "",
            }
            for it in items
        ],
        "patient_note": None,
        "disclaimer": DISCLAIMER,
    }


def render_plain(result: dict[str, Any]) -> str:
    """把 JSON 结果渲染成纯文本，供不支持富文本的端使用。"""
    lines = [result.get("summary", ""), ""]
    for it in result.get("items", []):
        lines.append(f"[rule_{it.get('rule_id')}] {it.get('explanation', '')}")
        if it.get("suggestion"):
            lines.append(f"  建议：{it['suggestion']}")
    if result.get("patient_note"):
        lines.append(f"\n特别提示：{result['patient_note']}")
    lines.append(f"\n{result.get('disclaimer', DISCLAIMER)}")
    return "\n".join(lines)


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)
