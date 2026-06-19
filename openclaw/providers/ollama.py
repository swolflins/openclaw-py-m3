"""Ollama 本地 Provider(走 OpenAI 兼容协议)。

Ollama 0.5+ 自带 /v1 OpenAI 兼容端点;直接复用 OpenAICompatProvider。
本类保留为独立入口,便于以后接 Ollama 自己的 /api/chat 协议和流式增强。
"""
from __future__ import annotations


from openclaw.providers.openai_compat import OpenAICompatProvider


class OllamaProvider(OpenAICompatProvider):
    def __init__(
        self,
        base_url: str = "http://localhost:11434/v1",
        model: str = "llama3.1",
        timeout: float = 120.0,
        api_key: str = "ollama",  # Ollama 不校验 key,占位即可
    ) -> None:
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            model=model,
            timeout=timeout,
        )
