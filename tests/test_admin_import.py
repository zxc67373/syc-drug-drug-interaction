"""智能导入 API 测试：解析 → 暂存 → 编辑 → 确认入库 / 丢弃。

fixture 与 test_admin.py 同款（monkeypatch settings 带 token + 临时库）。
LLM 输出用 FakeLLM.payload_obj 注入，不烧真 token。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

# 不写 `from tests.test_api import …`：/root/zhihu-cli/tests 是带 __init__.py
# 的正包，会遮住本目录的命名空间包（历史踩坑）。
from test_api import FakeLLM


@pytest.fixture
def app(seed_db, tmp_path, monkeypatch):
    from ddi.config import settings
    from ddi.db.session import session
    from ddi.normalize import Normalizer
    from ddi.rag.embedder import HashEmbedder
    from ddi.rag.index import build_index
    from ddi.rag.retriever import Retriever
    from ddi.rules import RuleEngine
    from ddi.service import DDIService

    import importlib

    import ddi.api.admin as admin_mod
    import ddi.config as config_mod

    token = "test-token"
    patched = replace(settings, admin_token=token, db_path=seed_db)
    monkeypatch.setattr(admin_mod, "settings", patched)
    monkeypatch.setattr(config_mod, "settings", patched)
    session_mod = importlib.import_module("ddi.db.session")
    monkeypatch.setattr(session_mod, "settings", patched)

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
    application.config["TEST_ADMIN_TOKEN"] = token
    yield application
    monkeypatch.undo()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def auth(app):
    return {"Authorization": "Bearer " + app.config["TEST_ADMIN_TOKEN"]}


def _rule_candidate(a="ZZ导入成分甲", b="ZZ导入成分乙", **fields_over) -> dict:
    fields = {
        "ing_a_name": a,
        "ing_b_name": b,
        "severity": "caution",
        "mechanism": "测试机制",
        "consequence": "测试后果",
        "suggestion": "测试建议",
        "evidence_level": "B",
        "sources": [{
            "title": "ZZ导入测试说明书",
            "source_type": "说明书",
            "year": 2020,
            "excerpt": "测试摘录",
        }],
    }
    fields.update(fields_over)
    return {
        "kind": "ddi_rule",
        "raw_span": f"{a} + {b} 原文片段",
        "fields": fields,
        "warnings": [],
    }


def _drug_candidate(name="ZZ导入药品甲", **fields_over) -> dict:
    fields = {
        "name_cn": name,
        "dosage_form": "胶囊",
        "trade_names": ["ZZ导入商品"],
        "aliases": [],
        "is_otc": False,
        "is_tcm": False,
        "approval_no": None,
        "ingredients": [
            {"name": "ZZ导入成分甲", "strength": "0.25g"},
            {"name": "ZZ导入成分乙"},
        ],
    }
    fields.update(fields_over)
    return {
        "kind": "drug",
        "raw_span": f"{name} 说明书片段",
        "fields": fields,
        "warnings": [],
    }


def _cleanup_drug(seed_db, drug_name, ing_names=()):
    from ddi.db.session import session as db_session

    with db_session(seed_db) as c:
        c.execute("DELETE FROM drug WHERE name_cn = ?", (drug_name,))
        for n in ing_names:
            c.execute("DELETE FROM ingredient WHERE name_cn = ?", (n,))


def _stage(client, auth, app, candidates, text="原文") -> dict:
    """注入 payload 并调 parse，返回响应体。"""
    app.config["DDI_SERVICE"].llm.payload_obj = {"candidates": candidates}
    r = client.post("/api/v1/admin/import/parse", headers=auth,
                    json={"text": text})
    assert r.status_code == 201, r.get_json()
    return r.get_json()


def _cleanup(seed_db, names=(), pair=None):
    """清测试残留。pair=(a, b)：先删该成分对的规则再删成分（FK 顺序）。"""
    from ddi.db.session import session as db_session

    with db_session(seed_db) as c:
        if pair:
            ids = []
            for n in pair:
                row = c.execute(
                    "SELECT id FROM ingredient WHERE name_cn = ?", (n,)
                ).fetchone()
                ids.append(row["id"] if row else None)
            if all(i is not None for i in ids):
                x, y = sorted(ids)
                c.execute(
                    "DELETE FROM ddi_rule WHERE ing_a_id = ? AND ing_b_id = ?",
                    (x, y),
                )
        c.execute("DELETE FROM import_draft")
        c.execute(
            "DELETE FROM rule_source WHERE rule_id NOT IN (SELECT id FROM ddi_rule)"
        )
        c.execute("DELETE FROM source WHERE title = 'ZZ导入测试说明书'")
        for n in names:
            c.execute("DELETE FROM ingredient WHERE name_cn = ?", (n,))


class TestParse:
    def test_requires_auth(self, client):
        r = client.post("/api/v1/admin/import/parse", json={"text": "x"})
        assert r.status_code == 401

    def test_empty_text_400(self, client, auth):
        r = client.post("/api/v1/admin/import/parse", headers=auth,
                        json={"text": "   "})
        assert r.status_code == 400

    def test_text_too_long_400(self, client, auth):
        r = client.post("/api/v1/admin/import/parse", headers=auth,
                        json={"text": "x" * 8001})
        assert r.status_code == 400
        assert "过长" in r.get_json()["error"]

    def test_happy_path_stages_drafts(self, client, auth, app, seed_db):
        body = _stage(client, auth, app, [_rule_candidate(), _rule_candidate(
            a="ZZ导入成分丙", b="ZZ导入成分丁")])
        assert body["ok"] is True
        assert body["batch_id"]
        assert len(body["drafts"]) == 2
        assert body["drafts"][0]["kind"] == "ddi_rule"

        listed = client.get("/api/v1/admin/import/drafts",
                            headers=auth).get_json()
        assert listed["total"] >= 2
        assert all(i["status"] == "pending" for i in listed["items"])

        # 必须清掉：后续用例断言 total==0，残留会把它们弄红
        _cleanup(seed_db)

    def test_llm_down_writes_nothing(self, client, auth, app, seed_db):
        app.config["DDI_SERVICE"].llm = FakeLLM(configured=False)
        r = client.post("/api/v1/admin/import/parse", headers=auth,
                        json={"text": "华法林与阿司匹林"})
        assert r.status_code == 502
        assert "未配置" in r.get_json()["degraded_reason"]
        # 一行都不能写
        listed = client.get("/api/v1/admin/import/drafts", headers=auth)
        assert listed.get_json()["total"] == 0
        _cleanup(seed_db)

    def test_unparseable_writes_nothing(self, client, auth, app, seed_db):
        app.config["DDI_SERVICE"].llm.raw_text = "不是 JSON"
        r = client.post("/api/v1/admin/import/parse", headers=auth,
                        json={"text": "华法林与阿司匹林"})
        assert r.status_code == 502
        listed = client.get("/api/v1/admin/import/drafts", headers=auth)
        assert listed.get_json()["total"] == 0
        _cleanup(seed_db)


class TestDraftLifecycle:
    def test_get_put_delete(self, client, auth, app):
        body = _stage(client, auth, app, [_rule_candidate()])
        did = body["drafts"][0]["id"]

        got = client.get(f"/api/v1/admin/import/drafts/{did}", headers=auth)
        assert got.status_code == 200
        assert got.get_json()["fields"]["severity"] == "caution"

        # 编辑：改严重度 → 立刻反映
        fields = got.get_json()["fields"]
        fields["severity"] = "monitor"
        put = client.put(f"/api/v1/admin/import/drafts/{did}", headers=auth,
                         json={"fields": fields})
        assert put.status_code == 200
        assert client.get(
            f"/api/v1/admin/import/drafts/{did}", headers=auth
        ).get_json()["fields"]["severity"] == "monitor"

        # 非法字段硬校验 400
        bad = dict(fields, severity="nuke")
        assert client.put(
            f"/api/v1/admin/import/drafts/{did}", headers=auth,
            json={"fields": bad},
        ).status_code == 400

        # 丢弃 → 404
        assert client.delete(
            f"/api/v1/admin/import/drafts/{did}", headers=auth
        ).status_code == 200
        assert client.get(
            f"/api/v1/admin/import/drafts/{did}", headers=auth
        ).status_code == 404

    def test_get_missing_404(self, client, auth):
        assert client.get("/api/v1/admin/import/drafts/999999",
                          headers=auth).status_code == 404

    def test_list_default_only_pending(self, client, auth, app, seed_db):
        body = _stage(client, auth, app, [_rule_candidate()])
        did = body["drafts"][0]["id"]
        assert client.post(
            f"/api/v1/admin/import/drafts/{did}/confirm", headers=auth,
            json={},
        ).status_code == 201

        pending = client.get("/api/v1/admin/import/drafts",
                             headers=auth).get_json()
        assert all(i["id"] != did for i in pending["items"])
        confirmed = client.get(
            "/api/v1/admin/import/drafts?status=confirmed", headers=auth
        ).get_json()
        assert any(i["id"] == did for i in confirmed["items"])
        _cleanup(seed_db, ("ZZ导入成分甲", "ZZ导入成分乙"),
                 ("ZZ导入成分甲", "ZZ导入成分乙"))


class TestConfirm:
    def test_confirm_ddi_rule(self, client, auth, app, seed_db):
        # 预清：上一次跑挂掉留下的残留会让 created_ingredients 断言变假
        _cleanup(seed_db, ("ZZ导入成分甲", "ZZ导入成分乙"),
                 pair=("ZZ导入成分甲", "ZZ导入成分乙"))
        body = _stage(client, auth, app, [_rule_candidate()])
        did = body["drafts"][0]["id"]

        r = client.post(f"/api/v1/admin/import/drafts/{did}/confirm",
                        headers=auth, json={})
        assert r.status_code == 201
        result = r.get_json()
        rid = result["target_id"]
        # 未知成分自动创建
        assert set(result["created_ingredients"]) == {
            "ZZ导入成分甲", "ZZ导入成分乙"}

        # 公开详情立刻可见（confirm 内部 reload_rules 生效）
        pub = client.get(f"/api/v1/rules/{rid}")
        assert pub.status_code == 200
        assert pub.get_json()["severity"] == "caution"
        assert pub.get_json()["status"] == "reviewed"  # 确认即审核

        # 溯源已写入
        detail = client.get(f"/api/v1/admin/rules/{rid}", headers=auth)
        assert detail.get_json()["source_count"] == 1
        assert detail.get_json()["sources"][0]["title"] == "ZZ导入测试说明书"

        # 成分对已规范化（ing_a_id < ing_b_id）
        assert detail.get_json()["ing_a_id"] < detail.get_json()["ing_b_id"]

        # 草稿状态翻转 + target_id
        draft = client.get(f"/api/v1/admin/import/drafts/{did}",
                           headers=auth).get_json()
        assert draft["status"] == "confirmed"
        assert draft["target_id"] == rid

        # 清理
        client.delete(f"/api/v1/admin/rules/{rid}", headers=auth)
        _cleanup(seed_db, ("ZZ导入成分甲", "ZZ导入成分乙"))

    def test_confirm_twice_409(self, client, auth, app, seed_db):
        body = _stage(client, auth, app, [_rule_candidate(
            a="ZZ导入成分戊", b="ZZ导入成分己")])
        did = body["drafts"][0]["id"]
        first = client.post(f"/api/v1/admin/import/drafts/{did}/confirm",
                            headers=auth, json={})
        assert first.status_code == 201
        second = client.post(f"/api/v1/admin/import/drafts/{did}/confirm",
                             headers=auth, json={})
        assert second.status_code == 409
        rid = first.get_json()["target_id"]
        client.delete(f"/api/v1/admin/rules/{rid}", headers=auth)
        _cleanup(seed_db, ("ZZ导入成分戊", "ZZ导入成分己"),
                 ("ZZ导入成分戊", "ZZ导入成分己"))

    def test_confirm_duplicate_pair_409(self, client, auth, app, seed_db):
        # 造一个与既有规则同成分对的草稿（取库里已有规则的两个成分）
        from ddi.db.session import session as db_session

        with db_session(seed_db) as c:
            row = c.execute(
                "SELECT ia.name_cn AS a, ib.name_cn AS b FROM ddi_rule r"
                " JOIN ingredient ia ON ia.id = r.ing_a_id"
                " JOIN ingredient ib ON ib.id = r.ing_b_id LIMIT 1"
            ).fetchone()
        body = _stage(client, auth, app, [
            _rule_candidate(a=row["a"], b=row["b"])
        ])
        did = body["drafts"][0]["id"]
        r = client.post(f"/api/v1/admin/import/drafts/{did}/confirm",
                        headers=auth, json={})
        assert r.status_code == 409
        assert "已有规则" in r.get_json()["error"]
        # 草稿还在 pending（失败不消耗草稿）
        draft = client.get(f"/api/v1/admin/import/drafts/{did}",
                           headers=auth).get_json()
        assert draft["status"] == "pending"
        _cleanup(seed_db)

    def test_confirm_bad_fields_400(self, client, auth, app, seed_db):
        body = _stage(client, auth, app, [_rule_candidate()])
        did = body["drafts"][0]["id"]
        # 直接改库里的 fields 成非法值，绕过 PUT 的校验
        from ddi.db.session import session as db_session

        import json as _json

        with db_session(seed_db) as c:
            row = c.execute(
                "SELECT fields FROM import_draft WHERE id = ?", (did,)
            ).fetchone()
            fields = _json.loads(row["fields"])
            fields["severity"] = "nuke"
            c.execute("UPDATE import_draft SET fields = ? WHERE id = ?",
                      (_json.dumps(fields, ensure_ascii=False), did))
        r = client.post(f"/api/v1/admin/import/drafts/{did}/confirm",
                        headers=auth, json={})
        assert r.status_code == 400
        _cleanup(seed_db)

    def test_confirm_missing_404(self, client, auth):
        r = client.post("/api/v1/admin/import/drafts/999999/confirm",
                        headers=auth, json={})
        assert r.status_code == 404

    def test_confirm_sources_upsert_reuses_id(self, client, auth, app, seed_db):
        """两条草稿同 (title, year) → 确认后复用同一 source 行（UPSERT 语义）。"""
        body = _stage(client, auth, app, [
            _rule_candidate(a="ZZ导入成分甲", b="ZZ导入成分乙"),
            _rule_candidate(a="ZZ导入成分庚", b="ZZ导入成分辛"),
        ])
        ids = [d["id"] for d in body["drafts"]]
        targets = []
        for did in ids:
            r = client.post(f"/api/v1/admin/import/drafts/{did}/confirm",
                            headers=auth, json={})
            assert r.status_code == 201, r.get_json()
            targets.append(r.get_json()["target_id"])

        from ddi.db.session import session as db_session

        with db_session(seed_db) as c:
            n = c.execute(
                "SELECT COUNT(*) AS n FROM source WHERE title = 'ZZ导入测试说明书'"
            ).fetchone()["n"]
            assert n == 1, "同 (title, year) 应复用同一 source 行"

        for rid in targets:
            client.delete(f"/api/v1/admin/rules/{rid}", headers=auth)
        _cleanup(seed_db, ("ZZ导入成分甲", "ZZ导入成分乙", "ZZ导入成分庚",
                           "ZZ导入成分辛"),
                 ("ZZ导入成分庚", "ZZ导入成分辛"))


class TestConfirmDrug:
    """药品草稿：确认 → drug + drug_ingredient，未知成分自动创建。"""

    def test_confirm_drug(self, client, auth, app, seed_db):
        _cleanup_drug(seed_db, "ZZ导入药品甲",
                      ["ZZ导入成分甲", "ZZ导入成分乙"])
        body = _stage(client, auth, app, [_drug_candidate()])
        did = body["drafts"][0]["id"]
        assert body["drafts"][0]["kind"] == "drug"

        r = client.post(f"/api/v1/admin/import/drafts/{did}/confirm",
                        headers=auth, json={})
        assert r.status_code == 201, r.get_json()
        result = r.get_json()
        assert result["kind"] == "drug"
        assert set(result["created_ingredients"]) == {
            "ZZ导入成分甲", "ZZ导入成分乙"}
        drug_id = result["target_id"]

        # reload_drugs 生效：归一化立刻认得新药，拼音已生成
        norm = client.post("/api/v1/normalize", json={"drugs": ["ZZ导入药品甲"]})
        matched = norm.get_json()["results"][0]["matched"]
        assert matched and matched["drug_id"] == drug_id

        detail = client.get(f"/api/v1/admin/drugs/{drug_id}", headers=auth)
        d = detail.get_json()
        assert d["dosage_form"] == "胶囊"
        assert d["pinyin"]
        by_name = {i["name_cn"]: i for i in d["ingredients"]}
        assert set(by_name) == {"ZZ导入成分甲", "ZZ导入成分乙"}
        assert by_name["ZZ导入成分甲"]["strength"] == "0.25g"
        assert by_name["ZZ导入成分乙"]["strength"] is None

        _cleanup_drug(seed_db, "ZZ导入药品甲",
                      ["ZZ导入成分甲", "ZZ导入成分乙"])
        assert client.post("/api/v1/admin/reload", headers=auth).status_code == 200

    def test_confirm_drug_duplicate_409(self, client, auth, app, seed_db):
        _cleanup_drug(seed_db, "ZZ导入药品乙",
                      ["ZZ导入成分甲", "ZZ导入成分乙"])
        body = _stage(client, auth, app, [
            _drug_candidate(name="ZZ导入药品乙"),
            _drug_candidate(name="ZZ导入药品乙"),
        ])
        ids = [d["id"] for d in body["drafts"]]
        first = client.post(f"/api/v1/admin/import/drafts/{ids[0]}/confirm",
                            headers=auth, json={})
        assert first.status_code == 201
        second = client.post(f"/api/v1/admin/import/drafts/{ids[1]}/confirm",
                             headers=auth, json={})
        assert second.status_code == 409
        assert "已存在" in second.get_json()["error"]
        _cleanup_drug(seed_db, "ZZ导入药品乙",
                      ["ZZ导入成分甲", "ZZ导入成分乙"])
        client.post("/api/v1/admin/reload", headers=auth)

    def test_drug_draft_put_requires_ingredients(self, client, auth, app,
                                                 seed_db):
        body = _stage(client, auth, app, [_drug_candidate(name="ZZ导入药品丙")])
        did = body["drafts"][0]["id"]
        fields = body["drafts"][0]["fields"]
        fields["ingredients"] = []
        r = client.put(f"/api/v1/admin/import/drafts/{did}", headers=auth,
                       json={"fields": fields})
        assert r.status_code == 400
        assert "ingredients" in r.get_json()["error"]
        client.delete(f"/api/v1/admin/import/drafts/{did}", headers=auth)
        _cleanup_drug(seed_db, "ZZ导入药品丙")

    def test_mixed_kinds_in_one_batch(self, client, auth, app, seed_db):
        """一次解析同时出规则和药品草稿 —— kind 各归其类。"""
        _cleanup_drug(seed_db, "ZZ导入药品丁",
                      ["ZZ导入成分甲", "ZZ导入成分乙"])
        _cleanup(seed_db, ("ZZ导入成分甲", "ZZ导入成分乙"),
                 pair=("ZZ导入成分甲", "ZZ导入成分乙"))
        body = _stage(client, auth, app, [
            _rule_candidate(),
            _drug_candidate(name="ZZ导入药品丁"),
        ])
        kinds = {d["kind"] for d in body["drafts"]}
        assert kinds == {"ddi_rule", "drug"}
        _cleanup(seed_db)   # 清草稿；药品草稿未确认，库里无残留
        _cleanup_drug(seed_db, "ZZ导入药品丁")


class TestStatsImport:
    def test_stats_reports_pending(self, client, auth, app, seed_db):
        body = _stage(client, auth, app, [_rule_candidate(
            a="ZZ导入成分壬", b="ZZ导入成分癸")])
        stats = client.get("/api/v1/admin/stats", headers=auth).get_json()
        assert stats["imports"]["pending"] >= 1
        _cleanup(seed_db)
