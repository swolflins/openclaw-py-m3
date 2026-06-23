"""``openclaw tools`` —— 工具管理(走 Gateway REST)。

子命令:
  list         列出已注册工具
  call         调用指定工具
  approver     查询/设置工具审批状态
"""
from __future__ import annotations

from typing import Any, Optional

import typer

from openclaw.cli.context import get_ctx
from openclaw.cli.http import GatewayClient


def _tools_app() -> typer.Typer:
    t_app = typer.Typer(help="工具管理:list / call / approver(走 Gateway /v1/tools)", no_args_is_help=True)

    @t_app.command("list")
    def tools_list(
        ctx: typer.Context,
        url: Optional[str] = typer.Option(None, "--url"),
        token: Optional[str] = typer.Option(None, "--token"),
    ) -> None:
        """列出 gateway 已注册的工具。"""
        cli_ctx = get_ctx(ctx.obj)
        data = GatewayClient(url, token).get("/v1/tools")
        tools = data.get("tools", []) if isinstance(data, dict) else []
        rows = [
            [t.get("name", "?"), t.get("description", "")[:50], t.get("category", "?"), t.get("permission", "?")]
            for t in tools if isinstance(t, dict)
        ]
        cli_ctx.output.table(["name", "description", "category", "permission"], rows, title=f"工具 ({len(tools)})")

    @t_app.command("call")
    def tools_call(
        ctx: typer.Context,
        name: str = typer.Argument(..., help="工具名"),
        arguments: str = typer.Option("{}", "--args", "-a", help="JSON 格式参数"),
        url: Optional[str] = typer.Option(None, "--url"),
        token: Optional[str] = typer.Option(None, "--token"),
    ) -> None:
        """调用指定工具。"""
        import json

        cli_ctx = get_ctx(ctx.obj)
        try:
            args: dict[str, Any] = json.loads(arguments)
        except json.JSONDecodeError as e:
            from openclaw.cli.errors import EXIT_CONFIG, CLIError

            raise CLIError(f"--args 不是合法 JSON: {e}", exit_code=EXIT_CONFIG) from e

        data = GatewayClient(url, token).post(
            "/v1/tools/call",
            json_body={"name": name, "arguments": args},
        )
        cli_ctx.output.print(data, title=f"工具调用: {name}")

    @t_app.command("approver")
    def tools_approver(
        ctx: typer.Context,
        action: str = typer.Argument("status", help="操作:status / approve / reject"),
        request_id: Optional[str] = typer.Option(None, "--id", help="审批请求 id(approve/reject 时必填)"),
        url: Optional[str] = typer.Option(None, "--url"),
        token: Optional[str] = typer.Option(None, "--token"),
    ) -> None:
        """查询/设置工具审批状态。"""
        cli_ctx = get_ctx(ctx.obj)
        if action == "status":
            data = GatewayClient(url, token).get("/v1/tools/approver")
        elif action in ("approve", "reject"):
            if not request_id:
                from openclaw.cli.errors import EXIT_CONFIG, CLIError

                raise CLIError(f"{action} 需要提供 --id", exit_code=EXIT_CONFIG)
            data = GatewayClient(url, token).post(
                "/v1/tools/approver",
                json_body={"request_id": request_id, "decision": action},
            )
        else:
            from openclaw.cli.errors import EXIT_CONFIG, CLIError

            raise CLIError(f"未知操作: {action},支持:status / approve / reject", exit_code=EXIT_CONFIG)
        cli_ctx.output.print(data, title=f"审批: {action}")

    return t_app


def register(app: typer.Typer) -> None:
    app.add_typer(_tools_app(), name="tools")


__all__ = ["register"]
