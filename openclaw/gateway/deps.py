"""Gateway 依赖容器 — 把 AgentLoop / ScopedMemory / ChannelManager 集中起来。

设计原则:
- 单例,所有路由共享
- 通过 `attach()` 注入(便于测试 + 启动时延迟构建)
- 永远不假设一定有 LLM key(可以纯 offline 跑)
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from openclaw.agent.loop import AgentLoop
from openclaw.core.logging import get_logger

logger = get_logger(__name__)


def _default_journal_dir() -> Path:
    """默认 journal 目录:OPENCLAW_JOURNAL_DIR 或 ~/.openclaw/journal/。

    可以通过环境变量覆盖。
    """
    custom = os.environ.get("OPENCLAW_JOURNAL_DIR", "").strip()
    if custom:
        return Path(custom).expanduser()
    return Path.home() / ".openclaw" / "journal"


def make_default_journal():
    """构造一个默认 AgentJournal(无 LLM key 也能跑,TemplateReflector)。

    失败返回 None(磁盘满 / 权限不足等)— 不阻断 gateway 启动。
    """
    try:
        from openclaw.agent.journal import AgentJournal
        root = _default_journal_dir()
        root.mkdir(parents=True, exist_ok=True)
        return AgentJournal(root=root)
    except Exception as e:  # noqa: BLE001
        logger.warning("default journal 构造失败(不影响启动): %s", e)
        return None


@dataclass
class GatewayDeps:
    """Gateway 用到的全部依赖。

    字段:
    - agent_loop: AgentLoop 实例(可能为 None,这种时候 /v1/chat 会返回 503)
    - config: OpenClawConfig(可能为 None)
    - config_path: 配置 yaml 路径(供 /v1/config 查看)
    - journal: AgentJournal 实例(可能为 None,无 journal 时 /v1/journal/* 会 503)
    - extra: 路由可自由读写的扩展点
    """

    agent_loop: Optional[AgentLoop] = None
    config: Any = None
    config_path: Optional[Path] = None
    journal: Any = None  # AgentJournal (避免循环 import 写 Any)
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
