"""管理后台 API（Blueprint）。

路由前缀 ``/api/v1/admin``。鉴权用单一共享令牌（``config.yaml`` 的
``admin.token``）：

- token 为空 = **整个管理接口禁用**（fail closed，默认状态）
- 鉴权头 ``Authorization: Bearer <token>``（也接受 ``X-Admin-Token``）
- 比较用 ``hmac.compare_digest`` 防时序侧信道
- 不用 cookie / session —— 无 CSRF 面

**红线**：响应里永不出现 ``llm.api_key`` 的值、``admin.token`` 的值。
配置读接口只返回 ``api_key_set`` / ``token_set`` 这类布尔标记。
"""

from __future__ import annotations

import hmac
import json
import logging
from functools import wraps
from typing import Any

import yaml
from flask import Blueprint, jsonify, request

from ddi.config import CONFIG_FILE, settings
from ddi.db.session import session
from ddi.generate.llm_client import LLMClient
from ddi.providers import PROVIDERS, UnknownProvider, resolve as resolve_provider

log = logging.getLogger(__name__)

admin_bp = Blueprint("admin", __name__, url_prefix="/api/v1/admin")

SEVERITIES = ("contraindicated", "caution", "monitor")
STATUSES = ("draft", "reviewed", "published")
EVIDENCE_LEVELS = ("A", "B", "C", "D")
TEXT_MAX = 2000  # 文本字段长度上限，防离谱数据

# 规则可编辑字段白名单。id / ing_a_id / ing_b_id 永不可改 ——
# 成分对有 UNIQUE 约束和 CHECK(ing_a_id < ing_b_id)，改它风险太大。
_RULE_EDITABLE = (
    "severity", "mechanism", "consequence", "suggestion",
    "evidence_level", "status", "reviewed_by",
)

# 需要重启才生效的配置键（settings 是 frozen 单例，只有 svc.llm 能热替换）
_RESTART_KEYS = (
    "api.port", "api.debug", "api.cors_origins",
    "embed.backend", "embed.model_path", "database.path",
    "admin.token",
)


def _bad(msg: str, code: int = 400):
    return jsonify({"error": msg}), code


def require_admin(fn):
    """管理接口鉴权装饰器。token 未配置时整体 403（fail closed）。"""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not settings.admin_enabled:
            return _bad("管理后台未启用（config.yaml 的 admin.token 为空）", 403)
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:].strip()
        else:
            token = request.headers.get("X-Admin-Token", "")
        if not token or not hmac.compare_digest(token, settings.admin_token):
            return _bad("未授权", 401)
        return fn(*args, **kwargs)

    return wrapper


def _svc():
    from flask import current_app

    return current_app.config["DDI_SERVICE"]


# ── 登录 ────────────────────────────────────────────────
@admin_bp.post("/login")
def login():
    """校验令牌。客户端之后把令牌放进 sessionStorage 并随每次请求带上。"""
    if not settings.admin_enabled:
        return _bad("管理后台未启用（config.yaml 的 admin.token 为空）", 403)
    body = request.get_json(silent=True) or {}
    token = str(body.get("token") or "").strip()
    if not token or not hmac.compare_digest(token, settings.admin_token):
        return _bad("令牌错误", 401)
    return jsonify({"ok": True})


# ── 规则库 ──────────────────────────────────────────────
_RULES_SELECT = """
SELECT r.id, r.ing_a_id, r.ing_b_id, r.severity, r.mechanism, r.consequence,
       r.suggestion, r.evidence_level, r.status, r.reviewed_by,
       ia.name_cn AS ing_a_name, ib.name_cn AS ing_b_name,
       (SELECT COUNT(*) FROM rule_source rs WHERE rs.rule_id = r.id) AS source_count
FROM ddi_rule r
JOIN ingredient ia ON ia.id = r.ing_a_id
JOIN ingredient ib ON ib.id = r.ing_b_id
"""


@admin_bp.get("/rules")
@require_admin
def rules_list():
    q = (request.args.get("q") or "").strip()
    severity = (request.args.get("severity") or "").strip()
    status = (request.args.get("status") or "").strip()
    page = max(1, int(request.args.get("page", 1)))
    page_size = max(1, min(100, int(request.args.get("page_size", 20))))

    where = []
    params: list[Any] = []
    if q:
        where.append(
            "(ia.name_cn LIKE ? OR ib.name_cn LIKE ?"
            " OR r.mechanism LIKE ? OR r.consequence LIKE ?)"
        )
        like = f"%{q}%"
        params.extend([like, like, like, like])
    if severity:
        where.append("r.severity = ?")
        params.append(severity)
    if status:
        where.append("r.status = ?")
        params.append(status)

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    with session() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM ddi_rule r"
            f" JOIN ingredient ia ON ia.id = r.ing_a_id"
            f" JOIN ingredient ib ON ib.id = r.ing_b_id{where_sql}",
            params,
        ).fetchone()["n"]
        rows = conn.execute(
            _RULES_SELECT + where_sql + " ORDER BY r.id LIMIT ? OFFSET ?",
            params + [page_size, (page - 1) * page_size],
        ).fetchall()

    import math

    pages = max(1, math.ceil(total / page_size)) if total else 1
    return jsonify({
        "items": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    })


@admin_bp.get("/rules/<int:rule_id>")
@require_admin
def rule_detail(rule_id: int):
    with session() as conn:
        row = conn.execute(
            _RULES_SELECT + " WHERE r.id = ?", (rule_id,)
        ).fetchone()
        if not row:
            return _bad("规则不存在", 404)
        sources = conn.execute(
            "SELECT s.id, s.title, s.source_type, s.publisher, s.year, s.url, rs.excerpt"
            " FROM rule_source rs JOIN source s ON s.id = rs.source_id"
            " WHERE rs.rule_id = ? ORDER BY s.id",
            (rule_id,),
        ).fetchall()
    out = dict(row)
    out["sources"] = [dict(s) for s in sources]
    return jsonify(out)


def _validate_rule_body(body: dict) -> tuple[dict, str | None]:
    """校验并归一化规则编辑请求。返回 (字段值, 错误)。"""
    if not isinstance(body, dict):
        return {}, "请求体必须是 JSON 对象"
    updates: dict[str, Any] = {}
    for key in _RULE_EDITABLE:
        if key not in body:
            continue
        val = body[key]
        if key == "severity":
            if val not in SEVERITIES:
                return {}, f"severity 必须是 {'/'.join(SEVERITIES)} 之一"
            updates[key] = val
        elif key == "status":
            if val not in STATUSES:
                return {}, f"status 必须是 {'/'.join(STATUSES)} 之一"
            updates[key] = val
        elif key == "evidence_level":
            if val in (None, ""):
                updates[key] = None
            elif val in EVIDENCE_LEVELS:
                updates[key] = val
            else:
                return {}, f"evidence_level 必须是 {'/'.join(EVIDENCE_LEVELS)} 或空"
        elif key == "reviewed_by":
            updates[key] = None if val in (None, "") else str(val).strip()[:100]
        else:  # 文本字段
            if val is None:
                updates[key] = None
            else:
                s = str(val).strip()
                if len(s) > TEXT_MAX:
                    return {}, f"{key} 超长（最多 {TEXT_MAX} 字）"
                updates[key] = s or None
    if not updates:
        return {}, "没有可更新的字段"
    return updates, None


@admin_bp.put("/rules/<int:rule_id>")
@require_admin
def rule_update(rule_id: int):
    updates, err = _validate_rule_body(request.get_json(silent=True) or {})
    if err:
        return _bad(err)
    sets = ", ".join(f"{k} = ?" for k in updates)
    with session() as conn:
        exists = conn.execute(
            "SELECT 1 FROM ddi_rule WHERE id = ?", (rule_id,)
        ).fetchone()
        if not exists:
            return _bad("规则不存在", 404)
        try:
            conn.execute(
                f"UPDATE ddi_rule SET {sets} WHERE id = ?",
                list(updates.values()) + [rule_id],
            )
        except Exception as e:  # noqa: BLE001 — SQLite CHECK 约束失败
            return _bad(f"更新失败：{e}")
    # 引擎读的是内存副本，写完必须重载
    _svc().reload_rules()
    return jsonify({"ok": True, "updated": sorted(updates)})


@admin_bp.delete("/rules/<int:rule_id>")
@require_admin
def rule_delete(rule_id: int):
    with session() as conn:
        exists = conn.execute(
            "SELECT 1 FROM ddi_rule WHERE id = ?", (rule_id,)
        ).fetchone()
        if not exists:
            return _bad("规则不存在", 404)
        conn.execute("DELETE FROM ddi_rule WHERE id = ?", (rule_id,))
    _svc().reload_rules()
    return jsonify({"ok": True})


# ── 药品数据 ────────────────────────────────────────────
@admin_bp.get("/drugs")
@require_admin
def drugs_list():
    q = (request.args.get("q") or "").strip()
    page = max(1, int(request.args.get("page", 1)))
    page_size = max(1, min(100, int(request.args.get("page_size", 20))))

    where, params = "", []
    if q:
        where = " WHERE d.name_cn LIKE ? OR d.pinyin LIKE ? OR d.aliases LIKE ?"
        like = f"%{q}%"
        params = [like, like, like]

    import math

    with session() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM drug d{where}", params
        ).fetchone()["n"]
        rows = conn.execute(
            "SELECT d.id, d.name_cn, d.dosage_form, d.pinyin, d.is_otc, d.is_tcm,"
            " d.trade_names, d.aliases,"
            " (SELECT GROUP_CONCAT(i.name_cn, '、') FROM drug_ingredient di"
            "  JOIN ingredient i ON i.id = di.ingredient_id"
            "  WHERE di.drug_id = d.id) AS ingredients"
            f" FROM drug d{where} ORDER BY d.id LIMIT ? OFFSET ?",
            params + [page_size, (page - 1) * page_size],
        ).fetchall()

    pages = max(1, math.ceil(total / page_size)) if total else 1
    items = []
    for r in rows:
        item = dict(r)
        for key in ("trade_names", "aliases"):
            raw = item.get(key)
            if isinstance(raw, str) and raw:
                try:
                    item[key] = json.loads(raw)
                except (ValueError, TypeError):
                    item[key] = []
        items.append(item)
    return jsonify({
        "items": items, "total": total, "page": page,
        "page_size": page_size, "pages": pages,
    })


@admin_bp.get("/drugs/<int:drug_id>")
@require_admin
def drug_detail(drug_id: int):
    with session() as conn:
        row = conn.execute("SELECT * FROM drug WHERE id = ?", (drug_id,)).fetchone()
        if not row:
            return _bad("药品不存在", 404)
        ings = conn.execute(
            "SELECT i.id, i.name_cn, i.name_en, i.category, di.strength"
            " FROM drug_ingredient di JOIN ingredient i ON i.id = di.ingredient_id"
            " WHERE di.drug_id = ?",
            (drug_id,),
        ).fetchall()
    out = dict(row)
    for key in ("trade_names", "aliases"):
        raw = out.get(key)
        if isinstance(raw, str) and raw:
            try:
                out[key] = json.loads(raw)
            except (ValueError, TypeError):
                out[key] = []
    out["ingredients"] = [dict(i) for i in ings]
    return jsonify(out)


# ── 评估记录 ────────────────────────────────────────────
@admin_bp.get("/logs")
@require_admin
def logs_list():
    from ddi.db.audit import list_logs

    return jsonify(list_logs(
        page=int(request.args.get("page", 1)),
        page_size=int(request.args.get("page_size", 20)),
    ))


@admin_bp.get("/logs/<int:log_id>")
@require_admin
def log_detail(log_id: int):
    from ddi.db.audit import get_log

    row = get_log(log_id)
    if not row:
        return _bad("记录不存在", 404)
    return jsonify(row)


# ── 配置 ────────────────────────────────────────────────
@admin_bp.get("/config")
@require_admin
def config_get():
    """返回**脱敏后**的配置。api_key / admin.token 永不出现，只有布尔标记。"""
    import os

    from ddi.config import _ENV_SPEC, _env  # noqa: PLC2701 — 同包内部使用

    env_overrides = [
        canonical
        for (_, _), canonical, legacy in _ENV_SPEC
        if _env(canonical, legacy) is not None
    ]
    return jsonify({
        "llm": {
            "provider": settings.llm_provider,
            "base_url": settings.llm_base_url,
            "model": settings.llm_model,
            "max_tokens": settings.llm_max_tokens,
            "timeout": settings.llm_timeout,
            "thinking": settings.llm_thinking,
            "temperature": settings.llm_temperature,
            "api_key_set": bool(settings.llm_api_key),
        },
        "embed": {
            "backend": settings.embed_backend,
            "model_path": str(settings.embed_model_path),
        },
        "database": {"path": str(settings.db_path)},
        "api": {
            "port": settings.api_port,
            "debug": settings.api_debug,
            "cors_origins": list(settings.api_cors_origins),
        },
        "admin": {"token_set": bool(settings.admin_token)},
        "providers": sorted(PROVIDERS),
        "config_source": settings.config_source,
        "env_overrides": env_overrides,
        "restart_required_keys": list(_RESTART_KEYS),
    })


def _read_config_yaml() -> dict:
    """读 config.yaml 原文（不合并默认值），供深合并后写回。"""
    if CONFIG_FILE.exists():
        raw = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
        if isinstance(raw, dict):
            return raw
    return {}


def _write_config_yaml(cfg: dict) -> None:
    CONFIG_FILE.write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


# PUT 时允许写入的配置键（嵌套 allowlist）。api_key 是写专用：读接口永不返回它。
_CONFIG_PUT_SCHEMA = {
    "llm": {
        "provider": str,
        "api_key": str,       # "" = 不变；"__CLEAR__" = 清除；其他 = 设置
        "base_url": str,
        "model": str,
        "max_tokens": int,
        "timeout": int,
        "thinking": bool,
        "temperature": (int, float, type(None)),
    },
    "api": {
        "port": int,
        "debug": bool,
        "cors_origins": list,
    },
}


@admin_bp.put("/config")
@require_admin
def config_put():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _bad("请求体必须是 JSON 对象")

    # 校验：嵌套白名单 + 类型
    updates: dict[str, dict] = {}
    for section, spec in _CONFIG_PUT_SCHEMA.items():
        raw_sec = body.get(section)
        if raw_sec is None:
            continue
        if not isinstance(raw_sec, dict):
            return _bad(f"{section} 必须是对象")
        for key, val in raw_sec.items():
            if key not in spec:
                return _bad(f"不允许修改 {section}.{key}")
            expected = spec[key]
            if expected is list:
                if not isinstance(val, list):
                    return _bad(f"{section}.{key} 必须是数组")
            elif not isinstance(val, expected):
                return _bad(f"{section}.{key} 类型不正确")
            updates.setdefault(section, {})[key] = val

    if not updates:
        return _bad("没有可更新的字段")

    # provider 拼写校验必须在写盘之前 —— resolve 的 UnknownProvider 是刻意的失败模式
    new_provider = updates.get("llm", {}).get("provider", settings.llm_provider)
    new_base = updates.get("llm", {}).get("base_url", "")
    new_model = updates.get("llm", {}).get("model", "")
    try:
        base_url, model = resolve_provider(new_provider, new_base, new_model)
    except UnknownProvider as e:
        return _bad(str(e))

    # 深合并进 config.yaml
    cfg = _read_config_yaml()

    def _merge(dst: dict, src: dict) -> None:
        for k, v in src.items():
            if isinstance(v, dict) and isinstance(dst.get(k), dict):
                _merge(dst[k], v)
            else:
                dst[k] = v

    llm_updates = dict(updates.get("llm", {}))
    # api_key 语义：缺省/"" = 保留现值（不写盘）；"__CLEAR__" = 清除；其他 = 设置
    api_key = llm_updates.pop("api_key", None)
    if api_key == "__CLEAR__":
        cfg.setdefault("llm", {})["api_key"] = ""
    elif api_key:  # 非空字符串 → 设置
        cfg.setdefault("llm", {})["api_key"] = api_key
    # 其余字段写入归一化后的 provider 解析结果
    llm_updates["provider"] = new_provider
    if "base_url" in updates.get("llm", {}) or "model" in updates.get("llm", {}):
        llm_updates["base_url"] = base_url
        llm_updates["model"] = model

    _merge(cfg, {**{k: v for k, v in updates.items() if k != "llm"}, "llm": llm_updates})
    try:
        _write_config_yaml(cfg)
    except OSError as e:
        return _bad(f"写入 config.yaml 失败：{e}")

    # 热替换只有 svc.llm 一项能立即生效
    effective_key = None
    if api_key == "__CLEAR__":
        effective_key = ""
    elif api_key:
        effective_key = api_key
    svc = _svc()
    svc.llm = LLMClient(
        api_key=effective_key,  # None → 回退 settings（即文件里刚写入的值）
        base_url=base_url,
        model=model,
        max_tokens=updates.get("llm", {}).get("max_tokens"),
        timeout=updates.get("llm", {}).get("timeout"),
        thinking=updates.get("llm", {}).get("thinking"),
        temperature=updates.get("llm", {}).get("temperature"),
    )

    restart = [k for k in _RESTART_KEYS
               if k.split(".")[0] in updates]
    return jsonify({
        "ok": True,
        "restart_required": restart,
        "llm_hot_swapped": True,
    })


# ── 手动重载 ────────────────────────────────────────────
@admin_bp.post("/reload")
@require_admin
def reload_all():
    """外部直接改了 SQLite 时，用这个让内存索引跟上。"""
    svc = _svc()
    svc.reload_rules()
    svc.reload_drugs()
    return jsonify({
        "ok": True,
        "rules": svc.engine.rule_count,
        "drugs_indexed": svc.normalizer.size,
    })
