"""工具注册中心(Phase 4 增强版)。

- 装饰器: @registry.tool
- 自动从函数签名生成 JSON Schema
- 同步 / 异步函数都支持
- 工具属性: name, description, parameters, category, permission, requires_approval
- 危险操作可通过 Registry.set_approver() 注入人工审批回调
"""
from __future__ import annotations

import asyncio
import inspect
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional, Union

from openclaw.llm.base import ToolSpec


ToolFunc = Union[Callable[..., Any], Callable[..., Awaitable[Any]]]
Approver = Callable[[str, dict[str, Any]], Awaitable[bool]]
"""(tool_name, arguments) -> bool;True 表示放行。"""


# ---------------- 枚举 ----------------

class ToolCategory(str, Enum):
    """工具分类,便于渠道按需过滤 + UI 分组。"""
    UTILITY = "utility"      # 计算、时间、echo
    FS = "fs"                # 文件系统
    SHELL = "shell"          # shell 命令
    HTTP = "http"            # 网络
    CRON = "cron"            # 定时任务
    MEMORY = "memory"        # 记忆读写
    SANDBOX = "sandbox"      # 沙箱执行
    WEB = "web"              # 浏览器
    CUSTOM = "custom"


class ToolPermission(str, Enum):
    """工具权限等级(由低到高)。"""
    SAFE = "safe"            # 纯计算/只读
    READ = "read"            # 读文件、读网络
    WRITE = "write"          # 写文件
    EXEC = "exec"            # 执行 shell
    NETWORK = "network"      # 主动外联
    ADMIN = "admin"          # 沙箱/Docker


# ---------------- JSON Schema 简易生成器 ----------------

_TYPE_MAP = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
    type(None): "null",
}


def _python_type_to_json(tp: Any) -> str:
    if tp in _TYPE_MAP:
        return _TYPE_MAP[tp]
    origin = getattr(tp, "__origin__", None)
    if origin in (list, tuple, set):
        return "array"
    if origin is dict:
        return "object"
    if origin is Union:
        args = [a for a in tp.__args__ if a is not type(None)]
        if args:
            return _python_type_to_json(args[0])
    return "string"


def _parse_docstring_params(doc: str | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if not doc:
        return out
    lines = doc.splitlines()
    i = 0
    pat = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:\([^)]*\))?\s*:\s*(.*)")
    while i < len(lines):
        m = pat.match(lines[i])
        if m and m.group(2).strip():
            out[m.group(1)] = m.group(2).strip()
            i += 1
            while i < len(lines) and lines[i].startswith(("    ", "\t")) and lines[i].strip():
                out[m.group(1)] += " " + lines[i].strip()
                i += 1
        else:
            i += 1
    return out


def _build_schema(func: ToolFunc) -> tuple[dict[str, Any], str]:
    sig = inspect.signature(func)
    hints = getattr(func, "__annotations__", {}) or {}
    doc_params = _parse_docstring_params(inspect.getdoc(func))
    properties: dict[str, Any] = {}
    required: list[str] = []

    for name, param in sig.parameters.items():
        if name == "self":
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        py_type = hints.get(name, str)
        schema: dict[str, Any] = {"type": _python_type_to_json(py_type)}
        if param.default is inspect.Parameter.empty:
            required.append(name)
        else:
            schema["default"] = param.default
        if name in doc_params:
            schema["description"] = doc_params[name]
        properties[name] = schema

    schema_obj = {"type": "object", "properties": properties}
    if required:
        schema_obj["required"] = required

    description = (inspect.getdoc(func) or "").split("\n\n")[0].strip() or func.__name__
    return schema_obj, description


# ---------------- Tool / ToolRegistry ----------------

@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    func: ToolFunc
    is_async: bool = field(default=False)
    category: ToolCategory = field(default=ToolCategory.CUSTOM)
    permission: ToolPermission = field(default=ToolPermission.SAFE)
    requires_approval: bool = field(default=False)

    def to_spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description, parameters=self.parameters)

    async def __call__(self, **kwargs: Any) -> Any:
        if self.is_async:
            return await self.func(**kwargs)  # type: ignore[misc]
        return await asyncio.to_thread(self.func, **kwargs)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._approver: Optional[Approver] = None

    # ----- 元注册 -----

    def set_approver(self, approver: Approver) -> None:
        self._approver = approver

    def register(
        self,
        func: ToolFunc,
        *,
        name: str | None = None,
        description: str | None = None,
        category: ToolCategory = ToolCategory.CUSTOM,
        permission: ToolPermission = ToolPermission.SAFE,
        requires_approval: bool | None = None,
    ) -> Tool:
        tool_name = name or func.__name__
        schema, auto_desc = _build_schema(func)
        # 默认:permission >= NETWORK(>=WRITE)的需要审批
        # SEC-2:NETWORK 也加审批(SSRF / 数据外泄)
        if requires_approval is None:
            requires_approval = permission in (
                ToolPermission.WRITE,
                ToolPermission.NETWORK,
                ToolPermission.EXEC,
                ToolPermission.ADMIN,
            )
        t = Tool(
            name=tool_name,
            description=description or auto_desc,
            parameters=schema,
            func=func,
            is_async=asyncio.iscoroutinefunction(func),
            category=category,
            permission=permission,
            requires_approval=requires_approval,
        )
        self._tools[tool_name] = t
        return t

    def tool(
        self,
        func: ToolFunc | None = None,
        *,
        name: str | None = None,
        description: str | None = None,
        category: ToolCategory = ToolCategory.CUSTOM,
        permission: ToolPermission = ToolPermission.SAFE,
        requires_approval: bool | None = None,
    ) -> Any:
        def _wrap(f: ToolFunc) -> ToolFunc:
            self.register(
                f, name=name, description=description,
                category=category, permission=permission, requires_approval=requires_approval,
            )
            return f

        if func is not None and callable(func):
            return _wrap(func)
        return _wrap

    # ----- 工具调用 -----

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise KeyError(f"tool not found: {name}")
        return self._tools[name]

    def list_tools(
        self,
        category: ToolCategory | None = None,
        max_permission: ToolPermission | None = None,
    ) -> list[Tool]:
        """按 category / 权限过滤。"""
        order = {
            ToolPermission.SAFE: 0, ToolPermission.READ: 1,
            ToolPermission.WRITE: 2, ToolPermission.NETWORK: 3,
            ToolPermission.EXEC: 4, ToolPermission.ADMIN: 5,
        }
        out: list[Tool] = []
        for t in self._tools.values():
            if category is not None and t.category != category:
                continue
            if max_permission is not None and order[t.permission] > order[max_permission]:
                continue
            out.append(t)
        return out

    def specs(
        self,
        category: ToolCategory | None = None,
        max_permission: ToolPermission | None = None,
    ) -> list[ToolSpec]:
        return [t.to_spec() for t in self.list_tools(category=category, max_permission=max_permission)]

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        """带审批检查的调用入口。AgentLoop 在 Phase 4 也会用此入口。"""
        t = self.get(name)
        if t.requires_approval and self._approver is not None:
            ok = await self._approver(name, arguments)
            if not ok:
                raise PermissionError(f"tool {name} rejected by approver")
        return await t(**arguments)


# 兼容旧名
def tool(*args: Any, **kwargs: Any) -> Any:
    raise NotImplementedError("请使用 ToolRegistry().tool 装饰函数")
