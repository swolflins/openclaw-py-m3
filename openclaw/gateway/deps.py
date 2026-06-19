"""Gateway 依赖容器 — 把 AgentLoop / ScopedMemory / ChannelManager 集中起来。

设计原则:
- 单例,所有路由共享
- 通过 `attach()` 注入(便于测试 + 启动时延迟构建)
- 永远不假设一定有 LLM key(可以纯 offline 跑)
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from openclaw.agent.loop import AgentLoop
from openclaw.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class GatewayDeps:
    """Gateway 用到的全部依赖。

    字段:
    - agent_loop: AgentLoop 实例(可能为 None,这种时候 /v1/chat 会返回 503)
    - config: OpenClawConfig(可能为 None)
    - config_path: 配置 yaml 路径(供 /v1/config 查看)
    - extra: 路由可自由读写的扩展点
    """

    agent_loop: Optional[AgentLoop] = None
    config: Any = None
    config_path: Optional[Path] = None
    started_at: float = field(default_factory=lambda: __import__("time").time())
    extra: dict[str, Any] = field(default_factory=dict)

    def ready(self) -> bool:
        return self.agent_loop is not None

    def uptime(self) -> float:
        import time

        return time.time() - self.started_at


# 全局单例
_deps: GatewayDeps | None = None
_lock = asyncio.Lock()


def get_deps() -> GatewayDeps:
    global _deps
    if _deps is None:
        _deps = GatewayDeps()
    return _deps


def set_deps(deps: GatewayDeps) -> None:
    """测试 / 启动脚本可用 set_deps() 注入。"""
    global _deps
    _deps = deps


def reset_deps() -> None:
    """测试用 — 还原默认空 deps。"""
    global _deps
    _deps = None
