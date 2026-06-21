"""``openclaw agent`` —— 单轮 Agent 调用。

对齐上游 ``openclaw agent --to --message --deliver``。
直接走本地 AgentLoop.handle(不经 gateway),适合脚本化调用。

  openclaw agent --message "你好"
  openclaw agent -m "查一下天气" --session my-sess
"""
from __future__ import annotations

import asyncio
from typing import Optional

import typer

from openclaw.cli.context import get_ctx
from openclaw.cli.errors import CLIError


def agent(
    ctx: typer.Context,
    message: str = typer.Option(..., "--message", "-m", help="用户消息文本"),
    session: Optional[str] = typer.Option(None, "--session", "-s", help="session id(默认随机)"),
    provider: Optional[str] = typer.Option(None, "--provider", help="指定 provider"),
    system_prompt: Optional[str] = typer.Option(None, "--system-prompt", help="覆盖 system prompt"),
    deliver: bool = typer.Option(False, "--deliver", help="通过 channel 发送回复(需 gateway)"),
    deliver_channel: Optional[str] = typer.Option(None, "--to", help="deliver 目标 channel 名"),
) -> None:
    """单轮 Agent 调用(本地 ReAct 循环)。"""
    cli_ctx = get_ctx(ctx.obj)

    from openclaw.cli.factory import build_agent_loop
    from openclaw.core.errors import ConfigError

    try:
        loop, cfg = build_agent_loop(cli_ctx.config_path, provider_override=provider)
    except ConfigError as e:
        raise CLIError(
            f"启动失败: {e}", exit_code=2,
            hint="请先配置 provider:openclaw config set providers '[...]'",
        ) from e

    if system_prompt:
        loop.system_prompt = system_prompt

    import uuid

    session_id = session or f"agent_{uuid.uuid4().hex[:8]}"

    async def _go():
        return await loop.handle(session_id, message)

    resp = asyncio.run(_go())

    data = {
        "session_id": resp.session_id,
        "content": resp.content,
        "iterations": resp.iterations,
        "tool_calls": [
            {"name": tc.name, "arguments": dict(tc.arguments or {})} for tc in resp.tool_calls
        ],
    }
    cli_ctx.output.print(data, title="回复")

    # --deliver:通过 gateway 把回复发到指定 channel
    if deliver:
        if not deliver_channel:
            raise CLIError("--deliver 需配合 --to <channel> 指定目标 channel", exit_code=2)
        from openclaw.cli.http import GatewayClient

        client = GatewayClient()
        client.post("/v1/channels/send", json_body={
            "name": deliver_channel,
            "session_id": session_id,
            "text": resp.content,
        })
        cli_ctx.output.success(f"已通过 {deliver_channel} 发送回复")


def register(app: typer.Typer) -> None:
    app.command("agent")(agent)


__all__ = ["agent", "register"]
