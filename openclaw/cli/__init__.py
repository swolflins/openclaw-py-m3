"""OpenClaw CLI 入口包。

模块化 CLI,对齐上游 openclaw 的命令结构。原 ``openclaw/cli.py`` 已迁移为该包;
入口 ``openclaw = "openclaw.cli:main"`` 仍兼容。

命令分组(见 commands/):
  version / run / serve / gateway / config / models / sessions
  plugins / skills / doctor / completion

横切选项(root):
  --json / --plain   输出模式
  --config / -c      配置文件路径
  --profile          配置 profile
  --verbose / -v     详细日志
  -V / --version     快速打印版本
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import typer

from openclaw.cli.context import CLIContext
from openclaw.cli.errors import CLIError, handle_error
from openclaw.cli.output import OutputFormatter

app = typer.Typer(
    name="openclaw",
    help="OpenClaw Python — 异步 AI Agent 运行时",
    no_args_is_help=True,
    add_completion=True,  # 启用 Typer 自带的 --install-completion / --show-completion
    rich_markup_mode="rich",
)


def _version_callback(value: bool) -> None:
    if value:
        import openclaw

        typer.echo(f"openclaw-py {openclaw.__version__}")
        raise typer.Exit(code=0)


@app.callback()
def root(
    ctx: typer.Context,
    json: bool = typer.Option(False, "--json", help="结构化 JSON 输出(stdout),日志走 stderr"),
    plain: bool = typer.Option(False, "--plain", help="纯文本输出,无表格/颜色"),
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="配置文件路径(yaml/json/toml)"),
    profile: Optional[str] = typer.Option(None, "--profile", help="配置 profile 名"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="详细日志(含堆栈)"),
    show_secrets: bool = typer.Option(False, "--show-secrets", help="显示明文 secret(默认脱敏为 ***)"),
    version_flag: bool = typer.Option(
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="打印版本号并退出",
    ),
) -> None:
    """OpenClaw 命令行入口。"""
    if json and plain:
        raise CLIError("--json 与 --plain 互斥,只能选其一", exit_code=2)
    mode = "json" if json else ("plain" if plain else "rich")

    # 配置日志级别
    try:
        from openclaw.core.logging import setup_logging

        setup_logging("DEBUG" if verbose else "WARNING", json=json)
    except Exception:
        pass  # 日志初始化失败不阻断 CLI

    ctx.obj = CLIContext(
        output=OutputFormatter(mode, show_secrets=show_secrets),
        config_path=config,
        profile=profile,
        verbose=verbose,
    )


def main() -> None:
    """pyproject.toml 入口:openclaw = "openclaw.cli:main"。"""
    try:
        app()
    except CLIError as e:
        # 命令内部抛出的 CLIError(未自行 catch 的)
        sys.exit(handle_error(e, verbose=False))
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        # 从 ctx.obj 取 verbose 不现实(异常路径),默认不详细
        verbose = "--verbose" in sys.argv or "-v" in sys.argv
        sys.exit(handle_error(e, verbose=verbose))


__all__ = ["app", "main", "CLIContext"]


# 模块级注册所有子命令组(便于 CliRunner 测试与直接 import app 使用)
# 放在文件末尾以避免循环 import:此时 app 与 root callback 已定义完毕
from openclaw.cli import commands as _commands  # noqa: E402,F401

_commands.register(app)
