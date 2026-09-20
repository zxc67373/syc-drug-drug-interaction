"""数据库会话与初始化。用原生 sqlite3，不引入 ORM —— 查询都是点查/范围查，
SQL 直白比 ORM 更好审，也少一层依赖。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ddi.config import ROOT, settings

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def db_path() -> Path:
    return settings.db_path


def connect(path: Path | None = None) -> sqlite3.Connection:
    """打开连接。row_factory 设为 sqlite3.Row，取列用名字而不是下标。"""
    target = Path(path) if path else db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(path: Path | None = None) -> Path:
    """建表。幂等：全部 CREATE TABLE IF NOT EXISTS。"""
    target = Path(path) if path else db_path()
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with session(target) as conn:
        conn.executescript(sql)
    return target


def tables(path: Path | None = None) -> list[str]:
    with session(path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    return [r["name"] for r in rows]
