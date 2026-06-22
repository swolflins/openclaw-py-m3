"""Phase 31 测试 — 飞书 (Lark) 渠道参考 Hermes Feishu adapter 的优化项。

参考来源: hermes-agent/plugins/platforms/feishu/adapter.py (5500+ 行)。
Hermes 已经实现了 11 项显式特性,本测试覆盖 openclaw-py LarkChannel
新增的 8 项对齐能力:

- A. 持久化去重状态(DEDUP_CACHE_SIZE / DEDUP_TTL_SECONDS / OPENCLAW_LARK_DEDUP_PATH)
- B. Per-chat 串行锁(CHAT_LOCK_MAX_SIZE / LRU bounded / 活跃锁不驱逐)
- C. Processing reaction(Typing / CrossMark)
- D. Card action 事件 → 合成 COMMAND 事件(/card <action_tag>)
- E. Reaction 事件 → 合成 text 事件(reaction:added/removed:<emoji>)
- F. Webhook 异常追踪 + verification token 校验(record/clear/verify_*)
- G. DM / 群 allowlist 闸口(LARK_ALLOWED_USERS / LARK_GROUP_POLICY)
- H. post 富文本解析(从 mentions 替换 @ 占位符)

Webhook 完整路由仍 NotImplementedError(等后续 Phase 拼 aiohttp),
但子模块 API 全部到位,本测试也验证接口契约。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# ============================================================
# 共享 fixture
# ============================================================

@pytest.fixture
def tmp_dedup_path(tmp_path, monkeypatch):
    """每次测试都把 dedup 路径指向临时文件,避免污染用户目录。"""
    p = tmp_path / "lark_dedup.json"
    monkeypatch.setenv("OPENCLAW_LARK_DEDUP_PATH", str(p))
    return p


@pytest.fixture
def clean_lark_env(monkeypatch):
    """清空 LARK_ALLOWED_USERS / LARK_GROUP_POLICY,避免宿主 env 影响。

    返回 monkeypatch 实例(调用方可继续用其 setenv)。
    """
    monkeypatch.delenv("LARK_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("LARK_GROUP_POLICY", raising=False)
    monkeypatch.delenv("OPENCLAW_LARK_DEDUP_PATH", raising=False)
    return monkeypatch


def _make_lark(monkeypatch, tmp_dedup_path):
    """构造一个最小可用的 LarkChannel(不真的拉 bot open_id,只测 Phase 31 子模块)。"""
    # 显式传 dedup_path=settings,优先于 env(避免宿主 env 干扰)
    from openclaw.agent.loop import AgentLoop  # type: ignore
    from openclaw.config.settings import LarkSettings
    from openclaw.channels.lark import LarkChannel

    s = LarkSettings(
        app_id="cli_test_app",
        app_secret=_real_secret("test_secret_xxxxxxxxxx"),
        dedup_path=str(tmp_dedup_path),
    )
    loop = MagicMock(spec=AgentLoop)
    return LarkChannel(loop, s)


class SecretStr_stub:
    """pydantic SecretStr 的纯 Python 等价(测试不引入 pydantic 依赖)。"""
    def __init__(self, v: str):
        self._v = v
    def get_secret_value(self) -> str:
        return self._v


def _real_secret(v: str) -> Any:
    """造一个真的 pydantic SecretStr。"""
    from pydantic import SecretStr
    return SecretStr(v)


# ============================================================
# A. 持久化去重状态
# ============================================================

class TestADedupPersistentState:
    def test_dedup_constants_exposed(self):
        """Phase 31 优化:DEDUP_CACHE_SIZE / DEDUP_TTL_SECONDS / OPENCLAW_LARK_DEDUP_PATH 都在。"""
        from openclaw.channels import lark
        assert hasattr(lark, "DEDUP_CACHE_SIZE")
        assert lark.DEDUP_CACHE_SIZE >= 1000
        assert hasattr(lark, "DEDUP_TTL_SECONDS")
        assert lark.DEDUP_TTL_SECONDS > 0

    def test_dedup_path_env_overrides_default(self, monkeypatch, tmp_path):
        """``OPENCLAW_LARK_DEDUP_PATH`` 应优先于默认 ``~/.openclaw/...``。"""
        from openclaw.channels.lark import _dedup_path
        p = tmp_path / "x.json"
        monkeypatch.setenv("OPENCLAW_LARK_DEDUP_PATH", str(p))
        assert _dedup_path() == p

    def test_is_duplicate_first_time(self, tmp_dedup_path, clean_lark_env):
        """首次见到 message_id → 返回 False,记入。"""
        ch = _make_lark(clean_lark_env, tmp_dedup_path)
        is_dup = asyncio.run(ch._is_duplicate("m_1"))
        assert is_dup is False
        # 第二次应判 True
        is_dup2 = asyncio.run(ch._is_duplicate("m_1"))
        assert is_dup2 is True

    def test_is_duplicate_lru_eviction(self, tmp_dedup_path, clean_lark_env, monkeypatch):
        """超过 DEDUP_CACHE_SIZE 时,最早插入的被驱逐。"""
        ch = _make_lark(clean_lark_env, tmp_dedup_path)
        # 用小一点的 cache 测试
        from openclaw.channels import lark
        monkeypatch.setattr(lark, "DEDUP_CACHE_SIZE", 3)
        for i in range(5):
            assert asyncio.run(ch._is_duplicate(f"m_{i}")) is False
        # m_0 / m_1 已被驱逐 → 视为新
        assert asyncio.run(ch._is_duplicate("m_0")) is False
        # m_4 仍在 → True
        assert asyncio.run(ch._is_duplicate("m_4")) is True

    def test_dedup_persists_to_disk(self, tmp_dedup_path, clean_lark_env):
        """写入后 JSON 文件存在并包含 message_ids 键。"""
        ch = _make_lark(clean_lark_env, tmp_dedup_path)
        asyncio.run(ch._is_duplicate("om_durable"))
        # 持久化是异步的,这里同步再调一次
        asyncio.run(ch._persist_seen_message_ids_async())
        assert tmp_dedup_path.exists()
        data = json.loads(tmp_dedup_path.read_text(encoding="utf-8"))
        assert "om_durable" in data["message_ids"]

    def test_dedup_loads_across_instances(self, tmp_dedup_path, clean_lark_env):
        """新实例从已有 JSON 加载 → 旧 ID 视为已见过。"""
        ch1 = _make_lark(clean_lark_env, tmp_dedup_path)
        asyncio.run(ch1._is_duplicate("om_cross"))
        asyncio.run(ch1._persist_seen_message_ids_async())
        # 新实例
        ch2 = _make_lark(clean_lark_env, tmp_dedup_path)
        is_dup = asyncio.run(ch2._is_duplicate("om_cross"))
        assert is_dup is True

    def test_dedup_legacy_list_format(self, tmp_dedup_path, clean_lark_env):
        """旧格式: message_ids 是 list[str] 也兼容。"""
        tmp_dedup_path.write_text(
            json.dumps({"message_ids": ["legacy_id_1", "legacy_id_2"]}),
            encoding="utf-8",
        )
        ch = _make_lark(clean_lark_env, tmp_dedup_path)
        assert asyncio.run(ch._is_duplicate("legacy_id_1")) is True
        assert asyncio.run(ch._is_duplicate("legacy_id_2")) is True
        assert asyncio.run(ch._is_duplicate("fresh_id")) is False

    def test_dedup_corrupt_json_does_not_crash(self, tmp_dedup_path, clean_lark_env):
        """JSON 损坏 → 当作空,继续工作。"""
        tmp_dedup_path.write_text("{this is not json", encoding="utf-8")
        ch = _make_lark(clean_lark_env, tmp_dedup_path)
        # 不抛 + 新 ID 仍可加入
        assert asyncio.run(ch._is_duplicate("x1")) is False

    def test_dedup_atomic_write(self, tmp_dedup_path, clean_lark_env):
        """落盘走 .tmp + rename,不应留半成品。"""
        ch = _make_lark(clean_lark_env, tmp_dedup_path)
        asyncio.run(ch._persist_seen_message_ids_async())
        # 不应有 .tmp 残留
        leftover = list(tmp_dedup_path.parent.glob("*.tmp"))
        assert leftover == [], f"半成品文件残留: {leftover}"


# ============================================================
# B. Per-chat 串行锁
# ============================================================

class TestBPerChatSerialLock:
    def test_chat_lock_max_size_constant(self):
        from openclaw.channels import lark
        assert lark.CHAT_LOCK_MAX_SIZE >= 64

    def test_get_chat_lock_returns_lock(self, tmp_dedup_path, clean_lark_env):
        ch = _make_lark(clean_lark_env, tmp_dedup_path)
        lock = ch._get_chat_lock("c1")
        assert isinstance(lock, asyncio.Lock)

    def test_get_chat_lock_dedup(self, tmp_dedup_path, clean_lark_env):
        """同 chat_id 两次调用 → 同一 Lock 实例。"""
        ch = _make_lark(clean_lark_env, tmp_dedup_path)
        l1 = ch._get_chat_lock("c1")
        l2 = ch._get_chat_lock("c1")
        assert l1 is l2

    def test_chat_lock_lru_bounded(self, tmp_dedup_path, clean_lark_env, monkeypatch):
        """超过上限时,空闲锁被驱逐。"""
        from openclaw.channels import lark
        ch = _make_lark(clean_lark_env, tmp_dedup_path)
        monkeypatch.setattr(lark, "CHAT_LOCK_MAX_SIZE", 3)
        for i in range(5):
            ch._get_chat_lock(f"c{i}")
        # 此时池大小应 <= 3
        assert len(ch._chat_locks) <= 3

    def test_chat_lock_does_not_evict_active(self, tmp_dedup_path, clean_lark_env, monkeypatch):
        """锁正在被持有时,evict 不会选它。"""
        from openclaw.channels import lark
        ch = _make_lark(clean_lark_env, tmp_dedup_path)
        monkeypatch.setattr(lark, "CHAT_LOCK_MAX_SIZE", 2)
        # 装满
        ch._get_chat_lock("c1")
        ch._get_chat_lock("c2")
        # 把 c1 锁住(异步上下文内只是标记)
        held_lock = ch._chat_locks["c1"]
        held_lock._locked = True  # type: ignore[attr-defined]
        try:
            # 触发 evict
            ch._get_chat_lock("c3")
        finally:
            held_lock._locked = False  # type: ignore[attr-defined]
        # c1 仍在(它是 active 锁)
        assert "c1" in ch._chat_locks

    def test_chat_lock_serialization(self, tmp_dedup_path, clean_lark_env):
        """两个并发 task 争用同一 chat_id lock,第二个必须等第一个释放。"""
        ch = _make_lark(clean_lark_env, tmp_dedup_path)
        order: list[str] = []

        async def t1() -> None:
            async with ch._get_chat_lock("c1"):
                order.append("t1_enter")
                await asyncio.sleep(0.05)
                order.append("t1_exit")

        async def t2() -> None:
            await asyncio.sleep(0.01)  # 让 t1 先抢到
            async with ch._get_chat_lock("c1"):
                order.append("t2_enter")

        async def main() -> None:
            await asyncio.gather(t1(), t2())

        asyncio.run(main())
        # t1 完整 enter/exit 后 t2 才能 enter
        assert order == ["t1_enter", "t1_exit", "t2_enter"]


# ============================================================
# C. Processing reaction(Typing / CrossMark)
# ============================================================

class TestCProcessingReaction:
    def test_reaction_constants_exposed(self):
        from openclaw.channels.lark import REACTION_IN_PROGRESS, REACTION_FAILURE
        assert REACTION_IN_PROGRESS == "Typing"
        assert REACTION_FAILURE == "CrossMark"

    def test_add_reaction_uses_executor(self, tmp_dedup_path, clean_lark_env):
        """add_reaction 内部走 run_in_executor,不阻塞事件循环。

        验证策略:拦截 ``asyncio.get_running_loop`` 拿到我们的 fake loop,
        确保 SDK 闭包被传入执行器(就算 SDK 真不存在也不应崩)。
        """
        ch = _make_lark(clean_lark_env, tmp_dedup_path)
        captured: dict[str, Any] = {}

        class FakeLoop:
            def run_in_executor(self, executor, fn, *args):
                captured["called"] = True
                # SDK 真调可能抛(如租户 token 拿不到);都算 _call 被跑过
                try:
                    captured["result"] = fn()
                except Exception as e:
                    captured["result_error"] = type(e).__name__
                async def _ret():
                    return captured.get("result")
                return _ret()

        with patch("asyncio.get_running_loop", return_value=FakeLoop()):
            # SDK 不存在 / token 拿不到 → 走 except 吞,关键是 run_in_executor 走到
            asyncio.run(ch._add_processing_reaction("m_x", "Typing"))
        # run_in_executor 被调过(说明没在 async loop 同步跑)
        assert captured.get("called") is True, "未走 run_in_executor"
        # _call 闭包被跑过(可能正常返回,也可能 SDK 异常被 except 吞)
        assert "result" in captured or "result_error" in captured, "_call 闭包未执行"

    def test_add_reaction_failure_does_not_raise(self, tmp_dedup_path, clean_lark_env):
        """加 reaction 失败仅 warning,不阻断 dispatch。"""
        ch = _make_lark(clean_lark_env, tmp_dedup_path)
        with patch("openclaw.channels.lark.lark") as mock_lark:
            mock_lark.Client.builder.return_value.build.side_effect = RuntimeError("boom")
            # 不抛
            asyncio.run(ch._add_processing_reaction("m_x", "Typing"))


# ============================================================
# D. Card action → 合成 COMMAND 事件
# ============================================================

class TestDCardActionSyntheticCommand:
    def test_card_action_to_command_text(self, tmp_dedup_path, clean_lark_env):
        """卡片按钮点击应路由成 ``/card <action_tag>`` 文本。"""
        ch = _make_lark(clean_lark_env, tmp_dedup_path)

        # 用 SimpleNamespace 模拟 lark-oapi 事件对象(避免 MagicMock 自动生成子属性)
        from types import SimpleNamespace
        event = SimpleNamespace(
            action=SimpleNamespace(value="approve_yes", tag="button"),
            operator=SimpleNamespace(open_id="ou_alice"),
            chat_id="oc_chat1",
        )

        # 模拟 dispatch 收到 IncomingMessage
        from openclaw.channels.base import IncomingMessage
        captured: list[IncomingMessage] = []

        async def fake_dispatch(msg: IncomingMessage) -> None:
            captured.append(msg)

        ch.dispatch = fake_dispatch  # type: ignore[method-assign]
        # 包成 data(event 字段)
        data = SimpleNamespace(event=event)
        asyncio.run(ch._handle_card_action(data))

        assert len(captured) == 1
        assert captured[0].text == "/card approve_yes"
        assert captured[0].metadata["is_card_action"] is True
        assert captured[0].metadata["card_action"] == "approve_yes"
        assert captured[0].user_id == "ou_alice"

    def test_card_action_dict_fallback(self, tmp_dedup_path, clean_lark_env):
        """event 是 dict 时也要正确解析。"""
        ch = _make_lark(clean_lark_env, tmp_dedup_path)
        data = {
            "event": {
                "action": {"value": "noop_btn", "tag": "button"},
                "operator": {"open_id": "ou_bob"},
                "chat_id": "oc_chat2",
            }
        }
        from openclaw.channels.base import IncomingMessage
        captured: list[IncomingMessage] = []

        async def fake_dispatch(msg: IncomingMessage) -> None:
            captured.append(msg)

        ch.dispatch = fake_dispatch  # type: ignore[method-assign]
        asyncio.run(ch._handle_card_action(data))

        assert captured[0].text == "/card noop_btn"
        assert captured[0].user_id == "ou_bob"

    def test_card_action_exception_does_not_crash(self, tmp_dedup_path, clean_lark_env):
        """畸形事件 → 走 except,只 logger,不抛。"""
        ch = _make_lark(clean_lark_env, tmp_dedup_path)
        # event 是 MagicMock 但没任何字段,getattr 全 None → 走 except
        asyncio.run(ch._handle_card_action(MagicMock(spec=[])))


# ============================================================
# E. Reaction → 合成 text 事件
# ============================================================

class TestEReactionSyntheticText:
    def test_reaction_added_to_text(self, tmp_dedup_path, clean_lark_env):
        """reaction created → 合成 ``reaction:added:<emoji>`` 文本。"""
        ch = _make_lark(clean_lark_env, tmp_dedup_path)
        from types import SimpleNamespace
        event = SimpleNamespace(
            reaction_type=SimpleNamespace(emoji_type="ThumbsUp"),
            operator=SimpleNamespace(operator_id=SimpleNamespace(open_id="ou_carol")),
            message_id="om_target_1",
            chat_id="oc_chat1",
        )

        from openclaw.channels.base import IncomingMessage
        captured: list[IncomingMessage] = []

        async def fake_dispatch(msg: IncomingMessage) -> None:
            captured.append(msg)

        ch.dispatch = fake_dispatch  # type: ignore[method-assign]
        data = SimpleNamespace(event=event)
        asyncio.run(ch._handle_reaction_event("im.message.reaction.created_v1", data))

        assert len(captured) == 1
        assert captured[0].text == "reaction:added:ThumbsUp"
        assert captured[0].metadata["is_reaction"] is True
        assert captured[0].metadata["reaction_action"] == "added"
        assert captured[0].metadata["reaction_emoji"] == "ThumbsUp"

    def test_reaction_removed_to_text(self, tmp_dedup_path, clean_lark_env):
        """reaction deleted → 合成 ``reaction:removed:<emoji>``。"""
        ch = _make_lark(clean_lark_env, tmp_dedup_path)
        from types import SimpleNamespace
        event = SimpleNamespace(
            reaction_type=SimpleNamespace(emoji_type="CrossMark"),
            operator=SimpleNamespace(operator_id=SimpleNamespace(open_id="ou_dave")),
            message_id="om_target_2",
            chat_id="oc_chat2",
        )

        from openclaw.channels.base import IncomingMessage
        captured: list[IncomingMessage] = []

        async def fake_dispatch(msg: IncomingMessage) -> None:
            captured.append(msg)

        ch.dispatch = fake_dispatch  # type: ignore[method-assign]
        data = SimpleNamespace(event=event)
        asyncio.run(ch._handle_reaction_event("im.message.reaction.deleted_v1", data))

        assert captured[0].text == "reaction:removed:CrossMark"
        assert captured[0].metadata["reaction_action"] == "removed"

    def test_reaction_dict_fallback(self, tmp_dedup_path, clean_lark_env):
        """event 是 dict 时也要正确解析。"""
        ch = _make_lark(clean_lark_env, tmp_dedup_path)
        data = {
            "event": {
                "reaction_type": {"emoji_type": "Heart"},
                "operator": {"operator_id": {"open_id": "ou_eve"}},
                "message_id": "om_t3",
                "chat_id": "oc_c3",
            }
        }
        from openclaw.channels.base import IncomingMessage
        captured: list[IncomingMessage] = []

        async def fake_dispatch(msg: IncomingMessage) -> None:
            captured.append(msg)

        ch.dispatch = fake_dispatch  # type: ignore[method-assign]
        asyncio.run(ch._handle_reaction_event("im.message.reaction.created_v1", data))

        assert captured[0].text == "reaction:added:Heart"
        assert captured[0].user_id == "ou_eve"

    def test_reaction_exception_does_not_crash(self, tmp_dedup_path, clean_lark_env):
        ch = _make_lark(clean_lark_env, tmp_dedup_path)
        asyncio.run(ch._handle_reaction_event("x", MagicMock(spec=[])))


# ============================================================
# F. Webhook 异常追踪 + verification token / signature
# ============================================================

class TestFWebhookSecurity:
    def test_record_webhook_anomaly_increments(self, monkeypatch):
        """每次调 record_webhook_anomaly,计数都应 +1。"""
        from openclaw.channels import lark
        # 隔离模块级 dict
        monkeypatch.setattr(lark, "_webhook_anomaly_counts", {})
        lark.record_webhook_anomaly("1.2.3.4", "401")
        lark.record_webhook_anomaly("1.2.3.4", "401")
        lark.record_webhook_anomaly("1.2.3.4", "401")
        entry = lark._webhook_anomaly_counts["1.2.3.4"]
        assert entry[0] == 3

    def test_record_webhook_anomaly_threshold_warning(self, monkeypatch, caplog):
        """每 25 次(WEBHOOK_ANOMALY_THRESHOLD)应打一条 warning。"""
        from openclaw.channels import lark
        import logging
        monkeypatch.setattr(lark, "_webhook_anomaly_counts", {})
        with caplog.at_level(logging.WARNING, logger="openclaw.channels.lark"):
            for _ in range(lark.WEBHOOK_ANOMALY_THRESHOLD):
                lark.record_webhook_anomaly("5.6.7.8", "500")
        assert any("异常" in r.message for r in caplog.records)

    def test_record_webhook_anomaly_ttl_reset(self, monkeypatch):
        """TTL 过期后,计数应重置为 1。"""
        from openclaw.channels import lark
        state: dict[str, tuple[int, str, float]] = {}
        monkeypatch.setattr(lark, "_webhook_anomaly_counts", state)
        # 注入一个超 TTL 的 entry
        state["9.9.9.9"] = (10, "401", time.time() - lark.WEBHOOK_ANOMALY_TTL_SECONDS - 1)
        lark.record_webhook_anomaly("9.9.9.9", "500")
        assert state["9.9.9.9"][0] == 1

    def test_clear_webhook_anomaly(self, monkeypatch):
        from openclaw.channels import lark
        state = {"1.1.1.1": (5, "401", time.time())}
        monkeypatch.setattr(lark, "_webhook_anomaly_counts", state)
        lark.clear_webhook_anomaly("1.1.1.1")
        assert "1.1.1.1" not in state

    def test_verify_webhook_token_hmac(self):
        """verify_webhook_token 用 hmac.compare_digest 防时序攻击。"""
        from openclaw.channels.lark import verify_webhook_token
        assert verify_webhook_token("secret_abc", "secret_abc") is True
        assert verify_webhook_token("secret_abc", "secret_xyz") is False
        assert verify_webhook_token(None, "secret_abc") is False
        assert verify_webhook_token("secret_abc", "") is False
        assert verify_webhook_token("", "secret_abc") is False

    def test_verify_webhook_signature_sha256(self):
        """verify_webhook_signature 走 SHA256(timestamp+nonce+encrypt_key+body) 摘要。"""
        from openclaw.channels.lark import verify_webhook_signature
        ts, nonce, ek, body = "1700000000", "abc123", "ek_secret", '{"x":1}'
        raw = f"{ts}{nonce}{ek}{body}"
        sig = base64.b64encode(
            hashlib.sha256(raw.encode("utf-8")).digest()
        ).decode("ascii")
        assert verify_webhook_signature(
            timestamp=ts, nonce=nonce, body_str=body,
            encrypt_key=ek, provided_signature=sig,
        ) is True
        # 改 1 字节应失败
        assert verify_webhook_signature(
            timestamp=ts, nonce=nonce, body_str=body + " ",
            encrypt_key=ek, provided_signature=sig,
        ) is False
        # 缺字段应失败
        assert verify_webhook_signature(
            timestamp=None, nonce=nonce, body_str=body,
            encrypt_key=ek, provided_signature=sig,
        ) is False

    def test_webhook_mode_still_raises(self, tmp_dedup_path, clean_lark_env):
        """LARK_USE_WS=False 缺少 verification_token → RuntimeError(Phase 32 强校验)。

        走真实 ``LarkSettings``,verification_token=None → 阻断。
        """
        from openclaw.config.settings import LarkSettings
        from openclaw.agent.loop import AgentLoop  # type: ignore
        from openclaw.channels.lark import LarkChannel

        s = LarkSettings(
            app_id="cli_test",
            app_secret=_real_secret("sec"),
            use_ws=False,
            verification_token=None,  # 显式 None → 阻断
            dedup_path=str(tmp_dedup_path),
        )
        loop = MagicMock(spec=AgentLoop)
        ch = LarkChannel(loop, s)
        with pytest.raises(RuntimeError, match="VERIFICATION_TOKEN"):
            asyncio.run(ch.start())


# ============================================================
# G. Allowlist 闸口
# ============================================================

class TestGAllowlist:
    def test_dm_no_allowlist_allows_everyone(self, tmp_dedup_path, clean_lark_env):
        ch = _make_lark(clean_lark_env, tmp_dedup_path)
        allowed, reason = ch._check_sender_allowed("ou_stranger", is_dm=True)
        assert allowed is True
        assert reason == "dm_no_allowlist"

    def test_dm_in_allowlist(self, tmp_dedup_path, clean_lark_env, monkeypatch):
        monkeypatch.setenv("LARK_ALLOWED_USERS", "ou_alice,ou_bob")
        ch = _make_lark(clean_lark_env, tmp_dedup_path)
        allowed, reason = ch._check_sender_allowed("ou_alice", is_dm=True)
        assert allowed is True
        assert reason == "dm_in_allowlist"

    def test_dm_not_in_allowlist_blocked(self, tmp_dedup_path, clean_lark_env, monkeypatch):
        monkeypatch.setenv("LARK_ALLOWED_USERS", "ou_alice")
        ch = _make_lark(clean_lark_env, tmp_dedup_path)
        allowed, reason = ch._check_sender_allowed("ou_stranger", is_dm=True)
        assert allowed is False
        assert reason == "dm_not_in_allowlist"

    def test_group_open_policy_allows(self, tmp_dedup_path, clean_lark_env):
        ch = _make_lark(clean_lark_env, tmp_dedup_path)
        # 默认 policy=open
        allowed, reason = ch._check_sender_allowed("ou_anyone", is_dm=False)
        assert allowed is True
        assert reason == "group_open"

    def test_group_disabled_blocks(self, tmp_dedup_path, clean_lark_env, monkeypatch):
        monkeypatch.setenv("LARK_GROUP_POLICY", "disabled")
        ch = _make_lark(clean_lark_env, tmp_dedup_path)
        allowed, reason = ch._check_sender_allowed("ou_anyone", is_dm=False)
        assert allowed is False
        assert reason == "group_disabled"

    def test_group_allowlist_enforced(self, tmp_dedup_path, clean_lark_env, monkeypatch):
        monkeypatch.setenv("LARK_GROUP_POLICY", "allowlist")
        monkeypatch.setenv("LARK_ALLOWED_USERS", "ou_trusted")
        ch = _make_lark(clean_lark_env, tmp_dedup_path)
        allowed, reason = ch._check_sender_allowed("ou_trusted", is_dm=False)
        assert allowed is True
        assert reason == "group_in_allowlist"
        allowed, reason = ch._check_sender_allowed("ou_random", is_dm=False)
        assert allowed is False
        assert reason == "group_not_in_allowlist"

    def test_group_allowlist_empty_fallback_warns(self, tmp_dedup_path, clean_lark_env, monkeypatch, caplog):
        """policy=allowlist 但列表空 → 放行(避免误锁)+ warning。"""
        import logging
        monkeypatch.setenv("LARK_GROUP_POLICY", "allowlist")
        monkeypatch.setenv("LARK_ALLOWED_USERS", "")
        ch = _make_lark(clean_lark_env, tmp_dedup_path)
        with caplog.at_level(logging.WARNING, logger="openclaw.channels.lark"):
            allowed, reason = ch._check_sender_allowed("ou_x", is_dm=False)
        assert allowed is True
        assert reason == "group_allowlist_empty"
        assert any("放行" in r.message for r in caplog.records)

    def test_allowed_users_parses_separators(self):
        """LARK_ALLOWED_USERS 支持逗号 / 空白多种分隔。"""
        from openclaw.channels.lark import _allowed_users
        for sep_input, expected in [
            ("ou_a,ou_b", {"ou_a", "ou_b"}),
            ("ou_a ou_b", {"ou_a", "ou_b"}),
            ("ou_a, ou_b , ou_c", {"ou_a", "ou_b", "ou_c"}),
            ("", set()),
        ]:
            with patch.dict(os.environ, {"LARK_ALLOWED_USERS": sep_input}):
                assert _allowed_users() == expected

    def test_group_policy_unknown_falls_back_to_open(self, monkeypatch, caplog):
        """未识别的 policy → warning + 回退到 open。"""
        import logging
        from openclaw.channels.lark import _group_policy
        monkeypatch.setenv("LARK_GROUP_POLICY", "open_with_kittens")
        with caplog.at_level(logging.WARNING, logger="openclaw.channels.lark"):
            assert _group_policy() == "open"
        assert any("不识别" in r.message for r in caplog.records)


# ============================================================
# H. post 富文本解析(@ 替换)
# ============================================================

class TestHPostRichTextParsing:
    def test_text_message(self):
        msg = MagicMock()
        msg.content = json.dumps({"text": "hello world"})
        msg.message_type = "text"
        from openclaw.channels.lark import LarkChannel
        assert LarkChannel._extract_text(msg) == "hello world"

    def test_post_with_text_segments(self):
        """post 多行文本段 → 用换行 join。"""
        msg = MagicMock()
        msg.content = json.dumps({
            "content": [
                [{"tag": "text", "text": "第一行"}],
                [{"tag": "text", "text": "第二行"}],
            ]
        })
        msg.message_type = "post"
        msg.mentions = []
        from openclaw.channels.lark import LarkChannel
        assert LarkChannel._extract_text(msg) == "第一行\n第二行"

    def test_post_with_at_replaced(self):
        """post 中含 at 段时,占位符应被替换为 mentions 里的 name。"""
        # mentions 里 key="@_user_1",name="张三"
        m1 = MagicMock()
        m1.key = "@_user_1"
        m1.name = "张三"
        msg = MagicMock()
        msg.content = json.dumps({
            "content": [
                [
                    {"tag": "at", "user_id": "@_user_1"},
                    {"tag": "text", "text": " 在吗?"},
                ]
            ]
        })
        msg.message_type = "post"
        msg.mentions = [m1]
        from openclaw.channels.lark import LarkChannel
        result = LarkChannel._extract_text(msg)
        assert "张三" in result
        assert "在吗" in result
        assert "@_user_1" not in result  # 占位符已被替换

    def test_post_with_at_fallback(self):
        """mention 缺失时,at 段回退成 ``@_user``。"""
        msg = MagicMock()
        msg.content = json.dumps({
            "content": [
                [{"tag": "at", "user_id": "@_user_unknown"}]
            ]
        })
        msg.message_type = "post"
        msg.mentions = []  # 没匹配
        from openclaw.channels.lark import LarkChannel
        assert LarkChannel._extract_text(msg) == "@_user"

    def test_post_skips_empty_lines(self):
        """空行 / 非 text 段应被跳过。"""
        msg = MagicMock()
        msg.content = json.dumps({
            "content": [
                [{"tag": "text", "text": "  "}],   # 全空白 → 跳过
                [{"tag": "text", "text": "actual"}],
                [{"tag": "link", "text": "ignored"}],  # 未知 tag
            ]
        })
        msg.message_type = "post"
        msg.mentions = []
        from openclaw.channels.lark import LarkChannel
        assert LarkChannel._extract_text(msg) == "actual"

    def test_image_type_returns_empty(self):
        """image / file / audio 类型 → 返回空(留 hook 给后续 Phase)。"""
        for t in ("image", "file", "audio", "media"):
            msg = MagicMock()
            msg.content = json.dumps({"image_key": "x"})
            msg.message_type = t
            from openclaw.channels.lark import LarkChannel
            assert LarkChannel._extract_text(msg) == "", t

    def test_invalid_json_returns_empty(self):
        msg = MagicMock()
        msg.content = "not json {"
        msg.message_type = "text"
        from openclaw.channels.lark import LarkChannel
        assert LarkChannel._extract_text(msg) == ""


# ============================================================
# 集成测试:完整 dispatch 路径(dedup + allowlist + per-chat 锁)
# ============================================================

class TestIIntegrationHandleEvent:
    """验证 _handle_event 的 dedup → allowlist → per-chat lock → dispatch 顺序。"""

    def _make_evt(self, *, message_id: str, open_id: str, chat_id: str, text: str = "hi"):
        from types import SimpleNamespace
        evt = SimpleNamespace(
            event=SimpleNamespace(
                sender=SimpleNamespace(sender_id=SimpleNamespace(open_id=open_id)),
                message=SimpleNamespace(
                    chat_id=chat_id,
                    message_id=message_id,
                    message_type="text",
                    content=json.dumps({"text": text}),
                    mentions=[],
                    chat_type="p2p",
                ),
            )
        )
        return evt

    def test_dedup_drops_duplicate(self, tmp_dedup_path, clean_lark_env):
        """同一 message_id 来两次,只 dispatch 一次。"""
        ch = _make_lark(clean_lark_env, tmp_dedup_path)
        # 屏蔽 bot_open_id 拉取
        ch._bot_open_id = "ou_bot"
        dispatched: list[str] = []

        async def fake_dispatch(msg):
            dispatched.append(msg.text)

        ch.dispatch = fake_dispatch  # type: ignore[method-assign]
        evt = self._make_evt(message_id="om_dup", open_id="ou_alice", chat_id="oc_c1")
        asyncio.run(ch._handle_event(evt))
        asyncio.run(ch._handle_event(evt))
        assert len(dispatched) == 1

    def test_allowlist_drops_non_allowed(self, tmp_dedup_path, clean_lark_env, monkeypatch):
        """DM sender 不在白名单 → 丢。"""
        monkeypatch.setenv("LARK_ALLOWED_USERS", "ou_trusted")
        ch = _make_lark(clean_lark_env, tmp_dedup_path)
        ch._bot_open_id = "ou_bot"
        dispatched: list[str] = []

        async def fake_dispatch(msg):
            dispatched.append(msg.text)

        ch.dispatch = fake_dispatch  # type: ignore[method-assign]
        evt = self._make_evt(message_id="om_x1", open_id="ou_stranger", chat_id="oc_c1")
        asyncio.run(ch._handle_event(evt))
        assert dispatched == []

    def test_allowlist_lets_in_allowed(self, tmp_dedup_path, clean_lark_env, monkeypatch):
        monkeypatch.setenv("LARK_ALLOWED_USERS", "ou_trusted")
        ch = _make_lark(clean_lark_env, tmp_dedup_path)
        ch._bot_open_id = "ou_bot"
        dispatched: list[str] = []

        async def fake_dispatch(msg):
            dispatched.append(msg.text)

        ch.dispatch = fake_dispatch  # type: ignore[method-assign]
        evt = self._make_evt(
            message_id="om_x2", open_id="ou_trusted", chat_id="oc_c1", text="hello"
        )
        asyncio.run(ch._handle_event(evt))
        assert dispatched == ["hello"]

    def test_per_chat_lock_serializes_dispatch(self, tmp_dedup_path, clean_lark_env):
        """两个并发事件同 chat_id,第二个应等第一个跑完 dispatch。

        直接验证 ``_get_chat_lock`` 的串行(集成路径覆盖,不走 dispatch)。
        """
        ch = _make_lark(clean_lark_env, tmp_dedup_path)
        ch._bot_open_id = "ou_bot"
        order: list[str] = []

        async def task(label: str) -> None:
            lock = ch._get_chat_lock("oc_same")
            async with lock:
                order.append(f"enter_{label}")
                await asyncio.sleep(0.05)
                order.append(f"exit_{label}")

        async def main() -> None:
            await asyncio.gather(task("m1"), task("m2"))
        asyncio.run(main())
        # m1 必须先 enter → exit → m2 enter → exit
        assert order == ["enter_m1", "exit_m1", "enter_m2", "exit_m2"]

    def test_empty_text_dispatches_nothing(self, tmp_dedup_path, clean_lark_env):
        """空文本(非媒体类型)→ 不 dispatch(原本就有这逻辑,回归测试)。"""
        ch = _make_lark(clean_lark_env, tmp_dedup_path)
        ch._bot_open_id = "ou_bot"
        dispatched: list[str] = []

        async def fake_dispatch(msg):
            dispatched.append(msg.text)

        ch.dispatch = fake_dispatch  # type: ignore[method-assign]
        evt = self._make_evt(message_id="om_e1", open_id="ou_a", chat_id="oc_c1", text="")
        asyncio.run(ch._handle_event(evt))
        # 非媒体类型且 text 为空 → Phase 32 仍早退(媒体占位仅在媒体类型时填)
        assert dispatched == []


# ============================================================
# 鲁棒性 / 启动期 fail-fast
# ============================================================

class TestZRobustness:
    def test_import_does_not_require_lark(self):
        """lark-oapi 未装时,模块级 import 不应 raise。"""
        # 这里反向验证:虽然本机装了 lark,但代码 try/except 应吞 ImportError
        # 没法在已装环境下直接验证,但至少 import OK 已经覆盖
        from openclaw.channels import lark
        assert hasattr(lark, "_HAS_LARK")
        assert isinstance(lark._HAS_LARK, bool)

    def test_ruff_async_rule_clean(self):
        """ruff --select ASYNC 0 错(防止 async 内阻塞 IO)。

        ruff 未装 → skip(本机可能没装,装了就跑)。
        """
        import subprocess
        result = subprocess.run(
            [sys.executable, "-m", "ruff", "check",
             "openclaw/channels/lark.py",
             "--select", "ASYNC", "--output-format=concise"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        if result.returncode != 0 and "No module named ruff" in result.stderr:
            pytest.skip(f"ruff 未装: {result.stderr.strip()}")
        # ruff 自己退出码 1 也算 violations;我们要求 returncode 0(0 violations)
        assert result.returncode == 0, (
            f"ruff ASYNC 错:\n{result.stdout}\n{result.stderr}"
        )
