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


# ── 总览统计（仪表盘）────────────────────────────────────
@admin_bp.get("/stats")
@require_admin
def stats_overview():
    """仪表盘数据：内容统计 + 内存索引状态。**不回显任何配置密钥**。"""
    from datetime import datetime, timedelta, timezone

    from ddi.db.audit import ensure_log_table

    ensure_log_table()  # 老库可能建表早于日志模块
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(
        timespec="seconds"
    )

    def _fill(keys: dict[str, int], counts: dict[str, int]) -> dict[str, int]:
        """补零：没有 contraindicated 规则时也要回 0，前端不用判 undefined。"""
        return {k: int(counts.get(k, 0)) for k in keys}

    with session() as conn:
        rule_total = conn.execute(
            "SELECT COUNT(*) AS n FROM ddi_rule"
        ).fetchone()["n"]
        by_status = {
            r["status"]: r["n"]
            for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM ddi_rule GROUP BY status"
            )
        }
        by_severity = {
            r["severity"]: r["n"]
            for r in conn.execute(
                "SELECT severity, COUNT(*) AS n FROM ddi_rule GROUP BY severity"
            )
        }
        with_source = conn.execute(
            "SELECT COUNT(DISTINCT rule_id) AS n FROM rule_source"
        ).fetchone()["n"]
        source_total = conn.execute(
            "SELECT COUNT(*) AS n FROM source"
        ).fetchone()["n"]
        drug_count = conn.execute("SELECT COUNT(*) AS n FROM drug").fetchone()["n"]
        ing_count = conn.execute(
            "SELECT COUNT(*) AS n FROM ingredient"
        ).fetchone()["n"]
        pop_count = conn.execute(
            "SELECT COUNT(*) AS n FROM population_rule"
        ).fetchone()["n"]
        logs_total = conn.execute(
            "SELECT COUNT(*) AS n FROM assess_log"
        ).fetchone()["n"]
        logs_recent = conn.execute(
            "SELECT COUNT(*) AS n FROM assess_log WHERE created_at >= ?",
            (cutoff,),
        ).fetchone()["n"]
        # import_draft 是懒建表（见 _ensure_import_table），老库可能还没有
        has_drafts = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='import_draft'"
        ).fetchone()
        import_pending = (
            conn.execute(
                "SELECT COUNT(*) AS n FROM import_draft WHERE status = 'pending'"
            ).fetchone()["n"]
            if has_drafts else 0
        )

    svc = _svc()
    rule_total = int(rule_total)
    return jsonify({
        "rules": {
            "total": rule_total,
            "by_status": _fill(STATUSES, by_status),
            "by_severity": _fill(SEVERITIES, by_severity),
            "with_source": int(with_source),
            # 溯源覆盖是当前数据最大的缺口（README 实测 20.3%），
            # 放进仪表盘就是让它每次打开都可见
            "source_rate": round(with_source / rule_total, 4) if rule_total else 0.0,
        },
        "drugs": int(drug_count),
        "ingredients": int(ing_count),
        "population": int(pop_count),
        "sources": int(source_total),
        "logs": {"total": int(logs_total), "recent_24h": int(logs_recent)},
        "imports": {"pending": int(import_pending)},
        "service": {
            "llm_configured": bool(svc.llm.configured),
            "llm_provider": svc.llm.provider_label if svc.llm.configured else None,
            "llm_model": svc.llm.model if svc.llm.configured else None,
            "rag_ready": svc.retriever is not None,
            "embed_backend": settings.embed_backend,
            "engine_rules": svc.engine.rule_count,
            "drugs_indexed": svc.normalizer.size,
        },
    })


# ── 智能导入（LLM 解析 → 待确认 → 确认入库）──────────────
# 流程：粘贴文本 → POST /import/parse 大模型抽取 → 草稿落 import_draft
# （pending）→ 管理员核对/编辑 → confirm 才写 ddi_rule → 或丢弃（硬删）。
# 红线：LLM 失败一行不写；确认事务里中途失败必须 raise（session() 正常
# 退出即 commit，中途 return 会把半截写入提交掉）。

IMPORT_TEXT_MAX = 8000
KIND_CN = {"ddi_rule": "DDI 规则", "drug": "药品", "population_rule": "人群规则"}

# 与 schema.sql 的 import_draft 一致。建表早于本功能的老库靠它懒建 ——
# 与 audit.ensure_log_table 同一套路，不依赖 init_db（测试库也不用动）。
_IMPORT_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS import_draft (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  kind        TEXT NOT NULL
              CHECK (kind IN ('ddi_rule','drug','population_rule')),
  status      TEXT NOT NULL DEFAULT 'pending'
              CHECK (status IN ('pending','confirmed')),
  fields      TEXT NOT NULL,
  raw_span    TEXT,
  warnings    TEXT DEFAULT '[]',
  batch_id    TEXT,
  source_name TEXT,
  target_id   INTEGER,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_import_draft_status ON import_draft(status, id DESC);
CREATE INDEX IF NOT EXISTS idx_import_draft_batch  ON import_draft(batch_id);
"""


def _ensure_import_table() -> None:
    """确保 import_draft 存在。幂等；每个导入接口入口调一次。"""
    with session() as conn:
        conn.executescript(_IMPORT_TABLE_SQL)


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _ImportError(Exception):
    """确认事务内的失败。raise 触发 session() rollback —— 这是写入开始后
    唯一安全的中止方式（正常 return 会把半截数据提交掉）。"""

    def __init__(self, msg: str, code: int = 400):
        super().__init__(msg)
        self.msg = msg
        self.code = code


def _draft_to_dict(row) -> dict:
    out = dict(row)
    for key in ("fields", "warnings"):
        raw = out.get(key)
        if isinstance(raw, str) and raw:
            try:
                out[key] = json.loads(raw)
            except (ValueError, TypeError):
                out[key] = {} if key == "fields" else []
    out["kind_cn"] = KIND_CN.get(out.get("kind"), out.get("kind"))
    return out


def _validate_drug_fields(raw: dict) -> tuple[dict | None, str | None]:
    """药品草稿硬校验 —— 与 drug_create 的标量校验同一套口径。"""
    name = str(raw.get("name_cn") or "").strip()
    if not name:
        return None, "name_cn 不能为空"
    if len(name) > 200:
        return None, "药名超长（最多 200 字）"
    form = str(raw.get("dosage_form") or "").strip() or None
    if form and len(form) > 50:
        return None, "剂型超长（最多 50 字）"
    trade, err = _json_str_list(raw.get("trade_names"), "trade_names")
    if err:
        return None, err
    alias, err = _json_str_list(raw.get("aliases"), "aliases")
    if err:
        return None, err
    approval = str(raw.get("approval_no") or "").strip() or None
    if approval and len(approval) > 100:
        return None, "批准文号超长（最多 100 字）"

    raw_ings = raw.get("ingredients")
    if not isinstance(raw_ings, list) or not raw_ings:
        return None, "ingredients 必填：至少关联一个成分"
    if len(raw_ings) > 30:
        return None, "成分最多 30 项"
    ings: list[dict] = []
    seen: set[str] = set()
    for i, item in enumerate(raw_ings):
        if not isinstance(item, dict):
            return None, f"ingredients[{i}] 必须是对象"
        nm = str(item.get("name") or "").strip()
        if not nm:
            return None, f"ingredients[{i}].name 不能为空"
        if len(nm) > 100:
            return None, f"ingredients[{i}] 成分名超长（最多 100 字）"
        st = str(item.get("strength") or "").strip() or None
        if st and len(st) > 50:
            return None, f"ingredients[{i}] 规格超长（最多 50 字）"
        if nm in seen:
            return None, "成分重复"
        seen.add(nm)
        entry: dict[str, Any] = {"name": nm}
        if st:
            entry["strength"] = st
        ings.append(entry)
    return {
        "name_cn": name,
        "dosage_form": form,
        "trade_names": trade,
        "aliases": alias,
        "is_otc": bool(raw.get("is_otc")),
        "is_tcm": bool(raw.get("is_tcm")),
        "approval_no": approval,
        "ingredients": ings,
    }, None


def _validate_draft_fields(kind: str, raw: Any) -> tuple[dict | None, str | None]:
    """确认/编辑前的硬校验。返回 (规范化 fields, 错误)。

    与 extract.normalize_candidate 的软校验分工：那边"能救就救"，
    这边是入库前的最后一道闸，**从严** —— 错误必须在此拦死。
    """
    if kind not in ("ddi_rule", "drug"):
        return None, f"暂不支持的类型（支持 ddi_rule / drug，得到 {kind}）"
    if not isinstance(raw, dict):
        return None, "fields 必须是对象"
    if kind == "drug":
        return _validate_drug_fields(raw)

    out: dict[str, Any] = {}
    for key in ("ing_a_name", "ing_b_name"):
        name = str(raw.get(key) or "").strip()
        if not name:
            return None, f"{key} 不能为空"
        if len(name) > 100:
            return None, f"{key} 超长（最多 100 字）"
        out[key] = name
    if out["ing_a_name"] == out["ing_b_name"]:
        return None, "成分对不能是同一个成分"
    if raw.get("severity") not in SEVERITIES:
        return None, f"severity 必须是 {'/'.join(SEVERITIES)} 之一"

    # 其余可编辑字段复用规则编辑的校验（severity 已单独查过必填）
    editable = {k: raw.get(k) for k in _RULE_EDITABLE if k in raw}
    updates, err = _validate_rule_body(editable)
    if err and err != "没有可更新的字段":
        return None, err
    out.update(updates)

    raw_sources = raw.get("sources")
    if raw_sources in (None, ""):
        raw_sources = []
    if not isinstance(raw_sources, list):
        return None, "sources 必须是数组"
    if len(raw_sources) > 10:
        return None, "sources 最多 10 条"
    sources: list[dict] = []
    for i, s in enumerate(raw_sources):
        if not isinstance(s, dict):
            return None, f"sources[{i}] 必须是对象"
        title = str(s.get("title") or "").strip()
        if not title:
            return None, f"sources[{i}].title 不能为空"
        if len(title) > 200:
            return None, f"sources[{i}].title 超长（最多 200 字）"
        year = s.get("year")
        if year in (None, ""):
            year = None
        else:
            try:
                year = int(year)
            except (TypeError, ValueError):
                return None, f"sources[{i}].year 必须是整数"
            if not 1000 <= year <= 2100:
                return None, f"sources[{i}].year 超出 1000–2100"

        def _txt(key: str, cap: int) -> str | None:
            val = str(s.get(key) or "").strip()
            if not val:
                return None
            if len(val) > cap:
                raise ValueError(f"sources[{i}].{key} 超长（最多 {cap} 字）")
            return val

        try:
            stype = _txt("source_type", 100)
            if stype is None:
                stype = _txt("type", 100)
            publisher = _txt("publisher", 200)
            url = _txt("url", 300)
            excerpt = _txt("excerpt", 2000)
        except ValueError as e:
            return None, str(e)
        sources.append({
            "title": title, "source_type": stype, "publisher": publisher,
            "year": year, "url": url, "excerpt": excerpt,
        })
    out["sources"] = sources
    return out, None


@admin_bp.post("/import/parse")
@require_admin
def import_parse():
    """粘贴文本 → LLM 抽取 → 草稿批量落暂存表。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _bad("请求体必须是 JSON 对象")
    text = str(body.get("text") or "").strip()
    if not text:
        return _bad("text 不能为空")
    if len(text) > IMPORT_TEXT_MAX:
        return _bad(
            f"文本过长（{len(text)} 字），上限 {IMPORT_TEXT_MAX} 字，"
            f"请拆分后分批解析"
        )
    source_name = str(body.get("source_name") or "").strip()[:200] or None

    from ddi.generate.extract import extract_candidates

    extraction = extract_candidates(text, client=_svc().llm)
    if extraction.degraded_reason:
        # 502 而非 400：失败在模型侧，且**一行都不写**，不留半坏状态
        return jsonify({
            "error": extraction.degraded_reason,
            "degraded_reason": extraction.degraded_reason,
        }), 502

    from uuid import uuid4

    batch_id = uuid4().hex[:12]
    now = _now()
    _ensure_import_table()
    drafts: list[dict] = []
    with session() as conn:
        for cand in extraction.candidates:
            cur = conn.execute(
                """INSERT INTO import_draft
                   (kind, status, fields, raw_span, warnings, batch_id,
                    source_name, created_at, updated_at)
                   VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?)
                   RETURNING id""",
                (
                    cand["kind"],
                    json.dumps(cand["fields"], ensure_ascii=False),
                    cand["raw_span"],
                    json.dumps(cand["warnings"], ensure_ascii=False),
                    batch_id,
                    source_name,
                    now,
                    now,
                ),
            )
            drafts.append({
                "id": int(cur.fetchone()["id"]),
                "kind": cand["kind"],
                "kind_cn": KIND_CN.get(cand["kind"], cand["kind"]),
                "fields": cand["fields"],
                "raw_span": cand["raw_span"],
                "warnings": cand["warnings"],
            })
    return jsonify({
        "ok": True,
        "batch_id": batch_id,
        "used_llm": extraction.used_llm,
        "model": extraction.model,
        "drafts": drafts,
        "skipped": extraction.skipped,
    }), 201


@admin_bp.get("/import/drafts")
@require_admin
def import_drafts_list():
    _ensure_import_table()
    status = (request.args.get("status") or "pending").strip()
    kind = (request.args.get("kind") or "").strip()
    batch_id = (request.args.get("batch_id") or "").strip()
    q = (request.args.get("q") or "").strip()
    page = max(1, int(request.args.get("page", 1)))
    page_size = max(1, min(100, int(request.args.get("page_size", 20))))

    where: list[str] = []
    params: list[Any] = []
    if status != "all":
        if status not in ("pending", "confirmed"):
            return _bad("status 必须是 pending / confirmed / all")
        where.append("status = ?")
        params.append(status)
    if kind:
        if kind not in KIND_CN:
            return _bad(f"kind 必须是 {'/'.join(KIND_CN)} 之一")
        where.append("kind = ?")
        params.append(kind)
    if batch_id:
        where.append("batch_id = ?")
        params.append(batch_id)
    if q:
        where.append("(fields LIKE ? OR IFNULL(raw_span, '') LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like])
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    import math

    with session() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM import_draft{where_sql}", params
        ).fetchone()["n"]
        rows = conn.execute(
            f"SELECT * FROM import_draft{where_sql} ORDER BY id DESC"
            " LIMIT ? OFFSET ?",
            params + [page_size, (page - 1) * page_size],
        ).fetchall()
    pages = max(1, math.ceil(total / page_size)) if total else 1
    return jsonify({
        "items": [_draft_to_dict(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    })


@admin_bp.get("/import/drafts/<int:draft_id>")
@require_admin
def import_draft_get(draft_id: int):
    _ensure_import_table()
    with session() as conn:
        row = conn.execute(
            "SELECT * FROM import_draft WHERE id = ?", (draft_id,)
        ).fetchone()
    if not row:
        return _bad("草稿不存在", 404)
    return jsonify(_draft_to_dict(row))


@admin_bp.put("/import/drafts/<int:draft_id>")
@require_admin
def import_draft_update(draft_id: int):
    """人工修正草稿字段。kind / status / target_id 不可改。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("fields"), dict):
        return _bad("请求体需包含 fields 对象")
    _ensure_import_table()
    with session() as conn:
        row = conn.execute(
            "SELECT * FROM import_draft WHERE id = ?", (draft_id,)
        ).fetchone()
        if not row:
            return _bad("草稿不存在", 404)
        if row["status"] != "pending":
            return _bad("已确认的草稿不可编辑", 409)
        # 硬校验在**任何写入之前** —— 校验失败直接 return 是安全的
        normalized, err = _validate_draft_fields(row["kind"], body["fields"])
        if err:
            return _bad(err)
        conn.execute(
            "UPDATE import_draft SET fields = ?, updated_at = ? WHERE id = ?",
            (json.dumps(normalized, ensure_ascii=False), _now(), draft_id),
        )
    return jsonify({"ok": True, "updated": ["fields"]})


@admin_bp.delete("/import/drafts/<int:draft_id>")
@require_admin
def import_draft_delete(draft_id: int):
    """丢弃草稿（硬删除）。confirmed 的清理也走这里。"""
    _ensure_import_table()
    with session() as conn:
        exists = conn.execute(
            "SELECT 1 FROM import_draft WHERE id = ?", (draft_id,)
        ).fetchone()
        if not exists:
            return _bad("草稿不存在", 404)
        conn.execute("DELETE FROM import_draft WHERE id = ?", (draft_id,))
    return jsonify({"ok": True})


@admin_bp.post("/import/drafts/<int:draft_id>/confirm")
@require_admin
def import_confirm(draft_id: int):
    """确认入库：草稿 → ddi_rule（+ source 溯源）。未知成分自动创建。"""
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return _bad("请求体必须是 JSON 对象")
    status_override = body.get("status")
    if status_override is not None and status_override not in STATUSES:
        return _bad(f"status 必须是 {'/'.join(STATUSES)} 之一")

    _ensure_import_table()
    kind: str | None = None
    target_id: int | None = None
    created: list[str] = []
    reload_kind = "rules"   # drug → reload_drugs，其余（规则）→ reload_rules
    try:
        with session() as conn:
            row = conn.execute(
                "SELECT * FROM import_draft WHERE id = ?", (draft_id,)
            ).fetchone()
            if not row:
                raise _ImportError("草稿不存在", 404)
            if row["status"] != "pending":
                raise _ImportError("草稿已确认过", 409)
            kind = row["kind"]
            try:
                raw_fields = json.loads(row["fields"] or "{}")
            except (ValueError, TypeError):
                raise _ImportError("草稿 fields 损坏，无法解析", 500) from None
            fields, err = _validate_draft_fields(kind, raw_fields)
            if err:
                raise _ImportError(f"草稿校验未通过：{err}", 400)

            if kind == "ddi_rule":
                # 确认即人工审核通过 → 默认 reviewed；可用 body.status 覆盖
                fields["status"] = (
                    status_override or fields.get("status") or "reviewed"
                )
                if body.get("reviewed_by") is not None:
                    fields["reviewed_by"] = (
                        str(body["reviewed_by"]).strip()[:100] or None
                    )

                # ── 读阶段（一行未写）：成分解析 + 查重 ──
                name_a, name_b = fields["ing_a_name"], fields["ing_b_name"]
                resolved: dict[str, int] = {}
                for nm in {name_a, name_b}:
                    hit = conn.execute(
                        "SELECT id FROM ingredient WHERE name_cn = ?", (nm,)
                    ).fetchone()
                    if hit:
                        resolved[nm] = int(hit["id"])
                if len(resolved) == 2:
                    id_a, id_b = sorted(resolved.values())
                    dup = conn.execute(
                        "SELECT id FROM ddi_rule"
                        " WHERE ing_a_id = ? AND ing_b_id = ?",
                        (id_a, id_b),
                    ).fetchone()
                    if dup:
                        raise _ImportError(
                            f"该成分对已有规则 #{dup['id']}，"
                            f"请直接编辑它或丢弃本草稿",
                            409,
                        )

                # ── 写阶段：此后**只 raise 不 return**（return = 半截提交）──
                for nm in (name_a, name_b):
                    if nm not in resolved:
                        iid = int(conn.execute(
                            "INSERT INTO ingredient (name_cn) VALUES (?)", (nm,)
                        ).lastrowid)
                        resolved[nm] = iid
                        created.append(nm)
                id_a, id_b = sorted((resolved[name_a], resolved[name_b]))
                cols = ["ing_a_id", "ing_b_id"]
                vals: list[Any] = [id_a, id_b]
                for key in ("severity", "mechanism", "consequence",
                            "suggestion", "evidence_level", "status",
                            "reviewed_by"):
                    if key in fields:
                        cols.append(key)
                        vals.append(fields[key])
                cur = conn.execute(
                    f"INSERT INTO ddi_rule ({', '.join(cols)})"
                    f" VALUES ({', '.join('?' * len(cols))})",
                    vals,
                )
                target_id = int(cur.lastrowid)

                from ddi.db.sources import link_rule_source, upsert_source

                for src in fields.get("sources", []):
                    sid = upsert_source(conn, src)
                    link_rule_source(conn, target_id, sid, src.get("excerpt"))
                reload_kind = "rules"

            else:  # kind == "drug"（校验层已拦掉其他类型）
                # ── 读阶段：同名同剂型查重 + 成分解析（全只读）──
                drug_name = fields["name_cn"]
                form = fields["dosage_form"]
                dup = conn.execute(
                    "SELECT id FROM drug"
                    " WHERE name_cn = ? AND IFNULL(dosage_form, '') = IFNULL(?, '')",
                    (drug_name, form),
                ).fetchone()
                if dup:
                    raise _ImportError(
                        f"同名同剂型药品已存在（id={dup['id']}）", 409
                    )
                resolved_d: dict[str, int] = {}
                to_create: list[str] = []
                for ing in fields["ingredients"]:
                    nm = ing["name"]
                    if nm in resolved_d:
                        continue
                    hit = conn.execute(
                        "SELECT id FROM ingredient WHERE name_cn = ?", (nm,)
                    ).fetchone()
                    if hit:
                        resolved_d[nm] = int(hit["id"])
                    else:
                        to_create.append(nm)

                # ── 写阶段：只 raise 不 return ──
                for nm in to_create:
                    iid = int(conn.execute(
                        "INSERT INTO ingredient (name_cn) VALUES (?)", (nm,)
                    ).lastrowid)
                    resolved_d[nm] = iid
                    created.append(nm)
                from ddi.db.loader import _pinyin

                full_py, abbr_py = _pinyin(drug_name)
                target_id = int(conn.execute(
                    """INSERT INTO drug (name_cn, trade_names, aliases, pinyin,
                                        pinyin_abbr, dosage_form, is_otc, is_tcm,
                                        approval_no)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        drug_name,
                        json.dumps(fields["trade_names"], ensure_ascii=False),
                        json.dumps(fields["aliases"], ensure_ascii=False),
                        full_py,
                        abbr_py,
                        form,
                        int(fields["is_otc"]),
                        int(fields["is_tcm"]),
                        fields["approval_no"],
                    ),
                ).lastrowid)
                for ing in fields["ingredients"]:
                    conn.execute(
                        "INSERT INTO drug_ingredient"
                        " (drug_id, ingredient_id, strength) VALUES (?, ?, ?)",
                        (target_id, resolved_d[ing["name"]],
                         ing.get("strength")),
                    )
                reload_kind = "drugs"

            conn.execute(
                "UPDATE import_draft SET status = 'confirmed', target_id = ?,"
                " updated_at = ? WHERE id = ?",
                (target_id, _now(), draft_id),
            )
    except _ImportError as e:
        return _bad(e.msg, e.code)

    if reload_kind == "drugs":
        _svc().reload_drugs()
    else:
        _svc().reload_rules()
    return jsonify({
        "ok": True,
        "kind": kind,
        "target_id": target_id,
        "created_ingredients": created,
    }), 201


# ── 成分（新建规则/药品时的选择器数据源）────────────────
@admin_bp.get("/ingredients")
@require_admin
def ingredients_list():
    q = (request.args.get("q") or "").strip()
    page = max(1, int(request.args.get("page", 1)))
    page_size = max(1, min(500, int(request.args.get("page_size", 200))))

    where, params = "", []
    if q:
        where = " WHERE name_cn LIKE ? OR IFNULL(name_en, '') LIKE ?"
        like = f"%{q}%"
        params = [like, like]

    import math

    with session() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM ingredient{where}", params
        ).fetchone()["n"]
        rows = conn.execute(
            f"SELECT id, name_cn, name_en, category FROM ingredient{where}"
            " ORDER BY name_cn LIMIT ? OFFSET ?",
            params + [page_size, (page - 1) * page_size],
        ).fetchall()

    pages = max(1, math.ceil(total / page_size)) if total else 1
    return jsonify({
        "items": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
    })


@admin_bp.post("/ingredients")
@require_admin
def ingredient_create():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _bad("请求体必须是 JSON 对象")
    name = str(body.get("name_cn") or "").strip()
    if not name:
        return _bad("name_cn 不能为空")
    if len(name) > 100:
        return _bad("成分名超长（最多 100 字）")
    name_en = str(body.get("name_en") or "").strip() or None
    category = str(body.get("category") or "").strip() or None
    if category and len(category) > 50:
        return _bad("分类超长（最多 50 字）")

    with session() as conn:
        exists = conn.execute(
            "SELECT id FROM ingredient WHERE name_cn = ?", (name,)
        ).fetchone()
        if exists:
            return _bad(f"成分已存在（id={exists['id']}）", 409)
        cur = conn.execute(
            "INSERT INTO ingredient (name_cn, name_en, category) VALUES (?, ?, ?)",
            (name, name_en, category),
        )
        new_id = int(cur.lastrowid)
    # 成分本身不进内存索引（规则引擎按规则加载、归一化按药品加载），
    # 真正生效是在它被挂上规则/药品之后的那次 reload。
    return jsonify({"ok": True, "id": new_id, "name_cn": name}), 201


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
    # 只有按成分名搜索（q）才需要 JOIN 两张成分表 ——
    # 纯状态/严重度筛选直接数主表，少两次 JOIN。
    count_from = (
        " FROM ddi_rule r"
        " JOIN ingredient ia ON ia.id = r.ing_a_id"
        " JOIN ingredient ib ON ib.id = r.ing_b_id"
        if q else " FROM ddi_rule r"
    )
    with session() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS n{count_from}{where_sql}", params,
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


@admin_bp.post("/rules")
@require_admin
def rule_create():
    """新建规则。成分对由服务端规范成 ing_a_id < ing_b_id（schema CHECK）。"""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _bad("请求体必须是 JSON 对象")

    severity = body.get("severity")
    if severity not in SEVERITIES:
        return _bad(f"severity 必须是 {'/'.join(SEVERITIES)} 之一")

    try:
        id_a = int(body.get("ing_a_id"))
        id_b = int(body.get("ing_b_id"))
    except (TypeError, ValueError):
        return _bad("ing_a_id / ing_b_id 必填且必须是整数")
    if id_a == id_b:
        return _bad("成分对不能是同一个成分")
    # 小 id 在前 —— 与 UNIQUE(ing_a_id, ing_b_id) + CHECK 配套，选反了服务端兜底
    id_a, id_b = sorted((id_a, id_b))

    # 其余字段复用编辑校验；status 缺省走 schema 默认 draft
    editable = {k: v for k, v in body.items() if k in _RULE_EDITABLE and k != "severity"}
    updates, err = _validate_rule_body(editable) if editable else ({}, None)
    if err and err != "没有可更新的字段":
        return _bad(err)

    cols = ["ing_a_id", "ing_b_id", "severity"] + sorted(updates)
    vals: list[Any] = [id_a, id_b, severity] + [updates[k] for k in sorted(updates)]

    with session() as conn:
        for iid in (id_a, id_b):
            if not conn.execute(
                "SELECT 1 FROM ingredient WHERE id = ?", (iid,)
            ).fetchone():
                return _bad(f"成分不存在（id={iid}）", 404)
        dup = conn.execute(
            "SELECT id FROM ddi_rule WHERE ing_a_id = ? AND ing_b_id = ?",
            (id_a, id_b),
        ).fetchone()
        if dup:
            return _bad(f"该成分对已有规则 #{dup['id']}，请直接编辑它", 409)
        try:
            cur = conn.execute(
                f"INSERT INTO ddi_rule ({', '.join(cols)})"
                f" VALUES ({', '.join('?' * len(cols))})",
                vals,
            )
        except Exception as e:  # noqa: BLE001 — SQLite CHECK 约束失败
            return _bad(f"创建失败：{e}")
        new_id = int(cur.lastrowid)

    _svc().reload_rules()
    return jsonify({"ok": True, "id": new_id}), 201


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


@admin_bp.post("/rules/batch")
@require_admin
def rules_batch():
    """批量改状态 / 批量删除。审核流（draft → reviewed → published）
    靠它一次走完 —— 182 条规则逐条编辑是不现实的。

    批量相对逐条的另一个收益：**只重载一次规则引擎**（reload_rules
    是全量重建，逐条操作会把重建成本放大 N 倍）。
    """
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _bad("请求体必须是 JSON 对象")
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        return _bad("ids 必须是非空数组")
    try:
        id_list = sorted({int(i) for i in ids})
    except (TypeError, ValueError):
        return _bad("ids 必须全是整数")
    if len(id_list) > 500:
        return _bad("一次最多处理 500 条")

    action = body.get("action")
    updates: dict[str, Any] = {}
    if action == "set_status":
        updates, err = _validate_rule_body({"status": body.get("status")})
        if err:
            return _bad(err)
    elif action != "delete":
        return _bad("action 必须是 set_status 或 delete")

    with session() as conn:
        # 先校验全部 id 都存在，再动写入 —— session() 正常退出即 commit，
        # 中途 return 会把半截写入提交掉（这是本文件反复强调的坑）。
        ph = ",".join("?" * len(id_list))
        existing = {
            r["id"] for r in conn.execute(
                f"SELECT id FROM ddi_rule WHERE id IN ({ph})", id_list
            ).fetchall()
        }
        missing = [i for i in id_list if i not in existing]
        if missing:
            shown = ", ".join(str(i) for i in missing[:10])
            more = f" 等 {len(missing)} 条" if len(missing) > 10 else ""
            return _bad(f"规则不存在：{shown}{more}", 404)

        if action == "delete":
            cur = conn.execute(
                f"DELETE FROM ddi_rule WHERE id IN ({ph})", id_list
            )
        else:
            sets = ", ".join(f"{k} = ?" for k in updates)
            cur = conn.execute(
                f"UPDATE ddi_rule SET {sets} WHERE id IN ({ph})",
                list(updates.values()) + id_list,
            )
        affected = cur.rowcount

    _svc().reload_rules()
    return jsonify({"ok": True, "action": action, "affected": affected})


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


def _json_str_list(val: Any, key: str) -> tuple[list[str] | None, str | None]:
    """接受 JSON 数组或顿号/逗号分隔的字符串，归一化成去空列表。"""
    if val in (None, ""):
        return [], None
    if isinstance(val, str):
        items = [s.strip() for s in val.replace("，", "、").replace(",", "、").split("、")]
    elif isinstance(val, list):
        items = [str(s).strip() for s in val]
    else:
        return None, f"{key} 必须是数组或字符串"
    items = [s for s in items if s]
    if len(items) > 50:
        return None, f"{key} 最多 50 项"
    if any(len(s) > 100 for s in items):
        return None, f"{key} 单项超长（最多 100 字）"
    return items, None


@admin_bp.post("/drugs")
@require_admin
def drug_create():
    """新建药品并挂成分。成分按名字匹配，库里没有的自动创建 ——
    后台加一个新药时成分往往也是新的，分两步徒增摩擦。"""
    from ddi.db.loader import _pinyin

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _bad("请求体必须是 JSON 对象")

    name = str(body.get("name_cn") or "").strip()
    if not name:
        return _bad("name_cn 不能为空")
    if len(name) > 200:
        return _bad("药名超长（最多 200 字）")
    form = str(body.get("dosage_form") or "").strip() or None
    if form and len(form) > 50:
        return _bad("剂型超长（最多 50 字）")

    trade, err = _json_str_list(body.get("trade_names"), "trade_names")
    if err:
        return _bad(err)
    alias, err = _json_str_list(body.get("aliases"), "aliases")
    if err:
        return _bad(err)

    raw_ings = body.get("ingredients")
    if not isinstance(raw_ings, list) or not raw_ings:
        return _bad("ingredients 必填：至少关联一个成分")
    if len(raw_ings) > 30:
        return _bad("成分最多 30 项")

    # 先在事务外校验/解析每一条成分，避免写了一半才发现脏数据
    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_ings:
        if not isinstance(item, dict):
            return _bad("ingredients 每项必须是对象")
        ing_id = item.get("id")
        ing_name = str(item.get("name") or "").strip()
        if ing_id is None and not ing_name:
            return _bad("成分项需要 id 或 name")
        if ing_name and len(ing_name) > 100:
            return _bad("成分名超长（最多 100 字）")
        strength = str(item.get("strength") or "").strip() or None
        if strength and len(strength) > 50:
            return _bad("规格超长（最多 50 字）")
        category = str(item.get("category") or "").strip() or None
        if category and len(category) > 50:
            return _bad("成分分类超长（最多 50 字）")
        key = f"id:{ing_id}" if ing_id is not None else f"name:{ing_name}"
        if key in seen:
            return _bad("成分重复")
        seen.add(key)
        parsed.append({
            "id": ing_id, "name": ing_name, "strength": strength,
            "category": category,
        })

    full_py, abbr_py = _pinyin(name)
    is_otc = 1 if body.get("is_otc") else 0
    is_tcm = 1 if body.get("is_tcm") else 0
    approval = str(body.get("approval_no") or "").strip() or None

    # session() 正常退出即 commit —— 所以所有能在校验里拦下的错误
    # 必须在**任何写入之前**返回，否则中途 return 会把半截数据提交掉。
    with session() as conn:
        dup = conn.execute(
            "SELECT id FROM drug WHERE name_cn = ? AND IFNULL(dosage_form, '') = IFNULL(?, '')",
            (name, form),
        ).fetchone()
        if dup:
            return _bad(f"同名同剂型药品已存在（id={dup['id']}）", 409)

        # 解析阶段只读：已有的记 id，没有的攒起来待建。
        # 同名重复已在 seen 里拦掉，to_create 里每个名字只出现一次。
        resolved: list[tuple[int, str | None]] = []   # 负 id = 待建成分的占位
        to_create: list[dict[str, Any]] = []
        for p in parsed:
            if p["id"] is not None:
                row = conn.execute(
                    "SELECT id FROM ingredient WHERE id = ?", (int(p["id"]),)
                ).fetchone()
                if not row:
                    return _bad(f"成分不存在（id={p['id']}）", 404)
                resolved.append((int(row["id"]), p["strength"]))
            else:
                row = conn.execute(
                    "SELECT id FROM ingredient WHERE name_cn = ?", (p["name"],)
                ).fetchone()
                if row:
                    resolved.append((int(row["id"]), p["strength"]))
                else:
                    to_create.append(p)
                    resolved.append((-len(to_create), p["strength"]))

        # ── 校验全部通过，开始写 ──
        placeholders: dict[int, int] = {}
        for p in to_create:
            iid = int(conn.execute(
                "INSERT INTO ingredient (name_cn, category) VALUES (?, ?)",
                (p["name"], p["category"]),
            ).lastrowid)
            placeholders[len(placeholders) + 1] = iid

        try:
            drug_id = int(conn.execute(
                """INSERT INTO drug (name_cn, trade_names, aliases, pinyin, pinyin_abbr,
                                     dosage_form, is_otc, is_tcm, approval_no)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    name,
                    json.dumps(trade, ensure_ascii=False),
                    json.dumps(alias, ensure_ascii=False),
                    full_py,
                    abbr_py,
                    form,
                    is_otc,
                    is_tcm,
                    approval,
                ),
            ).lastrowid)
        except Exception as e:  # noqa: BLE001 — UNIQUE(name_cn, dosage_form)
            return _bad(f"创建失败：{e}")

        for iid, strength in resolved:
            if iid < 0:
                iid = placeholders[-iid]
            conn.execute(
                "INSERT INTO drug_ingredient (drug_id, ingredient_id, strength)"
                " VALUES (?, ?, ?)",
                (drug_id, iid, strength),
            )

    _svc().reload_drugs()
    return jsonify({"ok": True, "id": drug_id}), 201


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
