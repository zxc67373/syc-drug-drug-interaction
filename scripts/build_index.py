#!/usr/bin/env python
"""构建检索索引（FAISS + BM25）。

    python scripts/build_index.py [--backend hash|m3e|bge-m3]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ddi.console import setup_console  # noqa: E402
from ddi.rag.embedder import get_embedder  # noqa: E402
from ddi.rag.index import build_index  # noqa: E402


def main() -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="构建检索索引")
    ap.add_argument("--backend", default=None, help="hash | m3e | bge-m3")
    args = ap.parse_args()

    embedder = get_embedder(args.backend)
    print(f"嵌入后端：{embedder.name}")
    if embedder.name == "hash":
        print("  ⚠️  hash 仅用于跑通链路，不具备语义能力。生产请用 --backend m3e")

    stats = build_index(embedder=embedder)
    print(f"\n✅ 索引完成：{stats['docs']} 篇文档，{stats['dim']} 维")
    print(f"   输出目录：{stats['build_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
