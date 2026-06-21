"""``openclaw cron`` —— 定时任务管理。

基于内部 ``openclaw.tools.builtin.cron.CronManager``(APScheduler)。
注意:CronManager 是进程内单例,本命令管理的是当前进程的定时任务。
若 gateway 已启动且挂载了 cron tools,可通过 gateway 调用(此处直接用本地单例)。

子命令:
  list              列出当前定时任务
  remove <id>       删除指定任务
  add --expr "*/5 * * * *" --command "shell echo hi"   添加 cron 任务(简化版)
"""
from __future__ import annotations

from typing import Optional

import typer

from openclaw.cli.context import get_ctx
from openclaw.cli.errors import CLIError


def _cron_app() -> typer.Typer:
    cr_app = typer.Typer(help="定时任务管理:list / add / remove", no_args_is_help=True)

    @cr_app.command("list")
    def cron_list(ctx: typer.Context) -> None:
        """列出当前定时任务。"""
        cli_ctx = get_ctx(ctx.obj)
        try:
            from openclaw.tools.builtin.cron import get_cron_manager
        except ImportError as e:
            raise CLIError(
                f"cron 模块不可用: {e}",
                exit_code=3,
                hint="安装 scheduler extras:pip install 'openclaw-py[scheduler]'",
            ) from e

        mgr = get_cron_manager()
        jobs = mgr.list_jobs()
        rows = [
            [j.get("id", "?"), j.get("expr", j.get("trigger", "?")), j.get("next_run", "?"), j.get("name", "")]
            for j in jobs
        ]
        cli_ctx.output.table(["id", "trigger", "next_run", "name"], rows, title=f"定时任务 ({len(jobs)})")

    @cr_app.command("add")
    def cron_add(
        ctx: typer.Context,
        expr: str = typer.Option(..., "--expr", "-e", help="cron 表达式,如 '*/5 * * * *'"),
        command: str = typer.Option(..., "--command", "-c", help="要执行的 shell 命令"),
        name: Optional[str] = typer.Option(None, "--name", help="任务名"),
    ) -> None:
        """添加 cron 任务(执行 shell 命令)。

        注意:此为简化版,任务在当前进程内调度。要持久化需配合 gateway 长驻。
        """
        cli_ctx = get_ctx(ctx.obj)
        try:
            from openclaw.tools.builtin.cron import get_cron_manager
        except ImportError as e:
            raise CLIError(
                f"cron 模块不可用: {e}", exit_code=3,
                hint="pip install 'openclaw-py[scheduler]'",
            ) from e

        import subprocess

        mgr = get_cron_manager()

        def _run():
            subprocess.run(command, shell=True, capture_output=True)

        jid = mgr.add_cron(expr, _run)
        cli_ctx.output.success(f"已添加 cron 任务: {jid} (expr={expr})")

    @cr_app.command("remove")
    def cron_remove(
        ctx: typer.Context,
        job_id: str = typer.Argument(..., help="任务 id"),
    ) -> None:
        """删除指定定时任务。"""
        cli_ctx = get_ctx(ctx.obj)
        try:
            from openclaw.tools.builtin.cron import get_cron_manager
        except ImportError as e:
            raise CLIError(f"cron 模块不可用: {e}", exit_code=3) from e

        mgr = get_cron_manager()
        ok = mgr.remove(job_id)
        if ok:
            cli_ctx.output.success(f"已删除: {job_id}")
        else:
            raise CLIError(f"任务不存在: {job_id}", exit_code=5)

    return cr_app


def register(app: typer.Typer) -> None:
    app.add_typer(_cron_app(), name="cron")


__all__ = ["register"]
