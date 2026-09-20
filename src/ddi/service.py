"""端到端编排：归一化 → 规则引擎 → RAG → 生成 → 溯源。

这是 API 层唯一需要接触的入口。所有降级逻辑都在这里收敛，
API 层拿到的永远是一个结构完整的报告。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ddi.generate.generator import Generation, generate
from ddi.generate.llm_client import LLMClient
from ddi.generate.prompts import DISCLAIMER
from ddi.normalize import Normalizer
from ddi.rag import Retriever
from ddi.rules import RuleEngine
from ddi.rules.engine import SEVERITY_CN

log = logging.getLogger(__name__)


@dataclass
class DrugInput:
    raw: str
    drug_id: int | None = None
    name_cn: str | None = None
    confidence: float = 0.0
    match_type: str = "unmatched"
    needs_confirmation: bool = False
    candidates: list[dict] = field(default_factory=list)


@dataclass
class Report:
    overall_risk: str
    hard_blocked: bool
    items: list[dict]
    summary: str
    patient_note: str | None
    disclaimer: str
    drugs: list[DrugInput]
    ingredients: list[str]
    engine: dict[str, Any]
    elapsed_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "overall_risk": self.overall_risk,
            "overall_risk_cn": SEVERITY_CN.get(self.overall_risk, "未发现"),
            "hard_blocked": self.hard_blocked,
            "summary": self.summary,
            "patient_note": self.patient_note,
            "items": self.items,
            "drugs": [d.__dict__ for d in self.drugs],
            "ingredients": self.ingredients,
            "engine": self.engine,
            "elapsed_ms": self.elapsed_ms,
            "disclaimer": self.disclaimer,
        }


class DDIService:
    def __init__(
        self,
        normalizer: Normalizer | None = None,
        engine: RuleEngine | None = None,
        retriever: Retriever | None = None,
        llm: LLMClient | None = None,
    ):
        self.normalizer = normalizer or Normalizer()
        self.engine = engine or RuleEngine()
        self._retriever = retriever
        self.llm = llm or LLMClient()

    @property
    def retriever(self) -> Retriever | None:
        """索引可能尚未构建 —— 检索是增强项，缺了不应阻断评估。"""
        if self._retriever is None:
            try:
                r = Retriever()
                if r.ready:
                    r.load()
                    self._retriever = r
            except Exception as e:  # noqa: BLE001
                log.warning("检索索引不可用，跳过 RAG：%s", e)
                return None
        return self._retriever

    def warmup(self) -> float:
        """服务启动时调用：预载索引与嵌入模型，返回耗时（秒）。

        嵌入模型冷启动约 40 秒。不在启动时付这个成本，就得让第一个
        发请求的用户付 —— 而他会以为服务挂了。返回 None 表示检索不可用。
        """
        r = self.retriever
        if r is None:
            return 0.0
        return r.warmup()

    # ── 主流程 ───────────────────────────────────────
    def assess(
        self,
        drugs: list[str],
        profile: dict[str, Any] | None = None,
        *,
        use_llm: bool = True,
        use_rag: bool = True,
        top_k: int = 5,
    ) -> Report:
        started = time.perf_counter()
        profile = dict(profile or {})

        # 1) 归一化
        results = self.normalizer.normalize_many(drugs)
        drug_inputs: list[DrugInput] = []
        resolved_ids: list[int] = []
        for raw, res in zip(drugs, results):
            if res.ok and res.matched:
                m = res.matched
                resolved_ids.append(m.drug_id)
                drug_inputs.append(
                    DrugInput(
                        raw=raw,
                        drug_id=m.drug_id,
                        name_cn=m.name_cn,
                        confidence=m.confidence,
                        match_type=m.match_type,
                        needs_confirmation=m.needs_confirmation,
                        candidates=[c.__dict__ for c in res.candidates[:5]],
                    )
                )
            else:
                drug_inputs.append(
                    DrugInput(
                        raw=raw,
                        candidates=[c.__dict__ for c in res.candidates[:5]],
                    )
                )

        # 2) 成分展开（规则挂在成分上）
        ingredient_ids = self.normalizer.ingredients_of(resolved_ids)
        ingredient_names = [
            self.engine._ing_names.get(i, str(i)) for i in ingredient_ids
        ]

        # 3) 规则引擎（硬拦截在此发生）
        assessed = self.engine.assess(ingredient_ids, profile)

        # 4) RAG 补充检索
        rag_hits: list[dict] = []
        if use_rag and ingredient_names:
            retriever = self.retriever
            if retriever is not None:
                try:
                    hits = retriever.retrieve_excluding(
                        "、".join(ingredient_names) + " 联合用药 相互作用 风险",
                        exclude_ids={r.rule_id for r in assessed.risks if r.rule_id},
                        top_k=top_k,
                    )
                    rag_hits = [h.to_dict() for h in hits]
                except Exception as e:  # noqa: BLE001
                    log.warning("RAG 检索失败，跳过：%s", e)

        # 5) 生成（含二次校验与降级）
        gen: Generation = generate(
            drugs=drugs,
            ingredient_names=ingredient_names,
            profile=profile,
            assessed=assessed,
            rag_hits=rag_hits,
            client=self.llm,
            use_llm=use_llm,
        )

        # 6) 溯源：把 rule_id 映射回 source.excerpt
        items = self._attach_sources(gen.result.get("items") or [], assessed)

        elapsed = int((time.perf_counter() - started) * 1000)
        return Report(
            overall_risk=assessed.overall_risk,
            hard_blocked=assessed.hard_blocked,
            items=items,
            summary=gen.result.get("summary", ""),
            patient_note=gen.result.get("patient_note"),
            disclaimer=gen.result.get("disclaimer", DISCLAIMER),
            drugs=drug_inputs,
            ingredients=ingredient_names,
            engine={
                "used_llm": gen.used_llm,
                "degraded_reason": gen.degraded_reason,
                "recheck_passed": gen.recheck_passed,
                "recheck_notes": gen.recheck_notes,
                "model": gen.model,
                "usage": gen.usage,
                "rag_hits": len(rag_hits),
            },
            elapsed_ms=elapsed,
        )

    def _attach_sources(self, items: list[dict], assessed) -> list[dict]:
        """把规则库的权威文本与溯源挂到每条结论上。

        **字段归属是刻意划分的**，不是随手 merge：

        - ``title`` / ``mechanism`` / ``consequence`` / ``suggestion`` / ``sources``
          —— 一律取自**规则库**。这几项是药师要照着执行的内容，必须来自
          受审核的规则文本，不能是模型现场措辞。
        - ``explanation`` —— 取自模型（或降级时的规则拼接）。它是同一件事的
          通俗复述，用于展示，不用于决策。

        这样无论走 LLM 还是硬拦截降级，前端拿到的条目结构完全一致 ——
        否则禁忌级（最重要的一条路径）反而会缺字段。
        - ``level`` / ``level_cn`` —— 取自**规则引擎**，不取模型输出的值。
          二次校验本来就会把不一致的答案整体打回，所以正常情况下两者相同；
          这里再取一次规则值，是为了让"等级由规则决定"成为**结构上的事实**，
          而不是依赖校验逻辑没有漏洞。
        """
        by_rule = {r.rule_id: r for r in assessed.risks if r.rule_id is not None}
        out: list[dict] = []
        for it in items:
            try:
                rid = int(it.get("rule_id"))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                out.append(it)
                continue
            risk = by_rule.get(rid)
            level = risk.severity if risk else it.get("level")
            merged = {
                **it,
                "level": level,
                "level_cn": SEVERITY_CN.get(level, level),
                "title": (risk.title if risk else "") or it.get("title", ""),
                "drugs": risk.ingredient_names if risk else it.get("drugs", []),
                "mechanism": risk.mechanism if risk else it.get("mechanism"),
                "consequence": risk.consequence if risk else it.get("consequence"),
                # 规则库有建议就用规则库的；没有才退回模型写的
                "suggestion": (risk.suggestion if risk else None) or it.get("suggestion"),
                "evidence_level": risk.evidence_level if risk else it.get("evidence_level"),
                "note": risk.condition_note if risk else it.get("note"),
                "sources": risk.sources if risk else [],
            }
            out.append(merged)
        return out

    def normalize_only(self, drugs: list[str]) -> list[dict]:
        """调试用：只跑归一化，返回可 JSON 序列化的结果。"""
        out: list[dict] = []
        for raw, res in zip(drugs, self.normalizer.normalize_many(drugs)):
            out.append(
                {
                    "query": raw,
                    "matched": (
                        {
                            "drug_id": res.matched.drug_id,
                            "name_cn": res.matched.name_cn,
                            "confidence": res.matched.confidence,
                            "match_type": res.matched.match_type,
                            "ingredients": res.matched.ingredient_names,
                            "needs_confirmation": res.matched.needs_confirmation,
                        }
                        if res.matched
                        else None
                    ),
                    "candidates": [c.__dict__ for c in res.candidates[:5]],
                    "notes": res.notes,
                }
            )
        return out
