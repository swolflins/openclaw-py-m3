"""Plan 数据结构(Phase 5)。

一个 Plan 是一棵 DAG:
- PlanStep: 一个原子步骤(可执行 / 调用工具 / 调用 LLM / 委派给 sub-agent)
- depends_on: 前置步骤 id 列表
- executor/step() 走拓扑序并行执行

LLM 生成 plan 的接口留给上层(multi_agent 或 AgentLoop 选用)。
"""
from __future__ import annotations

import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class StepKind(str, Enum):
    LLM = "llm"            # 调一次 LLM(可附 tools)
    TOOL = "tool"          # 直接调一个工具
    AGENT = "agent"        # 委派给一个 sub-agent(phase 5+)
    SCRIPT = "script"      # 跑一段 Python 表达式


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class PlanStep:
    """Plan 的一个节点。"""
    id: str = field(default_factory=lambda: f"step_{uuid.uuid4().hex[:8]}")
    name: str = ""
    kind: StepKind = StepKind.LLM
    # LLM/AGENT: prompt; TOOL: tool name; SCRIPT: python expr str
    target: str = ""
    # 调工具时的参数(仅 TOOL/AGENT 用得到)
    arguments: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    # 失败重试次数(本身失败时,executor 会重试整个 step)
    max_retries: int = 0
    # 跳过后续步骤(用于 on-fail 短路)
    critical: bool = True
    # 上下文标签,便于多 agent 时把 step 输出路由给 critic / reflector
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StepResult:
    """Step 执行的输出。"""
    step_id: str
    status: StepStatus
    output: Any = None
    error: Optional[str] = None
    attempts: int = 1
    duration_ms: int = 0


@dataclass
class Plan:
    """完整的执行计划。"""
    goal: str = ""
    steps: list[PlanStep] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def step_map(self) -> dict[str, PlanStep]:
        return {s.id: s for s in self.steps}

    def validate(self) -> list[str]:
        """返回所有错误(空列表 = 合法)。"""
        errs: list[str] = []
        ids = {s.id for s in self.steps}
        for s in self.steps:
            for d in s.depends_on:
                if d not in ids:
                    errs.append(f"step {s.id} depends on unknown step {d}")
        # 检测环:用 Kahn 算法(只在 id 完整时跑)
        if any("depends on unknown" in e for e in errs):
            return errs
        indeg: dict[str, int] = {s.id: 0 for s in self.steps}
        adj: dict[str, list[str]] = {s.id: [] for s in self.steps}
        for s in self.steps:
            for d in s.depends_on:
                adj[d].append(s.id)
                indeg[s.id] += 1
        queue = deque(nid for nid, c in indeg.items() if c == 0)  # L8 修复:用 deque 替代 list
        visited = 0
        while queue:
            n = queue.popleft()  # L8 修复:O(1) popleft 替代 O(n) pop(0)
            visited += 1
            for m in adj[n]:
                indeg[m] -= 1
                if indeg[m] == 0:
                    queue.append(m)
        if visited != len(self.steps):
            errs.append("plan has cycles")
        return errs

    def topological_layers(self) -> list[list[PlanStep]]:
        """返回一组层(同层可并行)。"""
        indeg: dict[str, int] = {s.id: 0 for s in self.steps}
        adj: dict[str, list[str]] = {s.id: [] for s in self.steps}
        smap = self.step_map()
        for s in self.steps:
            for d in s.depends_on:
                if d in smap:
                    adj[d].append(s.id)
                    indeg[s.id] += 1
        layers: list[list[PlanStep]] = []
        remaining = set(smap)
        while remaining:
            ready = [smap[nid] for nid in remaining if indeg[nid] == 0]
            if not ready:
                break  # 环,留 validate 报
            layers.append(ready)
            for s in ready:
                remaining.discard(s.id)
                for m in adj[s.id]:
                    indeg[m] -= 1
        return layers


# ------------- 序列化(给 LLM 用) -------------

PLAN_JSON_SCHEMA_HINT = """\
{
  "goal": "<一句话目标>",
  "steps": [
    {
      "id": "step_1",
      "name": "简短描述",
      "kind": "llm|tool|agent|script",
      "target": "tool_name 或 prompt 或 sub_agent_name",
      "arguments": { ... },            // tool/agent 必填
      "depends_on": ["step_0"],        // 可选
      "max_retries": 0,
      "critical": true
    }
  ]
}"""


def plan_from_json(data: dict[str, Any]) -> Plan:
    """从 LLM 输出的 dict 构造 Plan(缺字段给默认值,不做严格校验)。"""
    steps_raw = data.get("steps") or []
    steps: list[PlanStep] = []
    for s in steps_raw:
        if not isinstance(s, dict):
            continue
        try:
            kind = StepKind(s.get("kind", "llm"))
        except ValueError:
            kind = StepKind.LLM
        steps.append(PlanStep(
            id=str(s.get("id") or f"step_{uuid.uuid4().hex[:6]}"),
            name=str(s.get("name") or ""),
            kind=kind,
            target=str(s.get("target") or ""),
            arguments=dict(s.get("arguments") or {}),
            depends_on=list(s.get("depends_on") or []),
            max_retries=int(s.get("max_retries", 0) or 0),
            critical=bool(s.get("critical", True)),
        ))
    return Plan(goal=str(data.get("goal") or ""), steps=steps)
