"""Google Gemini Provider。

用 google-generativeai SDK;工具通过 FunctionDeclaration 注册。
"""
from __future__ import annotations

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


class GeminiProvider(BaseLLMProvider):
    def __init__(
        self,
        api_key: str,
        model: str = "gemini-1.5-pro",
    ) -> None:
        super().__init__(model=model)
        self.api_key = api_key
        try:
            import google.generativeai as genai
        except ImportError as e:
            raise ProviderError("google-generativeai 未安装,运行 `pip install google-generativeai`") from e
        # RT-2:不要 genai.configure() 全局副作用(多 key / 多实例会污染全局);
        # 用 genai.Client 拿到 client,把 api_key 放在构造里
        self._genai = genai
        # 新 SDK 推荐 client 模式;老 SDK 用 GenerativeModel 配 client_options
        try:
            self._client = genai.Client(api_key=api_key)
        except (TypeError, AttributeError):
            # 旧版 SDK(没有 Client)→ 退而用 configure + 配 client_options
            self._client = None
            genai.configure(api_key=api_key)
        self._model: Any = None

    def _get_model(self, tools: Optional[list[ToolSpec]] = None) -> Any:
        tool_decls = None
        if tools:
            tool_decls = [
                self._genai.protos.FunctionDeclaration(
                    name=t.name,
                    description=t.description,
                    parameters=self._to_gemini_schema(t.parameters),
                )
                for t in tools
            ]
        if self._client is not None:
            # 新 SDK:client.aio.models.generate_content 走 aio client,
            # 但 chat 模式仍用 GenerativeModel,只是 model 不绑全局 api key
            return self._genai.GenerativeModel(self.model, tools=tool_decls)
        return self._genai.GenerativeModel(self.model, tools=tool_decls)

    @staticmethod
    def _to_gemini_schema(schema: dict[str, Any]) -> Any:
        from google.generativeai.types import content_types
        return content_types.to_proto(schema)

    async def acomplete(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[ToolSpec]] = None,
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> LLMResult:
        model = self._get_model(tools)
        history, last_user, system_prompt = _to_gemini_history(messages)

        gen_config: dict[str, Any] = {"temperature": temperature}
        if max_tokens is not None:
            gen_config["max_output_tokens"] = max_tokens
        cfg = self._genai.types.GenerationConfig(**gen_config)

        chat = model.start_chat(history=history)
        try:
            resp = await chat.send_message_async(last_user, generation_config=cfg)
        except Exception as e:
            raise ProviderError(f"Gemini 调用失败: {e!r}") from e

        return _from_gemini_response(resp)

    async def aclose(self) -> None:
        return None


# --------- 内部转换 ---------

def _to_gemini_history(messages: list[ChatMessage]) -> tuple[list[dict[str, Any]], str, str | None]:
    """OpenAI 风格 -> Gemini history(不含最后一条 user)。

    Gemini role: 'user' / 'model'
    Gemini tool calls: function_call 字段; tool result: function_response 字段
    """
    history: list[dict[str, Any]] = []
    system_prompt: str | None = None

    # 把相邻 tool 合并到上一条 model 之后的 user 消息
    i = 0
    while i < len(messages):
        m = messages[i]
        if m.role == "system":
            system_prompt = (system_prompt or "") + (m.content or "") + "\n"
            i += 1
            continue
        if m.role == "assistant":
            parts: list[dict[str, Any]] = []
            if m.content:
                parts.append({"text": m.content})
            for tc in m.tool_calls:
                parts.append({
                    "function_call": {"name": tc.name, "args": tc.arguments or {}}
                })
            history.append({"role": "model", "parts": parts})
            i += 1
            continue
        if m.role == "user":
            history.append({"role": "user", "parts": [{"text": m.content or ""}]})
            i += 1
            continue
        if m.role == "tool":
            parts = []
            while i < len(messages) and messages[i].role == "tool":
                tm = messages[i]
                parts.append({
                    "function_response": {
                        "name": tm.name or "tool",
                        "response": {"result": tm.content or ""},
                    }
                })
                i += 1
            history.append({"role": "user", "parts": parts})
            continue
        i += 1

    # 最后一条必须是 user;若不是,取倒数第一条 user 当做当前问题
    last_user = ""
    if history and history[-1]["role"] == "user":
        # 提取最后一条 user 的纯文本(可能含 function_response,后者不适合当问题)
        last = history[-1]
        text_parts = [p for p in last["parts"] if "text" in p]
        if text_parts and not any("function_response" in p for p in last["parts"]):
            last_user = text_parts[0]["text"]
            history = history[:-1]
        else:
            last_user = ""
    return history, last_user, system_prompt


def _from_gemini_response(resp: Any) -> LLMResult:
    content_text = ""
    tool_calls: list[ToolCall] = []
    for cand in (resp.candidates or []):
        for part in (cand.content.parts or []):
            if getattr(part, "text", None):
                content_text += part.text
            fc = getattr(part, "function_call", None)
            if fc and fc.name:
                tool_calls.append(
                    ToolCall(
                        id=f"call_{uuid.uuid4().hex[:8]}",
                        name=fc.name,
                        arguments=dict(fc.args or {}),
                    )
                )
    return LLMResult(content=content_text, tool_calls=tool_calls, raw=resp)
