"""``openclaw completion`` —— 生成 shell 补全脚本。

Typer 已自带 ``--install-completion`` / ``--show-completion``,本命令提供
独立子命令形式,方便脚本化:``openclaw completion bash > /etc/...``。

支持:bash / zsh / fish / powershell。
"""
from __future__ import annotations

import sys

import typer

from openclaw.cli.errors import CLIError, EXIT_CONFIG, EXIT_UNKNOWN


def completion(
    ctx: typer.Context,
    shell: str = typer.Argument(
        None,
        help="目标 shell:bash / zsh / fish / powershell。省略则检测当前 shell。",
    ),
) -> None:
    """生成 shell 补全脚本(输出到 stdout,可重定向到文件)。"""
    from openclaw.cli import app as _app

    if shell is None:
        # 检测当前 shell
        import os

        sh = os.environ.get("SHELL", "")
        if "zsh" in sh:
            shell = "zsh"
        elif "bash" in sh:
            shell = "bash"
        elif "fish" in sh:
            shell = "fish"
        else:
            raise CLIError(
                "未指定 shell 且无法自动检测,请显式传入:openclaw completion <bash|zsh|fish|powershell>",
                exit_code=EXIT_CONFIG,
            )

    shell = shell.lower()
    supported = {"bash", "zsh", "fish", "powershell"}
    if shell not in supported:
        raise CLIError(
            f"不支持的 shell: {shell!r},支持: {sorted(supported)}",
            exit_code=EXIT_CONFIG,
        )

    # 用 click 的 shell_completion 生成脚本
    try:
        from typer.main import get_command

        click_cmd = get_command(_app)
        if shell == "powershell":
            # click 8.x 内置仅支持 bash/zsh/fish;powershell 走 Typer 内置安装机制
            sys.stdout.write(
                "# PowerShell 补全:请运行以下命令安装(由 Typer 内置支持):\n"
                "#   openclaw --install-completion powershell\n"
                "# 或在 PowerShell 配置文件中加入:\n"
                "#   Import-Module PSReadLine\n"
                "#   Register-ArgumentCompleter ... (见 --show-completion powershell)\n"
            )
            return
        from click.shell_completion import _available_shells

        comp_cls = _available_shells.get(shell)
        if comp_cls is None:
            raise CLIError(f"click 不支持生成 {shell} 补全", exit_code=EXIT_UNKNOWN)
        comp = comp_cls(
            cli=click_cmd, ctx_args={}, prog_name="openclaw",
            complete_var="_OPENCLAW_COMPLETE",
        )
        sys.stdout.write(comp.source())
    except CLIError:
        raise
    except Exception as e:  # noqa: BLE001
        raise CLIError(f"生成补全脚本失败: {e}", exit_code=EXIT_UNKNOWN) from e


def register(app: typer.Typer) -> None:
    app.command("completion")(completion)


__all__ = ["completion", "register"]
