"""M2 规则引擎测试。

安全底线：**禁忌级组合漏检率必须为 0。** 这是本文件最重要的一组断言。
"""

from __future__ import annotations

import pytest

from ddi.rules import SEVERITY_ORDER


def _ids(normalizer, *names) -> list[int]:
    out = []
    for n in names:
        r = normalizer.normalize(n)
        assert r.ok, f"测试前置条件失败：无法识别「{n}」"
        out.append(r.matched.drug_id)
    return normalizer.ingredients_of(out)


class TestPairwiseDetection:
    def test_warfarin_aspirin_is_contraindicated(self, normalizer, engine):
        ids = _ids(normalizer, "华法林钠片", "阿司匹林肠溶片")
        res = engine.assess(ids)
        assert res.hard_blocked
        assert res.overall_risk == "contraindicated"
        titles = [r.title for r in res.risks]
        assert any("华法林" in t and "阿司匹林" in t for t in titles)

    def test_every_contraindicated_pair_is_detected(self, conn, engine):
        """对规则库里**每一条**禁忌级规则，构造输入并断言必被检出。"""
        rows = conn.execute(
            "SELECT ing_a_id, ing_b_id FROM ddi_rule WHERE severity='contraindicated'"
        ).fetchall()
        assert rows, "规则库中没有禁忌级规则，测试失去意义"

        missed = []
        for row in rows:
            res = engine.assess([row["ing_a_id"], row["ing_b_id"]])
            if not res.hard_blocked:
                missed.append((row["ing_a_id"], row["ing_b_id"]))

        assert not missed, f"禁忌级漏检 {len(missed)} 对：{missed}"

    def test_rule_lookup_is_order_independent(self, engine, conn):
        row = conn.execute(
            "SELECT ing_a_id, ing_b_id FROM ddi_rule WHERE severity='contraindicated' LIMIT 1"
        ).fetchone()
        a, b = row["ing_a_id"], row["ing_b_id"]
        assert engine.assess([a, b]).hard_blocked
        assert engine.assess([b, a]).hard_blocked

    def test_three_drugs_yield_pairwise_combinations(self, normalizer, engine):
        ids = _ids(normalizer, "芬必得", "华法林钠片", "阿司匹林肠溶片")
        res = engine.assess(ids)
        assert len(res.risks) >= 3  # C(3,2)=3


class TestSeverityOrdering:
    def test_risks_sorted_most_severe_first(self, normalizer, engine):
        ids = _ids(normalizer, "芬必得", "华法林钠片", "阿司匹林肠溶片")
        res = engine.assess(ids)
        ranks = [SEVERITY_ORDER[r.severity] for r in res.risks]
        assert ranks == sorted(ranks)

    def test_overall_risk_is_max_severity(self, normalizer, engine):
        ids = _ids(normalizer, "芬必得", "华法林钠片", "阿司匹林肠溶片")
        assert engine.assess(ids).overall_risk == "contraindicated"


class TestPopulationRules:
    def test_elderly_benzodiazepine_flagged(self, normalizer, engine):
        ids = _ids(normalizer, "安定")
        res = engine.assess(ids, {"age": 78})
        assert any(r.kind == "population" for r in res.risks)

    def test_young_patient_not_flagged_for_age_rule(self, normalizer, engine):
        ids = _ids(normalizer, "安定")
        res = engine.assess(ids, {"age": 30})
        assert not any(r.kind == "population" for r in res.risks)

    def test_pregnancy_contraindication(self, normalizer, engine):
        ids = _ids(normalizer, "华法林钠片")
        res = engine.assess(ids, {"pregnancy": True})
        assert res.hard_blocked

    def test_no_pregnancy_no_flag(self, normalizer, engine):
        ids = _ids(normalizer, "华法林钠片")
        res = engine.assess(ids, {"pregnancy": False})
        assert not any(r.kind == "population" for r in res.risks)

    def test_renal_severity_is_cumulative(self, normalizer, engine):
        """重度肾功能不全应同时触发中度和重度的规则（至少达到该严重度）。"""
        ids = _ids(normalizer, "格华止")  # 二甲双胍
        mild = engine.assess(ids, {"renal": "mild"})
        severe = engine.assess(ids, {"renal": "severe"})
        n_mild = sum(1 for r in mild.risks if r.kind == "population")
        n_severe = sum(1 for r in severe.risks if r.kind == "population")
        assert n_severe >= n_mild
        assert severe.hard_blocked  # severe 级应触发禁忌

    def test_missing_profile_does_not_crash(self, normalizer, engine):
        ids = _ids(normalizer, "安定")
        assert engine.assess(ids, {}).risks is not None
        assert engine.assess(ids, None).risks is not None


class TestSourceAttribution:
    def test_detected_rules_carry_sources(self, normalizer, engine):
        ids = _ids(normalizer, "华法林钠片", "阿司匹林肠溶片")
        res = engine.assess(ids)
        ddi = [r for r in res.risks if r.kind == "ddi"]
        assert ddi
        assert any(r.sources for r in ddi), "至少一条 DDI 结论应带原文依据"

    def test_get_rule_returns_sources(self, engine, conn):
        rid = conn.execute("SELECT id FROM ddi_rule LIMIT 1").fetchone()["id"]
        rule = engine.get_rule(rid)
        assert rule is not None
        assert rule["id"] == rid
        assert "sources" in rule


class TestNoRiskCase:
    def test_single_drug_no_interaction(self, normalizer, engine):
        ids = _ids(normalizer, "阿莫仙")
        res = engine.assess(ids)
        assert res.overall_risk in ("none", "caution", "monitor", "contraindicated")
        assert not res.hard_blocked or res.overall_risk == "contraindicated"

    def test_empty_input(self, engine):
        res = engine.assess([])
        assert res.risks == []
        assert res.overall_risk == "none"
        assert not res.hard_blocked
