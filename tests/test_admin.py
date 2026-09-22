"""管理后台 API 测试。

鉴权靠 ``ddi.api.admin.settings``（模块级引用），Settings 是 frozen dataclass，
所以测试用 ``dataclasses.replace`` 造带 token 的副本再 monkeypatch 模块名。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

# 不写 `from tests.test_api import …`：机器上 /root/zhihu-cli/tests 是带
# __init__.py 的正包，会遮住本目录的命名空间包，pytest 顶层导入才稳。
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

    # 开启管理后台：造一份带 token 的 settings 副本，替换 admin 模块看到的那个。
    # db_path 指向临时库 —— 评估日志不能写进真实 data/build/ddi.db。
    import importlib

    import ddi.api.admin as admin_mod
    import ddi.config as config_mod

    token = "test-token"
    patched = replace(settings, admin_token=token, db_path=seed_db)
    monkeypatch.setattr(admin_mod, "settings", patched)
    monkeypatch.setattr(config_mod, "settings", patched)
    # session 模块在 import 时绑定了 settings（db_path() 用它）。
    # 注意不能写 `import ddi.db.session as m` —— ddi/db/__init__.py 把
    # session 函数绑到了包属性上，会遮住同名子模块，拿到的是函数不是模块。
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
    # 日志会写进 patched settings 指向的 db —— 指到临时库，别碰真实数据
    yield application
    monkeypatch.undo()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def auth(app):
    return {"Authorization": "Bearer " + app.config["TEST_ADMIN_TOKEN"]}


class TestAuth:
    def test_login_ok(self, app, client):
        r = client.post("/api/v1/admin/login",
                        json={"token": app.config["TEST_ADMIN_TOKEN"]})
        assert r.status_code == 200
        assert r.get_json()["ok"] is True

    def test_login_wrong_token(self, client):
        r = client.post("/api/v1/admin/login", json={"token": "nope"})
        assert r.status_code == 401

    def test_protected_without_token(self, client):
        assert client.get("/api/v1/admin/rules").status_code == 401

    def test_protected_wrong_token(self, client):
        r = client.get("/api/v1/admin/rules",
                       headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

    def test_admin_disabled(self, client, monkeypatch):
        import ddi.api.admin as admin_mod
        from ddi.config import settings

        monkeypatch.setattr(admin_mod, "settings", replace(settings, admin_token=""))
        assert client.post("/api/v1/admin/login", json={"token": "x"}).status_code == 403
        assert client.get("/api/v1/admin/rules").status_code == 403

    def test_header_token_works(self, client, app):
        r = client.get("/api/v1/admin/rules",
                       headers={"X-Admin-Token": app.config["TEST_ADMIN_TOKEN"]})
        assert r.status_code == 200


class TestIngredients:
    def test_list_shape(self, client, auth):
        r = client.get("/api/v1/admin/ingredients", headers=auth)
        assert r.status_code == 200
        body = r.get_json()
        assert body["total"] > 0
        assert {"id", "name_cn"} <= set(body["items"][0])

    def test_search(self, client, auth):
        r = client.get("/api/v1/admin/ingredients?q=华法林", headers=auth)
        items = r.get_json()["items"]
        assert items and items[0]["name_cn"] == "华法林"

    @staticmethod
    def _cleanup(seed_db, name):
        from ddi.db.session import session as db_session

        with db_session(seed_db) as c:
            c.execute("DELETE FROM ingredient WHERE name_cn = ?", (name,))

    def test_create_ok(self, client, auth, seed_db):
        name = "ZZ测试成分"
        self._cleanup(seed_db, name)  # session 级共享库，先清残留
        r = client.post("/api/v1/admin/ingredients", headers=auth,
                        json={"name_cn": name, "category": "测试"})
        assert r.status_code == 201
        body = r.get_json()
        assert body["name_cn"] == name

        got = client.get(f"/api/v1/admin/ingredients?q={name}", headers=auth)
        assert got.get_json()["total"] == 1
        self._cleanup(seed_db, name)

    def test_create_duplicate_409(self, client, auth):
        r = client.post("/api/v1/admin/ingredients", headers=auth,
                        json={"name_cn": "华法林"})
        assert r.status_code == 409

    def test_create_requires_name(self, client, auth):
        r = client.post("/api/v1/admin/ingredients", headers=auth, json={})
        assert r.status_code == 400


class TestRules:
    def test_list_shape(self, client, auth):
        r = client.get("/api/v1/admin/rules", headers=auth)
        assert r.status_code == 200
        body = r.get_json()
        assert body["total"] > 0
        assert {"items", "page", "pages", "total"} <= set(body)
        assert "ing_a_name" in body["items"][0]

    def test_filter_severity(self, client, auth):
        r = client.get("/api/v1/admin/rules?severity=caution", headers=auth)
        items = r.get_json()["items"]
        assert items and all(i["severity"] == "caution" for i in items)

    def test_search(self, client, auth):
        r = client.get("/api/v1/admin/rules?q=华法林", headers=auth)
        assert r.status_code == 200
        assert r.get_json()["total"] >= 1

    def test_detail_with_sources(self, client, auth):
        listed = client.get("/api/v1/admin/rules", headers=auth).get_json()
        rid = listed["items"][0]["id"]
        r = client.get(f"/api/v1/admin/rules/{rid}", headers=auth)
        assert r.status_code == 200
        body = r.get_json()
        assert "sources" in body
        assert body["ing_a_name"]

    @staticmethod
    def _make_rule(seed_db) -> int:
        """造一条专用规则。seed_db 是 session 级共享的 ——
        直接改既有规则会污染其它测试（华法林+阿司匹林 那条就被弄坏过）。"""
        from ddi.db.session import session as db_session

        with db_session(seed_db) as c:
            ing = c.execute("SELECT id FROM ingredient ORDER BY id LIMIT 2").fetchall()
            pair = tuple(sorted(i["id"] for i in ing))
            # 同一对成分可能已有规则（UNIQUE），先删再造
            c.execute("DELETE FROM ddi_rule WHERE ing_a_id = ? AND ing_b_id = ?", pair)
            cur = c.execute(
                "INSERT INTO ddi_rule (ing_a_id, ing_b_id, severity, status)"
                " VALUES (?, ?, 'caution', 'draft')",
                pair,
            )
            return int(cur.lastrowid)

    def test_update_then_public_reflects(self, client, auth, seed_db):
        """改完必须热重载：公开详情接口要立刻看到新值。"""
        rid = self._make_rule(seed_db)
        # 造完先重载一次，引擎内存里才有这条
        assert client.post("/api/v1/admin/reload", headers=auth).status_code == 200

        r = client.put(f"/api/v1/admin/rules/{rid}",
                       headers=auth, json={"severity": "monitor", "status": "reviewed"})
        assert r.status_code == 200

        pub = client.get(f"/api/v1/rules/{rid}")
        assert pub.status_code == 200
        assert pub.get_json()["severity"] == "monitor"

        # 收尾：删掉专用规则并重载，别把污染留给后续测试
        client.delete(f"/api/v1/admin/rules/{rid}", headers=auth)

    def test_update_bad_severity(self, client, auth, seed_db):
        rid = self._make_rule(seed_db)
        r = client.put(f"/api/v1/admin/rules/{rid}",
                       headers=auth, json={"severity": "nuke"})
        assert r.status_code == 400
        client.delete(f"/api/v1/admin/rules/{rid}", headers=auth)

    def test_update_immutable_pair_rejected_silently(self, client, auth, seed_db):
        """成分对字段不在白名单 —— 传了也不改，只更新合法字段。"""
        rid = self._make_rule(seed_db)
        client.post("/api/v1/admin/reload", headers=auth)
        before = client.get(f"/api/v1/admin/rules/{rid}", headers=auth).get_json()
        r = client.put(f"/api/v1/admin/rules/{rid}",
                       headers=auth, json={"ing_a_id": 999, "status": "draft"})
        assert r.status_code == 200
        assert "ing_a_id" not in r.get_json()["updated"]
        after = client.get(f"/api/v1/admin/rules/{rid}", headers=auth).get_json()
        assert after["ing_a_id"] == before["ing_a_id"]
        client.delete(f"/api/v1/admin/rules/{rid}", headers=auth)

    def test_delete_then_public_404(self, client, auth, seed_db):
        """删除后公开接口 404 —— 证明 reload 生效。"""
        import sqlite3

        # 借一条规则删（测试库是 session 级共享的，删了会影响其它测试 ——
        # 所以先造一条专用的再删）
        from ddi.db.session import session as db_session

        with db_session(seed_db) as c:
            ing = c.execute("SELECT id FROM ingredient ORDER BY id LIMIT 2").fetchall()
            pair = tuple(sorted(i["id"] for i in ing))
            cur = c.execute(
                "INSERT INTO ddi_rule (ing_a_id, ing_b_id, severity, status)"
                " VALUES (?, ?, 'monitor', 'draft')",
                pair,
            )
            rid = cur.lastrowid

        assert client.get(f"/api/v1/admin/rules/{rid}",
                          headers=auth).status_code == 200
        assert client.delete(f"/api/v1/admin/rules/{rid}",
                             headers=auth).status_code == 200
        assert client.get(f"/api/v1/rules/{rid}").status_code == 404

    # ── 创建 ─────────────────────────────────────────
    @staticmethod
    def _fresh_pair(client, auth, seed_db, suffix):
        """造两个专用成分返回 (id_a, id_b)，并保证这对成分上没有既有规则。"""
        ids = []
        for n in (f"ZZ成{suffix}一", f"ZZ成{suffix}二"):
            r = client.post("/api/v1/admin/ingredients", headers=auth,
                            json={"name_cn": n})
            if r.status_code == 201:
                ids.append(r.get_json()["id"])
            else:  # 残留：查出来复用
                got = client.get(
                    f"/api/v1/admin/ingredients?q={n}", headers=auth
                ).get_json()["items"]
                ids.append(got[0]["id"])
        return tuple(ids)

    @staticmethod
    def _cleanup_pair(client, auth, seed_db, ids, names):
        from ddi.db.session import session as db_session

        with db_session(seed_db) as c:
            for a, b in ((ids[0], ids[1]), (ids[1], ids[0])):
                c.execute("DELETE FROM ddi_rule WHERE ing_a_id = ? AND ing_b_id = ?",
                          (a, b))
            for n in names:
                c.execute("DELETE FROM ingredient WHERE name_cn = ?", (n,))
        client.post("/api/v1/admin/reload", headers=auth)

    def test_create_then_public_reflects(self, client, auth, seed_db):
        names = ("ZZ成十一", "ZZ成十二")
        ids = self._fresh_pair(client, auth, seed_db, "甲")
        r = client.post("/api/v1/admin/rules", headers=auth, json={
            "ing_a_id": ids[0], "ing_b_id": ids[1],
            "severity": "caution", "mechanism": "测试机制",
        })
        assert r.status_code == 201
        rid = r.get_json()["id"]

        # 创建接口内部已 reload —— 公开详情应立刻可见
        pub = client.get(f"/api/v1/rules/{rid}")
        assert pub.status_code == 200
        assert pub.get_json()["severity"] == "caution"

        self._cleanup_pair(client, auth, seed_db, ids, names)

    def test_create_normalizes_pair_order(self, client, auth, seed_db):
        """传反了也应落成 ing_a_id < ing_b_id（schema CHECK 依赖这个约定）。"""
        names = ("ZZ成乙一", "ZZ成乙二")
        ids = self._fresh_pair(client, auth, seed_db, "乙")
        r = client.post("/api/v1/admin/rules", headers=auth, json={
            "ing_a_id": ids[1], "ing_b_id": ids[0],  # 故意反着传
            "severity": "monitor",
        })
        assert r.status_code == 201
        rid = r.get_json()["id"]
        detail = client.get(f"/api/v1/admin/rules/{rid}", headers=auth).get_json()
        assert detail["ing_a_id"] < detail["ing_b_id"]

        self._cleanup_pair(client, auth, seed_db, ids, names)

    def test_create_duplicate_pair_409(self, client, auth, seed_db):
        names = ("ZZ成丙一", "ZZ成丙二")
        ids = self._fresh_pair(client, auth, seed_db, "丙")
        payload = {"ing_a_id": ids[0], "ing_b_id": ids[1], "severity": "monitor"}
        assert client.post("/api/v1/admin/rules", headers=auth,
                           json=payload).status_code == 201
        # 反序再建也应命中 UNIQUE
        dup = client.post("/api/v1/admin/rules", headers=auth, json={
            "ing_a_id": ids[1], "ing_b_id": ids[0], "severity": "caution",
        })
        assert dup.status_code == 409
        self._cleanup_pair(client, auth, seed_db, ids, names)

    def test_create_same_ingredient_400(self, client, auth):
        r = client.post("/api/v1/admin/rules", headers=auth, json={
            "ing_a_id": 1, "ing_b_id": 1, "severity": "monitor",
        })
        assert r.status_code == 400

    def test_create_bad_severity_400(self, client, auth):
        r = client.post("/api/v1/admin/rules", headers=auth, json={
            "ing_a_id": 1, "ing_b_id": 2, "severity": "nuke",
        })
        assert r.status_code == 400

    def test_create_unknown_ingredient_404(self, client, auth):
        r = client.post("/api/v1/admin/rules", headers=auth, json={
            "ing_a_id": 999999, "ing_b_id": 1, "severity": "monitor",
        })
        assert r.status_code == 404


class TestDrugs:
    def test_list(self, client, auth):
        r = client.get("/api/v1/admin/drugs", headers=auth)
        assert r.status_code == 200
        body = r.get_json()
        assert body["total"] > 0
        assert "ingredients" in body["items"][0]

    def test_search(self, client, auth):
        r = client.get("/api/v1/admin/drugs?q=华法林", headers=auth)
        assert r.get_json()["total"] >= 1

    def test_detail(self, client, auth):
        listed = client.get("/api/v1/admin/drugs", headers=auth).get_json()
        did = listed["items"][0]["id"]
        r = client.get(f"/api/v1/admin/drugs/{did}", headers=auth)
        assert r.status_code == 200
        assert r.get_json()["ingredients"]

    def test_not_found(self, client, auth):
        assert client.get("/api/v1/admin/drugs/999999", headers=auth).status_code == 404

    # ── 创建 ─────────────────────────────────────────
    @staticmethod
    def _cleanup(seed_db, drug_name, ing_names=()):
        from ddi.db.session import session as db_session

        with db_session(seed_db) as c:
            c.execute("DELETE FROM drug WHERE name_cn = ?", (drug_name,))
            for n in ing_names:
                c.execute("DELETE FROM ingredient WHERE name_cn = ?", (n,))

    def test_create_with_existing_ingredient(self, client, auth, seed_db):
        name = "ZZ测试药品甲"
        self._cleanup(seed_db, name)
        r = client.post("/api/v1/admin/drugs", headers=auth, json={
            "name_cn": name, "dosage_form": "片剂",
            "ingredients": [{"id": 1, "strength": "5mg"}],  # 华法林
        })
        assert r.status_code == 201
        did = r.get_json()["id"]

        detail = client.get(f"/api/v1/admin/drugs/{did}", headers=auth).get_json()
        assert detail["name_cn"] == name
        assert detail["ingredients"][0]["name_cn"] == "华法林"
        assert detail["pinyin"]  # 拼音已生成

        # reload_drugs 生效：公开 normalize 能立刻认出新药
        norm = client.post("/api/v1/normalize", json={"drugs": [name]})
        results = norm.get_json()["results"]
        assert results[0]["matched"] and results[0]["matched"]["drug_id"] == did

        # 同名同剂型重复 → 409
        dup = client.post("/api/v1/admin/drugs", headers=auth, json={
            "name_cn": name, "dosage_form": "片剂",
            "ingredients": [{"id": 1}],
        })
        assert dup.status_code == 409

        self._cleanup(seed_db, name)
        assert client.post("/api/v1/admin/reload", headers=auth).status_code == 200

    def test_create_auto_creates_ingredient(self, client, auth, seed_db):
        drug, ing = "ZZ测试药品乙", "ZZ自建成分"
        self._cleanup(seed_db, drug, [ing])
        r = client.post("/api/v1/admin/drugs", headers=auth, json={
            "name_cn": drug,
            "ingredients": [{"name": ing, "strength": "10mg"}],
        })
        assert r.status_code == 201
        did = r.get_json()["id"]

        detail = client.get(f"/api/v1/admin/drugs/{did}", headers=auth).get_json()
        assert detail["ingredients"] and detail["ingredients"][0]["name_cn"] == ing

        self._cleanup(seed_db, drug, [ing])
        assert client.post("/api/v1/admin/reload", headers=auth).status_code == 200

    def test_create_requires_ingredients(self, client, auth):
        r = client.post("/api/v1/admin/drugs", headers=auth,
                        json={"name_cn": "ZZ空成分药"})
        assert r.status_code == 400

    def test_create_requires_name(self, client, auth):
        r = client.post("/api/v1/admin/drugs", headers=auth,
                        json={"ingredients": [{"id": 1}]})
        assert r.status_code == 400


class TestLogs:
    def test_assess_writes_log(self, client, auth):
        r = client.post("/api/v1/assess", json={
            "drugs": ["华法林钠片", "阿司匹林肠溶片"],
            "profile": {"age": 70, "allergies": ["青霉素"]},
        })
        assert r.status_code == 200

        logs = client.get("/api/v1/admin/logs", headers=auth).get_json()
        assert logs["total"] >= 1
        latest = logs["items"][0]
        assert latest["overall_risk"]
        # drugs_raw 是白名单内的自由输入，允许存；allergies 绝不能出现
        import json

        profile = latest.get("profile_summary") or {}
        if isinstance(profile, str):
            profile = json.loads(profile)
        assert "allergies" not in profile

    def test_log_detail(self, client, auth):
        logs = client.get("/api/v1/admin/logs", headers=auth).get_json()
        if not logs["items"]:
            pytest.skip("尚无日志")
        lid = logs["items"][0]["id"]
        assert client.get(f"/api/v1/admin/logs/{lid}", headers=auth).status_code == 200

    def test_pagination(self, client, auth):
        body = client.get("/api/v1/admin/logs?page=1&page_size=5", headers=auth).get_json()
        assert body["page"] == 1
        assert body["page_size"] == 5
        assert len(body["items"]) <= 5


class TestConfig:
    def test_get_never_leaks_secrets(self, client, auth, monkeypatch, app):
        import ddi.api.admin as admin_mod
        from ddi.config import settings

        # admin_token 必须保持测试令牌不变（否则 auth 头失效）；
        # 真正要断言的是：即使 token/api_key 都在 settings 里，响应也不回显它们。
        monkeypatch.setattr(admin_mod, "settings",
                            replace(settings,
                                    llm_api_key="sk-SECRET-VALUE",
                                    admin_token=app.config["TEST_ADMIN_TOKEN"],
                                    config_source="config.yaml"))
        r = client.get("/api/v1/admin/config", headers=auth)
        assert r.status_code == 200
        text = r.get_data(as_text=True)
        assert "sk-SECRET-VALUE" not in text
        body = r.get_json()
        assert body["llm"]["api_key_set"] is True
        assert body["admin"]["token_set"] is True
        # 确认被替换后的 settings 确实带着密钥 —— 否则断言是空的
        assert "sk-SECRET-VALUE" in repr(
            replace(settings, llm_api_key="sk-SECRET-VALUE"))

    def test_put_unknown_provider_400(self, client, auth, tmp_path, monkeypatch):
        import ddi.api.admin as admin_mod
        from ddi.config import CONFIG_FILE

        monkeypatch.setattr(admin_mod, "CONFIG_FILE", tmp_path / "cfg.yaml")
        r = client.put("/api/v1/admin/config", headers=auth,
                       json={"llm": {"provider": "deepsek"}})
        assert r.status_code == 400
        assert "deepseek" in r.get_json()["error"]

    def test_put_writes_and_hot_swaps(self, client, auth, tmp_path, monkeypatch, app):
        import ddi.api.admin as admin_mod
        from ddi.config import CONFIG_FILE

        monkeypatch.setattr(admin_mod, "CONFIG_FILE", tmp_path / "cfg.yaml")
        r = client.put("/api/v1/admin/config", headers=auth, json={
            "llm": {"provider": "deepseek", "max_tokens": 1024},
        })
        assert r.status_code == 200
        body = r.get_json()
        assert body["llm_hot_swapped"] is True
        assert body["restart_required"] == []

        svc = app.config["DDI_SERVICE"]
        assert svc.llm.model == "deepseek-v4-pro"  # 热替换生效

        import yaml

        written = yaml.safe_load((tmp_path / "cfg.yaml").read_text(encoding="utf-8"))
        assert written["llm"]["provider"] == "deepseek"
        assert written["llm"]["max_tokens"] == 1024

    def test_put_port_lists_restart(self, client, auth, tmp_path, monkeypatch):
        import ddi.api.admin as admin_mod

        monkeypatch.setattr(admin_mod, "CONFIG_FILE", tmp_path / "cfg.yaml")
        r = client.put("/api/v1/admin/config", headers=auth,
                       json={"api": {"port": 5001}})
        assert r.status_code == 200
        assert "api.port" in r.get_json()["restart_required"]

    def test_put_rejects_unknown_key(self, client, auth):
        r = client.put("/api/v1/admin/config", headers=auth,
                       json={"llm": {"evil_key": 1}})
        assert r.status_code == 400


class TestReload:
    def test_reload(self, client, auth):
        r = client.post("/api/v1/admin/reload", headers=auth)
        assert r.status_code == 200
        body = r.get_json()
        assert body["rules"] > 0
        assert body["drugs_indexed"] > 0
