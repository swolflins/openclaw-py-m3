"""``openclaw logs`` —— 日志查看。

由于 openclaw 用 structlog,日志通常输出到 stderr / 文件。本命令:
- 默认显示最近的结构化日志(若配置了文件日志)
- --tail 持续跟踪
- --level 过滤级别

当前实现:查看 memory 目录下的日志(若有),或提示日志输出位置。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import typer

from openclaw.cli.context import get_ctx
from openclaw.cli.errors import EXIT_NOT_FOUND, CLIError

logger = logging.getLogger(__name__)


def _find_log_files(cli_ctx) -> list[Path]:
    """查找可能的日志文件。"""
    candidates: list[Path] = []
    try:
        from openclaw.cli.factory import load_config

        cfg, _ = load_config(cli_ctx.config_path)
        candidates.append(cfg.memory.dir / "openclaw.log")
        candidates.append(cfg.memory.dir / "logs" / "openclaw.log")
    except Exception as exc:  # noqa: BLE001
        logger.debug("加载配置以定位日志文件失败: %s", exc)
    candidates.append(Path("openclaw.log"))
    candidates.append(Path("logs/openclaw.log"))
    candidates.append(Path("/tmp/openclaw.log"))
    return [c for c in candidates if c.exists()]


def logs(
    ctx: typer.Context,
    tail: bool = typer.Option(False, "--tail", "-f", help="持续跟踪(类似 tail -f)"),
    lines: int = typer.Option(50, "--lines", "-n", help="显示最近 N 行"),
    level: Optional[str] = typer.Option(None, "--level", "-l", help="过滤级别:debug/info/warning/error"),
) -> None:
    """查看日志。"""
    cli_ctx = get_ctx(ctx.obj)
    log_files = _find_log_files(cli_ctx)

    if not log_files:
        cli_ctx.output.warn("未找到日志文件(openclaw 默认日志输出到 stderr)")
        cli_ctx.output.print({
            "hint": "若需文件日志,启动时重定向:openclaw serve 2> openclaw.log",
            "searched": [str(p) for p in _find_log_files(cli_ctx)] or ["(无)"],
        })
        return

    log_file = log_files[0]
    cli_ctx.output.warn(f"日志文件: {log_file}")

    if tail:
        import time

        try:
            with open(log_file, "r", encoding="utf-8") as f:
                f.seek(0, 2)  # 到末尾
                while True:
                    line = f.readline()
                    if line:
                        if level is None or level.lower() in line.lower():
                            print(line, end="")
                    else:
                        time.sleep(0.5)
        except KeyboardInterrupt:
            pass
    else:
        # 读最后 N 行
        try:
            all_lines = log_file.read_text(encoding="utf-8").splitlines()
        except Exception as e:  # noqa: BLE001
            raise CLIError(f"读取日志失败: {e}", exit_code=EXIT_NOT_FOUND) from e

        recent = all_lines[-lines:] if len(all_lines) > lines else all_lines
        if level:
            lv = level.lower()
            recent = [line for line in recent if lv in line.lower()]

        cli_ctx.output.plain("\n".join(recent) if recent else "(无日志)")
        cli_ctx.output.print({"file": str(log_file), "shown": len(recent), "total": len(all_lines)})


def register(app: typer.Typer) -> None:
    app.command("logs")(logs)


__all__ = ["logs", "register"]
