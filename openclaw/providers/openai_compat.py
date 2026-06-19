"""OpenAI 兼容 Provider。

通过 httpx 直接调用 /chat/completions,适用于:
- OpenAI
- DeepSeek
- Moonshot / Kimi
- Ollama (http://localhost:11434/v1)
- OneAPI / NewAPI 等自部署网关

这样不依赖官方 openai SDK,体积更小、依赖更少。
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Optional

import httpx

from openclaw.core.errors import ProviderError
from openclaw.core.logging import get_logger
from openclaw.llm.base import (
    BaseLLMProvider,
    ChatMessage,
    LLMResult,
    ToolCall,
    ToolSpec,
)

logger = get_logger(__name__)


class OpenAICompatProvider(BaseLLMProvider):
    """通用 OpenAI Chat Completions 客户端。"""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com/v1",
        model: str = "deepseek-chat",
        timeout: float = 60.0,
        **kwargs: Any,
    ) -> None:
        super().__init__(model=model, **kwargs)
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None
        # 记录 client 创建时所在的 event loop id,跨 asyncio.run() 跨边界时重建
        self._client_loop_id: Optional[int] = None

    async def _get_client(self) -> httpx.AsyncClient:
        current_loop_id = id(asyncio.get_running_loop())
        if (
            self._client is not None
            and self._client_loop_id == current_loop_id
            and not self._client.is_closed
        ):
            return self._client
        # 旧 loop 已销毁,或从未创建 → 重建
        if self._client is not None and not self._client.is_closed:
            try:
                await self._client.aclose()
            except Exception:
                pass
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        self._client_loop_id = current_loop_id
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def acomplete(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[ToolSpec]] = None,
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> LLMResult:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [_msg_to_payload(m) for m in messages],
            "temperature": temperature,
            "stream": False,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = [t.to_openai_tool() for t in tools]
            payload["tool_choice"] = "auto"

        client = await self._get_client()
        try:
            resp = await client.post("/chat/completions", json=payload)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            body = e.response.text if e.response else "<no body>"
            raise ProviderError(
                f"LLM 调用失败: HTTP {e.response.status_code if e.response else '?'} - {body[:500]}"
            ) from e
        except httpx.HTTPError as e:
            raise ProviderError(f"LLM 网络错误: {e!r}") from e

        return _parse_response(data)

    # 给 router 提供重试入口
    acomplete_with_retry = acomplete


# --------------------- 内部辅助函数 ---------------------

def _msg_to_payload(m: ChatMessage) -> dict[str, Any]:
    """ChatMessage -> OpenAI 协议 dict。

    工具消息: role=tool, 必传 tool_call_id, content 是工具返回的字符串。
    带工具调用的助手消息: content 可为空字符串, 必传 tool_calls。
    """
    out: dict[str, Any] = {"role": m.role}

    if m.role == "tool":
        out["content"] = m.content or ""
        if m.tool_call_id:
            out["tool_call_id"] = m.tool_call_id
        return out

    out["content"] = m.content or ""

    if m.tool_calls:
        out["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": tc.arguments
                    if isinstance(tc.arguments, str)
                    else json.dumps(tc.arguments, ensure_ascii=False),
                },
            }
            for tc in m.tool_calls
        ]
        # 兼容某些不允许 assistant content 为空的网关
        if not out["content"]:
            out["content"] = None  # type: ignore[assignment]

    return out


def _parse_response(data: dict[str, Any]) -> LLMResult:
    """解析 OpenAI 响应 -> LLMResult。"""
    choices = data.get("choices") or []
    if not choices:
        return LLMResult(content="", raw=data)

    msg = choices[0].get("message") or {}
    content = msg.get("content") or ""
    tool_calls_raw = msg.get("tool_calls") or []

    tool_calls: list[ToolCall] = []
    for tc in tool_calls_raw:
        fn = tc.get("function") or {}
        args = fn.get("arguments") or "{}"
        if isinstance(args, str):
            try:
                args_dict = json.loads(args)
            except json.JSONDecodeError:
                args_dict = {"_raw": args}
        else:
            args_dict = args
        tool_calls.append(
            ToolCall(
                id=tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                name=fn.get("name", ""),
                arguments=args_dict,
            )
        )

    return LLMResult(content=content, tool_calls=tool_calls, raw=data)
