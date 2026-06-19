"""Phase 7 单测:多渠道 + ChannelManager。

覆盖:
- IncomingMessage + ChannelManager 注册/启动
- EchoChannel 走统一管道
- 各 channel 的入站解析(不连真网络,用 ingest_xxx)
- signature 验签(slack, discord)
"""
from __future__ import annotations

import asyncio
import hmac
import hashlib
import time
from unittest.mock import MagicMock


from openclaw.channels import (
    ChannelManager,
    EchoChannel,
    IncomingMessage,
    TelegramChannel,
    DiscordChannel,
    SlackChannel,
    WhatsAppChannel,
    SignalChannel,
    IMessageChannel,
)
from openclaw.core.auto_reply import AutoReplyConfig, AutoReplyManager


# ---------------- ChannelManager + EchoChannel ----------------

class _StubAgent:
    """替身 AgentLoop:回显 prefix + text。"""
    async def handle(self, session_id, text, **kw):
        return MagicMock(content=f"[{session_id}] {text}", tool_calls=[], iterations=1)


def test_echo_channel_dispatch_with_no_auto_reply():
    agent = _StubAgent()
    ch = EchoChannel(agent)
    asyncio.run(ch.dispatch(IncomingMessage(
        channel="echo", session_id="e:1", user_id="u1", text="hi",
        metadata={"is_dm": True},
    )))
    assert len(ch.received) == 1
    assert len(ch.replies) == 1
    assert ch.replies[0][0] == "e:1"
    assert "[e:1] hi" in ch.replies[0][1]


def test_echo_channel_template_short_circuits():
    agent = _StubAgent()
    arm = AutoReplyManager(AutoReplyConfig(templates={"ping": "pong"}))
    ch = EchoChannel(agent, auto_reply=arm)
    asyncio.run(ch.dispatch(IncomingMessage(
        channel="echo", session_id="e:1", user_id="u1", text="ping please",
        metadata={"is_dm": True},
    )))
    assert ch.replies == [("e:1", "pong")]
    # 没进 agent
    assert agent.handle is not None


def test_echo_channel_blacklist_drops():
    agent = _StubAgent()
    arm = AutoReplyManager(AutoReplyConfig(blacklist=[r"rm\s+-rf"]))
    ch = EchoChannel(agent, auto_reply=arm)
    asyncio.run(ch.dispatch(IncomingMessage(
        channel="echo", session_id="e:1", user_id="u1", text="rm -rf /",
        metadata={"is_dm": True},
    )))
    # blacklist 拦截:received 仍记录(便于审计),但无回复
    assert len(ch.received) == 1
    assert ch.replies == []


def test_channel_manager_injects_dependencies():
    agent = _StubAgent()
    arm = AutoReplyManager(AutoReplyConfig())
    mgr = ChannelManager(agent, auto_reply=arm)
    ch = EchoChannel()  # 没传 agent
    mgr.register(ch)
    assert ch.agent_loop is agent
    assert ch.auto_reply is arm


def test_channel_manager_multi_channel():
    agent = _StubAgent()
    mgr = ChannelManager(agent)
    for _ in range(3):
        mgr.register(EchoChannel())
    assert len(mgr.channels()) == 3


# ---------------- Telegram ----------------

def test_telegram_handle_update():
    agent = _StubAgent()
    ch = TelegramChannel(token="x:1", agent_loop=agent)
    upd = {
        "update_id": 42,
        "message": {
            "message_id": 1,
            "chat": {"id": 999, "type": "private"},
            "from": {"id": 7, "username": "alice", "first_name": "Alice"},
            "text": "hello bot",
        },
    }
    asyncio.run(ch._handle_update(upd))
    assert len(ch.received) == 1
    msg = ch.received[0]
    assert msg.channel == "telegram"
    assert msg.session_id == "telegram:999"
    assert msg.user_id == "7"
    assert msg.text == "hello bot"
    assert msg.metadata["is_dm"] is True
    # agent 拿到了 prompt 前缀(空,因为没 auto_reply)
    # echo reply 应该回 "[telegram:999] hello bot"
    assert any(r[0] == "telegram:999" and "hello bot" in r[1] for r in ch.replies)


def test_telegram_handle_update_skips_bot_message():
    agent = _StubAgent()
    ch = TelegramChannel(token="x", agent_loop=agent)
    upd = {
        "update_id": 1,
        "message": {
            "chat": {"id": 1, "type": "private"},
            "from": {"id": 2, "is_bot": True},
            "text": "i'm bot",
        },
    }
    asyncio.run(ch._handle_update(upd))
    assert ch.received == []  # 不收 bot 自己消息


def test_telegram_handle_update_no_text():
    agent = _StubAgent()
    ch = TelegramChannel(token="x", agent_loop=agent)
    upd = {
        "update_id": 1,
        "message": {
            "chat": {"id": 1, "type": "private"},
            "from": {"id": 2},
            "photo": [],
        },
    }
    asyncio.run(ch._handle_update(upd))
    assert ch.received == []


# ---------------- Discord ----------------

def test_discord_ingest_slash():
    agent = _StubAgent()
    ch = DiscordChannel(token="x", agent_loop=agent)
    asyncio.run(ch.ingest_webhook({
        "type": 2,
        "channel_id": "888",
        "guild_id": "g1",
        "member": {"user": {"id": "123", "username": "bob"}},
        "data": {"name": "ask", "options": [{"name": "q", "value": "weather"}]},
    }))
    assert len(ch.received) == 1
    assert ch.received[0].channel == "discord"
    assert ch.received[0].session_id == "discord:888"
    assert ch.received[0].text == "ask"  # 我们只读 data.name
    assert ch.received[0].metadata["is_dm"] is False
    assert ch.received[0].metadata["mentioned"] is True


def test_discord_ping_is_dropped():
    agent = _StubAgent()
    ch = DiscordChannel(token="x", agent_loop=agent)
    asyncio.run(ch.ingest_webhook({"type": 1}))
    assert ch.received == []


def test_discord_verify_signature_no_key():
    agent = _StubAgent()
    ch = DiscordChannel(token="x", agent_loop=agent)
    # 没设 public_key → 默认放行
    assert ch.verify_signature(b"body", "sig", "ts") is True


# ---------------- Slack ----------------

def test_slack_ingest_message():
    agent = _StubAgent()
    ch = SlackChannel(token="x", agent_loop=agent)
    asyncio.run(ch.ingest_event({
        "type": "event_callback",
        "event": {
            "type": "message",
            "channel": "C123",
            "user": "U999",
            "text": "hello world",
        },
    }))
    assert len(ch.received) == 1
    m = ch.received[0]
    assert m.channel == "slack"
    assert m.session_id == "slack:C123"
    assert m.text == "hello world"
    assert m.metadata["is_dm"] is False  # C123 是 channel


def test_slack_ingest_message_dm():
    agent = _StubAgent()
    ch = SlackChannel(token="x", agent_loop=agent)
    asyncio.run(ch.ingest_event({
        "type": "event_callback",
        "event": {
            "type": "message",
            "channel": "D123",
            "user": "U1",
            "text": "hi",
        },
    }))
    assert ch.received[0].metadata["is_dm"] is True


def test_slack_ingest_strips_mention():
    agent = _StubAgent()
    ch = SlackChannel(token="x", agent_loop=agent)
    asyncio.run(ch.ingest_event({
        "type": "event_callback",
        "event": {
            "type": "app_mention",
            "channel": "C1",
            "user": "U1",
            "text": "<@UBOTID> 帮我算 7*8",
        },
    }))
    assert ch.received[0].text == "帮我算 7*8"
    assert ch.received[0].metadata["mentioned"] is True


def test_slack_verify_signature():
    secret = "shhh"
    body = b'{"type":"event_callback"}'
    ts = str(int(time.time()))
    sig_base = f"v0:{ts}".encode() + body
    expected = "v0=" + hmac.new(secret.encode(), sig_base, hashlib.sha256).hexdigest()
    ch = SlackChannel(token="x", agent_loop=_StubAgent(), signing_secret=secret)
    assert ch.verify_signature(body, ts, expected) is True
    assert ch.verify_signature(body, ts, "v0=deadbeef") is False
    # 旧时间戳
    assert ch.verify_signature(body, "1000000000", expected) is False


def test_slack_url_verification_passes_through():
    agent = _StubAgent()
    ch = SlackChannel(token="x", agent_loop=agent)
    asyncio.run(ch.ingest_event({"type": "url_verification", "challenge": "abc"}))
    # url_verification 不产生入站消息
    assert ch.received == []


# ---------------- WhatsApp ----------------

def test_whatsapp_ingest():
    agent = _StubAgent()
    ch = WhatsAppChannel(token="t", phone_id="p", agent_loop=agent)
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "1",
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"display_phone_number": "p", "phone_number_id": "p"},
                    "contacts": [{"wa_id": "8613800138000"}],
                    "messages": [{
                        "from": "8613800138000",
                        "id": "wamid.1",
                        "timestamp": "1234567",
                        "type": "text",
                        "text": {"body": "hi there"},
                    }],
                },
                "field": "messages",
            }],
        }],
    }
    asyncio.run(ch.ingest_webhook(payload))
    assert len(ch.received) == 1
    m = ch.received[0]
    assert m.channel == "whatsapp"
    assert m.session_id == "whatsapp:8613800138000"
    assert m.text == "hi there"
    assert m.metadata["is_dm"] is True


def test_whatsapp_ingest_skips_non_text():
    agent = _StubAgent()
    ch = WhatsAppChannel(token="t", phone_id="p", agent_loop=agent)
    payload = {
        "entry": [{
            "changes": [{
                "field": "messages",
                "value": {"messages": [{"type": "image", "from": "1", "id": "x"}]},
            }],
        }],
    }
    asyncio.run(ch.ingest_webhook(payload))
    assert ch.received == []


def test_whatsapp_verify():
    ch = WhatsAppChannel(token="t", phone_id="p", agent_loop=_StubAgent(), verify_token="v")
    assert ch.verify_webhook("subscribe", "v", "chall123") == "chall123"
    assert ch.verify_webhook("subscribe", "wrong", "chall123") is None


# ---------------- Signal ----------------

def test_signal_handle_envelope():
    agent = _StubAgent()
    ch = SignalChannel(base_url="http://x", account="+8613800138000", agent_loop=agent)
    env = {
        "source": "+8613800138000",
        "timestamp": 1700000000000,
        "dataMessage": {"message": "hello signal"},
    }
    asyncio.run(ch._handle_envelope(env))
    assert len(ch.received) == 1
    m = ch.received[0]
    assert m.channel == "signal"
    assert m.session_id == "signal:+8613800138000"
    assert m.text == "hello signal"


def test_signal_handle_envelope_no_text():
    agent = _StubAgent()
    ch = SignalChannel(base_url="http://x", account="acc", agent_loop=agent)
    asyncio.run(ch._handle_envelope({"envelope": {"source": "x"}, "dataMessage": {}}))
    assert ch.received == []


# ---------------- iMessage ----------------

def test_imessage_ingest():
    agent = _StubAgent()
    ch = IMessageChannel(agent_loop=agent, bluebubbles_url="http://bb")
    asyncio.run(ch.ingest_webhook({
        "type": "new-message",
        "data": {
            "text": "hi imessage",
            "handle": {"address": "+8613800138000"},
            "chats": [{"chatGuid": "iMessage;-;+8613800138000"}],
        },
    }))
    assert len(ch.received) == 1
    m = ch.received[0]
    assert m.channel == "imessage"
    assert m.session_id == "imessage:iMessage;-;+8613800138000"
    assert m.text == "hi imessage"


def test_imessage_ingest_non_message():
    agent = _StubAgent()
    ch = IMessageChannel(agent_loop=agent, bluebubbles_url="http://bb")
    asyncio.run(ch.ingest_webhook({"type": "typing-indicator"}))
    assert ch.received == []


# ---------------- BaseChannel.send 切块 ----------------

class _FakeTelegram(TelegramChannel):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls: list[dict] = []

    async def call(self, method, **params):
        self.calls.append({"method": method, "params": params})
        return {"ok": True, "result": {"message_id": 1}}


def test_telegram_send_chunks_long_text():
    agent = _StubAgent()
    ch = _FakeTelegram(token="x:1", agent_loop=agent)
    asyncio.run(ch.send("telegram:42", "x" * 8500))
    # 8500 字符应切 3 段(0-4000, 4000-8000, 8000-8500)
    assert len(ch.calls) == 3
    assert ch.calls[0]["params"]["text"] == "x" * 4000
    assert ch.calls[1]["params"]["text"] == "x" * 4000
    assert ch.calls[2]["params"]["text"] == "x" * 500
