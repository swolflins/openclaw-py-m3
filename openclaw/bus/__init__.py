"""事件总线(进程内 + 可选 Redis Streams 跨进程)。

典型用法:
    bus = get_bus()
    bus.subscribe("message.incoming", my_handler)
    await bus.publish("message.incoming", {"text": "..."})
"""
from __future__ import annotations

import asyncio
import fnmatch
import threading
from collections import defaultdict
from typing import Any, Awaitable, Callable

from openclaw.core.logging import get_logger

logger = get_logger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[None]]


class EventBus:
    """进程内异步事件总线。"""

    def __init__(self) -> None:
        self._subs: dict[str, list[Handler]] = defaultdict(list)
        self._lock = threading.RLock()
        self._history: list[tuple[str, dict[str, Any]]] = []  # 最近 200 条

    def subscribe(self, topic: str, handler: Handler) -> None:
        with self._lock:
            self._subs[topic].append(handler)

    def unsubscribe(self, topic: str, handler: Handler) -> None:
        with self._lock:
            if handler in self._subs.get(topic, []):
                self._subs[topic].remove(handler)

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        self._history.append((topic, payload))
        if len(self._history) > 200:
            self._history = self._history[-200:]

        # 直接订阅 + 模式订阅(topic 通配符 * ?)
        handlers: list[Handler] = []
        with self._lock:
            for pattern, lst in self._subs.items():
                if pattern == topic or _match_topic(pattern, topic):
                    handlers.extend(lst)
        if not handlers:
            return

        results = await asyncio.gather(
            *(h(payload) for h in handlers), return_exceptions=True
        )
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.warning(
                    "handler_error",
                    topic=topic,
                    handler=getattr(handlers[i], "__name__", "?"),
                    error=str(r),
                )

    def history(self, topic: str | None = None, limit: int = 50) -> list[tuple[str, dict[str, Any]]]:
        items = [h for h in self._history if topic is None or h[0] == topic]
        return items[-limit:]


_default_bus: EventBus | None = None
_default_lock = threading.Lock()


def get_bus() -> EventBus:
    global _default_bus
    with _default_lock:
        if _default_bus is None:
            _default_bus = EventBus()
        return _default_bus


def _match_topic(pattern: str, topic: str) -> bool:
    """简单 topic 匹配:支持 * 和 ? 通配符。"""
    if "*" not in pattern and "?" not in pattern:
        return False
    return fnmatch.fnmatchcase(topic, pattern)
