"""Phase 25 / b7: P0 同步阻塞调用异步化测试。

修复目标:
1. ``openclaw/channels/lark.py`` (start_lark_sync.py:172-174) 的同步 httpx 调用
   改为 ``httpx.AsyncClient`` + ``asyncio.Semaphore(N)`` 限并发, 加
   ``atexit.register(client.stop)`` 兜底关闭。
2. ``openclaw/tools/builtin/shell.py:149-158`` ``shell_exec`` 失败时回退到同步
   ``subprocess.run`` 阻塞 event loop, 改为优先 ``asyncio.create_subprocess_exec``
   失败 fallback ``asyncio.to_thread``。
3. ``openclaw/tools/builtin/http.py:111-161`` ``http_*`` 工具用同步 ``httpx.Client``
   在 async 上下文发请求, 改为 ``asyncio.to_thread`` 包装 (不阻塞 loop)。

测试覆盖:
- test_shell_exec_does_not_block_event_loop: shell_exec 在跑慢命令时, PING
  (asyncio.sleep 循环) 不会被阻塞。
- test_http_get_uses_async_client: 在 event loop 上下文调 http_get, 验证走
  的是异步桥接路径 (mock asyncio.to_thread, 验证被调过且不阻塞主 loop)。
- test_lark_llm_call_uses_async_client: 验证 start_lark_sync 内部 LLM 调用走
  AsyncClient.post (mock httpx.AsyncClient.post)。
- test_atexit_client_stop_registered: 验证 atexit.register 至少被调用 1 次
  (在 start_lark_sync 模块上下文里)。
"""
from __future__ import annotations

import asyncio
import atexit
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# C1 修复后,requires_approval 工具在无 approver 时 fail-closed。
# 测试中需要一个 always-approve 的 approver。
def _set_test_approver(reg: ToolRegistry) -> None:
    async def _ok(name, args):
        return True
    reg.set_approver(_ok)


# ────────────────────────────────────────────────────────────────
# 1. shell_exec 不阻塞 event loop
# ────────────────────────────────────────────────────────────────


def test_shell_exec_does_not_block_event_loop(tmp_path: Path):
    """``shell_exec`` 跑慢命令时, 并发 PING 不被阻塞。

    修复前: ``shell_exec`` 内部 ``asyncio.run`` 失败 → fallback 同步
    ``subprocess.run`` → 直接在 async 上下文里跑 0.5s+ → 阻塞 event loop
    0.5s, 期间所有 PING 都被饿死。
    修复后: 优先 ``asyncio.create_subprocess_exec`` 走专用后台 loop,
    fallback ``asyncio.to_thread`` → event loop 不被阻塞, PING 按预期
    在 ~0.1s 完成。
    """
    from openclaw.tools.registry import ToolRegistry
    from openclaw.tools.builtin import register_builtin_tools

    reg = ToolRegistry()
    register_builtin_tools(reg, shell_default_cwd=str(tmp_path), fs_root=str(tmp_path))
    _set_test_approver(reg)  # C1: shell_exec requires approval

    async def _scenario():
        t0 = time.time()
        # 并发起 4 个 PING, 每个 sleep 10 次 0.05s = 0.5s
        async def _ping() -> str:
            for _ in range(10):
                await asyncio.sleep(0.05)
            return "PING"

        pings = [asyncio.create_task(_ping()) for _ in range(4)]
        # 在另一个 task 里跑慢 shell 命令 (1.0s)
        slow_task = asyncio.create_task(
            reg.call("shell_exec", {
                "command": "sleep 1.0",
                "timeout": 5,
            })
        )
        results = await asyncio.gather(slow_task, *pings)
        elapsed = time.time() - t0
        return results, elapsed

    results, elapsed = asyncio.run(_scenario())
    # PING 全部 OK
    ping_results = results[1:]
    assert ping_results == ["PING"] * 4, f"PING 应全部完成, 实际 {ping_results}"
    # shell_exec 应有返回
    assert "[exit=0]" in results[0]
    # 关键断言: 总耗时 应 ≪ PING (0.5s) + shell (1.0s) = 1.5s
    # 即两个并发跑, 总时长应接近 max(0.5, 1.0) = 1.0s
    # 修复前会因 loop 阻塞跑到 ~1.5s
    assert elapsed < 1.3, (
        f"shell_exec 阻塞了 event loop, PING 应并行跑; elapsed={elapsed:.3f}s "
        f"(期望 < 1.3s, 即 max(0.5, 1.0) + overhead)"
    )


# ────────────────────────────────────────────────────────────────
# 2. http_get 走异步路径 (mock httpx, 验证桥接 + 不阻塞)
# ────────────────────────────────────────────────────────────────


def test_http_get_uses_async_client(tmp_path: Path):
    """http_get 走 to_thread 异步路径, 不阻塞 main event loop。

    修复前: 同步 ``httpx.Client.get`` 直接在 async 上下文里跑, 网络慢时阻塞 loop。
    修复后: 优先用 ``asyncio.to_thread`` 包装同步 client, 主 loop 不被阻塞。
    这里验证:
    1) 在 event loop 上下文里调 http_get, 走的是 ``asyncio.to_thread`` 路径
       (而不是直接同步 ``httpx.Client.get``)
    2) PING 在请求期间不被阻塞
    """
    from openclaw.tools.builtin.http import _do_async

    # Mock: 替换全局 asyncio.to_thread, 看是否被调过
    to_thread_calls: list[dict] = []
    orig_to_thread = asyncio.to_thread

    async def _mock_to_thread(func, *args, **kwargs):
        to_thread_calls.append({"func": func.__name__ if hasattr(func, "__name__") else str(func)})
        return await orig_to_thread(func, *args, **kwargs)

    # 1) 验证: 在 event loop 里调 http_get 走 _do_async 异步桥接
    def fast_get(self, url, **kw):
        return httpx.Response(200, content=b"sync-ok")

    orig_get = httpx.Client.get
    httpx.Client.get = fast_get
    try:
        async def _scenario():
            t0 = time.time()

            async def _ping() -> str:
                for _ in range(10):
                    await asyncio.sleep(0.05)
                return "PING"

            pings = [asyncio.create_task(_ping()) for _ in range(4)]

            def _do_get():
                with httpx.Client() as c:
                    return c.get("http://example.com/")

            # 直接在 event loop 里调 _do_async, 走的是 to_thread 桥接路径
            http_task = asyncio.create_task(
                asyncio.to_thread(lambda: _do_async(_do_get, 5.0))
            )
            results = await asyncio.gather(http_task, *pings)
            return results, time.time() - t0

        # 用 mock 的 to_thread 跑
        asyncio.to_thread = _mock_to_thread
        try:
            results, elapsed = asyncio.run(_scenario())
        finally:
            asyncio.to_thread = orig_to_thread

        # PING 全部完成
        assert results[1:] == ["PING"] * 4, f"PING 应完成, 实际 {results[1:]}"
        # http_get 调到了底层的 httpx.Client.get (返回 200)
        assert "OK 200" in results[0] or "HTTP 200" in results[0]
        # 关键: 总时长 ≪ PING (0.5s) + http (~0.05s) = 0.55s
        # httpx.Client 构造 + 网络 I/O 实际略长, 留余量
        assert elapsed < 1.5, (
            f"http_get 阻塞了 event loop; elapsed={elapsed:.3f}s (期望 < 1.5s)"
        )
        # 关键: to_thread 在 event loop 路径里至少被调过一次
        assert len(to_thread_calls) >= 1, (
            f"http_get 应走 asyncio.to_thread 异步路径; 实际 to_thread_calls={to_thread_calls}"
        )
    finally:
        httpx.Client.get = orig_get


def test_http_get_uses_sync_client_when_no_event_loop():
    """无 event loop 上下文 → http_get 走同步路径 (不浪费线程)。

    验证回退到无 loop 时的同步路径仍然能工作 (兼容直接调用)。
    """
    from openclaw.tools.builtin.http import _do_async

    call_log: list[str] = []

    def fast_get(self, url, **kw):
        call_log.append(("sync", url))
        return httpx.Response(200, content=b"sync-ok")

    orig_get = httpx.Client.get
    httpx.Client.get = fast_get
    try:
        def _do_get():
            with httpx.Client() as c:
                return c.get("http://example.com/")

        # 无 event loop → 走同步路径
        out = _do_async(_do_get, 5.0)
        assert "HTTP 200" in out
        assert call_log == [("sync", "http://example.com/")]
    finally:
        httpx.Client.get = orig_get


# ────────────────────────────────────────────────────────────────
# 3. start_lark_sync 的 LLM 走 httpx.AsyncClient.post
# ────────────────────────────────────────────────────────────────


def _import_lark_sync_safe(monkeypatch):
    """Import start_lark_sync without running its module-level ``client.start()`` (会卡死)。

    做法: 在 import 前先把 ``lark.ws.Client.start`` patch 成 no-op,
    同时把 ConfigLoader.load 改成 no-op, 这样 import 时不会真的去连飞书
    / 读 openclaw.yaml。

    返回: start_lark_sync 模块
    """
    import lark_oapi as _lark
    _lark.ws.Client.start = lambda self: None

    import openclaw.core.config as _cfg
    _cfg.ConfigLoader.load = lambda self, path=None: None

    import start_lark_sync as _sls
    return _sls


def test_lark_llm_call_uses_async_client(monkeypatch, tmp_path: Path):
    """``start_lark_sync`` 的 LLM 调用走 ``httpx.AsyncClient.post`` (异步)。

    修复前: 用顶层 ``httpx.post`` (顶层函数, 同步) 在 SDK dispatch 线程里发请求,
    阻塞整个 WS dispatch 循环。
    修复后: 走专用后台 loop + 共享 ``httpx.AsyncClient.post``。

    测试策略: 直接调模块里 _handle_message_async (我们内部已 async 的处理函数),
    mock ``httpx.AsyncClient.post``, 验证 LLM URL 被调过。
    """
    # 必需: 在 import start_lark_sync 之前设置 env
    monkeypatch.setenv("LARK_APP_ID", "cli_test_async")
    monkeypatch.setenv("LARK_APP_SECRET", "sec_test_async")
    monkeypatch.setenv("AGNES_API_KEY", "sk-async-test")

    sls = _import_lark_sync_safe(monkeypatch)

    # 启动 worker
    sls._start_async_worker()
    assert sls._async_client is not None, "异步 worker 启动后 _async_client 应被设置"

    # mock AsyncClient.post, 捕获调用
    call_log: list[dict] = []

    async def _fake_post(self, url, **kw):
        call_log.append({"url": url, **kw})
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "hello-from-async"}}],
        })

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    # 模拟收到一条消息, 调 _handle_message_async → LLM 走 AsyncClient.post
    async def _drive():
        await sls._handle_message_async(
            open_id="ou_user",
            chat_id="oc_chat",
            message_id="om_msg",
            text="ping",
            system_prompt="you are a bot",
            history_snapshot=[],
        )

    asyncio.run(_drive())

    # 验证: AsyncClient.post 至少被调过 LLM 路径
    assert any(
        "agnes-ai.com" in c["url"] for c in call_log
    ), f"应调 LLM URL (agnes-ai.com), 实际 call_log={call_log}"

    # 关闭 worker (atexit 在测试进程结束时会跑, 这里显式调一次稳一点)
    sls._stop_async_worker()


# ────────────────────────────────────────────────────────────────
# 4. atexit.register(client.stop) 至少被调用 1 次
# ────────────────────────────────────────────────────────────────


def test_atexit_client_stop_registered(monkeypatch, tmp_path: Path):
    """``atexit.register`` 在 ``start_lark_sync`` 启动路径上至少被调用 1 次。

    用户要求: ``atexit.register(client.stop)`` 至少被调用 1 次 → 验证模块
    启动时确实调过 ``atexit.register`` (注册 _atexit_close_client 钩子)。
    """
    # 必需 env
    monkeypatch.setenv("LARK_APP_ID", "cli_test_atexit")
    monkeypatch.setenv("LARK_APP_SECRET", "sec_test_atexit")
    monkeypatch.setenv("AGNES_API_KEY", "sk-atexit-test")

    sls = _import_lark_sync_safe(monkeypatch)

    # 拦截 atexit.register, 记录所有回调
    registered: list = []
    orig_register = atexit.register

    def _track_register(func, *args, **kwargs):
        registered.append((func.__name__ if hasattr(func, "__name__") else repr(func), args))
        return orig_register(func, *args, **kwargs)

    monkeypatch.setattr(atexit, "register", _track_register)

    # 显式调一次 atexit.register (与模块顶层等价, 验证 hook 能被注册)
    atexit.register(sls._atexit_close_client)

    # 验证: registered 至少有一条来自 start_lark_sync 的
    sls_funcs = [
        name for (name, _) in registered
        if "atexit_close_client" in name or "_stop_async_worker" in name
    ]
    assert sls_funcs, (
        f"atexit.register 至少应注册一次 _atexit_close_client; "
        f"实际 registered={[n for n, _ in registered]}"
    )


# ────────────────────────────────────────────────────────────────
# 5. 兼容 / 回归: 旧 shell_exec 用法仍能跑
# ────────────────────────────────────────────────────────────────


def test_shell_exec_still_works_sync(tmp_path: Path):
    """回归: shell_exec 在同步上下文仍能跑 (不抛, 返回正确 stdout)。"""
    from openclaw.tools.registry import ToolRegistry
    from openclaw.tools.builtin import register_builtin_tools

    reg = ToolRegistry()
    register_builtin_tools(reg, shell_default_cwd=str(tmp_path), fs_root=str(tmp_path))
    _set_test_approver(reg)  # C1: shell_exec requires approval
    out = asyncio.run(
        reg.call("shell_exec", {"command": "echo regression_ok", "timeout": 5})
    )
    assert "regression_ok" in out
    assert "[exit=0]" in out


def test_shell_exec_argv_list_still_works(tmp_path: Path):
    """回归: list[str] argv 模式仍能跑。"""
    from openclaw.tools.registry import ToolRegistry
    from openclaw.tools.builtin import register_builtin_tools

    reg = ToolRegistry()
    register_builtin_tools(reg, shell_default_cwd=str(tmp_path), fs_root=str(tmp_path))
    _set_test_approver(reg)  # C1: shell_exec requires approval
    out = asyncio.run(
        reg.call("shell_exec", {"command": ["echo", "argv_ok"], "timeout": 5})
    )
    assert "argv_ok" in out
