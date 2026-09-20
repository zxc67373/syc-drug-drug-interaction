#!/usr/bin/env python
"""大模型连通性自检。

    python scripts/smoke_llm.py

发一次最小请求，确认 base_url / key / model 三者都对，并打印原始响应。
排查问题时先跑这个，不要在业务链路里猜。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ddi.config import settings  # noqa: E402
from ddi.console import setup_console  # noqa: E402
from ddi.generate.llm_client import LLMClient, LLMUnavailable  # noqa: E402


def main() -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="大模型连通性自检")
    ap.add_argument("--prompt", default="用一句话说明联合用药风险评估的意义。")
    ap.add_argument("--no-thinking", action="store_true", help="不发送 thinking 参数")
    args = ap.parse_args()

    print(f"provider : {settings.llm_provider} ({settings.llm_provider_label})")
    print(f"base_url : {settings.llm_base_url}")
    print(f"model    : {settings.llm_model}")
    print(f"api_key  : {settings.llm_api_key[:12]}…{settings.llm_api_key[-6:]}"
          if settings.llm_api_key else "api_key  : （未设置）")
    print(f"配置来源 : {settings.config_source}")
    print()

    client = LLMClient()
    if not client.configured:
        print(
            "❌ 未配置大模型 api_key。\n"
            "   复制 config.example.yaml 为 config.yaml，在 llm.api_key 填入密钥。",
            file=sys.stderr,
        )
        return 1

    try:
        resp = client.complete(
            system="你是一个简洁的助手。",
            user=args.prompt,
            thinking=not args.no_thinking,
        )
    except LLMUnavailable as e:
        print(f"❌ 调用失败：{e}", file=sys.stderr)
        return 1

    if resp.refused:
        print(f"⚠️  模型拒答，类别：{resp.refusal_category}")
        return 2

    print("✅ 连通正常")
    print(json.dumps(
        {
            "model": resp.model,
            "stop_reason": resp.raw_stop_reason,
            "input_tokens": resp.input_tokens,
            "output_tokens": resp.output_tokens,
        },
        ensure_ascii=False, indent=2,
    ))
    print(f"\n回复：\n{resp.text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
