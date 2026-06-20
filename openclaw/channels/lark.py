"""飞书 (Lark) 消息渠道。

默认走长连接(WebSocket),无需公网 IP 即可接收消息;
如果 lark-oapi 不可用或没装,该模块退化为「占位实现」,只 import 不报错,
由 CLI 入口在启动前检测并提示。

依赖: pip install lark-oapi
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from openclaw.agent.loop import AgentLoop
from openclaw.channels.base import BaseChannel
from openclaw.config.settings import LarkSettings

logger = logging.getLogger(__name__)


def _is_bot_mentioned(mentions: Optional[list], bot_open_id: Optional[str]) -> bool:
    """CH-1:判断飞书事件里是否 @ 了 bot。

    mentions 是 lark_oapi MentionEvent 列表,任一满足:
    - mentioned_type == "bot"(明确是 bot)
    - id.open_id 等于 bot 自己的 open_id
    即认为被 @。
    """
    if not mentions:
        return False
    for m in mentions:
        try:
            if getattr(m, "mentioned_type", None) == "bot":
                return True
            if bot_open_id and getattr(getattr(m, "id", None), "open_id", None) == bot_open_id:
                return True
        except Exception:
            continue
    return False

try:  # 飞书 SDK 可选依赖
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import (
        P2ImMessageReceiveV1,
    )

    _HAS_LARK = True
except Exception:  # pragma: no cover - 兼容未装 SDK
    lark = None  # type: ignore[assignment]
    _HAS_LARK = False


class LarkChannel(BaseChannel):
    """飞书自建应用消息渠道(长连接)。"""

    name = "lark"

    def __init__(self, agent_loop: AgentLoop, settings: LarkSettings) -> None:
        super().__init__(agent_loop)
        self.settings = settings
        self._ws_client: Optional[Any] = None
        self._stopped = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        # session_id → 最近一条 message_id,send() 用它 reply 原消息
        self._last_msg_id: dict[str, str] = {}

    # ---------- 公共接口 ----------

    @property
    def available(self) -> bool:
        return _HAS_LARK and bool(self.settings.app_id) and bool(self.settings.app_secret)

    async def start(self) -> None:
        if not _HAS_LARK:
            raise RuntimeError(
                "lark-oapi 未安装,无法启动飞书渠道。请先 `pip install lark-oapi`"
            )
        if not self.available:
            raise RuntimeError("飞书凭据未配置 (LARK_APP_ID / LARK_APP_SECRET)")

        if not self.settings.use_ws:
            raise NotImplementedError(
                "Webhook 模式尚未实现,当前仅支持长连接模式(LARK_USE_WS=true)。"
            )

        # 在后台线程跑 lark WS,避免阻塞 asyncio loop
        self._task = asyncio.create_task(self._ws_loop())
        logger.info("Lark WS 渠道已启动,等待消息...")
        await self._stopped.wait()

    async def stop(self) -> None:
        self._stopped.set()
        if self._ws_client is not None:
            try:
                self._ws_client.stop()
            except Exception:
                pass
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=3)
            except asyncio.TimeoutError:
                self._task.cancel()

    async def send(self, session_id: str, text: str) -> None:
        """主动给 session_id 发送消息(默认 reply 原消息)。"""
        if not self.available:
            logger.warning("Lark 未配置,无法发送消息")
            return
        if not text:
            return
        message_id = self._last_msg_id.get(session_id, "")
        if not message_id:
            logger.warning(
                "Lark send 失败:session 没有对应 message_id(可能从 WS 收的消息),"
                "session=%s text=%r", session_id, text[:60],
            )
            return
        await self._reply_to_lark(message_id, text)

    def _fetch_bot_open_id(self) -> Optional[str]:
        """CH-1:拉一次 bot 自己的 open_id(缓存到 self._bot_open_id)。

        用 im/v1/bot_info 接口。失败返回 None(不致命 — 群 @ 检测会回退到 mentioned_type)。
        """
        if getattr(self, "_bot_open_id", None):
            return self._bot_open_id
        try:
            import httpx
            import asyncio
            token = asyncio.run(self._get_tenant_token())
            if not token:
                return None
            r = httpx.get(
                "https://open.feishu.cn/open-apis/im/v1/bots/me",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            data = r.json()
            bot = data.get("data", {}).get("bot", {})
            open_id = bot.get("open_id")
            if open_id:
                self._bot_open_id = open_id
                logger.info("Lark bot open_id 缓存: %s", open_id)
            return open_id
        except Exception:
            logger.exception("fetch bot open_id failed")
            return None

    # ---------- 内部实现 ----------

    async def _ws_loop(self) -> None:
        """在独立线程跑飞书 WS 客户端,带崩溃重连(REL-1)。

        旧实现:ws_client.start() 一旦抛异常,_ws_loop 退出,channel 永远静默。
        新实现:
        - 用外层 try/except 包住 start()
        - 退避重连:1s, 2s, 4s, 8s, 16s, 30s(上限)
        - 收到 _stopped 后干净退出
        - 连续 N 次失败后停止重连(让上层可以重启)
        """
        loop = asyncio.get_running_loop()
        backoffs = [1, 2, 4, 8, 16, 30]
        max_attempts = 12  # 约 2 分钟后停止重连(让运维介入)
        attempt = 0

        def _on_message(data: Any) -> None:
            try:
                evt = P2ImMessageReceiveV1.model_validate(data)
                asyncio.run_coroutine_threadsafe(self._handle_event(evt), loop)
            except Exception:
                logger.exception("解析飞书事件失败")

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(_on_message)
            .build()
        )

        while not self._stopped.is_set():
            self._ws_client = lark.ws.Client(
                self.settings.app_id,
                self.settings.app_secret,
                event_handler=handler,
                log_level=lark.LogLevel.INFO,
            )
            try:
                # 阻塞;期间 _stopped.set() 后 ws_client.stop() 会让 start() 返回
                await loop.run_in_executor(None, self._ws_client.start)
                # 正常退出(被 stop())→ 不重连
                if self._stopped.is_set():
                    return
                # 否则可能是意外退出
                logger.warning("Lark WS 客户端意外退出,准备重连")
            except Exception:
                logger.exception("Lark WS 崩溃,准备重连")
            finally:
                self._ws_client = None

            attempt += 1
            if attempt > max_attempts:
                logger.error(
                    "Lark WS 重连超上限(%d 次),停止重连(请人工检查凭据 / 网络)",
                    max_attempts,
                )
                return

            delay = backoffs[min(attempt - 1, len(backoffs) - 1)]
            logger.info("Lark WS 第 %d 次重连,等 %ds", attempt, delay)
            # 可中断 sleep
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=delay)
                return  # 期间被 stop()
            except asyncio.TimeoutError:
                pass

    async def _handle_event(self, evt: Any) -> None:
        """处理一条飞书消息事件。"""
        try:
            sender = evt.event.sender
            msg = evt.event.message
            chat_id = msg.chat_id
            open_id = sender.sender_id.open_id if sender and sender.sender_id else "unknown"
            text = self._extract_text(msg)
            if not text:
                return

            from openclaw.channels.base import IncomingMessage
            session_id = f"lark:{chat_id}:{open_id}"
            message_id = getattr(msg, "message_id", "")
            if message_id:
                # send() 内部用这个 reply 原消息
                self._last_msg_id[session_id] = message_id
            # CH-1:解析 mentions;若 mention 列表里有 bot 自己的 open_id 则 mentioned=True
            is_dm = (getattr(msg, "chat_type", "") == "p2p")
            bot_open_id = getattr(self, "_bot_open_id", None) or self._fetch_bot_open_id()
            mentioned = _is_bot_mentioned(getattr(msg, "mentions", None), bot_open_id)
            # 走统一管道(经过 AutoReply)
            await self.dispatch(IncomingMessage(
                channel=self.name,
                session_id=session_id,
                user_id=open_id,
                text=text,
                raw=msg,
                metadata={
                    "is_dm": is_dm,
                    "mentioned": mentioned,
                    "chat_id": chat_id,
                    "message_id": message_id,
                },
            ))
        except Exception:
            logger.exception("处理飞书事件失败")

    @staticmethod
    def _extract_text(msg: Any) -> str:
        """从飞书消息结构里提取纯文本,支持 text 和 post 类型。"""
        try:
            content = json.loads(msg.content or "{}")
        except json.JSONDecodeError:
            return ""
        if msg.message_type == "text":
            return (content.get("text") or "").strip()
        if msg.message_type == "post":
            # 简单提取所有 @ 用户名之外的第一段纯文本
            post = content.get("content") or [[]]
            for line in post:
                for seg in line:
                    if seg.get("tag") == "text":
                        return (seg.get("text") or "").strip()
        return ""

    async def _reply_to_lark(self, message_id: str, text: str) -> None:
        """回复一条飞书消息(用 im/v1/messages/:id/reply)。"""
        import httpx

        if not text:
            return

        token = await self._get_tenant_token()
        if not token:
            logger.error("reply 失败:拿不到 tenant_access_token")
            return

        url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reply"
        body = {
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(url, json=body, headers=headers)
                ctype = getattr(r, "headers", {}).get("content-type", "")
                if ctype.startswith("application/json"):
                    try:
                        data = r.json()
                    except Exception:
                        data = {}
                else:
                    data = {}
                if r.status_code != 200 or data.get("code", 0) != 0:
                    logger.error(
                        "reply 失败 http=%s code=%s msg=%s body=%s",
                        r.status_code, data.get("code"), data.get("msg"), data,
                    )
                else:
                    logger.info("reply 成功 message_id=%s len=%d", message_id, len(text))
        except Exception:
            logger.exception("回复飞书消息失败")

    async def _get_tenant_token(self) -> str | None:
        import httpx

        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        body = {
            "app_id": self.settings.app_id,
            "app_secret": self.settings.app_secret,
        }
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(url, json=body)
                r.raise_for_status()
                data = r.json()
                return data.get("tenant_access_token")
        except Exception:
            logger.exception("获取 tenant_access_token 失败")
            return None
