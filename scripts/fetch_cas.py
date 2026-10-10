"""从 PubChem 补全成分 CAS 号 → 写回 data/seed/drugs.yaml。

PubChem PUG-REST（免认证，≤5 req/s）：按 name_en 查 synonyms，
取第一个符合 CAS 格式的同义词（PubChem 把目标化合物的 CAS 放最前）。

    python scripts/fetch_cas.py            # 只补缺失的（幂等）
    python scripts/fetch_cas.py --dry-run  # 只打印将要写入的内容

写入方式：drugs.yaml 的成分是**单行 flow 格式**（- {name_cn: ...}），
在行尾 `}` 前插入 `, cas: XX-XX-X`，不重排整文件、不丢注释。
写完跑 `python scripts/load_seed.py` 才会进库 —— 注意 loader 的
ON CONFLICT 子句已包含 cas=excluded.cas（本脚本配套修改）。
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DRUGS_YAML = ROOT / "data/seed/drugs.yaml"

CAS_RE = re.compile(r"^\d{2,7}-\d{2}-\d$")
ING_LINE_RE = re.compile(r"^(\s*-\s*\{.*name_en:\s*)([^,}]+)(.*)$")
API = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/{name}/synonyms/JSON"


def fetch_cas(name_en: str, retries: int = 2) -> str | None:
    url = API.format(name=name_en.strip().replace(" ", "%20"))
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                data = json.load(resp)
            syns = data["InformationList"]["Information"][0].get("Synonym", [])
            for s in syns:
                if CAS_RE.match(str(s).strip()):
                    return str(s).strip()
            return None   # 查到了但没有 CAS 形状的同义词
        except (urllib.error.HTTPError, urllib.error.URLError, KeyError, IndexError):
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            return None
    return None


def main() -> int:
    dry = "--dry-run" in sys.argv
    lines = DRUGS_YAML.read_text(encoding="utf-8").splitlines(keepends=True)

    # 收集：单行 flow 成分、带 name_en、缺 cas
    targets: list[tuple[int, str, str]] = []   # (行号, name_cn, name_en)
    for idx, line in enumerate(lines):
        if "cas:" in line:
            continue
        m = re.search(r"name_cn:\s*([^,}]+)", line)
        m2 = ING_LINE_RE.match(line.rstrip("\n"))
        if m and m2:
            targets.append((idx, m.group(1).strip(), m2.group(2).strip()))

    if not targets:
        print("没有缺 CAS 的成分行（或格式不符），不做修改")
        return 0

    print(f"待补 {len(targets)} 个成分的 CAS…")
    hits, misses = 0, []
    for i, (idx, name_cn, name_en) in enumerate(targets, 1):
        cas = fetch_cas(name_en)
        time.sleep(0.25)   # PubChem 限速 ~5/s
        if cas:
            hits += 1
            line = lines[idx].rstrip("\n")
            # 行尾 `}` 前插入（可能有行尾注释，先剥离）
            comment = ""
            if "#" in line and not line.rstrip().endswith("}"):
                pass   # 成分行一般无行尾注释，保守起见只处理以 } 结尾的
            if line.rstrip().endswith("}"):
                new_line = line.rstrip()[:-1].rstrip()
                if new_line.endswith(","):
                    new_line = new_line[:-1]
                lines[idx] = f"{new_line}, cas: {cas}}}{comment}\n"
            print(f"  [{i}/{len(targets)}] {name_cn} ({name_en}) → {cas}")
        else:
            misses.append(f"{name_cn} ({name_en})")
            print(f"  [{i}/{len(targets)}] {name_cn} ({name_en}) → 未找到")
        if dry:
            # dry-run 不写文件但保留统计
            pass

    if dry:
        print(f"\n[dry-run] 命中 {hits}，未命中 {len(misses)} —— 未写文件")
        return 0

    DRUGS_YAML.write_text("".join(lines), encoding="utf-8")
    print(f"\n写入 {hits} 个 CAS → {DRUGS_YAML.relative_to(ROOT)}")
    if misses:
        print(f"未命中 {len(misses)}: {', '.join(misses)}")
    print("记得跑: python scripts/load_seed.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
