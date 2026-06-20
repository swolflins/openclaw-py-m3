"""Phase 13 code review 修复单测(中危/低危批)。"""
from __future__ import annotations

import asyncio
import tempfile

import pytest


# ─────── RT-4 trim_history ───────

def test_trim_history_under_window_unchanged():
    from openclaw.agent.loop import trim_history
    from openclaw.llm.base import ChatMessage
    msgs = [ChatMessage(role="user", content="hi")]
    assert trim_history(msgs, soft_window=10, max_chars=1000) == msgs


def test_trim_history_collapses_middle():
    from openclaw.agent.loop import trim_history
    from openclaw.llm.base import ChatMessage
    msgs = [ChatMessage(role="system", content="sys")] + [
        ChatMessage(role="user", content=f"m{i}") for i in range(50)
    ]
    trimmed = trim_history(msgs, soft_window=10, max_chars=10_000_000)
    assert trimmed[0].role == "system"
    assert len(trimmed) < len(msgs)
    assert any("trimmed" in (m.content or "").lower() for m in trimmed)


def test_trim_history_respects_max_chars():
    from openclaw.agent.loop import trim_history
    from openclaw.llm.base import ChatMessage
    msgs = [ChatMessage(role="system", content="sys")] + [
        ChatMessage(role="user", content="x" * 1000) for _ in range(20)
    ]
    trimmed = trim_history(msgs, soft_window=100, max_chars=5000)
    total = sum(len(m.content or "") for m in trimmed)
    # system 可能保留 + 1 条 user 整条(超长也保留)
    assert total <= 5000 + 4000


# ─────── CH-1 LarkChannel 解析 mentions ───────

def test_lark_is_bot_mentioned_with_bot_type():
    from openclaw.channels.lark import _is_bot_mentioned

    class M:
        mentioned_type = "bot"
        id = type("I", (), {"open_id": "ou_x"})()
    assert _is_bot_mentioned([M()], None) is True
    assert _is_bot_mentioned([M()], "ou_x") is True


def test_lark_is_bot_mentioned_with_open_id_match():
    from openclaw.channels.lark import _is_bot_mentioned

    class M:
        mentioned_type = "user"
        id = type("I", (), {"open_id": "ou_bot"})()
    assert _is_bot_mentioned([M()], "ou_bot") is True
    assert _is_bot_mentioned([M()], "ou_other") is False


def test_lark_is_bot_mentioned_empty():
    from openclaw.channels.lark import _is_bot_mentioned
    assert _is_bot_mentioned(None, "ou_x") is False
    assert _is_bot_mentioned([], "ou_x") is False


# ─────── CH-2 AutoReplyConfig allow_from ───────

def test_auto_reply_allow_from_blocks_other_users():
    from openclaw.core.auto_reply import AutoReplyManager, AutoReplyConfig
    arm = AutoReplyManager(AutoReplyConfig(allow_from=["alice"]))
    d = asyncio.run(arm.decide("bob", "telegram", "hi", metadata={"is_dm": True}))
    assert d.passthrough is False
    assert "allow_from" in d.reason


def test_auto_reply_allow_from_passes_listed_user():
    from openclaw.core.auto_reply import AutoReplyManager, AutoReplyConfig
    arm = AutoReplyManager(AutoReplyConfig(allow_from=["alice"]))
    d = asyncio.run(arm.decide("alice", "telegram", "hi", metadata={"is_dm": True}))
    assert d.passthrough is True


def test_auto_reply_empty_allow_from_passes_everyone():
    from openclaw.core.auto_reply import AutoReplyManager, AutoReplyConfig
    arm = AutoReplyManager(AutoReplyConfig())
    d = asyncio.run(arm.decide("anyone", "telegram", "hi", metadata={"is_dm": True}))
    assert d.passthrough is True


# ─────── SEC-12 RateLimiter max_keys + LRU ───────

def test_rate_limiter_respects_max_keys():
    from openclaw.core.rate_limit import RateLimiter
    rl = RateLimiter(rate=10, burst=5, max_keys=10)
    for i in range(20):
        rl.allow(f"k{i}")
    snap = rl.snapshot()
    # max_keys=10 限,允许 ≤ max_keys + LRU 余量
    assert len(snap) <= 11


def test_rate_limiter_default_max_keys_is_100k():
    from openclaw.core.rate_limit import RateLimiter
    rl = RateLimiter()
    assert rl.max_keys == 100_000


# ─────── MEM-4/5 long_term 空 text 拒 + max_items 参数 ───────

def _has_chroma() -> bool:
    try:
        import chromadb  # noqa: F401
        return True
    except Exception:
        return False


def test_long_term_rejects_empty_text():
    if not _has_chroma():
        pytest.skip("chromadb 未装")
    from openclaw.memory.long_term import LongTermStore
    with tempfile.TemporaryDirectory() as tmp:
        st = LongTermStore(tmp)
        assert st.add("", scope="t") == ""
        assert st.add("   ", scope="t") == ""


def test_long_term_max_items_param():
    if not _has_chroma():
        pytest.skip("chromadb 未装")
    from openclaw.memory.long_term import LongTermStore
    with tempfile.TemporaryDirectory() as tmp:
        st = LongTermStore(tmp, max_items=100)
        assert st.max_items == 100


# ─────── imports smoke(确保不崩) ───────

def test_phase13_imports():
    from openclaw.agent.loop import trim_history
    from openclaw.core.rate_limit import RateLimiter
    from openclaw.memory.short_term import ShortTermStore
    from openclaw.channels.lark import _is_bot_mentioned
    assert callable(trim_history)
    assert callable(RateLimiter)
    assert callable(ShortTermStore)
    assert callable(_is_bot_mentioned)
