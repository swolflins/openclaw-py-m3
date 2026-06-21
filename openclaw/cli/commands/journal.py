"""``openclaw journal`` —— Agent 日记/反思(走 Gateway REST)。

子命令:
  entries          列出日记条目
  weekly           生成本周反思汇总
  soul-proposals   列出 SOUL 改进提案
"""
from __future__ import annotations

from typing import Optional

import typer

from openclaw.cli.context import get_ctx
from openclaw.cli.http import GatewayClient


def _journal_app() -> typer.Typer:
    j_app = typer.Typer(help="Agent 日记/反思(走 Gateway /v1/journal)", no_args_is_help=True)

    @j_app.command("entries")
    def journal_entries(
        ctx: typer.Context,
        limit: int = typer.Option(20, "--limit", "-n", help="返回条数"),
        unread: bool = typer.Option(False, "--unread", help="仅未读"),
        url: Optional[str] = typer.Option(None, "--url"),
        token: Optional[str] = typer.Option(None, "--token"),
    ) -> None:
        """列出日记条目。"""
        cli_ctx = get_ctx(ctx.obj)
        path = "/v1/journal/entries/read" if unread else "/v1/journal/entries"
        data = GatewayClient(url, token).get(path, params={"limit": limit})
        entries = data if isinstance(data, list) else (data.get("entries", []) if isinstance(data, dict) else [])
        rows = [
            [e.get("id", e.get("ts", "?")), e.get("type", "?"), str(e.get("content", ""))[:60]]
            for e in entries if isinstance(e, dict)
        ]
        cli_ctx.output.table(["id/ts", "type", "content"], rows, title=f"日记条目 ({len(entries)})")

    @j_app.command("weekly")
    def journal_weekly(
        ctx: typer.Context,
        url: Optional[str] = typer.Option(None, "--url"),
        token: Optional[str] = typer.Option(None, "--token"),
    ) -> None:
        """生成本周反思汇总。"""
        cli_ctx = get_ctx(ctx.obj)
        data = GatewayClient(url, token).post("/v1/journal/weekly", json_body={})
        cli_ctx.output.print(data, title="周反思")

    @j_app.command("soul-proposals")
    def journal_soul_proposals(
        ctx: typer.Context,
        url: Optional[str] = typer.Option(None, "--url"),
        token: Optional[str] = typer.Option(None, "--token"),
    ) -> None:
        """列出 SOUL 改进提案。"""
        cli_ctx = get_ctx(ctx.obj)
        data = GatewayClient(url, token).get("/v1/journal/soul-proposals")
        proposals = data if isinstance(data, list) else (data.get("proposals", []) if isinstance(data, dict) else [])
        rows = [
            [p.get("id", "?"), p.get("summary", p.get("content", ""))[:60], p.get("status", "?")]
            for p in proposals if isinstance(p, dict)
        ]
        cli_ctx.output.table(["id", "summary", "status"], rows, title=f"SOUL 提案 ({len(proposals)})")

    return j_app


def register(app: typer.Typer) -> None:
    app.add_typer(_journal_app(), name="journal")


__all__ = ["register"]
