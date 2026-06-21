"""``openclaw channels`` —— 渠道管理(走 Gateway REST)。

子命令:
  list              列出 gateway 已注册的运行中渠道
  send              通过指定 channel 主动发一条消息
  types             列出内置可用的 channel 类型(不需 gateway)
"""
from __future__ import annotations

from typing import Optional

import typer

from openclaw.cli.context import get_ctx
from openclaw.cli.http import GatewayClient

# 内置 channel 类型(从模块定义收集,不需实例化)
_BUILTIN_CHANNEL_TYPES = ["cli", "echo", "lark", "telegram", "discord", "slack", "whatsapp", "signal", "imessage"]


def _channels_app() -> typer.Typer:
    ch_app = typer.Typer(help="渠道管理:list / send / types", no_args_is_help=True)

    @ch_app.command("list")
    def channels_list(
        ctx: typer.Context,
        url: Optional[str] = typer.Option(None, "--url"),
        token: Optional[str] = typer.Option(None, "--token"),
    ) -> None:
        """列出 gateway 已注册的运行中渠道。"""
        cli_ctx = get_ctx(ctx.obj)
        data = GatewayClient(url, token).get("/v1/channels")
        channels = data.get("channels", []) if isinstance(data, dict) else []
        rows = [
            [c.get("name", "?"), c.get("running", "?"), c.get("agent_attached", "?"), c.get("auto_reply_attached", "?")]
            for c in channels
        ]
        cli_ctx.output.table(["name", "running", "agent", "auto_reply"], rows, title=f"运行中渠道 ({len(channels)})")

    @ch_app.command("send")
    def channels_send(
        ctx: typer.Context,
        name: str = typer.Argument(..., help="目标 channel 名"),
        text: str = typer.Argument(..., help="消息文本"),
        session: str = typer.Option("default", "--session", "-s", help="session id"),
        url: Optional[str] = typer.Option(None, "--url"),
        token: Optional[str] = typer.Option(None, "--token"),
    ) -> None:
        """通过指定 channel 主动发一条消息。"""
        cli_ctx = get_ctx(ctx.obj)
        data = GatewayClient(url, token).post(
            "/v1/channels/send",
            json_body={"name": name, "session_id": session, "text": text},
        )
        cli_ctx.output.success(f"已通过 {name} 发送消息")
        if data:
            cli_ctx.output.print(data)

    @ch_app.command("types")
    def channels_types(ctx: typer.Context) -> None:
        """列出内置可用的 channel 类型(不需 gateway)。"""
        cli_ctx = get_ctx(ctx.obj)
        rows = [[t, "内置" if t in ("cli", "echo") else "需 extras"] for t in _BUILTIN_CHANNEL_TYPES]
        cli_ctx.output.table(["name", "备注"], rows, title=f"内置 channel 类型 ({len(rows)})")

    return ch_app


def register(app: typer.Typer) -> None:
    app.add_typer(_channels_app(), name="channels")


__all__ = ["register"]
