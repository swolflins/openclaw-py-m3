"""单元测试:工具注册和 AgentLoop 基础流程(用 fake provider)。

Phase 3 适配:AgentLoop 现在用 ScopedMemory;接受新参数(system_prompt/.../max_tool_iterations/.../history_window/.../recall_top_k)。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional


from openclaw.agent.loop import AgentLoop
from openclaw.llm.base import BaseLLMProvider, ChatMessage, LLMResult, ToolCall
from openclaw.memory.scoped import ScopedMemory
from openclaw.memory.short_term import ShortTermStore
from openclaw.tools.builtin import register_builtin_tools
from openclaw.tools.registry import ToolRegistry


class FakeProvider(BaseLLMProvider):
    def __init__(self, script: list[LLMResult]) -> None:
        super().__init__(model="fake")
        self.script = list(script)
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
        if not self.script:
            return LLMResult(content="(script empty)")
        return self.script.pop(0)


def _make_loop(llm: BaseLLMProvider, tools: ToolRegistry, memory_dir: Path) -> AgentLoop:
    scoped = ScopedMemory(short_term=ShortTermStore(memory_dir), long_term=None, soul=None)
    return AgentLoop(
        llm=llm,
        tools=tools,
        memory=scoped,
        system_prompt="test",
        max_tool_iterations=3,
        history_window=10,
        recall_top_k=0,
    )


def test_calculator_tool_directly():
    tools = ToolRegistry()
    register_builtin_tools(tools)
    t = tools.get("calculator")
    out = asyncio.run(t(expression="(1+2)*3"))
    assert out == "9"


def test_agent_loop_terminates_on_text_response(tmp_path: Path):
    llm = FakeProvider([LLMResult(content="你好!")])
    loop = _make_loop(llm, ToolRegistry(), tmp_path)
    r = asyncio.run(loop.handle("s1", "hi"))
    assert r.content == "你好!"
    assert r.iterations == 1


def test_agent_loop_executes_tool_then_returns_text(tmp_path: Path):
    tools = ToolRegistry()
    register_builtin_tools(tools)
    llm = FakeProvider([
        LLMResult(content="", tool_calls=[ToolCall(id="c1", name="calculator", arguments={"expression": "2+2"})]),
        LLMResult(content="结果是 4"),
    ])
    loop = _make_loop(llm, tools, tmp_path)
    r = asyncio.run(loop.handle("s2", "算 2+2"))
    assert r.content == "结果是 4"
    assert r.iterations == 2
    assert llm.calls == 2

    rows = loop.memory.short.recent("s2", k=10)
    assert [m.role for m in rows] == ["user", "assistant"]


def test_agent_loop_handles_unknown_tool_gracefully(tmp_path: Path):
    tools = ToolRegistry()
    register_builtin_tools(tools)
    llm = FakeProvider([
        LLMResult(content="", tool_calls=[ToolCall(id="c1", name="no_such_tool", arguments={})]),
        LLMResult(content="好的,我了解了"),
    ])
    loop = _make_loop(llm, tools, tmp_path)
    r = asyncio.run(loop.handle("s3", "test"))
    assert r.content == "好的,我了解了"
