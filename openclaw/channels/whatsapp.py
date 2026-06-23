"""WhatsApp Cloud API 渠道(Phase 7)。

入站:Webhook(FastAPI 路由 → ingest_webhook())
出站:Cloud API(https://graph.facebook.com/v17.0/<PHONE_ID>/messages)

环境变量:
    WHATSAPP_TOKEN          long-lived access token from Meta Business
    WHATSAPP_PHONE_ID       phone number ID
    WHATSAPP_VERIFY_TOKEN   webhook 握手时校验
    WHATSAPP_APP_SECRET     App Secret,用于校验 X-Hub-Signature-256(C2 修复)
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
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
        app_secret: Optional[str] = None,
    ) -> None:
        super().__init__(agent_loop)
        self.token = token
        self.phone_id = phone_id
        self.verify_token = verify_token
        # C2 修复:App Secret 用于校验 Meta 推送的 X-Hub-Signature-256
        self.app_secret = app_secret
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
        # C2 修复:用 hmac.compare_digest 防时序攻击
        if mode == "subscribe" and self.verify_token and hmac.compare_digest(token, self.verify_token):
            return challenge
        return None

    async def verify_signature(self, raw_body: bytes, signature_header: str | None) -> bool:
        """C2 修复:校验 Meta 推送的 X-Hub-Signature-256。

        签名格式:``sha256=<hex>``,其中 hex = HMAC-SHA256(app_secret, raw_body)。
        - ``app_secret`` 未配置时返回 False(fail-closed,防止未验签部署)
        - ``signature_header`` 缺失或不匹配时返回 False
        """
        if not self.app_secret:
            logger.critical(
                "whatsapp_webhook_no_app_secret:WHATSAPP_APP_SECRET 未配置,"
                "webhook 验签被跳过 — 任何人可伪造消息。请设置 WHATSAPP_APP_SECRET。"
            )
            return False
        if not signature_header:
            return False
        # Meta 格式:sha256=<hex>
        if not signature_header.startswith("sha256="):
            return False

        def _verify() -> bool:
            expected = hmac.new(
                self.app_secret.encode("utf-8"), raw_body, hashlib.sha256
            ).hexdigest()
            provided = signature_header[7:]  # 去掉 "sha256=" 前缀
            return hmac.compare_digest(expected, provided)

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _verify)

    async def ingest_webhook(
        self,
        payload: dict[str, Any],
        *,
        raw_body: bytes | None = None,
        signature: str | None = None,
    ) -> None:
        # C2 修复:入口验签
        #   - app_secret 已配置:必须提供 raw_body + signature 且校验通过
        #   - app_secret 未配置:fail-closed,拒绝处理(防止伪造消息)
        #   - raw_body=None(旧调用方式/测试):若 app_secret 也未配置,记 critical
        #     警告后放行(向后兼容);若 app_secret 已配置则拒绝
        if self.app_secret:
            if raw_body is None or not await self.verify_signature(raw_body, signature):
                logger.warning("whatsapp_webhook_signature_invalid:拒绝未验签的 webhook")
                return
        else:
            logger.critical(
                "whatsapp_webhook_no_app_secret:WHATSAPP_APP_SECRET 未配置,"
                "webhook 验签被跳过 — 任何人可伪造消息。请设置 WHATSAPP_APP_SECRET。"
            )
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
        app_secret=os.environ.get("WHATSAPP_APP_SECRET"),  # C2 修复
    )
