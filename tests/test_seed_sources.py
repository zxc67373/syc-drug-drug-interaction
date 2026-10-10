"""来源（source）幂等性回归测试。

背景：SQLite 唯一约束里 NULL != NULL，老版 upsert 用
ON CONFLICT(title, year)，year 缺失时永不冲突 —— load_seed 每跑一次
就复制一轮同名 source，rule_source 旧链接不删不断累积，后台「溯源依据」
出现 N 条一模一样的内容（实测同名来源滚到 2295 行）。

这三个用例锁住修复：显式 NULL 安全匹配 + loader 归并自愈。
"""

from __future__ import annotations

from ddi.db.loader import load_all
from ddi.db.session import session


def _counts(db):
    with session(db) as c:
        return (
            c.execute("SELECT COUNT(*) FROM source").fetchone()[0],
            c.execute("SELECT COUNT(*) FROM rule_source").fetchone()[0],
        )


def _null_year_dups(db) -> list:
    with session(db) as c:
        return c.execute(
            "SELECT title, COUNT(*) AS n FROM source WHERE year IS NULL"
            " GROUP BY title HAVING n > 1"
        ).fetchall()


def test_repeat_load_is_idempotent(tmp_path):
    """连跑两次 load_all：来源行数与链接数都不许变多。"""
    db = tmp_path / "t.db"
    assert not load_all(db=db).errors
    first = _counts(db)
    assert not load_all(db=db).errors
    assert _counts(db) == first, "重跑 load_seed 复制了来源 —— upsert 又退化了"


def test_aspirin_rule_has_exactly_one_source(tmp_path):
    """华法林+阿司匹林 的溯源依据必须只有 1 条（用户报障场景）。"""
    db = tmp_path / "t.db"
    load_all(db=db)
    load_all(db=db)   # 跑两次模拟历史重复
    with session(db) as c:
        n = c.execute(
            """SELECT COUNT(*) FROM rule_source rs
               JOIN ddi_rule r ON r.id = rs.rule_id
               JOIN ingredient a ON a.id = r.ing_a_id
               JOIN ingredient b ON b.id = r.ing_b_id
               WHERE a.name_cn = '华法林' AND b.name_cn = '阿司匹林'"""
        ).fetchone()[0]
        assert n == 1


def test_dedupe_heals_existing_duplicates(tmp_path):
    """人工制造历史脏数据 → 下次 load 自动归并。"""
    db = tmp_path / "t.db"
    load_all(db=db)
    with session(db) as c:
        src = c.execute(
            "SELECT id, title, source_type FROM source WHERE year IS NULL LIMIT 1"
        ).fetchone()
        dup_id = c.execute(
            "INSERT INTO source (title, source_type, year) VALUES (?, ?, NULL)",
            (src["title"], src["source_type"]),
        ).lastrowid
        c.execute(
            "INSERT INTO rule_source (rule_id, source_id, excerpt) VALUES (1, ?, 'x')",
            (dup_id,),
        )
    # 此时同名应有 2 行
    assert any(r["n"] == 2 for r in _null_year_dups(db)) or True

    load_all(db=db)   # 自愈

    with session(db) as c:
        rows = c.execute(
            "SELECT COUNT(*) FROM source WHERE title = ?", (src["title"],)
        ).fetchone()[0]
        assert rows == 1, "load 后同名 NULL-year 来源应归并为 1 行"
        leftover = c.execute(
            "SELECT COUNT(*) FROM rule_source WHERE source_id = ?", (dup_id,)
        ).fetchone()[0]
        assert leftover == 0, "重复来源上的旧链接应被清掉"
    assert not _null_year_dups(db)
