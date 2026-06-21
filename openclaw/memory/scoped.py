"""Scoped 统一记忆访问。

scope 格式: '{kind}:{id}'   例如 'session:abc' / 'user:u1' / 'channel:lark:chat1'

为调用方提供:
- 短期 turns (short_term)
- 长期向量 (long_term, 可选,不存在时跳过)
- 一站式 retrieve:把长期 + 短期 + SOUL 拼成 messages

**RT-1 修复**:所有 SQLite / ChromaDB / 文件 IO 都通过
``asyncio.to_thread`` 包装,绝不阻塞事件循环。

**Phase 25 / b10 修复**:长期记忆的写入和读出都过 sanitize,防双向 prompt-injection:
- 写入时:防投毒(恶意 assistant reply 落库后污染后续召回)
- 读出时:防召回内容里嵌入 "ignore previous instructions" 等指令覆写
- 净化 = ``strip_prompt_injection(strip_external_content(text))``:
  - ``strip_external_content`` 负责 HTML / 特殊 token / 零宽字符
  - ``strip_prompt_injection`` 负责主动删除已知注入模式
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Optional

from openclaw.core.sanitize import strip_external_content, strip_prompt_injection
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
        # RT-1: 同步 SQLite IO 不能直接调,用 to_thread 包到线程池
        await asyncio.to_thread(
            self.short.append, scope, user, assistant, metadata=metadata
        )
        # 长期记忆:把 assistant 回复(知识性内容)写入
        # Phase 25 / b10:写入前 sanitize,防投毒落库
        # 既过 ``strip_external_content``(HTML/特殊 token/零宽),也过
        # ``strip_prompt_injection``(主动删 "ignore previous instructions" 等)
        if self.long is not None and assistant and len(assistant) >= 20:
            try:
                safe_text = strip_prompt_injection(strip_external_content(assistant))
                if not safe_text or not safe_text.strip():
                    # 净化后空 → 跳过(避免写入空串)
                    return
                # RT-1 + NEW-2: 同步 ChromaDB IO 也走 to_thread
                await asyncio.to_thread(
                    self.long.add,
                    safe_text,
                    scope=scope,
                    metadata={"source": "assistant"},
                )
            except Exception:
                logger.exception("long_term_add_failed")

    # --- 读取 ---

    async def recent_messages(self, scope: str, k: int = 20) -> list[ChatMessage]:
        # RT-1: 同步 IO 走 to_thread
        return await asyncio.to_thread(self.short.recent, scope, k=k)

    async def recall(self, scope: str, query: str, top_k: int = 5) -> list[MemoryItem]:
        if self.long is None:
            return []
        try:
            # RT-1: ChromaDB query 同步 → 异步
            items = await asyncio.to_thread(
                self.long.query, query, scope=scope, top_k=top_k
            )
            # Phase 25 / b10:读出时 sanitize,防召回内容里嵌 prompt injection
            sanitized: list[MemoryItem] = []
            for it in items:
                # 既过 ``strip_external_content``(HTML/特殊 token/零宽),也过
                # ``strip_prompt_injection``(主动删 "ignore previous instructions" 等)
                safe = strip_prompt_injection(strip_external_content(it.text or ""))
                if safe != it.text:
                    # 文本被净化过 → 记录日志(便于审计)
                    logger.info(
                        "long_term_recall_sanitized",
                        id=it.id,
                        orig_len=len(it.text or ""),
                        new_len=len(safe),
                    )
                # 用 replace 产生新对象,保留原 id / metadata / distance
                sanitized.append(replace(it, text=safe))
            return sanitized
        except Exception:
            logger.exception("long_term_query_failed")
            return []

    def render_system_prompt(self, base: str = "") -> str:
        if self.soul is None:
            return base
        return self.soul.render_system_prompt(base)

    # --- 一站式:组装给 LLM 的 messages ---

    async def build_messages(
        self,
        scope: str,
        user_message: str,
        *,
        system_prompt: str = "",
        history_window: int = 20,
        recall_top_k: int = 3,
    ) -> list[ChatMessage]:
        """拼装: [soul-augmented-system, recalled-context, history..., user]。

        **RT-1 修复**:此方法已改 async,所有底层 IO 异步化。
        **Phase 25 / b10 修复**:recalled 内容已在 ``recall()`` 中过 sanitize。
        """
        system = self.render_system_prompt(base=system_prompt)

        msgs: list[ChatMessage] = [ChatMessage(role="system", content=system)]

        # 长期记忆检索(异步)— 拼成 system reminder
        recalled = await self.recall(scope, user_message, top_k=recall_top_k)
        if recalled:
            lines = ["以下是与用户当前问题相关的历史记忆,请参考回答(不要直接复读):"]
            for i, item in enumerate(recalled, 1):
                snippet = item.text[:300]
                lines.append(f"{i}. {snippet}")
            msgs.append(
                ChatMessage(role="system", content="\n".join(lines))
            )

        # 短期历史(异步)
        history = await self.recent_messages(scope, k=history_window)
        msgs.extend(history)
        # 当前问题
        msgs.append(ChatMessage(role="user", content=user_message))
        return msgs
