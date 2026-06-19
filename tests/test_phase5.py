"""Phase 5 单测:Plan-Execute / Multi-Agent / Router。

全部使用 mock LLM / mock tool,不依赖网络。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from openclaw.agent import (
    Plan,
    PlanExecutor,
    PlanStep,
    StepKind,
    StepStatus,
    MultiAgentRoles,
)
from openclaw.core.errors import ProviderError
from openclaw.llm.base import BaseLLMProvider, ChatMessage, LLMResult
from openclaw.providers.router import ProviderRouter
from openclaw.tools.registry import ToolRegistry


# ---------------- 通用 mock ----------------

class MockProvider(BaseLLMProvider):
    """可控的 mock LLM,response_fn 决定每次返回什么。"""
    def __init__(self, name: str, response_fn):
        super().__init__(model=name)
        self.name = name
        self.response_fn = response_fn
        self.calls: list[dict[str, Any]] = []

    async def acomplete(
        self,
        messages: list[ChatMessage],
        tools=None,
        *,
        temperature: float = 0.7,
        max_tokens=None,
    ) -> LLMResult:
        self.calls.append({"n_msgs": len(messages), "tools": len(tools or [])})
        return self.response_fn(messages, tools)


def _text(s: str) -> LLMResult:
    return LLMResult(content=s, tool_calls=[])


# ---------------- Plan / Planner 数据类 ----------------

def test_plan_validate_dag_ok():
    p = Plan(goal="g", steps=[
        PlanStep(id="a", name="a", kind=StepKind.LLM, target="x"),
        PlanStep(id="b", name="b", kind=StepKind.TOOL, target="t", arguments={}, depends_on=["a"]),
        PlanStep(id="c", name="c", kind=StepKind.LLM, target="y", depends_on=["a"]),
        PlanStep(id="d", name="d", kind=StepKind.LLM, target="z", depends_on=["b", "c"]),
    ])
    assert p.validate() == []
    layers = p.topological_layers()
    assert [len(layer) for layer in layers] == [1, 2, 1]
    assert {s.id for s in layers[0]} == {"a"}
    assert {s.id for s in layers[1]} == {"b", "c"}
    assert {s.id for s in layers[2]} == {"d"}


def test_plan_validate_unknown_dep():
    p = Plan(steps=[PlanStep(id="a", name="a", kind=StepKind.LLM, target="x", depends_on=["ghost"])])
    errs = p.validate()
    assert any("ghost" in e for e in errs)


def test_plan_validate_cycle():
    p = Plan(steps=[
        PlanStep(id="a", name="a", kind=StepKind.LLM, target="x", depends_on=["b"]),
        PlanStep(id="b", name="b", kind=StepKind.LLM, target="y", depends_on=["a"]),
    ])
    assert any("cycle" in e for e in p.validate())


def test_plan_from_json():
    raw = {
        "goal": "算一下",
        "steps": [
            {"id": "s1", "kind": "tool", "target": "calculator", "arguments": {"expression": "1+1"}},
            {"id": "s2", "kind": "llm", "target": "回答用户", "depends_on": ["s1"]},
        ],
    }
    from openclaw.agent.planner import plan_from_json
    p = plan_from_json(raw)
    assert p.goal == "算一下"
    assert len(p.steps) == 2
    assert p.steps[0].kind == StepKind.TOOL
    assert p.steps[1].depends_on == ["s1"]


# ---------------- Executor ----------------

def test_executor_runs_topologically():
    events: list[str] = []
    async def on_llm(prompt, step):
        events.append(f"llm:{step.id}:{step.target}")
        return f"out_{step.id}"
    async def on_tool(name, args, step):
        events.append(f"tool:{name}:{step.id}")
        return f"toolout_{step.id}"
    ex = PlanExecutor(on_llm=on_llm, on_tool=on_tool)
    plan = Plan(steps=[
        PlanStep(id="a", name="a", kind=StepKind.LLM, target="ask"),
        PlanStep(id="b", name="b", kind=StepKind.TOOL, target="echo", arguments={"m": "hi"}, depends_on=["a"]),
        PlanStep(id="c", name="c", kind=StepKind.LLM, target="summarize", depends_on=["b"]),
    ])
    res = asyncio.run(ex.run(plan))
    assert res.finished
    assert {r.step_id: r.output for r in res.steps if r.status.value == "done"} == {
        "a": "out_a", "b": "toolout_b", "c": "out_c",
    }
    # b 必须在 a 之后,c 必须在 b 之后
    assert events.index("llm:a:ask") < events.index("tool:echo:b")
    assert events.index("tool:echo:b") < events.index("llm:c:summarize")


def test_executor_parallel_layer():
    # 两个无依赖 step 应并行触发(同层)
    started: list[str] = []
    async def on_llm(prompt, step):
        started.append(step.id)
        await asyncio.sleep(0.05)
        return f"o_{step.id}"
    ex = PlanExecutor(on_llm=on_llm)
    plan = Plan(steps=[
        PlanStep(id="x", name="x", kind=StepKind.LLM, target="a"),
        PlanStep(id="y", name="y", kind=StepKind.LLM, target="b"),
    ])
    import time as _t
    t0 = _t.time()
    res = asyncio.run(ex.run(plan))
    elapsed = _t.time() - t0
    # 两个并行各 0.05s,串行需要 0.1s
    assert elapsed < 0.09
    assert res.finished


def test_executor_retry_succeeds():
    n = {"v": 0}
    async def on_llm(prompt, step):
        n["v"] += 1
        if n["v"] < 2:
            raise RuntimeError("transient")
        return "ok"
    ex = PlanExecutor(on_llm=on_llm)
    plan = Plan(steps=[PlanStep(id="x", name="x", kind=StepKind.LLM, target="t", max_retries=2)])
    res = asyncio.run(ex.run(plan))
    assert res.finished
    assert res.steps[0].attempts == 2
    assert res.steps[0].output == "ok"


def test_executor_short_circuit_on_critical_fail():
    ran = []
    async def on_llm(prompt, step):
        ran.append(step.id)
        if step.id == "a":
            raise RuntimeError("boom")
        return "ok"
    ex = PlanExecutor(on_llm=on_llm)
    plan = Plan(steps=[
        PlanStep(id="a", name="a", kind=StepKind.LLM, target="t", max_retries=0, critical=True),
        PlanStep(id="b", name="b", kind=StepKind.LLM, target="t", critical=True, depends_on=["a"]),
    ])
    res = asyncio.run(ex.run(plan))
    assert not res.finished
    # b 不应执行
    assert "b" not in ran
    assert res.steps[-1].status == StepStatus.SKIPPED


def test_executor_noncritical_fail_continues():
    ran = []
    async def on_llm(prompt, step):
        ran.append(step.id)
        if step.id == "a":
            raise RuntimeError("oops")
        return f"ok_{step.id}"
    ex = PlanExecutor(on_llm=on_llm)
    plan = Plan(steps=[
        PlanStep(id="a", name="a", kind=StepKind.LLM, target="t", critical=False),
        PlanStep(id="b", name="b", kind=StepKind.LLM, target="t", depends_on=["a"]),
    ])
    res = asyncio.run(ex.run(plan))
    # 计划标记为 finished(因为 a 不 critical)
    assert res.finished
    assert ran == ["a", "b"]


# ---------------- Router ----------------

def test_router_fallback_only():
    p1 = MockProvider("p1", lambda *_: (_ for _ in ()).throw(RuntimeError("p1 fail")))
    p2 = MockProvider("p2", lambda *_: _text("from p2"))
    r = ProviderRouter(p1, [p2], strategy="fallback_only")
    res = asyncio.run(r.acomplete([ChatMessage(role="user", content="hi")]))
    assert res.content == "from p2"
    assert r.stats.by_provider["MockProvider:p1"]["fail"] == 1
    assert r.stats.by_provider["MockProvider:p2"]["ok"] == 1


def test_router_round_robin():
    a = MockProvider("a", lambda *_: _text("A"))
    b = MockProvider("b", lambda *_: _text("B"))
    r = ProviderRouter(a, [b], strategy="round_robin")
    seen = set()
    for _ in range(4):
        r2 = asyncio.run(r.acomplete([ChatMessage(role="user", content="x")]))
        seen.add(r2.content)
    assert seen == {"A", "B"}


def test_router_cost_aware_order():
    expensive = MockProvider("exp", lambda *_: _text("expensive"))
    cheap = MockProvider("cheap", lambda *_: _text("cheap"))
    r = ProviderRouter(expensive, [cheap], strategy="cost_aware")
    r.set_meta(expensive, cost_per_1k=10.0)
    r.set_meta(cheap, cost_per_1k=1.0)
    # 第一个调用应该走 cheap
    res = asyncio.run(r.acomplete([ChatMessage(role="user", content="x")]))
    assert res.content == "cheap"


def test_router_priority_order():
    high = MockProvider("high", lambda *_: _text("HIGH"))
    low = MockProvider("low", lambda *_: _text("LOW"))
    r = ProviderRouter(high, [low], strategy="priority")
    r.set_meta(high, priority=999)
    r.set_meta(low, priority=1)
    res = asyncio.run(r.acomplete([ChatMessage(role="user", content="x")]))
    assert res.content == "LOW"


def test_router_all_fail_raises():
    a = MockProvider("a", lambda *_: (_ for _ in ()).throw(RuntimeError("a fail")))
    b = MockProvider("b", lambda *_: (_ for _ in ()).throw(RuntimeError("b fail")))
    r = ProviderRouter(a, [b], strategy="fallback_only")
    with pytest.raises(ProviderError):
        asyncio.run(r.acomplete([ChatMessage(role="user", content="x")]))


def test_router_step_retry():
    n = {"v": 0}
    def resp(msgs, tools):
        n["v"] += 1
        if n["v"] < 2:
            raise RuntimeError("transient")
        return _text("ok")
    a = MockProvider("a", resp)
    r = ProviderRouter(a, [], strategy="fallback_only")
    r.set_meta(a, max_attempts=3)
    res = asyncio.run(r.acomplete_with_retry(
        [ChatMessage(role="user", content="x")], max_attempts_per_step=3,
    ))
    assert res.content == "ok"
    assert n["v"] == 2


# ---------------- Multi-Agent(mock LLM) ----------------

def test_multi_agent_runs_planner_then_steps():
    # 第一次 LLM 调用 = Planner,返回 JSON 计划
    # 后续 LLM 调用 = executor step,返回 step 输出
    step_outputs = iter(["step1 done", "step2 done"])
    call = {"n": 0}

    def resp(msgs, tools):
        call["n"] += 1
        if call["n"] == 1:
            # planner
            return _text(json.dumps({
                "goal": "g",
                "steps": [
                    {"id": "s1", "kind": "llm", "target": "先思考"},
                    {"id": "s2", "kind": "llm", "target": "再总结", "depends_on": ["s1"]},
                ],
            }))
        return _text(next(step_outputs))

    llm = MockProvider("llm", resp)
    reg = ToolRegistry()
    ma = MultiAgentRoles(llm, reg, enable_critic=False, enable_reflector=False)
    res = asyncio.run(ma.run("用户问题"))
    # 至少调了 3 次(1 次 planner + 2 次 executor step + 1 次 critic=0)
    assert call["n"] >= 3
    assert "step2 done" in res.final_answer or "step1 done" in res.final_answer


def test_multi_agent_planner_json_fallback():
    # planner 第一次没返回 JSON,应回退到单 step
    def resp(msgs, tools):
        return _text("直接是文本不是 JSON,但应该被兜底")

    llm = MockProvider("llm", resp)
    reg = ToolRegistry()
    ma = MultiAgentRoles(llm, reg, enable_critic=False, enable_reflector=False)
    res = asyncio.run(ma.run("?"))
    assert "直接是文本" in res.plan.steps[0].target


def test_multi_agent_critic_flags_bad_answer():
    call = {"n": 0}
    def resp(msgs, tools):
        call["n"] += 1
        if call["n"] == 1:
            return _text(json.dumps({"goal": "g", "steps": [
                {"id": "s1", "kind": "llm", "target": "做"},
            ]}))
        if call["n"] == 2:
            return _text("step 输出")
        # critic
        return _text(json.dumps({"ok": False, "issues": ["答非所问"], "score": 0.2}))

    llm = MockProvider("llm", resp)
    reg = ToolRegistry()
    ma = MultiAgentRoles(llm, reg, enable_critic=True, enable_reflector=False)
    res = asyncio.run(ma.run("用户问数学"))
    assert res.critic is not None
    assert res.critic["ok"] is False
    assert "Critic 提示" in res.final_answer
    assert any("答非所问" in i for i in res.critic["issues"])
