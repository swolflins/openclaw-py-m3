"""LLM 子包:通用接口与数据结构。

具体实现已迁移到 openclaw.providers.*;本模块仅保留接口与数据类。
"""
from __future__ import annotations

from typing import Any

from openclaw.llm.base import (
    BaseLLMProvider,
    ChatMessage,
    LLMResult,
    ToolCall,
    ToolSpec,
)

# 兼容旧导入路径:phase 2 才存在,这里用 lazy
_LAZY_EXPORTS = {
    "OpenAICompatProvider": ("openclaw.providers.openai_compat", "OpenAICompatProvider"),
}


def __getattr__(name: str) -> Any:
    spec = _LAZY_EXPORTS.get(name)
    if spec is None:
        raise AttributeError(f"module 'openclaw.llm' has no attribute {name!r}")
    import importlib
    mod = importlib.import_module(spec[0])
    value = getattr(mod, spec[1])
    globals()[name] = value
    return value


__all__ = [
    "BaseLLMProvider",
    "ChatMessage",
    "LLMResult",
    "ToolCall",
    "ToolSpec",
    "OpenAICompatProvider",
]
