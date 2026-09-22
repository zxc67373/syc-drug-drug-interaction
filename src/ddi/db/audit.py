"""评估请求日志（管理后台"评估记录"页用）。

设计要点：

- 存 SQLite 而不是内存 —— 重启不丢，且与单实例部署假设一致。
- ``assess_log`` 表在现有 ddi.db 里可能不存在（建库早于本模块），
  所以**首次写入时懒建表**（``ensure_log_table``），不依赖 ``init_db``。
- 隐私红线：只写入白名单字段，``allergies`` 等自由文本患者数据绝不落库。
- 写日志失败绝不能让评估接口失败 —— 调用方负责 try/except。
"""

from __future__ import annotations

import json
import math
from typing import Any

from ddi.db.session import session

# 与 schema.sql 中 assess_log 对应。写日志时只允许这些列，
# 防止调用方误把自由文本塞进来。
_LOG_COLUMNS = (
    "created_at", "drugs_raw", "drugs_matched", "profile_summary",
    "overall_risk", "hard_blocked", "use_llm", "use_rag",
    "used_llm", "degraded_reason", "elapsed_ms", "ok", "error",
)

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS assess_log (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at      TEXT NOT NULL,
  drugs_raw       TEXT NOT NULL,
  drugs_matched   TEXT,
  profile_summary TEXT,
  overall_risk    TEXT,
  hard_blocked    INTEGER NOT NULL DEFAULT 0,
  use_llm         INTEGER NOT NULL DEFAULT 0,
  use_rag         INTEGER NOT NULL DEFAULT 0,
  used_llm        INTEGER,
  degraded_reason TEXT,
  elapsed_ms      INTEGER,
  ok              INTEGER NOT NULL DEFAULT 1,
  error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_assess_log_created ON assess_log(created_at DESC);
"""

# profile 白名单 —— 与 schema 注释里的隐私红线保持一致。
_PROFILE_ALLOW = ("age", "sex", "hepatic", "renal", "pregnancy")


def ensure_log_table() -> None:
    """确保 assess_log 表存在。幂等，可重复调用。"""
    with session() as conn:
        conn.executescript(_TABLE_SQL)


def audit_profile(profile: dict | None) -> dict:
    """从 profile 里抠出允许落库的白名单字段。allergies 被有意排除。"""
    if not isinstance(profile, dict):
        return {}
    return {k: profile[k] for k in _PROFILE_ALLOW if k in profile}


def write_log(record: dict[str, Any]) -> int:
    """写入一条日志，返回自增 id。

    调用方负责 try/except —— 日志失败不应影响评估本身。
    """
    ensure_log_table()
    cols = [c for c in _LOG_COLUMNS if c in record]
    if not cols:
        raise ValueError("日志记录为空")
    placeholders = ", ".join("?" for _ in cols)
    with session() as conn:
        cur = conn.execute(
            f"INSERT INTO assess_log ({', '.join(cols)}) VALUES ({placeholders})",
            [record[c] for c in cols],
        )
        return int(cur.lastrowid)


def _row_to_dict(row) -> dict:
    """sqlite3.Row → dict。JSON 字段尽力解析，坏数据原样返回字符串。"""
    out = dict(row)
    for key in ("drugs_raw", "drugs_matched", "profile_summary"):
        raw = out.get(key)
        if isinstance(raw, str) and raw:
            try:
                out[key] = json.loads(raw)
            except (ValueError, TypeError):
                pass
    return out


def list_logs(page: int = 1, page_size: int = 20) -> dict:
    """分页列出日志，按时间倒序。"""
    ensure_log_table()
    page = max(1, int(page))
    page_size = max(1, min(100, int(page_size)))
    with session() as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM assess_log").fetchone()["n"]
        rows = conn.execute(
            "SELECT * FROM assess_log ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (page_size, (page - 1) * page_size),
        ).fetchall()
    pages = max(1, math.ceil(total / page_size)) if total else 1
    return {
        "items": [_row_to_dict(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    }


def get_log(log_id: int) -> dict | None:
    """取单条日志详情。"""
    ensure_log_table()
    with session() as conn:
        row = conn.execute("SELECT * FROM assess_log WHERE id = ?", (log_id,)).fetchone()
    return _row_to_dict(row) if row else None
