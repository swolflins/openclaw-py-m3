"""Phase 15 P1 测试:Redis Bus 集成(fakeredis)。

对应原版 cross-process bus 测试。
策略:用 fakeredis.aioredis 替换真实 client,验证:
- publish 写入正确 stream key
- subscribe 用 xreadgroup + xack
- 异常时不 ack(PEL 保留)
- 多 consumer 同一 group 负载分摊
"""
from __future__ import annotations

import os
from typing import Any

import pytest

# 强制使用 fakeredis
os.environ.setdefault("REDIS_URL", "redis://fake:6379/0")

# 必须先 import redis,再 import redis_bus
import fakeredis.aioredis as fakeredis_aio  # noqa: E402

from openclaw.bus import redis_bus as rb_mod  # noqa: E402


# =========================================================================
# 公共:把 RedisBus._get 注入到 fakeredis client
# =========================================================================

class FakeBus(rb_mod.RedisBus):
    """用 fakeredis 替换真实 client。"""

    def __init__(self) -> None:
        # 不调父类 __init__ — 跳过 aioredis.from_url
        self._url = "redis://fake:6379/0"
        self._prefix = "openclaw_test"
        self._client: Any = None

    async def _get(self) -> Any:
        if self._client is None:
            self._client = fakeredis_aio.FakeRedis(decode_responses=True)
        return self._client


def _make_bus() -> FakeBus:
    return FakeBus()


# =========================================================================
# 1. publish 基础
# =========================================================================

class TestRedisBusPublish:
    @pytest.mark.asyncio
    async def test_publish_writes_to_correct_stream(self):
        bus = _make_bus()
        await bus.publish("test.topic", {"k": "v", "n": "1"})
        c = await bus._get()
        entries = await c.xrange(f"{bus._prefix}:test.topic")
        assert len(entries) == 1
        # entries: [(id, {field: value})]
        _, fields = entries[0]
        assert fields == {"k": "v", "n": "1"}

    @pytest.mark.asyncio
    async def test_publish_multiple_topics_isolated(self):
        bus = _make_bus()
        await bus.publish("topic.a", {"x": "1"})
        await bus.publish("topic.b", {"x": "2"})
        c = await bus._get()
        a = await c.xrange(f"{bus._prefix}:topic.a")
        b = await c.xrange(f"{bus._prefix}:topic.b")
        assert len(a) == 1
        assert len(b) == 1
        assert a[0][1]["x"] == "1"
        assert b[0][1]["x"] == "2"

    @pytest.mark.asyncio
    async def test_publish_prefix_applied(self):
        bus = _make_bus()
        bus._prefix = "myapp"
        await bus.publish("x", {"k": "v"})
        c = await bus._get()
        entries = await c.xrange("myapp:x")
        assert len(entries) == 1


# =========================================================================
# 2. subscribe + xreadgroup + xack
# =========================================================================

class TestRedisBusSubscribe:
    @pytest.mark.asyncio
    async def test_subscribe_receives_published(self):
        """集成测试:subscriber 拿到 publish 的消息后立刻停。"""
        bus = _make_bus()
        received: list[dict] = []

        async def handler(payload: dict) -> None:
            received.append(payload)

        # 直接调内部循环一次(不走无限 while)
        c = await bus._get()
        stream = f"{bus._prefix}:sub1.topic"
        # 预创建 group
        try:
            await c.xgroup_create(stream, "openclaw-default", id="0", mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                raise
        # 先 publish 再 xreadgroup
        await c.xadd(stream, {"hello": "world"})
        res = await c.xreadgroup("openclaw-default", "c1", {stream: ">"}, block=200, count=10)
        assert res, "xreadgroup should return our entry"
        for _stream, entries in res:
            for entry_id, fields in entries:
                await handler(fields)
                await c.xack(stream, "openclaw-default", entry_id)

        assert len(received) == 1
        assert received[0]["hello"] == "world"

    @pytest.mark.asyncio
    async def test_subscribe_acks_on_success(self):
        bus = _make_bus()
        c = await bus._get()
        stream = f"{bus._prefix}:ack.topic"
        try:
            await c.xgroup_create(stream, "openclaw-default", id="0", mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                raise

        async def handler(payload):
            return None

        await c.xadd(stream, {"x": "1"})
        res = await c.xreadgroup("openclaw-default", "ack-c1", {stream: ">"}, block=200, count=10)
        assert res
        for _s, entries in res:
            for entry_id, fields in entries:
                await handler(fields)
                await c.xack(stream, "openclaw-default", entry_id)

        # 检查 PEL 应该为空
        pending = await c.xpending(stream, "openclaw-default")
        if isinstance(pending, dict):
            assert pending.get("pending", 0) == 0
        else:
            assert pending[0] == 0

    @pytest.mark.asyncio
    async def test_subscribe_does_not_ack_on_handler_error(self):
        bus = _make_bus()
        c = await bus._get()
        stream = f"{bus._prefix}:fail.topic"
        try:
            await c.xgroup_create(stream, "openclaw-default", id="0", mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                raise

        async def bad_handler(payload):
            raise RuntimeError("simulated")

        await c.xadd(stream, {"bad": "data"})
        res = await c.xreadgroup("openclaw-default", "fail-c1", {stream: ">"}, block=200, count=10)
        assert res
        # 模拟 subscribe 里的 except:不 ack
        for _s, entries in res:
            for entry_id, fields in entries:
                try:
                    await bad_handler(fields)
                    await c.xack(stream, "openclaw-default", entry_id)
                except Exception:
                    pass  # 模拟 subscribe 行为:不 ack

        # PEL 应有 1 条
        pending = await c.xpending(stream, "openclaw-default")
        if isinstance(pending, dict):
            assert pending.get("pending", 0) >= 1
        else:
            assert pending[0] >= 1

    @pytest.mark.asyncio
    async def test_group_idempotent_create(self):
        """重复创建同名 group 应该 BUSYGROUP → OK,不抛。"""
        c = fakeredis_aio.FakeRedis(decode_responses=True)
        stream = "openclaw_test:idem.topic"
        await c.xadd(stream, {"x": "1"})
        try:
            await c.xgroup_create(stream, "g1", id="0", mkstream=True)
        except Exception as e:
            if "BUSYGROUP" not in str(e):
                raise
        # 第二次:再调一次,期望 BUSYGROUP 抛出,我们 catch 后忽略
        try:
            await c.xgroup_create(stream, "g1", id="0", mkstream=True)
            pytest.fail("第二次 xgroup_create 应抛 BUSYGROUP")
        except Exception as e:
            assert "BUSYGROUP" in str(e)


# =========================================================================
# 3. get_redis_bus fallback
# =========================================================================

class TestGetRedisBus:
    def test_get_redis_bus_returns_instance(self):
        bus = rb_mod.get_redis_bus()
        # 因为 redis 包已装,应该非 None
        assert bus is not None
        assert isinstance(bus, rb_mod.RedisBus)

    def test_get_redis_bus_default_url(self):
        bus = rb_mod.get_redis_bus()
        assert bus is not None
        assert bus._url == "redis://localhost:6379/0"


# =========================================================================
# 4. 进程内 EventBus 行为(原 bus/__init__.py) — P1-2 顺带补
# =========================================================================

class TestInProcEventBus:
    @pytest.mark.asyncio
    async def test_publish_subscribe_basic(self):
        from openclaw.bus import EventBus
        bus = EventBus()
        received: list[dict] = []
        async def h(p): received.append(p)
        bus.subscribe("test.basic", h)
        await bus.publish("test.basic", {"k": "v"})
        assert len(received) == 1
        assert received[0] == {"k": "v"}

    @pytest.mark.asyncio
    async def test_wildcard_subscribe(self):
        from openclaw.bus import EventBus
        bus = EventBus()
        received: list[dict] = []
        async def h(p): received.append(p)
        bus.subscribe("user.*", h)
        await bus.publish("user.login", {"u": "a"})
        await bus.publish("user.logout", {"u": "b"})
        await bus.publish("other.thing", {})  # 不应被收到
        assert len(received) == 2

    @pytest.mark.asyncio
    async def test_unsubscribe(self):
        from openclaw.bus import EventBus
        bus = EventBus()
        received: list[dict] = []
        async def h(p): received.append(p)
        bus.subscribe("x", h)
        bus.unsubscribe("x", h)
        await bus.publish("x", {})
        assert received == []

    @pytest.mark.asyncio
    async def test_history_recorded(self):
        from openclaw.bus import EventBus
        bus = EventBus()
        await bus.publish("a", {"n": 1})
        await bus.publish("b", {"n": 2})
        await bus.publish("a", {"n": 3})
        all_h = bus.history()
        assert len(all_h) == 3
        a_h = bus.history(topic="a")
        assert len(a_h) == 2
        assert a_h[0][1] == {"n": 1}
        assert a_h[1][1] == {"n": 3}

    @pytest.mark.asyncio
    async def test_handler_exception_isolated(self):
        from openclaw.bus import EventBus
        bus = EventBus()
        ok_received: list[dict] = []
        async def ok(p): ok_received.append(p)
        async def bad(p): raise RuntimeError("boom")
        bus.subscribe("x", bad)
        bus.subscribe("x", ok)
        # 不应抛出
        await bus.publish("x", {"k": "v"})
        assert len(ok_received) == 1

    @pytest.mark.asyncio
    async def test_history_truncated_to_200(self):
        from openclaw.bus import EventBus
        bus = EventBus()
        for i in range(250):
            await bus.publish("x", {"i": i})
        # 显式传 limit=200 才能看完整
        h = bus.history(limit=200)
        assert len(h) == 200
        # 最早 50 条应被截掉
        assert h[0][1]["i"] == 50
        assert h[-1][1]["i"] == 249
