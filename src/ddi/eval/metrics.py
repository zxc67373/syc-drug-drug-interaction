"""评测指标。

原则：**分开报，不要笼统说"准确率"。**
- 召回率        —— 真实存在的相互作用找出了多少（对应漏检，药学场景最致命）
- 精确率        —— 报出来的风险有多少是真的（对应误判）
- 禁忌级漏检率  —— 安全底线，目标 = 0
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ConfusionMatrix:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    @property
    def total(self) -> int:
        return self.tp + self.fp + self.fn + self.tn

    def add(self, predicted: bool, actual: bool) -> None:
        if predicted and actual:
            self.tp += 1
        elif predicted and not actual:
            self.fp += 1
        elif not predicted and actual:
            self.fn += 1
        else:
            self.tn += 1

    def merge(self, other: "ConfusionMatrix") -> None:
        self.tp += other.tp
        self.fp += other.fp
        self.fn += other.fn
        self.tn += other.tn


@dataclass
class Metrics:
    name: str
    cm: ConfusionMatrix = field(default_factory=ConfusionMatrix)
    # 禁忌级单独统计：这组的漏检是安全问题，不是质量问题
    contra_cm: ConfusionMatrix = field(default_factory=ConfusionMatrix)

    @staticmethod
    def _safe_div(num: float, den: float) -> float:
        return num / den if den else 0.0

    @property
    def recall(self) -> float:
        return self._safe_div(self.cm.tp, self.cm.tp + self.cm.fn)

    @property
    def precision(self) -> float:
        return self._safe_div(self.cm.tp, self.cm.tp + self.cm.fp)

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return self._safe_div(2 * p * r, p + r)

    @property
    def accuracy(self) -> float:
        return self._safe_div(self.cm.tp + self.cm.tn, self.cm.total)

    @property
    def contraindicated_miss_rate(self) -> float:
        """禁忌级漏检率。目标 = 0，任何非零值都必须当作阻断性问题。"""
        return self._safe_div(self.contra_cm.fn, self.contra_cm.tp + self.contra_cm.fn)

    def to_dict(self) -> dict:
        return {
            "config": self.name,
            "samples": self.cm.total,
            "recall": round(self.recall, 4),
            "precision": round(self.precision, 4),
            "f1": round(self.f1, 4),
            "accuracy": round(self.accuracy, 4),
            "contraindicated_miss_rate": round(self.contraindicated_miss_rate, 4),
            "tp": self.cm.tp, "fp": self.cm.fp, "fn": self.cm.fn, "tn": self.cm.tn,
        }


def evaluate_pairs(
    name: str,
    predictions: list[bool],
    truths: list[bool],
    contra_flags: list[bool] | None = None,
) -> Metrics:
    """把逐条预测与真值汇总成指标。

    predictions[i]  —— 系统是否报告这对组合存在风险
    truths[i]       —— 该组合是否真的存在相互作用
    contra_flags[i] —— 该组合是否为禁忌级（仅对这些样本统计禁忌级漏检率）
    """
    if len(predictions) != len(truths):
        raise ValueError("predictions 与 truths 长度不一致")

    m = Metrics(name=name)
    for i, (pred, truth) in enumerate(zip(predictions, truths)):
        m.cm.add(pred, truth)
        if contra_flags and i < len(contra_flags) and contra_flags[i]:
            m.contra_cm.add(pred, truth)
    return m


def format_table(metrics: list[Metrics]) -> str:
    """渲染成可直接贴进文档的对照表。"""
    header = (
        f"{'配置':<20}{'样本':>6}{'召回率':>10}{'精确率':>10}"
        f"{'F1':>9}{'禁忌级漏检率':>14}"
    )
    lines = [header, "-" * len(header)]
    for m in metrics:
        lines.append(
            f"{m.name:<20}{m.cm.total:>6}{m.recall:>10.3f}{m.precision:>10.3f}"
            f"{m.f1:>9.3f}{m.contraindicated_miss_rate:>14.3f}"
        )
    return "\n".join(lines)
