#!/usr/bin/env python
"""数据质量自检。

    python scripts/check_data.py
    python scripts/check_data.py --strict    # 有严重问题则退出码 1，可用于 CI

查的是"数据能不能对外"，不是"代码跑不跑得通"。这类问题不会让测试变红，
但会让产品在真实使用中出错 —— 所以单独查一遍。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ddi.config import settings  # noqa: E402
from ddi.console import setup_console  # noqa: E402
from ddi.db.session import session  # noqa: E402


class Check:
    def __init__(self) -> None:
        self.problems: list[tuple[str, str]] = []   # (级别, 说明)

    def fail(self, msg: str) -> None:
        self.problems.append(("严重", msg))

    def warn(self, msg: str) -> None:
        self.problems.append(("提示", msg))


def run(db_path: Path) -> Check:
    c = Check()
    if not db_path.exists():
        c.fail(f"数据库不存在：{db_path}（先跑 python scripts/load_seed.py）")
        return c

    with session(db_path) as conn:
        # ── 1. 规则状态：草稿不得对外 ──────────────────
        rows = conn.execute(
            "SELECT status, COUNT(*) n FROM ddi_rule GROUP BY status"
        ).fetchall()
        by_status = {r["status"]: r["n"] for r in rows}
        total = sum(by_status.values())
        print("规则状态分布：")
        for st in ("draft", "reviewed", "published"):
            print(f"  {st:<10} {by_status.get(st, 0)}")
        if by_status.get("published", 0) == 0:
            c.warn(f"全部 {total} 条规则均为 draft，未经执业药师核验 —— 不得对外提供服务")

        # ── 2. 溯源覆盖 ────────────────────────────────
        with_src = conn.execute(
            "SELECT COUNT(DISTINCT rule_id) n FROM rule_source"
        ).fetchone()["n"]
        missing = total - with_src
        pct = (with_src / total * 100) if total else 0
        print(f"\n溯源覆盖：{with_src}/{total}（{pct:.1f}%）")
        if missing:
            c.fail(
                f"{missing} 条规则没有 rule_source 记录 —— "
                f"这些规则在前端会显示「暂无溯源依据」"
            )

        # ── 3. 必填文本字段 ────────────────────────────
        for col in ("mechanism", "consequence", "suggestion"):
            n = conn.execute(
                f"SELECT COUNT(*) n FROM ddi_rule WHERE {col} IS NULL OR TRIM({col})=''"
            ).fetchone()["n"]
            if n:
                c.fail(f"{n} 条规则缺 {col}")

        # ── 4. 人群规则的条件 ──────────────────────────
        n = conn.execute(
            "SELECT COUNT(*) n FROM population_rule "
            "WHERE condition_type IS NULL OR condition_value IS NULL"
        ).fetchone()["n"]
        if n:
            c.fail(f"{n} 条人群规则缺条件")

        # ── 5. 药品基础信息 ────────────────────────────
        n = conn.execute(
            "SELECT COUNT(*) n FROM drug WHERE approval_no IS NULL OR TRIM(approval_no)=''"
        ).fetchone()["n"]
        tot_drug = conn.execute("SELECT COUNT(*) n FROM drug").fetchone()["n"]
        print(f"\n药品批准文号：{tot_drug - n}/{tot_drug} 已填")
        if n:
            c.warn(f"{n}/{tot_drug} 个药品没有国药准字 —— 对外展示前应补齐核验")

        # ── 6. 孤儿药品（没有任何成分）─────────────────
        n = conn.execute(
            "SELECT COUNT(*) n FROM drug d "
            "WHERE NOT EXISTS (SELECT 1 FROM drug_ingredient di WHERE di.drug_id=d.id)"
        ).fetchone()["n"]
        if n:
            c.fail(f"{n} 个药品没有关联任何成分 —— 它们永远不会触发任何规则")

        # ── 7. 严重度分布 ──────────────────────────────
        rows = conn.execute(
            "SELECT severity, COUNT(*) n FROM ddi_rule GROUP BY severity"
        ).fetchall()
        print("\n严重度分布：")
        for r in rows:
            print(f"  {r['severity']:<16} {r['n']}")

        # ── 8. 自相互作用（同一成分自己配自己）──────────
        n = conn.execute("SELECT COUNT(*) n FROM ddi_rule WHERE ing_a_id = ing_b_id").fetchone()["n"]
        if n:
            c.fail(f"{n} 条规则的 ing_a 与 ing_b 是同一成分")

    return c


def main() -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="数据质量自检")
    ap.add_argument("--strict", action="store_true", help="有严重问题则返回非零退出码")
    ap.add_argument("--db", type=Path, default=None)
    args = ap.parse_args()

    db = args.db or settings.db_path
    print(f"数据库：{db}\n")

    c = run(db)

    print()
    if not c.problems:
        print("✅ 未发现问题")
        return 0

    for level, msg in c.problems:
        mark = "❌" if level == "严重" else "⚠️ "
        print(f"{mark} [{level}] {msg}")

    severe = sum(1 for lv, _ in c.problems if lv == "严重")
    print(f"\n共 {severe} 项严重、{len(c.problems) - severe} 项提示。")
    if args.strict and severe:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
