"""Windows 控制台默认 GBK，打印中文/emoji 会抛 UnicodeEncodeError。
脚本入口先调 setup_console()。"""

from __future__ import annotations

import sys


def setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        enc = (getattr(stream, "encoding", "") or "").lower()
        if enc.replace("-", "") not in ("utf8", "utf8mb4", "cp65001"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
