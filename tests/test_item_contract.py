"""API 返回条目的**结构契约**测试。

这个文件是为了防住一类具体的回归：禁忌级硬拦截路径不经过大模型，
如果生成层换一套字段名，前端就会在**最重要的一条路径上**拿到缺字段的结果 ——
而其他路径的测试全绿，问题要到用户投诉才暴露。

契约：无论走哪条路径，``items[]`` 的每一项都必须有同样的键。

注意：这里一律传 ``use_rag=False``。这些用例验的是条目结构，与检索无关，
而构造真实的 ``DDIService`` 会拉起嵌入模型 —— 配上 m3e 就是 40 秒冷启动，
整个测试套件会被这一个文件拖慢十倍。
"""

from __future__ import annotations

import pytest

# 前端渲染一条风险时会读的键，一个都不能少
REQUIRED_ITEM_KEYS = {
    "rule_id",
    "level",
    "level_cn",
    "title",
    "drugs",
    "mechanism",
    "consequence",
    "suggestion",
    "sources",
}

# 禁忌级：走硬拦截，完全不经过大模型
BLOCKED_DRUGS = ["芬必得", "华法林钠片"]
# 慎用级：会走大模型
LLM_DRUGS = ["华法林钠片", "阿莫西林"]


class _FakeLLM:
    """可控的假模型，避免测试依赖真实 API。"""

    model = "fake-model"
    base_url = "https://example.invalid/anthropic"
    provider_label = "Fake"

    def __init__(self, payload=None, configured=True):
        self.payload = payload
        self._configured = configured

    @property
    def configured(self):
        return self._configured

    def complete_json(self, system, user, **kw):
        from ddi.generate.llm_client import LLMResponse

        return self.payload, LLMResponse(text="", model=self.model)


def _service(**kw):
    from ddi.service import DDIService

    return DDIService(**kw)


def _first_rule(svc, drugs):
    """跑一遍规则引擎，取第一条风险 —— 用来给假模型回填真实的 rule_id。"""
    ids = [r.matched.drug_id for r in svc.normalizer.normalize_many(drugs) if r.matched]
    return svc.engine.assess(svc.normalizer.ingredients_of(ids), {})


class TestItemContract:
    def test_hard_blocked_items_have_all_keys(self):
        """硬拦截路径不经过大模型 —— 最容易漏字段的一条路。"""
        report = _service().assess(BLOCKED_DRUGS, {"age": 68}, use_rag=False)
        assert report.hard_blocked, "该组合应当是禁忌级，测试前提不成立"
        assert report.items, "禁忌级必须至少产出一条风险"
        for item in report.items:
            missing = REQUIRED_ITEM_KEYS - set(item)
            assert not missing, f"硬拦截路径缺字段：{missing}"

    def test_degraded_items_have_all_keys(self):
        """无 key 降级路径（纯规则答案）同样要结构完整。"""
        report = _service(llm=_FakeLLM(configured=False)).assess(BLOCKED_DRUGS, {"age": 68}, use_rag=False)
        for item in report.items:
            missing = REQUIRED_ITEM_KEYS - set(item)
            assert not missing, f"降级路径缺字段：{missing}"

    def test_llm_path_items_have_all_keys(self):
        """模型只给 rule_id/level/explanation/suggestion 时，其余字段要由规则库补齐。"""
        payload = {
            "summary": "模型写的摘要",
            "items": [
                {"rule_id": None, "level": "caution", "explanation": "模型解释",
                 "suggestion": "模型建议"},
            ],
            "patient_note": None,
            "disclaimer": "x",
        }
        # 用真实规则 id 回填，模拟模型正常作答
        svc = _service(llm=_FakeLLM(payload=payload))
        base = _first_rule(svc, LLM_DRUGS)
        if not base.risks:
            pytest.skip("该组合在当前规则库中没有风险，测试前提不成立")
        payload["items"][0]["rule_id"] = base.risks[0].rule_id
        payload["items"][0]["level"] = base.risks[0].severity

        report = svc.assess(LLM_DRUGS, {}, use_rag=False)
        for item in report.items:
            missing = REQUIRED_ITEM_KEYS - set(item)
            assert not missing, f"LLM 路径缺字段：{missing}"


class TestFieldProvenance:
    """字段归属：可执行的文本必须来自规则库，不能来自模型现场措辞。"""

    def test_title_and_advice_come_from_rule_library(self):
        payload = {
            "summary": "模型摘要",
            "items": [{"rule_id": None, "level": "caution",
                       "explanation": "模型解释", "suggestion": "模型建议"}],
            "patient_note": None,
            "disclaimer": "x",
        }
        svc = _service(llm=_FakeLLM(payload=payload))
        ids = [r.matched.drug_id for r in svc.normalizer.normalize_many(LLM_DRUGS) if r.matched]
        base = svc.engine.assess(svc.normalizer.ingredients_of(ids), {})
        if not base.risks:
            pytest.skip("该组合在当前规则库中没有风险，测试前提不成立")

        rule = base.risks[0]
        payload["items"][0]["rule_id"] = rule.rule_id
        payload["items"][0]["level"] = rule.severity

        item = svc.assess(LLM_DRUGS, {}, use_rag=False).items[0]
        assert item["title"] == rule.title
        assert item["mechanism"] == rule.mechanism
        assert item["consequence"] == rule.consequence
        assert item["suggestion"] == rule.suggestion, "建议文本被模型改写，不应发生"
        assert item["sources"] == rule.sources

    def test_level_always_comes_from_rule_engine(self):
        """等级由规则引擎决定，不由模型决定 —— 这是结构保证，不是提示词约定。

        故意让模型把 monitor 说成 contraindicated（更严重的方向），
        断言返回值仍取规则引擎的等级。
        """
        payload = {
            "summary": "模型摘要",
            "items": [{"rule_id": None, "level": "contraindicated",
                       "explanation": "模型把等级说高了", "suggestion": "模型建议"}],
            "patient_note": None,
            "disclaimer": "x",
        }
        svc = _service(llm=_FakeLLM(payload=payload))
        base = _first_rule(svc, LLM_DRUGS)
        if not base.risks:
            pytest.skip("该组合在当前规则库中没有风险，测试前提不成立")

        rule = base.risks[0]
        payload["items"][0]["rule_id"] = rule.rule_id

        report = svc.assess(LLM_DRUGS, {}, use_rag=False)
        # 二次校验会先把整体打回规则答案，所以这里断言的是最终对外结果
        for item in report.items:
            assert item["level"] == rule.severity, "等级没有以规则引擎为准"
            assert item["level_cn"] == rule.severity_cn

    def test_every_item_carries_sources(self):
        """可溯源是本项目的核心承诺 —— 没有依据的结论不该出现在结果里。"""
        report = _service().assess(BLOCKED_DRUGS, {"age": 68}, use_rag=False)
        for item in report.items:
            assert item["sources"], f"规则 {item['rule_id']} 没有溯源依据"
            for s in item["sources"]:
                assert s.get("title"), "来源缺标题"
                assert s.get("excerpt"), "来源缺摘录"


class TestHardBlockNeverCallsLLM:
    def test_llm_is_not_invoked_on_contraindicated(self):
        """禁忌级不允许被模型改写 —— 因此根本不该发请求。"""
        llm = _FakeLLM(payload=None)
        calls = []
        llm.complete_json = lambda *a, **k: calls.append(1)  # type: ignore[assignment]

        report = _service(llm=llm).assess(BLOCKED_DRUGS, {"age": 68}, use_rag=False)
        assert report.hard_blocked
        assert not calls, "命中禁忌级却调用了大模型"
        assert report.engine["used_llm"] is False
