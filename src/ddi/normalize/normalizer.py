"""M1 药品名归一化。

核心原则：**宁可让用户确认，不要猜。** 猜错药名 = 整个评估失效。

匹配优先级（命中即停，置信度递减）：
    1.00  原名精确匹配 name_cn / trade_names / aliases
    0.98  清洗后（去规格、去剂型）精确匹配
    0.95  拼音全拼精确匹配
    0.92  拼音首字母精确匹配
    0.60–0.90  rapidfuzz 模糊匹配（≥85 视为可用）
    <0.85 不猜 —— 返回候选列表让用户确认
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Iterable

from ddi.db.session import session

# 剂型后缀，按长度降序排列，保证"缓释胶囊"先于"胶囊"被剥掉
DOSAGE_SUFFIXES = sorted(
    [
        "缓释胶囊", "缓释片", "控释片", "控释胶囊", "肠溶胶囊", "肠溶片",
        "分散片", "咀嚼片", "泡腾片", "软胶囊", "薄膜衣片", "糖衣片",
        "口腔崩解片", "滴丸", "注射剂", "注射液", "喷雾剂", "气雾剂",
        "混悬液", "口服液", "糖浆", "颗粒", "胶囊", "片剂", "片",
        "丸", "散", "膏", "栓", "凝胶", "乳膏", "贴", "滴眼液", "滴鼻液",
    ],
    key=len,
    reverse=True,
)

# 规格：100mg / 0.3g / 50μg / 10ml / 5万单位 / 0.1%
STRENGTH_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:mg|g|μg|ug|mcg|ml|IU|iu|万单位|单位|%)",
    re.IGNORECASE,
)
# 括号及其内容：阿司匹林(肠溶) -> 阿司匹林
PAREN_RE = re.compile(r"[（(][^）)]*[）)]")
# 顿号/逗号后的规格说明
TAIL_RE = re.compile(r"[,，、;；]\s*\d.*$")

FUZZY_ACCEPT = 85.0  # rapidfuzz 阈值，低于此值必须让用户确认


@dataclass
class Match:
    drug_id: int
    name_cn: str
    confidence: float
    match_type: str
    ingredient_ids: list[int] = field(default_factory=list)
    ingredient_names: list[str] = field(default_factory=list)
    is_tcm: bool = False

    @property
    def needs_confirmation(self) -> bool:
        return self.confidence < FUZZY_ACCEPT / 100


@dataclass
class NormalizeResult:
    query: str
    matched: Match | None = None
    candidates: list[Match] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.matched is not None

    @property
    def needs_confirmation(self) -> bool:
        return self.ok and self.matched.needs_confirmation  # type: ignore[union-attr]


def clean_name(raw: str) -> str:
    """去规格、去剂型后缀、去括号内容、去空白。中英文混排也能处理。"""
    s = raw.strip()
    s = PAREN_RE.sub("", s)
    s = TAIL_RE.sub("", s)
    s = STRENGTH_RE.sub("", s)
    s = re.sub(r"\s+", "", s)
    for suffix in DOSAGE_SUFFIXES:
        if s.endswith(suffix) and len(s) > len(suffix):
            s = s[: -len(suffix)]
            break
    return s


def _to_pinyin(name: str) -> tuple[str, str]:
    try:
        from pypinyin import lazy_pinyin, Style
    except ImportError:
        return "", ""
    return (
        "".join(lazy_pinyin(name)).lower(),
        "".join(lazy_pinyin(name, style=Style.FIRST_LETTER)).lower(),
    )


class Normalizer:
    """把数据库读进内存建索引。药品量级在万级以内，全量加载比每次查库快得多，
    也避免模糊匹配时反复回表。"""

    def __init__(self, conn: sqlite3.Connection | None = None):
        self._by_name: dict[str, Match] = {}
        self._by_pinyin: dict[str, Match] = {}
        self._by_abbr: dict[str, Match] = {}
        self._all: list[Match] = []
        self._load(conn)

    def _load(self, conn: sqlite3.Connection | None) -> None:
        import json

        def build(c: sqlite3.Connection) -> None:
            ing_map: dict[int, list[tuple[int, str]]] = {}
            for row in c.execute(
                """SELECT di.drug_id, i.id AS ing_id, i.name_cn AS ing_name
                   FROM drug_ingredient di JOIN ingredient i ON i.id = di.ingredient_id"""
            ):
                ing_map.setdefault(row["drug_id"], []).append(
                    (row["ing_id"], row["ing_name"])
                )

            for row in c.execute(
                "SELECT id, name_cn, trade_names, aliases, pinyin, pinyin_abbr, is_tcm FROM drug"
            ):
                comps = ing_map.get(row["id"], [])
                m = Match(
                    drug_id=row["id"],
                    name_cn=row["name_cn"],
                    confidence=1.0,
                    match_type="exact",
                    ingredient_ids=[c_[0] for c_ in comps],
                    ingredient_names=[c_[1] for c_ in comps],
                    is_tcm=bool(row["is_tcm"]),
                )
                self._all.append(m)

                names = [row["name_cn"], clean_name(row["name_cn"])]
                names += json.loads(row["trade_names"] or "[]")
                names += json.loads(row["aliases"] or "[]")
                for n in names:
                    if n:
                        self._by_name.setdefault(n, m)
                        self._by_name.setdefault(clean_name(n), m)

                if row["pinyin"]:
                    self._by_pinyin.setdefault(row["pinyin"].lower(), m)
                if row["pinyin_abbr"]:
                    self._by_abbr.setdefault(row["pinyin_abbr"].lower(), m)

        if conn is not None:
            build(conn)
        else:
            with session() as c:
                build(c)

    # ── 匹配 ─────────────────────────────────────────
    def normalize(self, raw: str, top_k: int = 5) -> NormalizeResult:
        from rapidfuzz import fuzz, process

        q = raw.strip()
        res = NormalizeResult(query=raw)
        if not q:
            res.notes.append("药品名为空")
            return res

        # 1) 原名精确
        if q in self._by_name:
            m = self._by_name[q]
            res.matched = Match(**{**m.__dict__, "confidence": 1.0, "match_type": "exact"})
            return self._finish(res)

        # 2) 清洗后精确
        cleaned = clean_name(q)
        if cleaned and cleaned in self._by_name:
            m = self._by_name[cleaned]
            res.matched = Match(
                **{**m.__dict__, "confidence": 0.98, "match_type": "cleaned"}
            )
            return self._finish(res)

        # 3) 拼音
        full, abbr = _to_pinyin(cleaned or q)
        if full and full in self._by_pinyin:
            m = self._by_pinyin[full]
            res.matched = Match(
                **{**m.__dict__, "confidence": 0.95, "match_type": "pinyin"}
            )
            return self._finish(res)
        if abbr and abbr in self._by_abbr:
            m = self._by_abbr[abbr]
            res.matched = Match(
                **{**m.__dict__, "confidence": 0.92, "match_type": "pinyin_abbr"}
            )
            return self._finish(res)

        # 4) 模糊匹配：中文按字符相似度，拼音按完整度
        pool = list(self._by_name.keys())
        scored: dict[int, tuple[float, Match, str]] = {}

        for key, score, _idx in process.extract(
            cleaned or q, pool, scorer=fuzz.ratio, limit=top_k * 3
        ):
            match = self._by_name[key]
            s = float(score)
            cur = scored.get(match.drug_id)
            if cur is None or s > cur[0]:
                scored[match.drug_id] = (s, match, key)

        if full:
            py_pool = list(self._by_pinyin.keys())
            for key, score, _idx in process.extract(
                full, py_pool, scorer=fuzz.ratio, limit=top_k * 3
            ):
                m = self._by_pinyin[key]
                s = float(score) * 0.95  # 拼音命中略降权
                cur = scored.get(m.drug_id)
                if cur is None or s > cur[0]:
                    scored[m.drug_id] = (s, m, key)

        ranked = sorted(scored.values(), key=lambda t: -t[0])[:top_k]
        res.candidates = [
            Match(**{**m.__dict__, "confidence": round(s / 100, 2), "match_type": "fuzzy"})
            for s, m, _k in ranked
        ]

        if res.candidates and res.candidates[0].confidence >= FUZZY_ACCEPT / 100:
            best = res.candidates[0]
            res.matched = best
            if best.confidence < 0.95:
                res.notes.append(
                    f"「{raw}」按模糊匹配识别为「{best.name_cn}」"
                    f"（相似度 {best.confidence:.0%}），建议向用户确认"
                )
        else:
            res.notes.append(f"未能可靠识别「{raw}」，请从候选中确认或重新输入")

        return self._finish(res)

    def _finish(self, res: NormalizeResult) -> NormalizeResult:
        if res.matched and not res.candidates:
            res.candidates = [res.matched]
        if res.matched and res.matched.is_tcm:
            res.notes.append(
                f"「{res.matched.name_cn}」为中成药，已按成分表拆解为 "
                f"{len(res.matched.ingredient_ids)} 个成分；成分表需人工维护，请核实"
            )
        return res

    def normalize_many(self, names: Iterable[str]) -> list[NormalizeResult]:
        return [self.normalize(n) for n in names]

    def ingredients_of(self, drug_ids: Iterable[int]) -> list[int]:
        """把药品 id 集合展开为去重后的成分 id 列表。"""
        wanted = set(drug_ids)
        out: set[int] = set()
        for m in self._all:
            if m.drug_id in wanted:
                out.update(m.ingredient_ids)
        return sorted(out)

    @property
    def size(self) -> int:
        return len(self._all)
