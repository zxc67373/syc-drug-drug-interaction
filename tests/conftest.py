"""共享 fixture。

测试用**独立的临时数据库**，不碰 data/build/ddi.db —— 跑测试不该污染开发数据。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ddi.console import setup_console  # noqa: E402
from ddi.db.loader import load_all  # noqa: E402

# Windows 控制台默认 GBK，中文断言消息会变成乱码，排查困难
setup_console()


@pytest.fixture(scope="session")
def seed_db(tmp_path_factory) -> Path:
    db = tmp_path_factory.mktemp("db") / "test.db"
    report = load_all(seed_dir=ROOT / "data" / "seed", db=db)
    assert not report.errors, f"种子数据有误：{report.errors}"
    return db


@pytest.fixture(scope="session")
def conn(seed_db: Path):
    from ddi.db.session import session

    with session(seed_db) as c:
        yield c


@pytest.fixture(scope="session")
def normalizer(conn):
    from ddi.normalize import Normalizer

    return Normalizer(conn)


@pytest.fixture(scope="session")
def engine(conn):
    from ddi.rules import RuleEngine

    return RuleEngine(conn)


@pytest.fixture(scope="session")
def indexed(conn, tmp_path_factory):
    """构建一个临时检索索引，返回 (build_dir, embedder)。"""
    from ddi.rag.embedder import HashEmbedder
    from ddi.rag.index import build_index

    build_dir = tmp_path_factory.mktemp("index")
    emb = HashEmbedder()
    build_index(build_dir=build_dir, embedder=emb, db=None)
    return build_dir, emb
