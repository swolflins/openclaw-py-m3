"""``openclaw sessions`` —— 会话管理(走 Gateway REST API)。

子命令:
  list              列出所有 session
  show SESSION_ID   查看某 session 的消息历史
  new               创建新 session
  clear SESSION_ID  清空某 session
"""
from __future__ import annotations

from typing import Optional

import typer

from openclaw.cli.context import get_ctx
from openclaw.cli.http import GatewayClient


def _client(url: Optional[str], token: Optional[str]) -> GatewayClient:
    return GatewayClient(url, token)


def _sessions_app() -> typer.Typer:
    s_app = typer.Typer(help="会话管理(走 Gateway /v1/sessions)", no_args_is_help=True)

    @s_app.command("list")
    def sessions_list(
        ctx: typer.Context,
        url: Optional[str] = typer.Option(None, "--url", help="gateway 地址(默认 http://127.0.0.1:8088)"),
        token: Optional[str] = typer.Option(None, "--token", help="gateway 鉴权 token"),
    ) -> None:
        """列出所有 session。"""
        cli_ctx = get_ctx(ctx.obj)
        data = _client(url, token).get("/v1/sessions")
        sessions = data if isinstance(data, list) else data.get("sessions", []) if isinstance(data, dict) else []
        rows = []
        for s in sessions:
            if isinstance(s, dict):
                rows.append([s.get("session_id", s.get("id", "?")), s.get("messages", s.get("count", "?"))])
            else:
                rows.append([str(s), ""])
        cli_ctx.output.table(["session_id", "messages"], rows, title="sessions")

    @s_app.command("show")
    def sessions_show(
        ctx: typer.Context,
        session_id: str = typer.Argument(..., help="session id"),
        url: Optional[str] = typer.Option(None, "--url"),
        token: Optional[str] = typer.Option(None, "--token"),
        k: int = typer.Option(20, "--k", help="返回最近 k 条消息"),
    ) -> None:
        """查看某 session 的消息历史。"""
        cli_ctx = get_ctx(ctx.obj)
        data = _client(url, token).get(f"/v1/sessions/{session_id}/messages", params={"k": k})
        msgs = data if isinstance(data, list) else data.get("messages", []) if isinstance(data, dict) else []
        rows = []
        for m in msgs:
            if isinstance(m, dict):
                rows.append([m.get("role", "?"), str(m.get("content", ""))[:80]])
        cli_ctx.output.table(["role", "content"], rows, title=f"session {session_id}")

    @s_app.command("new")
    def sessions_new(
        ctx: typer.Context,
        session_id: Optional[str] = typer.Option(None, "--session-id", help="指定 id(默认随机)"),
        url: Optional[str] = typer.Option(None, "--url"),
        token: Optional[str] = typer.Option(None, "--token"),
    ) -> None:
        """创建新 session。"""
        cli_ctx = get_ctx(ctx.obj)
        import uuid

        sid = session_id or f"cli_{uuid.uuid4().hex[:12]}"
        data = _client(url, token).post("/v1/sessions", json_body={"session_id": sid})
        cli_ctx.output.success(f"已创建 session: {sid}")
        if data:
            cli_ctx.output.print(data)

    @s_app.command("clear")
    def sessions_clear(
        ctx: typer.Context,
        session_id: str = typer.Argument(..., help="session id"),
        url: Optional[str] = typer.Option(None, "--url"),
        token: Optional[str] = typer.Option(None, "--token"),
    ) -> None:
        """清空某 session 的消息历史。"""
        cli_ctx = get_ctx(ctx.obj)
        _client(url, token).delete(f"/v1/sessions/{session_id}")
        cli_ctx.output.success(f"已清空 session: {session_id}")

    return s_app


def register(app: typer.Typer) -> None:
    app.add_typer(_sessions_app(), name="sessions")


__all__ = ["register"]
