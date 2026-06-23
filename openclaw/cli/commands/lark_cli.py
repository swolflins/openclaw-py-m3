"""``openclaw channels lark`` —— 飞书 WS 显式控制。

对齐上游 openclaw 对 Phase 34 lark-ws 能力的 CLI 暴露,提供 start /
status / stop 三个子命令。当前实现走 foreground 模式,stop 通过
SIGINT(Ctrl+C) 或进程管理器完成。
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Optional

import typer

from openclaw.cli.context import get_ctx
from openclaw.cli.errors import EXIT_CONFIG, EXIT_DEPENDENCY, CLIError
from openclaw.cli.factory import build_agent_loop, load_config
from openclaw.cli.http import GatewayClient

logger = logging.getLogger(__name__)


def _require_lark() -> None:
    """懒检查 lark-oapi 依赖。"""
    try:
        import lark_oapi  # noqa: F401
    except ImportError as e:
        raise CLIError(
            "缺少可选依赖 [lark],请运行: pip install 'openclaw-py[lark]'",
            exit_code=EXIT_DEPENDENCY,
        ) from e


def _has_lark_creds() -> tuple[bool, dict]:
    """检查飞书凭据/配置是否完整。"""
    app_id = os.environ.get("LARK_APP_ID", "").strip()
    app_secret = os.environ.get("LARK_APP_SECRET", "").strip()
    return bool(app_id) and bool(app_secret), {"app_id": bool(app_id), "app_secret": bool(app_secret)}


def _lark_status_from_gateway(url: Optional[str], token: Optional[str]) -> dict:
    """尝试从 gateway 读取 lark channel 状态。"""
    try:
        data = GatewayClient(url, token, timeout=3.0).get("/v1/channels")
        channels = data.get("channels", []) if isinstance(data, dict) else []
        for ch in channels:
            if isinstance(ch, dict) and ch.get("name") == "lark":
                return {"running": ch.get("running", False), "agent_attached": ch.get("agent_attached", False)}
        return {"running": False, "reason": "gateway 中未注册 lark channel"}
    except Exception as exc:  # noqa: BLE001
        return {"running": False, "reason": f"无法连接 gateway: {exc}"}


def _lark_app() -> typer.Typer:
    lk_app = typer.Typer(help="飞书 Lark WS 控制:start / status / stop", no_args_is_help=True)

    @lk_app.command("status")
    def lark_status(
        ctx: typer.Context,
        url: Optional[str] = typer.Option(None, "--url", help="gateway 地址(可选)"),
        token: Optional[str] = typer.Option(None, "--token", help="gateway token"),
    ) -> None:
        """查看飞书 WS 配置与运行状态。"""
        cli_ctx = get_ctx(ctx.obj)
        creds_ok, creds = _has_lark_creds()
        result: dict = {"creds_ok": creds_ok, "creds": creds}

        if url is not None:
            result["gateway"] = _lark_status_from_gateway(url, token)
        else:
            # 尝试默认 gateway 地址
            result["gateway"] = _lark_status_from_gateway(None, token)

        cli_ctx.output.print(result, title="lark status")

    @lk_app.command("start")
    def lark_start(
        ctx: typer.Context,
        config: Optional[Path] = typer.Option(None, "--config", "-c", help="配置文件路径"),
    ) -> None:
        """前台启动飞书 WS 客户端(阻塞,按 Ctrl+C 停止)。"""
        cli_ctx = get_ctx(ctx.obj)
        _require_lark()

        creds_ok, creds = _has_lark_creds()
        if not creds_ok:
            raise CLIError(
                "未配置飞书凭据,请设置 LARK_APP_ID 与 LARK_APP_SECRET",
                exit_code=EXIT_CONFIG,
                hint="export LARK_APP_ID=cli_xxx && export LARK_APP_SECRET=xxx",
            )

        cfg, _ = load_config(config or cli_ctx.config_path)
        loop, _ = build_agent_loop(config_path=config or cli_ctx.config_path)

        from openclaw.channels.lark import LarkChannel
        from openclaw.config.settings import LarkSettings

        settings = getattr(cfg, "lark", None) or LarkSettings(
            app_id=os.environ["LARK_APP_ID"],
            app_secret=os.environ["LARK_APP_SECRET"],
        )
        channel = LarkChannel(agent_loop=loop, settings=settings)

        async def _run() -> None:
            await channel.start()
            cli_ctx.output.success("飞书 WS 已启动,按 Ctrl+C 停止")
            try:
                await channel._stopped.wait()  # type: ignore[attr-defined]
            except asyncio.CancelledError:
                pass
            finally:
                await channel.stop()

        def _on_signal(sig: int, _frame: object) -> None:
            cli_ctx.output.warn(f"收到信号 {sig},正在停止飞书 WS...")
            channel._stopped.set()  # type: ignore[attr-defined]

        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)
        try:
            asyncio.run(_run())
        except KeyboardInterrupt:
            print("\n已中断", file=sys.stderr)
            sys.exit(130)

    @lk_app.command("stop")
    def lark_stop(ctx: typer.Context) -> None:
        """停止前台运行的飞书 WS 客户端。

        当前实现下 ``channels lark start`` 是前台进程,直接按 Ctrl+C 即停止。
        若通过 systemd/supervisor 托管,请使用对应管理命令。
        """
        cli_ctx = get_ctx(ctx.obj)
        cli_ctx.output.warn("飞书 WS 前台进程请按 Ctrl+C 停止;若已托管请使用服务管理命令")

    return lk_app


def register(app: typer.Typer) -> None:
    app.add_typer(_lark_app(), name="lark")


__all__ = ["register"]
