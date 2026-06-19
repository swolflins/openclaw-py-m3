"""Telegram 渠道(Phase 7)。

实现:Long Polling + Bot API(纯 httpx,无第三方 SDK)。
文档:https://core.telegram.org/bots/api

依赖:无(只用 httpx)
环境变量: TELEGRAM_BOT_TOKEN
启动方式:
    from openclaw.channels.telegram import TelegramChannel
    ch = TelegramChannel(token="123456:ABC", agent_loop=loop)
    await ch.start()
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional

import httpx

from openclaw.agent.loop import AgentLoop
from openclaw.channels.base import BaseChannel, IncomingMessage
from openclaw.core.logging import get_logger

logger = get_logger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramChannel(BaseChannel):
    name = "telegram"

    def __init__(
        self,
        token: str,
        agent_loop: AgentLoop,
        *,
        api_base: str = "https://api.telegram.org",
        poll_timeout: int = 30,
        allowed_updates: Optional[list[str]] = None,
    ) -> None:
        super().__init__(agent_loop)
        self.token = token
        self.api_base = api_base.rstrip("/")
        self.poll_timeout = int(poll_timeout)
        self.allowed_updates = allowed_updates or ["message", "edited_message"]
        self._client: Optional[httpx.AsyncClient] = None
        self._offset: int = 0
        self._task: Optional[asyncio.Task] = None

    @property
    def available(self) -> bool:
        return bool(self.token)

    # ---------- 公共 API ----------

    async def call(self, method: str, **params: Any) -> dict[str, Any]:
        """通用 Bot API 调用,带 ok 校验。"""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.poll_timeout + 10)
        url = f"{self.api_base}/bot{self.token}/{method}"
        r = await self._client.post(url, json=params)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method} failed: {data}")
        return data["result"]

    async def start(self) -> None:
        if not self.available:
            raise RuntimeError("Telegram 凭据未配置 (TELEGRAM_BOT_TOKEN)")
        # 先确认 bot 身份
        try:
            me = await self.call("getMe")
            logger.info("telegram_getme", username=me.get("username"), id=me.get("id"))
        except Exception:
            logger.exception("telegram getMe failed")
            raise
        self._task = asyncio.create_task(self._poll_loop(), name="telegram-poll")
        await self._stopped.wait()

    async def stop(self) -> None:
        self._stopped.set()
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=3)
            except asyncio.TimeoutError:
                self._task.cancel()

    async def send(self, session_id: str, text: str) -> None:
        # session_id 格式: telegram:<chat_id>
        chat_id = session_id.split(":", 1)[1] if ":" in session_id else session_id
        # 切 4000 字符(Telegram 单条上限)
        for i in range(0, len(text), 4000):
            chunk = text[i:i + 4000]
            try:
                await self.call("sendMessage", chat_id=chat_id, text=chunk)
            except Exception:
                logger.exception("telegram sendMessage failed")
                raise

    # ---------- 内部 ----------

    async def _poll_loop(self) -> None:
        backoff = 1
        while not self._stopped.is_set():
            try:
                updates = await self.call(
                    "getUpdates",
                    offset=self._offset,
                    timeout=self.poll_timeout,
                    allowed_updates=self.allowed_updates,
                )
                if updates:
                    for upd in updates:
                        # 单条失败不阻塞整体
                        try:
                            await self._handle_update(upd)
                        except Exception:
                            logger.exception("handle telegram update failed", update_id=upd.get("update_id"))
                        self._offset = max(self._offset, upd["update_id"] + 1)
                    backoff = 1
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("telegram poll error; backoff=%ds", backoff)
                await asyncio.sleep(min(backoff, 30))
                backoff *= 2
            else:
                # 短间隔防止 busy loop
                if not updates:
                    await asyncio.sleep(0.1)

    async def _handle_update(self, upd: dict[str, Any]) -> None:
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            return
        chat = msg.get("chat") or {}
        from_ = msg.get("from") or {}
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if not text:
            return
        chat_id = chat.get("id")
        user_id = from_.get("id")
        if chat_id is None or user_id is None:
            return
        if from_.get("is_bot"):
            return  # 跳过 bot 自己的消息
        is_dm = chat.get("type") == "private"
        await self.dispatch(IncomingMessage(
            channel=self.name,
            session_id=f"telegram:{chat_id}",
            user_id=str(user_id),
            text=text,
            raw=msg,
            metadata={
                "is_dm": is_dm,
                "mentioned": False,  # 简化:群里只接 @bot 后续再做
                "chat_id": str(chat_id),
                "username": from_.get("username"),
                "first_name": from_.get("first_name"),
                "message_id": msg.get("message_id"),
            },
        ))


# ---------------- CLI helper ----------------

def from_env(agent_loop: AgentLoop) -> Optional[TelegramChannel]:
    import os
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    return TelegramChannel(token=token, agent_loop=agent_loop)
