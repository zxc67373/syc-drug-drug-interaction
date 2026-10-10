"""LLM 结构化抽取：自由文本 → 结构化导入草稿（待确认）。

与 :mod:`ddi.generate.generator` 的分工：

- ``generator`` 是**评估链路**的生成层，产出给患者看的解释，带二次规则校验，
  降级时回退纯规则答案；
- 本模块是**管理后台智能导入**的抽取层，产出给管理员审核的草稿，
  落 ``import_draft`` 暂存表，人工确认前不碰任何正式表。

两者的共同红线：**绝不向上抛异常** —— 失败用 ``Extraction.degraded_reason``
表达，由 HTTP 层决定怎么报。

为什么输出必须包一层 ``{"candidates": [...]}``：
``llm_client.extract_json`` 只接受顶层对象（dict），顶层数组会被当解析失败。
这是刻意约束，测试里有专门用例锁住，所以 schema 里明令禁止顶层数组。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

MAX_INPUT_CHARS = 8000   # 超过直接拒绝，不做静默截断 —— 临床文本截半条比报错危险
MAX_CANDIDATES = 20
EXTRACT_MAX_TOKENS = 4096   # 配置默认 2048，多候选 JSON 会截半条
MAX_SOURCES = 10
RAW_SPAN_MAX = 500

_SEVERITIES = ("contraindicated", "caution", "monitor")
_EVIDENCE = ("A", "B", "C", "D")
_TEXT_MAX = 2000

EXTRACT_SYSTEM_PROMPT = """你是医药数据结构化助手，服务于药师录入药品数据库与 DDI（药物-药物相互作用）规则库。

你的任务：从用户提供的原文（药品说明书、文献、相互作用列表等）中**抽取**结构化候选，交给人工审核后入库。

严格遵守：
1. 只抽取原文中**明确提到**的内容，禁止用外部知识补充、推断或发明。原文没写的字段留 null 或省略。
2. 只抽两类（kind 二选一）：
   - "ddi_rule"：成分与成分之间的相互作用规则（含严重度、机制、建议）
   - "drug"：药品条目（通用名、剂型、成分组成、商品名等说明书基础信息）
   不要抽人群禁忌或其他类型。
3. 每条候选必须带 raw_span：支撑本条的原文片段（尽量短，≤200 字），供人工回查。
4. 拿不准的地方（严重度判断、成分名不规范、机制表述模糊）写进 warnings 数组，不要编造确定性。
5. 输出严格的 JSON：顶层必须是对象且只含 "candidates" 键；**禁止输出顶层数组**、markdown 围栏或任何解释性文字。
6. 最多输出 20 条候选；没有可抽取的内容就输出空数组。

ddi_rule 的 severity 只能取这三个英文枚举值（对应原文的禁用/避免/慎用/注意/监测等表述）：
- contraindicated：禁忌、禁用、避免合用
- caution：慎用、谨慎合用、不推荐
- monitor：注意、监测、关注

drug 的 ingredients（成分组成）至少一项 —— 没有成分信息的药品条目不要抽。
原文里有出处（说明书名称、指南、文献、药典）时**务必**填进 sources 并尽量带上 excerpt 原文摘录 —— 溯源是规则库的核心要求。"""

EXTRACT_OUTPUT_SCHEMA = """{
  "candidates": [
    {
      "kind": "ddi_rule",
      "raw_span": "支撑本条的原文片段（≤200 字）",
      "fields": {
        "ing_a_name": "成分A中文名",
        "ing_b_name": "成分B中文名",
        "severity": "contraindicated | caution | monitor",
        "mechanism": "相互作用机制（原文没有则 null）",
        "consequence": "可能导致的后果（原文没有则 null）",
        "suggestion": "用药建议（原文没有则 null）",
        "evidence_level": "A | B | C | D | null",
        "sources": [
          {
            "title": "出处标题（如：华法林钠片说明书）",
            "source_type": "说明书 | 药典 | 临床指南 | 文献",
            "publisher": "发布方（可 null）",
            "year": 2019,
            "url": "链接（可 null）",
            "excerpt": "原文摘录"
          }
        ]
      },
      "warnings": ["不确定之处，没有则空数组"]
    },
    {
      "kind": "drug",
      "raw_span": "支撑本条的原文片段（≤200 字）",
      "fields": {
        "name_cn": "药品通用名（如 阿莫西林胶囊）",
        "dosage_form": "剂型（如 胶囊，可 null）",
        "trade_names": ["商品名，如 阿莫仙"],
        "aliases": ["别名，如 羟氨苄青霉素"],
        "is_otc": false,
        "is_tcm": false,
        "approval_no": "批准文号（可 null）",
        "ingredients": [
          {"name": "成分名（必填，如 阿莫西林）", "strength": "规格（可 null，如 0.25g）"}
        ]
      },
      "warnings": ["不确定之处，没有则空数组"]
    }
  ]
}"""


def build_extract_user_prompt(text: str) -> str:
    """组装抽取用的用户消息。schema 内联 + 明示禁止顶层数组。"""
    return f"""请从下面的原文中抽取 DDI 规则候选。

【输出 JSON Schema】
{EXTRACT_OUTPUT_SCHEMA}

【输出要求】
- 顶层必须是对象 {{"candidates": [...]}}，**禁止输出顶层数组**
- severity 只能是 contraindicated / caution / monitor
- 每条必须带 raw_span 原文片段
- 有出处时务必填 sources + excerpt
- 最多 20 条
- 请只输出 JSON

【原文】
{text}"""


@dataclass
class Extraction:
    """抽取结果。任何失败路径都返回本结构，不抛异常。"""

    candidates: list[dict] = field(default_factory=list)
    # 每项 {"raw_span": str|None, "reason": str} —— 模型抽了但不该入库的
    skipped: list[dict] = field(default_factory=list)
    used_llm: bool = False
    degraded_reason: str | None = None
    model: str | None = None


def _cap(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n]


def _opt_text(val, key: str, cap: int, warns: list[str]) -> str | None:
    """可选文本：转 str、去空、截断到 cap（超长记 warning）。"""
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    if len(s) > cap:
        warns.append(f"{key} 超长已截断至 {cap} 字")
        s = s[:cap]
    return s


def _opt_bool(val) -> bool:
    """宽松布尔：模型偶发把 true 写成 "是"/"true" 字符串。"""
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "y", "是")
    return bool(val)


def _str_list(val, key: str, warns: list[str]) -> list[str]:
    """商品名/别名：数组或顿号字符串 → 去空列表（与后台 _json_str_list 同语义）。"""
    if val in (None, ""):
        return []
    if isinstance(val, str):
        items = [s.strip() for s in val.replace("，", "、").replace(",", "、").split("、")]
    elif isinstance(val, list):
        items = [str(s).strip() for s in val]
    else:
        warns.append(f"{key} 格式不对已忽略")
        return []
    items = [s for s in items if s]
    if len(items) > 50:
        warns.append(f"{key} 超过 50 项已截断")
        items = items[:50]
    over = [s for s in items if len(s) > 100]
    if over:
        warns.append(f"{key} 有 {len(over)} 项超长已丢弃")
        items = [s for s in items if len(s) <= 100]
    return items


def _normalize_drug_fields(
    fields_in: dict, warns: list[str]
) -> tuple[dict | None, str | None]:
    """药品候选软归一化。返回 (fields, None) 或 (None, 跳过原因)。"""
    name = str(fields_in.get("name_cn") or "").strip()
    if not name:
        return None, "缺少药品名 name_cn"
    if len(name) > 200:
        return None, "药品名超长（>200 字）"

    raw_ings = fields_in.get("ingredients")
    if not isinstance(raw_ings, list):
        raw_ings = []
        if fields_in.get("ingredients") not in (None, ""):
            warns.append("ingredients 不是数组")
    ings: list[dict] = []
    seen: set[str] = set()
    for it in raw_ings[:30]:
        if not isinstance(it, dict):
            warns.append("非对象的成分项已丢弃")
            continue
        nm = str(it.get("name") or "").strip()
        if not nm:
            warns.append("缺名的成分项已丢弃")
            continue
        if len(nm) > 100:
            warns.append(f"成分名「{_cap(nm, 20)}…」超长已丢弃")
            continue
        if nm in seen:
            continue
        seen.add(nm)
        entry: dict = {"name": nm}
        strength = _opt_text(it.get("strength"), "strength", 50, warns)
        if strength:
            entry["strength"] = strength
        ings.append(entry)
    if not ings:
        # 挂不出成分的药品没有意义 —— 人工也没法凭空补
        return None, "药品缺少成分（ingredients 至少一项）"

    return {
        "name_cn": name,
        "dosage_form": _opt_text(fields_in.get("dosage_form"), "dosage_form", 50, warns),
        "trade_names": _str_list(fields_in.get("trade_names"), "trade_names", warns),
        "aliases": _str_list(fields_in.get("aliases"), "aliases", warns),
        "is_otc": _opt_bool(fields_in.get("is_otc")),
        "is_tcm": _opt_bool(fields_in.get("is_tcm")),
        "approval_no": _opt_text(fields_in.get("approval_no"), "approval_no", 100, warns),
        "ingredients": ings,
    }, None


def normalize_candidate(raw: dict) -> tuple[dict | None, str | None]:
    """软校验 + 类型收敛。返回 (草稿, 跳过原因)，二者只出其一。

    与确认入库的硬校验（admin._validate_draft_fields）分工：
    这里**能救就救**（加 warning 留给人工修），只有救不了的才跳过
    （类型不对、缺标识字段）。严重度这种枚举错了也不拦 ——
    管理员在编辑器里改一下就行，拦在暂存外反而少了一次纠错机会。
    """
    if not isinstance(raw, dict):
        return None, "候选不是 JSON 对象"

    kind = str(raw.get("kind") or "").strip()
    if kind not in ("ddi_rule", "drug"):
        return None, f"不支持的类型「{kind or '空'}」（当前只支持 ddi_rule / drug）"

    fields_in = raw.get("fields")
    if not isinstance(fields_in, dict):
        return None, "缺少 fields 对象"

    warns: list[str] = []
    for w in (raw.get("warnings") or [])[:20]:
        if isinstance(w, (str, int, float)):
            warns.append(_cap(str(w), 200))

    # ── 药品：独立分支（字段集与规则完全不同），抽完直接返回 ──
    if kind == "drug":
        fields, why = _normalize_drug_fields(fields_in, warns)
        if fields is None:
            return None, why
        raw_span = _opt_text(raw.get("raw_span"), "raw_span", RAW_SPAN_MAX, warns)
        return {"kind": kind, "fields": fields, "raw_span": raw_span,
                "warnings": warns}, None

    # ── DDI 规则 ──
    # 标识字段：缺了人工无从修起，跳过
    fields: dict = {}
    for key in ("ing_a_name", "ing_b_name"):
        name = str(fields_in.get(key) or "").strip()
        if not name:
            return None, f"缺少成分名 {key}"
        if len(name) > 100:
            return None, f"成分名 {key} 超长（>100 字）"
        fields[key] = name
    if fields["ing_a_name"] == fields["ing_b_name"]:
        return None, "两个成分名相同"

    # ── 枚举字段：错了记 warning，留给人工在编辑器里改 ──
    sev = str(fields_in.get("severity") or "").strip().lower()
    if sev in _SEVERITIES:
        fields["severity"] = sev
    else:
        warns.append(f"severity「{sev or '空'}」不合法，需人工改为 "
                     f"{'/'.join(_SEVERITIES)} 之一")
        fields["severity"] = sev  # 原样暂存，确认时硬校验会拦

    ev = str(fields_in.get("evidence_level") or "").strip().upper()
    if ev in _EVIDENCE:
        fields["evidence_level"] = ev
    else:
        fields["evidence_level"] = None
        if ev and ev not in ("NULL", "NONE"):
            warns.append(f"evidence_level「{ev}」不合法，已置空")

    for key in ("mechanism", "consequence", "suggestion"):
        fields[key] = _opt_text(fields_in.get(key), key, _TEXT_MAX, warns)

    # ── sources：结构错了丢条目 + warning，不让一条烂 source 毁整批 ──
    raw_sources = fields_in.get("sources")
    sources: list[dict] = []
    if isinstance(raw_sources, list):
        if len(raw_sources) > MAX_SOURCES:
            warns.append(f"sources 超过 {MAX_SOURCES} 条，多余已丢弃")
        for s in raw_sources[:MAX_SOURCES]:
            if not isinstance(s, dict):
                warns.append("有非对象的 source 已丢弃")
                continue
            title = str(s.get("title") or "").strip()
            if not title:
                warns.append("缺 title 的 source 已丢弃")
                continue
            year = s.get("year")
            if year in (None, ""):
                year = None
            else:
                try:
                    year = int(year)
                except (TypeError, ValueError):
                    warns.append(f"source「{_cap(title, 30)}」的 year 非整数已置空")
                    year = None
            excerpt = _opt_text(s.get("excerpt"), "excerpt", _TEXT_MAX, warns)
            sources.append({
                "title": _cap(title, 200),
                "source_type": _opt_text(
                    s.get("source_type") or s.get("type"), "source_type", 100, warns
                ),
                "publisher": _opt_text(s.get("publisher"), "publisher", 200, warns),
                "year": year,
                "url": _opt_text(s.get("url"), "url", 300, warns),
                "excerpt": excerpt,
            })
    elif raw_sources not in (None, ""):
        warns.append("sources 不是数组已忽略")
    fields["sources"] = sources

    raw_span = _opt_text(raw.get("raw_span"), "raw_span", RAW_SPAN_MAX, warns)

    return {"kind": kind, "fields": fields, "raw_span": raw_span,
            "warnings": warns}, None


def extract_candidates(text: str, *, client=None) -> Extraction:
    """调 LLM 抽取候选。**任何异常都降级为带 degraded_reason 的空结果**。"""
    text = (text or "").strip()
    if not text:
        return Extraction(degraded_reason="文本为空")
    if len(text) > MAX_INPUT_CHARS:
        return Extraction(
            degraded_reason=f"文本过长（{len(text)} 字），上限 {MAX_INPUT_CHARS} 字"
        )
    if client is None or not getattr(client, "configured", False):
        return Extraction(degraded_reason="未配置大模型 api_key，无法智能解析")

    from ddi.generate.llm_client import LLMUnavailable

    user = build_extract_user_prompt(text)
    payload = None
    resp = None
    # 解析失败重试一次：实测模型偶发在 JSON 外裹一层说明文字或输出不稳定的
    # 围栏格式，同 prompt 立刻重来一次基本就能拿到干净 JSON。只重试解析失败 ——
    # 拒答/不可用重试没有意义。
    for attempt in (1, 2):
        try:
            payload, resp = client.complete_json(
                EXTRACT_SYSTEM_PROMPT,
                user,
                thinking=False,          # 抽取是机械活，思考模式只拖延迟
                temperature=0,
                max_tokens=EXTRACT_MAX_TOKENS,
            )
        except LLMUnavailable as e:
            return Extraction(degraded_reason=f"模型不可用：{e}")
        except Exception as e:  # noqa: BLE001 — 红线：抽取绝不把异常抛给 HTTP 层
            log.exception("结构化抽取调用失败")
            return Extraction(degraded_reason=f"模型调用失败：{e}")

        if getattr(resp, "refused", False):
            return Extraction(degraded_reason="模型拒答（内容被安全策略拦截）")
        if payload is not None:
            break
        if attempt == 1:
            raw = getattr(resp, "text", "") or ""
            log.warning(
                "抽取输出第 1 次无法解析（%d 字，stop=%s），重试：%s",
                len(raw), getattr(resp, "raw_stop_reason", None), raw[:200],
            )

    if payload is None:
        # 含顶层数组、纯文本、半截 JSON —— extract_json 尽力 + 一次重试仍失败
        raw = getattr(resp, "text", "") or ""
        log.warning(
            "抽取输出重试后仍无法解析（%d 字，stop=%s）：%s",
            len(raw), getattr(resp, "raw_stop_reason", None), raw[:300],
        )
        return Extraction(degraded_reason="模型输出无法解析为 JSON")

    raw_list = payload.get("candidates")
    if not isinstance(raw_list, list):
        return Extraction(
            degraded_reason="模型输出结构不合规：缺少 candidates 数组"
        )

    out = Extraction(used_llm=True, model=getattr(resp, "model", None))
    if len(raw_list) > MAX_CANDIDATES:
        for extra in raw_list[MAX_CANDIDATES:]:
            out.skipped.append({"raw_span": None,
                                "reason": f"超出单次上限 {MAX_CANDIDATES} 条"})
        raw_list = raw_list[:MAX_CANDIDATES]

    for raw in raw_list:
        draft, why = normalize_candidate(raw)
        if draft is None:
            out.skipped.append({
                "raw_span": str((raw or {}).get("raw_span") or "")[:RAW_SPAN_MAX]
                if isinstance(raw, dict) else None,
                "reason": why or "无法归一化",
            })
        else:
            out.candidates.append(draft)
    return out
