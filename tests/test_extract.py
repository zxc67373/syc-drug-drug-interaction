"""智能导入 · 结构化抽取单测（不走 HTTP）。

覆盖三块：prompt 契约、候选归一化（软校验）、抽取降级路径。
硬校验（confirm 前的 _validate_draft_fields）在 test_admin_import.py 里测。
"""

from __future__ import annotations

from test_api import FakeLLM

from ddi.generate.extract import (
    EXTRACT_OUTPUT_SCHEMA,
    EXTRACT_SYSTEM_PROMPT,
    MAX_INPUT_CHARS,
    build_extract_user_prompt,
    extract_candidates,
    normalize_candidate,
)


def _rule_raw(**over) -> dict:
    base = {
        "kind": "ddi_rule",
        "raw_span": "华法林与阿司匹林合用可增加出血风险",
        "fields": {
            "ing_a_name": "华法林",
            "ing_b_name": "阿司匹林",
            "severity": "caution",
            "mechanism": "阿司匹林抑制血小板聚集，叠加华法林抗凝",
            "consequence": "出血风险增加",
            "suggestion": "合用时加强 INR 监测",
            "evidence_level": "B",
            "sources": [{
                "title": "华法林钠片说明书",
                "source_type": "说明书",
                "year": "2019",
                "excerpt": "与阿司匹林合用增加出血风险",
            }],
        },
        "warnings": [],
    }
    base["fields"].update(over.pop("fields", {}))
    base.update(over)
    return base


class TestPrompts:
    def test_schema_requires_candidates_wrapper(self):
        # extract_json 只认顶层 dict —— schema 必须明令 candidates 包装
        assert '"candidates"' in EXTRACT_OUTPUT_SCHEMA
        assert "禁止输出顶层数组" in EXTRACT_SYSTEM_PROMPT
        assert "禁止输出顶层数组" in build_extract_user_prompt("x")

    def test_schema_covers_rule_fields(self):
        for key in ("ing_a_name", "ing_b_name", "severity", "mechanism",
                    "evidence_level", "sources", "raw_span"):
            assert key in EXTRACT_OUTPUT_SCHEMA

    def test_schema_covers_drug_fields(self):
        assert '"drug"' in EXTRACT_OUTPUT_SCHEMA
        for key in ("name_cn", "dosage_form", "trade_names", "ingredients"):
            assert key in EXTRACT_OUTPUT_SCHEMA
        assert '"drug"' in EXTRACT_SYSTEM_PROMPT

    def test_severity_enums_listed(self):
        assert "contraindicated" in EXTRACT_SYSTEM_PROMPT
        assert "caution" in EXTRACT_SYSTEM_PROMPT
        assert "monitor" in EXTRACT_SYSTEM_PROMPT

    def test_user_prompt_embeds_text(self):
        prompt = build_extract_user_prompt("阿莫西林与丙磺舒合用")
        assert "阿莫西林与丙磺舒合用" in prompt


class TestNormalizeCandidate:
    def test_rule_happy_path(self):
        draft, why = normalize_candidate(_rule_raw())
        assert why is None
        assert draft["kind"] == "ddi_rule"
        assert draft["fields"]["severity"] == "caution"
        # year "2019"（字符串）应被收敛成 int
        assert draft["fields"]["sources"][0]["year"] == 2019
        assert draft["raw_span"]

    def test_unknown_kind_skipped(self):
        draft, why = normalize_candidate({"kind": "population_rule", "fields": {}})
        assert draft is None
        assert "不支持的类型" in why

    def test_missing_ing_name_skipped(self):
        raw = _rule_raw()
        del raw["fields"]["ing_b_name"]
        draft, why = normalize_candidate(raw)
        assert draft is None
        assert "ing_b_name" in why

    def test_same_names_skipped(self):
        draft, why = normalize_candidate(_rule_raw(fields={"ing_b_name": "华法林"}))
        assert draft is None
        assert "相同" in why

    def test_bad_severity_staged_with_warning(self):
        # 软校验：枚举错了不拦，加 warning 留给人工改
        draft, why = normalize_candidate(_rule_raw(fields={"severity": "danger"}))
        assert why is None
        assert any("severity" in w for w in draft["warnings"])

    def test_year_coerced_to_int(self):
        draft, _ = normalize_candidate(_rule_raw())
        assert isinstance(draft["fields"]["sources"][0]["year"], int)

    def test_bad_year_nulled_with_warning(self):
        raw = _rule_raw()
        raw["fields"]["sources"][0]["year"] = "n.d."
        draft, _ = normalize_candidate(raw)
        assert draft["fields"]["sources"][0]["year"] is None
        assert draft["warnings"]

    def test_long_text_truncated_with_warning(self):
        draft, _ = normalize_candidate(
            _rule_raw(fields={"mechanism": "长" * 3000})
        )
        assert len(draft["fields"]["mechanism"]) == 2000
        assert any("截断" in w for w in draft["warnings"])

    def test_source_without_title_dropped(self):
        raw = _rule_raw()
        raw["fields"]["sources"].append({"publisher": "无题"})
        draft, _ = normalize_candidate(raw)
        assert len(draft["fields"]["sources"]) == 1
        assert draft["warnings"]

    def test_not_dict_skipped(self):
        draft, why = normalize_candidate(["不是对象"])
        assert draft is None
        assert why


def _drug_raw(**fields_over) -> dict:
    fields = {
        "name_cn": "ZZ提取药品",
        "dosage_form": "胶囊",
        "trade_names": ["ZZ商品"],
        "aliases": ["别名一", "别名二"],
        "is_otc": True,
        "is_tcm": False,
        "approval_no": "国药准字Z20260001",
        "ingredients": [
            {"name": "成分甲", "strength": "0.25g"},
            {"name": "成分乙"},
        ],
    }
    fields.update(fields_over)
    return {
        "kind": "drug",
        "raw_span": "ZZ提取药品说明书片段",
        "fields": fields,
        "warnings": [],
    }


class TestNormalizeDrug:
    def test_happy_path(self):
        draft, why = normalize_candidate(_drug_raw())
        assert why is None
        assert draft["kind"] == "drug"
        f = draft["fields"]
        assert f["name_cn"] == "ZZ提取药品"
        assert f["is_otc"] is True
        assert len(f["ingredients"]) == 2
        assert f["ingredients"][0]["strength"] == "0.25g"
        assert "strength" not in f["ingredients"][1]

    def test_missing_name_skipped(self):
        draft, why = normalize_candidate(_drug_raw(name_cn="  "))
        assert draft is None
        assert "name_cn" in why

    def test_no_ingredients_skipped(self):
        draft, why = normalize_candidate(_drug_raw(ingredients=[]))
        assert draft is None
        assert "成分" in why

    def test_bad_ingredient_dropped_with_warning(self):
        draft, _ = normalize_candidate(_drug_raw(
            ingredients=[{"name": "成分甲"}, {"strength": "无名"}]
        ))
        assert [i["name"] for i in draft["fields"]["ingredients"]] == ["成分甲"]
        assert draft["warnings"]

    def test_trade_names_string_split(self):
        draft, _ = normalize_candidate(_drug_raw(trade_names="甲药、乙药"))
        assert draft["fields"]["trade_names"] == ["甲药", "乙药"]

    def test_string_bool_coerced(self):
        draft, _ = normalize_candidate(_drug_raw(is_otc="true", is_tcm="是"))
        assert draft["fields"]["is_otc"] is True
        assert draft["fields"]["is_tcm"] is True


class TestDegradation:
    def test_unconfigured(self):
        out = extract_candidates("任意文本", client=FakeLLM(configured=False))
        assert out.used_llm is False
        assert out.candidates == []
        assert "未配置" in out.degraded_reason

    def test_unparseable_output(self):
        out = extract_candidates(
            "文本", client=FakeLLM(raw_text="完全不是 JSON 的散文")
        )
        assert out.degraded_reason and "JSON" in out.degraded_reason

    def test_top_level_array_rejected(self):
        # extract_json 只收 dict —— 顶层数组必须走降级而不是静默丢数据
        out = extract_candidates("文本", client=FakeLLM(raw_text="[1, 2, 3]"))
        assert out.degraded_reason
        assert out.candidates == []

    def test_wrong_shape(self):
        out = extract_candidates(
            "文本", client=FakeLLM(payload_obj={"items": []})
        )
        assert "candidates" in out.degraded_reason

    def test_empty_text(self):
        out = extract_candidates("   ", client=FakeLLM())
        assert out.degraded_reason
        assert out.used_llm is False

    def test_text_too_long(self):
        out = extract_candidates("x" * (MAX_INPUT_CHARS + 1), client=FakeLLM())
        assert "过长" in out.degraded_reason

    def test_happy_path(self):
        client = FakeLLM(payload_obj={"candidates": [
            _rule_raw(),
            {"kind": "drug", "fields": {"name_cn": "不该出现"}},  # 被跳过
        ]})
        out = extract_candidates("华法林与阿司匹林……", client=client)
        assert out.degraded_reason is None
        assert out.used_llm is True
        assert len(out.candidates) == 1
        assert len(out.skipped) == 1
        assert out.model == "fake-model"

    def test_zero_candidates_is_success(self):
        # 模型说"没抽到"是合法结果，不是降级
        out = extract_candidates("文本", client=FakeLLM(payload_obj={"candidates": []}))
        assert out.degraded_reason is None
        assert out.candidates == []
        assert out.used_llm is True
