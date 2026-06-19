"""Tools 子包:工具注册与执行。

Phase 4 升级:
- 工具分类(category)与权限(permission)
- 可选审批回调(approval)用于高危操作
- 内置工具按 category 集中注册
"""
from openclaw.tools.registry import (
    Tool,
    ToolRegistry,
    ToolCategory,
    ToolPermission,
    tool,
)
from openclaw.tools.builtin import register_builtin_tools

__all__ = [
    "Tool",
    "ToolRegistry",
    "ToolCategory",
    "ToolPermission",
    "tool",
    "register_builtin_tools",
]
