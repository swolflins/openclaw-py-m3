"""渠道抽象 + 统一管道(Phase 7)。

所有渠道(CLI / 飞书 / Telegram / Discord / Slack / WhatsApp / Signal / iMessage)
都实现相同的接口,把消息归一为 IncomingMessage,经过 AutoReply 决策,再交给
AgentLoop 处理。ChannelManager 负责协调多个 channel 并行运行。
"""
from __future__ import annotations

import abc
import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from openclaw.agent.loop import AgentLoop
from openclaw.core.auto_reply import AutoReplyManager
from openclaw.core.logging import get_logger

logger = get_logger(__name__)


# ---------------- 入站信封 ----------------

@dataclass
class IncomingMessage:
    """一条入站消息(已规范化)。"""
    channel: str                  # 渠道名: telegram / discord / slack / whatsapp / signal / imessage / cli / lark
    session_id: str               # 用作 memory key(如 telegram:12345 或 chat_id:user_id)
    user_id: str                  # 平台用户 ID
    text: str                     # 纯文本
    raw: Any = None               # 原始平台消息
    metadata: dict[str, Any] = field(default_factory=dict)
    # metadata 常见键: is_dm, mentioned, channel_name, thread_id, attachments...


ReplyCallback = Callable[[str, str], Awaitable[None]]
"""(session_id, reply_text) -> coroutine"""


# ---------------- Channel 基类 ----------------

class BaseChannel(abc.ABC):
    """所有消息渠道实现。

    子类需要:
    - 覆盖 name 类属性
    - 实现 start() / stop() / send() / recv_loop() 这 4 个钩子
    - 收到消息时调用 self.dispatch(msg) 进入统一管道
    """

    name: str = "base"

    def __init__(
        self,
        agent_loop: Optional[AgentLoop] = None,
        on_reply: Optional[ReplyCallback] = None,
        auto_reply: Optional[AutoReplyManager] = None,
    ) -> None:
        self.agent_loop = agent_loop
        self.on_reply = on_reply
        self.auto_reply = auto_reply
        self._stopped = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        # 收到的入站消息(测试 / 调试用)
        self.received: list[IncomingMessage] = []
        # 发出的回复(测试 / 调试用)
        self.replies: list[tuple[str, str]] = []

    # -------- 抽象接口 --------

    @abc.abstractmethod
    async def start(self) -> None:
        """启动渠道。子类实现应该创建 recv_loop 任务。"""

    @abc.abstractmethod
    async def stop(self) -> None:
        """停止渠道。"""

    @abc.abstractmethod
    async def send(self, session_id: str, text: str) -> None:
        """主动给某个 session 发一条消息(可选实现)。"""

    # -------- 统一管道(子类不重写) --------

    async def dispatch(self, msg: IncomingMessage) -> None:
        """收到一条入站消息后,过 AutoReply → AgentLoop → 发送。"""
        self.received.append(msg)
        # 1) Auto-Reply 决策
        if self.auto_reply is not None:
            try:
                decision = await self.auto_reply.decide(
                    user_id=msg.user_id,
                    channel=msg.channel,
                    text=msg.text,
                    metadata=msg.metadata,
                )
            except Exception:
                logger.exception("auto_reply.decide failed, fallthrough")
                decision = None
        else:
            decision = None

        # 模板命中 → 直接发
        if decision is not None and decision.reply is not None:
            await self._safe_send(msg.session_id, decision.reply)
            return

        # 被丢弃(黑名单/静默/未 @ 触发/限流)
        if decision is not None and not decision.passthrough:
            logger.info("channel_dispatch_dropped", channel=msg.channel,
                        session=msg.session_id, reason=decision.reason)
            return

        # 2) AgentLoop
        if self.agent_loop is None:
            await self._safe_send(msg.session_id, "[错误] channel 未绑定 agent_loop")
            return
        prefix = decision.prompt_prefix if decision else None
        full_text = (prefix or "") + msg.text
        try:
            resp = await self.agent_loop.handle(msg.session_id, full_text)
        except Exception as e:  # noqa: BLE001
            logger.exception("agent_loop handle failed")
            await self._safe_send(msg.session_id, f"[错误] {type(e).__name__}: {e}")
            return
        reply = resp.content or "(无回复)"
        if decision is not None and not decision.passthrough and decision.reply:
            # 上一段是兜底(decision.reply 在 passthrough=False 时被填,比如限流提示)
            reply = decision.reply + "\n" + reply
        await self._safe_send(msg.session_id, reply)

    async def _safe_send(self, session_id: str, text: str) -> None:
        self.replies.append((session_id, text))
        if self.on_reply is not None:
            try:
                if inspect.iscoroutinefunction(self.on_reply):
                    await self.on_reply(session_id, text)
                    return
            except Exception:
                logger.exception("on_reply callback failed")
        try:
            await self.send(session_id, text)
        except Exception:
            logger.exception("channel send failed", channel=self.name, session=session_id)


# ---------------- Channel Manager ----------------

class ChannelManager:
    """统一管理多个渠道,共享 agent / auto_reply。"""

    def __init__(
        self,
        agent_loop: AgentLoop,
        auto_reply: Optional[AutoReplyManager] = None,
        on_reply: Optional[ReplyCallback] = None,
    ) -> None:
        self.agent_loop = agent_loop
        self.auto_reply = auto_reply
        self.on_reply = on_reply
        self._channels: list[BaseChannel] = []
        self._stopped = asyncio.Event()

    def register(self, channel: BaseChannel) -> None:
        # 注入共享的 agent / auto_reply / on_reply(如果 channel 还没绑)
        if channel.agent_loop is None:
            channel.agent_loop = self.agent_loop
        if channel.auto_reply is None and self.auto_reply is not None:
            channel.auto_reply = self.auto_reply
        if channel.on_reply is None and self.on_reply is not None:
            channel.on_reply = self.on_reply
        self._channels.append(channel)
        logger.info("channel_registered", name=channel.name)

    def channels(self) -> list[BaseChannel]:
        return list(self._channels)

    async def start_all(self) -> None:
        """启动所有 channel,直到 stop_all() 被调用。"""
        if not self._channels:
            logger.warning("no channels registered, manager exits immediately")
            return
        tasks = [asyncio.create_task(ch.start(), name=f"ch-{ch.name}") for ch in self._channels]
        try:
            await asyncio.gather(*tasks, return_exceptions=False)
        except asyncio.CancelledError:
            pass

    async def stop_all(self) -> None:
        for ch in self._channels:
            try:
                await ch.stop()
            except Exception:
                logger.exception("stop channel failed", name=ch.name)
        self._stopped.set()


# ---------------- 一个简单的"测试 channel" ----------------

class EchoChannel(BaseChannel):
    """无外部依赖的测试 channel:接收到的消息原样回显。

    适合在 CI / 单测里验证 ChannelManager 通路。
    """

    name = "echo"

    def __init__(self, *args: Any, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self._in_queue: asyncio.Queue[IncomingMessage] = asyncio.Queue()

    async def start(self) -> None:
        # 由外部 push_incoming() 灌消息,start 只是挂着等待 stop
        await self._stopped.wait()

    async def stop(self) -> None:
        self._stopped.set()

    async def send(self, session_id: str, text: str) -> None:
        # 父类 _safe_send 已经 append 到 self.replies
        return None

    async def push_incoming(self, msg: IncomingMessage) -> None:
        await self.dispatch(msg)
