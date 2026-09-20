"""评测指标测试。

指标算错比没有指标更糟——它会让人以为系统是好的。
"""

from __future__ import annotations

import pytest

from ddi.eval.metrics import evaluate_pairs, format_table


class TestConfusionMatrix:
    def test_perfect_prediction(self):
        m = evaluate_pairs("t", [True, False], [True, False])
        assert m.recall == 1.0 and m.precision == 1.0 and m.f1 == 1.0
        assert m.cm.tp == 1 and m.cm.tn == 1

    def test_all_missed(self):
        m = evaluate_pairs("t", [False, False], [True, True])
        assert m.recall == 0.0
        assert m.cm.fn == 2

    def test_all_false_alarms(self):
        m = evaluate_pairs("t", [True, True], [False, False])
        assert m.precision == 0.0
        assert m.cm.fp == 2

    def test_f1_harmonic_mean(self):
        # tp=1, fp=1, fn=1 → p=0.5, r=0.5, f1=0.5
        m = evaluate_pairs("t", [True, True, False], [True, False, True])
        assert m.precision == pytest.approx(0.5)
        assert m.recall == pytest.approx(0.5)
        assert m.f1 == pytest.approx(0.5)


class TestContraindicatedMissRate:
    def test_zero_when_all_contra_detected(self):
        m = evaluate_pairs("t", [True, False], [True, False], [True, False])
        assert m.contraindicated_miss_rate == 0.0

    def test_nonzero_when_contra_missed(self):
        """禁忌级漏检是本项目最严重的失败模式，指标必须能捕捉到。"""
        m = evaluate_pairs(
            "t",
            [False, False],   # 第一条禁忌级没检出
            [True, False],
            [True, False],
        )
        assert m.contraindicated_miss_rate == 1.0

    def test_only_counts_flagged_samples(self):
        # 非禁忌样本的漏检不应污染禁忌级漏检率
        m = evaluate_pairs("t", [True, False], [True, True], [False, False])
        assert m.contraindicated_miss_rate == 0.0

    def test_no_contra_samples_is_zero_not_error(self):
        m = evaluate_pairs("t", [True], [True], [False])
        assert m.contraindicated_miss_rate == 0.0


class TestEdgeCases:
    def test_empty_input(self):
        m = evaluate_pairs("t", [], [])
        assert m.cm.total == 0
        assert m.recall == 0.0 and m.precision == 0.0 and m.f1 == 0.0

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            evaluate_pairs("t", [True], [True, False])


class TestSerialization:
    def test_to_dict_has_all_metrics(self):
        m = evaluate_pairs("仅规则引擎", [True, False], [True, False], [True, False])
        d = m.to_dict()
        assert set(d) >= {
            "config", "samples", "recall", "precision", "f1",
            "accuracy", "contraindicated_miss_rate", "tp", "fp", "fn", "tn",
        }
        assert d["config"] == "仅规则引擎"

    def test_format_table_renders(self):
        m = evaluate_pairs("cfg", [True], [True])
        out = format_table([m])
        assert "cfg" in out and "召回率" in out
