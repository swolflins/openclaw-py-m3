"""WhatsApp Cloud API 渠道(Phase 7)。

入站:Webhook(FastAPI 路由 → ingest_webhook())
出站:Cloud API(https://graph.facebook.com/v17.0/<PHONE_ID>/messages)

环境变量:
    WHATSAPP_TOKEN          long-lived access token from Meta Business
    WHATSAPP_PHONE_ID       phone number ID
    WHATSAPP_VERIFY_TOKEN   webhook 握手时校验
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

import httpx

from openclaw.agent.loop import AgentLoop
from openclaw.channels.base import BaseChannel, IncomingMessage
from openclaw.core.logging import get_logger

logger = get_logger(__name__)

API = "https://graph.facebook.com/v17.0"


class WhatsAppChannel(BaseChannel):
    name = "whatsapp"

    def __init__(
        self,
        token: str,
        phone_id: str,
        agent_loop: AgentLoop,
        *,
        verify_token: Optional[str] = None,
    ) -> None:
        super().__init__(agent_loop)
        self.token = token
        self.phone_id = phone_id
        self.verify_token = verify_token
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def available(self) -> bool:
        return bool(self.token) and bool(self.phone_id)

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
            raise RuntimeError("WhatsApp 凭据未配置 (WHATSAPP_TOKEN / WHATSAPP_PHONE_ID)")
        logger.info("whatsapp_channel_running_in_webhook_mode", phone_id=self.phone_id)
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
        # session_id: whatsapp:<phone_number>
        to = session_id.split(":", 1)[1] if ":" in session_id else session_id
        client = await self._get_client()
        # 切 4096 字符
        for i in range(0, len(text), 4096):
            chunk = text[i:i + 4096]
            r = await client.post(
                f"/{self.phone_id}/messages",
                json={
                    "messaging_product": "whatsapp",
                    "to": to,
                    "type": "text",
                    "text": {"body": chunk, "preview_url": False},
                },
            )
            if r.status_code >= 400:
                logger.warning("whatsapp_send_failed", status=r.status_code, body=r.text[:200])

    # ---------- Webhook ----------

    def verify_webhook(self, mode: str, token: str, challenge: str) -> Optional[str]:
        if mode == "subscribe" and token == self.verify_token:
            return challenge
        return None

    async def ingest_webhook(self, payload: dict[str, Any]) -> None:
        entries = payload.get("entry") or []
        for entry in entries:
            for change in entry.get("changes") or []:
                if change.get("field") != "messages":
                    continue
                value = change.get("value") or {}
                contacts = value.get("contacts") or []
                wa_id = contacts[0].get("wa_id") if contacts else None
                for msg in value.get("messages") or []:
                    if msg.get("type") != "text":
                        continue
                    text = (msg.get("text") or {}).get("body", "").strip()
                    from_ = msg.get("from")
                    if not text or not from_:
                        continue
                    await self.dispatch(IncomingMessage(
                        channel=self.name,
                        session_id=f"whatsapp:{from_}",
                        user_id=str(wa_id or from_),
                        text=text,
                        raw=msg,
                        metadata={
                            "is_dm": True,           # Cloud API 没有"群"概念(企业版)
                            "mentioned": True,       # 私聊总是 addressed
                            "phone": from_,
                            "message_id": msg.get("id"),
                        },
                    ))


def from_env(agent_loop: AgentLoop) -> Optional[WhatsAppChannel]:
    token = os.environ.get("WHATSAPP_TOKEN")
    phone = os.environ.get("WHATSAPP_PHONE_ID")
    if not token or not phone:
        return None
    return WhatsAppChannel(
        token=token,
        phone_id=phone,
        agent_loop=agent_loop,
        verify_token=os.environ.get("WHATSAPP_VERIFY_TOKEN"),
    )
