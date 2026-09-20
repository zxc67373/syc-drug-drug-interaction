"""Flask API。

前后端分离：前端在 ``web/``，通过 HTTP 调这里的 ``/api/v1/*``。
**密钥只留在服务端** —— 前端永远不接触大模型 API。

前端可以两种方式部署：

1. Nginx 直接托管 ``web/``，``/api`` 反代到本服务（生产推荐）
2. 本服务顺带托管 ``web/``（本机开发 / 单进程快速验证）

两种都支持，靠 ``web/`` 目录是否存在自动切换。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

from ddi.config import settings
from ddi.service import DDIService

log = logging.getLogger(__name__)

MAX_DRUGS = 20            # 20 种药 = 190 对，够用且能防滥用
ALLOWED_PROFILE_KEYS = {"age", "sex", "hepatic", "renal", "pregnancy", "allergies"}


def _bad(msg: str, code: int = 400):
    return jsonify({"error": msg}), code


def _validate_assess_payload(data: Any) -> tuple[list[str] | None, dict | None, str | None]:
    if not isinstance(data, dict):
        return None, None, "请求体必须是 JSON 对象"

    drugs = data.get("drugs")
    if not isinstance(drugs, list) or not drugs:
        return None, None, "drugs 必须是非空数组"
    drugs = [str(d).strip() for d in drugs if str(d).strip()]
    if not drugs:
        return None, None, "drugs 中没有有效药名"
    if len(drugs) > MAX_DRUGS:
        return None, None, f"一次最多评估 {MAX_DRUGS} 种药品"

    raw_profile = data.get("profile") or {}
    if not isinstance(raw_profile, dict):
        return None, None, "profile 必须是 JSON 对象"

    profile: dict[str, Any] = {
        k: v for k, v in raw_profile.items() if k in ALLOWED_PROFILE_KEYS
    }
    if "age" in profile:
        try:
            age = int(profile["age"])
        except (TypeError, ValueError):
            return None, None, "profile.age 必须是整数"
        if not 0 < age < 130:
            return None, None, "profile.age 超出合理范围"
        profile["age"] = age
    if "allergies" in profile and not isinstance(profile["allergies"], list):
        return None, None, "profile.allergies 必须是数组"

    return drugs, profile, None


def create_app(service: DDIService | None = None) -> Flask:
    web_dir: Path = settings.web_dir
    app = Flask(__name__)
    # 同域部署时用不上 CORS；分域部署时来源由 config.yaml 的 api.cors_origins 指定。
    # 生产环境务必写死来源，不要留 "*"。
    CORS(app, resources={r"/api/*": {"origins": list(settings.api_cors_origins)}})
    svc = service or DDIService()
    app.config["DDI_SERVICE"] = svc
    logging.basicConfig(
        level=logging.DEBUG if settings.api_debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # 预载索引与嵌入模型。嵌入模型冷启动约 40 秒 —— 必须在这里付掉，
    # 否则第一个发请求的用户会以为服务挂了。
    #
    # 只在自建 service 时做：调用方注入了自己的 service（测试就是这么干的），
    # 说明它自己管生命周期，不该被这里拖去加载几百 MB 的模型。
    if service is None:
        if settings.embed_backend in ("hash", ""):
            log.warning(
                "嵌入后端是 hash —— 它只做字面重合匹配，**不是语义检索**。"
                "生产环境请在 config.yaml 里改成 m3e。"
            )
        try:
            log.info("预载检索（嵌入后端 %s）…", settings.embed_backend)
            took = svc.warmup()
            if took:
                log.info("检索就绪，耗时 %.1fs", took)
            else:
                log.warning("检索索引不可用，评估将不含 RAG 补充片段")
        except Exception:  # noqa: BLE001
            log.exception("预载失败，服务继续启动但检索不可用")

    @app.get("/api/v1/health")
    def health():
        return jsonify(
            {
                "status": "ok",
                "rules": svc.engine.rule_count,
                "drugs_indexed": svc.normalizer.size,
                "llm_configured": svc.llm.configured,
                "llm_provider": svc.llm.provider_label if svc.llm.configured else None,
                "llm_model": svc.llm.model if svc.llm.configured else None,
                "llm_base_url": svc.llm.base_url if svc.llm.configured else None,
                "rag_ready": svc.retriever is not None,
                # 暴露嵌入后端：hash 是字面匹配不是语义检索，
                # 不写出来就没法一眼看出线上跑的是不是假语义
                "embed_backend": settings.embed_backend,
                "embed_semantic": settings.embed_backend not in ("hash", ""),
            }
        )

    @app.post("/api/v1/assess")
    def assess():
        body = request.get_json(silent=True)
        drugs, profile, err = _validate_assess_payload(body)
        if err:
            return _bad(err)

        report = svc.assess(
            drugs=drugs,               # type: ignore[arg-type]
            profile=profile,           # type: ignore[arg-type]
            use_llm=bool(body.get("use_llm", True)),      # type: ignore[union-attr]
            use_rag=bool(body.get("use_rag", True)),      # type: ignore[union-attr]
        )
        return jsonify(report.to_dict())

    @app.post("/api/v1/normalize")
    def normalize():
        data = request.get_json(silent=True) or {}
        drugs = data.get("drugs")
        if not isinstance(drugs, list) or not drugs:
            return _bad("drugs 必须是非空数组")
        return jsonify({"results": svc.normalize_only([str(d) for d in drugs])})

    @app.get("/api/v1/rules/<int:rule_id>")
    def rule_detail(rule_id: int):
        rule = svc.engine.get_rule(rule_id)
        if not rule:
            return _bad("规则不存在", 404)
        return jsonify(rule)

    @app.errorhandler(500)
    def server_error(e):  # pragma: no cover
        log.exception("未捕获的服务端错误")
        return _bad("服务端内部错误", 500)

    # ── 顺带托管前端（可选）──────────────────────────────
    # web/ 存在时挂上，不存在就只提供 API。生产环境更推荐让 Nginx
    # 直接托管静态文件 —— 那比让 Flask 发静态资源快得多。
    if web_dir.is_dir():
        log.info("检测到前端目录，顺带托管：%s", web_dir)

        @app.get("/")
        def index():
            return send_from_directory(web_dir, "index.html")

        @app.get("/<path:filename>")
        def static_files(filename: str):
            # 不吞掉 /api/* —— 未匹配的 API 路径应当返回 404 而不是 index.html
            if filename.startswith("api/"):
                return _bad("接口不存在", 404)
            return send_from_directory(web_dir, filename)
    else:
        log.info("未找到前端目录 %s，仅提供 API", web_dir)

    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=settings.api_port, debug=settings.api_debug)
