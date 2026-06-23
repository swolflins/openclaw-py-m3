"""``openclaw plugins`` —— 插件管理。

子命令:
  list            列出已发现的插件(entry_points + 本地目录)
  install NAME    安装插件(--pip 或 --local)
  uninstall NAME  卸载插件
  search QUERY    模糊搜索可用插件
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import typer

from openclaw.cli.context import get_ctx
from openclaw.cli.errors import EXIT_CONFIG, EXIT_NOT_FOUND, CLIError

# 本地插件目录约定(对齐 core/plugin.py 的 load_local 默认)
_LOCAL_PLUGINS_DIR = Path("./openclaw_plugins")

logger = logging.getLogger(__name__)


def _discover_all(group: Optional[str]) -> list[tuple[str, str, str, str]]:
    """返回 [(group, name, value, source)]。不执行插件代码。"""
    from openclaw.core.plugin import ENTRY_POINT_GROUPS, discover_entry_points

    groups = [group] if group else list(ENTRY_POINT_GROUPS.values())
    out: list[tuple[str, str, str, str]] = []
    for g in groups:
        try:
            for name, ep in discover_entry_points(g):
                out.append((g, name, getattr(ep, "value", str(ep)), "entry_point"))
        except Exception as exc:  # noqa: BLE001
            logger.debug("发现 entry_point 组 %r 失败: %s", g, exc)
    # 本地目录
    if _LOCAL_PLUGINS_DIR.exists():
        for f in sorted(_LOCAL_PLUGINS_DIR.glob("*.py")):
            if not f.name.startswith("_"):
                out.append(("local", f.stem, str(f), "local"))
    return out


def _plugins_app() -> typer.Typer:
    pl_app = typer.Typer(help="插件管理:list / install / uninstall / search", no_args_is_help=True)

    @pl_app.command("list")
    def plugins_list(
        ctx: typer.Context,
        group: Optional[str] = typer.Option(
            None, "--group", help="过滤扩展点组:plugin/channel/provider/tool"
        ),
    ) -> None:
        """列出已发现的插件(不执行插件代码)。"""
        cli_ctx = get_ctx(ctx.obj)
        items = _discover_all(group)
        rows = [[g, name, src, source] for g, name, src, source in items]
        cli_ctx.output.table(["group", "name", "entry", "source"], rows, title=f"插件 ({len(items)})")

    @pl_app.command("install")
    def plugins_install(
        ctx: typer.Context,
        name: str = typer.Argument(..., help="插件名"),
        pip: Optional[str] = typer.Option(None, "--pip", help="pip 包名(从 PyPI 安装)"),
        local: Optional[Path] = typer.Option(None, "--local", help="本地 .py 文件路径"),
    ) -> None:
        """安装插件(pip 或本地文件)。"""
        cli_ctx = get_ctx(ctx.obj)
        if pip:
            cli_ctx.output.warn(f"正在 pip install {pip} ...")
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", pip],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise CLIError(
                    f"pip install 失败(returncode={result.returncode})\n{result.stderr}",
                    exit_code=EXIT_CONFIG,
                )
            cli_ctx.output.success(f"已安装 {pip}(重启进程后 entry_points 生效)")
            return
        if local:
            if not local.is_file():
                raise CLIError(f"本地文件不存在: {local}", exit_code=EXIT_NOT_FOUND)
            _LOCAL_PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
            target = _LOCAL_PLUGINS_DIR / f"{name}.py"
            shutil.copy(local, target)
            cli_ctx.output.success(f"已安装本地插件: {local} -> {target}")
            return
        raise CLIError("请指定 --pip 或 --local", exit_code=EXIT_CONFIG)

    @pl_app.command("uninstall")
    def plugins_uninstall(
        ctx: typer.Context,
        name: str = typer.Argument(..., help="插件名"),
        pip: bool = typer.Option(False, "--pip", help="用 pip uninstall 卸载 PyPI 包"),
    ) -> None:
        """卸载插件。"""
        cli_ctx = get_ctx(ctx.obj)
        if pip:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "uninstall", "-y", name],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise CLIError(f"pip uninstall 失败\n{result.stderr}", exit_code=EXIT_CONFIG)
            cli_ctx.output.success(f"已卸载 {name}")
            return
        # 本地卸载
        target = _LOCAL_PLUGINS_DIR / f"{name}.py"
        if target.exists():
            target.unlink()
            cli_ctx.output.success(f"已删除本地插件: {target}")
        else:
            raise CLIError(f"本地插件不存在: {target}(若是 pip 包,加 --pip)", exit_code=EXIT_NOT_FOUND)

    @pl_app.command("search")
    def plugins_search(
        ctx: typer.Context,
        query: str = typer.Argument(..., help="搜索关键词"),
    ) -> None:
        """模糊搜索可用插件。"""
        import difflib

        cli_ctx = get_ctx(ctx.obj)
        items = _discover_all(None)
        names = [name for _, name, _, _ in items]
        matches = difflib.get_close_matches(query, names, n=10, cutoff=0.3)
        rows = [[g, name, src] for g, name, src, _ in items if name in matches]
        cli_ctx.output.table(["group", "name", "entry"], rows, title=f"搜索 '{query}' ({len(rows)})")

    return pl_app


def register(app: typer.Typer) -> None:
    app.add_typer(_plugins_app(), name="plugins")


__all__ = ["register"]
