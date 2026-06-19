"""System status skill:读本机 CPU/内存/磁盘/启动时间。"""
import os
import platform
import shutil
import time
from datetime import datetime

from openclaw.core.skills import SkillAPI
from openclaw.tools.registry import ToolCategory, ToolPermission


def _read_psutil() -> dict | None:
    try:
        import psutil  # type: ignore
    except Exception:
        return None
    try:
        mem = psutil.virtual_memory()
        du = shutil.disk_usage(os.getcwd())
        return {
            "cpu_percent": psutil.cpu_percent(interval=0.1),
            "mem_used_pct": mem.percent,
            "mem_used_gb": round(mem.used / 1024 ** 3, 2),
            "mem_total_gb": round(mem.total / 1024 ** 3, 2),
            "disk_free_gb": round(du.free / 1024 ** 3, 2),
            "disk_total_gb": round(du.total / 1024 ** 3, 2),
            "boot_time": datetime.fromtimestamp(psutil.boot_time()).isoformat(timespec="seconds"),
        }
    except Exception:
        return None


def _read_fallback() -> dict:
    # 兜底:用 shutil 读磁盘;内存/CPU 读不到就 None
    du = shutil.disk_usage(os.getcwd())
    return {
        "cpu_percent": None,
        "mem_used_pct": None,
        "mem_used_gb": None,
        "mem_total_gb": None,
        "disk_free_gb": round(du.free / 1024 ** 3, 2),
        "disk_total_gb": round(du.total / 1024 ** 3, 2),
        "boot_time": None,
        "_note": "psutil 不可用,只填了磁盘;CPU/内存留空",
    }


def register(api: SkillAPI) -> None:
    @api.tool(
        name="system_status",
        description="读本机 CPU/内存/磁盘/启动时间。需要 psutil 才有完整数据。",
        category=ToolCategory.UTILITY,
        permission=ToolPermission.SAFE,
    )
    def system_status() -> str:
        s = _read_psutil() or _read_fallback()
        s["platform"] = platform.platform()
        s["python"] = platform.python_version()
        s["now"] = datetime.now().isoformat(timespec="seconds")
        return str(s)

    api.inject_prompt(
        "用户问系统状态时,直接调 `system_status` 工具,再用 1-2 句话总结关键指标(CPU 占用/内存/磁盘剩余)。"
    )
