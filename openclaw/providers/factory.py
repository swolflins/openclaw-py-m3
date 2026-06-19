"""Provider 工厂:按名字(配置驱动)构造 LLM Provider。"""
from __future__ import annotations

from typing import Callable

from openclaw.core.config import ProviderConfig
from openclaw.core.errors import ProviderError
from openclaw.core.logging import get_logger
from openclaw.llm.base import BaseLLMProvider
from openclaw.providers.openai_compat import OpenAICompatProvider

logger = get_logger(__name__)

_Factory = Callable[[ProviderConfig], BaseLLMProvider]


class ProviderFactory:
    """注册表 + 构造器。"""

    def __init__(self) -> None:
        self._factories: dict[str, _Factory] = {
            "openai_compat": self._build_openai_compat,
            "anthropic": self._build_anthropic,
            "gemini": self._build_gemini,
            "ollama": self._build_ollama,
        }

    def register(self, name: str, factory: _Factory) -> None:
        self._factories[name] = factory

    def names(self) -> list[str]:
        return list(self._factories.keys())

    def build(self, cfg: ProviderConfig) -> BaseLLMProvider:
        f = self._factories.get(cfg.name)
        if f is None:
            raise ProviderError(
                f"unknown provider '{cfg.name}', available: {self.names()}"
            )
        return f(cfg)

    # ---- 内置工厂 ----

    @staticmethod
    def _build_openai_compat(cfg: ProviderConfig) -> BaseLLMProvider:
        return OpenAICompatProvider(
            api_key=cfg.api_key or "sk-placeholder",
            base_url=cfg.base_url or "https://api.deepseek.com/v1",
            model=cfg.model,
            timeout=float(cfg.extra.get("timeout", 60.0)),
        )

    @staticmethod
    def _build_anthropic(cfg: ProviderConfig) -> BaseLLMProvider:
        from openclaw.providers.anthropic import AnthropicProvider
        return AnthropicProvider(
            api_key=cfg.api_key or "",
            model=cfg.model,
            base_url=cfg.base_url,
            max_tokens=int(cfg.extra.get("max_tokens", 4096)),
        )

    @staticmethod
    def _build_gemini(cfg: ProviderConfig) -> BaseLLMProvider:
        from openclaw.providers.gemini import GeminiProvider
        return GeminiProvider(
            api_key=cfg.api_key or "",
            model=cfg.model,
        )

    @staticmethod
    def _build_ollama(cfg: ProviderConfig) -> BaseLLMProvider:
        from openclaw.providers.ollama import OllamaProvider
        return OllamaProvider(
            base_url=cfg.base_url or "http://localhost:11434/v1",
            model=cfg.model,
            timeout=float(cfg.extra.get("timeout", 120.0)),
        )


_default_factory: ProviderFactory | None = None


def get_factory() -> ProviderFactory:
    global _default_factory
    if _default_factory is None:
        _default_factory = ProviderFactory()
    return _default_factory
