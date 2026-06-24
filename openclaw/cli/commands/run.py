"""``openclaw run`` —— 启动交互式 REPL 或单轮调用。

  openclaw run                  启动 CLI REPL(每行一条消息,:exit 退出)
  openclaw run --once "问题"    单轮调用,输出回复(支持 --json)

复用内部 CLIChannel(openclaw.channels.cli)+ AgentLoop。
"""
from __future__ import annotations

import asyncio
from typing import Optional

import typer

from openclaw.cli.context import get_ctx
from openclaw.cli.errors import CLIError


def _run_repl(loop, session_id: str) -> None:
    """启动 CLIChannel REPL。"""
    from openclaw.channels.cli import CLIChannel

    channel = CLIChannel(loop, session_id=session_id)
    try:
        asyncio.run(channel.start())
    except KeyboardInterrupt:
        pass


def _run_once(loop, session_id: str, text: str, output) -> None:
    """单轮调用,输出结果(支持 --json)。"""

    async def _go():
        return await loop.handle(session_id, text)

    resp = asyncio.run(_go())
    data = {
        "session_id": resp.session_id,
        "content": resp.content,
        "iterations": resp.iterations,
        "tool_calls": [
            {"name": tc.name, "arguments": dict(tc.arguments or {})} for tc in resp.tool_calls
        ],
    }
    output.print(data, title="回复")


def run(
    ctx: typer.Context,
    once: Optional[str] = typer.Option(
        None, "--once", "-o", help="非交互单轮模式:传入问题文本,输出回复后退出(支持 --json)"
    ),
    provider: Optional[str] = typer.Option(None, "--provider", help="指定使用的 provider(覆盖默认)"),
    session: Optional[str] = typer.Option(None, "--session", help="指定 session id(默认随机)"),
    system_prompt: Optional[str] = typer.Option(
        None, "--system-prompt", help="覆盖配置中的 system_prompt"
    ),
) -> None:
    """启动 Agent REPL 或单轮调用。"""
    cli_ctx = get_ctx(ctx.obj)

    from openclaw.cli.factory import build_agent_loop
    from openclaw.core.errors import ConfigError

    try:
        loop, cfg = build_agent_loop(cli_ctx.config_path, provider_override=provider)
    except ConfigError as e:
        raise CLIError(
            f"启动失败: {e}",
            exit_code=2,
            hint="请先配置 provider:openclaw config set providers '[{name: openai_compat, model: ..., api_key: ...}]'",
        ) from e

    # 覆盖 system_prompt
    if system_prompt:
        loop.system_prompt = system_prompt

    import uuid

    session_id = session or f"cli_{uuid.uuid4().hex[:8]}"

    if once:
        _run_once(loop, session_id, once, cli_ctx.output)
    else:
        # REPL 模式:--json 不生效(CLIChannel 内部 print),提示用户
        if cli_ctx.output.mode == "json":
            cli_ctx.output.warn("REPL 模式下 --json 不生效,如需结构化输出请用 --once")
        _run_repl(loop, session_id)


def register(app: typer.Typer) -> None:
    app.command("run")(run)


__all__ = ["run", "register"]
