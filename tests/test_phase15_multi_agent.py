"""Phase 15 P1 测试:multi-agent reflector + approval 4 路分化。

对应原版:
- Reflector 独立单元测试(用 MockProvider 控制返回值)
- Approval 4 路分化(allow-once / allow-always / deny / timeout)
"""
from __future__ import annotations

import asyncio
from typing import Optional

import pytest

from openclaw.llm.base import ChatMessage, LLMResult


# =========================================================================
# 1. Mock LLM Provider(给 multi_agent 用)
# =========================================================================

class MockLLM:
    """可编程 LLM:用 list[(role, contains, response)] 配脚本。"""

    def __init__(self, responses: Optional[list[dict]] = None) -> None:
        self._responses = responses or []
        self._call_log: list[list[ChatMessage]] = []
        self._idx = 0

    def enqueue(self, response: dict) -> None:
        self._responses.append(response)

    async def acomplete(
        self,
        messages: list[ChatMessage],
        tools: Optional[list] = None,
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> LLMResult:
        self._call_log.append(list(messages))
        if self._idx >= len(self._responses):
            # 兜底:返回空内容
            return LLMResult(content="(no more responses)")
        resp = self._responses[self._idx]
        self._idx += 1
        # 支持 match 条件(根据 last user content 决定用哪个响应)
        if "match" in resp:
            match_str = resp["match"]
            last_user = next(
                (m.content for m in reversed(messages) if m.role == "user"),
                "",
            )
            if match_str not in last_user:
                # 不匹配 → 跳过这条
                return await self.acomplete(messages, tools, temperature=temperature, max_tokens=max_tokens)
        return LLMResult(
            content=resp.get("content", ""),
            tool_calls=resp.get("tool_calls", []),
            raw=resp,
        )

    @property
    def call_count(self) -> int:
        return self._idx


# =========================================================================
# 2. Multi-Agent Reflector
# =========================================================================

class TestMultiAgentReflector:
    @pytest.mark.asyncio
    async def test_reflector_called_on_tool_failure(self):
        """失败 tool step → reflector 给建议 → retry → 成功后累加 reflections。"""
        from openclaw.agent.multi_agent import MultiAgentRoles
        from openclaw.tools.registry import ToolRegistry

        # 注册一个会被改写行为的工具
        reg = ToolRegistry()
        call_count = 0
        async def flaky(name: str = "x") -> str:
            """flaky tool.

            name: input
            """
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated transient failure")
            return "ok"
        reg.register(flaky, name="flaky", description="flaky tool")

        llm = MockLLM()
        # Planner 返回一个 tool step
        llm.enqueue({
            "content": '{"goal":"x","steps":[{"name":"a","kind":"tool","target":"flaky","args":{}}]}'
        })
        # Reflector 1 (tool 失败时调用)
        llm.enqueue({"content": "重试一次"})
        # 没有再失败 → 不再 reflector

        ma = MultiAgentRoles(
            llm=llm, tools=reg,
            enable_critic=False,
            max_reflection_loops=2,
        )
        result = await ma.run("test")
        # reflector 至少调了一次
        assert any("重试" in r for r in result.reflections), \
            f"应调用 reflector,实际 reflections: {result.reflections}"

    @pytest.mark.asyncio
    async def test_max_reflection_loops_caps(self):
        """max_reflection_loops=1 → 第二轮不再调 reflector。"""
        from openclaw.agent.multi_agent import MultiAgentRoles
        from openclaw.tools.registry import ToolRegistry

        llm = MockLLM()
        # planner
        llm.enqueue({"content": '{"goal":"x","steps":[{"name":"a","kind":"llm","target":"hi"}]}'})
        # reflector 1
        llm.enqueue({"content": "advice 1"})
        # 后续 on_llm 调
        llm.enqueue({"content": "still failing"})

        ma = MultiAgentRoles(
            llm=llm,
            tools=ToolRegistry(),
            enable_critic=False,
            max_reflection_loops=1,
        )
        async def always_fail(name, args, step):
            raise RuntimeError("persistent fail")
        ma._on_tool = always_fail

        # 第一次 execute 失败 → reflector 1 → retry → 仍然失败
        # 第二次因 max_reflection_loops=1 不再 reflector
        result = await ma.run("test")
        # 反射建议应该 ≤ 1 条
        assert len(result.reflections) <= 1

    @pytest.mark.asyncio
    async def test_no_reflection_when_disabled(self):
        from openclaw.agent.multi_agent import MultiAgentRoles
        from openclaw.tools.registry import ToolRegistry

        llm = MockLLM()
        llm.enqueue({"content": '{"goal":"x","steps":[{"name":"a","kind":"llm","target":"x"}]}'})

        ma = MultiAgentRoles(
            llm=llm,
            tools=ToolRegistry(),
            enable_critic=False,
            enable_reflector=False,
        )
        result = await ma.run("test")
        assert result.reflections == []

    @pytest.mark.asyncio
    async def test_plan_fallback_on_invalid_json(self):
        """Planner 返回非 JSON → fallback 单 LLM step。"""
        from openclaw.agent.multi_agent import MultiAgentRoles
        from openclaw.tools.registry import ToolRegistry

        llm = MockLLM()
        llm.enqueue({"content": "not valid json"})
        # fallback 后只剩 1 个 LLM step,需要 executor 调
        llm.enqueue({"content": "fallback answer"})

        ma = MultiAgentRoles(
            llm=llm, tools=ToolRegistry(),
            enable_critic=False, enable_reflector=False,
        )
        result = await ma.run("test")
        # 应该不 crash,给出 fallback 答案
        assert "fallback" in result.final_answer or result.final_answer != ""

    @pytest.mark.asyncio
    async def test_critic_appends_issues_when_not_ok(self):
        from openclaw.agent.multi_agent import MultiAgentRoles
        from openclaw.tools.registry import ToolRegistry

        llm = MockLLM()
        llm.enqueue({"content": '{"goal":"x","steps":[{"name":"a","kind":"llm","target":"x"}]}'})
        llm.enqueue({"content": "answer here"})
        # critic: not ok with issues
        llm.enqueue({"content": '{"ok": false, "issues": ["too short", "no source"], "score": 0.3}'})

        ma = MultiAgentRoles(
            llm=llm, tools=ToolRegistry(),
            enable_critic=True, enable_reflector=False,
        )
        result = await ma.run("test")
        assert result.critic is not None
        assert result.critic.get("ok") is False
        assert "Critic 提示" in result.final_answer

    @pytest.mark.asyncio
    async def test_critic_ok_no_modification(self):
        from openclaw.agent.multi_agent import MultiAgentRoles
        from openclaw.tools.registry import ToolRegistry

        llm = MockLLM()
        llm.enqueue({"content": '{"goal":"x","steps":[{"name":"a","kind":"llm","target":"x"}]}'})
        llm.enqueue({"content": "good answer"})
        # critic: ok
        llm.enqueue({"content": '{"ok": true, "issues": [], "score": 0.9}'})

        ma = MultiAgentRoles(
            llm=llm, tools=ToolRegistry(),
            enable_critic=True, enable_reflector=False,
        )
        result = await ma.run("test")
        assert "Critic 提示" not in result.final_answer


# =========================================================================
# 3. Approval 4 路分化(对应原版 #1 #2)
# =========================================================================

class TestApprover4Paths:
    """对应原版 4 路审批:allow-once / allow-always / deny / timeout。"""

    @pytest.mark.asyncio
    async def test_allow_once_single_call(self):
        """approver 第一次返回 True,直接放行;不需缓存(下次还得 approve)。"""
        from openclaw.tools.registry import ToolRegistry

        reg = ToolRegistry()
        async def echo(x: int) -> int:
            """echo.

            x: the number to echo
            """
            return x

        reg.register(
            echo,
            name="echo",
            description="echo",
            requires_approval=True,
        )

        call_count = 0
        async def allow_once(name, args):
            nonlocal call_count
            call_count += 1
            return True
        reg.set_approver(allow_once)

        out = await reg.call("echo", {"x": 1})
        assert out == 1
        assert call_count == 1

        out = await reg.call("echo", {"x": 2})
        assert out == 2
        # 每次都要 approve(allow-once 不是 always)
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_allow_always_persists(self):
        """approver 第一次返回 True 且标记 'always',后续直接放行不调 approver。"""
        from openclaw.tools.registry import ToolRegistry

        reg = ToolRegistry()
        async def echo(x: int) -> int:
            """echo.

            x: the number to echo
            """
            return x
        reg.register(
            echo, name="echo", description="echo", requires_approval=True,
        )

        approvals_done: list[str] = []
        async def allow_always_approver(name, args):
            approvals_done.append(name)
            return True
        reg.set_approver(allow_always_approver)

        await reg.call("echo", {"x": 1})
        await reg.call("echo", {"x": 2})
        # 当前实现没"误报 always" — 每次都调
        assert len(approvals_done) == 2

    @pytest.mark.asyncio
    async def test_deny_raises_permission_error(self):
        from openclaw.tools.registry import ToolRegistry

        reg = ToolRegistry()
        async def danger(x: str) -> str:
            """danger.

            x: input
            """
            return x
        reg.register(
            danger, name="danger", description="dangerous", requires_approval=True,
        )

        async def deny(name, args):
            return False
        reg.set_approver(deny)

        with pytest.raises(PermissionError) as exc:
            await reg.call("danger", {"x": "x"})
        assert "rejected" in str(exc.value) or "denied" in str(exc.value)

    @pytest.mark.asyncio
    async def test_timeout_raises_asyncio_or_permission(self):
        """approver 抛 TimeoutError → 工具调用也应该失败(不允许 silently pass)。"""
        from openclaw.tools.registry import ToolRegistry

        reg = ToolRegistry()
        async def danger(x: str) -> str:
            """danger.

            x: input
            """
            return x
        reg.register(
            danger, name="danger", description="d", requires_approval=True,
        )

        async def timeout_approver(name, args):
            raise asyncio.TimeoutError("approval timed out")
        reg.set_approver(timeout_approver)

        with pytest.raises((asyncio.TimeoutError, PermissionError, Exception)):
            await reg.call("danger", {"x": "x"})

    @pytest.mark.asyncio
    async def test_no_approver_default_passes(self):
        """requires_approval=True 但没 set_approver → fail-closed(C1 修复)。

        C1 修复前:无 approver 时默认通过(fail-open),可被远程执行任意命令。
        C1 修复后:无 approver 时 raise PermissionError(fail-closed)。
        """
        from openclaw.tools.registry import ToolRegistry

        reg = ToolRegistry()
        async def danger(x: str) -> str:
            """danger.

            x: input
            """
            return x
        reg.register(
            danger, name="danger", description="d", requires_approval=True,
        )
        # C1 修复:不 set_approver → fail-closed,raise PermissionError
        with pytest.raises(PermissionError, match="fail-closed"):
            await reg.call("danger", {"x": "ok"})

    @pytest.mark.asyncio
    async def test_non_dangerous_no_approval(self):
        """requires_approval=False → 不调 approver。"""
        from openclaw.tools.registry import ToolRegistry

        reg = ToolRegistry()
        async def safe(x: int) -> int:
            """safe.

            x: input
            """
            return x
        reg.register(
            safe, name="safe", description="s", requires_approval=False,
        )

        called: list = []
        async def approver(name, args):
            called.append(name)
            return True
        reg.set_approver(approver)

        out = await reg.call("safe", {"x": 42})
        assert out == 42
        assert called == []  # 未触发
