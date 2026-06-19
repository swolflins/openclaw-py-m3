"""Redis Streams 后端(可选)。

需要安装 redis 包(在 [all] extra 里)。这里定义接口 + 一个可选实现;
如果环境没装 redis,get_redis_bus() 返回 None,调用方降级为进程内 EventBus。
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from openclaw.core.logging import get_logger

logger = get_logger(__name__)

try:
    import redis.asyncio as aioredis  # type: ignore[import-not-found]

    _HAS_REDIS = True
except Exception:  # pragma: no cover
    aioredis = None  # type: ignore[assignment]
    _HAS_REDIS = False


class RedisBus:
    """基于 Redis Streams 的跨进程事件总线。"""

    def __init__(self, url: str = "redis://localhost:6379/0", prefix: str = "openclaw") -> None:
        if not _HAS_REDIS:
            raise RuntimeError("redis 包未安装,请 `pip install openclaw-py[all]`")
        self._url = url
        self._prefix = prefix
        self._client: Any = None

    async def _get(self) -> Any:
        if self._client is None:
            self._client = aioredis.from_url(self._url, decode_responses=True)
        return self._client

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        c = await self._get()
        stream = f"{self._prefix}:{topic}"
        await c.xadd(stream, payload)

    async def subscribe(self, topic: str, handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        c = await self._get()
        stream = f"{self._prefix}:{topic}"
        last_id = "0"
        while True:
            res = await c.xread({stream: last_id}, block=5000, count=10)
            if not res:
                continue
            for _stream, entries in res:
                for entry_id, fields in entries:
                    last_id = entry_id
                    try:
                        await handler(fields)
                    except Exception:
                        logger.exception("redis_bus_handler_error", topic=topic)


def get_redis_bus(url: str = "redis://localhost:6379/0") -> Optional[RedisBus]:
    if not _HAS_REDIS:
        return None
    try:
        return RedisBus(url=url)
    except Exception:
        logger.warning("redis_bus_init_failed")
        return None
