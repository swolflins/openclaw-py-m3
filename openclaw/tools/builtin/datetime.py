"""时间/日期工具(子包)。

- get_current_time(向后兼容,来自 Phase 0)
- format_time: 任意时区 + 格式
- parse_time: 字符串 -> 标准化时间
- timezone_convert: 跨时区转换
- date_diff: 两个时间差
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from openclaw.tools.registry import ToolCategory, ToolPermission, ToolRegistry

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - py3.9
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]


def register_datetime_tools(registry: ToolRegistry) -> None:
    @registry.tool(category=ToolCategory.UTILITY, permission=ToolPermission.SAFE)
    def get_current_time(tz: str = "UTC") -> str:
        """获取当前时间。tz: 时区名,例如 UTC / Asia/Shanghai。"""
        try:
            now = datetime.now(tz=ZoneInfo(tz))
        except Exception:
            now = datetime.now(tz=timezone.utc)
        return now.strftime("%Y-%m-%d %H:%M:%S %Z")

    @registry.tool(category=ToolCategory.UTILITY, permission=ToolPermission.SAFE)
    def format_time(
        iso: str,
        fmt: str = "%Y-%m-%d %H:%M:%S %Z",
        tz: str = "UTC",
    ) -> str:
        """把 ISO 格式时间按 fmt 格式化到指定时区。iso: ISO 字符串; fmt: strftime 格式; tz: 目标时区。"""
        dt = _parse(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo(tz)).strftime(fmt)

    @registry.tool(category=ToolCategory.UTILITY, permission=ToolPermission.SAFE)
    def parse_time(s: str, fmt: str = "") -> str:
        """把任意时间字符串解析为 ISO 8601。s: 字符串; fmt: 可选 strftime 格式,空=自动探测。"""
        dt = _parse(s, fmt=fmt or None)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()

    @registry.tool(category=ToolCategory.UTILITY, permission=ToolPermission.SAFE)
    def timezone_convert(iso: str, from_tz: str, to_tz: str) -> str:
        """跨时区转换。iso: ISO 时间; from_tz / to_tz: 源/目标时区。"""
        dt = _parse(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(from_tz))
        out = dt.astimezone(ZoneInfo(to_tz))
        return out.isoformat()

    @registry.tool(category=ToolCategory.UTILITY, permission=ToolPermission.SAFE)
    def date_diff(iso_a: str, iso_b: str, unit: str = "seconds") -> str:
        """计算两个时间的差。iso_a / iso_b: ISO 字符串(可混合 naive/aware); unit: seconds|minutes|hours|days。"""
        a = _parse(iso_a)
        b = _parse(iso_b)
        # naive datetime 自动按 UTC 处理
        if a.tzinfo is None and b.tzinfo is not None:
            a = a.replace(tzinfo=timezone.utc)
        elif b.tzinfo is None and a.tzinfo is not None:
            b = b.replace(tzinfo=timezone.utc)
        elif a.tzinfo is None and b.tzinfo is None:
            a = a.replace(tzinfo=timezone.utc)
            b = b.replace(tzinfo=timezone.utc)
        delta = (a - b).total_seconds()
        if unit == "seconds":
            return str(delta)
        if unit == "minutes":
            return str(delta / 60.0)
        if unit == "hours":
            return str(delta / 3600.0)
        if unit == "days":
            return str(delta / 86400.0)
        raise ValueError(f"unsupported unit: {unit}")


def _parse(s: str, fmt: Optional[str] = None) -> datetime:
    if fmt:
        return datetime.strptime(s, fmt)
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        # 退而求其次:as ISO with Z
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
