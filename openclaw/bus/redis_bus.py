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
            # Phase 30 / L3 修复:每个循环前尝试 XAUTOCLAIM 抢 stale 任务
            # (idle > stale_idle_ms,默认 60s),把死掉的 consumer 的 PEL
            # 任务转给当前 consumer。防止 consumer 进程崩了之后任务卡在 PEL。
            try:
                await self._reclaim_stale(stream, group, consumer, stale_idle_ms=60_000)
            except Exception:
                logger.exception("redis_bus_xautoclaim_failed", topic=topic)
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
                        # 不 ack → pending,后续可 XCLAIM / XAUTOCLAIM 重投

    async def _reclaim_stale(
        self,
        stream: str,
        group: str,
        consumer: str,
        *,
        stale_idle_ms: int = 60_000,
        count: int = 10,
    ) -> int:
        """Phase 30 / L3 修复:用 XAUTOCLAIM 把 dead consumer 的 PEL 任务
        转给当前 consumer。返回重投的条目数。

        用途:消费者进程 OOM kill / 死锁时,任务卡在它的 PEL;XAUTOCLAIM
        把 idle > ``stale_idle_ms`` 的任务转给活着的人,避免"无人处理"。
        """
        c = await self._get()
        try:
            # XAUTOCLAIM key group consumer min-idle-time start [COUNT count] [JUSTID]
            res = await c.xautoclaim(
                stream, group, consumer, stale_idle_ms, "0-0", count=count,
            )
        except Exception:  # pragma: no cover
            logger.exception("xautoclaim_call_failed", stream=stream)
            return 0
        # res = (next_cursor, claimed_entries, deleted_ids)
        if not res:
            return 0
        claimed = res[1] if len(res) > 1 else []
        if claimed:
            logger.info(
                "redis_bus_reclaimed_stale",
                stream=stream, count=len(claimed),
            )
        return len(claimed) if claimed else 0

    async def aclose(self) -> None:
        """Phase 30 / L3 修复:优雅关闭,关掉底层 aioredis 客户端。

        应用层应在 ``finally`` 块或 lifespan 退出时调本方法,
        防 redis 连接泄漏(M11 graceful shutdown 链路需要这个)。
        """
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # pragma: no cover
                logger.exception("redis_bus_aclose_failed")
            finally:
                self._client = None


def get_redis_bus(url: str = "redis://localhost:6379/0") -> Optional[RedisBus]:
    if not _HAS_REDIS:
        return None
    try:
        return RedisBus(url=url)
    except Exception:
        logger.warning("redis_bus_init_failed")
        return None
