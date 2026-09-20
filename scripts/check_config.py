#!/usr/bin/env python
"""配置自检。

    python scripts/check_config.py            # 只看当前配置
    python scripts/check_config.py --list     # 列出所有可用供应商
    python scripts/check_config.py --try deepseek   # 临时试另一个供应商（不改文件）

换供应商之后先跑这个，确认 base_url / model 真的换了 ——
拼错 provider 名字会直接报错而不是静默连回原供应商。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ddi.config import CONFIG_FILE, ROOT, build_settings, settings  # noqa: E402
from ddi.console import setup_console  # noqa: E402
from ddi.providers import PROVIDERS, describe  # noqa: E402


def _mask(key: str) -> str:
    """只露头尾。密钥不该在终端里被完整打印出来 —— 屏幕会被截图。"""
    if not key:
        return "（未设置）"
    if len(key) <= 20:
        return key[:4] + "…"
    return f"{key[:10]}…{key[-4:]}（{len(key)} 字符）"


def _show(s) -> None:
    print("── 大模型 ──────────────────────────────────────")
    print(f"  provider   : {s.llm_provider}  ({s.llm_provider_label})")
    print(f"  base_url   : {s.llm_base_url}")
    print(f"  model      : {s.llm_model}")
    print(f"  api_key    : {_mask(s.llm_api_key)}")
    print(f"  max_tokens : {s.llm_max_tokens}")
    print(f"  timeout    : {s.llm_timeout}s")
    print(f"  thinking   : {s.llm_thinking}")
    print(f"  temperature: {s.llm_temperature if s.llm_temperature is not None else '（用供应商默认）'}")
    print()
    print("── 检索 ────────────────────────────────────────")
    print(f"  backend    : {s.embed_backend}")
    if s.embed_backend == "hash":
        print("               ⚠️  hash 只捕捉字面重合，不具备语义能力。生产请换 bge-m3。")
    print()
    print("── 数据 / 服务 ─────────────────────────────────")
    print(f"  数据库     : {s.db_path}")
    print(f"  前端目录   : {s.web_dir}  {'（存在）' if s.web_dir.is_dir() else '（不存在，只提供 API）'}")
    print(f"  监听端口   : {s.api_port}")
    print(f"  debug      : {s.api_debug}")
    print(f"  CORS 来源  : {', '.join(s.api_cors_origins)}")
    if "*" in s.api_cors_origins and not s.api_debug:
        print("               ⚠️  生产环境不要把 CORS 开成 *，会被白嫖大模型额度。")
    print()
    print(f"配置来源：{s.config_source}")
    print(f"配置文件：{s.config_file or '（不存在，全部使用默认值）'}")


def main() -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="配置自检")
    ap.add_argument("--list", action="store_true", help="列出所有内置供应商预设")
    ap.add_argument(
        "--try", dest="try_provider", metavar="PROVIDER",
        help="临时按指定供应商解析（不写文件）",
    )
    args = ap.parse_args()

    if args.list:
        print("内置供应商预设（三家都兼容 Anthropic 协议）：\n")
        print(describe())
        print(f"\n切换方式：改 config.yaml 里的 llm.provider 一行，或设环境变量 DDI_LLM_PROVIDER。")
        return 0

    if args.try_provider:
        key = args.try_provider.strip().lower()
        if key not in PROVIDERS:
            valid = " / ".join(PROVIDERS)
            print(f"❌ 未知供应商 {key!r}。可选：{valid}", file=sys.stderr)
            return 1
        # 用一个临时配置对象解析，不碰磁盘上的 config.yaml
        probe = build_settings()
        import ddi.providers as _p

        base_url, model = _p.resolve(key, "", "")
        print(f"若把 llm.provider 改成 {key!r}，将连到：\n")
        print(f"  base_url : {base_url}")
        print(f"  model    : {model}")
        print(f"  api_key  : {_mask(probe.llm_api_key)}  ← 密钥沿用当前配置，不随供应商变")
        print()
        note = PROVIDERS[key].note
        if note:
            print(f"  备注：{note}")
        return 0

    print(f"项目根目录：{ROOT}")
    print(f"配置文件  ：{CONFIG_FILE}\n")
    _show(settings)

    if not CONFIG_FILE.exists():
        print()
        print("提示：还没有 config.yaml，当前用的是默认值（无密钥）。")
        print("      执行 cp config.example.yaml config.yaml 然后填入 llm.api_key。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
