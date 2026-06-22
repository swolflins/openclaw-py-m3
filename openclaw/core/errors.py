"""统一异常类型。"""
from __future__ import annotations

import os


class OpenClawError(Exception):
    """所有 OpenClaw 异常的根。"""


class ConfigError(OpenClawError):
    """配置加载/校验失败。

    Phase 27 follow-up / M18 修复:子类化时支持 ``path=`` 关键字,把出错的
    配置文件路径(以及原始 message)一起塞到 ``__str__`` 里,便于排查。
    ``str(exc)`` 拿到的格式:
    - 有 path: ``[config: <path>] <原 message>``
    - 无 path: ``<原 message>``(与原版完全一致,零回归)
    """

    def __init__(self, message: str, *, path: str | os.PathLike[str] | None = None) -> None:
        super().__init__(message)
        self.path = str(path) if path is not None else None

    def __str__(self) -> str:
        base = super().__str__()
        if self.path:
            return f"[config: {self.path}] {base}"
        return base


class PluginError(OpenClawError):
    """插件加载/注册失败。"""


class ProviderError(OpenClawError):
    """LLM Provider 调用失败。"""


class ChannelError(OpenClawError):
    """消息渠道错误。"""


class ToolError(OpenClawError):
    """工具执行错误。"""


class ToolValidationError(ToolError):
    """工具参数 JSON Schema 校验失败(LLM 输出不合法时抛出)。

    Phase 25 / a4:阻止 LLM 任意字段直接落入工具参数(例如构造
    ``{"name":"shell_exec","arguments":{"command":"rm -rf /"}}`` 直接
    在 host 上执行)。Registry.call() 在调工具函数前会用
    ``jsonschema.validate(arguments, parameters_schema)`` 严格校验,
    失败抛本异常,不 fallback 运行。
    """

    def __init__(
        self,
        message: str,
        *,
        tool: str | None = None,
        errors: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.tool = tool
        self.errors = list(errors) if errors else []

    @property
    def extra_info(self) -> dict[str, object]:
        """对外暴露的诊断信息(与现有 OpenClawError 风格一致)。"""
        info: dict[str, object] = {}
        if self.tool is not None:
            info["tool"] = self.tool
        if self.errors:
            info["errors"] = list(self.errors)
        return info
