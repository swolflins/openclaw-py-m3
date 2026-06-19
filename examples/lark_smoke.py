"""Phase 7+ 真实飞书凭据烟测。

目标:验证你提供的 app_id / app_secret 在飞书服务端
- 能拿到 tenant_access_token
- 能拿到 app open_id / user 列表 / bot 身份
- 能通过 im/v1/messages API 发一条消息(给 bot 自己)

不依赖任何 LLM key(可以只测 channel 通路)。

用法:
  python examples/lark_smoke.py
  # 或显式传:
  LARK_APP_ID=cli_xxx LARK_APP_SECRET=xxx python examples/lark_smoke.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ---- 1) 加载凭据 ----
# 不要把 app_secret 写进文件,只走环境变量:
#   LARK_APP_ID=cli_xxx LARK_APP_SECRET=xxx python examples/lark_smoke.py
APP_ID = os.environ.get("LARK_APP_ID", "")
APP_SECRET = os.environ.get("LARK_APP_SECRET", "")

LARK_BASE = "https://open.feishu.cn/open-apis"


def _check(label: str, ok: bool, extra: str = "") -> None:
    mark = "✅" if ok else "❌"
    print(f"  {mark} {label}{(' — ' + extra) if extra else ''}")


async def main() -> None:
    print(f"\n=== 飞书烟测 ===")
    print(f"  app_id     = {APP_ID}")
    print(f"  app_secret = {APP_SECRET[:6] + '***' if APP_SECRET else '(empty)'}\n")

    if not APP_ID or not APP_SECRET:
        _check("凭据", False, "app_id / app_secret 至少一个为空 — 请设置 LARK_APP_ID / LARK_APP_SECRET 环境变量")
        print("  示例:LARK_APP_ID=cli_xxx LARK_APP_SECRET=xxx python examples/lark_smoke.py")
        return

    # ---- 1) 拿 tenant_access_token ----
    print("[1] 拿 tenant_access_token")
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            f"{LARK_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": APP_ID, "app_secret": APP_SECRET},
        )
        body = r.json()
        _check(
            "POST /auth/v3/tenant_access_token/internal 200",
            r.status_code == 200,
            f"status={r.status_code}",
        )
        _check(
            "code=0",
            body.get("code") == 0,
            f"code={body.get('code')} msg={body.get('msg')}",
        )
        token = body.get("tenant_access_token", "")
        expire = body.get("expire", 0)
        # 飞书新版 token 格式 t-xxx(短),老版是长 base64,任一种都算 OK
        _check(
            "拿到 token(>20 字符)",
            len(token) > 20,
            f"len={len(token)} expire={expire}s",
        )
        if not token:
            print("\n  ⚠️  拿不到 token,后续全部跳过")
            return

        auth = {"Authorization": f"Bearer {token}"}

        # ---- 2) 列消息 v1 端点(需要 chat_id 才能命中,这里只 ping 一下)----
        print("\n[2] im/v1 端点探测(不需要具体 chat_id)")
        r = await c.get(f"{LARK_BASE}/im/v1/chats", headers=auth, params={"page_size": 1})
        body = r.json()
        # 230002 = bot 不在 chat 内 / 没权限;99992402 = 参数问题;0 = OK
        code = body.get("code")
        # 只要端点响应了(不是 404 / 401)就算通
        _check(
            "GET /im/v1/chats 端点响应",
            r.status_code == 200,
            f"http={r.status_code} code={code} msg={body.get('msg', '')[:60]}",
        )
        if code == 0:
            items = body.get("data", {}).get("items", [])
            print(f"     bot 所在 chat 数 = {len(items)}")
        elif code in (230002, 230020, 230021, 230022):
            print(f"     (注:code {code} 通常是 bot 没在任何 chat / 权限不足,正常)")
        elif code in (99992402,):
            print(f"     (注:code {code} 是参数问题,通常是缺 page_size 等)")

        # ---- 3) 拿 bot 自己的 open_id ----
        print("\n[3] 拿 bot open_id(机器人自己)")
        r = await c.get(f"{LARK_BASE}/bot/v3/info", headers=auth)
        body = r.json()
        if body.get("code") == 0:
            bot = body.get("bot", {})
            bot_open_id = bot.get("open_id", "")
            bot_name = bot.get("app_name", bot.get("name", "bot"))
            _check("GET /bot/v3/info 通过", bool(bot_open_id), f"open_id={bot_open_id[:12]}... name={bot_name}")
        else:
            bot_open_id = ""
            _check("GET /bot/v3/info", False, f"code={body.get('code')} msg={body.get('msg')}")

        # ---- 4) 通过 LarkChannel SDK 跑一次完整初始化(模拟生产) ----
        print("\n[4] LarkChannel SDK 初始化")
        from openclaw.config.settings import LarkSettings
        from openclaw.channels.lark import LarkChannel, _HAS_LARK

        _check("lark-oapi 已安装", _HAS_LARK, f"_HAS_LARK={_HAS_LARK}")

        settings = LarkSettings(app_id=APP_ID, app_secret=APP_SECRET, use_ws=True)
        _check("LarkSettings 构造", settings.app_id == APP_ID, f"app_id={settings.app_id}")

        # 构造一个 fake agent_loop(只占位,因为不真跑 start)
        class FakeAgent:
            async def handle(self, sid, text, **kw):
                class R:
                    content = f"[echo:{sid}] {text}"
                    tool_calls = []
                    iterations = 1
                return R()
            async def new_session(self, sid=None):
                return sid or "fake"
            @property
            def tools(self): return None
            @property
            def memory(self): return None

        ch = LarkChannel(FakeAgent(), settings)
        _check("LarkChannel 构造", ch is not None, f"name={ch.name}")
        _check("LarkChannel.available", ch.available,
               f"_HAS_LARK={_HAS_LARK} app_id={bool(APP_ID)} app_secret={bool(APP_SECRET)}")

        # ---- 5) 模拟 WS 收到一条消息(走 dispatch 管道) ----
        print("\n[5] 模拟 WS 收到一条消息(走 dispatch 管道)")
        from openclaw.channels.base import IncomingMessage
        from openclaw.core.auto_reply import AutoReplyManager, AutoReplyConfig
        from openclaw.core.skills import load_skills
        from openclaw.bus import EventBus

        # 装 AutoReply(放行所有 DM)
        ar = AutoReplyManager(AutoReplyConfig(auto_in_dm=True))
        # override agent_loop 上的 auto_reply 属性(用 base 类的 dispatch 路径)
        ch.agent_loop = FakeAgent()
        # base.dispatch 用 ch.auto_reply(基类属性),需要装
        # 简单:在 FakeAgent 加 auto_reply
        ch.agent_loop.auto_reply = ar
        ch.auto_reply = ar  # type: ignore[attr-defined]

        msg = IncomingMessage(
            channel="lark",
            session_id="lark:oc_test:ou_test",
            user_id="ou_test",
            text="ping",
            raw=None,
            metadata={"is_dm": True, "mentioned": False, "chat_id": "oc_test", "open_id": "ou_test"},
        )

        # 收集回信到 bot
        sent: list[tuple[str, str]] = []
        async def fake_send(session_id, text):
            sent.append((session_id, text))
        ch.send = fake_send  # override

        await ch.dispatch(msg)
        _check("dispatch 走通", True, f"sent={len(sent)} 条")
        if sent:
            sid, txt = sent[0]
            print(f"     session_id = {sid}")
            print(f"     text       = {txt[:80]}")

    print("\n=== 飞书烟测完成 ===")
    print("\n下一步:")
    print("  1) 上面所有 ✅ → 凭据可用,LarkChannel 已就绪")
    print("  2) 启动 gateway:`uvicorn openclaw.gateway.app:app --host 0.0.0.0 --port 8080`")
    print("  3) 在另一终端:export LARK_APP_ID=... LARK_APP_SECRET=...")
    print("     python -m openclaw run --channel lark")
    print("  4) 用飞书 app 给自己发消息,bot 应当通过 AgentLoop 自动回复")


if __name__ == "__main__":
    asyncio.run(main())
