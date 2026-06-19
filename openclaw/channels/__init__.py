"""Channels 子包:消息接入渠道。

Phase 7 一次性补齐 6 个:
- CLI / 飞书(已有)
- Telegram / Discord / Slack / WhatsApp / Signal / iMessage
"""
from openclaw.channels.base import (
    BaseChannel,
    ChannelManager,
    EchoChannel,
    IncomingMessage,
    ReplyCallback,
)
from openclaw.channels.cli import CLIChannel
from openclaw.channels.lark import LarkChannel

# Phase 7 新增;每个都可能 import 失败(可选依赖),用 try/except 保护
try:
    from openclaw.channels.telegram import TelegramChannel
except Exception:  # pragma: no cover
    TelegramChannel = None  # type: ignore

try:
    from openclaw.channels.discord import DiscordChannel
except Exception:  # pragma: no cover
    DiscordChannel = None  # type: ignore

try:
    from openclaw.channels.slack import SlackChannel
except Exception:  # pragma: no cover
    SlackChannel = None  # type: ignore

try:
    from openclaw.channels.whatsapp import WhatsAppChannel
except Exception:  # pragma: no cover
    WhatsAppChannel = None  # type: ignore

try:
    from openclaw.channels.signal import SignalChannel
except Exception:  # pragma: no cover
    SignalChannel = None  # type: ignore

try:
    from openclaw.channels.imessage import IMessageChannel
except Exception:  # pragma: no cover
    IMessageChannel = None  # type: ignore


__all__ = [
    "BaseChannel",
    "ChannelManager",
    "EchoChannel",
    "IncomingMessage",
    "ReplyCallback",
    "CLIChannel",
    "LarkChannel",
    "TelegramChannel",
    "DiscordChannel",
    "SlackChannel",
    "WhatsAppChannel",
    "SignalChannel",
    "IMessageChannel",
]
