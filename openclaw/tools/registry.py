"""工具注册中心(Phase 4 增强版)。

- 装饰器: @registry.tool
- 自动从函数签名生成 JSON Schema
- 同步 / 异步函数都支持
- 工具属性: name, description, parameters, category, permission, requires_approval
- 危险操作可通过 Registry.set_approver() 注入人工审批回调
- Phase 25 / a4: ``call()`` 在审批通过后会用 ``jsonschema.validate``
  严格校验 LLM 输出的 ``arguments`` 字段,失败抛 ``ToolValidationError``,
  不 fallback 运行(防 LLM 任意字段直接落入工具参数 → host 任意命令执行)。
"""
from __future__ import annotations

import asyncio
import inspect
import re
import typing
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Optional, Union

from openclaw.core.errors import ToolValidationError
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


def _python_type_to_json(tp: Any) -> Any:
    """Python 类型 → JSON Schema ``type``。

    Phase 25 / a4:``Union`` 之前只返回第一个非 None 成员的类型,
    会让 ``Union[str, list[str]]`` 退化成 ``string`` → LLM 喂 list
    时 jsonschema 会误拒。这里改成 ``Union`` 展开成类型列表。
    """
    if tp in _TYPE_MAP:
        return _TYPE_MAP[tp]
    origin = getattr(tp, "__origin__", None)
    if origin in (list, tuple, set):
        return "array"
    if origin is dict:
        return "object"
    if origin is Union:
        args = [a for a in tp.__args__ if a is not type(None)]
        if not args:
            return "null"
        if len(args) == 1:
            return _python_type_to_json(args[0])
        # 多个非 None 成员 → 列表(每项再递归,展开 list[str] 之类的内层 origin)
        return [_python_type_to_json(a) for a in args]
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
    # Phase 25 / a4:用 ``get_type_hints`` 解析 PEP 563 字符串化注解
    # (模块顶层 ``from __future__ import annotations`` 会让
    # ``func.__annotations__`` 全是字符串,直接查 ``_TYPE_MAP`` 会
    # 全部 fallback 到 ``string``,后续 jsonschema 校验会失效)。
    try:
        hints = typing.get_type_hints(func) if hasattr(typing, "get_type_hints") else {}
    except Exception:  # pragma: no cover - 引用失败的 fallback
        hints = getattr(func, "__annotations__", {}) or {}
    if not hints:
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

    schema_obj = {
        "type": "object",
        "properties": properties,
        # Phase 25 / a4:严禁 LLM 在 arguments 里塞未声明字段
        # (例如 shell_exec 不该接任意 kwarg)。声明为 false 后
        # jsonschema 校验会把多余字段视为错误,直接抛 ToolValidationError。
        "additionalProperties": False,
    }
    if required:
        schema_obj["required"] = required

    description = (inspect.getdoc(func) or "").split("\n\n")[0].strip() or func.__name__
    return schema_obj, description


def _format_jsonschema_error(err: Any) -> str:
    """把 jsonschema.ValidationError 压成一行可读字符串。"""
    path = "/".join(str(p) for p in getattr(err, "absolute_path", []) or [])
    msg = getattr(err, "message", str(err))
    return f"{path}: {msg}" if path else msg


def _validate_arguments(
    name: str,
    schema: dict[str, Any],
    arguments: dict[str, Any],
) -> None:
    """Phase 25 / a4:对 LLM 输出的 ``arguments`` 做 JSON Schema 强校验。

    - 用 ``jsonschema.Draft202012Validator``(支持 ``additionalProperties: false``)
    - 失败抛 ``ToolValidationError(extra_info={"tool": name, "errors": [...]})``
    - 缺 jsonschema 库时,首次调用 ``import`` 失败抛 ``ToolValidationError``
      (fail-closed,绝对不能让 LLM 旁路校验)
    """
    try:
        import jsonschema  # type: ignore[import-untyped]
    except Exception as e:  # pragma: no cover - 不太可能
        raise ToolValidationError(
            f"tool {name} validation unavailable: jsonschema not installed",
            tool=name,
            errors=[f"jsonschema import failed: {e!r}"],
        ) from e
    try:
        validator_cls = jsonschema.Draft202012Validator
    except AttributeError:  # pragma: no cover - 兼容老版本
        validator_cls = jsonschema.Draft7Validator
    validator = validator_cls(schema)
    errors = sorted(validator.iter_errors(arguments), key=lambda e: list(e.absolute_path))
    if errors:
        rendered = [_format_jsonschema_error(e) for e in errors]
        raise ToolValidationError(
            f"tool {name} arguments failed JSON Schema validation "
            f"({len(rendered)} error(s))",
            tool=name,
            errors=rendered,
        )


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
        """带审批检查 + JSON Schema 强校验的调用入口。

        流程:
        1. 取 tool
        2. Phase 25 / a4:用 ``jsonschema.validate(arguments, parameters_schema)``
           严格校验 LLM 输出。失败抛 ``ToolValidationError``(不 fallback)
        3. 走审批(若需要)
        4. 透传给函数
        """
        t = self.get(name)
        # 1) JSON Schema 强校验 — 必须在审批前/后做都 OK,但放在审批前
        #    可以更早拒绝明显攻击;放审批后也合理(先看 approver 同不同意)。
        #    我们选在审批前:LLM 输出根本不合法,审批都无意义。
        #    arguments 必须是 dict(str->Any);非 dict 直接拒(防 list/str 透传)
        if not isinstance(arguments, dict):
            raise ToolValidationError(
                f"tool {name} arguments must be a JSON object, got {type(arguments).__name__}",
                tool=name,
                errors=[f"arguments is not a dict: {type(arguments).__name__}"],
            )
        _validate_arguments(name, t.parameters, arguments)
        # 2) 审批
        if t.requires_approval and self._approver is not None:
            ok = await self._approver(name, arguments)
            if not ok:
                raise PermissionError(f"tool {name} rejected by approver")
        return await t(**arguments)


# 兼容旧名
def tool(*args: Any, **kwargs: Any) -> Any:
    raise NotImplementedError("请使用 ToolRegistry().tool 装饰函数")
