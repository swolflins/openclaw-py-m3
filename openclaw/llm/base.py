"""LLM 抽象基类与通用数据结构。

任何具体的 Provider (OpenAI、Anthropic、Ollama、本地) 都需要:
1. 继承 BaseLLMProvider
2. 实现 acomplete() —— 单次异步补全,支持可选 tools
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Optional


# ------------------------- 数据结构 -------------------------

@dataclass
class ToolCall:
    """LLM 决定要调用的工具。"""
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChatMessage:
    """一条对话消息。"""
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str = ""
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_calls: list[ToolCall] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            d["name"] = self.name
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": _to_json_str(tc.arguments)},
                }
                for tc in self.tool_calls
            ]
        return d


@dataclass
class ToolSpec:
    """一个可被 LLM 看到的工具描述。"""
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema

    def to_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class LLMResult:
    """Provider 返回的统一结果。"""
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: Any = None  # 原始响应,便于调试


# ------------------------- 抽象基类 -------------------------

class BaseLLMProvider(abc.ABC):
    """所有 LLM Provider 的统一接口。"""

    def __init__(self, model: str, **kwargs: Any) -> None:
        self.model = model
        self._extra = kwargs

    @abc.abstractmethod
    async def acomplete(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[ToolSpec]] = None,
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> LLMResult:
        """异步对话补全,可选传入工具列表。"""


# ------------------------- 工具函数 -------------------------

def _to_json_str(obj: Any) -> str:
    """容错地把对象转成 JSON 字符串。"""
    import json
    if isinstance(obj, str):
        return obj
    try:
        return json.dumps(obj, ensure_ascii=False)
    except TypeError:
        return str(obj)
