"""DDInter 2.0 → data/seed/ddi_ddinter.yaml 转换器。

数据源：https://ddinter2.scbdd.com （CC BY-NC-SA 4.0，非商业 + 署名 + 同许可）
原始 CSV：data/raw/ddinter/*.csv —— **不入库**（体积 + 许可），重新获取：

    mkdir -p data/raw/ddinter && cd data/raw/ddinter
    for c in A B D H L P R V; do
      curl -sO "https://ddinter2.scbdd.com/static/media/download/ddinter_downloads_code_${c}.csv"
    done

用法：python scripts/import_ddinter.py   （幂等，整文件重生成）

转换规则（刻意保守 —— 这是安全评估工具，机器导入的数据不许碰硬拦截）：

1. 只保留**两端成分都能映射**到本库 ingredient.name_en 的 pair；
2. 剔除与现有 seed 规则重复的 pair（loader 对跨文件重复会报错）；
3. 定级：Major → caution，Moderate / Minor / Unknown → monitor。
   **不生成 contraindicated** —— 禁忌级会触发评估硬拦截，那必须是
   人工核验后的结论，不能来自未审的机器导入；核验后可在后台升级；
4. 每条挂 DDInter 来源（excerpt = 原始 Level），status 走 schema 默认 draft；
5. 不发明机制/后果文本，只给一句标注"待核验"的建议。
"""

from __future__ import annotations

import csv
import glob
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

RAW_GLOB = ROOT / "data/raw/ddinter/*.csv"
SEED_DIR = ROOT / "data/seed"
OUT_PATH = SEED_DIR / "ddi_ddinter.yaml"

SOURCE = {
    "title": "DDInter 2.0（drug-drug interaction database）",
    "type": "数据库",
    "url": "https://ddinter2.scbdd.com",
    "license": "CC BY-NC-SA 4.0",
}

# 保守定级：机器导入未核验，最高只到 caution（见模块 docstring 第 3 条）
LEVEL_MAP = {
    "Major": ("caution", "重要相互作用"),
    "Moderate": ("monitor", "中等相互作用"),
    "Minor": ("monitor", "轻微相互作用"),
    "Unknown": ("monitor", "等级未标注"),
}


def _load_ingredients() -> dict[str, str]:
    """name_en(小写) → name_cn，来自 seed 的 drugs.yaml（源头真值）。"""
    data = yaml.safe_load((SEED_DIR / "drugs.yaml").read_text(encoding="utf-8")) or {}
    en2cn: dict[str, str] = {}
    for ing in data.get("ingredients", []):
        en = (ing.get("name_en") or "").strip()
        if en:
            en2cn[en.lower()] = ing["name_cn"]
    return en2cn


def _existing_pairs() -> set[tuple[str, str]]:
    """现有 seed 规则的成分对（按名字），排除本脚本的输出文件。"""
    pairs: set[tuple[str, str]] = set()
    for path in sorted(SEED_DIR.glob("ddi_*.yaml")):
        if path == OUT_PATH:
            continue
        rules = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        for r in rules:
            pairs.add(tuple(sorted((r["ing_a"], r["ing_b"]))))
    return pairs


def main() -> int:
    paths = sorted(glob.glob(str(RAW_GLOB)))
    if not paths:
        print(f"未找到原始 CSV（{RAW_GLOB}）——获取方式见本文件 docstring")
        return 1

    en2cn = _load_ingredients()
    existing = _existing_pairs()

    seen: dict[tuple[str, str], tuple[str, str, str]] = {}
    rows = 0
    for path in paths:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows += 1
                a = (row.get("Drug_A") or "").strip()
                b = (row.get("Drug_B") or "").strip()
                lv = (row.get("Level") or "").strip()
                key = tuple(sorted((a.lower(), b.lower())))
                seen.setdefault(key, (a, b, lv))

    rules_out = []
    stats = {"no_map": 0, "dup": 0, "same": 0}
    for (a_low, b_low), (a, b, lv) in sorted(seen.items()):
        ca, cb = en2cn.get(a_low), en2cn.get(b_low)
        if not ca or not cb:
            stats["no_map"] += 1
            continue
        if ca == cb:
            stats["same"] += 1
            continue
        pair = tuple(sorted((ca, cb)))
        if pair in existing:
            stats["dup"] += 1
            continue
        existing.add(pair)   # 输出内部去重
        severity, level_cn = LEVEL_MAP.get(lv, ("monitor", "等级未标注"))
        rules_out.append({
            "ing_a": pair[0],
            "ing_b": pair[1],
            "severity": severity,
            "suggestion": (
                f"DDInter 2.0 收录的相互作用（Level: {lv}，{level_cn}），"
                f"尚未经药师核验；合用请核对药品说明书并注意监测"
            ),
            "sources": [{
                "title": SOURCE["title"],
                "type": SOURCE["type"],
                "url": SOURCE["url"],
                "excerpt": f"DDInter Level: {lv}",
            }],
        })

    header = f"""# ⚠️ 本文件由 scripts/import_ddinter.py 生成，请勿手改。
# 数据源：DDInter 2.0 — https://ddinter2.scbdd.com
# 许可：CC BY-NC-SA 4.0（署名-非商业性使用-相同方式共享）
# 定级映射：Major→caution；Moderate/Minor/Unknown→monitor
#   （机器导入未经核验，**不生成 contraindicated**，避免触发硬拦截；
#     人工核验后可在管理后台升级等级并补充机制/后果文本）
# 重新生成：先按 scripts/import_ddinter.py docstring 拉取原始 CSV，再运行本脚本。
---
"""
    OUT_PATH.write_text(
        header + yaml.safe_dump(rules_out, allow_unicode=True,
                                sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )

    from collections import Counter
    sev = Counter(r["severity"] for r in rules_out)
    print(f"原始行 {rows} → 去重 pair {len(seen)}")
    print(f"未映射（成分不在本库）: {stats['no_map']}，"
          f"与现有规则重复: {stats['dup']}，同成分: {stats['same']}")
    print(f"写入 {len(rules_out)} 条 → {OUT_PATH.relative_to(ROOT)}")
    print(f"严重度分布: {dict(sev)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
