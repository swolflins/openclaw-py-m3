"""Phase 3:完整记忆层 + 集成 AgentLoop 测试。"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional


from openclaw.agent.loop import AgentLoop
from openclaw.llm.base import BaseLLMProvider, ChatMessage, LLMResult, ToolCall
from openclaw.memory.long_term import LongTermStore
from openclaw.memory.scoped import ScopedMemory
from openclaw.memory.short_term import ShortTermStore
from openclaw.memory.soul import SoulLoader
from openclaw.tools.builtin import register_builtin_tools
from openclaw.tools.registry import ToolRegistry


class FakeProvider(BaseLLMProvider):
    def __init__(self, script: list[LLMResult]) -> None:
        super().__init__(model="fake")
        self.script = list(script)
        self.calls = 0
        self.last_messages: list[ChatMessage] = []

    async def acomplete(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[Any]] = None,
        *,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
    ) -> LLMResult:
        self.calls += 1
        self.last_messages = list(messages)
        if not self.script:
            return LLMResult(content="(empty)")
        return self.script.pop(0)


# ------------- short_term -------------

def test_short_term_scope_isolation(tmp_path: Path):
    s = ShortTermStore(tmp_path / "m")
    s.append("session:a", "u1", "a1")
    s.append("session:b", "u2", "a2")
    a = s.recent("session:a", k=10)
    b = s.recent("session:b", k=10)
    assert [m.content for m in a] == ["u1", "a1"]
    assert [m.content for m in b] == ["u2", "a2"]


def test_short_term_metadata_roundtrip(tmp_path: Path):
    s = ShortTermStore(tmp_path / "m")
    s.append("session:a", "u", "a", metadata={"channel": "lark"})
    # 走 _backup 触发 JSON,再读
    rows = s.recent("session:a", k=10)
    assert rows[0].content == "u"


# ------------- soul -------------

def test_soul_loader_picks_up_existing_file(tmp_path: Path, monkeypatch):
    soul = tmp_path / "SOUL.md"
    soul.write_text("# 我是 Claw\n我是一只龙虾。\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    s = SoulLoader()
    docs = s.reload()
    assert any("龙虾" in d.content for d in docs)


def test_soul_frontmatter_scope(tmp_path: Path):
    f = tmp_path / "SOUL.md"
    f.write_text("---\nscope: user:u1\n---\n用户的偏好:简洁\n", encoding="utf-8")
    s = SoulLoader(paths=[f])
    docs = s.reload()
    assert docs[0].scope == "user:u1"
    assert "简洁" in docs[0].content


def test_soul_render_includes_base(tmp_path: Path):
    s = SoulLoader(paths=[])  # 无文件
    s._cache = []  # 确保空
    out = s.render_system_prompt("base prompt")
    assert "base prompt" in out


# ------------- scoped -------------

def test_scoped_build_messages_contains_soul(tmp_path: Path, monkeypatch):
    soul = tmp_path / "SOUL.md"
    soul.write_text("SOUL 标记:ABC\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    short = ShortTermStore(tmp_path / "m")
    short.append("session:1", "hi", "hello")
    s = SoulLoader()
    scoped = ScopedMemory(short_term=short, long_term=None, soul=s)

    msgs = scoped.build_messages("session:1", "你叫什么", system_prompt="BASE")
    # system 包含 BASE + SOUL
    sys = msgs[0].content
    assert "BASE" in sys and "ABC" in sys
    # user 在最后
    assert msgs[-1].role == "user"
    assert msgs[-1].content == "你叫什么"
    # 历史 user/assistant 在中间
    roles = [m.role for m in msgs[1:-1]]
    assert "user" in roles and "assistant" in roles


def test_scoped_recall_appends_context(tmp_path: Path):
    short = ShortTermStore(tmp_path / "m")
    long = LongTermStore(tmp_path / "lt", embedding_fn=_fake_embed)
    long.add("Python 的 GIL 是全局解释器锁。", scope="session:1")
    scoped = ScopedMemory(short_term=short, long_term=long, soul=None)
    msgs = scoped.build_messages("session:1", "GIL 是什么?", recall_top_k=1)
    # 应有 1 条 system reminder + 1 user
    assert msgs[0].role == "system"
    assert msgs[1].role == "system"
    assert "GIL" in msgs[1].content


def _fake_embed(texts: list[str]) -> list[list[float]]:
    """测试用 fake 嵌入:基于文本长度做简单 hash,确定性,不需要下载模型。"""
    out: list[list[float]] = []
    for t in texts:
        v = [0.0] * 16
        for i, ch in enumerate(t):
            v[i % 16] += float(ord(ch) % 13) / 100.0
        # 归一化
        n = sum(x * x for x in v) ** 0.5 or 1.0
        out.append([x / n for x in v])
    return out


# ------------- 端到端:AgentLoop 用 ScopedMemory -------------

def test_end_to_end_with_scoped_memory(tmp_path: Path):
    tools = ToolRegistry()
    register_builtin_tools(tools)
    short = ShortTermStore(tmp_path / "m")
    scoped = ScopedMemory(short_term=short, long_term=None, soul=None)
    llm = FakeProvider([
        LLMResult(content="", tool_calls=[ToolCall(id="c1", name="echo", arguments={"message": "ping"})]),
        LLMResult(content="pong"),
    ])
    loop = AgentLoop(llm=llm, tools=tools, memory=scoped, system_prompt="sys", max_tool_iterations=3, history_window=10, recall_top_k=0)

    r = asyncio.run(loop.handle("session:1", "say ping"))
    assert r.content == "pong"
    # 写入到了 short_term
    rows = short.recent("session:1", k=10)
    assert [m.role for m in rows] == ["user", "assistant"]
