"""``openclaw update`` —— 自我升级检查(基于 pip index)。

子命令:
  check                  仅检查是否有新版本(不安装)
  update                 检查并升级到最新版
  status                 显示当前版本 + 渠道 + 远程版本

升级方式:用 pip 查 PyPI 最新 stable 版本,如果当前 installed_version < latest,
提示用户运行 `pip install -U openclaw-py`(不自动执行,避免破坏依赖)。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from typing import Optional

import typer

from openclaw.cli.context import get_ctx
from openclaw.cli.errors import CLIError, EXIT_NETWORK


def _get_installed_version() -> Optional[str]:
    """从 importlib.metadata 读 installed version,失败回退 pip show。"""
    try:
        from importlib.metadata import version

        return version("openclaw-py")
    except Exception:  # noqa: BLE001
        pass
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "show", "openclaw-py"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                if line.startswith("Version:"):
                    return line.split(":", 1)[1].strip()
    except Exception:  # noqa: BLE001
        pass
    return None


def _get_latest_version() -> Optional[str]:
    """从 PyPI 查 latest version。失败返回 None(网络问题)。"""
    try:
        import urllib.request

        with urllib.request.urlopen("https://pypi.org/pypi/openclaw-py/json", timeout=5) as r:
            data = json.load(r)
        return data.get("info", {}).get("version")
    except Exception:  # noqa: BLE001
        return None


def _parse_version(v: str) -> tuple[int, ...]:
    """解析 '1.2.3' / '1.2.3a1' / '1.2.3.post0' 为可比较元组。"""
    if not v:
        return (0,)
    m = re.match(r"(\d+(?:\.\d+)*)", v)
    if not m:
        return (0,)
    return tuple(int(x) for x in m.group(1).split("."))


def _update_app() -> typer.Typer:
    up_app = typer.Typer(help="自我升级:check / status / update", no_args_is_help=True)

    @up_app.command("check")
    def update_check(
        ctx: typer.Context,
        channel: str = typer.Option("stable", "--channel", help="stable / beta / dev"),
    ) -> None:
        """检查是否有新版本(不安装)。"""
        cli_ctx = get_ctx(ctx.obj)
        installed = _get_installed_version()
        latest = _get_latest_version()

        if latest is None:
            raise CLIError("无法连接 PyPI(检查网络/代理)", exit_code=EXIT_NETWORK)

        cmp_installed = _parse_version(installed or "0")
        cmp_latest = _parse_version(latest)

        out = {
            "installed": installed,
            "latest": latest,
            "channel": channel,
            "upgradable": cmp_installed < cmp_latest,
        }
        cli_ctx.output.print(out, title="update check")
        # 提示:JSON 模式下不再调 warn(避免在 stdout 写入第二个 JSON 对象),
        # 信息已在 out['upgradable'] / 'installed' / 'latest' 中。
        if out["upgradable"] and cli_ctx.output.mode != "json":
            cli_ctx.output.warn(f"新版本可用: {installed} → {latest}")
            cli_ctx.output.warn("运行 `openclaw update update` 升级(或手动 `pip install -U openclaw-py`)")
        elif out["upgradable"]:
            # JSON 模式:把结构化提示写到 stderr,不影响 stdout 的 JSON 解析
            print(
                json.dumps({
                    "status": "warn",
                    "message": f"new version available: {installed} -> {latest}; run `openclaw update update`",
                }, ensure_ascii=False),
                file=sys.stderr,
            )
        elif cli_ctx.output.mode != "json":
            cli_ctx.output.success("已是最新")

    @up_app.command("status")
    def update_status(
        ctx: typer.Context,
        channel: str = typer.Option("stable", "--channel"),
    ) -> None:
        """显示当前版本 + 渠道 + 远程版本。"""
        cli_ctx = get_ctx(ctx.obj)
        installed = _get_installed_version()
        latest = _get_latest_version()
        cli_ctx.output.print({
            "installed": installed or "(未知)",
            "channel": channel,
            "latest": latest or "(无法连接 PyPI)",
            "index_url": os.environ.get("PIP_INDEX_URL", "https://pypi.org/simple/"),
        }, title="update status")

    @up_app.command("update")
    def update_update(
        ctx: typer.Context,
        channel: str = typer.Option("stable", "--channel"),
        dry_run: bool = typer.Option(False, "--dry-run", help="只显示会执行的命令,不真跑"),
    ) -> None:
        """检查并升级(默认 dry-run;要真升级需 --dry-run=false)。"""
        cli_ctx = get_ctx(ctx.obj)
        installed = _get_installed_version()
        latest = _get_latest_version()
        if latest is None:
            raise CLIError("无法连接 PyPI", exit_code=EXIT_NETWORK)
        if _parse_version(installed or "0") >= _parse_version(latest):
            cli_ctx.output.success(f"已是最新({installed})")
            return
        cli_ctx.output.warn(f"将升级 {installed} → {latest}")
        cmd = [sys.executable, "-m", "pip", "install", "-U", "openclaw-py"]
        if channel == "beta":
            cmd.append("--pre")
        if dry_run:
            cli_ctx.output.print({"would_run": " ".join(cmd), "installed": installed, "latest": latest}, title="dry-run")
            return
        # 实际升级
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if proc.returncode == 0:
                cli_ctx.output.success(f"升级成功:{installed} → {latest}")
            else:
                raise CLIError(f"pip install 失败(exit={proc.returncode}):\n{proc.stderr[-500:]}", exit_code=1)
        except subprocess.TimeoutExpired as e:
            raise CLIError(f"升级超时: {e}", exit_code=1) from e

    return up_app


def register(app: typer.Typer) -> None:
    app.add_typer(_update_app(), name="update")


__all__ = ["register"]
