"""CLI 上下文:在 root callback 中构造,子命令通过 ctx.obj 取用。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from openclaw.cli.output import OutputFormatter


@dataclass
class CLIContext:
    """贯穿一次 CLI 调用的全局状态。"""

    output: OutputFormatter
    config_path: Optional[Path] = None
    profile: Optional[str] = None
    verbose: bool = False


def get_ctx(ctx_obj: object) -> CLIContext:
    """从 typer.Context.obj 安全取出 CLIContext。

    若未设置(如直接调用命令函数测试),返回一个默认 rich 模式上下文。
    """
    if isinstance(ctx_obj, CLIContext):
        return ctx_obj
    return CLIContext(output=OutputFormatter("rich"))
