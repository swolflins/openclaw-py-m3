"""P11 飞书端到端 mock 演示。

在沙箱(WS 出不去)也能跑通完整 dispatch 链路:
  用户发消息 → LarkChannel 解析 → AutoReply → AgentLoop(echo) → send() → reply

跟 examples/lark_run.py 的区别:
  lark_run.py       起真 WS,需要沙箱外运行(本地)
  lark_e2e_mock.py  不起 WS,直接 inject 入站事件,在沙箱里就能跑

用法:
  python examples/lark_e2e_mock.py
  python examples/lark_e2e_mock.py --text "求和 1+1"

输出:
  入站消息 → 中间产物(session_id / message_id / agent 输入)
  → agent 输出 → reply 调用(message_id + text)→ 标记
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lark_oapi.api.im.v1 import (  # noqa: E402
    P2ImMessageReceiveV1,
    P2ImMessageReceiveV1Data,
)
from lark_oapi.api.im.v1.model.event_sender import EventSender  # noqa: E402
from lark_oapi.api.im.v1.model.event_message import EventMessage  # noqa: E402

from openclaw.channels.lark import LarkChannel  # noqa: E402
from openclaw.config.settings import LarkSettings  # noqa: E402


class _EchoAgent:
    """演示用 agent:简单 echo。"""

    async def handle(self, session_id, text, **kw):
        class R:
            content = f"🤖 echo: {text}"
            tool_calls = []
            iterations = 1
        return R()

    async def new_session(self, sid=None):
        return sid or "echo"

    @property
    def tools(self): return None

    @property
    def memory(self): return None

    @property
    def auto_reply(self): return None


def _make_event(chat_id: str, open_id: str, message_id: str, text: str) -> P2ImMessageReceiveV1:
    evt = P2ImMessageReceiveV1()
    evt.event = P2ImMessageReceiveV1Data()
    evt.event.sender = EventSender(
        d={"sender_id": {"open_id": open_id, "union_id": "u", "user_id": "u"},
           "sender_type": "user", "tenant_key": "tk"}
    )
    evt.event.message = EventMessage(
        d={
            "message_id": message_id,
            "chat_id": chat_id,
            "chat_type": "p2p",
            "message_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
    )
    return evt


def _print_banner(text: str) -> None:
    print()
    print("─" * 60)
    print(f"  模拟用户发: {text!r}")
    print("─" * 60)


async def main_async(text: str, chat_id: str, open_id: str, message_id: str) -> None:
    _print_banner(text)

    # 抓 reply 调用(替掉真 reply,不真发飞书)
    replies: list[tuple[str, str]] = []

    async def _fake_reply(self, msg_id, body):
        replies.append((msg_id, body))

    # 替掉 _reply_to_lark 后,真的 LarkChannel 内部流程
    # 还是会调 send(),send 走 _safe_send → on_reply → self.send → _reply_to_lark
    # base.BaseChannel 在 channel 上调 self.send(session_id, text)
    # LarkChannel.send 内部从 _last_msg_id 取 message_id → 调 _reply_to_lark
    # 所以我们 monkeypatch LarkChannel._reply_to_lark 就行
    from openclaw.channels import lark as _lark_mod
    _real = _lark_mod.LarkChannel._reply_to_lark
    _lark_mod.LarkChannel._reply_to_lark = _fake_reply  # type: ignore[assignment]

    try:
        # 真 LarkChannel + 假 agent(不连真飞书)
        ch = LarkChannel(
            _EchoAgent(),
            LarkSettings(app_id="cli_aabf7da5e178dbb5", app_secret="mock"),
        )

        # 模拟 1 条入站消息
        evt = _make_event(chat_id, open_id, message_id, text)
        await ch._handle_event(evt)

        # 打印中间产物
        print(f"  session_id       = {ch.received[0].session_id}")
        print(f"  user_id          = {ch.received[0].user_id}")
        print(f"  text             = {ch.received[0].text!r}")
        print(f"  metadata.is_dm   = {ch.received[0].metadata['is_dm']}")
        print(f"  metadata.msg_id  = {ch.received[0].metadata['message_id']}")

        # reply
        if replies:
            msg_id, body = replies[0]
            print(f"  → reply 到 msg_id = {msg_id}")
            print(f"  → reply text     = {body!r}")
            print(f"  ✅ 端到端链路通:WS 收到 → dispatch → agent → reply")
        else:
            print("  ❌ 没生成 reply(被 AutoReply drop 或 agent 返空)")
    finally:
        _lark_mod.LarkChannel._reply_to_lark = _real  # 还原


def main() -> None:
    p = argparse.ArgumentParser(description="飞书 e2e mock 演示")
    p.add_argument("--text", default="ping", help="模拟用户发的消息")
    p.add_argument("--chat-id", default="oc_demo_chat")
    p.add_argument("--open-id", default="ou_demo_user")
    p.add_argument("--message-id", default="om_demo_msg")
    args = p.parse_args()

    print("\n=== 飞书 e2e mock(沙箱可跑)===")
    print("  模拟飞书 WS 收到一条 P2ImMessageReceiveV1 事件")
    print("  走 LarkChannel._handle_event → dispatch → echo agent → reply")
    print("  reply 调用被拦下打印,不真发飞书")

    asyncio.run(main_async(args.text, args.chat_id, args.open_id, args.message_id))
    print("\n  下一步:本地跑 examples/lark_run.py,真接 WS → 真发飞书\n")


if __name__ == "__main__":
    main()
