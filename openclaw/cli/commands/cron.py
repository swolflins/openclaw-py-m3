"""``openclaw cron`` —— 定时任务管理(基于 apscheduler BackgroundScheduler)。

注意:CronManager 是进程内单例,本命令管理的是当前进程的定时任务。
若 gateway 已启动且挂载了 cron tools,可通过 gateway 调用(此处直接用本地单例)。

子命令:
  list              列出当前定时任务
  add               添加 cron 任务(简化版,执行 shell 命令)
  show JOB_ID       展示任务详情
  edit JOB_ID       修改任务触发时间(替换)
  remove JOB_ID     删除指定任务
  enable JOB_ID     启用(取消 paused)
  disable JOB_ID    暂停(paused)
  run JOB_ID        立即跑一次(调试)
  runs              显示 run history
"""
from __future__ import annotations

import subprocess
from typing import Any, Optional

import typer

from openclaw.cli.context import get_ctx
from openclaw.cli.errors import CLIError, EXIT_NOT_FOUND


# 进程内 run history(最大 100 条,FIFO 淘汰)
_RUN_HISTORY: list[dict[str, Any]] = []
_MAX_HISTORY = 100


# Phase 27 / C3 修复:与 tools/builtin/shell.py 对齐的安全约束
# 拒绝 shell 元字符与解释器黑名单(防 cron add -c 的 RCE 通道)
_FORBIDDEN_METACHARS = ("&&", "||", ";", "|", "&", ">", "<", "`", "$(", "${", "\n", "\r")
_INTERPRETER_BLACKLIST = {
    "python", "python3", "python2", "sh", "bash", "zsh",
    "perl", "ruby", "node", "nodejs", "lua", "php",
}
_MAX_COMMAND_LEN = 4096  # 长度上限防 OOM / log 注入


def _cron_config(config_path: str | None = None) -> "CronConfig":
    """加载 OpenClawConfig 并返回 cron 子配置(不存在则默认)。"""
    try:
        from openclaw.cli.factory import load_config
        from openclaw.core.config import OpenClawConfig

        if config_path:
            cfg, _ = load_config(config_path)
            return cfg.cron
        return OpenClawConfig().cron
    except Exception:
        # 配置加载失败时仍返回安全默认值,不阻断 cron 管理
        from openclaw.core.config import CronConfig
        return CronConfig()


def _validate_command(
    command: str,
    interpreter_blacklist: Optional[set[str]] = None,
) -> list[str]:
    """校验 + 分词,失败抛 CLIError(CONFIG=2);返回 shlex 分词后的 argv 列表。

    行为:
    - 拒绝空命令 / 长度超限
    - 拒绝换行 / 回车
    - 拒绝 shell 元字符(&&, ||, ;, |, &, >, <, `, $(, ${)
    - 拒绝解释器黑名单(python/sh/bash/... 防 -c 注入)
    - POSIX 走 shlex.split(posix=True);Windows 走 posix=False(路径安全)
    """
    import shlex
    import sys

    blacklist = interpreter_blacklist if interpreter_blacklist is not None else _INTERPRETER_BLACKLIST

    if not command or not command.strip():
        raise CLIError("command 为空", exit_code=2)
    if len(command) > _MAX_COMMAND_LEN:
        raise CLIError(
            f"command 长度 {len(command)} 超过上限 {_MAX_COMMAND_LEN}",
            exit_code=2,
        )
    if "\n" in command or "\r" in command:
        raise CLIError("command 含换行/回车(防 multi-line 注入)", exit_code=2)
    for ch in _FORBIDDEN_METACHARS:
        if ch in command:
            raise CLIError(
                f"shell metachar {ch!r} 不允许(请把命令包成可执行脚本路径)",
                exit_code=2,
            )
    posix = sys.platform != "win32"
    try:
        args = shlex.split(command, posix=posix)
    except ValueError as e:
        # POSIX 失败时 Windows 走 fallback
        if posix:
            try:
                args = shlex.split(command, posix=False)
            except ValueError as e2:
                raise CLIError(f"无法解析 command: {e2}", exit_code=2) from e2
        else:
            raise CLIError(f"无法解析 command: {e}", exit_code=2) from e
    if not args:
        raise CLIError("command 解析后为空", exit_code=2)
    import os
    first_tok = os.path.basename(args[0])
    if first_tok.lower() in {tok.lower() for tok in blacklist}:
        raise CLIError(
            f"解释器 {first_tok!r} 不允许(可经 -c/-e 注入任意代码;"
            f"请用编译后的脚本路径)",
            exit_code=2,
        )
    return args


def _record_run(jid: str, status: str, output: str) -> None:
    import time as _time
    _RUN_HISTORY.append({
        "job_id": jid,
        "ts": _time.time(),
        "status": status,
        "output": output[:500],  # 截断避免 OOM
    })
    if len(_RUN_HISTORY) > _MAX_HISTORY:
        del _RUN_HISTORY[: len(_RUN_HISTORY) - _MAX_HISTORY]


def _cron_app() -> typer.Typer:
    cr_app = typer.Typer(help="定时任务管理:list / add / show / edit / remove / enable / disable / run / runs", no_args_is_help=True)

    def _mgr():
        try:
            from openclaw.tools.builtin.cron import get_cron_manager
        except ImportError as e:
            raise CLIError(
                f"cron 模块不可用: {e}",
                exit_code=3,
                hint="安装 scheduler extras:pip install 'openclaw-py[scheduler]'",
            ) from e
        return get_cron_manager()

    @cr_app.command("list")
    def cron_list(ctx: typer.Context) -> None:
        """列出当前定时任务。"""
        cli_ctx = get_ctx(ctx.obj)
        mgr = _mgr()
        jobs = mgr.list_jobs()
        rows = [
            [j.get("id", "?"), j.get("trigger", "?"), j.get("next_run") or "-", "✓" if not j.get("paused", False) else "✗"]
            for j in jobs
        ]
        cli_ctx.output.table(["id", "trigger", "next_run", "enabled"], rows, title=f"定时任务 ({len(jobs)})")

    @cr_app.command("add")
    def cron_add(
        ctx: typer.Context,
        expr: str = typer.Option(..., "--expr", "-e", help="cron 表达式,如 '*/5 * * * *'"),
        command: str = typer.Option(..., "--command", "-c", help="要执行的命令(已 shlex 分词,无 shell 解释)"),
        name: Optional[str] = typer.Option(None, "--name", help="任务名(备注)"),
        timeout: Optional[int] = typer.Option(None, "--timeout", help="单次执行超时秒数(默认走 cron 配置)"),
    ) -> None:
        """添加 cron 任务(执行命令)。

        注意:此为简化版,任务在当前进程内调度。要持久化需配合 gateway 长驻。

        Phase 27 / C3 修复:不再用 ``shell=True``(会被 RCE)。改为 ``subprocess.run(args, shell=False)``,
        command 必须可被 shlex 解析且不含 shell 元字符 / 解释器黑名单。
        """
        cli_ctx = get_ctx(ctx.obj)
        mgr = _mgr()
        cron_cfg = _cron_config(cli_ctx.config_path)
        # 校验并分词;失败抛 CLIError(2)
        argv = _validate_command(command, interpreter_blacklist=set(cron_cfg.interpreter_blacklist))

        run_timeout = timeout if timeout is not None else cron_cfg.default_timeout_seconds
        if run_timeout > cron_cfg.max_timeout_seconds:
            raise CLIError(
                f"timeout {run_timeout}s 超过配置上限 {cron_cfg.max_timeout_seconds}s",
                exit_code=2,
            )

        def _run():
            try:
                # SEC-3 / Phase 27 / C3:shell=False + argv 列表,杜绝 shell 注入
                proc = subprocess.run(
                    argv, shell=False, capture_output=True, text=True, timeout=run_timeout,
                )
                _record_run(jid, "ok" if proc.returncode == 0 else "fail", proc.stdout + proc.stderr)
            except subprocess.TimeoutExpired:
                _record_run(jid, "timeout", f"command timed out ({run_timeout}s)")

        jid = mgr.add_cron(expr, _run)
        cli_ctx.output.success(f"已添加 cron 任务: {jid} (expr={expr}, timeout={run_timeout}s)")

    @cr_app.command("show")
    def cron_show(
        ctx: typer.Context,
        job_id: str = typer.Argument(..., help="任务 id"),
    ) -> None:
        """展示任务详情。"""
        cli_ctx = get_ctx(ctx.obj)
        mgr = _mgr()
        for j in mgr.list_jobs():
            if j.get("id") == job_id:
                cli_ctx.output.print(j, title=f"cron job {job_id}")
                return
        raise CLIError(f"任务不存在: {job_id}", exit_code=EXIT_NOT_FOUND)

    @cr_app.command("edit")
    def cron_edit(
        ctx: typer.Context,
        job_id: str = typer.Argument(..., help="任务 id"),
        expr: str = typer.Option(..., "--expr", "-e", help="新的 cron 表达式"),
    ) -> None:
        """修改任务触发表达式(替换 cron_trigger,保留原 callback)。"""
        cli_ctx = get_ctx(ctx.obj)
        mgr = _mgr()
        # apscheduler 0.4+ 暴露 _bg 对象,reschedule_job 是公开 API
        bg = mgr._ensure_bg()  # noqa: SLF001
        try:
            from apscheduler.triggers.cron import CronTrigger
        except ImportError as e:
            raise CLIError(f"apscheduler 不可用: {e}", exit_code=3) from e
        try:
            trig = CronTrigger.from_crontab(expr)
        except Exception as e:  # noqa: BLE001
            raise CLIError(f"bad cron expr {expr!r}: {e}", exit_code=2) from e
        try:
            bg.reschedule_job(job_id, trigger=trig)
        except Exception as e:  # noqa: BLE001
            raise CLIError(f"任务不存在或修改失败: {e}", exit_code=EXIT_NOT_FOUND) from e
        cli_ctx.output.success(f"已更新 cron 任务 {job_id} → expr={expr}")

    @cr_app.command("remove")
    def cron_remove(
        ctx: typer.Context,
        job_id: str = typer.Argument(..., help="任务 id"),
    ) -> None:
        """删除指定定时任务。"""
        cli_ctx = get_ctx(ctx.obj)
        mgr = _mgr()
        ok = mgr.remove(job_id)
        if ok:
            cli_ctx.output.success(f"已删除: {job_id}")
        else:
            raise CLIError(f"任务不存在: {job_id}", exit_code=EXIT_NOT_FOUND)

    @cr_app.command("enable")
    def cron_enable(
        ctx: typer.Context,
        job_id: str = typer.Argument(..., help="任务 id"),
    ) -> None:
        """启用(取消 paused)。"""
        cli_ctx = get_ctx(ctx.obj)
        mgr = _mgr()
        bg = mgr._ensure_bg()  # noqa: SLF001
        try:
            bg.resume_job(job_id)
        except Exception as e:  # noqa: BLE001
            raise CLIError(f"任务不存在或启用失败: {e}", exit_code=EXIT_NOT_FOUND) from e
        cli_ctx.output.success(f"已启用: {job_id}")

    @cr_app.command("disable")
    def cron_disable(
        ctx: typer.Context,
        job_id: str = typer.Argument(..., help="任务 id"),
    ) -> None:
        """暂停(paused,仍保留任务但不触发)。"""
        cli_ctx = get_ctx(ctx.obj)
        mgr = _mgr()
        bg = mgr._ensure_bg()  # noqa: SLF001
        try:
            bg.pause_job(job_id)
        except Exception as e:  # noqa: BLE001
            raise CLIError(f"任务不存在或暂停失败: {e}", exit_code=EXIT_NOT_FOUND) from e
        cli_ctx.output.success(f"已暂停: {job_id}")

    @cr_app.command("run")
    def cron_run(
        ctx: typer.Context,
        job_id: str = typer.Argument(..., help="任务 id"),
    ) -> None:
        """立即跑一次(调试用,不影响原 schedule)。"""
        cli_ctx = get_ctx(ctx.obj)
        mgr = _mgr()
        bg = mgr._ensure_bg()  # noqa: SLF001
        try:
            job = bg.get_job(job_id)
        except Exception as e:  # noqa: BLE001
            raise CLIError(f"任务不存在: {e}", exit_code=EXIT_NOT_FOUND) from e
        if job is None:
            raise CLIError(f"任务不存在: {job_id}", exit_code=EXIT_NOT_FOUND)
        # 同步调 callback
        try:
            job.func(*job.args, **job.kwargs)
            cli_ctx.output.success(f"已手动触发: {job_id}")
        except Exception as e:  # noqa: BLE001
            raise CLIError(f"执行失败: {e}", exit_code=1) from e

    @cr_app.command("runs")
    def cron_runs(
        ctx: typer.Context,
        job_id: Optional[str] = typer.Option(None, "--id", help="只显示某 job_id 的记录"),
        limit: int = typer.Option(20, "--limit", "-n", help="最多显示 N 条"),
    ) -> None:
        """显示 run history(进程内,最近 100 条)。"""
        cli_ctx = get_ctx(ctx.obj)
        items = _RUN_HISTORY
        if job_id:
            items = [r for r in items if r.get("job_id") == job_id]
        items = items[-limit:]
        rows = [
            [
                r.get("job_id", "?"),
                # 显示 ISO 时间
                __import__("datetime").datetime.fromtimestamp(r["ts"], tz=__import__("datetime").timezone.utc).isoformat()
                if r.get("ts") else "?",
                r.get("status", "?"),
                (r.get("output", "") or "")[:60],
            ]
            for r in items
        ]
        cli_ctx.output.table(["job_id", "ts", "status", "output"], rows, title=f"run history ({len(items)})")

    return cr_app


def register(app: typer.Typer) -> None:
    app.add_typer(_cron_app(), name="cron")


__all__ = ["register"]
