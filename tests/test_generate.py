"""M3 检索 与 M4 生成的安全性测试。

M4 的重点不是"模型答得好不好"，而是**模型答错时系统会不会跟着错**。
"""

from __future__ import annotations

import pytest

from ddi.generate.generator import recheck
from ddi.generate.llm_client import extract_json
from ddi.generate.prompts import DISCLAIMER, fallback_summary
from ddi.rag.retriever import Retriever, rrf_fuse


class TestRRF:
    def test_single_list_preserves_order(self):
        scores = rrf_fuse([[1, 2, 3]])
        assert scores[1] > scores[2] > scores[3]

    def test_agreement_across_lists_ranks_higher(self):
        # 1 在两路都排第一；2 只在第二路第一
        scores = rrf_fuse([[1, 3], [1, 2]])
        assert scores[1] > scores[2]

    def test_k_parameter_dampens(self):
        small = rrf_fuse([[1]], k=1)
        large = rrf_fuse([[1]], k=60)
        assert small[1] > large[1]

    def test_empty_input(self):
        assert rrf_fuse([]) == {}
        assert rrf_fuse([[], []]) == {}


class TestExtractJson:
    def test_plain_json(self):
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_fenced_json(self):
        assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}

    def test_fenced_without_language(self):
        assert extract_json('```\n{"a": 1}\n```') == {"a": 1}

    def test_json_with_surrounding_prose(self):
        text = '好的，这是结果：\n{"a": 1}\n希望对你有帮助。'
        assert extract_json(text) == {"a": 1}

    def test_nested_braces(self):
        assert extract_json('前缀 {"a": {"b": 2}} 后缀') == {"a": {"b": 2}}

    def test_garbage_returns_none(self):
        assert extract_json("完全不是 JSON") is None
        assert extract_json("") is None

    def test_json_array_rejected(self):
        # 顶层必须是对象，数组无法映射到我们的 schema
        assert extract_json("[1,2,3]") is None


class TestRecheck:
    """二次校验是整个防幻觉设计的闸门，必须逐条覆盖。"""

    @pytest.fixture
    def assessed(self, normalizer, engine):
        # 用左甲状腺素钠 + 奥美拉唑：确有一条「慎用」规则，便于构造比对场景
        ids = normalizer.ingredients_of(
            [
                normalizer.normalize("优甲乐").matched.drug_id,
                normalizer.normalize("洛赛克").matched.drug_id,
            ]
        )
        result = engine.assess(ids, {"age": 45})
        assert result.risks, "测试前置条件失败：该组合应产生风险"
        return result

    def test_matching_levels_pass(self, assessed):
        rid = assessed.risks[0].rule_id
        payload = {"items": [{"rule_id": rid, "level": assessed.risks[0].severity}]}
        passed, notes = recheck(payload, assessed)
        assert passed and not notes

    def test_downgraded_level_fails(self, assessed):
        """模型把禁忌降级成关注 —— 这是最危险的幻觉，必须拦住。"""
        risk = assessed.risks[0]
        downgraded = "monitor" if risk.severity != "monitor" else "caution"
        payload = {"items": [{"rule_id": risk.rule_id, "level": downgraded}]}
        passed, notes = recheck(payload, assessed)
        assert not passed
        assert notes

    def test_fabricated_rule_id_fails(self, assessed):
        payload = {"items": [{"rule_id": 999999, "level": "monitor"}]}
        passed, notes = recheck(payload, assessed)
        assert not passed
        assert any("999999" in n for n in notes)

    def test_empty_items_passes(self, assessed):
        passed, _ = recheck({"items": []}, assessed)
        assert passed


class TestFallback:
    def test_no_risk_summary(self):
        out = fallback_summary([])
        assert out["items"] == []
        assert out["disclaimer"] == DISCLAIMER

    def test_summary_counts_by_level(self):
        items = [
            {"rule_id": 1, "level": "contraindicated", "level_cn": "禁忌",
             "mechanism": "m", "consequence": "c", "suggestion": "s"},
            {"rule_id": 2, "level": "caution", "level_cn": "慎用",
             "mechanism": "m", "consequence": "c", "suggestion": "s"},
        ]
        out = fallback_summary(items)
        assert "2 项" in out["summary"]
        assert len(out["items"]) == 2

    def test_disclaimer_always_present(self):
        items = [{"rule_id": 1, "level": "monitor", "level_cn": "关注",
                  "mechanism": "m", "consequence": "c", "suggestion": "s"}]
        assert fallback_summary(items)["disclaimer"] == DISCLAIMER


class TestRetriever:
    def test_retrieve_returns_hits(self, indexed):
        build_dir, emb = indexed
        r = Retriever(build_dir=build_dir, embedder=emb).load()
        hits = r.retrieve("华法林 阿司匹林 出血", top_k=3)
        assert hits
        assert len(hits) <= 3
        assert any("华法林" in "".join(h.drugs) for h in hits)

    def test_excluding_filters_out(self, indexed):
        build_dir, emb = indexed
        r = Retriever(build_dir=build_dir, embedder=emb).load()
        first = r.retrieve("华法林 阿司匹林", top_k=1)[0]
        out = r.retrieve_excluding("华法林 阿司匹林", {first.rule_id}, top_k=3)
        assert all(h.rule_id != first.rule_id for h in out)

    def test_hits_are_serializable(self, indexed):
        build_dir, emb = indexed
        r = Retriever(build_dir=build_dir, embedder=emb).load()
        for h in r.retrieve("他汀 肌痛", top_k=2):
            assert set(h.to_dict()) >= {"rule_id", "severity", "drugs", "score", "text"}
