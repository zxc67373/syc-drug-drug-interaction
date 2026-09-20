"""嵌入后端。

三种实现：

- ``m3e``     —— **推荐**。中文语义嵌入（moka-ai/m3e-base），768 维，权重约 390MB。
                 用原生 transformers 实现，只需 torch + transformers。
- ``bge-m3``  —— 多语言、1024 维、支持长文本（8192），但权重大（约 2.2GB）
                 且要额外装 FlagEmbedding。
- ``hash``    —— 零依赖兜底。字符 n-gram 哈希 + L2 归一化。

**hash 后端不是语义模型**，它只捕捉字面重合。它的价值在于：让检索链路、RRF 融合、
API 端到端在没有模型权重的情况下也能跑通和测试。上生产必须换成 m3e 或 bge-m3 ——
这一点不要含糊，否则"语义检索"是假的。

m3e 与 bge-m3 的取舍：本项目语料是**中文短文本**（药名 + 机制 + 建议，几十字），
m3e 完全够用，且权重小 5 倍、依赖少一层。bge-m3 的优势在长文本和多语言，
本项目用不上。
"""

from __future__ import annotations

import hashlib
from typing import Protocol, Sequence

import numpy as np

HASH_DIM = 1024
M3E_DIM = 768


class Embedder(Protocol):
    dim: int
    name: str

    def encode(self, texts: Sequence[str], is_query: bool = False) -> np.ndarray: ...


class HashEmbedder:
    """字符 n-gram 哈希嵌入。纯 numpy，无外部依赖。"""

    dim = HASH_DIM
    name = "hash"

    def __init__(self, ngram: int = 2, dim: int = HASH_DIM):
        self.ngram = ngram
        self.dim = dim

    def _grams(self, text: str) -> list[str]:
        t = "".join(text.split())
        if len(t) < self.ngram:
            return [t] if t else []
        return [t[i : i + self.ngram] for i in range(len(t) - self.ngram + 1)]

    def encode(self, texts: Sequence[str], is_query: bool = False) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            for gram in self._grams(text):
                h = hashlib.md5(gram.encode("utf-8")).digest()
                idx = int.from_bytes(h[:4], "little") % self.dim
                sign = 1.0 if h[4] & 1 else -1.0  # 带符号哈希，减少碰撞偏置
                out[row, idx] += sign
            norm = np.linalg.norm(out[row])
            if norm > 0:
                out[row] /= norm
        return out


class M3EEmbedder:
    """M3E（moka-ai/m3e-base）。中文语义嵌入，768 维。

    用原生 transformers 实现，**不依赖 sentence-transformers** ——
    这个模型的句子编码就是「mean pooling + L2 归一化」，十行的事，
    没必要为它多装一个库（sentence-transformers 会再拖进 datasets 等一串依赖）。

    池化方式取自模型自带的 ``1_Pooling/config.json``：
    ``pooling_mode_mean_tokens: true``，其余为 false。
    换成别的模型前先看这个文件，池化方式选错会让向量质量显著变差，
    而且**不会报错** —— 只是检索结果悄悄变烂。
    """

    name = "m3e"

    def __init__(
        self,
        model_path: str | None = None,
        max_length: int = 512,
        batch_size: int = 16,
    ):
        if model_path is None:
            from ddi.config import settings

            model_path = str(settings.embed_model_path)
        self.model_path = model_path
        self.max_length = max_length
        self.batch_size = batch_size
        self._model = None
        self._tokenizer = None
        self.dim = M3E_DIM

    def _ensure(self):
        if self._model is None:
            import torch
            from transformers import AutoModel, AutoTokenizer

            self._tokenizer = AutoTokenizer.from_pretrained(self.model_path)
            self._model = AutoModel.from_pretrained(self.model_path)
            self._model.eval()
            # 从模型配置读维度，而不是硬编码 —— 换成 m3e-small/large 时不用改代码
            self.dim = int(self._model.config.hidden_size)
            self._torch = torch
        return self._model

    def encode(self, texts: Sequence[str], is_query: bool = False) -> np.ndarray:
        """``is_query`` 对本模型无影响。

        M3E 不需要查询指令前缀（不像 bge 系列要加「为这个句子生成表示…」）。
        参数保留是为了满足 ``Embedder`` 协议，让调用方写法统一。
        """
        model = self._ensure()
        torch = self._torch
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)

        chunks: list[np.ndarray] = []
        for i in range(0, len(texts), self.batch_size):
            batch = self._tokenizer(
                list(texts[i : i + self.batch_size]),
                padding=True,
                truncation=True,
                max_length=self.max_length,
                return_tensors="pt",
            )
            with torch.no_grad():
                hidden = model(**batch).last_hidden_state       # (B, L, H)

            # mean pooling —— 必须用 attention_mask 把 padding 排除掉，
            # 否则短句会被 padding token 稀释，句子越短向量越偏
            mask = batch["attention_mask"].unsqueeze(-1).float()
            summed = (hidden * mask).sum(dim=1)
            counts = mask.sum(dim=1).clamp(min=1e-9)
            emb = summed / counts

            emb = torch.nn.functional.normalize(emb, p=2, dim=1)
            chunks.append(emb.cpu().numpy().astype(np.float32))

        return np.vstack(chunks)


class BGEM3Embedder:
    """BGE-M3（FlagEmbedding）。懒加载，权重只在首次 encode 时载入。"""

    name = "bge-m3"

    def __init__(self, model_path: str = "BAAI/bge-m3", use_fp16: bool = False):
        self.model_path = model_path
        self.use_fp16 = use_fp16
        self._model = None
        self.dim = 1024

    def _ensure(self):
        if self._model is None:
            from FlagEmbedding import BGEM3FlagModel

            self._model = BGEM3FlagModel(
                self.model_path, use_fp16=self.use_fp16, devices="cpu"
            )
        return self._model

    def encode(self, texts: Sequence[str], is_query: bool = False) -> np.ndarray:
        model = self._ensure()
        vecs = model.encode(
            list(texts), batch_size=8, max_length=512
        )["dense_vecs"]
        arr = np.asarray(vecs, dtype=np.float32)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        np.divide(arr, norms, out=arr, where=norms > 0)
        return arr


def get_embedder(backend: str | None = None) -> Embedder:
    from ddi.config import settings

    backend = (backend or settings.embed_backend).lower()
    if backend in ("bge-m3", "bge", "bgem3"):
        return BGEM3Embedder()
    if backend in ("m3e", "m3e-base"):
        return M3EEmbedder()
    return HashEmbedder()
