"""Plan Executor(Phase 5)。

职责:
- 拓扑序执行 Plan
- 同层并行(asyncio.gather)
- 单步失败重试 + on-fail 短路
- 把 step 输出注入到下一步的 LLM 上下文(可选)
- 写回 PlanResult

设计:
- Executor 不直接感知 LLM,而是通过回调把 LLM / TOOL 调用外置:
    - on_llm(prompt, step) -> str
    - on_tool(name, args, step) -> Any
    - on_agent(name, prompt, step) -> str
  这样 Executor 是 100% 可单测的(传 mock 回调即可)。
"""
from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from openclaw.agent.planner import Plan, PlanStep, StepKind, StepResult, StepStatus
from openclaw.core.logging import get_logger

logger = get_logger(__name__)


# 回调类型
OnLLM = Callable[[str, PlanStep], Awaitable[str]]
OnTool = Callable[[str, dict[str, Any], PlanStep], Awaitable[Any]]
OnAgent = Callable[[str, str, PlanStep], Awaitable[str]]
OnScript = Callable[[str, PlanStep], Awaitable[Any]]


@dataclass
class PlanResult:
    plan: Plan
    steps: list[StepResult] = field(default_factory=list)
    finished: bool = False
    error: Optional[str] = None

    def outputs(self) -> dict[str, Any]:
        """step_id -> output,只包含成功的。"""
        return {r.step_id: r.output for r in self.steps if r.status == StepStatus.DONE}

    def last_output(self) -> Any:
        for r in reversed(self.steps):
            if r.status == StepStatus.DONE and r.output is not None:
                return r.output
        return None


class PlanExecutor:
    """按拓扑层执行 Plan。"""

    def __init__(
        self,
        *,
        on_llm: Optional[OnLLM] = None,
        on_tool: Optional[OnTool] = None,
        on_agent: Optional[OnAgent] = None,
        on_script: Optional[OnScript] = None,
        on_step_start: Optional[Callable[[PlanStep], None]] = None,
        on_step_done: Optional[Callable[[PlanStep, StepResult], None]] = None,
        max_parallel: int = 4,
    ) -> None:
        self.on_llm = on_llm
        self.on_tool = on_tool
        self.on_agent = on_agent
        self.on_script = on_script
        self.on_step_start = on_step_start
        self.on_step_done = on_step_done
        self.max_parallel = max(1, max_parallel)

    async def run(
        self,
        plan: Plan,
        *,
        step_cache: Optional[dict[str, StepResult]] = None,
    ) -> PlanResult:
        """执行 plan。

        Args:
            plan: 要跑的 plan
            step_cache: **RT-6 修复** — 已完成的 step 直接复用,不重复执行。
                适用场景:reflection 重试同一 plan 的一部分,
                已 done 的 step 不应浪费 LLM/tool 调用。
                key 是 ``step.id``。

        Returns:
            ``PlanResult``,带 ``step_cache`` 里的 completed steps 合并入 ``result.steps``。
        """
        errs = plan.validate()
        if errs:
            return PlanResult(plan=plan, finished=False, error="; ".join(errs))

        result = PlanResult(plan=plan)
        step_results: dict[str, StepResult] = {}
        # RT-6:先把 cache 里的已完成结果预填进去
        if step_cache:
            for sid, sr in step_cache.items():
                if sr.status == StepStatus.DONE:
                    step_results[sid] = sr
                    result.steps.append(sr)
        failed: Optional[str] = None

        for layer in plan.topological_layers():
            if failed:
                # 短路:把剩下的全标 skipped
                for s in layer:
                    sr = StepResult(step_id=s.id, status=StepStatus.SKIPPED)
                    step_results[s.id] = sr
                    result.steps.append(sr)
                continue
            # RT-6:同层内若 step 已在 cache 中 done,直接用
            pending = [s for s in layer if s.id not in step_results]
            # 把 cache 命中的也标 done
            for s in layer:
                if s.id in step_results and step_results[s.id].status == StepStatus.DONE:
                    if not any(r.step_id == s.id for r in result.steps):
                        result.steps.append(step_results[s.id])
            if not pending:
                continue
            # 同层并发,但限流到 max_parallel。
            # 用 cancel 机制让 critical step 失败时,同层其它 step 能被中断。
            sem = asyncio.Semaphore(self.max_parallel)
            local_failed: list[str] = []

            async def _run_one(s: PlanStep) -> StepResult:
                if local_failed and s.critical:
                    return StepResult(step_id=s.id, status=StepStatus.SKIPPED)
                if self.on_step_start:
                    try:
                        self.on_step_start(s)
                    except Exception:  # pragma: no cover
                        logger.exception("on_step_start hook failed")
                sr = await self._exec_step(s, step_results, sem)
                if sr.status == StepStatus.FAILED and s.critical:
                    local_failed.append(s.id)
                return sr

            outs = await asyncio.gather(*[_run_one(s) for s in pending], return_exceptions=False)
            for s, sr in zip(pending, outs):
                step_results[s.id] = sr
                result.steps.append(sr)
                if self.on_step_done:
                    try:
                        self.on_step_done(s, sr)
                    except Exception:  # pragma: no cover
                        logger.exception("on_step_done hook failed")
                if sr.status == StepStatus.FAILED and s.critical:
                    failed = s.id
                    break

        if failed:
            result.error = f"plan failed at step {failed}"
        else:
            result.finished = True
        return result

    # ---------- 单步执行 ----------

    async def _exec_step(
        self,
        step: PlanStep,
        results: dict[str, StepResult],
        sem: asyncio.Semaphore,
    ) -> StepResult:
        # 把依赖步骤的输出注入到 prompt / arguments
        context = "\n".join(
            f"[{sid}] {results[sid].output}"
            for sid in step.depends_on if sid in results
        )

        attempts = 0
        last_err: Optional[str] = None
        t0 = time.time()
        while attempts <= step.max_retries:
            attempts += 1
            try:
                async with sem:
                    output = await self._dispatch(step, context)
                return StepResult(
                    step_id=step.id,
                    status=StepStatus.DONE,
                    output=output,
                    attempts=attempts,
                    duration_ms=int((time.time() - t0) * 1000),
                )
            except Exception as e:  # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"
                logger.warning("step_failed step=%s attempt=%d err=%s", step.id, attempts, last_err)
                # RT-8 续:重试时退避(指数 + 抖动),避免上游 provider 雪崩
                if attempts <= step.max_retries:
                    backoff = min(1.0, 0.1 * (2 ** (attempts - 1)))
                    jitter = random.uniform(0, 0.05)  # noqa: S311 — 测试用,非密码学
                    await asyncio.sleep(backoff + jitter)
        return StepResult(
            step_id=step.id,
            status=StepStatus.FAILED,
            error=last_err,
            attempts=attempts,
            duration_ms=int((time.time() - t0) * 1000),
        )

    async def _dispatch(self, step: PlanStep, context: str) -> Any:
        if step.kind == StepKind.LLM:
            if self.on_llm is None:
                raise RuntimeError("no on_llm callback")
            prompt = step.target
            if context:
                prompt = f"{prompt}\n\n# 上游步骤输出\n{context}"
            return await self.on_llm(prompt, step)
        if step.kind == StepKind.TOOL:
            if self.on_tool is None:
                raise RuntimeError("no on_tool callback")
            merged = dict(step.arguments)
            if context and "context" not in merged:
                merged["_context"] = context
            return await self.on_tool(step.target, merged, step)
        if step.kind == StepKind.AGENT:
            if self.on_agent is None:
                raise RuntimeError("no on_agent callback")
            prompt = step.target
            if context:
                prompt = f"{prompt}\n\n# 上游步骤输出\n{context}"
            return await self.on_agent(step.target, prompt, step)
        if step.kind == StepKind.SCRIPT:
            if self.on_script is None:
                raise RuntimeError("no on_script callback")
            return await self.on_script(step.target, step)
        raise ValueError(f"unsupported step kind: {step.kind}")
