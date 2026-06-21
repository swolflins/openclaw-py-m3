"""Agent 运行时构建工厂(供 run / models / serve 命令复用)。

构建链路(对齐 examples/phase7_smoke.py):
  ConfigLoader(path).load() -> OpenClawConfig
  -> ProviderFactory.build(p) x N -> ProviderRouter(primary, fallbacks)
  -> ShortTermStore + (可选 LongTermStore) + SoulLoader -> ScopedMemory
  -> ToolRegistry + register_builtin_tools
  -> AgentLoop(llm, tools, memory, system_prompt=...)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Tuple

from openclaw.core.config import ConfigLoader, OpenClawConfig
from openclaw.core.errors import ConfigError
from openclaw.llm.base import BaseLLMProvider

logger = logging.getLogger(__name__)


def load_config(config_path: Optional[Path] = None) -> Tuple[OpenClawConfig, Optional[Path]]:
    """加载配置。返回 (cfg, resolved_path)。"""
    loader = ConfigLoader(config_path) if config_path else ConfigLoader()
    cfg = loader.load()
    return cfg, loader.path


def build_router(
    cfg: OpenClawConfig,
    *,
    provider_override: Optional[str] = None,
) -> BaseLLMProvider:
    """根据 cfg.providers 构造 LLM(ProviderRouter 或单 provider)。

    Args:
        provider_override: 指定只用某个 provider(按 cfg.providers 中 name 匹配)。
    """
    from openclaw.providers.factory import ProviderFactory
    from openclaw.providers.router import ProviderRouter

    if not cfg.providers:
        raise ConfigError(
            "未配置任何 provider,请用 `openclaw config set providers '[...]'` 添加,"
            "或在配置文件中配置 providers 列表"
        )

    factory = ProviderFactory()
    built: list[BaseLLMProvider] = []
    for pcfg in cfg.providers:
        try:
            built.append(factory.build(pcfg))
        except Exception as e:  # noqa: BLE001
            logger.warning("provider %s 构建失败: %s", pcfg.name, e)

    if not built:
        raise ConfigError("所有 provider 构建失败,请检查 api_key / base_url 配置")

    # provider_override:只选指定 provider
    if provider_override:
        match = [p for p in built if p.__class__.__name__.lower().startswith(provider_override.lower())
                 or getattr(p, "model", "").lower() == provider_override.lower()]
        if not match:
            raise ConfigError(
                f"--provider {provider_override!r} 未匹配到已配置 provider,可用: "
                f"{[p.__class__.__name__ for p in built]}"
            )
        return match[0]

    if len(built) == 1:
        return built[0]

    primary, fallbacks = built[0], built[1:]
    strategy = cfg.agent.router_strategy or "fallback_only"
    return ProviderRouter(primary, fallbacks, strategy=strategy)


def build_memory(cfg: OpenClawConfig):
    """构造 ScopedMemory(short_term + 可选 long_term + soul)。"""
    from openclaw.memory.scoped import ScopedMemory
    from openclaw.memory.short_term import ShortTermStore
    from openclaw.memory.soul import SoulLoader

    short = ShortTermStore(cfg.memory.dir)

    long_term = None
    if cfg.memory.long_term_enabled:
        try:
            from openclaw.memory.long_term import LongTermStore

            long_term = LongTermStore(cfg.memory.dir / "long_term")
        except RuntimeError as e:
            # chromadb 未安装 — 降级为仅短期记忆
            logger.warning("long_term 已禁用(chromadb 未安装): %s", e)
        except Exception as e:  # noqa: BLE001
            logger.warning("long_term 初始化失败,降级为仅短期记忆: %s", e)

    soul = SoulLoader(paths=cfg.agent.soul_paths)
    return ScopedMemory(short_term=short, long_term=long_term, soul=soul)


def build_tools(cfg: OpenClawConfig):
    """构造 ToolRegistry 并注册内置工具。"""
    from openclaw.tools.builtin import register_builtin_tools
    from openclaw.tools.registry import ToolRegistry

    tools = ToolRegistry()
    register_builtin_tools(
        tools,
        fs_root=cfg.tools.fs_root,
        shell_default_cwd=cfg.tools.shell_default_cwd,
        shell_allowed=cfg.tools.shell_allowed,
        http_allowed_hosts=cfg.tools.http_allowed_hosts,
        include=cfg.tools.include,
        exclude=cfg.tools.exclude,
    )
    return tools


def build_agent_loop(
    config_path: Optional[Path] = None,
    *,
    provider_override: Optional[str] = None,
):
    """完整构建 AgentLoop(llm + tools + memory)。

    Returns:
        (AgentLoop, OpenClawConfig)
    """
    from openclaw.agent.loop import AgentLoop

    cfg, _ = load_config(config_path)
    llm = build_router(cfg, provider_override=provider_override)
    memory = build_memory(cfg)
    tools = build_tools(cfg)

    loop = AgentLoop(
        llm=llm,
        tools=tools,
        memory=memory,
        system_prompt=cfg.agent.system_prompt,
        max_tool_iterations=cfg.agent.max_tool_iterations,
        history_window=cfg.agent.history_window,
    )
    return loop, cfg
