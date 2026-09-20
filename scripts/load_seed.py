#!/usr/bin/env python
"""种子 YAML → SQLite。

    python scripts/load_seed.py [--db PATH] [--seed DIR]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ddi.console import setup_console  # noqa: E402
from ddi.db.loader import load_all  # noqa: E402


def main() -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="加载种子数据到数据库")
    ap.add_argument("--db", type=Path, default=None, help="目标数据库路径")
    ap.add_argument("--seed", type=Path, default=None, help="种子目录")
    args = ap.parse_args()

    report = load_all(seed_dir=args.seed, db=args.db)
    print(report.summary())

    if report.errors:
        print("\n加载失败：请先修正上述错误。", file=sys.stderr)
        return 1
    print("\n✅ 加载完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
