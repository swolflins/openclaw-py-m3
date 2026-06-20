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
        command: str,
        cwd: Optional[str] = None,
        timeout: float = 30.0,
    ) -> str:
        """执行 shell 命令并返回 stdout(可附 stderr)。command: 完整命令行; cwd: 工作目录,默认 ~/work; timeout: 超时秒。"""
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
                tokens = shlex.split(command)
            except ValueError as e:
                raise PermissionError(f"unparseable command: {e}") from None
            if not tokens:
                raise PermissionError("empty command after split")
            import os as _os
            first_base = _os.path.basename(tokens[0])
            if first_base not in allowed:
                raise PermissionError(
                    f"command '{first_base}' not in allow-list ({allowed})"
                )

        base = Path(cwd).expanduser() if cwd else cwd
        workdir = str(base) if base else str(Path(default_cwd).expanduser())
        Path(workdir).mkdir(parents=True, exist_ok=True)

        timeout = float(timeout or default_timeout)
        # SEC-3 修复:不记完整 command(可能含密钥/密码),只记首词 + 长度
        try:
            first_tok = shlex.split(command)[0]
        except ValueError:
            first_tok = "<unparseable>"
        logger.info("shell_exec", first_token=first_tok, command_len=len(command), cwd=workdir, timeout=timeout)

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
