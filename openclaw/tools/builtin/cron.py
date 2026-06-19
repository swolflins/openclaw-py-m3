"""定时任务工具(子包)。

- cron_add:添加一次性或循环任务
- cron_list / cron_remove
- 后端:APScheduler(默认 BackgroundScheduler)

注意:cron_add 在没有 running event loop 时,会启动一个守护式 BackgroundScheduler。
如果 Agent 已经在 asyncio loop 中跑,推荐用 run_async 注册任务,而不是启动后台 scheduler。
"""
from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from openclaw.core.logging import get_logger
from openclaw.tools.registry import ToolCategory, ToolPermission, ToolRegistry

logger = get_logger(__name__)

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.date import DateTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    _HAS_APS = True
except Exception:  # pragma: no cover
    _HAS_APS = False


class CronManager:
    """单例式的 cron 管理器。

    - 同步任务:用 BackgroundScheduler
    - 异步任务:在主 event loop 里 schedule(asyncio.create_task)
    """

    def __init__(self) -> None:
        self._bg: Optional[BackgroundScheduler] = None
        self._async_jobs: dict[str, asyncio.Task] = {}
        self._lock = threading.Lock()
        self._bg_started = False

    def _ensure_bg(self) -> BackgroundScheduler:
        if self._bg is None:
            self._bg = BackgroundScheduler(daemon=True)
        if not self._bg_started:
            self._bg.start()
            self._bg_started = True
        return self._bg

    def add_interval(self, seconds: int, fn: Callable, *args: Any) -> str:
        if not _HAS_APS:
            raise RuntimeError("apscheduler 未安装,运行 `pip install apscheduler`")
        bg = self._ensure_bg()
        jid = f"job_{uuid.uuid4().hex[:8]}"
        bg.add_job(fn, IntervalTrigger(seconds=seconds), id=jid, args=list(args))
        logger.info("cron_add_interval", jid=jid, seconds=seconds)
        return jid

    def add_cron(self, cron_expr: str, fn: Callable, *args: Any) -> str:
        """cron 表达式: 5 字段 '分 时 日 月 周'。"""
        if not _HAS_APS:
            raise RuntimeError("apscheduler 未安装")
        bg = self._ensure_bg()
        jid = f"job_{uuid.uuid4().hex[:8]}"
        try:
            trig = CronTrigger.from_crontab(cron_expr)
        except Exception as e:
            raise ValueError(f"bad cron expr {cron_expr!r}: {e}") from e
        bg.add_job(fn, trig, id=jid, args=list(args))
        logger.info("cron_add_cron", jid=jid, expr=cron_expr)
        return jid

    def add_at(self, when: datetime, fn: Callable, *args: Any) -> str:
        if not _HAS_APS:
            raise RuntimeError("apscheduler 未安装")
        bg = self._ensure_bg()
        jid = f"job_{uuid.uuid4().hex[:8]}"
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        bg.add_job(fn, DateTrigger(run_date=when), id=jid, args=list(args))
        logger.info("cron_add_at", jid=jid, when=when.isoformat())
        return jid
    def list_jobs(self) -> list[dict[str, Any]]:
        if self._bg is None:
            return []
        out: list[dict[str, Any]] = []
        for j in self._bg.get_jobs():
            out.append({
                "id": j.id,
                "next_run": j.next_run_time.isoformat() if j.next_run_time else None,
                "trigger": str(j.trigger),
            })
        return out

    def remove(self, jid: str) -> bool:
        if self._bg is None:
            return False
        try:
            self._bg.remove_job(jid)
            return True
        except Exception:
            return False

    def shutdown(self) -> None:
        if self._bg is not None and self._bg_started:
            self._bg.shutdown(wait=False)
            self._bg_started = False


_default_manager: CronManager | None = None
_default_lock = threading.Lock()


def get_cron_manager() -> CronManager:
    global _default_manager
    with _default_lock:
        if _default_manager is None:
            _default_manager = CronManager()
        return _default_manager


def register_cron_tools(
    registry: ToolRegistry,
    *,
    manager: Optional[CronManager] = None,
    callback: Optional[Callable[[str], None]] = None,
) -> None:
    """注册 cron 工具。

    callback: 任务触发时的回调(jid),由调用方决定如何把通知转回渠道(比如发到飞书)。
    """
    mgr = manager or get_cron_manager()
    cb = callback or (lambda jid: logger.info("cron_fired", jid=jid))

    def _schedule(call: Callable[..., str], **kw: Any) -> str:
        """统一调度:把 jid 闭包后传给 apscheduler,避免签名校验失败。"""
        # 简单做法:用闭包捕获 jid
        captured: dict[str, Any] = {}

        def _fire() -> None:
            try:
                cb(captured["jid"])
            except Exception as e:  # pragma: no cover
                logger.warning("cron_fire_error", jid=captured.get("jid"), error=str(e))

        # 先 add 一次占位拿到 jid,再覆盖 func;但 apscheduler 允许后续 modify_job
        if "cron_expr" in kw:
            jid = mgr.add_cron(kw["cron_expr"], _fire)
        elif "every_seconds" in kw:
            jid = mgr.add_interval(int(kw["every_seconds"]), _fire)
        elif "at" in kw:
            jid = mgr.add_at(kw["at"], _fire)
        else:
            return "[error] must specify one of cron_expr / every_seconds / at"
        captured["jid"] = jid
        return jid

    @registry.tool(category=ToolCategory.CRON, permission=ToolPermission.WRITE)
    def cron_add(
        cron_expr: str = "",
        every_seconds: int = 0,
        at: str = "",
        payload: str = "",
    ) -> str:
        """添加一个定时任务(三选一:cron_expr / every_seconds / at)。cron_expr: 5字段 crontab; every_seconds: 周期秒(>0); at: ISO 时间(一次性); payload: 触发时回传,目前只记录日志。"""
        # LLM 经常把 int 写成字符串,做容错转换
        if isinstance(every_seconds, str):
            try:
                every_seconds = int(every_seconds.strip())
            except ValueError:
                return f"[error] every_seconds 不是合法整数: {every_seconds!r}"
        if cron_expr:
            jid = _schedule(cron_add, cron_expr=cron_expr)
            return f"added cron {jid} expr={cron_expr} payload={payload}"
        if every_seconds and every_seconds > 0:
            jid = _schedule(cron_add, every_seconds=int(every_seconds))
            return f"added interval {jid} every={every_seconds}s payload={payload}"
        if at:
            try:
                when = datetime.fromisoformat(at)
            except ValueError:
                return f"[error] bad 'at' ISO: {at}"
            jid = _schedule(cron_add, at=when)
            return f"added one-shot {jid} at={at} payload={payload}"
        return "[error] must specify one of cron_expr / every_seconds / at"

    @registry.tool(category=ToolCategory.CRON, permission=ToolPermission.READ)
    def cron_list() -> str:
        """列出所有计划任务(id / next_run / trigger)。"""
        jobs = mgr.list_jobs()
        if not jobs:
            return "(no jobs)"
        lines = [f"{j['id']:20s} next={j['next_run']} trigger={j['trigger']}" for j in jobs]
        return "\n".join(lines)

    @registry.tool(category=ToolCategory.CRON, permission=ToolPermission.WRITE)
    def cron_remove(job_id: str) -> str:
        """移除一个任务(按 id)。job_id: cron_list 返回的 id。"""
        ok = mgr.remove(job_id)
        return f"removed {job_id}" if ok else f"[error] job not found: {job_id}"
