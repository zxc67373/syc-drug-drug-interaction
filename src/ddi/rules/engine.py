"""M2 规则引擎。

职责：把「一组成分 + 患者画像」映射为「风险列表 + 是否硬拦截」。

关键设计：
- **硬拦截发生在 LLM 之前**。命中禁忌级时直接返回，LLM 无权改写结论。
- 确定性优先：同样的输入永远得到同样的输出，与是否开启 LLM 无关。
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from itertools import combinations

from ddi.db.session import session

SEVERITY_ORDER = {"contraindicated": 0, "caution": 1, "monitor": 2}
SEVERITY_CN = {"contraindicated": "禁忌", "caution": "慎用", "monitor": "关注"}

# 肝肾功能分级，用于「至少达到该严重度」的判断
RENAL_HEPATIC_ORDER = {"normal": 0, "mild": 1, "moderate": 2, "severe": 3}

_CMP_RE = re.compile(r"^\s*(>=|<=|>|<|=)\s*(\d+)\s*$")


@dataclass
class Risk:
    kind: str  # 'ddi' | 'population'
    severity: str
    title: str
    mechanism: str | None = None
    consequence: str | None = None
    suggestion: str | None = None
    evidence_level: str | None = None
    rule_id: int | None = None
    ingredient_ids: list[int] = field(default_factory=list)
    ingredient_names: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    # population 专用
    condition_note: str | None = None

    @property
    def severity_cn(self) -> str:
        return SEVERITY_CN.get(self.severity, self.severity)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "rule_id": self.rule_id,
            "level": self.severity,
            "level_cn": self.severity_cn,
            "title": self.title,
            "drugs": self.ingredient_names,
            "mechanism": self.mechanism,
            "consequence": self.consequence,
            "suggestion": self.suggestion,
            "evidence_level": self.evidence_level,
            "note": self.condition_note,
            "sources": self.sources,
        }


@dataclass
class AssessResult:
    risks: list[Risk]
    hard_blocked: bool
    overall_risk: str  # 最高严重度，无风险时 'none'

    def to_dict(self) -> dict:
        return {
            "overall_risk": self.overall_risk,
            "overall_risk_cn": SEVERITY_CN.get(self.overall_risk, "未发现"),
            "hard_blocked": self.hard_blocked,
            "items": [r.to_dict() for r in self.risks],
        }


def _match_condition(condition_type: str, condition_value: str, profile: dict) -> bool:
    """判断患者画像是否满足某条人群禁忌的触发条件。"""
    if condition_type == "age":
        age = profile.get("age")
        if age is None:
            return False
        m = _CMP_RE.match(condition_value)
        if not m:
            return False
        op, val = m.group(1), int(m.group(2))
        return {
            ">=": age >= val, "<=": age <= val,
            ">": age > val, "<": age < val, "=": age == val,
        }[op]

    if condition_type in ("hepatic", "renal"):
        level = RENAL_HEPATIC_ORDER.get(str(profile.get(condition_type, "normal")).lower(), 0)
        need = RENAL_HEPATIC_ORDER.get(str(condition_value).lower(), 99)
        return level >= need and level > 0

    if condition_type == "pregnancy":
        return bool(profile.get("pregnancy")) and str(condition_value) == "1"

    if condition_type == "allergy":
        allergies = profile.get("allergies") or []
        return any(str(condition_value) in str(a) for a in allergies)

    return False


class RuleEngine:
    def __init__(self, conn: sqlite3.Connection | None = None):
        if conn is not None:
            self._preload(conn)
        else:
            with session() as c:
                self._preload(c)

    def _preload(self, conn: sqlite3.Connection) -> None:
        self._ing_names: dict[int, str] = {
            r["id"]: r["name_cn"]
            for r in conn.execute("SELECT id, name_cn FROM ingredient")
        }

        self._ddi: dict[tuple[int, int], dict] = {}
        for r in conn.execute("SELECT * FROM ddi_rule"):
            self._ddi[(r["ing_a_id"], r["ing_b_id"])] = dict(r)
            self._ddi.setdefault((r["ing_b_id"], r["ing_a_id"]), dict(r))

        self._sources: dict[int, list[dict]] = {}
        for r in conn.execute(
            """SELECT rs.rule_id, rs.excerpt, s.title, s.source_type, s.publisher,
                      s.year, s.url
               FROM rule_source rs JOIN source s ON s.id = rs.source_id"""
        ):
            self._sources.setdefault(r["rule_id"], []).append(
                {
                    "title": r["title"],
                    "type": r["source_type"],
                    "publisher": r["publisher"],
                    "year": r["year"],
                    "url": r["url"],
                    "excerpt": r["excerpt"],
                }
            )

        self._pop: dict[int, list[dict]] = {}
        for r in conn.execute("SELECT * FROM population_rule"):
            self._pop.setdefault(r["ingredient_id"], []).append(dict(r))

    # ── 主入口 ───────────────────────────────────────
    def assess(self, ingredient_ids: list[int], profile: dict | None = None) -> AssessResult:
        profile = profile or {}
        uniq = sorted(set(ingredient_ids))
        risks: list[Risk] = []

        # 1) 两两配对查 DDI：n 种成分 → C(n,2) 对
        for a, b in combinations(uniq, 2):
            rule = self._ddi.get((a, b))
            if not rule:
                continue
            risks.append(
                Risk(
                    kind="ddi",
                    severity=rule["severity"],
                    title=f"{self._ing_names.get(a, a)} + {self._ing_names.get(b, b)}",
                    mechanism=rule["mechanism"],
                    consequence=rule["consequence"],
                    suggestion=rule["suggestion"],
                    evidence_level=rule["evidence_level"],
                    rule_id=rule["id"],
                    ingredient_ids=[a, b],
                    ingredient_names=[
                        self._ing_names.get(a, str(a)),
                        self._ing_names.get(b, str(b)),
                    ],
                    sources=self._sources.get(rule["id"], []),
                )
            )

        # 2) 人群禁忌
        for ing_id in uniq:
            for p in self._pop.get(ing_id, []):
                if not _match_condition(p["condition_type"], p["condition_value"], profile):
                    continue
                name = self._ing_names.get(ing_id, str(ing_id))
                risks.append(
                    Risk(
                        kind="population",
                        severity=p["severity"],
                        title=f"{name}（{_condition_cn(p['condition_type'], p['condition_value'], profile)}）",
                        mechanism=None,
                        consequence=None,
                        suggestion=p["note"],
                        rule_id=p["id"],
                        ingredient_ids=[ing_id],
                        ingredient_names=[name],
                        condition_note=p["note"],
                    )
                )

        # 3) 分级排序
        risks.sort(key=lambda r: (SEVERITY_ORDER.get(r.severity, 9), r.title))

        # 4) ★ 硬拦截
        hard_blocked = any(r.severity == "contraindicated" for r in risks)
        overall = risks[0].severity if risks else "none"

        return AssessResult(risks=risks, hard_blocked=hard_blocked, overall_risk=overall)

    def get_rule(self, rule_id: int) -> dict | None:
        for (a, b), rule in self._ddi.items():
            if rule["id"] == rule_id:
                return {
                    **rule,
                    "ing_a_name": self._ing_names.get(rule["ing_a_id"]),
                    "ing_b_name": self._ing_names.get(rule["ing_b_id"]),
                    "sources": self._sources.get(rule_id, []),
                }
        return None

    @property
    def rule_count(self) -> int:
        return len({r["id"] for r in self._ddi.values()})


def _condition_cn(ctype: str, cvalue: str, profile: dict) -> str:
    if ctype == "age":
        return f"年龄 {cvalue} 岁"
    if ctype == "hepatic":
        return {"mild": "肝功能轻度异常", "moderate": "肝功能中度异常", "severe": "肝功能重度异常"}.get(
            cvalue, f"肝功能{cvalue}"
        )
    if ctype == "renal":
        return {"mild": "肾功能轻度异常", "moderate": "肾功能中度异常", "severe": "肾功能重度异常"}.get(
            cvalue, f"肾功能{cvalue}"
        )
    if ctype == "pregnancy":
        return "妊娠期"
    if ctype == "allergy":
        return f"{cvalue}过敏史"
    return ctype
