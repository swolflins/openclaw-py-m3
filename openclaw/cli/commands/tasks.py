"""``openclaw tasks`` —— 轻量任务管理。

对齐上游 openclaw 的 ``tasks`` 命令,提供本地任务清单的 CRUD。
任务数据保存在 ``~/.openclaw/tasks.json``(可被 ``OPENCLAW_TASKS_PATH`` 覆盖)。
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer

from openclaw.cli.context import get_ctx
from openclaw.cli.errors import EXIT_NOT_FOUND, CLIError

logger = logging.getLogger(__name__)


def _tasks_path() -> Path:
    raw = os.environ.get("OPENCLAW_TASKS_PATH", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".openclaw" / "tasks.json"


def _load_tasks() -> list[dict]:
    path = _tasks_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as exc:  # noqa: BLE001
        logger.debug("读取任务文件失败: %s", exc)
        return []


def _save_tasks(tasks: list[dict]) -> None:
    path = _tasks_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tasks_app() -> typer.Typer:
    t_app = typer.Typer(help="任务管理:list / show / add / done / delete", no_args_is_help=True)

    @t_app.command("list")
    def tasks_list(
        ctx: typer.Context,
        status: Optional[str] = typer.Option(None, "--status", help="过滤状态:todo/done/all"),
    ) -> None:
        """列出任务。"""
        cli_ctx = get_ctx(ctx.obj)
        tasks = _load_tasks()
        status = (status or "todo").lower()
        if status != "all":
            tasks = [t for t in tasks if t.get("status", "todo") == status]
        rows = [[t.get("id", "?")[:8], t.get("title", ""), t.get("status", "todo"), t.get("created_at", "")[:19]] for t in tasks]
        cli_ctx.output.table(["id", "title", "status", "created_at"], rows, title=f"tasks ({len(tasks)})")

    @t_app.command("show")
    def tasks_show(
        ctx: typer.Context,
        task_id: str = typer.Argument(..., help="任务 ID 或前缀"),
    ) -> None:
        """查看任务详情。"""
        cli_ctx = get_ctx(ctx.obj)
        tasks = _load_tasks()
        matches = [t for t in tasks if str(t.get("id", "")).startswith(task_id)]
        if not matches:
            raise CLIError(f"任务不存在: {task_id}", exit_code=EXIT_NOT_FOUND)
        cli_ctx.output.print(matches[0], title=f"task {task_id}")

    @t_app.command("add")
    def tasks_add(
        ctx: typer.Context,
        title: str = typer.Argument(..., help="任务标题"),
        description: Optional[str] = typer.Option(None, "--description", "-d", help="任务描述"),
    ) -> None:
        """添加任务。"""
        cli_ctx = get_ctx(ctx.obj)
        tasks = _load_tasks()
        task = {
            "id": uuid.uuid4().hex,
            "title": title,
            "description": description or "",
            "status": "todo",
            "created_at": _now(),
            "updated_at": _now(),
        }
        tasks.append(task)
        _save_tasks(tasks)
        cli_ctx.output.success(f"已添加任务: {title} ({task['id'][:8]})")

    @t_app.command("done")
    def tasks_done(
        ctx: typer.Context,
        task_id: str = typer.Argument(..., help="任务 ID 或前缀"),
    ) -> None:
        """标记任务完成。"""
        cli_ctx = get_ctx(ctx.obj)
        tasks = _load_tasks()
        for t in tasks:
            if str(t.get("id", "")).startswith(task_id):
                t["status"] = "done"
                t["updated_at"] = _now()
                _save_tasks(tasks)
                cli_ctx.output.success(f"已完成任务: {t.get('title')} ({task_id})")
                return
        raise CLIError(f"任务不存在: {task_id}", exit_code=EXIT_NOT_FOUND)

    @t_app.command("delete")
    def tasks_delete(
        ctx: typer.Context,
        task_id: str = typer.Argument(..., help="任务 ID 或前缀"),
    ) -> None:
        """删除任务。"""
        cli_ctx = get_ctx(ctx.obj)
        tasks = _load_tasks()
        new_tasks = [t for t in tasks if not str(t.get("id", "")).startswith(task_id)]
        if len(new_tasks) == len(tasks):
            raise CLIError(f"任务不存在: {task_id}", exit_code=EXIT_NOT_FOUND)
        _save_tasks(new_tasks)
        cli_ctx.output.success(f"已删除任务: {task_id}")

    return t_app


def register(app: typer.Typer) -> None:
    app.add_typer(_tasks_app(), name="tasks")


__all__ = ["register"]
