"""统一异常类型。"""
from __future__ import annotations


class OpenClawError(Exception):
    """所有 OpenClaw 异常的根。"""


class ConfigError(OpenClawError):
    """配置加载/校验失败。"""


class PluginError(OpenClawError):
    """插件加载/注册失败。"""


class ProviderError(OpenClawError):
    """LLM Provider 调用失败。"""


class ChannelError(OpenClawError):
    """消息渠道错误。"""


class ToolError(OpenClawError):
    """工具执行错误。"""
