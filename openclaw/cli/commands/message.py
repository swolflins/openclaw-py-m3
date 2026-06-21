"""``openclaw message`` —— 消息发送(走 Gateway REST,channels send 的便捷别名)。

对齐上游 ``openclaw message send``。当前实现 send 子命令;
read/edit/delete/thread 等需 gateway 扩展对应端点后补齐。

  openclaw message send --channel lark --text "hello" --session my-sess
"""
from __future__ import annotations

from typing import Optional

import typer

from openclaw.cli.context import get_ctx
from openclaw.cli.http import GatewayClient


def _message_app() -> typer.Typer:
    msg_app = typer.Typer(help="消息发送:send(走 Gateway /v1/channels/send)", no_args_is_help=True)

    @msg_app.command("send")
    def message_send(
        ctx: typer.Context,
        channel: str = typer.Option(..., "--channel", "-c", help="目标 channel 名"),
        text: str = typer.Option(..., "--text", "-t", help="消息文本"),
        session: str = typer.Option("default", "--session", "-s"),
        url: Optional[str] = typer.Option(None, "--url"),
        token: Optional[str] = typer.Option(None, "--token"),
    ) -> None:
        """通过指定 channel 发送消息。"""
        cli_ctx = get_ctx(ctx.obj)
        data = GatewayClient(url, token).post(
            "/v1/channels/send",
            json_body={"name": channel, "session_id": session, "text": text},
        )
        cli_ctx.output.success(f"已通过 {channel} 发送消息")
        if data:
            cli_ctx.output.print(data)

    @msg_app.command("chat")
    def message_chat(
        ctx: typer.Context,
        message: str = typer.Argument(..., help="消息文本"),
        session: str = typer.Option("default", "--session", "-s"),
        url: Optional[str] = typer.Option(None, "--url"),
        token: Optional[str] = typer.Option(None, "--token"),
    ) -> None:
        """通过 gateway /v1/chat 发起一轮对话(Agent 回复)。"""
        cli_ctx = get_ctx(ctx.obj)
        data = GatewayClient(url, token).post(
            "/v1/chat",
            json_body={"session_id": session, "message": message},
        )
        cli_ctx.output.print(data, title="回复")

    return msg_app


def register(app: typer.Typer) -> None:
    app.add_typer(_message_app(), name="message")


__all__ = ["register"]
