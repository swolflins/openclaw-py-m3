"""Discord 渠道(Phase 7)。

实现两种入站方式:
- Webhook:由 FastAPI/uvicorn 之类外部 server 推送进来(用 ingest_webhook())
- Gateway:通过 discord.py-gateway 直接订阅 events(可选,需 pip install discord.py)

出站:Discord Bot API(httpx),只支持普通文本消息。

依赖:无核心依赖;gateway 模式需要 discord.py;
     **Webhook 验签(Ed25519)需要 pynacl** — 缺失时在生产模式启动会被拒绝。
环境变量: DISCORD_BOT_TOKEN (BOT 入站), DISCORD_PUBLIC_KEY (Webhook 验签)
         OPENCLAW_ENV=production 触发启动期 pynacl 缺失检查
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Optional
import httpx

from openclaw.agent.loop import AgentLoop
from openclaw.channels.base import BaseChannel, IncomingMessage
from openclaw.core.logging import get_logger

logger = get_logger(__name__)

API = "https://discord.com/api/v10"

# Phase 25 / A2: 启动期强制 pynacl 在 production 可用
_PROD_ENV_VARS = ("OPENCLAW_ENV", "OPENCLAW_GATEWAY_ENV")
_PROD_VALUES = {"production", "prod"}


def _is_production_mode() -> bool:
    """Phase 25: 检测是否处于生产模式(任一 env var ∈ {production, prod})。"""
    for var in _PROD_ENV_VARS:
        if os.environ.get(var, "").strip().lower() in _PROD_VALUES:
            return True
    return False


def _has_pynacl() -> bool:
    """Phase 25: 探测 pynacl 是否真的可用(不抛异常)。"""
    try:
        from nacl.signing import VerifyKey  # noqa: F401
    except Exception:
        return False
    return True


class DiscordChannel(BaseChannel):
    name = "discord"

    def __init__(
        self,
        token: str,
        agent_loop: AgentLoop,
        *,
        public_key: Optional[str] = None,
        use_gateway: bool = False,
    ) -> None:
        super().__init__(agent_loop)
        self.token = token
        self.public_key = public_key
        self.use_gateway = use_gateway
        self._client: Optional[httpx.AsyncClient] = None
        self._gw_task: Optional[asyncio.Task] = None
        # Phase 25 / A2: 生产模式下,如果配了 public_key 但 pynacl 不可用,
        # 直接抛 RuntimeError 拒绝启动(fail-closed)。
        # 非生产模式只 log warning,让本地开发不强制装 pynacl。
        if self.public_key and not _has_pynacl() and _is_production_mode():
            raise RuntimeError(
                "[phase25/A2] production 模式 + 配置了 DISCORD_PUBLIC_KEY 但 pynacl 缺失。"
                "Webhook 验签无法进行,启动被拒绝。修复: pip install pynacl"
            )

    @property
    def available(self) -> bool:
        return bool(self.token)

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bot {self.token}"}

    async def _get_client(self) -> httpx.AsyncClient:
        current_loop_id = id(asyncio.get_running_loop())
        if self._client is None or getattr(self, "_loop_id", None) != current_loop_id or self._client.is_closed:
            self._client = httpx.AsyncClient(base_url=API, timeout=20, headers=self._auth())
            self._loop_id = current_loop_id
        return self._client

    async def start(self) -> None:
        if not self.available:
            raise RuntimeError("Discord 凭据未配置 (DISCORD_BOT_TOKEN)")
        # Phase 25 / A2: 再次校验 production + public_key + pynacl(双保险)。
        if self.public_key and not _has_pynacl() and _is_production_mode():
            raise RuntimeError(
                "[phase25/A2] start() 检测到 production 模式 + public_key 已配置但 pynacl 缺失。"
                "启动被拒绝(fail-closed)。修复: pip install pynacl"
            )
        # 先 GET /users/@me 确认 token 有效
        client = await self._get_client()
        r = await client.get("/users/@me")
        if r.status_code != 200:
            raise RuntimeError(f"discord token invalid: {r.status_code} {r.text[:200]}")
        bot_id = r.json().get("id")
        logger.info("discord_bot_ok", bot_id=bot_id)
        if self.use_gateway:
            self._gw_task = asyncio.create_task(self._gateway_loop(), name="discord-gw")
        else:
            # webhook 模式:啥都不做,等 ingest_webhook() 灌消息
            logger.info("discord_channel_running_in_webhook_mode")
        await self._stopped.wait()

    async def stop(self) -> None:
        self._stopped.set()
        if self._gw_task:
            self._gw_task.cancel()
            try:
                await self._gw_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    async def send(self, session_id: str, text: str) -> None:
        # session_id 格式: discord:<channel_id>
        channel_id = session_id.split(":", 1)[1] if ":" in session_id else session_id
        # 切 2000 字符
        for i in range(0, len(text), 2000):
            chunk = text[i:i + 2000]
            try:
                client = await self._get_client()
                r = await client.post(
                    f"/channels/{channel_id}/messages",
                    json={"content": chunk},
                )
                if r.status_code >= 400:
                    logger.warning("discord_send_failed", status=r.status_code, body=r.text[:200])
            except Exception:
                logger.exception("discord send failed")

    # ---------- Webhook 入站 ----------

    def verify_signature(self, body: bytes, signature: str, timestamp: str) -> bool:
        """Discord 出站 webhook 必须验签(Ed25519),简化为可选(pip install nacl)。

        **Phase 25 / A2 安全修复(fail-closed)**:
        - 之前 pynacl 缺失时 ``return True`` → 生产部署忘装 pynacl 会**放行所有 webhook**(签名伪造)。
        - 现在: pynacl 不可用时 ``return False`` + log error,让外层路由返 400。
        - 启动期已在 ``__init__`` / ``start`` 强制 production + public_key + pynacl 三者必须一致,
          所以"运行到这里还缺 pynacl"一定是误配置 → 直接拒。
        """
        if not self.public_key:
            return True  # 没配公钥就不验(本地开发)
        try:
            from nacl.signing import VerifyKey
        except Exception:
            # **关键**: 缺失时 return False,不再 fail-open。
            logger.error(
                "pynacl not installed but DISCORD_PUBLIC_KEY is set — "
                "**rejecting** webhook (fail-closed). Install pynacl to verify signatures."
            )
            return False
        try:
            vk = VerifyKey(bytes.fromhex(self.public_key))
            vk.verify(timestamp.encode() + body, bytes.fromhex(signature))
            return True
        except Exception:
            return False

    async def ingest_webhook(self, payload: dict[str, Any]) -> None:
        """FastAPI/Flask 路由处理函数调用这个:把 Discord POST 进来的事件翻译成 IncomingMessage。"""
        # Discord interaction 有 3 种类型:
        #  1: PING
        #  2: APPLICATION_COMMAND  (slash)
        #  3: MESSAGE_COMPONENT     (button)
        t = payload.get("type")
        if t == 1:
            return  # PING 不需要处理
        data = payload.get("data") or {}
        channel_id = payload.get("channel_id")
        user = payload.get("member", {}).get("user") or payload.get("user") or {}
        user_id = str(user.get("id", ""))
        text = data.get("name") or data.get("custom_id") or data.get("content") or ""
        if not text or not channel_id or not user_id:
            return
        await self.dispatch(IncomingMessage(
            channel=self.name,
            session_id=f"discord:{channel_id}",
            user_id=user_id,
            text=str(text),
            raw=payload,
            metadata={
                "is_dm": payload.get("guild_id") is None,
                "mentioned": True,  # slash 视为 always addressed
                "channel_id": str(channel_id),
                "guild_id": payload.get("guild_id"),
                "username": user.get("username"),
            },
        ))

    # ---------- Gateway 入站(可选) ----------

    async def _gateway_loop(self) -> None:
        """最小 gateway:用 httpx 走 HTTP /gateway/bot 拿 URL,再简单 ws。
        生产环境推荐 discord.py-gateway,这里只是占位骨架。
        """
        try:
            import websockets  # type: ignore
        except ImportError:
            logger.error("discord gateway 需要 pip install websockets")
            return
        try:
            client = await self._get_client()
            r = await client.get("/gateway/bot")
            ws_url = r.json().get("url", "wss://gateway.discord.gg")
        except Exception:
            logger.exception("get gateway url failed")
            return
        async with websockets.connect(ws_url + "/?v=10&encoding=json") as ws:
            # 接收 hello
            json.loads(await ws.recv())
            await ws.send(json.dumps({
                "op": 2,
                "d": {
                    "token": self.token,
                    "intents": 513,  # GUILDS + GUILD_MESSAGES
                    "properties": {"os": "linux", "browser": "openclaw", "device": "openclaw"},
                },
            }))
            while not self._stopped.is_set():
                raw = await ws.recv()
                ev = json.loads(raw)
                if ev.get("t") == "MESSAGE_CREATE":
                    msg = ev.get("d") or {}
                    if msg.get("author", {}).get("bot"):
                        continue
                    await self.dispatch(IncomingMessage(
                        channel=self.name,
                        session_id=f"discord:{msg.get('channel_id')}",
                        user_id=str(msg.get("author", {}).get("id", "")),
                        text=msg.get("content", ""),
                        raw=msg,
                        metadata={
                            "is_dm": (msg.get("guild_id") is None),
                            "mentioned": False,
                            "channel_id": str(msg.get("channel_id")),
                            "guild_id": msg.get("guild_id"),
                            "username": msg.get("author", {}).get("username"),
                        },
                    ))


def from_env(agent_loop: AgentLoop) -> Optional[DiscordChannel]:
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        return None
    return DiscordChannel(
        token=token,
        agent_loop=agent_loop,
        public_key=os.environ.get("DISCORD_PUBLIC_KEY"),
        use_gateway=os.environ.get("DISCORD_GATEWAY", "0") == "1",
    )
