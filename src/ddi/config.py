"""集中配置。

**密钥只应出现在 ``config.yaml`` 或环境变量里，绝不写进代码。**
``config.yaml`` 已在 ``.gitignore`` 中，不会入库。

读取优先级（后者覆盖前者）：

1. 本文件里的默认值
2. ``config.yaml``        ← 日常改这个文件
3. 环境变量 ``DDI_*``     ← 部署时用，不必改文件（容器 / CI / 多环境）

配置文件的格式见 ``config.example.yaml``。``config.yaml`` 不存在时全部走默认值，
系统仍能起来（只是没有 key，大模型环节降级为纯规则答案）。
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ddi.providers import DEFAULT_PROVIDER
from ddi.providers import resolve as resolve_provider

# 项目根目录：src/ddi/config.py -> src/ddi -> src -> <root>
ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = ROOT / "config.yaml"
CONFIG_EXAMPLE = ROOT / "config.example.yaml"


DEFAULTS: dict[str, Any] = {
    "llm": {
        # ★ 换供应商只改这一行：minimax | deepseek | qwen | custom
        "provider": DEFAULT_PROVIDER,
        "api_key": "",
        "base_url": "",      # 留空 = 用 provider 预设
        "model": "",         # 留空 = 用 provider 预设
        "max_tokens": 2048,
        "timeout": 60,
        # 自适应思考能提升解释质量，但实测同一请求延迟从 2.6s 涨到 8.5s。
        # 本项目对延迟敏感（MVP 目标 <5s），默认关闭。
        "thinking": False,
        "temperature": None,
    },
    "embed": {
        # m3e     中文语义嵌入（推荐）。权重约 390MB，需 torch + transformers
        # bge-m3  多语言长文本。权重约 2.2GB，需额外装 FlagEmbedding
        # hash    纯 numpy 兜底，**只做字面匹配，不是语义检索**，仅用于跑通链路
        "backend": "hash",
        # m3e / bge-m3 的权重目录。相对路径按项目根解析。
        # 部署时把模型放这里，或用 scripts/fetch_model.py 从 ModelScope 拉。
        "model_path": "models/m3e-base",
    },
    "database": {
        "path": "data/build/ddi.db",
    },
    "api": {
        "port": 5000,
        "debug": False,
        # 前后端分域部署时填前端域名，例如 ["https://ddi.example.com"]
        # "*" 仅适用于本机开发 —— 生产必须写死来源
        "cors_origins": ["*"],
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并，``override`` 覆盖 ``base``。返回新字典，不改动入参。"""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config_file(path: Path | None = None) -> dict[str, Any]:
    """读 config.yaml 并与默认值合并。文件不存在或为空时返回纯默认值。"""
    path = path or CONFIG_FILE
    if not path.exists():
        return copy.deepcopy(DEFAULTS)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name} 的顶层必须是映射（key: value），实际是 {type(raw).__name__}")
    return _deep_merge(DEFAULTS, raw)


# ── 环境变量覆盖 ────────────────────────────────────────────
# (配置路径, 规范名, 兼容的旧名)
_ENV_SPEC: list[tuple[tuple[str, str], str, str | None]] = [
    (("llm", "provider"),    "DDI_LLM_PROVIDER",    None),
    (("llm", "api_key"),     "DDI_LLM_API_KEY",     "LLM_API_KEY"),
    (("llm", "base_url"),    "DDI_LLM_BASE_URL",    "LLM_BASE_URL"),
    (("llm", "model"),       "DDI_LLM_MODEL",       "LLM_MODEL"),
    (("llm", "max_tokens"),  "DDI_LLM_MAX_TOKENS",  "LLM_MAX_TOKENS"),
    (("llm", "timeout"),     "DDI_LLM_TIMEOUT",     "LLM_TIMEOUT"),
    (("llm", "thinking"),    "DDI_LLM_THINKING",    "LLM_THINKING"),
    (("llm", "temperature"), "DDI_LLM_TEMPERATURE", None),
    (("embed", "backend"),   "DDI_EMBED_BACKEND",   "EMBED_BACKEND"),
    (("embed", "model_path"), "DDI_EMBED_MODEL_PATH", None),
    (("database", "path"),   "DDI_DB_PATH",         None),
    (("api", "port"),        "DDI_API_PORT",        "API_PORT"),
    (("api", "debug"),       "DDI_API_DEBUG",       "API_DEBUG"),
    (("api", "cors_origins"), "DDI_API_CORS_ORIGINS", None),
]


def _env(*names: str | None) -> str | None:
    for name in names:
        if not name:
            continue
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def _as_bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(raw: str, fallback: int) -> int:
    try:
        return int(raw)
    except ValueError:
        return fallback


def _as_float_or_none(raw: str) -> float | None:
    try:
        return float(raw)
    except ValueError:
        return None


def apply_env_overrides(cfg: dict[str, Any]) -> dict[str, Any]:
    """把环境变量叠到配置上。返回值是新字典。"""
    out = copy.deepcopy(cfg)
    for (section, key), canonical, legacy in _ENV_SPEC:
        raw = _env(canonical, legacy)
        if raw is None:
            continue
        current = out.setdefault(section, {}).get(key)
        if isinstance(current, bool):
            out[section][key] = _as_bool(raw)
        elif isinstance(current, int):
            out[section][key] = _as_int(raw, current)
        elif key == "temperature":
            out[section][key] = _as_float_or_none(raw)
        elif key == "cors_origins":
            out[section][key] = [o.strip() for o in raw.split(",") if o.strip()]
        else:
            out[section][key] = raw
    return out


def _rel(path_str: str) -> Path:
    """相对路径按项目根解析 —— 从任何工作目录启动都指向同一个文件。"""
    p = Path(path_str).expanduser()
    return p if p.is_absolute() else (ROOT / p)


@dataclass(frozen=True)
class Settings:
    # ── 大模型 ────────────────────────────────────────
    llm_provider: str = DEFAULT_PROVIDER
    llm_api_key: str = ""
    # base_url 指向 Anthropic 兼容端点，SDK 会在其后拼 /v1/messages
    llm_base_url: str = ""
    llm_model: str = ""
    llm_max_tokens: int = 2048
    llm_timeout: int = 60
    llm_thinking: bool = False
    llm_temperature: float | None = None

    # ── 检索 ──────────────────────────────────────────
    embed_backend: str = "hash"
    embed_model_path: Path = ROOT / "models/m3e-base"

    # ── 数据 ──────────────────────────────────────────
    db_path: Path = ROOT / "data/build/ddi.db"
    seed_dir: Path = ROOT / "data" / "seed"
    build_dir: Path = ROOT / "data" / "build"

    # ── 服务 ──────────────────────────────────────────
    api_port: int = 5000
    api_debug: bool = False
    api_cors_origins: tuple[str, ...] = ("*",)
    web_dir: Path = ROOT / "web"

    # 记录配置从哪来，便于 /health 和排查
    config_file: Path | None = None
    config_source: str = "默认值"

    @property
    def llm_configured(self) -> bool:
        """没有 key 时系统仍须能跑 —— 只是降级为纯规则答案。"""
        return bool(self.llm_api_key) and not self.llm_api_key.startswith("sk-xxx")

    @property
    def llm_provider_label(self) -> str:
        from ddi.providers import PROVIDERS

        preset = PROVIDERS.get(self.llm_provider)
        return preset.label if preset else self.llm_provider


def build_settings(config_path: Path | None = None) -> Settings:
    """按 默认值 → config.yaml → 环境变量 的顺序装配配置。"""
    path = config_path or CONFIG_FILE
    cfg = apply_env_overrides(load_config_file(path))

    llm = cfg["llm"]
    base_url, model = resolve_provider(
        llm.get("provider", DEFAULT_PROVIDER),
        llm.get("base_url", ""),
        llm.get("model", ""),
    )

    if path.exists():
        source = f"{path.name}"
    else:
        source = "默认值（未找到 config.yaml）"
    if _env(*[c for _, c, _ in _ENV_SPEC]):
        source += " + 环境变量"

    return Settings(
        llm_provider=(llm.get("provider") or DEFAULT_PROVIDER).strip().lower(),
        llm_api_key=(llm.get("api_key") or "").strip(),
        llm_base_url=base_url,
        llm_model=model,
        llm_max_tokens=int(llm.get("max_tokens") or 2048),
        llm_timeout=int(llm.get("timeout") or 60),
        llm_thinking=bool(llm.get("thinking")),
        llm_temperature=llm.get("temperature"),
        embed_backend=(cfg["embed"].get("backend") or "hash").strip(),
        embed_model_path=_rel(cfg["embed"].get("model_path") or "models/m3e-base"),
        db_path=_rel(cfg["database"].get("path") or "data/build/ddi.db"),
        api_port=int(cfg["api"].get("port") or 5000),
        api_debug=bool(cfg["api"].get("debug")),
        api_cors_origins=tuple(cfg["api"].get("cors_origins") or ["*"]),
        config_file=path if path.exists() else None,
        config_source=source,
    )


settings = build_settings()
