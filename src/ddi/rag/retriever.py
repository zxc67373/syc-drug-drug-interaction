"""混合检索 + RRF 融合。

为什么混合：
- 药品名是**精确术语**。纯向量会把"阿司匹林"和"阿司匹林肠溶片"混为一谈。
- 纯 BM25 又抓不住"出血风险"这类语义查询。

RRF（Reciprocal Rank Fusion）用**排名**而非分数融合，避免了两路分数不可比的问题：
    score(d) = Σ_r  1 / (k + rank_r(d))
k=60 是文献常用默认值。
"""

from __future__ import annotations

import json
import pickle
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ddi.config import settings
from ddi.rag.embedder import Embedder, get_embedder
from ddi.rag.index import BM25_FILE, INDEX_FILE, META_FILE, _tokenize

RRF_K = 60


@dataclass
class Hit:
    rule_id: int
    text: str
    severity: str
    drugs: list[str]
    score: float
    rank_vector: int | None = None
    rank_bm25: int | None = None

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "drugs": self.drugs,
            "score": round(self.score, 6),
            "text": self.text,
        }


def rrf_fuse(
    ranked_lists: list[list[int]], k: int = RRF_K
) -> dict[int, float]:
    """输入若干「按相关性降序排列的 id 列表」，输出 id -> 融合分。"""
    scores: dict[int, float] = {}
    for ranked in ranked_lists:
        for rank, doc_id in enumerate(ranked, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return scores


class Retriever:
    """懒加载索引。构造时不读盘，首次 retrieve 才加载。"""

    def __init__(
        self,
        build_dir: Path | None = None,
        embedder: Embedder | None = None,
        use_vector: bool = True,
    ):
        self.build_dir = build_dir or settings.build_dir
        self.embedder = embedder or get_embedder()
        self.use_vector = use_vector
        self._index = None
        self._meta: dict = {}
        self._bm25 = None
        self._bm25_ids: list[int] = []

    @property
    def ready(self) -> bool:
        return (self.build_dir / META_FILE).exists() and (self.build_dir / INDEX_FILE).exists()

    def load(self) -> "Retriever":
        import faiss

        if not self.ready:
            raise FileNotFoundError(
                f"索引不存在于 {self.build_dir}，先跑 scripts/build_index.py"
            )
        self._index = faiss.read_index(str(self.build_dir / INDEX_FILE))
        self._meta = json.loads((self.build_dir / META_FILE).read_text(encoding="utf-8"))

        bm25_path = self.build_dir / BM25_FILE
        if bm25_path.exists():
            from rank_bm25 import BM25Okapi

            with bm25_path.open("rb") as f:
                payload = pickle.load(f)
            self._bm25 = BM25Okapi(payload["corpus"])
            self._bm25_ids = payload["rule_ids"]
        return self

    def warmup(self) -> float:
        """把索引和嵌入模型都载入内存，返回耗时（秒）。

        **服务启动时必须调这个。** 嵌入模型冷启动实测约 40 秒
        （import torch 17s + 加载权重 24s），留到第一个请求会让第一个用户干等。
        载入后单条查询只要 ~90ms，是一次性成本。

        代价换来的是一致性：宁可启动慢，也不要让某个倒霉用户撞上冷启动。
        """
        started = time.perf_counter()
        self.load()
        self.embedder.encode(["预热"])
        return time.perf_counter() - started

    def _ensure(self) -> None:
        if self._index is None:
            self.load()

    def retrieve(self, query: str, top_k: int = 5) -> list[Hit]:
        self._ensure()
        docs = self._meta["docs"]
        pool = max(top_k * 2, 10)

        vector_ids: list[int] = []
        if self.use_vector:
            qv = self.embedder.encode([query], is_query=True)
            _scores, idxs = self._index.search(qv.astype(np.float32), pool)
            vector_ids = [
                docs[i]["rule_id"] for i in idxs[0] if 0 <= i < len(docs)
            ]

        bm25_ids: list[int] = []
        if self._bm25 is not None:
            bm_scores = self._bm25.get_scores(_tokenize(query))
            order = np.argsort(bm_scores)[::-1][:pool]
            bm25_ids = [self._bm25_ids[i] for i in order if bm_scores[i] > 0]

        fused = rrf_fuse([vector_ids, bm25_ids])

        by_id = {d["rule_id"]: d for d in docs}
        rank_v = {rid: i for i, rid in enumerate(vector_ids, 1)}
        rank_b = {rid: i for i, rid in enumerate(bm25_ids, 1)}

        hits = [
            Hit(
                rule_id=rid,
                text=by_id[rid]["text"],
                severity=by_id[rid]["severity"],
                drugs=by_id[rid]["drugs"],
                score=score,
                rank_vector=rank_v.get(rid),
                rank_bm25=rank_b.get(rid),
            )
            for rid, score in sorted(fused.items(), key=lambda kv: -kv[1])
            if rid in by_id
        ]
        return hits[:top_k]

    def retrieve_excluding(self, query: str, exclude_ids: set[int], top_k: int = 5) -> list[Hit]:
        """检索并排除指定 rule_id。用于补充规则引擎未覆盖的片段。"""
        hits = self.retrieve(query, top_k=top_k + len(exclude_ids) + 5)
        return [h for h in hits if h.rule_id not in exclude_ids][:top_k]
