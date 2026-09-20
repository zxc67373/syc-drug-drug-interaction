"""API 层测试。

用假的 LLM 客户端 —— API 测试不该依赖网络，也不该烧 token。
"""

from __future__ import annotations

import pytest

from ddi.generate.llm_client import LLMResponse, LLMUnavailable, extract_json


class FakeLLM:
    """可控的假模型。configured=False 时模拟未配置 key 的降级路径。"""

    model = "fake-model"
    base_url = "https://example.invalid/anthropic"
    provider_label = "FakeProvider"

    def __init__(self, items: list[dict] | None = None, configured: bool = True):
        self.items = items or []
        self._configured = configured
        self.calls = 0

    @property
    def configured(self) -> bool:
        return self._configured

    @property
    def payload(self) -> str:
        import json

        return json.dumps(
            {
                "summary": "测试摘要",
                "items": self.items,
                "patient_note": None,
                "disclaimer": "由模型输出，应被服务端覆盖",
            },
            ensure_ascii=False,
        )

    def complete_json(self, system, user, thinking=True):
        self.calls += 1
        if not self._configured:
            raise LLMUnavailable("未配置")
        return extract_json(self.payload), LLMResponse(text=self.payload, model=self.model)

    def complete(self, system, user, **kw):
        self.calls += 1
        return LLMResponse(text=self.payload, model=self.model)


@pytest.fixture
def app(seed_db, tmp_path):
    from ddi.db.session import session
    from ddi.normalize import Normalizer
    from ddi.rag.embedder import HashEmbedder
    from ddi.rag.index import build_index
    from ddi.rag.retriever import Retriever
    from ddi.rules import RuleEngine
    from ddi.service import DDIService

    # 测试用独立索引，不读也不写 data/build/
    build_dir = tmp_path / "idx"
    emb = HashEmbedder()
    build_index(build_dir=build_dir, embedder=emb, db=seed_db)
    retriever = Retriever(build_dir=build_dir, embedder=emb).load()

    with session(seed_db) as c:
        svc = DDIService(
            normalizer=Normalizer(c),
            engine=RuleEngine(c),
            retriever=retriever,
            llm=FakeLLM(),
        )
    from ddi.api import create_app

    application = create_app(svc)
    application.config.update(TESTING=True)
    return application


@pytest.fixture
def client(app):
    return app.test_client()


class TestHealth:
    def test_health_reports_state(self, client):
        r = client.get("/api/v1/health")
        assert r.status_code == 200
        body = r.get_json()
        assert body["status"] == "ok"
        assert body["rules"] > 0
        assert body["drugs_indexed"] > 0


class TestAssessValidation:
    def test_missing_drugs(self, client):
        r = client.post("/api/v1/assess", json={})
        assert r.status_code == 400

    def test_empty_drugs(self, client):
        r = client.post("/api/v1/assess", json={"drugs": []})
        assert r.status_code == 400

    def test_too_many_drugs(self, client):
        r = client.post("/api/v1/assess", json={"drugs": ["布洛芬"] * 25})
        assert r.status_code == 400

    def test_bad_age(self, client):
        r = client.post("/api/v1/assess", json={"drugs": ["芬必得"], "profile": {"age": "abc"}})
        assert r.status_code == 400

    def test_age_out_of_range(self, client):
        r = client.post("/api/v1/assess", json={"drugs": ["芬必得"], "profile": {"age": 999}})
        assert r.status_code == 400

    def test_unknown_profile_keys_ignored(self, client):
        r = client.post(
            "/api/v1/assess",
            json={"drugs": ["芬必得"], "profile": {"age": 40, "身份证号": "x"}},
        )
        assert r.status_code == 200


class TestAssessResponse:
    def test_hard_block_skips_llm(self, app, client):
        fake = app.config["DDI_SERVICE"].llm
        before = fake.calls
        r = client.post(
            "/api/v1/assess",
            json={"drugs": ["华法林钠片", "阿司匹林肠溶片", "芬必得"], "profile": {"age": 68}},
        )
        assert r.status_code == 200
        body = r.get_json()
        assert body["hard_blocked"] is True
        assert body["overall_risk"] == "contraindicated"
        assert fake.calls == before, "禁忌级硬拦截时不应调用大模型"

    def test_items_carry_level_cn_and_sources(self, client):
        r = client.post(
            "/api/v1/assess",
            json={"drugs": ["华法林钠片", "阿司匹林肠溶片"], "profile": {"age": 68}},
        )
        body = r.get_json()
        assert body["items"]
        for it in body["items"]:
            assert it["level_cn"]
            assert "sources" in it

    def test_disclaimer_always_returned(self, client):
        r = client.post("/api/v1/assess", json={"drugs": ["阿莫仙"]})
        assert r.get_json()["disclaimer"]

    def test_unrecognized_drug_reported_not_guessed(self, client):
        r = client.post("/api/v1/assess", json={"drugs": ["完全不存在的药名xyz"]})
        body = r.get_json()
        assert r.status_code == 200
        unmatched = [d for d in body["drugs"] if d["drug_id"] is None]
        assert unmatched, "无法识别的药应标记为未匹配"
        assert unmatched[0]["candidates"], "未匹配时应给出候选"

    def test_use_llm_false_skips_model(self, app, client):
        fake = app.config["DDI_SERVICE"].llm
        before = fake.calls
        client.post(
            "/api/v1/assess",
            json={"drugs": ["优甲乐", "洛赛克"], "use_llm": False},
        )
        assert fake.calls == before


class TestNormalizeEndpoint:
    def test_returns_results(self, client):
        r = client.post("/api/v1/normalize", json={"drugs": ["芬必得", "乱输入xyz"]})
        assert r.status_code == 200
        results = r.get_json()["results"]
        assert len(results) == 2
        assert results[0]["matched"]["name_cn"] == "布洛芬缓释胶囊"

    def test_empty_rejected(self, client):
        assert client.post("/api/v1/normalize", json={"drugs": []}).status_code == 400


class TestRuleEndpoint:
    def test_existing_rule(self, client, conn):
        rid = conn.execute("SELECT id FROM ddi_rule LIMIT 1").fetchone()["id"]
        r = client.get(f"/api/v1/rules/{rid}")
        assert r.status_code == 200
        assert r.get_json()["id"] == rid

    def test_missing_rule_404(self, client):
        assert client.get("/api/v1/rules/999999").status_code == 404
