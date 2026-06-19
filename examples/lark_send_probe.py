"""第二段:探测 bot 的消息发送权限 + scopes。

通过 tenant_access_token 发到 im/v1/messages 需要:
  - bot 必须有 im:message 发送 scope
  - 必须指定 receive_id_type (open_id / chat_id / email)

我们 probe 一下发到 bot 自己(open_id):
  POST /im/v1/messages
  body: { receive_id: <bot open_id>, msg_type: "text", content: {...} }
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

APP_ID = os.environ.get("LARK_APP_ID", "")
APP_SECRET = os.environ.get("LARK_APP_SECRET", "")
LARK_BASE = "https://open.feishu.cn/open-apis"


def _check(label, ok, extra=""):
    mark = "✅" if ok else "❌"
    print(f"  {mark} {label}{(' — ' + extra) if extra else ''}")


async def main() -> None:
    print(f"\n=== 飞书发消息权限探测 ===\n")
    async with httpx.AsyncClient(timeout=15) as c:
        # 拿 token
        r = await c.post(
            f"{LARK_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": APP_ID, "app_secret": APP_SECRET},
        )
        token = r.json().get("tenant_access_token", "")
        if not token:
            print("  ❌ 拿不到 token,退出")
            return
        auth = {"Authorization": f"Bearer {token}"}

        # 拿 bot open_id
        r = await c.get(f"{LARK_BASE}/bot/v3/info", headers=auth)
        bot = r.json().get("bot", {})
        bot_open_id = bot.get("open_id", "")
        print(f"  bot open_id = {bot_open_id}")
        print(f"  bot name    = {bot.get('app_name') or bot.get('name')}")

        # 尝试发消息给 bot 自己
        print(f"\n  尝试给 bot 自己发一条 text 消息…")
        r = await c.post(
            f"{LARK_BASE}/im/v1/messages",
            headers=auth,
            params={"receive_id_type": "open_id"},
            json={
                "receive_id": bot_open_id,
                "msg_type": "text",
                "content": '{"text": "openclaw-py 烟测:如果你看到这条,代表 im:message 发送权限 OK"}',
            },
        )
        body = r.json()
        code = body.get("code")
        msg = body.get("msg", "")
        _check(
            f"POST /im/v1/messages (open_id)",
            r.status_code == 200 and code == 0,
            f"http={r.status_code} code={code} msg={msg[:80]}",
        )
        if code == 0:
            data = body.get("data", {})
            msg_id = data.get("message_id", "")
            print(f"     message_id = {msg_id}")
            print(f"     chat_id    = {data.get('chat_id', '')}")
            print(f"     (注:bot 不能给自己发消息,所以 message_id 即使成功 chat_id 也会是 null/empty)")
        else:
            print()
            print("  常见错误码:")
            if code == 230020:
                print("    230020  bot 不在 chat / 没权限(常见于个人测试 app)")
            elif code == 230002:
                print("    230002  bot 被禁用 / 不可用")
            elif code == 230006:
                print("    230006  你的 app 没有 im:message 发送 scope,需在后台开通")
            elif code == 99991663:
                print("    99991663  消息内容超长 / 包含违规词")
            else:
                print(f"    {code}  {msg}")


if __name__ == "__main__":
    asyncio.run(main())
