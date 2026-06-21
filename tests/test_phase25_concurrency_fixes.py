"""Phase 25 / b8: P1 修复 — MetricsMiddleware NameError + executor local_failed 锁 + journal 死代码。

修复目标:
1. ``openclaw/gateway/app.py:71-103`` ``MetricsMiddleware`` 异常分支
   ``response.headers[...]`` NameError → 500 响应丢失耗时 header。
2. ``openclaw/agent/executor.py:130-156`` ``local_failed`` 在多协程下无锁追加。
3. ``openclaw/agent/journal.py:339-341`` ``reflect`` 死代码 — 循环体只
   ``continue`` 没副作用;另:连续空行没归一。

测试覆盖:
- test_metrics_middleware_500_still_has_trace_header: 模拟下游抛错,验证
  500 响应里仍有 ``X-Request-Id`` 头(RequestIDMiddleware 注入的 trace_id)。
- test_executor_local_failed_thread_safe: 100 并发 ``_run_one`` 全成功 +
  全失败两个场景,断言 ``local_failed`` 长度一致(锁保护下不会出现
  覆盖/丢失)。
- test_journal_reflect_dedupes_consecutive_blank_lines: 写 5 个连续空行到
  ``user_message``,``reflect`` 后写入文件里连续空行被压成 1 个。
"""
from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ────────────────────────────────────────────────────────────────
# 1. MetricsMiddleware — 异常路径不回 NameError,500 仍带 trace_id
# ────────────────────────────────────────────────────────────────


def _make_app_with_boom_route():
    """造一个最小 FastAPI app,挂 MetricsMiddleware + RequestIDMiddleware,
    并在 ``/__boom`` 路由上抛 RuntimeError。"""
    from fastapi import FastAPI

    from openclaw.gateway.app import MetricsMiddleware, RequestIDMiddleware

    app = FastAPI()
    # 注册顺序:Starlette 后注册的为外层 → 期望执行顺序
    # RequestIDMiddleware(内) → MetricsMiddleware(外)
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(MetricsMiddleware)

    @app.get("/__boom")
    async def boom():
        raise RuntimeError("downstream blew up")

    @app.get("/__ok")
    async def ok():
        return {"ok": True}

    return app


def test_metrics_middleware_500_still_has_trace_header():
    """下游抛错时,500 响应里仍有 ``X-Request-Id`` 头(trace_id)。

    修复前:MetricsMiddleware 的 ``try/except/finally`` 在 except 走完后,
    ``response.headers[...]`` 会 NameError,导致 500 完全没 trace_id / 耗时。
    修复后:try/except/finally 重写 + 异常时构造 500 JSONResponse 带
    ``X-Request-Id`` + ``X-Response-Time-Ms``,trace_id 始终可达。
    Body 仍带 ``error_id``(SEC-11 兼容)。
    """
    from fastapi.testclient import TestClient

    app = _make_app_with_boom_route()
    # raise_server_exceptions=False:不让 TestClient 把 server 端异常
    # 重新抛到测试里(我们要看的是 500 响应本身)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/__boom", headers={"X-Request-Id": "trace-abc-123"})
    assert r.status_code == 500
    # 关键断言:trace_id 头还在
    assert r.headers.get("X-Request-Id") == "trace-abc-123"
    # X-Response-Time-Ms 也应设置
    assert "X-Response-Time-Ms" in r.headers
    # 耗时是数字
    assert r.headers["X-Response-Time-Ms"].isdigit()
    # SEC-11 兼容:body 应含 error_id
    body = r.json()
    assert body.get("error_id") == "trace-abc-123"
    assert body.get("request_id") == "trace-abc-123"
    assert body.get("detail") == "internal server error"


def test_metrics_middleware_success_path_still_sets_response_time():
    """正常路径(200)也必须带 ``X-Response-Time-Ms``(防止 finally 重构后回归)。"""
    from fastapi.testclient import TestClient

    app = _make_app_with_boom_route()
    client = TestClient(app)
    r = client.get("/__ok", headers={"X-Request-Id": "trace-ok-001"})
    assert r.status_code == 200
    assert r.headers.get("X-Request-Id") == "trace-ok-001"
    assert "X-Response-Time-Ms" in r.headers


def test_metrics_middleware_dispatch_handles_exception_gracefully():
    """直接调 ``MetricsMiddleware.dispatch``,验证异常路径不会 NameError。

    修复前:except 后访问 ``response.headers[...]`` → NameError(原 bug)。
    修复后:dispatch 在异常路径上直接返回构造好的 500 JSONResponse,
    不再依赖可能未赋值的 ``response`` 局部变量。同时 body 包含
    ``error_id``(与 ``errors.register_error_handlers`` 兼容),header
    包含 ``X-Request-Id`` 和 ``X-Response-Time-Ms``。
    """
    import json

    from openclaw.gateway.app import MetricsMiddleware
    from starlette.requests import Request

    # 造一个会抛错的 call_next
    async def boom_call_next(_request):
        raise RuntimeError("inner raised")

    # 造一个最小 request
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/__x",
        "headers": [],
        "query_string": b"",
    }
    request = Request(scope)
    # 显式设 request_id(模拟 RequestIDMiddleware 的前置行为)
    request.state.request_id = "trace-direct-001"

    md = MetricsMiddleware(app=None)  # type: ignore[arg-type]
    # 应返回 500 JSONResponse,不是 NameError
    response = asyncio.run(md.dispatch(request, boom_call_next))
    assert response.status_code == 500
    # 关键:trace_id 头从 request.state 取到了
    assert response.headers.get("X-Request-Id") == "trace-direct-001"
    assert "X-Response-Time-Ms" in response.headers
    # body 应含 error_id / request_id / detail(SEC-11 兼容)
    body = json.loads(response.body)
    assert body.get("detail") == "internal server error"
    assert body.get("error_id") == "trace-direct-001"
    assert body.get("request_id") == "trace-direct-001"


def test_metrics_middleware_dispatch_auto_generates_error_id():
    """``request.state.request_id`` 未设时,MetricsMiddleware 应自动生成。

    Phase 25 / b8:即使上游 RequestIDMiddleware 没运行(单元测试 / 罕见
    极端路径),MetricsMiddleware 也不应让 trace_id 字段为空 — 兜底用
    ``uuid.uuid4().hex[:12]``。"""
    import json

    from openclaw.gateway.app import MetricsMiddleware
    from starlette.requests import Request

    async def boom_call_next(_request):
        raise RuntimeError("inner raised")

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/__x",
        "headers": [],
        "query_string": b"",
    }
    request = Request(scope)
    # 不设 request.state.request_id

    md = MetricsMiddleware(app=None)  # type: ignore[arg-type]
    response = asyncio.run(md.dispatch(request, boom_call_next))
    assert response.status_code == 500
    # 自动生成的 id 应在 header / body 里都出现,且 12 字符 hex
    rid = response.headers.get("X-Request-Id")
    assert rid is not None and len(rid) == 12
    body = json.loads(response.body)
    assert body.get("error_id") == rid
    assert body.get("request_id") == rid


# ────────────────────────────────────────────────────────────────
# 2. PlanExecutor local_failed — 锁保护下并发安全
# ────────────────────────────────────────────────────────────────


def test_executor_local_failed_thread_safe_all_success():
    """100 并发 ``_run_one`` 全成功 → plan finished,无 SKIPPED/FAILED。

    锁保护下应正常运行,不应出现因锁死锁导致挂起。
    """
    from openclaw.agent import Plan, PlanExecutor, PlanStep, StepKind, StepStatus

    n = 100

    async def on_llm_ok(prompt, step):
        # 短暂 sleep 制造 yield 点,让调度器有机会穿插
        await asyncio.sleep(0)
        return f"ok_{step.id}"

    ex = PlanExecutor(on_llm=on_llm_ok, max_parallel=8)
    plan = Plan(steps=[
        PlanStep(id=f"s{i}", name=f"s{i}", kind=StepKind.LLM, target="t", critical=True)
        for i in range(n)
    ])
    res = asyncio.run(ex.run(plan))
    assert res.finished
    assert len(res.steps) == n
    assert all(r.status == StepStatus.DONE for r in res.steps)


def test_executor_local_failed_thread_safe_critical_short_circuit():
    """1 个 critical 失败 + 99 个依赖它的 critical step →
    第 1 个 FAILED,其余 99 个 SKIPPED。

    锁保护下,critical 失败信号可靠传给同层 / 下层,SKIPPED 数稳定。
    """
    from openclaw.agent import Plan, PlanExecutor, PlanStep, StepKind, StepStatus

    async def on_llm_fail(prompt, step):
        await asyncio.sleep(0)
        if step.id == "boom":
            raise RuntimeError("boom")
        return f"ok_{step.id}"

    n_deps = 99
    ex = PlanExecutor(on_llm=on_llm_fail, max_parallel=8)
    plan = Plan(steps=[
        # layer 0
        PlanStep(id="boom", name="boom", kind=StepKind.LLM, target="t",
                 max_retries=0, critical=True),
        # layer 1 — 99 个依赖 boom
        *[
            PlanStep(id=f"d{i}", name=f"d{i}", kind=StepKind.LLM, target="t",
                     critical=True, depends_on=["boom"])
            for i in range(n_deps)
        ],
    ])
    res = asyncio.run(ex.run(plan))
    assert not res.finished
    statuses = {r.step_id: r.status for r in res.steps}
    # boom FAILED
    assert statuses["boom"] == StepStatus.FAILED
    # 99 个 dep 应被 SKIPPED(不会跑 on_llm)
    skipped = [sid for sid, st in statuses.items() if sid != "boom" and st == StepStatus.SKIPPED]
    assert len(skipped) == n_deps
    # 总数 == 1 + 99 = 100
    assert len(res.steps) == 1 + n_deps


def test_executor_local_failed_lock_no_race_100_concurrent():
    """100 并发 critical step 全失败 → 锁不挂起,结果一致。

    锁保护下:
    - 没有死锁
    - plan 标记为 not finished
    - 至少 1 个 FAILED(实际有 1 个 — 其它 99 在 _run_one 阶段都进入
      ``_exec_step`` 并 raise,但 for 循环 ``break`` 在第一个 FAILED,
      所以 res.steps 只记录 1 个)
    """
    from openclaw.agent import Plan, PlanExecutor, PlanStep, StepKind, StepStatus

    n = 100

    async def on_llm_fail(prompt, step):
        await asyncio.sleep(0)  # 强制 yield
        raise RuntimeError("always fail")

    ex = PlanExecutor(on_llm=on_llm_fail, max_parallel=n)
    plan = Plan(steps=[
        PlanStep(id=f"s{i:03d}", name=f"s{i:03d}", kind=StepKind.LLM,
                 target="t", max_retries=0, critical=True)
        for i in range(n)
    ])
    # 跑完不应挂起(如果锁用错会卡死 → 测试超时)
    res = asyncio.run(asyncio.wait_for(ex.run(plan), timeout=10.0))
    assert not res.finished
    # 至少 1 个 FAILED
    failed = [r for r in res.steps if r.status == StepStatus.FAILED]
    assert failed, "expected at least one FAILED step"
    # plan 错误信息
    assert res.error and "boom" not in res.error  # 我们没在 plan 里加 "boom"
    assert "plan failed at step" in res.error
    # 没有 step 误标 DONE
    for r in res.steps:
        assert r.status != StepStatus.DONE, f"{r.step_id} should not be DONE"


# ────────────────────────────────────────────────────────────────
# 3. AgentJournal.reflect — 连续空行归一 + seen set 去重
# ────────────────────────────────────────────────────────────────


@dataclass
class _FakeResponse:
    content: str
    iterations: int
    tool_calls: list
    session_id: str


def test_journal_reflect_dedupes_consecutive_blank_lines(tmp_path: Path):
    """``reflect`` 写入文件时,5 个连续空行 → 1 个空行。

    修复前:``reflect`` 体里的 for 循环是死代码,且整篇 markdown 没归一
    连续空行 — 5 行空白照原样落盘。
    修复后:``_collapse_blank_lines`` 用状态机压连续空行,5 个空行
    被压成 1 个。
    """
    from openclaw.agent.journal import AgentJournal

    j = AgentJournal(root=tmp_path / "j")
    # 5 个连续空行 + 一行内容 + 5 个连续空行(模拟 chat transcript 里的
    # 大量空行被原样落进 user_message)
    user_with_blanks = "line1\n\n\n\n\nline2\n\n\n\n\nline3"
    resp = _FakeResponse(
        content="reply",
        iterations=1,
        tool_calls=[],
        session_id="sess_blanks",
    )
    e = j.record_session(
        session_id="sess_blanks",
        user_message=user_with_blanks,
        response=resp,
    )
    asyncio.run(j.reflect(e))

    files = list((tmp_path / "j").rglob("sess_*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")

    # 检测:任何位置出现连续 >=3 个 \n 都应被压成最多 2 个 \n
    long_blank_runs = re.findall(r"\n[ \t]*\n[ \t]*\n", text)
    assert not long_blank_runs, (
        f"expected no runs of >=2 blank lines, found: {long_blank_runs!r}\n"
        f"--- file content ---\n{text}\n--- end ---"
    )


def test_journal_reflect_dedupes_repeated_reflection(tmp_path: Path):
    """``reflect`` 多次调,同一反思文本应被去重(seen set 生效)。

    修复前:死代码 for 循环没副作用,真正去重是循环外的
    ``if reflection not in tail``,这一行其实是有用的 → 但死代码容易让
    后续维护者误以为已有去重而漏修。修复后:用 seen set 显式跟踪已
    处理反思,并在 tail 检查前先 mark。
    """
    from openclaw.agent.journal import AgentJournal

    class StaticReflector:
        """固定返回同一段反思,模拟 LLM 重复调用的场景。"""
        def __init__(self, text: str) -> None:
            self.text = text

        async def reflect(self, entry):  # H4: async
            return self.text

    fixed = "# 固定反思\n\n这是一段不变化的反思内容。"
    j = AgentJournal(root=tmp_path / "j", reflector=StaticReflector(fixed))
    resp = _FakeResponse("reply", 1, [], "sess_repeat")
    e = j.record_session(
        session_id="sess_repeat",
        user_message="hi",
        response=resp,
    )
    # 调 reflect 三次(模拟重复触发,例如 reflection 重试)
    for _ in range(3):
        asyncio.run(j.reflect(e))

    files = list((tmp_path / "j").rglob("sess_*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    # 同一段反思只应出现 1 次(去重生效)
    assert text.count(fixed) == 1, (
        f"reflection should be deduped, found {text.count(fixed)} copies\n"
        f"--- file content ---\n{text}\n--- end ---"
    )


def test_journal_collapse_blank_lines_helper():
    """直接验证 ``_collapse_blank_lines`` 辅助函数的行为。"""
    from openclaw.agent.journal import AgentJournal

    # 5 个连续空行 → 1 个空行
    text = "a\n\n\n\n\nb"
    out = AgentJournal._collapse_blank_lines(text)
    assert out == "a\n\nb", f"unexpected collapse: {out!r}"

    # 多个独立空行段各自保留 1 个
    text = "a\n\n\nb\n\n\n\nc"
    out = AgentJournal._collapse_blank_lines(text)
    assert out == "a\n\nb\n\nc", f"unexpected collapse: {out!r}"

    # 没有空行 → 原样
    text = "a\nb\nc"
    out = AgentJournal._collapse_blank_lines(text)
    assert out == "a\nb\nc", f"unexpected: {out!r}"

    # 单个空行保留
    text = "a\n\nb"
    out = AgentJournal._collapse_blank_lines(text)
    assert out == "a\n\nb", f"unexpected: {out!r}"


# ────────────────────────────────────────────────────────────────
# (可选)烟测:跑完所有修复后,既有测试不应该挂
# ────────────────────────────────────────────────────────────────


def test_smoke_all_three_fixes_coexist():
    """三处修复同时生效时,基本 flow 不应被破坏。

    - 创建一个 minimal plan → executor 跑通(验证 lock 没引入死锁)
    - 创建一个 journal entry → reflect 跑通(验证 _collapse_blank_lines
      没破坏 happy path)
    - 启动一个 minimal app + 正常 200 路由(验证 MetricsMiddleware 的
      try/except/finally 重构没影响成功路径)
    """
    from fastapi.testclient import TestClient

    from openclaw.agent import Plan, PlanExecutor, PlanStep, StepKind
    from openclaw.agent.journal import AgentJournal

    # (a) executor smoke
    async def on_llm_ok(prompt, step):
        return f"out_{step.id}"

    ex = PlanExecutor(on_llm=on_llm_ok)
    plan = Plan(steps=[PlanStep(id="x", name="x", kind=StepKind.LLM, target="t")])
    res = asyncio.run(ex.run(plan))
    assert res.finished

    # (b) journal smoke
    smoke_root = ROOT / ".test_journal_concurrency_smoke"
    if smoke_root.exists():
        import shutil
        shutil.rmtree(smoke_root)
    try:
        j = AgentJournal(root=smoke_root)
        resp = _FakeResponse("reply", 1, [], "sess_smoke")
        e = j.record_session(
            session_id="sess_smoke",
            user_message="hi",
            response=resp,
        )
        out = asyncio.run(j.reflect(e))
        assert "反思" in out
    finally:
        if smoke_root.exists():
            import shutil
            shutil.rmtree(smoke_root)

    # (c) gateway smoke
    from openclaw.gateway.app import MetricsMiddleware, RequestIDMiddleware
    from fastapi import FastAPI

    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(MetricsMiddleware)

    @app.get("/smoke")
    async def smoke():
        return {"ok": True}

    client = TestClient(app)
    r = client.get("/smoke", headers={"X-Request-Id": "smoke-001"})
    assert r.status_code == 200
    assert r.headers.get("X-Request-Id") == "smoke-001"
    assert "X-Response-Time-Ms" in r.headers
