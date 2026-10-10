"""source / rule_source 的写入（loader 与管理后台共用）。

原来这两段 UPSERT SQL 只存在于 loader 里；管理后台"智能导入确认"也需要
同样的幂等语义（同 (title, year) 复用同一来源行），所以提出来共享。

幂等语义（与 loader 原行为一致，勿擅自扩展）：
- source 按 UNIQUE(title, year) 冲突时**只更新 source_type**，
  不覆盖 publisher / year / url —— 后写者不该抹掉先写者的字段。
- rule_source 按 PK 冲突时更新 excerpt。
- 注意 SQLite 里 NULL 不参与 UNIQUE 等值判断：year 为空时同名会各建一行。
  这是既有行为，不在此处发明假年份去绕。
"""

from __future__ import annotations

import sqlite3
from typing import Any


def upsert_source(conn: sqlite3.Connection, src: dict[str, Any]) -> int:
    """幂等写入来源，返回 source id。

    同时接受两种键名：seed YAML 用 ``type``，API/提取用 ``source_type``。
    ``title`` 由调用方保证非空（loader / 确认端点各自校验）。
    """
    cur = conn.execute(
        """INSERT INTO source (title, source_type, publisher, year, url)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(title, year) DO UPDATE SET source_type=excluded.source_type
           RETURNING id""",
        (
            src.get("title"),
            src.get("source_type") or src.get("type"),
            src.get("publisher"),
            src.get("year"),
            src.get("url"),
        ),
    )
    return int(cur.fetchone()["id"])


def link_rule_source(
    conn: sqlite3.Connection, rule_id: int, source_id: int, excerpt: str | None
) -> None:
    """挂规则 ↔ 来源；同对重复挂时更新 excerpt。"""
    conn.execute(
        """INSERT INTO rule_source (rule_id, source_id, excerpt)
           VALUES (?, ?, ?)
           ON CONFLICT(rule_id, source_id) DO UPDATE SET excerpt=excluded.excerpt""",
        (rule_id, source_id, excerpt),
    )
