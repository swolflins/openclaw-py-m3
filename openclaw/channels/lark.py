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

try:  # 飞书 SDK 可选依赖
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import (
        P2ImMessageReceiveV1,
    )
    from lark_oapi.event.callback.model.p2.card_action_trigger import (
        P2CardActionTrigger,  # noqa: F401
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
        """主动给 session_id 发送消息。
        session_id 格式: 'oc_xxx:chat_id' (p2p) 或 'chat_id' (群)
        """
        if not self.available:
            logger.warning("Lark 未配置,无法发送消息")
            return
        # 这里需要根据实际 chat_id / open_chat_id 调用 im/v1/messages 创建
        # 为了 MVP 简洁,仅记录日志;真实发送由 on_message 内回复流程完成
        logger.info("[lark -> %s] %s", session_id, text[:200])

    # ---------- 内部实现 ----------

    async def _ws_loop(self) -> None:
        """在独立线程跑飞书 WS 客户端。"""
        loop = asyncio.get_running_loop()

        def _on_message(data: Any) -> None:
            try:
                evt = P2ImMessageReceiveV1.model_validate(data)
                asyncio.run_coroutine_threadsafe(self._handle_event(evt), loop)
            except Exception:
                logger.exception("解析飞书事件失败")

        # 构造事件处理器
        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(_on_message)
            .build()
        )

        self._ws_client = lark.ws.Client(
            self.settings.app_id,
            self.settings.app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
        )

        # 启动(阻塞)
        await loop.run_in_executor(None, self._ws_client.start)

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
            # 走统一管道(经过 AutoReply)
            await self.dispatch(IncomingMessage(
                channel=self.name,
                session_id=session_id,
                user_id=open_id,
                text=text,
                raw=msg,
                metadata={
                    "is_dm": getattr(msg, "chat_type", "") == "p2p",
                    "mentioned": False,  # 飞书未解析 mentions
                    "chat_id": chat_id,
                    "message_id": getattr(msg, "message_id", ""),
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
        """回复一条飞书消息(用 im/v1/messages create, mention 关联原消息)。"""
        import httpx

        if not text:
            return

        token = await self._get_tenant_token()
        if not token:
            return

        url = "https://open.feishu.cn/open-apis/im/v1/messages"
        params = {"receive_id_type": "chat_id"}
        body = {
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }

        async with httpx.AsyncClient(timeout=15) as client:
            # 通过 message_id 反查 chat_id
            try:
                # MVP 简化:不查原 chat_id,直接用回复接口
                await client.post(
                    f"{url}/{message_id}/reply",
                    params=params,
                    json=body,
                    headers=headers,
                )
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
