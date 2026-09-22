"""大模型客户端。

MiniMax / DeepSeek / 通义千问 都提供 Anthropic 兼容端点，因此直接用官方
``anthropic`` SDK 并覆盖 ``base_url`` —— 不手写裸 HTTP。

换供应商改 ``config.yaml`` 里的 ``llm.provider`` 一行即可，本文件不用动。
预设表见 :mod:`ddi.providers`。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from ddi.config import settings

log = logging.getLogger(__name__)

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


@dataclass
class LLMResponse:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    refused: bool = False
    refusal_category: str | None = None
    raw_stop_reason: str | None = None


class LLMUnavailable(RuntimeError):
    """未配置 key、网络失败或供应商拒绝服务。调用方应回退到纯规则答案。"""


def extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出 JSON。

    模型可能包 ```json 围栏、加前后缀说明，或输出多个对象。
    按「围栏 → 整体 → 首个平衡花括号块」依次尝试。
    """
    if not text:
        return None

    candidates: list[str] = []

    fence = _JSON_FENCE_RE.search(text)
    if fence:
        candidates.append(fence.group(1).strip())
    candidates.append(text.strip())

    # 首个花括号平衡块
    start = text.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : i + 1])
                    break

    for cand in candidates:
        if not cand:
            continue
        try:
            parsed = json.loads(cand)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return None


class LLMClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        timeout: int | None = None,
        thinking: bool | None = None,
        temperature: float | None = None,
    ):
        self.api_key = api_key or settings.llm_api_key
        self.base_url = base_url or settings.llm_base_url
        self.model = model or settings.llm_model
        self.max_tokens = max_tokens or settings.llm_max_tokens
        self.timeout = timeout or settings.llm_timeout
        # thinking/temperature 允许显式传入（管理后台热替换用），
        # 不传时回退 settings —— 与 max_tokens 等字段同一套回退逻辑。
        # 注意：temperature 允许 None（"不传该参数"），所以用 is not None 判断。
        self.thinking = settings.llm_thinking if thinking is None else bool(thinking)
        self.temperature = settings.llm_temperature if temperature is None else temperature
        self._client = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key) and not self.api_key.startswith("sk-xxx")

    @property
    def provider_label(self) -> str:
        """给 /health 和日志看的可读名称，例如 "MiniMax"。"""
        return settings.llm_provider_label

    def _ensure(self):
        if self._client is None:
            if not self.configured:
                raise LLMUnavailable(
                    "未配置大模型 api_key —— 请在 config.yaml 的 llm.api_key 填入"
                    "（模板见 config.example.yaml）"
                )
            import anthropic

            self._client = anthropic.Anthropic(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=float(self.timeout),
                max_retries=2,
            )
        return self._client

    def complete(
        self,
        system: str,
        user: str,
        *,
        thinking: bool | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        """单轮补全。

        ``thinking`` 默认取配置项 ``llm.thinking``（默认关）。开启自适应思考能提升
        解释质量，但实测同一请求延迟从 2.6s 涨到 8.5s。

        第三方兼容端点若不认这个参数会返回 400，此处自动降级重试一次 ——
        兼容层实现不一致是常态，不该让调用方处理。
        """
        import anthropic

        client = self._ensure()
        thinking = self.thinking if thinking is None else thinking
        if temperature is None:
            temperature = self.temperature
        messages = [{"role": "user", "content": user}]

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": messages,
        }
        if thinking:
            kwargs["thinking"] = {"type": "adaptive"}
        if temperature is not None:
            kwargs["temperature"] = temperature

        try:
            resp = self._call(client, kwargs)
        except anthropic.BadRequestError as e:
            # 端点不支持 thinking/temperature —— 去掉后重试
            if thinking or temperature is not None:
                log.warning("端点拒绝参数（%s），降级重试", e)
                kwargs.pop("thinking", None)
                kwargs.pop("temperature", None)
                resp = self._call(client, kwargs)
            else:
                raise LLMUnavailable(f"请求被拒绝：{e}") from e
        except anthropic.APIConnectionError as e:
            raise LLMUnavailable(f"无法连接 {self.base_url}：{e}") from e
        except anthropic.APIStatusError as e:
            raise LLMUnavailable(f"接口错误 {e.status_code}：{e.message}") from e

        return resp

    def _call(self, client, kwargs: dict) -> LLMResponse:
        resp = client.messages.create(**kwargs)

        # 拒答检查必须在读 content 之前 —— refused 时 content 可能为空
        if getattr(resp, "stop_reason", None) == "refusal":
            details = getattr(resp, "stop_details", None)
            return LLMResponse(
                text="",
                model=getattr(resp, "model", self.model),
                refused=True,
                refusal_category=getattr(details, "category", None),
                raw_stop_reason="refusal",
            )

        # content 是块列表，取 text 块 —— 不能假设 content[0] 就是文本
        parts = [
            b.text for b in getattr(resp, "content", []) if getattr(b, "type", None) == "text"
        ]
        usage = getattr(resp, "usage", None)
        return LLMResponse(
            text="\n".join(parts).strip(),
            model=getattr(resp, "model", self.model),
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            raw_stop_reason=getattr(resp, "stop_reason", None),
        )

    def complete_json(
        self, system: str, user: str, *, thinking: bool | None = None
    ) -> tuple[dict[str, Any] | None, LLMResponse]:
        """要求模型输出 JSON，并解析。解析失败返回 (None, 原始响应)。"""
        resp = self.complete(system, user, thinking=thinking)
        if resp.refused:
            return None, resp
        return extract_json(resp.text), resp
