"""Slack 渠道(Phase 7)。

支持:
- Events API webhook 入站(POST 到 FastAPI/Flask 路由,调 ingest_event())
- Web API 出站(chat.postMessage)

Socket Mode 需 pip install slack-sdk,这里不强制。

环境变量:
    SLACK_BOT_TOKEN        xoxb-...
    SLACK_SIGNING_SECRET   (Events API 签名校验)
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import time
from typing import Any, Optional

import httpx

from openclaw.agent.loop import AgentLoop
from openclaw.channels.base import BaseChannel, IncomingMessage
from openclaw.core.logging import get_logger

logger = get_logger(__name__)

API = "https://slack.com/api"


class SlackChannel(BaseChannel):
    name = "slack"

    def __init__(
        self,
        token: str,
        agent_loop: AgentLoop,
        *,
        signing_secret: Optional[str] = None,
    ) -> None:
        super().__init__(agent_loop)
        self.token = token
        self.signing_secret = signing_secret
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def available(self) -> bool:
        return bool(self.token)

    async def _get_client(self) -> httpx.AsyncClient:
        current_loop_id = id(asyncio.get_running_loop())
        if self._client is None or getattr(self, "_loop_id", None) != current_loop_id or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=API,
                timeout=20,
                headers={"Authorization": f"Bearer {self.token}"},
            )
            self._loop_id = current_loop_id
        return self._client

    async def start(self) -> None:
        if not self.available:
            raise RuntimeError("Slack 凭据未配置 (SLACK_BOT_TOKEN)")
        client = await self._get_client()
        r = await client.post("/auth.test")
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"slack token invalid: {data}")
        logger.info("slack_auth_ok", user=data.get("user"), team=data.get("team"))
        # webhook 模式:等 ingest_event()
        await self._stopped.wait()

    async def stop(self) -> None:
        self._stopped.set()
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    async def send(self, session_id: str, text: str) -> None:
        # session_id: slack:<channel_id>
        channel_id = session_id.split(":", 1)[1] if ":" in session_id else session_id
        for i in range(0, len(text), 4000):
            chunk = text[i:i + 4000]
            try:
                client = await self._get_client()
                r = await client.post(
                    "/chat.postMessage",
                    json={"channel": channel_id, "text": chunk, "mrkdwn": False},
                )
                if not r.json().get("ok"):
                    logger.warning("slack_send_failed", body=r.text[:200])
            except Exception:
                logger.exception("slack send failed")

    # ---------- Webhook 入站 ----------

    def verify_signature(self, body: bytes, timestamp: str, signature: str) -> bool:
        """Slack signing secret:HMAC-SHA256(secret, 'v0:'+ts+body)。"""
        # M8 修复:无 signing_secret 时 fail-closed(旧逻辑 return True = 放行)
        if not self.signing_secret:
            logger.critical(
                "slack_signing_secret_not_configured:webhook 验签被跳过 — "
                "任何人可伪造消息。请设置 SLACK_SIGNING_SECRET。"
            )
            return False
        if abs(time.time() - int(timestamp)) > 300:
            return False
        sig_base = f"v0:{timestamp}".encode() + body
        digest = hmac.new(self.signing_secret.encode(), sig_base, hashlib.sha256).hexdigest()
        return hmac.compare_digest(f"v0={digest}", signature)

    async def ingest_event(self, payload: dict[str, Any]) -> None:
        """FastAPI/Flask 路由处理函数调用这个。

        处理:event_callback (url_verification / app_mention / message)
        """
        # url_verification 握手:直接返回 challenge
        if payload.get("type") == "url_verification":
            return  # caller 应该读 payload["challenge"]
        ev = payload.get("event") or {}
        if ev.get("type") not in ("app_mention", "message"):
            return
        if ev.get("subtype"):
            return  # 跳过 edit/delete 等
        # 跳过 bot 自己发的
        if ev.get("bot_id"):
            return
        channel_id = ev.get("channel")
        user_id = ev.get("user")
        text = ev.get("text", "")
        if not channel_id or not user_id or not text:
            return
        # 去掉 <@BOTID> 之类 mention 标记
        import re
        text = re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()
        if not text:
            return
        await self.dispatch(IncomingMessage(
            channel=self.name,
            session_id=f"slack:{channel_id}",
            user_id=str(user_id),
            text=text,
            raw=ev,
            metadata={
                "is_dm": channel_id.startswith("D"),
                "mentioned": ev.get("type") == "app_mention",
                "channel_id": str(channel_id),
                "thread_ts": ev.get("thread_ts"),
            },
        ))


def from_env(agent_loop: AgentLoop) -> Optional[SlackChannel]:
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return None
    return SlackChannel(
        token=token,
        agent_loop=agent_loop,
        signing_secret=os.environ.get("SLACK_SIGNING_SECRET"),
    )
