#!/usr/bin/env python
"""启动 API 服务。

开发：

    python scripts/run_api.py

生产（用真正的 WSGI 服务器，不要用 Flask 自带的开发服务器）：

    python scripts/run_api.py --prod --workers 4

``--prod`` 优先用 waitress（纯 Python，Linux/Windows 都能跑），
装了 gunicorn 的话也可以直接：

    gunicorn -w 4 -b 0.0.0.0:5000 --chdir src "ddi.api:create_app()"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ddi.api import create_app  # noqa: E402
from ddi.config import settings  # noqa: E402
from ddi.console import setup_console  # noqa: E402


def _serve_prod(app, port: int, workers: int) -> None:
    try:
        from waitress import serve
    except ImportError:
        print(
            "❌ --prod 需要 waitress：pip install waitress\n"
            "   或改用 gunicorn：gunicorn -w 4 -b 0.0.0.0:%d --chdir src 'ddi.api:create_app()'" % port,
            file=sys.stderr,
        )
        raise SystemExit(1)

    print(f"WSGI     waitress（{workers} 线程）")
    serve(app, host="0.0.0.0", port=port, threads=workers, ident="ddi")
    return


def main() -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="启动 DDI 评估服务")
    ap.add_argument("--port", type=int, default=settings.api_port)
    ap.add_argument("--debug", action="store_true", default=settings.api_debug)
    ap.add_argument("--prod", action="store_true", help="用 waitress 而不是开发服务器")
    ap.add_argument("--workers", type=int, default=4, help="--prod 时的线程数")
    args = ap.parse_args()

    app = create_app()
    svc = app.config["DDI_SERVICE"]
    print(f"配置来源 {settings.config_source}")
    print(f"大模型   {settings.llm_provider_label} / {svc.llm.model}"
          if svc.llm.configured else "大模型   未配置（将只返回规则答案）")
    print(f"规则库   {svc.engine.rule_count} 条")
    print(f"药品索引 {svc.normalizer.size} 个")
    print(f"检索     {'可用' if svc.retriever else '不可用（先跑 build_index.py）'}")
    print(f"前端     {'已挂载 ' + str(settings.web_dir) if settings.web_dir.is_dir() else '未挂载（仅 API）'}")

    if args.prod:
        print()
        _serve_prod(app, args.port, args.workers)
        return 0

    print(f"\n监听 http://127.0.0.1:{args.port}")
    print("（开发模式。生产请加 --prod。）\n")
    app.run(host="0.0.0.0", port=args.port, debug=args.debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
