"""大模型供应商预设。

MiniMax / DeepSeek / 通义千问 三家都提供 **Anthropic 兼容端点**，
所以换供应商只是换 ``base_url`` + ``model``，客户端代码一行不用动。

注意：``base_url`` 不要带 ``/v1`` —— SDK 会自己在后面拼 ``/v1/messages``。
填成 ``.../anthropic/v1`` 会请求到 ``/anthropic/v1/v1/messages``，报 404。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    base_url: str
    model: str
    alt_models: tuple[str, ...] = ()
    note: str = ""


PROVIDERS: dict[str, Provider] = {
    "minimax": Provider(
        key="minimax",
        label="MiniMax",
        base_url="https://api.minimax.cn/anthropic",
        model="MiniMax-M3",
        note="本项目默认。已实测连通，支持 thinking 参数。",
    ),
    "deepseek": Provider(
        key="deepseek",
        label="DeepSeek",
        base_url="https://api.deepseek.com/anthropic",
        model="deepseek-v4-pro",
        alt_models=("deepseek-flash",),
        note=(
            "deepseek-v4-pro 质量更好，deepseek-flash 更快更便宜。"
            "该端点不支持 output_config，thinking 的 budget_tokens 会被忽略。"
        ),
    ),
    "qwen": Provider(
        key="qwen",
        label="通义千问（阿里云百炼）",
        base_url="https://dashscope.aliyuncs.com/apps/anthropic",
        model="qwen3.8-max",
        alt_models=("qwen3.5-plus",),
        note="国际站把 dashscope.aliyuncs.com 换成 dashscope-intl.aliyuncs.com。",
    ),
    "custom": Provider(
        key="custom",
        label="自定义（任何 Anthropic 兼容端点）",
        base_url="",
        model="",
        note="必须自己填 base_url 和 model，否则无法调用。",
    ),
}

DEFAULT_PROVIDER = "minimax"


class UnknownProvider(ValueError):
    """provider 名字拼错了。

    这里**必须报错而不是静默回退** —— 拼错 ``deepseek`` 却悄悄连到 MiniMax，
    会拿着一个能跑通的结果让人以为配置生效了。
    """


def resolve(provider_key: str, base_url: str = "", model: str = "") -> tuple[str, str]:
    """把 provider 名解析成 ``(base_url, model)``。

    显式填了 ``base_url`` / ``model`` 就用显式的，留空才取预设值 ——
    这样既能一行切换供应商，也能只覆盖其中一项。
    """
    key = (provider_key or DEFAULT_PROVIDER).strip().lower()
    preset = PROVIDERS.get(key)
    if preset is None:
        valid = " / ".join(PROVIDERS)
        raise UnknownProvider(
            f"未知的 provider: {provider_key!r}。可选：{valid}"
        )
    return (base_url.strip() or preset.base_url, model.strip() or preset.model)


def describe() -> str:
    """给 ``scripts/check_config.py`` 和报错信息用的一览表。"""
    lines = []
    for p in PROVIDERS.values():
        alts = f"  备选模型：{', '.join(p.alt_models)}" if p.alt_models else ""
        lines.append(f"  {p.key:<10} {p.label:<22} {p.base_url or '(需自填)'}")
        lines.append(f"  {'':<10} 模型：{p.model or '(需自填)'}{alts}")
        if p.note:
            lines.append(f"  {'':<10} {p.note}")
        lines.append("")
    return "\n".join(lines).rstrip()
