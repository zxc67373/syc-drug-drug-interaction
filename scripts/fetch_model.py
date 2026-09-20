#!/usr/bin/env python
"""下载嵌入模型权重。

    python scripts/fetch_model.py                 # 默认拉 m3e-base
    python scripts/fetch_model.py --model bge-m3  # 拉 bge-m3（约 2.2GB）
    python scripts/fetch_model.py --list          # 看有哪些

**权重不入 git**（几百 MB 的二进制会让仓库永远瘦不下来），所以部署到新机器时
要么从已有环境拷，要么跑这个脚本拉。

优先走 ModelScope 而不是 HuggingFace —— 国内访问 HF 经常超时，
ModelScope 是官方镜像，同样的权重。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ddi.config import ROOT, settings  # noqa: E402
from ddi.console import setup_console  # noqa: E402

# 默认落到 models/<name>，与 config.yaml 的 embed.model_path 对应
MODELS = {
    "m3e-base": {
        "modelscope": "xrunda/m3e-base",
        "huggingface": "moka-ai/m3e-base",
        "size": "约 390MB",
        "note": "中文语义嵌入，768 维。本项目推荐。",
    },
    "bge-m3": {
        "modelscope": "Xorbits/bge-m3",
        "huggingface": "BAAI/bge-m3",
        "size": "约 2.2GB",
        "note": "多语言长文本。需要额外装 FlagEmbedding。",
    },
}

# 这两个文件是同一份权重的两种格式，留一份就够 —— 都下会白占一倍磁盘
REDUNDANT = {"pytorch_model.bin", "tf_model.h5", "flax_model.msgpack"}


def fetch(name: str, dest: Path, source: str) -> int:
    spec = MODELS[name]
    dest.mkdir(parents=True, exist_ok=True)

    if source == "modelscope":
        try:
            from modelscope import snapshot_download
        except ImportError:
            print("❌ 需要 modelscope：pip install modelscope", file=sys.stderr)
            return 1
        print(f"从 ModelScope 拉取 {spec['modelscope']}（{spec['size']}）…")
        path = snapshot_download(spec["modelscope"], cache_dir=str(dest.parent / ".cache"))
    else:
        try:
            from huggingface_hub import snapshot_download
        except ImportError:
            print("❌ 需要 huggingface_hub：pip install huggingface_hub", file=sys.stderr)
            return 1
        print(f"从 HuggingFace 拉取 {spec['huggingface']}（{spec['size']}）…")
        path = snapshot_download(
            spec["huggingface"],
            cache_dir=str(dest.parent / ".cache"),
            ignore_patterns=list(REDUNDANT),
        )

    src = Path(path)
    copied = 0
    for item in src.rglob("*"):
        if not item.is_file() or item.name in REDUNDANT:
            continue
        target = dest / item.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copy2(item, target)
            copied += 1

    total = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
    print(f"\n✅ 完成：{copied} 个文件，{total / 1e6:.0f} MB")
    print(f"   位置：{dest}")
    print(f"\n确认 config.yaml 里是：\n   embed:\n     model_path: {dest.relative_to(ROOT).as_posix()}")
    return 0


def main() -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="下载嵌入模型权重")
    ap.add_argument("--model", default="m3e-base", help="m3e-base | bge-m3")
    ap.add_argument("--source", default="modelscope", choices=["modelscope", "huggingface"])
    ap.add_argument("--dest", type=Path, default=None)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        print("可下载的模型：\n")
        for k, v in MODELS.items():
            print(f"  {k:<12} {v['size']:<10} {v['note']}")
        print(f"\n默认下载到：{ROOT / 'models'}/<模型名>")
        print("当前 config.yaml 配置的是：" + str(settings.embed_model_path))
        return 0

    if args.model not in MODELS:
        print(f"❌ 未知模型 {args.model!r}。可选：{' / '.join(MODELS)}", file=sys.stderr)
        return 1

    dest = args.dest or (ROOT / "models" / args.model)

    if (dest / "config.json").exists() and (dest / "model.safetensors").exists():
        print(f"✅ {dest} 已存在且看起来完整，跳过。")
        print("   要强制重下，先删掉该目录。")
        return 0

    return fetch(args.model, dest, args.source)


if __name__ == "__main__":
    raise SystemExit(main())
