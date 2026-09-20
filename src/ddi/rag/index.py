"""索引构建与持久化。

产物放在 data/build/：
    ddi.index    FAISS IndexFlatIP
    ddi.meta.json  文档元数据（rule_id、原文）
    ddi.bm25.pkl   BM25 语料与分词结果

数据量 < 10 万，用 IndexFlatIP 做精确检索即可 —— 无需调参，召回不打折。
"""

from __future__ import annotations

import json
import pickle
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ddi.config import settings
from ddi.db.session import session
from ddi.rag.embedder import Embedder, get_embedder

INDEX_FILE = "ddi.index"
META_FILE = "ddi.meta.json"
BM25_FILE = "ddi.bm25.pkl"


@dataclass
class Doc:
    rule_id: int
    text: str
    severity: str
    drugs: list[str]

    def to_meta(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "text": self.text,
            "severity": self.severity,
            "drugs": self.drugs,
        }


def build_docs(conn: sqlite3.Connection) -> list[Doc]:
    """把每条 DDI 规则文本化，作为检索单元。"""
    rows = conn.execute(
        """SELECT d.id, d.severity, d.mechanism, d.consequence, d.suggestion,
                  i1.name_cn AS a, i2.name_cn AS b
           FROM ddi_rule d
           JOIN ingredient i1 ON i1.id = d.ing_a_id
           JOIN ingredient i2 ON i2.id = d.ing_b_id
           ORDER BY d.id"""
    ).fetchall()

    docs: list[Doc] = []
    for r in rows:
        text = (
            f"{r['a']} 与 {r['b']} 联用。"
            f"机制：{r['mechanism'] or '—'}。"
            f"后果：{r['consequence'] or '—'}。"
            f"建议：{r['suggestion'] or '—'}。"
        )
        docs.append(
            Doc(rule_id=r["id"], text=text, severity=r["severity"], drugs=[r["a"], r["b"]])
        )
    return docs


def _tokenize(text: str) -> list[str]:
    """中文按字符 bigram + 单字，英文/数字按词。无需外部分词器。"""
    import re

    tokens: list[str] = []
    for chunk in re.findall(r"[a-zA-Z0-9]+|[一-鿿]+", text):
        if chunk.isascii():
            tokens.append(chunk.lower())
        else:
            tokens.extend(chunk)
            tokens.extend(chunk[i : i + 2] for i in range(len(chunk) - 1))
    return tokens


def build_index(
    build_dir: Path | None = None,
    embedder: Embedder | None = None,
    db=None,
) -> dict:
    """从数据库重建 FAISS + BM25 索引。返回统计信息。"""
    import faiss

    build_dir = build_dir or settings.build_dir
    build_dir.mkdir(parents=True, exist_ok=True)
    embedder = embedder or get_embedder()

    with session(db) as conn:
        docs = build_docs(conn)

    if not docs:
        raise RuntimeError("规则库为空，先跑 scripts/load_seed.py")

    vectors = embedder.encode([d.text for d in docs])
    index = faiss.IndexFlatIP(vectors.shape[1])
    index.add(vectors)
    faiss.write_index(index, str(build_dir / INDEX_FILE))

    (build_dir / META_FILE).write_text(
        json.dumps(
            {
                "embedder": embedder.name,
                "dim": int(vectors.shape[1]),
                "docs": [d.to_meta() for d in docs],
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )

    corpus = [_tokenize(d.text) for d in docs]
    with (build_dir / BM25_FILE).open("wb") as f:
        pickle.dump({"corpus": corpus, "rule_ids": [d.rule_id for d in docs]}, f)

    return {
        "docs": len(docs),
        "dim": int(vectors.shape[1]),
        "embedder": embedder.name,
        "build_dir": str(build_dir),
    }
