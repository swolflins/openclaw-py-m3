"""Phase 23:Gateway 消息线程(reply / "1 条回复" 效果)。

覆盖:
- MessageStore 单元:add / get / list / count_replies / clear / LRU
- ChatRequest 加 reply_to_id 字段(向后兼容,不传也行)
- /v1/chat 返回 message_id / reply_to_id / reply_count
- /v1/chat/stream SSE 含 message 事件(user + assistant)
- /v1/sessions/{sid}/messages/{msg_id} 查父消息
- 跨 session 引用被拒(404)
- /v1/sessions DELETE 同步清 UI 消息
"""
from __future__ import annotations

import asyncio
import json
import re

import pytest
from fastapi.testclient import TestClient


# ──────────── 极简 Mock AgentLoop ────────────

class _FakeAgentLoop:
    """只要有 .handle(session_id, message) 异步返回 AgentResponse-like 即可。"""
    def __init__(self, reply_text: str = "hi from agent") -> None:
        self.reply_text = reply_text
        self.handled: list[tuple[str, str]] = []

    async def handle(self, session_id: str, message: str):
        from openclaw.agent.loop import AgentResponse
        self.handled.append((session_id, message))
        return AgentResponse(
            content=self.reply_text,
            iterations=1,
            tool_calls=[],
            session_id=session_id,
        )


@pytest.fixture
def deps():
    """带 fake agent_loop 的 GatewayDeps;每个 test 独立 message_store。"""
    from openclaw.gateway.deps import GatewayDeps, set_deps, reset_deps
    from openclaw.gateway.message_store import MessageStore

    loop = _FakeAgentLoop()
    deps = GatewayDeps(agent_loop=loop, extra={"message_store": MessageStore()})
    set_deps(deps)
    yield deps
    reset_deps()


@pytest.fixture
def client(deps):
    from openclaw.gateway.app import create_app
    return TestClient(create_app(rate_limiter=None))


# ═══════════════ MessageStore 单元 ═══════════════

class TestMessageStore:
    def test_add_and_get(self):
        from openclaw.gateway.message_store import MessageStore
        ms = MessageStore()
        asyncio.run(ms.add("s1", "user", "hello"))
        msgs = asyncio.run(ms.list_session("s1", k=10))
        assert len(msgs) == 1
        assert msgs[0].content == "hello"
        assert msgs[0].role == "user"
        assert msgs[0].parent_id is None
        # 用 msg_id 反查
        sm = asyncio.run(ms.get(msgs[0].msg_id))
        assert sm is not None
        assert sm.content == "hello"

    def test_add_with_parent_id(self):
        from openclaw.gateway.message_store import MessageStore
        ms = MessageStore()
        parent = asyncio.run(ms.add("s1", "user", "你好"))
        child = asyncio.run(ms.add("s1", "assistant", "回复", parent_id=parent.msg_id))
        assert child.parent_id == parent.msg_id

    def test_count_replies(self):
        """飞书"1 条回复"那个数字。"""
        from openclaw.gateway.message_store import MessageStore
        ms = MessageStore()
        root = asyncio.run(ms.add("s1", "user", "q"))
        # 没 reply
        assert asyncio.run(ms.count_replies("s1", root.msg_id)) == 0
        # 1 个 reply
        asyncio.run(ms.add("s1", "assistant", "a1", parent_id=root.msg_id))
        assert asyncio.run(ms.count_replies("s1", root.msg_id)) == 1
        # 2 个 reply(可以 reply 同一父消息多次,飞书实际也允许)
        asyncio.run(ms.add("s1", "user", "q2", parent_id=root.msg_id))
        assert asyncio.run(ms.count_replies("s1", root.msg_id)) == 2

    def test_cross_session_blocked(self):
        """msg_id 存在但在不同 session → get_in_session 返回 None。"""
        from openclaw.gateway.message_store import MessageStore
        ms = MessageStore()
        sm = asyncio.run(ms.add("s1", "user", "secret"))
        # 别的 session 查不到
        assert asyncio.run(ms.get_in_session("s2", sm.msg_id)) is None
        # 正确 session 能查到
        assert asyncio.run(ms.get_in_session("s1", sm.msg_id)) is not None

    def test_clear_session(self):
        from openclaw.gateway.message_store import MessageStore
        ms = MessageStore()
        asyncio.run(ms.add("s1", "user", "a"))
        asyncio.run(ms.add("s1", "user", "b"))
        asyncio.run(ms.add("s2", "user", "c"))
        cleared = asyncio.run(ms.clear_session("s1"))
        assert cleared == 2
        assert asyncio.run(ms.list_session("s1", k=10)) == []
        # s2 不受影响
        assert len(asyncio.run(ms.list_session("s2", k=10))) == 1

    def test_lru_eviction(self):
        from openclaw.gateway.message_store import MessageStore
        ms = MessageStore(max_per_session=3)
        m1 = asyncio.run(ms.add("s1", "user", "1"))
        m2 = asyncio.run(ms.add("s1", "user", "2"))
        m3 = asyncio.run(ms.add("s1", "user", "3"))
        m4 = asyncio.run(ms.add("s1", "user", "4"))  # 触发 LRU 砍头部
        # m1 已被淘汰
        assert asyncio.run(ms.get(m1.msg_id)) is None
        # m2/m3/m4 还在
        assert asyncio.run(ms.get(m2.msg_id)) is not None
        assert asyncio.run(ms.get(m3.msg_id)) is not None
        assert asyncio.run(ms.get(m4.msg_id)) is not None

    def test_list_session_recent_first(self):
        from openclaw.gateway.message_store import MessageStore
        ms = MessageStore()
        asyncio.run(ms.add("s1", "user", "oldest"))
        asyncio.run(ms.add("s1", "user", "middle"))
        asyncio.run(ms.add("s1", "user", "newest"))
        msgs = asyncio.run(ms.list_session("s1", k=10))
        # 倒序:最新在前
        assert [m.content for m in msgs] == ["newest", "middle", "oldest"]


# ═══════════════ /v1/chat API ═══════════════

class TestChatProtocol:
    def test_request_accepts_reply_to_id(self):
        """ChatRequest 加了 reply_to_id 字段,Optional,向后兼容。"""
        from openclaw.gateway.routes.chat import ChatRequest
        r = ChatRequest(message="hi", reply_to_id="abc123")
        assert r.reply_to_id == "abc123"
        # 不传也行
        r2 = ChatRequest(message="hi")
        assert r2.reply_to_id is None

    def test_response_has_message_id(self, client):
        """chat 响应里带 message_id + reply_to_id + reply_count。"""
        r = client.post("/v1/chat", json={"message": "你好"})
        assert r.status_code == 200
        j = r.json()
        assert j["message_id"] is not None
        assert len(j["message_id"]) == 12  # hex
        assert j["reply_to_id"] is not None  # ← assistant 的 parent = user 消息
        assert j["reply_to_id"] != j["message_id"]
        assert j["reply_count"] == 1  # user 消息被 reply 1 次(就是这次 assistant)

    def test_assistant_parent_equals_user_message_id(self, client):
        """assistant 的 reply_to_id == user 消息的 message_id(同 session 闭环)。"""
        # 拉 user 消息的 message_id
        r = client.post("/v1/chat", json={"message": "hi"})
        asst_mid = r.json()["message_id"]
        user_mid = r.json()["reply_to_id"]
        # 反查 user 消息
        r2 = client.get(f"/v1/sessions/default/messages/{user_mid}")
        assert r2.status_code == 200
        user_msg = r2.json()["message"]
        assert user_msg["msg_id"] == user_mid
        assert user_msg["role"] == "user"
        assert user_msg["content"] == "hi"
        assert user_msg["parent_id"] is None
        # 反查 assistant 消息
        r3 = client.get(f"/v1/sessions/default/messages/{asst_mid}")
        asst_msg = r3.json()["message"]
        assert asst_msg["role"] == "assistant"
        assert asst_msg["parent_id"] == user_mid

    def test_client_reply_to_id_stored_on_user_message(self, client):
        """client 带 reply_to_id → user 消息的 parent_id 就是它。"""
        # 先发一条 user
        r1 = client.post("/v1/chat", json={"message": "你好"})
        first_user_mid = r1.json()["reply_to_id"]
        # 再发一条,这次 reply 到上一条
        r2 = client.post("/v1/chat", json={
            "message": "接着说",
            "reply_to_id": first_user_mid,
        })
        assert r2.status_code == 200
        # 新 user 消息的 parent 是上一条 user
        new_user_mid = r2.json()["reply_to_id"]
        r3 = client.get(f"/v1/sessions/default/messages/{new_user_mid}")
        new_user = r3.json()["message"]
        assert new_user["parent_id"] == first_user_mid
        # 上一条 user 消息被 reply 了 1 次
        r4 = client.get(f"/v1/sessions/default/messages/{first_user_mid}")
        first_user = r4.json()["message"]
        # message_id 仍可拿到,parent_id 是 None(它是根)
        assert first_user["parent_id"] is None

    def test_no_agent_loop_returns_503(self, client_no_loop):
        r = client_no_loop.post("/v1/chat", json={"message": "hi"})
        assert r.status_code == 503

    def test_backward_compatible_no_reply_to_id(self, client):
        """老 client 不传 reply_to_id 仍能跑(向后兼容)。"""
        r = client.post("/v1/chat", json={"message": "hi"})
        assert r.status_code == 200
        # 字段都在
        for k in ("message_id", "reply_to_id", "reply_count"):
            assert k in r.json()


@pytest.fixture
def client_no_loop():
    from openclaw.gateway.deps import GatewayDeps, set_deps, reset_deps
    from openclaw.gateway.app import create_app
    from openclaw.gateway.message_store import MessageStore
    set_deps(GatewayDeps(agent_loop=None, extra={"message_store": MessageStore()}))
    yield TestClient(create_app(rate_limiter=None))
    reset_deps()


# ═══════════════ /v1/chat/stream SSE ═══════════════

class TestChatStreamProtocol:
    def _parse_sse_events(self, response_text: str) -> list[tuple[str, dict]]:
        """解析 SSE text/event-stream 文本,返回 [(event_name, data_dict), ...]。

        SSE 块用 CRLF (\\r\\n\\r\\n) 分隔,不是 LF;行用 \\r\\n 隔开。
        """
        events = []
        # 标准化分隔:把 \r\n\r\n 和 \n\n 都视为块分隔
        text = response_text.replace("\r\n", "\n")
        for block in re.split(r"\n\n+", text.strip()):
            if not block:
                continue
            ev_name = None
            data_lines: list[str] = []
            for line in block.splitlines():
                if line.startswith("event: "):
                    ev_name = line[len("event: "):]
                elif line.startswith("data: "):
                    data_lines.append(line[len("data: "):])
            if ev_name and data_lines:
                try:
                    events.append((ev_name, json.loads("\n".join(data_lines))))
                except json.JSONDecodeError:
                    pass
        return events

    def test_stream_emits_message_events(self, client):
        with client.stream("POST", "/v1/chat/stream", json={"message": "hi"}) as r:
            assert r.status_code == 200
            text = r.read().decode("utf-8")
        events = self._parse_sse_events(text)
        # 关键:应该有 2 个 message 事件(user + assistant)
        message_events = [e for e in events if e[0] == "message"]
        assert len(message_events) == 2
        user_ev, asst_ev = message_events
        # user event
        assert user_ev[1]["role"] == "user"
        assert user_ev[1]["message_id"] is not None
        assert user_ev[1]["reply_to_id"] is None  # 没带 reply_to
        # assistant event
        assert asst_ev[1]["role"] == "assistant"
        assert asst_ev[1]["message_id"] is not None
        assert asst_ev[1]["reply_to_id"] == user_ev[1]["message_id"]  # ← 关键:assistant 关联到 user
        assert asst_ev[1]["reply_count"] == 1  # user 消息被 reply 1 次
        # done 事件也带 message_id
        done_ev = next(e for e in events if e[0] == "done")
        assert done_ev[1]["message_id"] == asst_ev[1]["message_id"]
        assert done_ev[1]["reply_to_id"] == user_ev[1]["message_id"]

    def test_stream_client_reply_to_id_propagates(self, client):
        """client 带 reply_to_id → user message event 也会带。"""
        # 先发一条
        r1 = client.post("/v1/chat", json={"message": "first"})
        first_user_mid = r1.json()["reply_to_id"]
        # stream 发 reply
        with client.stream("POST", "/v1/chat/stream", json={
            "message": "reply to first",
            "reply_to_id": first_user_mid,
        }) as r:
            text = r.read().decode("utf-8")
        events = self._parse_sse_events(text)
        user_ev = next(e for e in events if e[0] == "message" and e[1]["role"] == "user")
        assert user_ev[1]["reply_to_id"] == first_user_mid


# ═══════════════ /v1/sessions/{sid}/messages/{msg_id} ═══════════════

class TestSessionMessagesAPI:
    def test_get_existing_message(self, client):
        r = client.post("/v1/chat", json={"message": "hi"})
        mid = r.json()["reply_to_id"]
        r2 = client.get(f"/v1/sessions/default/messages/{mid}")
        assert r2.status_code == 200
        j = r2.json()["message"]
        assert j["msg_id"] == mid
        assert j["role"] == "user"
        assert j["content"] == "hi"

    def test_get_404_for_missing(self, client):
        r = client.get("/v1/sessions/default/messages/zzzzzzzzzzzz")
        assert r.status_code == 404
        assert "not found" in r.json()["detail"].lower()

    def test_cross_session_lookup_404(self, client):
        """msg 存在但跨 session → 404(防引用泄漏)。"""
        r = client.post("/v1/chat", json={"message": "hi", "session_id": "sA"})
        mid = r.json()["reply_to_id"]
        # 另一个 session 查不到
        r2 = client.get(f"/v1/sessions/sB/messages/{mid}")
        assert r2.status_code == 404

    def test_list_messages_returns_recent_first(self, client):
        for i in range(3):
            client.post("/v1/chat", json={"message": f"msg{i}"})
        r = client.get("/v1/sessions/default/messages?k=10")
        assert r.status_code == 200
        j = r.json()
        assert j["count"] == 6  # 3 user + 3 assistant
        # 最新在前
        assert j["messages"][0]["role"] == "assistant"
        # 至少有 1 条是 reply 另一条
        replies = [m for m in j["messages"] if m["parent_id"]]
        assert len(replies) >= 3  # 3 个 assistant 都 reply 到 user

    def test_clear_session_also_clears_ui_messages(self, client):
        client.post("/v1/chat", json={"message": "hi"})
        r = client.delete("/v1/sessions/default")
        assert r.status_code == 200
        j = r.json()
        # memory 字段视 agent_loop 是否带 memory;ui_messages_cleared 才是关键
        assert j["ui_messages_cleared"] >= 2  # user + assistant
        # 现在列消息应该空
        r2 = client.get("/v1/sessions/default/messages")
        assert r2.json()["count"] == 0


# ═══════════════ 没 deps / 没 message_store 也能跑 ═══════════════

class TestRobustness:
    def test_creates_message_store_on_demand(self):
        """即使 deps.extra 没 message_store,chat 调用时也会自动建一个。"""
        from openclaw.gateway.deps import GatewayDeps, set_deps, reset_deps
        from openclaw.gateway.app import create_app

        loop = _FakeAgentLoop()
        set_deps(GatewayDeps(agent_loop=loop, extra={}))  # 没 message_store
        c = TestClient(create_app(rate_limiter=None))
        r = c.post("/v1/chat", json={"message": "hi"})
        assert r.status_code == 200
        assert r.json()["message_id"] is not None
        reset_deps()
