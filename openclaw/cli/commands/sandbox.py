"""``openclaw sandbox`` —— Docker 沙箱工具状态。

检查 docker 工具是否可用、列出沙箱相关配置。
对齐上游 ``openclaw sandbox``(Python 版有 docker tools 但无独立沙箱管理 CLI)。
"""
from __future__ import annotations

import typer

from openclaw.cli.context import get_ctx
from openclaw.cli.factory import load_config


def sandbox(ctx: typer.Context) -> None:
    """查看 Docker 沙箱工具状态。"""
    cli_ctx = get_ctx(ctx.obj)
    info: dict = {"available": False}

    # 检查 docker 依赖
    try:
        import docker  # noqa: F401

        info["docker_sdk"] = True
    except ImportError:
        info["docker_sdk"] = False

    # 检查 docker daemon 可达性
    if info["docker_sdk"]:
        try:
            import docker

            client = docker.from_env()
            info["daemon_running"] = True
            info["version"] = client.version().get("Version", "?")
            info["containers"] = len(client.containers.list(all=True))
            info["images"] = len(client.images.list())
            info["available"] = True
        except Exception as e:  # noqa: BLE001
            info["daemon_running"] = False
            info["daemon_error"] = str(e)[:100]

    # 配置
    try:
        cfg, _ = load_config(cli_ctx.config_path)
        info["config"] = {
            "shell_allowed": cfg.tools.shell_allowed,
            "http_allowed_hosts": cfg.tools.http_allowed_hosts,
            "fs_root": cfg.tools.fs_root,
        }
    except Exception as e:  # noqa: BLE001
        info["config"] = {"error": str(e)}

    if info["available"]:
        cli_ctx.output.success("Docker 沙箱可用")
    else:
        cli_ctx.output.warn("Docker 沙箱不可用")
        if not info.get("docker_sdk"):
            cli_ctx.output.error(
                "docker SDK 未安装",
                hint="pip install 'openclaw-py[docker]'",
            )
        elif not info.get("daemon_running"):
            cli_ctx.output.error(
                "docker daemon 未运行",
                hint="启动 Docker 服务后重试",
            )

    cli_ctx.output.print(info, title="沙箱状态")


def register(app: typer.Typer) -> None:
    app.command("sandbox")(sandbox)


__all__ = ["sandbox", "register"]
