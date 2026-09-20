"""配置层测试。

重点保护两件事：

1. **供应商名字拼错必须报错**，不能静默连回原供应商 —— 否则配置看起来"生效了"，
   实际跑的还是老端点，排查时会被误导很久。
2. **优先级顺序**：环境变量 > config.yaml > 默认值。
"""

from __future__ import annotations

import pytest
import yaml

from ddi.config import DEFAULTS, apply_env_overrides, build_settings, load_config_file
from ddi.providers import PROVIDERS, UnknownProvider, resolve


class TestProviderPresets:
    def test_every_preset_has_https_endpoint(self):
        """明文 http 会把密钥暴露在链路上。custom 除外（由用户自填）。"""
        for key, p in PROVIDERS.items():
            if key == "custom":
                continue
            assert p.base_url.startswith("https://"), f"{key} 不是 https"
            assert p.model, f"{key} 没有默认模型"

    def test_base_url_must_not_end_with_v1(self):
        """SDK 会自己拼 /v1/messages，预设里带了 /v1 就会请求到 /v1/v1/messages。"""
        for key, p in PROVIDERS.items():
            assert not p.base_url.rstrip("/").endswith("/v1"), f"{key} 的 base_url 多了 /v1"

    def test_resolve_uses_preset_when_blank(self):
        base_url, model = resolve("deepseek", "", "")
        assert base_url == "https://api.deepseek.com/anthropic"
        assert model == "deepseek-v4-pro"

    def test_explicit_values_override_preset(self):
        """允许只覆盖其中一项，另一项仍走预设。"""
        base_url, model = resolve("deepseek", "https://gateway.internal/anthropic", "")
        assert base_url == "https://gateway.internal/anthropic"
        assert model == "deepseek-v4-pro"   # 未覆盖，仍取预设

    def test_unknown_provider_raises_not_falls_back(self):
        with pytest.raises(UnknownProvider) as e:
            resolve("deepsek", "", "")      # 少了一个 e
        assert "deepseek" in str(e.value)   # 报错要提示正确拼法

    def test_provider_name_is_case_insensitive(self):
        assert resolve("DeepSeek", "", "") == resolve("deepseek", "", "")

    def test_all_three_vendors_ship_anthropic_endpoints(self):
        for key in ("minimax", "deepseek", "qwen"):
            assert PROVIDERS[key].base_url


class TestConfigFileLoading:
    def test_missing_file_yields_defaults(self, tmp_path):
        cfg = load_config_file(tmp_path / "nope.yaml")
        assert cfg == DEFAULTS

    def test_partial_file_merges_over_defaults(self, tmp_path):
        """只写想改的项，其余保持默认 —— 否则用户得抄一整份模板。"""
        f = tmp_path / "config.yaml"
        f.write_text("llm:\n  provider: deepseek\n", encoding="utf-8")
        cfg = load_config_file(f)
        assert cfg["llm"]["provider"] == "deepseek"
        assert cfg["llm"]["max_tokens"] == DEFAULTS["llm"]["max_tokens"]
        assert cfg["api"]["port"] == DEFAULTS["api"]["port"]

    def test_loading_does_not_mutate_defaults(self, tmp_path):
        """深拷贝没做对的话，第二次加载会带上第一次的改动。"""
        f = tmp_path / "config.yaml"
        f.write_text("llm:\n  provider: qwen\n", encoding="utf-8")
        load_config_file(f)
        assert DEFAULTS["llm"]["provider"] == "minimax"

    def test_non_mapping_toplevel_is_rejected(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text("- just\n- a\n- list\n", encoding="utf-8")
        with pytest.raises(ValueError):
            load_config_file(f)

    def test_roundtrip_through_yaml(self, tmp_path):
        f = tmp_path / "config.yaml"
        f.write_text(
            yaml.safe_dump({"llm": {"provider": "qwen", "api_key": "sk-test"}}, allow_unicode=True),
            encoding="utf-8",
        )
        cfg = load_config_file(f)
        assert cfg["llm"]["api_key"] == "sk-test"


class TestEnvOverride:
    def test_env_beats_file(self, tmp_path, monkeypatch):
        f = tmp_path / "config.yaml"
        f.write_text("llm:\n  provider: minimax\n", encoding="utf-8")
        monkeypatch.setenv("DDI_LLM_PROVIDER", "qwen")
        assert build_settings(f).llm_provider == "qwen"

    def test_legacy_env_names_still_work(self, tmp_path, monkeypatch):
        """老部署脚本用的是不带 DDI_ 前缀的名字，别把它们弄坏。"""
        monkeypatch.setenv("LLM_API_KEY", "sk-legacy-key")
        assert build_settings(tmp_path / "nope.yaml").llm_api_key == "sk-legacy-key"

    def test_canonical_name_wins_over_legacy(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "sk-legacy")
        monkeypatch.setenv("DDI_LLM_API_KEY", "sk-canonical")
        assert build_settings(tmp_path / "nope.yaml").llm_api_key == "sk-canonical"

    @pytest.mark.parametrize("raw,expected", [
        ("1", True), ("true", True), ("True", True), ("yes", True), ("on", True),
        ("0", False), ("false", False), ("no", False), ("", False),
    ])
    def test_bool_parsing(self, raw, expected, tmp_path, monkeypatch):
        """环境变量传进来永远是字符串，而 thinking 是布尔 —— 转换错了会静默走错分支。"""
        if raw == "":
            monkeypatch.delenv("DDI_LLM_THINKING", raising=False)
        else:
            monkeypatch.setenv("DDI_LLM_THINKING", raw)
        assert build_settings(tmp_path / "nope.yaml").llm_thinking is expected

    def test_cors_origins_parsed_from_csv(self, monkeypatch):
        monkeypatch.setenv("DDI_API_CORS_ORIGINS", "https://a.com, https://b.com")
        s = build_settings()
        assert s.api_cors_origins == ("https://a.com", "https://b.com")

    def test_blank_env_var_is_ignored(self, monkeypatch):
        """空字符串应当视为"没设"，否则 CI 里一个空变量就能把配置清掉。"""
        monkeypatch.setenv("DDI_LLM_PROVIDER", "   ")
        assert build_settings().llm_provider == "minimax"


class TestSettings:
    def test_missing_key_is_not_configured(self, tmp_path):
        s = build_settings(tmp_path / "nope.yaml")
        assert s.llm_api_key == ""
        assert s.llm_configured is False

    def test_placeholder_key_is_not_configured(self, tmp_path, monkeypatch):
        """模板里的 sk-xxx 占位符不算配置好 —— 否则会拿着假 key 去发请求。"""
        monkeypatch.setenv("DDI_LLM_API_KEY", "sk-xxxxxxxxxxxxxxxx")
        assert build_settings(tmp_path / "nope.yaml").llm_configured is False

    def test_real_looking_key_is_configured(self, tmp_path, monkeypatch):
        # 刻意不用真实密钥的前缀：那会污染"密钥是否泄漏"的全文检索审计
        monkeypatch.setenv("DDI_LLM_API_KEY", "sk-test-not-a-real-credential")
        assert build_settings(tmp_path / "nope.yaml").llm_configured is True

    def test_relative_db_path_resolves_against_project_root(self, tmp_path):
        """从任何工作目录启动都应指向同一个数据库文件。"""
        from ddi.config import ROOT

        s = build_settings(tmp_path / "nope.yaml")
        assert s.db_path.is_absolute()
        assert s.db_path == ROOT / "data/build/ddi.db"
