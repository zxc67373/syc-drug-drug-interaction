"""种子 YAML → SQLite。

设计取舍：
- 全部走 UPSERT（幂等），重复跑不会炸，也不会产生脏数据
- 跨文件重复的规则会**报错退出**而不是静默覆盖 —— 重复通常意味着两份内容已经分叉，
  静默取后者会让其中一份的机制/建议悄悄丢失
- 引用了未收录成分的规则会**收集后统一报错**，而不是中途抛异常
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ddi.config import settings
from ddi.db.session import init_db, session

DDI_FILE_GLOB = "ddi_*.yaml"


@dataclass
class LoadReport:
    ingredients: int = 0
    drugs: int = 0
    drug_ingredients: int = 0
    ddi_rules: int = 0
    population_rules: int = 0
    sources: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"成分      {self.ingredients}",
            f"药品      {self.drugs}",
            f"药-成分   {self.drug_ingredients}",
            f"DDI 规则  {self.ddi_rules}",
            f"人群规则  {self.population_rules}",
            f"来源      {self.sources}",
        ]
        if self.warnings:
            lines.append(f"\n⚠️  警告 {len(self.warnings)} 条：")
            lines += [f"   - {w}" for w in self.warnings]
        if self.errors:
            lines.append(f"\n❌ 错误 {len(self.errors)} 条：")
            lines += [f"   - {e}" for e in self.errors]
        return "\n".join(lines)


def _pinyin(name: str) -> tuple[str, str]:
    """返回 (全拼, 首字母缩写)。pypinyin 缺失时退化为空串，不阻断加载。"""
    try:
        from pypinyin import lazy_pinyin, Style
    except ImportError:
        return "", ""
    full = "".join(lazy_pinyin(name))
    abbr = "".join(lazy_pinyin(name, style=Style.FIRST_LETTER))
    return full, abbr


def _read_yaml(path: Path) -> object:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _load_drugs(conn: sqlite3.Connection, path: Path, report: LoadReport) -> None:
    data = _read_yaml(path) or {}
    ing_ids: dict[str, int] = {}

    for ing in data.get("ingredients", []):
        cur = conn.execute(
            """INSERT INTO ingredient (name_cn, name_en, cas, atc, category)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(name_cn) DO UPDATE SET
                 name_en=excluded.name_en, atc=excluded.atc, category=excluded.category
               RETURNING id""",
            (
                ing["name_cn"],
                ing.get("name_en"),
                ing.get("cas"),
                ing.get("atc"),
                ing.get("category"),
            ),
        )
        ing_ids[ing["name_cn"]] = cur.fetchone()["id"]
        report.ingredients += 1

    for drug in data.get("drugs", []):
        name = drug["name_cn"]
        form = drug.get("dosage_form")
        full, abbr = _pinyin(name)
        cur = conn.execute(
            """INSERT INTO drug (name_cn, trade_names, aliases, pinyin, pinyin_abbr,
                                 dosage_form, is_otc, is_tcm, approval_no)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(name_cn, dosage_form) DO UPDATE SET
                 trade_names=excluded.trade_names, aliases=excluded.aliases,
                 pinyin=excluded.pinyin, pinyin_abbr=excluded.pinyin_abbr
               RETURNING id""",
            (
                name,
                json.dumps(drug.get("trade_names", []), ensure_ascii=False),
                json.dumps(drug.get("aliases", []), ensure_ascii=False),
                full,
                abbr,
                form,
                int(drug.get("is_otc", 0)),
                int(drug.get("is_tcm", 0)),
                drug.get("approval_no"),
            ),
        )
        drug_id = cur.fetchone()["id"]
        report.drugs += 1

        for comp in drug.get("ingredients", []):
            ing_name = comp["name"]
            if ing_name not in ing_ids:
                report.errors.append(f"药品「{name}」引用了未定义成分「{ing_name}」")
                continue
            conn.execute(
                """INSERT INTO drug_ingredient (drug_id, ingredient_id, strength)
                   VALUES (?, ?, ?)
                   ON CONFLICT(drug_id, ingredient_id) DO UPDATE SET strength=excluded.strength""",
                (drug_id, ing_ids[ing_name], comp.get("strength")),
            )
            report.drug_ingredients += 1


def _load_sources(conn: sqlite3.Connection, src: dict, report: LoadReport) -> int:
    cur = conn.execute(
        """INSERT INTO source (title, source_type, publisher, year, url)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(title, year) DO UPDATE SET source_type=excluded.source_type
           RETURNING id""",
        (
            src["title"],
            src.get("type"),
            src.get("publisher"),
            src.get("year"),
            src.get("url"),
        ),
    )
    report.sources += 1
    return cur.fetchone()["id"]


def _load_ddi(conn: sqlite3.Connection, seed_dir: Path, report: LoadReport) -> None:
    ing_rows = conn.execute("SELECT id, name_cn FROM ingredient").fetchall()
    ing_ids = {r["name_cn"]: r["id"] for r in ing_rows}

    seen: dict[tuple[int, int], str] = {}

    for path in sorted(seed_dir.glob(DDI_FILE_GLOB)):
        rules = _read_yaml(path) or []
        for rule in rules:
            a, b = rule["ing_a"], rule["ing_b"]
            if a not in ing_ids or b not in ing_ids:
                missing = a if a not in ing_ids else b
                report.errors.append(f"{path.name}: 未收录成分「{missing}」（{a} + {b}）")
                continue

            id_a, id_b = sorted((ing_ids[a], ing_ids[b]))
            key = (id_a, id_b)
            if key in seen:
                report.errors.append(
                    f"{path.name}: 规则「{a} + {b}」与 {seen[key]} 重复。"
                    f"请删除其中一处（勿静默覆盖，两份内容可能已分叉）"
                )
                continue
            seen[key] = path.name

            status = rule.get("status", "draft")
            cur = conn.execute(
                """INSERT INTO ddi_rule (ing_a_id, ing_b_id, severity, mechanism,
                                         consequence, suggestion, evidence_level, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(ing_a_id, ing_b_id) DO UPDATE SET
                     severity=excluded.severity, mechanism=excluded.mechanism,
                     consequence=excluded.consequence, suggestion=excluded.suggestion,
                     evidence_level=excluded.evidence_level
                   RETURNING id""",
                (
                    id_a,
                    id_b,
                    rule["severity"],
                    rule.get("mechanism"),
                    rule.get("consequence"),
                    rule.get("suggestion"),
                    rule.get("evidence_level"),
                    status,
                ),
            )
            rule_id = cur.fetchone()["id"]
            report.ddi_rules += 1

            for src in rule.get("sources", []) or []:
                if not src.get("title"):
                    continue
                source_id = _load_sources(conn, src, report)
                conn.execute(
                    """INSERT INTO rule_source (rule_id, source_id, excerpt)
                       VALUES (?, ?, ?)
                       ON CONFLICT(rule_id, source_id) DO UPDATE SET excerpt=excluded.excerpt""",
                    (rule_id, source_id, src.get("excerpt")),
                )


def _load_population(conn: sqlite3.Connection, path: Path, report: LoadReport) -> None:
    if not path.exists():
        report.warnings.append(f"未找到 {path.name}，跳过人群规则")
        return
    rules = _read_yaml(path) or []
    ing_ids = {
        r["name_cn"]: r["id"]
        for r in conn.execute("SELECT id, name_cn FROM ingredient").fetchall()
    }

    conn.execute("DELETE FROM population_rule")  # 全量重建，避免历史残留
    for rule in rules:
        name = rule["ingredient"]
        if name not in ing_ids:
            report.errors.append(f"{path.name}: 未收录成分「{name}」")
            continue
        conn.execute(
            """INSERT INTO population_rule
                 (ingredient_id, condition_type, condition_value, severity, note)
               VALUES (?, ?, ?, ?, ?)""",
            (
                ing_ids[name],
                rule["condition_type"],
                str(rule["condition_value"]),
                rule["severity"],
                rule.get("note"),
            ),
        )
        report.population_rules += 1


def load_all(
    seed_dir: Path | None = None, db: Path | None = None
) -> LoadReport:
    """加载全部种子数据。返回报告；report.errors 非空时应视为失败。"""
    seed_dir = seed_dir or settings.seed_dir
    report = LoadReport()
    init_db(db)

    with session(db) as conn:
        drugs_yaml = seed_dir / "drugs.yaml"
        if not drugs_yaml.exists():
            report.errors.append(f"缺少 {drugs_yaml}")
            return report
        _load_drugs(conn, drugs_yaml, report)
        _load_ddi(conn, seed_dir, report)
        _load_population(conn, seed_dir / "population.yaml", report)

    return report
