"""Shell 工具(子包)。

- shell_exec: 同步执行 shell 命令(带 CWD/超时/白名单)
- 用法:
    from openclaw.tools.builtin.shell import register_shell_tools
    register_shell_tools(registry, default_cwd="~/work", allowed=["ls","cat","echo",...])
"""
from __future__ import annotations

import asyncio
import shlex
from pathlib import Path
from typing import Any, Optional

from openclaw.core.logging import get_logger
from openclaw.tools.registry import ToolCategory, ToolPermission, ToolRegistry

logger = get_logger(__name__)


def register_shell_tools(
    registry: ToolRegistry,
    *,
    default_cwd: Optional[Path | str] = None,
    allowed: Optional[list[str]] = None,
    default_timeout: float = 30.0,
) -> None:
    """注册 shell_exec 工具。

    allowed: 允许的"首词"(二进制名)白名单;None 表示全部允许(配合 approver 用)。
    """
    cwd = Path(default_cwd).expanduser() if default_cwd else Path.cwd()
    cwd.mkdir(parents=True, exist_ok=True)

    @registry.tool(
        category=ToolCategory.SHELL,
        permission=ToolPermission.EXEC,
    )
    def shell_exec(
        command: str,
        cwd: Optional[str] = None,
        timeout: float = 30.0,
    ) -> str:
        """执行 shell 命令并返回 stdout(可附 stderr)。command: 完整命令行; cwd: 工作目录,默认 ~/work; timeout: 超时秒。"""
        base = Path(cwd).expanduser() if cwd else cwd
        workdir = str(base) if base else str(Path(default_cwd).expanduser())
        Path(workdir).mkdir(parents=True, exist_ok=True)

        if allowed is not None:
            first = shlex.split(command)[0] if command else ""
            if first and first not in allowed:
                raise PermissionError(
                    f"command '{first}' not in allow-list ({allowed})"
                )
            # 拒绝 shell 元字符绕过(简单防御)
            for ch in ("&&", "||", ">", "<", "|", ";", "`", "$("):
                if ch in command:
                    raise PermissionError(
                        f"shell metachar {ch!r} not allowed (use shlex-splitted args instead)"
                    )

        timeout = float(timeout or default_timeout)
        logger.info("shell_exec", command=command, cwd=workdir, timeout=timeout)

        try:
            proc = asyncio.run(_run(command, workdir, timeout))
        except RuntimeError:
            # 已在跑的 event loop,fallback to subprocess.run
            import subprocess
            proc = subprocess.run(
                command, shell=True, cwd=workdir, capture_output=True,
                text=True, timeout=timeout,
            )
        return _format_result(proc, command, timeout)


async def _run(command: str, cwd: str, timeout: float) -> "object":
    """异步执行命令(在 to_thread 里跑 subprocess.run)。"""
    import subprocess
    return await asyncio.to_thread(
        subprocess.run, command, shell=True, cwd=cwd,
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
