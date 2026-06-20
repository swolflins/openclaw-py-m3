"""Anthropic Claude Provider。

把 OpenAI 风格的 messages/tools 转换为 Anthropic 原生 API。
Anthropic 工具协议:
    tools=[{"name", "description", "input_schema"}]
    messages 里 tool_use / tool_result block
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any, Optional

from openclaw.core.errors import ProviderError
from openclaw.llm.base import (
    BaseLLMProvider,
    ChatMessage,
    LLMResult,
    ToolCall,
    ToolSpec,
)


class AnthropicProvider(BaseLLMProvider):
    def __init__(
        self,
        api_key: str,
        model: str = "claude-3-5-sonnet-20241022",
        base_url: Optional[str] = None,
        max_tokens: int = 4096,
    ) -> None:
        super().__init__(model=model)
        self.api_key = api_key
        self.base_url = base_url
        self.max_tokens = max_tokens
        try:
            from anthropic import AsyncAnthropic
        except ImportError as e:
            raise ProviderError("anthropic 包未安装,运行 `pip install anthropic`") from e

        self._client: Optional[AsyncAnthropic] = None
        self._client_loop_id: Optional[int] = None

    async def _get_client(self) -> Any:
        """RT-3:在每个 event loop 首次调用时懒建 client,避免跨 loop 复用。"""
        try:
            current_loop_id = id(asyncio.get_running_loop())
        except RuntimeError:
            current_loop_id = -1
        if (
            self._client is not None
            and self._client_loop_id == current_loop_id
        ):
            return self._client
        # 旧 client(若还活着)先关
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
        kwargs: dict[str, Any] = {"api_key": self.api_key}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        # 避免 import 时未装 anthropic SDK
        try:
            from anthropic import AsyncAnthropic
        except ImportError as e:
            raise ProviderError("anthropic 未安装,运行 `pip install anthropic`") from e
        self._client = AsyncAnthropic(**kwargs)
        self._client_loop_id = current_loop_id
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None

    async def acomplete(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[ToolSpec]] = None,
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> LLMResult:
        system_prompt, converted = _to_anthropic_messages(messages)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": converted,
            "max_tokens": max_tokens or self.max_tokens,
            "temperature": temperature,
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        if tools:
            kwargs["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters,
                }
                for t in tools
            ]

        try:
            client = await self._get_client()
            resp = await client.messages.create(**kwargs)
        except Exception as e:
            raise ProviderError(f"Anthropic 调用失败: {e!r}") from e

        return _from_anthropic_response(resp)


# --------- 内部转换 ---------

def _to_anthropic_messages(messages: list[ChatMessage]) -> tuple[str, list[dict[str, Any]]]:
    """OpenAI 风格 -> Anthropic 风格:拆出 system,合并 user/assistant 内的 tool blocks。"""
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []

    i = 0
    while i < len(messages):
        m = messages[i]
        if m.role == "system":
            system_parts.append(m.content or "")
            i += 1
            continue
        if m.role == "user":
            out.append({"role": "user", "content": m.content or ""})
            i += 1
            continue
        if m.role == "assistant":
            blocks: list[dict[str, Any]] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for tc in m.tool_calls:
                blocks.append({
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.name,
                    "input": tc.arguments or {},
                })
            out.append({"role": "assistant", "content": blocks})
            i += 1
            continue
        if m.role == "tool":
            # 把相邻的 tool 合并到一个 user 消息里
            blocks = []
            while i < len(messages) and messages[i].role == "tool":
                tm = messages[i]
                blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tm.tool_call_id or f"call_{uuid.uuid4().hex[:8]}",
                    "content": tm.content or "",
                })
                i += 1
            out.append({"role": "user", "content": blocks})
            continue
        i += 1

    return "\n\n".join(system_parts), out


def _from_anthropic_response(resp: Any) -> LLMResult:
    content_text = ""
    tool_calls: list[ToolCall] = []
    for block in (resp.content or []):
        btype = getattr(block, "type", None)
        if btype == "text":
            content_text += getattr(block, "text", "") or ""
        elif btype == "tool_use":
            tool_calls.append(
                ToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=block.input or {},
                )
            )
    return LLMResult(content=content_text, tool_calls=tool_calls, raw=resp)
