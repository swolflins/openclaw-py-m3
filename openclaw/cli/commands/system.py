"""``openclaw system`` —— 系统信息。

汇总:版本、运行环境、配置路径、provider、已装依赖、内存目录等。
"""
from __future__ import annotations

import platform
import sys
from pathlib import Path

import typer

from openclaw.cli.context import get_ctx


def _check_optional_deps() -> dict[str, bool]:
    """检查可选依赖安装状态。"""
    import importlib

    deps = {}
    for mod, _extra, _desc in [
        ("fastapi", "server", ""), ("uvicorn", "server", ""), ("chromadb", "chroma", ""),
        ("anthropic", "anthropic", ""), ("google.generativeai", "gemini", ""),
        ("redis", "redis", ""), ("docker", "docker", ""), ("playwright", "playwright", ""),
        ("apscheduler", "scheduler", ""), ("watchdog", "fs-watch", ""),
        ("lark_oapi", "lark", ""),
    ]:
        try:
            importlib.import_module(mod)
            deps[mod] = True
        except ImportError:
            deps[mod] = False
    return deps


def system(ctx: typer.Context) -> None:
    """打印系统信息(版本 / 环境 / 配置 / 依赖)。"""
    cli_ctx = get_ctx(ctx.obj)
    import openclaw

    info: dict = {
        "openclaw_py": openclaw.__version__,
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.machine()}",
        "executable": sys.executable,
        "cwd": str(Path.cwd()),
    }

    # 配置信息
    try:
        from openclaw.cli.factory import load_config

        cfg, cfg_path = load_config(cli_ctx.config_path)
        info["config"] = {
            "path": str(cfg_path) if cfg_path else None,
            "providers": [p.name for p in cfg.providers],
            "default_provider": cfg.default_provider,
            "memory_dir": str(cfg.memory.dir),
            "long_term_enabled": cfg.memory.long_term_enabled,
            "router_strategy": cfg.agent.router_strategy,
            "skills_enabled": cfg.skills.enabled,
        }
    except Exception as e:  # noqa: BLE001
        info["config"] = {"error": str(e)}

    # 依赖
    info["dependencies"] = _check_optional_deps()

    # gateway URL
    import os

    info["gateway_url"] = os.environ.get("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:8088")

    cli_ctx.output.print(info, title="系统信息")


def register(app: typer.Typer) -> None:
    app.command("system")(system)


__all__ = ["system", "register"]
