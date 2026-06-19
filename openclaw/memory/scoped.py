"""Scoped 统一记忆访问。

scope 格式: '{kind}:{id}'   例如 'session:abc' / 'user:u1' / 'channel:lark:chat1'

为调用方提供:
- 短期 turns (short_term)
- 长期向量 (long_term, 可选,不存在时跳过)
- 一站式 retrieve:把长期 + 短期 + SOUL 拼成 messages
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from openclaw.llm.base import ChatMessage
from openclaw.memory.long_term import LongTermStore, MemoryItem
from openclaw.memory.short_term import ShortTermStore
from openclaw.memory.soul import SoulLoader
from openclaw.core.logging import get_logger

logger = get_logger(__name__)


@dataclass
class ScopeKey:
    kind: str  # session / user / channel / global
    id: str

    def __str__(self) -> str:
        return f"{self.kind}:{self.id}"

    @classmethod
    def parse(cls, s: str) -> "ScopeKey":
        if ":" in s:
            kind, _, sid = s.partition(":")
            return cls(kind=kind, id=sid)
        return cls(kind="session", id=s)


class ScopedMemory:
    """一个 scope 的统一门面。"""

    def __init__(
        self,
        short_term: ShortTermStore,
        long_term: Optional[LongTermStore] = None,
        soul: Optional[SoulLoader] = None,
    ) -> None:
        self.short = short_term
        self.long = long_term
        self.soul = soul

    # --- 写入 ---

    async def append_turn(
        self, scope: str, user: str, assistant: str, metadata: dict | None = None
    ) -> None:
        self.short.append(scope, user, assistant, metadata=metadata)
        # 长期记忆:把 assistant 回复(知识性内容)写入
        if self.long is not None and assistant and len(assistant) >= 20:
            try:
                self.long.add(assistant, scope=scope, metadata={"source": "assistant"})
            except Exception:
                logger.exception("long_term_add_failed")

    # --- 读取 ---

    def recent_messages(self, scope: str, k: int = 20) -> list[ChatMessage]:
        return self.short.recent(scope, k=k)

    def recall(self, scope: str, query: str, top_k: int = 5) -> list[MemoryItem]:
        if self.long is None:
            return []
        try:
            return self.long.query(query, scope=scope, top_k=top_k)
        except Exception:
            logger.exception("long_term_query_failed")
            return []

    def render_system_prompt(self, base: str = "") -> str:
        if self.soul is None:
            return base
        return self.soul.render_system_prompt(base)

    # --- 一站式:组装给 LLM 的 messages ---

    def build_messages(
        self,
        scope: str,
        user_message: str,
        *,
        system_prompt: str = "",
        history_window: int = 20,
        recall_top_k: int = 3,
    ) -> list[ChatMessage]:
        """拼装: [soul-augmented-system, recalled-context, history..., user]。"""
        system = self.render_system_prompt(base=system_prompt)

        msgs: list[ChatMessage] = [ChatMessage(role="system", content=system)]

        # 长期记忆检索 -> 拼成 system reminder(放在 system 之后)
        recalled = self.recall(scope, user_message, top_k=recall_top_k)
        if recalled:
            lines = ["以下是与用户当前问题相关的历史记忆,请参考回答(不要直接复读):"]
            for i, item in enumerate(recalled, 1):
                snippet = item.text[:300]
                lines.append(f"{i}. {snippet}")
            msgs.append(
                ChatMessage(role="system", content="\n".join(lines))
            )

        # 短期历史
        msgs.extend(self.short.recent(scope, k=history_window))
        # 当前问题
        msgs.append(ChatMessage(role="user", content=user_message))
        return msgs
