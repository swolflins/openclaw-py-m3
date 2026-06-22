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
from openclaw.cli.errors import CLIError, EXIT_CONFIG, EXIT_NOT_FOUND
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

    @ch_app.command("login")
    def channels_login(
        ctx: typer.Context,
        name: str = typer.Option(..., "--channel", "-c", help="channel 名(lark/whatsapp/slack/discord/telegram/...)"),
        account: str = typer.Option("default", "--account", "-a", help="account id(多账号场景)"),
        verbose: bool = typer.Option(False, "--verbose", "-v", help="显示详细步骤"),
    ) -> None:
        """链接 channel account(显示 OAuth/pairing URL 或 API key 输入提示)。

        不同 channel 的 login 流程不同,这里统一暴露:
        - lark:        引导用户填 AppID/AppSecret
        - whatsapp:    生成 pairing QR URL(用 `signal-cli` / `baileys` 等后端)
        - slack:       显示 OAuth scope URL
        - discord:     引导用户填 bot token
        - telegram:    引导用户填 bot token
        - signal:      显示 signal-cli 链接命令
        - imessage:    提示仅 macOS
        """
        cli_ctx = get_ctx(ctx.obj)
        if name not in _BUILTIN_CHANNEL_TYPES:
            raise CLIError(
                f"未知 channel: {name!r}(支持: {', '.join(_BUILTIN_CHANNEL_TYPES)})",
                exit_code=EXIT_NOT_FOUND,
            )

        cli_ctx.output.warn(f"=== {name} login (account={account}) ===")

        if name == "lark":
            cli_ctx.output.print({
                "step1": "打开 https://open.feishu.cn/app",
                "step2": "创建应用 → 复制 AppID + AppSecret",
                "step3": "在 event subscription 启用 im.message.receive_v1",
                "step4": "权限:im:message, im:message:readonly, im:message.group_at_msg",
                "step5": "export LARK_APP_ID=cli_xxx",
                "step6": "export LARK_APP_SECRET=xxx",
                "step7": "openclaw serve  # 启动 gateway",
            }, title="lark login 步骤")
        elif name == "whatsapp":
            cli_ctx.output.print({
                "step1": "WhatsApp Web 需扫码,走 https://github.com/WhiskeySockets/Baileys 后端",
                "step2": "首次启动 openclaw serve 后,channel manager 会输出 QR 链接到 stderr",
                "step3": "用手机 WhatsApp 扫该 QR,登录后凭据落盘到 channels_runtime.fs_root/whatsapp/creds.json",
            }, title="whatsapp login 步骤")
        elif name in ("slack", "discord", "telegram"):
            cli_ctx.output.print({
                "step1": f"到 {name} 开发者后台创建 bot",
                "step2": "复制 bot token",
                "step3": f"export {name.upper()}_BOT_TOKEN=xoxb-...(或 DISCORD_BOT_TOKEN / TELEGRAM_BOT_TOKEN)",
                "step4": "openclaw serve  # 启动时 channel 自动连接",
            }, title=f"{name} login 步骤")
        elif name == "signal":
            cli_ctx.output.print({
                "step1": "安装 signal-cli(https://github.com/AsamK/signal-cli)",
                "step2": "signal-cli link -n 'openclaw'  # 扫码链接",
                "step3": "把 signal-cli rpc 监听地址配置到 cfg.channels_runtime.signal_rpc_url",
            }, title="signal login 步骤")
        elif name == "imessage":
            cli_ctx.output.print({
                "platform": "macOS only",
                "step1": "本机需 macOS 且登录 Apple ID",
                "step2": "openclaw 启动时会自动 attach 到 messages.db",
                "step3": "无需手动 login",
            }, title="imessage login 步骤")
        elif name in ("cli", "echo"):
            cli_ctx.output.success(f"{name} channel 不需 login(本地模式)")

        if verbose:
            cli_ctx.output.print({"hint": "完成后用 `openclaw channels list` 验证", "channel": name, "account": account})

    @ch_app.command("logout")
    def channels_logout(
        ctx: typer.Context,
        name: str = typer.Option(..., "--channel", "-c", help="channel 名"),
        account: str = typer.Option("default", "--account", "-a"),
    ) -> None:
        """登出 channel session(清本地凭据,不会调 API revoke)。"""
        cli_ctx = get_ctx(ctx.obj)
        from openclaw.cli.factory import load_config

        cfg, cfg_path = load_config(cli_ctx.config_path)
        ch_root = getattr(getattr(cfg, "channels_runtime", None), "fs_root", None)
        if ch_root is None:
            raise CLIError("未配置 channels_runtime.fs_root", exit_code=EXIT_CONFIG)

        from pathlib import Path
        creds = Path(ch_root) / name / account / "creds.json"
        if creds.exists():
            creds.unlink()
            cli_ctx.output.success(f"已删除 {name}/{account} 凭据: {creds}")
        else:
            cli_ctx.output.warn(f"无凭据: {creds}")

    return ch_app


def register(app: typer.Typer) -> None:
    app.add_typer(_channels_app(), name="channels")


__all__ = ["register"]
