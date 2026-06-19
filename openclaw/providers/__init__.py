"""LLM Providers:多模型适配 + 统一注册 + Router。

设计:
- 公共接口: BaseLLMProvider (在 openclaw.llm.base)
- 适配器: providers/openai_compat.py, anthropic.py, gemini.py, ollama.py
- ProviderFactory: 用名字(配置驱动)构造 provider
- ProviderRouter: 失败自动 fallback + 可选多模型选择策略
"""
from __future__ import annotations

from openclaw.llm.base import BaseLLMProvider
from openclaw.providers.factory import ProviderFactory, get_factory
from openclaw.providers.openai_compat import OpenAICompatProvider
from openclaw.providers.anthropic import AnthropicProvider
from openclaw.providers.gemini import GeminiProvider
from openclaw.providers.ollama import OllamaProvider
from openclaw.providers.router import ProviderMeta, ProviderRouter, RouterStats, Strategy

__all__ = [
    "BaseLLMProvider",
    "OpenAICompatProvider",
    "AnthropicProvider",
    "GeminiProvider",
    "OllamaProvider",
    "ProviderFactory",
    "ProviderMeta",
    "ProviderRouter",
    "RouterStats",
    "Strategy",
    "get_factory",
]
