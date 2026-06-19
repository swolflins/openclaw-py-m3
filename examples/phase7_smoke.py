"""Phase 7 端到端烟测:多渠道 + ChannelManager + AutoReply + 真实 LLM。

用一个简单的 mock agent(不联网,直接回 echo)作为兜底,主要验证:
- 6 个 channel 的入站解析(sendMessage / ingest_event / ingest_webhook / _handle_envelope)
- ChannelManager 注册 + 共享 agent / auto_reply
- EchoChannel 走统一管道:模板/黑名单/触发词/限流
- 真实 LLM 跑 1 个有意义的端到端场景(用 mock agent 跑 + 1 次真 LLM)
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class StubAgent:
    """不连 LLM,回显 [session_id] text。"""
    async def handle(self, session_id, text, **kw):
        class R:
            content: str = ""
            tool_calls: list = []
            iterations: int = 1
        r = R()
        r.content = f"[{session_id}] {text}"
        return r


async def real_agent_handle(session_id: str, text: str) -> str:
    """调一次真 LLM,处理 1 个 turn。"""
    from openclaw.agent.loop import AgentLoop
    if not hasattr(real_agent_handle, "_loop"):
        from openclaw.core.config import ConfigLoader
        from openclaw.core.logging import setup_logging
        from openclaw.memory.scoped import ScopedMemory
        from openclaw.memory.short_term import ShortTermStore
        from openclaw.memory.soul import SoulLoader
        from openclaw.providers.factory import ProviderFactory
        from openclaw.providers.router import ProviderRouter
        from openclaw.tools.builtin import register_builtin_tools
        from openclaw.tools.registry import ToolRegistry

        setup_logging("WARN", json=False)
        cfg = ConfigLoader(ROOT / "openclaw.agnes.yaml").load()
        factory = ProviderFactory()
        providers = [factory.build(p) for p in cfg.providers]
        primary, fallbacks = providers[0], providers[1:]
        llm = ProviderRouter(primary, fallbacks, strategy="fallback_only")

        work = ROOT / ".test_p7_phase_memory"
        work.mkdir(exist_ok=True)
        cfg.memory.dir = work
        short = ShortTermStore(cfg.memory.dir)
        scoped = ScopedMemory(short_term=short, long_term=None, soul=SoulLoader(paths=cfg.agent.soul_paths))

        tools = ToolRegistry()
        register_builtin_tools(
            tools,
            fs_root=str(ROOT), shell_default_cwd=str(ROOT),
            shell_allowed=["echo", "ls", "date", "cat"],
            include=["echo", "shell_exec", "list_dir", "get_current_time", "date_diff"],
        )
        real_agent_handle._loop = AgentLoop(
            llm=llm, tools=tools, memory=scoped,
            system_prompt=cfg.agent.system_prompt,
            max_tool_iterations=cfg.agent.max_tool_iterations,
            history_window=cfg.agent.history_window,
        )
    loop = real_agent_handle._loop
    resp = await loop.handle(session_id, text)
    return resp.content or ""


def main() -> None:
    from openclaw.channels import (
        ChannelManager, EchoChannel,
        TelegramChannel, DiscordChannel, SlackChannel,
        WhatsAppChannel, SignalChannel, IMessageChannel,
        IncomingMessage,
    )
    from openclaw.core.auto_reply import AutoReplyConfig, AutoReplyManager
    from openclaw.core.rate_limit import RateLimiter

    print("=" * 70)
    print("Phase 7 多渠道烟测")
    print("=" * 70)

    # === 1) ChannelManager + 依赖注入 ===
    print("\n[1] ChannelManager + 共享 auto_reply")
    agent = StubAgent()
    arm = AutoReplyManager(AutoReplyConfig(
        triggers=["bot"],
        blacklist=[r"rm\s+-rf"],
        templates={"ping": "pong"},
        rate_per_user=RateLimiter(rate=0.5, burst=1),
    ))
    mgr = ChannelManager(agent, auto_reply=arm)
    e1 = EchoChannel()  # 不传 agent,让 manager 注入
    e2 = EchoChannel()
    mgr.register(e1)
    mgr.register(e2)
    print(f"    registered: {[c.name for c in mgr.channels()]}")
    assert e1.agent_loop is agent
    assert e1.auto_reply is arm
    print("    ✓ 依赖注入 OK")

    # === 2) 各 channel 的入站解析 ===
    print("\n[2] 6 个 channel 的入站解析")
    chs = {
        "telegram": TelegramChannel(token="x:1", agent_loop=agent),
        "discord":  DiscordChannel(token="x",   agent_loop=agent),
        "slack":    SlackChannel(token="x",     agent_loop=agent),
        "whatsapp": WhatsAppChannel(token="t", phone_id="p", agent_loop=agent),
        "signal":   SignalChannel(base_url="http://x", account="+8613800138000", agent_loop=agent),
        "imessage": IMessageChannel(agent_loop=agent, bluebubbles_url="http://bb"),
    }
    # Telegram: getUpdates 一条 message
    asyncio.run(chs["telegram"]._handle_update({
        "update_id": 1,
        "message": {
            "message_id": 1, "chat": {"id": 999, "type": "private"},
            "from": {"id": 7, "username": "alice", "first_name": "Alice"},
            "text": "hello",
        },
    }))
    # Discord: interaction
    asyncio.run(chs["discord"].ingest_webhook({
        "type": 2, "channel_id": "888", "guild_id": "g1",
        "member": {"user": {"id": "123", "username": "bob"}},
        "data": {"name": "ask"},
    }))
    # Slack: app_mention
    asyncio.run(chs["slack"].ingest_event({
        "type": "event_callback",
        "event": {"type": "app_mention", "channel": "C123", "user": "U999", "text": "<@UBOTID> hi"},
    }))
    # WhatsApp: text message
    asyncio.run(chs["whatsapp"].ingest_webhook({
        "entry": [{
            "changes": [{
                "field": "messages",
                "value": {
                    "contacts": [{"wa_id": "8613800138000"}],
                    "messages": [{
                        "from": "8613800138000", "id": "wamid.1",
                        "timestamp": "1", "type": "text",
                        "text": {"body": "hi wa"},
                    }],
                },
            }],
        }],
    }))
    # Signal: dataMessage
    asyncio.run(chs["signal"]._handle_envelope({
        "source": "+8613800138000", "dataMessage": {"message": "hi signal"},
    }))
    # iMessage: new-message
    asyncio.run(chs["imessage"].ingest_webhook({
        "type": "new-message",
        "data": {
            "text": "hi imessage",
            "handle": {"address": "+8613800138000"},
            "chats": [{"chatGuid": "i;-;1"}],
        },
    }))
    for name, ch in chs.items():
        assert len(ch.received) == 1, f"{name} should receive 1, got {len(ch.received)}"
        print(f"    ✓ {name:8s} session={ch.received[0].session_id:40s} text={ch.received[0].text!r}")

    # === 3) EchoChannel 走统一管道:模板/黑名单/触发/限流 ===
    print("\n[3] EchoChannel 统一管道")
    # 模板命中:写 replies,内容是模板
    ech_t = EchoChannel(agent, auto_reply=arm)
    asyncio.run(ech_t.dispatch(IncomingMessage(
        channel="echo", session_id="u1", user_id="u1",
        text="ping ping", metadata={"is_dm": True},
    )))
    assert len(ech_t.received) == 1 and len(ech_t.replies) == 1
    assert ech_t.replies[0][1] == "pong", f"template 错误: {ech_t.replies[0]}"
    print(f"    ✓ template 命中: {ech_t.replies[0][1]!r}")

    # 黑名单:replies 不会增加
    ech_b = EchoChannel(agent, auto_reply=arm)
    asyncio.run(ech_b.dispatch(IncomingMessage(
        channel="echo", session_id="u2", user_id="u2",
        text="rm -rf / x", metadata={"is_dm": True},
    )))
    assert len(ech_b.received) == 1 and len(ech_b.replies) == 0, \
        f"blacklist 应被丢,replies 仍空,实际: {ech_b.replies}"
    s = arm.stats()
    assert s["block_blacklist"] >= 1
    print(f"    ✓ blacklist drop: received=1 replies=0 stats={s['block_blacklist']}")

    # 白名单触发("bot"):prompt 前缀 + 真实 agent 回显
    ech_w = EchoChannel(agent, auto_reply=arm)
    asyncio.run(ech_w.dispatch(IncomingMessage(
        channel="echo", session_id="u3", user_id="u3",
        text="bot 帮我", metadata={"is_dm": True},
    )))
    assert len(ech_w.replies) == 1
    body = ech_w.replies[0][1]
    # body 是 stub agent 的回显:"[u3] [上下文] channel=echo user=u3 ts=...\nbot 帮我"
    assert "channel=echo" in body and "user=u3" in body, f"prompt 前缀未注入: {body!r}"
    assert "bot 帮我" in body, f"原文未透传: {body!r}"
    print(f"    ✓ 白名单通过: {body[:80]!r}...")

    # 未触发:默认 is_dm=True 也会放行(默认 auto_in_dm=True),
    # 所以这一条实际上是放行;验证决策原因不是 "not addressed" 即可
    ech_s = EchoChannel(agent, auto_reply=arm)
    asyncio.run(ech_s.dispatch(IncomingMessage(
        channel="echo", session_id="u4", user_id="u4",
        text="随便聊聊", metadata={"is_dm": True},
    )))
    assert len(ech_s.replies) == 1, f"DM 默认应放行: {ech_s.replies}"
    print(f"    ✓ DM 默认放行: {ech_s.replies[0][1][:60]!r}...")

    # 限流:2 次请求,第 1 次放行(agent 回显),第 2 次被限流(返回限流提示)
    arm2 = AutoReplyManager(AutoReplyConfig(
        triggers=["bot"],
        rate_per_user=RateLimiter(rate=0.1, burst=1),
    ))
    ech2 = EchoChannel(agent, auto_reply=arm2)
    asyncio.run(ech2.dispatch(IncomingMessage(channel="echo", session_id="u9", user_id="u9", text="bot hi1", metadata={"is_dm": True})))
    asyncio.run(ech2.dispatch(IncomingMessage(channel="echo", session_id="u9", user_id="u9", text="bot hi2", metadata={"is_dm": True})))
    assert len(ech2.replies) == 2, f"应有 1 个 agent 回显 + 1 个限流提示,实际: {ech2.replies}"
    assert "bot hi1" in ech2.replies[0][1], f"第 1 次未放行: {ech2.replies[0]}"
    assert "稍等" in ech2.replies[1][1] or "限流" in ech2.replies[1][1] or "太快" in ech2.replies[1][1], \
        f"第 2 次应给限流提示: {ech2.replies[1]}"
    assert arm2.stats()["block_rate_user"] == 1
    print(f"    ✓ 限流 OK: 1 次放行 + 1 次限流提示 (block_rate_user=1)")

    # === 4) 真实 LLM 跑 1 个场景(用真 agnes-2.0-flash) ===
    print("\n[4] 真实模型端到端(EchoChannel 灌消息 → 真 LLM)")
    real_arm = AutoReplyManager(AutoReplyConfig(triggers=["bot"]))
    real_ech = EchoChannel(auto_reply=real_arm)
    real_ech.agent_loop = _make_real_agent()

    async def _run_real() -> str:
        await real_ech.dispatch(IncomingMessage(
            channel="echo", session_id="p7-real", user_id="u1",
            text="bot 用 shell_exec 跑 `date` 给我看当前时间",
            metadata={"is_dm": True},
        ))
        return real_ech.replies[-1][1]

    real_reply = asyncio.run(_run_real())
    print(f"    ✓ 真 LLM 回复: {real_reply[:200]!r}")

    print("\n" + "=" * 70)
    print("✅ Phase 7 多渠道烟测全部通过")
    print("=" * 70)


def _make_real_agent():
    """造一个真 agent,wrap 真实 LLM(直接 await,避免嵌套 asyncio.run)。"""
    class RealAgent:
        async def handle(self, session_id, text, **kw):
            class R:
                content: str = ""
                tool_calls: list = []
                iterations: int = 1
            r = R()
            r.content = await real_agent_handle(session_id, text)
            return r
    return RealAgent()


if __name__ == "__main__":
    main()
