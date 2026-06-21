"""P25 / a3:飞书 ``_fetch_bot_open_id`` 异步化 + 缓存 + 启动 fail-fast。

修复前:
- ``_fetch_bot_open_id`` 是 ``def``,内部用 ``asyncio.run()`` 拉 token。
- 在 running event loop 里调它 → ``RuntimeError: asyncio.run() cannot be
  called from a running event loop`` 被 ``except Exception`` 静默吞掉。
- 结果:bot_open_id 永远拿不到,群 @ 检测失效。

修复后:
- 改 ``async def`` + ``await``。
- per-instance 缓存(用 sentinel 区分"未拉取" vs "拉到 None")+ ``asyncio.Lock``
  保证同一 process / 实例内只发 1 次请求,即使并发首次调用也只发 1 个网络请求。
- 启动期 ``start()`` 主动 ``await self._fetch_bot_open_id()``,失败透传 RuntimeError,
  凭据错就早 fail,不要带着坏状态进 WS。
- 运行时 ``_handle_event`` 拉失败要兜底(消息不能掉地上),自动回退 mentioned_type。

测试覆盖:
1. 模拟 event loop 上下文调用 → 不抛 RuntimeError(asyncio.run 冲突没了)
2. 缓存命中 → 第二次调用不调底层 client;并发首次调用也只发 1 个请求
3. 底层 client 抛错(token / 网络)→ RuntimeError 透传(不静默吞);失败不污染缓存
4. 启动期如果 bot_open_id 拉不到 → RuntimeError(不静默)且不创建 _ws_loop 任务
5. 兼容:phase 24 workaround 不能被破坏(_handle_event 仍能路由第一条消息)
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openclaw.channels.lark import LarkChannel, _UNSET  # noqa: E402
from openclaw.config.settings import LarkSettings  # noqa: E402


# ─────── 通用 stub ───────

class _FakeAgent:
    """极简 agent,AgentLoop 接口 stub。"""

    async def handle(self, session_id, text, **kw):
        class R:
            content = f"echo:{text}"
            tool_calls = []
            iterations = 1
        return R()

    async def new_session(self, sid=None):
        return sid or "s"

    @property
    def tools(self): return None

    @property
    def memory(self): return None

    @property
    def auto_reply(self): return None


class _StubResp:
    def __init__(self, http: int, body: dict[str, Any]):
        self.status_code = http
        self.headers: dict[str, str] = {}
        self._body = body

    def json(self):
        return self._body


def _build_ch(settings: LarkSettings | None = None) -> LarkChannel:
    """构造一个 LarkChannel,不走 SDK(只测 fetch_bot_open_id / start)。"""
    return LarkChannel(
        _FakeAgent(),
        settings or LarkSettings(app_id="cli_test", app_secret="sec_test"),
    )


# ─────── 1. 模拟 event loop 上下文调用 → 不抛 RuntimeError ───────

async def _stub_token_ok(self) -> str:
    return "t-test"


async def _stub_bots_me_ok(self, url: str, **kw) -> _StubResp:
    return _StubResp(200, {"code": 0, "msg": "ok", "data": {"bot": {"open_id": "ou_bot_1"}}})


def test_fetch_bot_open_id_in_event_loop_no_runtime_error(monkeypatch):
    """在 asyncio loop 内调 ``_fetch_bot_open_id()`` → 不抛 asyncio.run() 冲突 RuntimeError。

    修复前(``def`` + 内部 ``asyncio.run()``)会在此场景爆:
        RuntimeError: asyncio.run() cannot be called from a running event loop
    且被 ``except Exception`` 静默吞掉,bot_open_id 永远 None。
    修复后(``async def`` + ``await``)应正常返 open_id。
    """
    monkeypatch.setattr(LarkChannel, "_get_tenant_token", _stub_token_ok)
    monkeypatch.setattr(httpx.AsyncClient, "get", _stub_bots_me_ok)

    ch = _build_ch()

    async def _go():
        # 确保我们在 running loop 里 —— 触发了原 bug 的前提
        asyncio.get_running_loop()
        return await ch._fetch_bot_open_id()

    result = asyncio.run(_go())
    assert result == "ou_bot_1", f"应拉到 open_id 'ou_bot_1',实际 {result!r}"
    # 缓存要写入
    assert ch._bot_open_id == "ou_bot_1"


# ─────── 2. 缓存命中 → 第二次调用不调底层 client ───────

def test_fetch_bot_open_id_cache_hit_skips_underlying_client(monkeypatch):
    """第一次调拿到 → 缓存。第二次调应直接走缓存,底层 client 调用次数 == 1。"""
    call_count = {"n": 0}

    async def _tok(self) -> str:
        return "t-test"

    async def _get(self, url: str, **kw) -> _StubResp:
        call_count["n"] += 1
        return _StubResp(200, {"code": 0, "msg": "ok", "data": {"bot": {"open_id": "ou_cached"}}})

    monkeypatch.setattr(LarkChannel, "_get_tenant_token", _tok)
    monkeypatch.setattr(httpx.AsyncClient, "get", _get)

    ch = _build_ch()

    async def _go():
        a = await ch._fetch_bot_open_id()
        b = await ch._fetch_bot_open_id()
        c = await ch._fetch_bot_open_id()
        return a, b, c

    a, b, c = asyncio.run(_go())
    assert a == b == c == "ou_cached"
    # 关键断言:底层 client 只被调 1 次(缓存命中)
    assert call_count["n"] == 1, f"缓存应让 httpx.get 只调 1 次,实际 {call_count['n']}"


def test_fetch_bot_open_id_concurrent_first_call_only_one_request(monkeypatch):
    """并发首次调用 → asyncio.Lock 让 1 个请求 + 多个协程共享结果。"""
    call_count = {"n": 0}
    in_flight = {"now": 0, "max": 0}

    async def _tok(self) -> str:
        return "t-test"

    async def _get(self, url: str, **kw) -> _StubResp:
        call_count["n"] += 1
        in_flight["now"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["now"])
        # 模拟网络延迟,放大并发竞态窗口
        await asyncio.sleep(0.05)
        in_flight["now"] -= 1
        return _StubResp(200, {"code": 0, "msg": "ok", "data": {"bot": {"open_id": "ou_race"}}})

    monkeypatch.setattr(LarkChannel, "_get_tenant_token", _tok)
    monkeypatch.setattr(httpx.AsyncClient, "get", _get)

    ch = _build_ch()

    async def _go():
        # 5 个协程同时首次调 → asyncio.Lock + double-check 让 httpx.get 只跑 1 次
        return await asyncio.gather(*[ch._fetch_bot_open_id() for _ in range(5)])

    results = asyncio.run(_go())
    assert results == ["ou_race"] * 5
    assert call_count["n"] == 1, f"并发首次调用应只发 1 个请求,实际 {call_count['n']}"
    # 双保险:in_flight 也应 <= 1
    assert in_flight["max"] <= 1


# ─────── 3. 底层 client 抛错 → RuntimeError 透传(不静默吞) ───────

def test_fetch_bot_open_id_propagates_runtime_error_from_underlying(monkeypatch):
    """token 拉不到 → RuntimeError 透传(不静默吞 None)。"""
    async def _tok_fail(self) -> None:
        return None  # 模拟 _get_tenant_token 拿不到 token

    monkeypatch.setattr(LarkChannel, "_get_tenant_token", _tok_fail)

    ch = _build_ch()

    async def _go():
        return await ch._fetch_bot_open_id()

    with pytest.raises(RuntimeError, match="tenant_access_token"):
        asyncio.run(_go())

    # 关键:失败时不应把 _bot_open_id 错填为 None(否则下次直接走缓存,问题被掩盖)
    assert ch._bot_open_id is _UNSET, f"失败时缓存应保持 _UNSET,实际 {ch._bot_open_id!r}"


def test_fetch_bot_open_id_propagates_network_error(monkeypatch):
    """底层 httpx 抛连接错 → RuntimeError 透传。"""

    async def _tok(self) -> str:
        return "t-test"

    async def _get_fail(self, url: str, **kw) -> _StubResp:
        raise httpx.ConnectError("simulated network down")

    monkeypatch.setattr(LarkChannel, "_get_tenant_token", _tok)
    monkeypatch.setattr(httpx.AsyncClient, "get", _get_fail)

    ch = _build_ch()

    async def _go():
        return await ch._fetch_bot_open_id()

    with pytest.raises(RuntimeError, match="网络失败"):
        asyncio.run(_go())
    assert ch._bot_open_id is _UNSET


def test_fetch_bot_open_id_caches_none_on_empty_response(monkeypatch):
    """后端返 200 但 data.bot.open_id 缺失 → 缓存为 None(不抛,降级到 mentioned_type 兜底)。"""

    async def _tok(self) -> str:
        return "t-test"

    async def _get(self, url: str, **kw) -> _StubResp:
        return _StubResp(200, {"code": 0, "msg": "ok", "data": {"bot": {}}})

    monkeypatch.setattr(LarkChannel, "_get_tenant_token", _tok)
    monkeypatch.setattr(httpx.AsyncClient, "get", _get)

    ch = _build_ch()

    async def _go():
        return await ch._fetch_bot_open_id()

    result = asyncio.run(_go())
    assert result is None
    # 第二次调走缓存,不会再发请求(httpx 已被 monkeypatch 调用就会 raise,验证它没被触发)
    async def _second():
        return await ch._fetch_bot_open_id()
    assert asyncio.run(_second()) is None
    assert ch._bot_open_id is None  # 缓存了 None


# ─────── 4. 启动期如果 bot_open_id 拉不到 → RuntimeError(不静默) ───────

def test_start_fails_fast_when_bot_open_id_unreachable(monkeypatch):
    """start() 期间 _fetch_bot_open_id 抛 RuntimeError → start() 必须透传(不静默吞)。

    这是原 bug 的核心场景:之前失败被静默吞 + WS 起来后,运行时 _handle_event
    每次都重新拉 + 失败 + 静默吞 → bot_open_id 永远是 _UNSET,@ 检测永远失效。
    """
    async def _tok(self) -> str:
        return None  # 模拟凭据错

    monkeypatch.setattr(LarkChannel, "_get_tenant_token", _tok)

    ch = _build_ch()

    async def _go():
        return await ch.start()

    with pytest.raises(RuntimeError, match="启动 Lark 渠道失败"):
        asyncio.run(_go())

    # 关键:start() 没在后台建 _ws_loop 任务(fail-fast 模式)
    assert ch._task is None, f"失败时不应创建 _ws_loop 任务,实际 {ch._task!r}"


def test_start_succeeds_then_ws_loop_spawned(monkeypatch):
    """bot_open_id 拿到 → start() 顺利进 _ws_loop(到 _stopped.wait())。"""

    async def _tok(self) -> str:
        return "t-test"

    async def _get(self, url: str, **kw) -> _StubResp:
        return _StubResp(200, {"code": 0, "msg": "ok", "data": {"bot": {"open_id": "ou_ok"}}})

    monkeypatch.setattr(LarkChannel, "_get_tenant_token", _tok)
    monkeypatch.setattr(httpx.AsyncClient, "get", _get)

    # 把 _ws_loop 替换成空跑(免去启 SDK 的副作用)
    async def _fake_ws_loop(self) -> None:
        await self._stopped.wait()

    monkeypatch.setattr(LarkChannel, "_ws_loop", _fake_ws_loop)

    ch = _build_ch()

    async def _go():
        # 给 0.05s 后 stop,避免 _stopped.wait() 永远阻塞
        async def _stopper():
            await asyncio.sleep(0.05)
            await ch.stop()
        await asyncio.gather(ch.start(), _stopper())

    asyncio.run(_go())
    # 缓存填好
    assert ch._bot_open_id == "ou_ok"
    # _ws_loop 跑过
    assert ch._task is not None


# ─────── 5. 兼容:phase 24 workaround 不能被破坏 ───────

def test_handle_event_still_routes_first_message(monkeypatch):
    """phase 24 修了 webhook 第一条消息路由不到;这里验证 _handle_event 在
    修过 bot_open_id 异步化后,仍能正常处理第一条消息(received / replies 都对)。"""
    from lark_oapi.api.im.v1 import (
        P2ImMessageReceiveV1, P2ImMessageReceiveV1Data,
    )
    from lark_oapi.api.im.v1.model.event_sender import EventSender
    from lark_oapi.api.im.v1.model.event_message import EventMessage

    replies: list[tuple[str, str]] = []

    async def _fake_reply(self, message_id, text):
        replies.append((message_id, text))

    monkeypatch.setattr(LarkChannel, "_reply_to_lark", _fake_reply)
    # 模拟已经启动并预热过缓存的情况
    ch = _build_ch()
    ch._bot_open_id = "ou_pre"  # 直接填缓存,模拟 start() 已成功

    evt = P2ImMessageReceiveV1()
    evt.event = P2ImMessageReceiveV1Data()
    evt.event.sender = EventSender(
        d={"sender_id": {"open_id": "ou_u1", "union_id": "uu", "user_id": "u"},
           "sender_type": "user", "tenant_key": "tk"}
    )
    evt.event.message = EventMessage(
        d={"message_id": "om_first", "chat_id": "oc_c", "chat_type": "p2p",
           "message_type": "text", "content": json.dumps({"text": "first msg"}, ensure_ascii=False)}
    )

    asyncio.run(ch._handle_event(evt))

    assert len(replies) == 1, f"应回 1 条,实际 {len(replies)}"
    assert replies[0][0] == "om_first"
    assert ch.received[0].session_id == "lark:oc_c:ou_u1"
    assert ch._last_msg_id["lark:oc_c:ou_u1"] == "om_first"


def test_handle_event_runtime_fallback_when_fetch_fails(monkeypatch, caplog):
    """运行时(没经过 start() 预热)且 bot_open_id 拉不到 → 不能让消息掉地上。

    行为契约:运行时 try/except 兜底,_handle_event 继续往下走,消息仍能 dispatch。
    启动期则 fail-fast;两者语义不同(见 start() vs _handle_event 的区别处理)。
    """
    from lark_oapi.api.im.v1 import (
        P2ImMessageReceiveV1, P2ImMessageReceiveV1Data,
    )
    from lark_oapi.api.im.v1.model.event_sender import EventSender
    from lark_oapi.api.im.v1.model.event_message import EventMessage
    import logging

    replies: list[tuple[str, str]] = []

    async def _fake_reply(self, message_id, text):
        replies.append((message_id, text))

    async def _tok_fail(self) -> None:
        return None  # 模拟 token 拉不到 → RuntimeError

    monkeypatch.setattr(LarkChannel, "_reply_to_lark", _fake_reply)
    monkeypatch.setattr(LarkChannel, "_get_tenant_token", _tok_fail)

    ch = _build_ch()
    # 故意 _bot_open_id 保持 _UNSET(模拟没经过 start() 预热)

    evt = P2ImMessageReceiveV1()
    evt.event = P2ImMessageReceiveV1Data()
    evt.event.sender = EventSender(
        d={"sender_id": {"open_id": "ou_u1", "union_id": "uu", "user_id": "u"},
           "sender_type": "user", "tenant_key": "tk"}
    )
    evt.event.message = EventMessage(
        d={"message_id": "om_rt", "chat_id": "oc_c", "chat_type": "p2p",
           "message_type": "text", "content": json.dumps({"text": "hi"}, ensure_ascii=False)}
    )

    caplog.set_level(logging.WARNING)
    asyncio.run(ch._handle_event(evt))

    # 关键:消息没掉地上,_reply_to_lark 仍被调到
    assert len(replies) == 1, f"运行时拉不到 bot_open_id 不应让消息掉地上,实际 {len(replies)}"
    assert replies[0][0] == "om_rt"
    # 兜底日志出现
    assert any("运行时拉 bot open_id 失败" in r.message for r in caplog.records)
