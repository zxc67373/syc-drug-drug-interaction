"""M1 归一化测试。

重点是**不猜**这条底线：识别不了就必须返回候选让用户确认，不能给个错答案。
"""

from __future__ import annotations

import pytest

from ddi.normalize import FUZZY_ACCEPT, clean_name


class TestCleanName:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("阿司匹林肠溶片100mg", "阿司匹林"),
            ("布洛芬缓释胶囊", "布洛芬"),
            ("芬必得", "芬必得"),
            ("华法林钠片 2.5mg", "华法林钠"),
            ("阿托伐他汀钙片(立普妥)", "阿托伐他汀钙"),
            ("左甲状腺素钠片 50μg", "左甲状腺素钠"),
            (" 阿莫西林胶囊 ", "阿莫西林"),
        ],
    )
    def test_strips_strength_and_form(self, raw, expected):
        assert clean_name(raw) == expected

    def test_does_not_strip_when_name_is_only_suffix(self):
        # "片" 本身就是药名时不能被剥成空串
        assert clean_name("片") == "片"


class TestExactMatch:
    def test_brand_name(self, normalizer):
        r = normalizer.normalize("芬必得")
        assert r.ok
        assert r.matched.name_cn == "布洛芬缓释胶囊"
        assert r.matched.confidence == 1.0
        assert r.matched.match_type == "exact"

    def test_generic_with_strength(self, normalizer):
        r = normalizer.normalize("阿司匹林肠溶片100mg")
        assert r.ok
        assert "阿司匹林" in r.matched.ingredient_names
        assert r.matched.confidence >= 0.98

    def test_alias(self, normalizer):
        r = normalizer.normalize("安定")
        assert r.ok
        assert "地西泮" in r.matched.ingredient_names

    def test_trade_name(self, normalizer):
        r = normalizer.normalize("优甲乐")
        assert r.ok
        assert "左甲状腺素钠" in r.matched.ingredient_names


class TestNotGuessing:
    def test_gibberish_returns_no_match_with_candidates(self, normalizer):
        r = normalizer.normalize("xyz乱输入abc")
        if r.ok:
            # 若勉强匹配上，置信度必须低于接受阈值，且必须要求确认
            assert r.matched.confidence < FUZZY_ACCEPT / 100 or r.needs_confirmation
        else:
            assert r.notes, "未匹配时必须给出说明"
            assert r.candidates, "未匹配时应给出候选供用户选择"

    def test_empty_input(self, normalizer):
        r = normalizer.normalize("   ")
        assert not r.ok
        assert r.notes


class TestConfidenceScale:
    def test_all_confidences_are_unit_scale(self, normalizer):
        """候选与命中必须同一个尺度 —— 混用 0-1 和 0-100 会让阈值判断失效。"""
        for q in ["芬必得", "阿司匹林肠溶", "他汀", "完全不存在的药"]:
            r = normalizer.normalize(q)
            for c in r.candidates:
                assert 0.0 <= c.confidence <= 1.0, f"{q}: 候选置信度越界 {c.confidence}"
            if r.matched:
                assert 0.0 <= r.matched.confidence <= 1.0


class TestIngredientExpansion:
    def test_ingredients_of_drug_ids(self, normalizer):
        r = normalizer.normalize("芬必得")
        ings = normalizer.ingredients_of([r.matched.drug_id])
        assert len(ings) == 1

    def test_unknown_drug_id_yields_empty(self, normalizer):
        assert normalizer.ingredients_of([99999]) == []
