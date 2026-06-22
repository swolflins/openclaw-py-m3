"""CLI 子命令注册中心。

每个命令模块暴露 ``register(app: typer.Typer) -> None``,
本模块统一 import 并调用。增量开发期间缺失的模块会被跳过并记 debug 日志。
"""
from __future__ import annotations

import importlib
import sys

import typer

# 命令模块清单(按依赖顺序无严格要求,这里是注册顺序)
_COMMAND_MODULES = [
    "openclaw.cli.commands.version",
    "openclaw.cli.commands.completion",
    "openclaw.cli.commands.config",
    "openclaw.cli.commands.models",
    "openclaw.cli.commands.run",
    "openclaw.cli.commands.agents",
    "openclaw.cli.commands.sessions",
    "openclaw.cli.commands.channels",
    "openclaw.cli.commands.message",
    "openclaw.cli.commands.memory",
    "openclaw.cli.commands.journal",
    "openclaw.cli.commands.tools",
    "openclaw.cli.commands.skills",
    "openclaw.cli.commands.plugins",
    "openclaw.cli.commands.gateway",
    "openclaw.cli.commands.doctor",
    "openclaw.cli.commands.security",
    "openclaw.cli.commands.cron",
    "openclaw.cli.commands.system",
    "openclaw.cli.commands.logs",
    "openclaw.cli.commands.sandbox",
    "openclaw.cli.commands.update",
]


def register(app: typer.Typer) -> None:
    """把所有子命令注册到主 app。"""
    for mod_name in _COMMAND_MODULES:
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            # 增量开发中模块尚未创建 — 静默跳过(仅 debug)
            continue
        register_fn = getattr(mod, "register", None)
        if register_fn is None:
            print(f"警告: {mod_name} 未定义 register()", file=sys.stderr)
            continue
        register_fn(app)


__all__ = ["register"]
