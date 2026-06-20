"""Phase 15 P1 测试:LLM Provider 协议层 mock 测试。

对应原版 SDK adapter 集成测试:
- Anthropic messages.create -> content / tool_use / 异常
- Gemini generate_content -> parts / function_call / safety block
- OpenAI-compat /chat/completions -> message / tool_calls / HTTP error

策略:
- OpenAI-compat:用 httpx.MockTransport 拦截请求
- Anthropic / Gemini:替换 provider 内部 client 对象,返回 mock response
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from openclaw.core.errors import ProviderError
from openclaw.llm.base import ChatMessage, ToolCall, ToolSpec
from openclaw.providers.openai_compat import OpenAICompatProvider


# =========================================================================
# 公共 helper
# =========================================================================

def _user_msg(text: str) -> ChatMessage:
    return ChatMessage(role="user", content=text)


def _tool_msg(tool_call_id: str, content: str, name: str = "t") -> ChatMessage:
    return ChatMessage(role="tool", content=content, tool_call_id=tool_call_id, name=name)


def _assistant_with_tool(name: str, args: dict[str, Any], tc_id: str = "call_1") -> ChatMessage:
    return ChatMessage(
        role="assistant",
        content="",
        tool_calls=[ToolCall(id=tc_id, name=name, arguments=args)],
    )


# =========================================================================
# 1. OpenAI-compat:httpx.MockTransport
# =========================================================================

class TestOpenAICompatProvider:
    @pytest.mark.asyncio
    async def test_text_response_parsed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "choices": [{
                    "message": {"role": "assistant", "content": "hello world"},
                }]
            })

        provider = OpenAICompatProvider(api_key="sk-test", base_url="https://mock.local")
        # 直接把内部 client 替换为 mock transport
        provider._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.local",
        )
        provider._client_loop_id = id(__import__("asyncio").get_event_loop())

        result = await provider.acomplete([_user_msg("hi")])
        assert result.content == "hello world"
        assert result.tool_calls == []

    @pytest.mark.asyncio
    async def test_tool_call_response_parsed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": "call_99",
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "arguments": json.dumps({"city": "Beijing"}),
                            },
                        }],
                    },
                }],
            })

        provider = OpenAICompatProvider(api_key="sk-test", base_url="https://mock.local")
        provider._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.local",
        )
        provider._client_loop_id = id(__import__("asyncio").get_event_loop())

        tools = [ToolSpec(
            name="get_weather",
            description="Get weather",
            parameters={"type": "object", "properties": {"city": {"type": "string"}}},
        )]
        result = await provider.acomplete([_user_msg("天气")], tools=tools)
        assert result.content == ""
        assert len(result.tool_calls) == 1
        tc = result.tool_calls[0]
        assert tc.id == "call_99"
        assert tc.name == "get_weather"
        assert tc.arguments == {"city": "Beijing"}

    @pytest.mark.asyncio
    async def test_http_error_raises_provider_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="Unauthorized")

        provider = OpenAICompatProvider(api_key="bad", base_url="https://mock.local")
        provider._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.local",
        )
        provider._client_loop_id = id(__import__("asyncio").get_event_loop())

        with pytest.raises(ProviderError) as exc:
            await provider.acomplete([_user_msg("hi")])
        assert "401" in str(exc.value)

    @pytest.mark.asyncio
    async def test_empty_choices(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": []})

        provider = OpenAICompatProvider(api_key="sk", base_url="https://mock.local")
        provider._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.local",
        )
        provider._client_loop_id = id(__import__("asyncio").get_event_loop())

        result = await provider.acomplete([_user_msg("hi")])
        assert result.content == ""
        assert result.tool_calls == []

    @pytest.mark.asyncio
    async def test_invalid_tool_args_json_uses_raw(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "choices": [{
                    "message": {
                        "tool_calls": [{
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "f",
                                "arguments": "{invalid json}",
                            },
                        }],
                    },
                }],
            })

        provider = OpenAICompatProvider(api_key="sk", base_url="https://mock.local")
        provider._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.local",
        )
        provider._client_loop_id = id(__import__("asyncio").get_event_loop())

        result = await provider.acomplete([_user_msg("x")])
        # 解析失败 → _raw fallback
        assert result.tool_calls[0].arguments == {"_raw": "{invalid json}"}

    @pytest.mark.asyncio
    async def test_request_payload_structure(self):
        """验证 provider 发送的 payload 结构正确。"""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["method"] = request.method
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

        provider = OpenAICompatProvider(api_key="sk-test", base_url="https://mock.local")
        provider._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://mock.local",
        )
        provider._client_loop_id = id(__import__("asyncio").get_event_loop())

        tools = [ToolSpec(name="f", description="d", parameters={"type": "object"})]
        await provider.acomplete(
            [_user_msg("hi")],
            tools=tools,
            temperature=0.5,
            max_tokens=100,
        )
        assert captured["method"] == "POST"
        assert captured["url"].endswith("/chat/completions")
        body = captured["body"]
        assert body["stream"] is False
        assert body["temperature"] == 0.5
        assert body["max_tokens"] == 100
        assert body["messages"][0]["role"] == "user"
        assert body["tools"][0]["function"]["name"] == "f"

    @pytest.mark.asyncio
    async def test_msg_to_payload_tool_call(self):
        from openclaw.providers.openai_compat import _msg_to_payload
        m = _assistant_with_tool("f", {"x": 1})
        p = _msg_to_payload(m)
        assert p["role"] == "assistant"
        assert p["content"] is None
        assert p["tool_calls"][0]["function"]["name"] == "f"
        assert json.loads(p["tool_calls"][0]["function"]["arguments"]) == {"x": 1}

    def test_msg_to_payload_tool_message(self):
        from openclaw.providers.openai_compat import _msg_to_payload
        m = _tool_msg("call_x", "result", name="get_weather")
        p = _msg_to_payload(m)
        assert p["role"] == "tool"
        assert p["tool_call_id"] == "call_x"
        assert p["content"] == "result"


# =========================================================================
# 2. Anthropic:mock client.messages.create
# =========================================================================

class TestAnthropicProviderConversion:
    """不真的连 API,只测内部转换函数。"""

    def test_to_anthropic_messages_basic(self):
        from openclaw.providers.anthropic import _to_anthropic_messages
        msgs = [
            ChatMessage(role="system", content="you are helpful"),
            ChatMessage(role="user", content="hi"),
            ChatMessage(role="assistant", content="hello!"),
        ]
        system, converted = _to_anthropic_messages(msgs)
        assert system == "you are helpful"
        assert len(converted) == 2
        assert converted[0] == {"role": "user", "content": "hi"}
        assert converted[1]["role"] == "assistant"
        assert converted[1]["content"][0] == {"type": "text", "text": "hello!"}

    def test_to_anthropic_messages_with_tool_calls(self):
        from openclaw.providers.anthropic import _to_anthropic_messages
        msgs = [
            _assistant_with_tool("get_weather", {"city": "BJ"}, tc_id="call_1"),
        ]
        system, converted = _to_anthropic_messages(msgs)
        assert system == ""
        assert len(converted) == 1
        blocks = converted[0]["content"]
        # 应该有 tool_use block
        tool_uses = [b for b in blocks if b["type"] == "tool_use"]
        assert len(tool_uses) == 1
        assert tool_uses[0]["name"] == "get_weather"
        assert tool_uses[0]["id"] == "call_1"
        assert tool_uses[0]["input"] == {"city": "BJ"}

    def test_to_anthropic_messages_with_tool_result(self):
        from openclaw.providers.anthropic import _to_anthropic_messages
        msgs = [
            _assistant_with_tool("f", {}, tc_id="call_1"),
            _tool_msg("call_1", "result_data", name="f"),
        ]
        system, converted = _to_anthropic_messages(msgs)
        # 第二个 message 应该是 user 角色,内容是 tool_result block
        assert len(converted) == 2
        assert converted[1]["role"] == "user"
        tool_results = [b for b in converted[1]["content"] if b["type"] == "tool_result"]
        assert len(tool_results) == 1
        assert tool_results[0]["tool_use_id"] == "call_1"
        assert tool_results[0]["content"] == "result_data"

    def test_from_anthropic_response_text(self):
        from openclaw.providers.anthropic import _from_anthropic_response

        class Block:
            def __init__(self, type_, **kw):
                self.type = type_
                for k, v in kw.items():
                    setattr(self, k, v)
        resp = MagicMock()
        resp.content = [Block("text", text="hello")]
        result = _from_anthropic_response(resp)
        assert result.content == "hello"
        assert result.tool_calls == []

    def test_from_anthropic_response_tool_use(self):
        from openclaw.providers.anthropic import _from_anthropic_response

        class Block:
            def __init__(self, type_, **kw):
                self.type = type_
                for k, v in kw.items():
                    setattr(self, k, v)
        resp = MagicMock()
        resp.content = [
            Block("text", text="Let me check"),
            Block("tool_use", id="c1", name="get_weather", input={"city": "BJ"}),
        ]
        result = _from_anthropic_response(resp)
        assert result.content == "Let me check"
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "get_weather"
        assert result.tool_calls[0].arguments == {"city": "BJ"}

    def test_from_anthropic_response_empty(self):
        from openclaw.providers.anthropic import _from_anthropic_response
        resp = MagicMock()
        resp.content = []
        result = _from_anthropic_response(resp)
        assert result.content == ""
        assert result.tool_calls == []


# =========================================================================
# 3. Gemini:mock model + chat
# =========================================================================

class TestGeminiProviderConversion:
    def test_to_gemini_history_basic(self):
        from openclaw.providers.gemini import _to_gemini_history
        msgs = [
            ChatMessage(role="system", content="be helpful"),
            ChatMessage(role="user", content="hi"),
        ]
        history, last_user, system_prompt = _to_gemini_history(msgs)
        # history 应移除最后一条 user
        assert last_user == "hi"
        assert history == []
        assert system_prompt and "be helpful" in system_prompt

    def test_to_gemini_history_assistant(self):
        from openclaw.providers.gemini import _to_gemini_history
        msgs = [
            ChatMessage(role="user", content="hi"),
            ChatMessage(role="assistant", content="hello!"),
            ChatMessage(role="user", content="how are you"),
        ]
        history, last_user, _ = _to_gemini_history(msgs)
        assert last_user == "how are you"
        # history 应该包含 user "hi" 和 assistant "hello!"
        assert len(history) == 2
        assert history[0] == {"role": "user", "parts": [{"text": "hi"}]}
        assert history[1]["role"] == "model"
        assert history[1]["parts"][0] == {"text": "hello!"}

    def test_to_gemini_history_with_tool_call(self):
        from openclaw.providers.gemini import _to_gemini_history
        msgs = [
            ChatMessage(role="user", content="weather?"),
            _assistant_with_tool("get_weather", {"city": "BJ"}, tc_id="call_1"),
            _tool_msg("call_1", "Sunny 25°C", name="get_weather"),
        ]
        history, last_user, _ = _to_gemini_history(msgs)
        # 工具结果应作为 user 消息的 function_response
        assert last_user == ""
        # 历史中应有 3 段
        assert len(history) == 3
        # 第二个是 model 段,含 function_call
        model_msg = history[1]
        assert model_msg["role"] == "model"
        fc_parts = [p for p in model_msg["parts"] if "function_call" in p]
        assert len(fc_parts) == 1
        assert fc_parts[0]["function_call"]["name"] == "get_weather"

    def test_from_gemini_response_text(self):
        from openclaw.providers.gemini import _from_gemini_response
        resp = MagicMock()
        part = MagicMock()
        part.text = "hi"
        part.function_call = None
        cand = MagicMock()
        cand.content.parts = [part]
        resp.candidates = [cand]
        result = _from_gemini_response(resp)
        assert result.content == "hi"
        assert result.tool_calls == []

    def test_from_gemini_response_function_call(self):
        from openclaw.providers.gemini import _from_gemini_response
        resp = MagicMock()
        text_part = MagicMock()
        text_part.text = "Let me check"
        text_part.function_call = None
        fc_part = MagicMock()
        fc_part.text = None
        fc_part.function_call = MagicMock()
        fc_part.function_call.name = "get_weather"
        fc_part.function_call.args = {"city": "BJ"}
        cand = MagicMock()
        cand.content.parts = [text_part, fc_part]
        resp.candidates = [cand]
        result = _from_gemini_response(resp)
        assert "Let me check" in result.content
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "get_weather"
        assert result.tool_calls[0].arguments == {"city": "BJ"}

    def test_from_gemini_response_empty_candidates(self):
        from openclaw.providers.gemini import _from_gemini_response
        resp = MagicMock()
        resp.candidates = []
        result = _from_gemini_response(resp)
        assert result.content == ""
        assert result.tool_calls == []


# =========================================================================
# 4. Provider 工厂
# =========================================================================

class TestProviderFactory:
    def test_factory_openai_compat(self):
        from openclaw.providers.factory import get_factory
        from openclaw.core.config import ProviderConfig
        cfg = ProviderConfig(
            name="openai_compat",
            model="m",
            api_key="sk",
            base_url="https://x.com",
        )
        p = get_factory().build(cfg)
        assert isinstance(p, OpenAICompatProvider)
        assert p.model == "m"

    def test_factory_unknown_raises(self):
        from openclaw.providers.factory import get_factory
        from openclaw.core.config import ProviderConfig
        cfg = ProviderConfig(name="nonexistent_provider_xyz", model="m", api_key="sk")
        with pytest.raises(ProviderError):
            get_factory().build(cfg)

    def test_factory_names_include_builtin(self):
        from openclaw.providers.factory import get_factory
        names = get_factory().names()
        assert "openai_compat" in names
        assert "anthropic" in names
        assert "gemini" in names
        assert "ollama" in names
