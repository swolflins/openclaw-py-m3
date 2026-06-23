"""``openclaw health`` —— 健康检查。

对齐上游 openclaw 的顶层 ``health`` 命令,检查 gateway /readyz 与 /healthz。
"""
from __future__ import annotations

from typing import Optional

import typer

from openclaw.cli.context import get_ctx
from openclaw.cli.http import GatewayClient


def health(
    ctx: typer.Context,
    url: Optional[str] = typer.Option(None, "--url", help="gateway 地址"),
    token: Optional[str] = typer.Option(None, "--token", help="gateway token"),
) -> None:
    """检查 OpenClaw gateway 健康状态。"""
    cli_ctx = get_ctx(ctx.obj)
    client = GatewayClient(url, token, timeout=5.0)

    results: dict = {"url": client.base_url}
    healthy = True
    for path in ("/healthz", "/readyz"):
        try:
            data = client.get(path)
            results[path] = data
            if isinstance(data, dict) and data.get("status") not in ("ok", "ready"):
                healthy = False
        except Exception as exc:  # noqa: BLE001
            results[path] = {"error": str(exc)}
            healthy = False

    results["healthy"] = healthy
    cli_ctx.output.print(results, title="openclaw health")


def register(app: typer.Typer) -> None:
    app.command("health")(health)


__all__ = ["health", "register"]
