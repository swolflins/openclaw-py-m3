"""``openclaw status`` —— 聚合状态查看。

对齐上游 openclaw 的顶层 ``status`` 命令,汇总 gateway / channels /
providers / memory 等运行时信息。
"""
from __future__ import annotations

from typing import Optional

import typer

from openclaw.cli.context import get_ctx
from openclaw.cli.http import GatewayClient


def status(
    ctx: typer.Context,
    url: Optional[str] = typer.Option(None, "--url", help="gateway 地址"),
    token: Optional[str] = typer.Option(None, "--token", help="gateway token"),
) -> None:
    """查看 OpenClaw 运行时聚合状态(需 gateway 运行中)。"""
    cli_ctx = get_ctx(ctx.obj)
    client = GatewayClient(url, token, timeout=5.0)

    result: dict = {"url": client.base_url}
    try:
        data = client.get("/metrics")
        result["gateway"] = data
    except Exception as exc:  # noqa: BLE001
        result["gateway_error"] = str(exc)

    try:
        data = client.get("/v1/channels")
        channels = data.get("channels", []) if isinstance(data, dict) else []
        result["channels"] = {"count": len(channels), "names": [c.get("name") for c in channels if isinstance(c, dict)]}
    except Exception as exc:  # noqa: BLE001
        result["channels_error"] = str(exc)

    try:
        data = client.get("/v1/sessions")
        sessions = data.get("sessions", []) if isinstance(data, dict) else []
        result["sessions"] = {"count": len(sessions)}
    except Exception as exc:  # noqa: BLE001
        result["sessions_error"] = str(exc)

    cli_ctx.output.print(result, title="openclaw status")


def register(app: typer.Typer) -> None:
    app.command("status")(status)


__all__ = ["status", "register"]
