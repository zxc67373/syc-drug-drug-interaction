#!/usr/bin/env python
"""评测：检测质量 + 检索质量。

    python scripts/run_eval.py [--out results.json]

═══ 评测口径（必须写进对外材料，否则数字没有意义）═══

本脚本报三组**互不混同**的指标：

1. 检测指标（召回率 / 精确率 / F1 / 禁忌级漏检率）
   来源：规则引擎。这是唯一有权判定"是否存在风险"的组件。

2. 检索指标（Recall@k / MRR）
   来源：RAG。**RAG 不参与检测**——这是架构的有意设计，不是缺陷。
   它的职责是为 LLM 提供可引用的片段。拿它当检测器用，只会引入误报
   （本脚本早期版本正是这么做的，精确率被拉低到 0.50，已废弃该口径）。

3. 测试集构建方式的局限
   正样本从规则库分层抽样得到，负样本为规则库中不存在成分对。
   ⚠️ "负样本"只代表**规则库未收录**，不等于**临床无相互作用**。
   因此精确率是**上限估计**，会高估。
   要得到可对外引用的数字，必须补一份独立验证集（来自说明书/指南，
   且不参与种子数据录入）。这是 W14–16 的关键交付物。
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ddi.console import setup_console  # noqa: E402
from ddi.db.session import session  # noqa: E402
from ddi.eval.metrics import evaluate_pairs, format_table  # noqa: E402
from ddi.normalize import Normalizer  # noqa: E402
from ddi.rules import RuleEngine  # noqa: E402

SEVERITIES = ("contraindicated", "caution", "monitor")


def build_test_set(conn, seed: int = 42, n_neg: int | None = None):
    """返回 [(药名A, 药名B, 是否存在规则, 是否禁忌级, rule_id|None)]。"""
    rnd = random.Random(seed)

    ing_to_drug = {
        r["ingredient_id"]: r["name_cn"]
        for r in conn.execute(
            """SELECT di.ingredient_id, d.name_cn FROM drug_ingredient di
               JOIN drug d ON d.id = di.drug_id
               WHERE d.is_tcm = 0 ORDER BY d.id"""
        )
    }

    positives = []
    for r in conn.execute(
        f"SELECT id, ing_a_id, ing_b_id, severity FROM ddi_rule "
        f"WHERE severity IN {SEVERITIES}"
    ):
        a, b = r["ing_a_id"], r["ing_b_id"]
        if a in ing_to_drug and b in ing_to_drug:
            positives.append(
                (
                    ing_to_drug[a],
                    ing_to_drug[b],
                    True,
                    r["severity"] == "contraindicated",
                    r["id"],
                )
            )

    all_ing = sorted(ing_to_drug)
    known = {
        tuple(sorted((r["ing_a_id"], r["ing_b_id"])))
        for r in conn.execute("SELECT ing_a_id, ing_b_id FROM ddi_rule")
    }
    candidates = [
        (a, b) for a, b in combinations(all_ing, 2) if tuple(sorted((a, b))) not in known
    ]
    rnd.shuffle(candidates)
    n_neg = n_neg or len(positives)
    negatives = [
        (ing_to_drug[a], ing_to_drug[b], False, False, None) for a, b in candidates[:n_neg]
    ]

    combined = positives + negatives
    rnd.shuffle(combined)
    return combined


def eval_detection(cases, engine, normalizer):
    """检测指标：规则引擎是唯一的判定者。"""
    preds, truths, contras = [], [], []
    for drug_a, drug_b, exists, is_contra, _rid in cases:
        ra, rb = normalizer.normalize(drug_a), normalizer.normalize(drug_b)
        ids = (
            normalizer.ingredients_of([ra.matched.drug_id, rb.matched.drug_id])
            if ra.ok and rb.ok
            else []
        )
        result = engine.assess(ids, {})
        preds.append(bool(result.risks))
        truths.append(exists)
        contras.append(is_contra)
    return evaluate_pairs("仅规则引擎", preds, truths, contras)


def eval_retrieval(cases, retriever, normalizer, k: int = 5) -> dict:
    """检索指标：对每条正样本，正确的 rule_id 是否出现在 top-k。

    RAG 的职责是"为已知风险找到可引用的原文片段"，所以这里只对**正样本**评估。
    """
    ranks: list[int | None] = []
    by_severity: dict[str, list[int | None]] = {}

    for drug_a, drug_b, exists, is_contra, rid in cases:
        if not exists or rid is None:
            continue
        hits = retriever.retrieve(f"{drug_a} {drug_b} 联合用药 相互作用", top_k=k)
        found = next((i for i, h in enumerate(hits, 1) if h.rule_id == rid), None)
        ranks.append(found)

        sev = "contraindicated" if is_contra else "other"
        by_severity.setdefault(sev, []).append(found)

    def _recall_at(rs):
        return sum(1 for r in rs if r is not None and r <= k) / len(rs) if rs else 0.0

    def _mrr(rs):
        return statistics.mean([1.0 / r for r in rs if r]) if rs else 0.0

    return {
        "k": k,
        "samples": len(ranks),
        f"recall@{k}": round(_recall_at(ranks), 4),
        "mrr": round(_mrr(ranks), 4),
        "recall@k_禁忌级": round(_recall_at(by_severity.get("contraindicated", [])), 4),
        "recall@k_非禁忌级": round(_recall_at(by_severity.get("other", [])), 4),
    }


def main() -> int:
    setup_console()
    ap = argparse.ArgumentParser(description="评测")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    report: dict = {}

    with session() as conn:
        cases = build_test_set(conn, seed=args.seed)
        engine = RuleEngine(conn)
        normalizer = Normalizer(conn)

        n_pos = sum(1 for c in cases if c[2])
        n_neg = len(cases) - n_pos
        n_contra = sum(1 for c in cases if c[3])
        print(
            f"测试集：{len(cases)} 条"
            f"（正样本 {n_pos}，负样本 {n_neg}，其中禁忌级 {n_contra}）\n"
        )

        print("── 1. 检测质量（规则引擎）──")
        det = eval_detection(cases, engine, normalizer)
        print(format_table([det]))
        report["detection"] = det.to_dict()

        print("\n── 2. 检索质量（RAG，仅对正样本）──")
        try:
            from ddi.rag.retriever import Retriever

            retriever = Retriever()
            if retriever.ready:
                retriever.load()
                ret = eval_retrieval(cases, retriever, normalizer, k=args.top_k)
                for key, val in ret.items():
                    print(f"   {key:<20} {val}")
                report["retrieval"] = ret
            else:
                print("   ⚠️  索引未构建，跳过。先跑 scripts/build_index.py")
        except Exception as e:  # noqa: BLE001
            print(f"   ⚠️  检索不可用：{e}")

    print("\n" + "─" * 68)
    print("口径说明：")
    print("  · RAG 不参与风险判定，这是架构的有意设计 —— 它只为 LLM 提供可引用片段。")
    print("    把它当检测器会引入误报（实测精确率会从 1.00 掉到 0.50）。")
    print("  · 负样本 = 规则库未收录，≠ 临床无相互作用。精确率为上限估计。")
    print("  · 可对外引用的数字需要独立验证集（W14–16 交付物）。")

    if args.out:
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
