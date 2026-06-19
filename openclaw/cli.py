"""OpenClaw 命令行入口(占位,Phase 1 阶段只暴露 --version / --help)。

完整命令(`run` / `lark` / `once` / `soul` / `start`)会在 phase 2+5+6+7
逐步补齐,届时本文件会被覆写。
"""
from __future__ import annotations

import typer

app = typer.Typer(add_completion=False, help="OpenClaw Python — 异步 AI Agent 运行时")


@app.command()
def version() -> None:
    """打印 openclaw-py 版本号。"""
    import openclaw
    typer.echo(f"openclaw-py {openclaw.__version__}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
