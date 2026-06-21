"""Multi-Agent 角色编排(Phase 5)。

四个角色:
- Planner: 把 user_message + 当前可用工具/schema 拆成 Plan
- Executor: 跑 Plan(每步一次 LLM 调用,可用工具)
- Critic:   对 Executor 的最终答案做事实/质量检查
- Reflector: 对失败的步骤给出改进建议(下一步重新生成或换工具)

使用:
    ma = MultiAgentRoles(llm, tools, memory=...)
    final = await ma.run("今天几点了?然后算 7*8")
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Optional

from openclaw.agent.executor import PlanExecutor, PlanResult
from openclaw.agent.planner import Plan, PlanStep, StepKind, plan_from_json, PLAN_JSON_SCHEMA_HINT
from openclaw.core.logging import get_logger
from openclaw.llm.base import BaseLLMProvider, ChatMessage
from openclaw.memory.scoped import ScopedMemory
from openclaw.tools.registry import ToolRegistry

logger = get_logger(__name__)


_PLANNER_SYSTEM = """\
你是一个 Planner,把用户问题拆成可执行步骤,每步用一种 kind:
- llm:   需要 LLM 推理/总结(target=prompt)
- tool:  直接调一个工具(target=工具名,arguments 必填)
- agent: 委派子任务(本系统暂未启用,留作扩展)

要求:
1. 步骤尽量原子,优先用工具拿真实数据
2. 步骤之间用 depends_on 表达依赖
3. 同一层的步骤互不依赖,可并行
4. 只输出 JSON,不要加任何解释、注释、markdown 代码块

JSON Schema:
{schema}
"""

_EXECUTOR_STEP_SYSTEM = """\
你是 Executor。按 plan 步骤产出本步骤的输出,要求:
- 严格只输出本步骤该写的内容,不要再加额外解释
- 如果上游 context 给了真实数据,优先用真实数据
"""


_CRITIC_SYSTEM = """\
你是 Critic。判断 assistant 最终答案是否:
1) 回答了用户原问题
2) 与上游工具/步骤的输出事实一致(没编造)
3) 表达简洁清晰

输出严格 JSON,不要 markdown:
{{"ok": true/false, "issues": ["..."], "score": 0.0-1.0}}
"""

_REFLECTOR_SYSTEM = """\
你是 Reflector。某步骤 {step_name} 失败了,错误是:
{error}

给出一段具体的修复建议(改用哪个工具 / 改 prompt / 改参数),让 retry 更可能成功。
只输出建议正文,1-3 句话。
"""


@dataclass
class MultiAgentResult:
    plan: Plan
    execution: PlanResult
    final_answer: str
    critic: Optional[dict[str, Any]] = None
    reflections: list[str] = field(default_factory=list)


class MultiAgentRoles:
    """多 Agent 编排的入口。"""

    def __init__(
        self,
        llm: BaseLLMProvider,
        tools: ToolRegistry,
        memory: Optional[ScopedMemory] = None,
        *,
        session_id: str = "default",
        enable_critic: bool = True,
        enable_reflector: bool = True,
        max_reflection_loops: int = 1,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.memory = memory
        self.session_id = session_id
        self.enable_critic = enable_critic
        self.enable_reflector = enable_reflector
        self.max_reflection_loops = max_reflection_loops

    # ---------- 公开 API ----------

    async def run(self, user_message: str) -> MultiAgentResult:
        plan = await self._plan(user_message)
        # RT-6 修复:reflection 重试时,已 done 的 step 不再重复执行
        from openclaw.agent.planner import StepStatus
        step_cache: dict[str, Any] = {}
        execution = await self._execute(plan, reflections=[], step_cache=step_cache)
        # 反思循环:失败的步骤让 reflector 提建议,改 plan 后重跑
        loops = 0
        reflections: list[str] = []
        while (
            not execution.finished
            and self.enable_reflector
            and loops < self.max_reflection_loops
            and self._has_failed_critical(execution)
        ):
            failed = self._first_failed(execution)
            assert failed is not None
            advice = await self._reflect(failed.step, failed.error or "")
            reflections.append(advice)
            plan = self._patch_plan(plan, failed.step, advice)
            # RT-6:把上次执行中 done 的 step 填进 cache
            for r in execution.steps:
                if r.status == StepStatus.DONE:
                    step_cache[r.step_id] = r
            execution = await self._execute(plan, reflections=reflections, step_cache=step_cache)
            loops += 1

        # 把上游步骤输出拼成最终答案(最后一个 DONE 步骤的 output)
        final = self._compose_final(user_message, execution)
        # Critic 校验
        critic: Optional[dict[str, Any]] = None
        if self.enable_critic:
            critic = await self._critic(user_message, final, execution)
            if not critic.get("ok", True) and critic.get("issues"):
                final = f"{final}\n\n[Critic 提示] " + "; ".join(critic["issues"])

        # 写回记忆
        if self.memory is not None:
            try:
                await self.memory.append_turn(self.session_id, user_message, final)
            except Exception:  # pragma: no cover
                logger.exception("memory.append_turn failed")
        return MultiAgentResult(
            plan=plan,
            execution=execution,
            final_answer=final,
            critic=critic,
            reflections=reflections,
        )

    # ---------- Planner ----------

    async def _plan(self, user_message: str) -> Plan:
        tool_lines = "\n".join(
            f"- {s.name}: {s.description[:120]}" for s in self.tools.specs()
        ) or "(no tools registered)"
        sys_prompt = _PLANNER_SYSTEM.format(schema=PLAN_JSON_SCHEMA_HINT) + \
            f"\n\n# 可用工具\n{tool_lines}\n"
        msgs = [
            ChatMessage(role="system", content=sys_prompt),
            ChatMessage(role="user", content=user_message),
        ]
        result = await self.llm.acomplete(msgs, tools=None, temperature=0.2)
        return self._parse_plan(result.content or "")

    def _parse_plan(self, raw: str) -> Plan:
        # 容错:LLM 偶尔会包 ```json ... ```
        text = raw.strip()
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
        if m:
            text = m.group(1)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("planner returned non-JSON, fall back to single LLM step: %r", raw[:200])
            return Plan(goal="fallback", steps=[
                PlanStep(name="answer", kind=StepKind.LLM, target=raw),
            ])
        return plan_from_json(data)

    # ---------- Executor (bridged) ----------

    async def _execute(
        self,
        plan: Plan,
        reflections: list[str],
        *,
        step_cache: dict[str, Any] | None = None,
    ) -> PlanResult:
        exec_ = PlanExecutor(
            on_llm=self._on_llm,
            on_tool=self._on_tool,
        )
        return await exec_.run(plan, step_cache=step_cache)

    async def _on_llm(self, prompt: str, step: PlanStep) -> str:
        msgs = [
            ChatMessage(role="system", content=_EXECUTOR_STEP_SYSTEM),
            ChatMessage(role="user", content=prompt),
        ]
        specs = self.tools.specs()
        result = await self.llm.acomplete(msgs, tools=specs, temperature=0.3)
        if result.tool_calls:
            # Executor 的 on_llm 期望纯文本,这里简化:执行工具后再合成
            extra = []
            for tc in result.tool_calls:
                try:
                    out = await self.tools.call(tc.name, tc.arguments or {})
                    extra.append(f"[tool {tc.name}] {out}")
                except Exception as e:  # noqa: BLE001
                    extra.append(f"[tool {tc.name} error] {e}")
            follow = [
                *msgs,
                ChatMessage(role="assistant", content=result.content or "", tool_calls=result.tool_calls),
                ChatMessage(role="user", content="工具结果:\n" + "\n".join(extra) + "\n请基于以上结果给出最终回答。"),
            ]
            r2 = await self.llm.acomplete(follow, tools=None, temperature=0.3)
            return r2.content or ""
        return result.content or ""

    async def _on_tool(self, name: str, args: dict[str, Any], step: PlanStep) -> Any:
        clean = {k: v for k, v in args.items() if k != "_context"}
        return await self.tools.call(name, clean)

    # ---------- Reflector ----------

    async def _reflect(self, step: PlanStep, error: str) -> str:
        msgs = [
            ChatMessage(
                role="system",
                content=_REFLECTOR_SYSTEM.format(step_name=step.name or step.id, error=error),
            ),
            ChatMessage(role="user", content="给建议"),
        ]
        r = await self.llm.acomplete(msgs, tools=None, temperature=0.3)
        return r.content or "改用更简单的步骤,或拆得更细。"

    def _patch_plan(self, plan: Plan, failed: PlanStep, advice: str) -> Plan:
        # M12 修复:用 dataclasses.replace 深拷贝 PlanStep,避免浅拷贝污染
        # 旧逻辑:list(plan.steps) 浅拷贝导致 PlanStep 对象跨 plan 共享
        new_steps = [replace(s) for s in plan.steps]
        new = Plan(goal=plan.goal, steps=new_steps, metadata=dict(plan.metadata))
        # M12 修复:retry 保留原 kind/target,不写死 LLM
        # 旧逻辑:retry_step.kind=StepKind.LLM 写死,若失败 step 是 TOOL,
        # 会把工具名当 prompt 送 LLM
        retry_step = PlanStep(
            name=f"retry_{failed.id}",
            kind=failed.kind,  # 保留原 kind
            target=f"{failed.target}\n(Reflection 建议: {advice})",
            depends_on=list(failed.depends_on),
            max_retries=failed.max_retries,
        )
        # 失败节点的依赖方改为依赖 retry
        for s in new.steps:
            if failed.id in s.depends_on:
                if retry_step.id not in s.depends_on:
                    s.depends_on = [retry_step.id if d == failed.id else d for d in s.depends_on]
        # 把失败节点本身标记为可跳过(用一个新的 noop 替身),把 retry 插在它位置
        new.steps = [retry_step if s.id == failed.id else s for s in new.steps]
        return new

    # ---------- Critic ----------

    async def _critic(self, user_message: str, answer: str, execution: PlanResult) -> dict[str, Any]:
        evidence_lines: list[str] = []
        for r in execution.steps:
            if r.status.value == "done" and r.output is not None:
                evidence_lines.append(f"[{r.step_id}] {str(r.output)[:200]}")
        evidence = "\n".join(evidence_lines) or "(no tool evidence)"
        user = (
            f"用户问题: {user_message}\n\n"
            f"最终答案: {answer}\n\n"
            f"证据(工具/步骤输出):\n{evidence}\n\n"
            "请输出 JSON。"
        )
        msgs = [
            ChatMessage(role="system", content=_CRITIC_SYSTEM),
            ChatMessage(role="user", content=user),
        ]
        r = await self.llm.acomplete(msgs, tools=None, temperature=0.1)
        text = (r.content or "").strip()
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
        return {"ok": True, "issues": [], "score": 0.5, "_raw": text}

    # ---------- 拼装最终答案 ----------

    def _compose_final(self, user_message: str, execution: PlanResult) -> str:
        # 取最后一个 done step 的 output 作为主体
        last = execution.last_output()
        if last is None:
            return "(plan failed, no output)"
        if isinstance(last, str):
            return last
        return str(last)

    # ---------- helpers ----------

    def _has_failed_critical(self, execution: PlanResult) -> bool:
        return any(
            r.status.value == "failed" and (s := execution.plan.step_map().get(r.step_id)) and s.critical
            for r in execution.steps
        )

    def _first_failed(self, execution: PlanResult) -> Optional[Any]:
        # RT-5 修复:只返回 critical=True 的失败步骤,避免对非关键步骤浪费 reflector 推理
        smap = execution.plan.step_map()
        for r in execution.steps:
            if r.status.value == "failed":
                s = smap.get(r.step_id)
                if s is not None and s.critical:
                    return type("F", (), {"step": s, "error": r.error})()
        return None
