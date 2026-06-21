"""``openclaw memory`` —— 记忆管理(走 Gateway REST)。

子命令:
  short list       列出短期记忆(session 历史)
  short add        追加一轮对话
  short clear      ���空某 session 的短期记忆
  long search      长期向量记忆检索
  long add         写入长期记忆
  soul             查看 SOUL 文档
  soul reload      重新加载 SOUL
"""
from __future__ import annotations

from typing import Optional

import typer

from openclaw.cli.context import get_ctx
from openclaw.cli.http import GatewayClient


def _memory_app() -> typer.Typer:
    mem_app = typer.Typer(help="记忆管理(走 Gateway /v1/memory)", no_args_is_help=True)

    # ---- 短期记忆 ----

    @mem_app.command("short")
    def memory_short(
        ctx: typer.Context,
        scope: str = typer.Option("default", "--scope", "-s", help="session scope"),
        k: int = typer.Option(20, "--k", help="返回最近 k 条"),
        url: Optional[str] = typer.Option(None, "--url"),
        token: Optional[str] = typer.Option(None, "--token"),
    ) -> None:
        """查看短期记忆(某 session 的对话历史)。"""
        cli_ctx = get_ctx(ctx.obj)
        data = GatewayClient(url, token).get("/v1/memory/short", params={"scope": scope, "k": k})
        msgs = data if isinstance(data, list) else (data.get("messages", []) if isinstance(data, dict) else [])
        rows = [[m.get("role", "?"), str(m.get("content", ""))[:80]] for m in msgs if isinstance(m, dict)]
        cli_ctx.output.table(["role", "content"], rows, title=f"短期记忆 {scope} ({len(msgs)})")

    @mem_app.command("short-add")
    def memory_short_add(
        ctx: typer.Context,
        scope: str = typer.Option(..., "--scope", "-s"),
        user: str = typer.Option(..., "--user", help="用户消息"),
        assistant: str = typer.Option(..., "--assistant", help="助手回复"),
        url: Optional[str] = typer.Option(None, "--url"),
        token: Optional[str] = typer.Option(None, "--token"),
    ) -> None:
        """追加一轮对话到短期记忆。"""
        cli_ctx = get_ctx(ctx.obj)
        GatewayClient(url, token).post(
            "/v1/memory/short",
            json_body={"scope": scope, "user": user, "assistant": assistant},
        )
        cli_ctx.output.success(f"已追加到 {scope}")

    @mem_app.command("short-clear")
    def memory_short_clear(
        ctx: typer.Context,
        scope: str = typer.Argument(..., help="session scope"),
        url: Optional[str] = typer.Option(None, "--url"),
        token: Optional[str] = typer.Option(None, "--token"),
    ) -> None:
        """清空某 session 的短期记忆。"""
        cli_ctx = get_ctx(ctx.obj)
        GatewayClient(url, token).delete(f"/v1/memory/short/{scope}")
        cli_ctx.output.success(f"已清空短期记忆: {scope}")

    # ---- 长期记忆 ----

    @mem_app.command("long")
    def memory_long(
        ctx: typer.Context,
        query: str = typer.Argument(..., help="检索 query"),
        scope: Optional[str] = typer.Option(None, "--scope", help="限定 scope"),
        top_k: int = typer.Option(5, "--top-k", help="返回条数"),
        url: Optional[str] = typer.Option(None, "--url"),
        token: Optional[str] = typer.Option(None, "--token"),
    ) -> None:
        """长期向量记忆检索。"""
        cli_ctx = get_ctx(ctx.obj)
        params = {"query": query, "top_k": top_k}
        if scope:
            params["scope"] = scope
        data = GatewayClient(url, token).get("/v1/memory/long", params=params)
        items = data if isinstance(data, list) else (data.get("items", []) if isinstance(data, dict) else [])
        rows = [[i.get("text", "")[:80], i.get("scope", "?"), i.get("score", "?")] for i in items if isinstance(i, dict)]
        cli_ctx.output.table(["text", "scope", "score"], rows, title=f"长期记忆检索 ({len(items)})")

    @mem_app.command("long-add")
    def memory_long_add(
        ctx: typer.Context,
        text: str = typer.Option(..., "--text", "-t", help="记忆文本"),
        scope: str = typer.Option("session:default", "--scope", "-s"),
        url: Optional[str] = typer.Option(None, "--url"),
        token: Optional[str] = typer.Option(None, "--token"),
    ) -> None:
        """写入长期记忆。"""
        cli_ctx = get_ctx(ctx.obj)
        GatewayClient(url, token).post(
            "/v1/memory/long",
            json_body={"text": text, "scope": scope},
        )
        cli_ctx.output.success(f"已写入长期记忆到 {scope}")

    # ---- SOUL ----

    @mem_app.command("soul")
    def memory_soul(
        ctx: typer.Context,
        url: Optional[str] = typer.Option(None, "--url"),
        token: Optional[str] = typer.Option(None, "--token"),
    ) -> None:
        """查看 SOUL 文档。"""
        cli_ctx = get_ctx(ctx.obj)
        data = GatewayClient(url, token).get("/v1/memory/soul")
        docs = data if isinstance(data, list) else (data.get("docs", []) if isinstance(data, dict) else [])
        rows = [[d.get("name", "?"), str(d.get("content", ""))[:80]] for d in docs if isinstance(d, dict)]
        cli_ctx.output.table(["name", "content"], rows, title=f"SOUL 文档 ({len(docs)})")

    @mem_app.command("soul-reload")
    def memory_soul_reload(
        ctx: typer.Context,
        url: Optional[str] = typer.Option(None, "--url"),
        token: Optional[str] = typer.Option(None, "--token"),
    ) -> None:
        """重新加载 SOUL 文档。"""
        cli_ctx = get_ctx(ctx.obj)
        data = GatewayClient(url, token).post("/v1/memory/soul/reload", json_body={})
        cli_ctx.output.success("已重新加载 SOUL")
        if data:
            cli_ctx.output.print(data)

    return mem_app


def register(app: typer.Typer) -> None:
    app.add_typer(_memory_app(), name="memory")


__all__ = ["register"]
