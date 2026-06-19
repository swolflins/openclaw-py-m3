"""iMessage 渠道(Phase 7)—— macOS-only 占位。

真实实现两条路径:
1. BlueBubbles(macOS 上,开 HTTP API): 类似 whatsapp,webhook 入站
2. AppleScript 桥接 Messages.app: 用 subprocess 调 osascript
3. pyobjc 直接调 Messages.framework(macOS only,需 pip install pyobjc-framework-Messages)

本模块只提供 BlueBubbles 兼容的 webhook + send 实现,不依赖 macOS 平台特性,
跨平台 import 不报错;在非 mac 上 start() 给出清晰提示。
"""
from __future__ import annotations

import os
import platform
from typing import Any, Optional

import httpx

from openclaw.agent.loop import AgentLoop
from openclaw.channels.base import BaseChannel, IncomingMessage
from openclaw.core.logging import get_logger

logger = get_logger(__name__)


class IMessageChannel(BaseChannel):
    name = "imessage"

    def __init__(
        self,
        agent_loop: AgentLoop,
        *,
        bluebubbles_url: Optional[str] = None,
        bluebubbles_password: Optional[str] = None,
    ) -> None:
        super().__init__(agent_loop)
        self.bluebubbles_url = bluebubbles_url
        self.bluebubbles_password = bluebubbles_password
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def available(self) -> bool:
        return bool(self.bluebubbles_url)

    async def start(self) -> None:
        if not self.available:
            if platform.system() != "Darwin":
                raise RuntimeError(
                    "iMessage 暂未配置;若在 macOS 上,启动 BlueBubbles server "
                    "并设置 BLUEBUBBLES_URL/ BLUEBUBBLES_PASSWORD"
                )
            raise RuntimeError("iMessage 未配置 (BLUEBUBBLES_URL)")
        logger.info("imessage_running_in_webhook_mode", url=self.bluebubbles_url)
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
        # session_id: imessage:<chat_guid or handle>
        target = session_id.split(":", 1)[1] if ":" in session_id else session_id
        if not self.bluebubbles_url:
            return
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.bluebubbles_url,
                timeout=20,
                params={"password": self.bluebubbles_password} if self.bluebubbles_password else None,
            )
        try:
            # BlueBubbles API:POST /api/v1/message/text
            await self._client.post(
                "/api/v1/message/text",
                json={"chatGuid": target, "tempGuid": "openclaw", "text": text},
            )
        except Exception:
            logger.exception("imessage send failed")

    # ---------- Webhook ----------

    async def ingest_webhook(self, payload: dict[str, Any]) -> None:
        """BlueBubbles 推过来的事件结构(简化):
            {"type": "new-message", "data": {"text": ..., "chats": [{"chatGuid": ...}], "handle": {"id": ...}}}
        """
        if payload.get("type") != "new-message":
            return
        data = payload.get("data") or {}
        text = (data.get("text") or "").strip()
        if not text:
            return
        chats = data.get("chats") or [{}]
        chat_guid = chats[0].get("chatGuid")
        handle = (data.get("handle") or {}).get("address")
        if not chat_guid:
            return
        await self.dispatch(IncomingMessage(
            channel=self.name,
            session_id=f"imessage:{chat_guid}",
            user_id=str(handle or "unknown"),
            text=text,
            raw=payload,
            metadata={
                "is_dm": len(chats) == 1 and (chats[0].get("participants") or [None])[:1] == [handle],
                "mentioned": True,
                "chat_guid": chat_guid,
                "handle": handle,
            },
        ))


def from_env(agent_loop: AgentLoop) -> Optional[IMessageChannel]:
    url = os.environ.get("BLUEBUBBLES_URL")
    if not url:
        return None
    return IMessageChannel(
        agent_loop=agent_loop,
        bluebubbles_url=url,
        bluebubbles_password=os.environ.get("BLUEBUBBLES_PASSWORD"),
    )
