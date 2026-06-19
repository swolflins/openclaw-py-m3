"""OpenClaw Python — 异步 AI Agent 运行时

本包是对 OpenClaw (TypeScript) 的 Python 重写,
提供:
- 多模型抽象 (OpenAI 兼容 / Anthropic / Gemini / Ollama)
- 异步 Agent Loop (思考 - 工具调用 - 回复)
- 工具注册机制
- 持久化记忆 (短期 / 长期向量 / SOUL)
- 多消息渠道 (CLI / 飞书 / Telegram / Discord / Slack / WhatsApp / Signal / iMessage)
- 插件体系 (entry_points + 本地目录)
- 事件总线 (进程内 + 可选 Redis)
- 统一配置 (YAML/JSON/TOML + 热重载)

注意:子模块的导入采用 **lazy attribute** 风格
(模块级 `__getattr__`),这样当某个 phase 还没安装/还没初始化时,
`import openclaw` 本身不会因 ImportError 而整体失败,
调用方再 `openclaw.AgentLoop` 时才会触发实际加载。
"""
from __future__ import annotations

from typing import Any

__version__ = "0.1.0"

# 兼容老名字(直接 re-export,不会触发子模块 import 失败,
# 因为这些模块在 phase 0+1 阶段就应当存在)
from openclaw.config.settings import Settings, get_settings  # noqa: E402,F401

# 向后兼容:把 ShortTermStore 暴露为旧名 MemoryStore
# 这两个在 phase 3 才有;这里用 lazy re-export
_MemoryStore_proxy: Any = None
_ShortTermStore_proxy: Any = None
_LongTermStore_proxy: Any = None
_ScopedMemory_proxy: Any = None
_SoulLoader_proxy: Any = None


_LAZY_EXPORTS = {
    # phase 5
    "Agent": ("openclaw.agent.loop", "Agent"),
    "AgentLoop": ("openclaw.agent.loop", "AgentLoop"),
    # phase 2
    "BaseLLMProvider": ("openclaw.providers", "BaseLLMProvider"),
    "OpenAICompatProvider": ("openclaw.providers", "OpenAICompatProvider"),
    "AnthropicProvider": ("openclaw.providers", "AnthropicProvider"),
    "GeminiProvider": ("openclaw.providers", "GeminiProvider"),
    "OllamaProvider": ("openclaw.providers", "OllamaProvider"),
    "ProviderFactory": ("openclaw.providers", "ProviderFactory"),
    "ProviderRouter": ("openclaw.providers", "ProviderRouter"),
    "get_factory": ("openclaw.providers", "get_factory"),
    # phase 4
    "ToolRegistry": ("openclaw.tools.registry", "ToolRegistry"),
    # phase 3
    "ShortTermStore": ("openclaw.memory", "ShortTermStore"),
    "LongTermStore": ("openclaw.memory", "LongTermStore"),
    "ScopedMemory": ("openclaw.memory", "ScopedMemory"),
    "SoulLoader": ("openclaw.memory", "SoulLoader"),
}

# 旧名 alias:MemoryStore -> ShortTermStore
_ALIASES = {
    "MemoryStore": ("ShortTermStore",),
}


def __getattr__(name: str) -> Any:
    """PEP 562 lazy attribute — 真正用到时再 import,避免破坏分阶段上传。"""
    if name in _ALIASES:
        # alias:转去取对应目标
        target = _ALIASES[name][0]
        return __getattr__(target)
    spec = _LAZY_EXPORTS.get(name)
    if spec is None:
        raise AttributeError(f"module 'openclaw' has no attribute {name!r}")
    mod_name, attr = spec
    import importlib
    mod = importlib.import_module(mod_name)
    value = getattr(mod, attr)
    globals()[name] = value  # 缓存
    return value


__all__ = [
    "Settings",
    "get_settings",
    "Agent",
    "AgentLoop",
    "BaseLLMProvider",
    "OpenAICompatProvider",
    "AnthropicProvider",
    "GeminiProvider",
    "OllamaProvider",
    "ProviderFactory",
    "ProviderRouter",
    "get_factory",
    "ToolRegistry",
    "ShortTermStore",
    "LongTermStore",
    "ScopedMemory",
    "SoulLoader",
    "MemoryStore",  # alias for ShortTermStore
    "__version__",
]
