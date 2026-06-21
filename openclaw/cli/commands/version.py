"""``openclaw version`` —— 打印版本号。

对齐上游 ``openclaw -V`` / ``openclaw version``。
"""
from __future__ import annotations

import platform
import sys

import typer

from openclaw.cli.context import get_ctx


def version(ctx: typer.Context) -> None:
    """打印 openclaw-py 版本号及运行环境。"""
    import openclaw

    cli_ctx = get_ctx(ctx.obj)
    info = {
        "openclaw_py": openclaw.__version__,
        "python": platform.python_version(),
        "platform": platform.system(),
    }
    cli_ctx.output.print(info, title="版本")


def register(app: typer.Typer) -> None:
    app.command("version")(version)


__all__ = ["version", "register"]
