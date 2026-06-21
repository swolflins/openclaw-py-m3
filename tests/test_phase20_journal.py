"""Phase 20:Agent Journal — 自我反思与成长日志(OpenClaw #5 idea)。

覆盖:
- TemplateReflector 提取标签 / 评分 / 建议
- AgentJournal.record_session 落盘
- AgentJournal.reflect 追加反思
- AgentJournal.generate_soul_proposal 不修改 SOUL(dry-run)
- AgentJournal.weekly_report 聚合
- register_journal_tools 注入 list/read/weekly_report 3 个工具
- Agent.run 集成 journal — 每次 session 结束自动写
- LLMReflector 用 mock provider 调一次
- 错误不阻断主流程(journal 异常 → agent 仍能返回)
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest  # noqa: F401  # 保留以备 fixture


# ─────────────── 辅助 ───────────────

class FakeProvider:
    """最小 mock LLM — 用于 LLMReflector 测试。"""
    def __init__(self, response: str = "# LLM 反思\n\n做得不错。") -> None:
        self.response = response
        self.calls: list[list[Any]] = []

    async def acomplete(self, messages, tools=None, *, temperature=0.7, max_tokens=None):
        from openclaw.llm.base import LLMResult
        self.calls.append(messages)
        return LLMResult(content=self.response)


@dataclass
class FakeResponse:
    content: str
    iterations: int
    tool_calls: list
    session_id: str


def make_entry(tool_calls=None, iterations=1, content="hi", user="hello"):
    from openclaw.agent.journal import JournalEntry
    return JournalEntry(
        session_id="sess_test1",
        timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        user_message=user,
        final_content=content,
        iterations=iterations,
        tool_calls=tool_calls or [],
    )


# ─────────────── TemplateReflector ───────────────

def test_template_reflector_simple_qa():
    from openclaw.agent.journal import TemplateReflector
    e = make_entry(tool_calls=[], iterations=1)
    text = asyncio.run(TemplateReflector().reflect(e))  # H4: reflect 改为 async
    assert "# 反思" in text
    assert "simple_qa" in e.tags
    assert "工具调用次数: **0**" in text
    assert "✅ 简单任务一次性回答" in text


def test_template_reflector_complex_marks_deep_reasoning():
    from openclaw.agent.journal import TemplateReflector
    e = make_entry(tool_calls=[{"name": "x"}] * 7, iterations=6)
    asyncio.run(TemplateReflector().reflect(e))  # H4: reflect 改为 async
    assert "complex_workflow" in e.tags
    assert "deep_reasoning" in e.tags


def test_template_reflector_with_error():
    from openclaw.agent.journal import TemplateReflector
    e = make_entry(
        tool_calls=[{"name": "shell_exec", "result": "[tool error] something failed"}],
        iterations=2,
    )
    text = asyncio.run(TemplateReflector().reflect(e))  # H4: reflect 改为 async
    assert "had_errors" in e.tags
    assert "工具出错 1 次" in text
    assert "⚠️ 部分工具调用失败" in text
    # 改进建议应有"检查失败工具"
    assert "检查失败工具" in text


# ─────────────── LLMReflector ───────────────

def test_llm_reflector_uses_provider():
    from openclaw.agent.journal import LLMReflector
    prov = FakeProvider(response="# LLM 反思\n\n应该更简洁。")
    refl = LLMReflector(prov)
    e = make_entry()
    out = asyncio.run(refl.reflect(e))
    assert "应该更简洁" in out
    assert prov.calls, "LLM 被调过"


def test_llm_reflector_falls_back_on_failure():
    """LLM 失败 → 用 TemplateReflector 兜底,不抛。"""
    from openclaw.agent.journal import LLMReflector

    class BadProvider:
        async def acomplete(self, *args, **kwargs):
            raise RuntimeError("api down")

    refl = LLMReflector(BadProvider())
    e = make_entry(tool_calls=[], iterations=1)
    out = asyncio.run(refl.reflect(e))
    # 模板兜底
    assert "工具调用次数" in out
    assert "LLM 反思失败" in out


# ─────────────── AgentJournal 落盘 ───────────────

def test_journal_record_session_writes_file(tmp_path: Path):
    from openclaw.agent.journal import AgentJournal
    j = AgentJournal(root=tmp_path / "j")
    resp = FakeResponse(content="reply!", iterations=1, tool_calls=[], session_id="sess_x")
    e = j.record_session(
        session_id="sess_x",
        user_message="hello world",
        response=resp,
    )
    assert e.session_id == "sess_x"
    files = list(tmp_path.rglob("sess_*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "hello world" in text
    assert "reply!" in text
    assert files[0].parent.name == datetime.now(timezone.utc).strftime("%Y-%m-%d")


def test_journal_reflect_appends_to_file(tmp_path: Path):
    from openclaw.agent.journal import AgentJournal
    j = AgentJournal(root=tmp_path / "j")
    e = j.record_session(
        session_id="sess_x",
        user_message="hi",
        response=FakeResponse("yo", 1, [], "sess_x"),
    )
    asyncio.run(j.reflect(e))
    files = list(tmp_path.rglob("sess_*.md"))
    text = files[0].read_text(encoding="utf-8")
    assert "反思" in text  # 模板反思标题
    assert "simple_qa" in text  # tag 被写回


def test_journal_weekly_report_aggregates(tmp_path: Path):
    from openclaw.agent.journal import AgentJournal
    j = AgentJournal(root=tmp_path / "j")
    for i in range(3):
        e = j.record_session(
            session_id=f"sess_{i}",
            user_message=f"q{i}",
            response=FakeResponse(f"a{i}", 1 + i, [], f"sess_{i}"),
        )
        asyncio.run(j.reflect(e))
    out = j.weekly_report()
    text = out.read_text(encoding="utf-8")
    assert out.exists()
    assert "周报" in text
    assert "Session 总数" in text
    assert "3" in text  # 3 个 session


def test_journal_soul_proposal_dry_run(tmp_path: Path):
    """SOUL proposal 写入 _soul_proposals.md,**不**碰真实 SOUL 文件。"""
    from openclaw.agent.journal import AgentJournal
    j = AgentJournal(root=tmp_path / "j")
    e = j.record_session(
        session_id="sess_x",
        user_message="how to be better",
        response=FakeResponse("answer", 1, [{"name": "x"}], "sess_x"),
    )
    asyncio.run(j.reflect(e))
    prop_path = j.generate_soul_proposal(e)
    assert "proposals" in prop_path
    text = (tmp_path / "j" / "_soul_proposals.md").read_text(encoding="utf-8")
    assert "sess_x" in text
    # 没有覆盖 SOUL(没有 soul/ 目录被创建)
    assert not (tmp_path / "j" / "SOUL.md").exists()


def test_journal_list_entries_filters_by_date(tmp_path: Path):
    from openclaw.agent.journal import AgentJournal
    from datetime import timedelta
    j = AgentJournal(root=tmp_path / "j")
    for i in range(2):
        j.record_session(
            session_id=f"sess_{i}",
            user_message=f"q{i}",
            response=FakeResponse(f"a{i}", 1, [], f"sess_{i}"),
        )
    # since=now+1day → 0 个
    future = datetime.now(timezone.utc) + timedelta(days=1)
    assert len(j.list_entries(since=future)) == 0
    # since=now-1day → 2 个
    past = datetime.now(timezone.utc) - timedelta(days=1)
    assert len(j.list_entries(since=past)) == 2


# ─────────────── register_journal_tools ───────────────

def test_register_journal_tools_injects_3_tools(tmp_path: Path):
    from openclaw.agent.journal import AgentJournal, register_journal_tools
    from openclaw.tools.registry import ToolRegistry
    reg = ToolRegistry()
    j = AgentJournal(root=tmp_path / "j")
    # 先写一个 entry 供 list/read
    j.record_session(
        session_id="sess_1",
        user_message="hi",
        response=FakeResponse("yo", 1, [], "sess_1"),
    )
    register_journal_tools(reg, j)
    specs = {s.name for s in reg.specs()}
    assert "list_journal" in specs
    assert "read_journal" in specs
    assert "weekly_report" in specs


def test_journal_tool_read_blocks_path_escape(tmp_path: Path):
    """read_journal 必须限制在 journal root 内。"""
    from openclaw.agent.journal import AgentJournal, register_journal_tools
    from openclaw.tools.registry import ToolRegistry
    reg = ToolRegistry()
    j = AgentJournal(root=tmp_path / "j")
    register_journal_tools(reg, j)
    # 试图越界
    out = asyncio.run(reg.call("read_journal", {"path": "../../etc/passwd"}))
    assert "outside journal root" in out or "not found" in out


# ─────────────── Agent.run 集成 ───────────────

def test_agent_run_invokes_tool_via_registry(tmp_path: Path):
    """Phase 20 demo 发现并修:Agent.run 调 tool 必须用 call(name, arguments) ——
    旧版 `call(name, **kwargs)` 跟 registry.call(name, arguments) 签名不符,
    导致工具调用永远 TypeError。
    """
    from openclaw.llm.base import ChatMessage, LLMResult, ToolCall
    from openclaw.tools.registry import ToolCategory, ToolPermission, ToolRegistry

    calls: list[dict] = []

    class ToolCallLLM:
        def __init__(self):
            self.calls = 0

        async def acomplete(self, messages, tools=None, **kw):
            self.calls += 1
            # 第一次:返回 tool_call; 后续:返回最终答案
            if self.calls == 1:
                return LLMResult(
                    content="",
                    tool_calls=[ToolCall(id="1", name="my_tool", arguments={"x": 1})],
                )
            # 找最近一条 tool result 作为上下文
            last_tool = next(
                (m for m in reversed(messages) if m.role == "tool"),
                None,
            )
            text = last_tool.content if last_tool else "(empty)"
            return LLMResult(content=f"final: {text}", tool_calls=[])

    reg = ToolRegistry()
    @reg.tool(category=ToolCategory.UTILITY, permission=ToolPermission.SAFE)
    def my_tool(x: int = 0) -> str:
        """测试工具。"""
        calls.append({"x": x})
        return f"got x={x}"

    class Mem:
        async def build_messages(self, *a, **kw):
            return [ChatMessage(role="user", content="hi")]
        async def append_turn(self, *a, **kw):
            pass

    from openclaw.agent.loop import Agent
    agent = Agent(llm=ToolCallLLM(), tools=reg, memory=Mem(), session_id="sess_tc")
    resp = asyncio.run(agent.run("use my_tool"))
    # 关键断言:registry.call 真的被调过,且 arguments 正确传入
    assert calls, "Agent.run 应触发 tool 调用"
    assert calls[0]["x"] == 1
    assert "got x=1" in resp.content

def test_agent_run_writes_journal_automatically(tmp_path: Path):
    """Agent.run 完成后,journal 应自动记录(无需调用方显式触发)。"""
    from openclaw.agent.journal import AgentJournal
    from openclaw.agent.loop import Agent
    from openclaw.tools.registry import ToolRegistry

    # mock LLM,只跑 1 轮
    class OneShot:
        async def acomplete(self, messages, tools=None, **kw):
            from openclaw.llm.base import LLMResult
            return LLMResult(content="done!", tool_calls=[])

    # mock memory
    class Mem:
        async def build_messages(self, *a, **kw):
            from openclaw.llm.base import ChatMessage
            return [ChatMessage(role="system", content="sys"), ChatMessage(role="user", content="user")]
        async def append_turn(self, *a, **kw):
            pass

    j = AgentJournal(root=tmp_path / "j")
    agent = Agent(
        llm=OneShot(), tools=ToolRegistry(), memory=Mem(),
        session_id="sess_integration",
        journal=j,
    )
    resp = asyncio.run(agent.run("hello agent"))
    assert resp.content == "done!"
    files = list((tmp_path / "j").rglob("sess_*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "hello agent" in text
    assert "done!" in text
    # 反思已自动追加
    assert "反思" in text


def test_agent_run_journal_failure_does_not_break(tmp_path: Path):
    """journal 内部出错时,agent run 仍应正常返回。"""
    from openclaw.agent.loop import Agent
    from openclaw.tools.registry import ToolRegistry

    class OneShot:
        async def acomplete(self, messages, tools=None, **kw):
            from openclaw.llm.base import LLMResult
            return LLMResult(content="ok", tool_calls=[])

    class Mem:
        async def build_messages(self, *a, **kw):
            from openclaw.llm.base import ChatMessage
            return [ChatMessage(role="user", content="x")]
        async def append_turn(self, *a, **kw):
            pass

    # 故意传一个 broken journal
    class BrokenJournal:
        def record_session(self, *a, **kw):
            raise RuntimeError("disk full")

    agent = Agent(
        llm=OneShot(), tools=ToolRegistry(), memory=Mem(),
        session_id="sess_broken",
        journal=BrokenJournal(),
    )
    # 不应抛
    resp = asyncio.run(agent.run("hi"))
    assert resp.content == "ok"
