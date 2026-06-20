"""Redis Streams 后端(可选)。

需要安装 redis 包(在 [all] extra 里)。这里定义接口 + 一个可选实现;
如果环境没装 redis,get_redis_bus() 返回 None,调用方降级为进程内 EventBus。
"""
from __future__ import annotations

import asyncio
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

    async def subscribe(
        self,
        topic: str,
        handler: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        group: str = "openclaw-default",
        consumer: Optional[str] = None,
    ) -> None:
        """订阅 stream。

        ENG-7:用 xreadgroup + 消费组(不是裸 xread):
        - 多 worker 不会重复消费同一条
        - 失败可重投(XCLAIM)
        - 消费完 xack → 仍可后续查询(PEL 列表)
        """
        import uuid as _uuid

        c = await self._get()
        stream = f"{self._prefix}:{topic}"
        consumer = consumer or f"c-{_uuid.uuid4().hex[:8]}"
        # ensure group exists(MKSTREAM)
        try:
            await c.xgroup_create(stream, group, id="0", mkstream=True)
        except Exception as e:
            # BUSYGROUP → group 已存在,OK
            if "BUSYGROUP" not in str(e):
                raise

        while True:
            try:
                res = await c.xreadgroup(
                    group, consumer, {stream: ">"},
                    block=5000, count=10,
                )
            except Exception:
                logger.exception("redis_xreadgroup_failed, retrying")
                await asyncio.sleep(1)
                continue
            if not res:
                continue
            for _stream, entries in res:
                for entry_id, fields in entries:
                    try:
                        await handler(fields)
                        # 消费成功 → ack
                        await c.xack(stream, group, entry_id)
                    except Exception:
                        logger.exception(
                            "redis_bus_handler_error, NOT acked, will redeliver",
                            topic=topic, entry_id=entry_id,
                        )
                        # 不 ack → pending,后续可 XCLAIM 重投


def get_redis_bus(url: str = "redis://localhost:6379/0") -> Optional[RedisBus]:
    if not _HAS_REDIS:
        return None
    try:
        return RedisBus(url=url)
    except Exception:
        logger.warning("redis_bus_init_failed")
        return None
