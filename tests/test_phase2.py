"""Phase 2:多 Provider + Router 测试(不依赖真实网络)。"""
from __future__ import annotations

from typing import Any, Optional

import pytest

from openclaw.core.config import ProviderConfig
from openclaw.core.errors import ProviderError
from openclaw.llm.base import BaseLLMProvider, ChatMessage, LLMResult
from openclaw.providers.factory import ProviderFactory
from openclaw.providers.router import ProviderRouter


class FakeProvider(BaseLLMProvider):
    """可控返回 / 抛错 的假 provider。"""

    def __init__(
        self,
        name: str = "fake",
        response: LLMResult | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        super().__init__(model=f"{name}-model")
        self._name = name
        self._response = response or LLMResult(content=f"hello from {name}")
        self._raise = raise_exc
        self.calls = 0

    async def acomplete(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[Any]] = None,
        *,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
    ) -> LLMResult:
        self.calls += 1
        if self._raise:
            raise self._raise
        return self._response


# --------- factory ---------

def test_factory_known_providers():
    f = ProviderFactory()
    for name in ("openai_compat", "anthropic", "gemini", "ollama"):
        assert name in f.names()


def test_factory_build_openai_compat():
    f = ProviderFactory()
    p = f.build(ProviderConfig(name="openai_compat", model="x", api_key="k", base_url="http://x"))
    assert p.model == "x"


def test_factory_unknown_raises():
    f = ProviderFactory()
    with pytest.raises(ProviderError):
        f.build(ProviderConfig(name="nope", model="x"))


# --------- router ---------

@pytest.mark.asyncio
async def test_router_uses_primary_when_healthy():
    p1 = FakeProvider("p1", LLMResult(content="from p1"))
    p2 = FakeProvider("p2", LLMResult(content="from p2"))
    r = ProviderRouter(p1, [p2])
    out = await r.acomplete([ChatMessage(role="user", content="hi")])
    assert out.content == "from p1"
    assert p1.calls == 1
    assert p2.calls == 0


@pytest.mark.asyncio
async def test_router_falls_back_on_failure():
    p1 = FakeProvider("p1", raise_exc=ProviderError("primary down"))
    p2 = FakeProvider("p2", LLMResult(content="fallback ok"))
    r = ProviderRouter(p1, [p2])
    out = await r.acomplete([ChatMessage(role="user", content="hi")])
    assert out.content == "fallback ok"
    assert p1.calls == 1
    assert p2.calls == 1


@pytest.mark.asyncio
async def test_router_raises_when_all_fail():
    p1 = FakeProvider("p1", raise_exc=ProviderError("a"))
    p2 = FakeProvider("p2", raise_exc=ProviderError("b"))
    r = ProviderRouter(p1, [p2])
    with pytest.raises(ProviderError):
        await r.acomplete([ChatMessage(role="user", content="hi")])


@pytest.mark.asyncio
async def test_router_round_robin():
    p1 = FakeProvider("p1", LLMResult(content="a"))
    p2 = FakeProvider("p2", LLMResult(content="b"))
    r = ProviderRouter(p1, [p2], strategy="round_robin")
    outs = []
    for _ in range(3):
        outs.append((await r.acomplete([ChatMessage(role="user", content="x")])).content)
    assert outs == ["a", "b", "a"] or outs == ["b", "a", "b"]


# --------- OpenAI 兼容:parse 端到端 ---------

@pytest.mark.asyncio
async def test_openai_compat_parses_response(monkeypatch):
    """用 monkeypatch httpx 客户端,验证请求/解析闭环。"""
    from openclaw.providers import openai_compat as oc

    class _FakeResp:
        status_code = 200
        def __init__(self, data):
            self._data = data
            self.text = ""
        def json(self):
            return self._data
        def raise_for_status(self):
            return None

    class _FakeClient:
        def __init__(self, *a, **k):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, json):
            return _FakeResp({
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "ok",
                        "tool_calls": [{
                            "id": "call_xyz",
                            "type": "function",
                            "function": {"name": "echo", "arguments": '{"message":"hi"}'},
                        }],
                    }
                }]
            })
        async def aclose(self):
            return None

    monkeypatch.setattr(oc.httpx, "AsyncClient", _FakeClient)
    p = oc.OpenAICompatProvider(api_key="k", base_url="http://x", model="m")
    r = await p.acomplete([ChatMessage(role="user", content="ping")])
    assert r.content == "ok"
    assert r.tool_calls[0].name == "echo"
    assert r.tool_calls[0].arguments == {"message": "hi"}
