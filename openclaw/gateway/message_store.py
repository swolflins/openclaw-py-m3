"""Gateway 层的消息元数据存储(线程关联 / "1 条回复" 效果用)。

跟 `openclaw.llm.ChatMessage` / `ScopedMemory` 严格解耦:
- ChatMessage 是给 LLM 用的 schema(无 id、无 parent)
- ScopedMemory 存的是 LLM 推理所需的历史
- **MessageStore 存的是「UI 渲染用」的消息元数据**:
  msg_id → {role, content, parent_id, created_at, session_id, iterations, tool_calls_count}

为什么单独搞:
- 不动 ChatMessage 避免破坏 LLM 客户端接口
- 不动 ScopedMemory(那是 SQLite-backed,加字段要迁移)
- 这个只服务 Gateway API,内存级,重启丢,跟 "thread UI 状态" 一个生命周期

设计要点(Phase 23):
- 线程模型:一条 user 消息是 thread 根,后续 assistant/user 消息是 reply
  - reply 通过 `parent_id` 关联到父消息(不是 root_id)
  - 这样可以"回复 assistant 消息"形成 nested thread(飞书实际也是这么做的)
- 容量上限:每个 session 最多 5000 条,LRU 淘汰;避免内存无限增长
- msg_id 用 uuid4 hex(12 字符),跟现有 request_id 风格一致
- 线程安全:asyncio.Lock 保护 dict 读写
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class StoredMessage:
    """UI 渲染用消息元数据(跟 LLM 推理解耦)。"""
    msg_id: str
    session_id: str
    role: str  # "user" | "assistant" | "system"
    content: str
    parent_id: Optional[str] = None  # 指向它要 reply 的消息
    created_at: float = field(default_factory=time.time)
    iterations: int = 0  # assistant 消息:agent 跑了多少轮
    tool_calls_count: int = 0  # assistant 消息:调用了几个工具

    def to_dict(self) -> dict[str, Any]:
        return {
            "msg_id": self.msg_id,
            "session_id": self.session_id,
            "role": self.role,
            "content": self.content,
            "parent_id": self.parent_id,
            "created_at": self.created_at,
            "iterations": self.iterations,
            "tool_calls_count": self.tool_calls_count,
        }


class MessageStore:
    """线程安全的内存消息元数据存储,按 session_id 分组。

    用途:
    - 存 user/assistant 消息 → 返回 msg_id 给 client
    - client 用 reply_to_id(= 父消息 msg_id)指明"我的消息是 reply 哪条"
    - client 按 parent_id 反查原文(实现"1 条回复"展开效果)
    """

    DEFAULT_MAX_PER_SESSION = 5000

    def __init__(self, max_per_session: int = DEFAULT_MAX_PER_SESSION) -> None:
        self._max = max_per_session
        # session_id → ordered list of msg_id(实现 LRU + 顺序遍历)
        self._by_session: dict[str, list[str]] = {}
        # msg_id → StoredMessage
        self._by_id: dict[str, StoredMessage] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def new_id() -> str:
        """12 字符 hex msg_id(跟 request_id 风格一致)。"""
        return uuid.uuid4().hex[:12]

    async def add(
        self,
        session_id: str,
        role: str,
        content: str,
        parent_id: Optional[str] = None,
        iterations: int = 0,
        tool_calls_count: int = 0,
        msg_id: Optional[str] = None,
    ) -> StoredMessage:
        """存一条消息 → 返回 StoredMessage(含 msg_id)。"""
        async with self._lock:
            mid = msg_id or self.new_id()
            sm = StoredMessage(
                msg_id=mid,
                session_id=session_id,
                role=role,
                content=content,
                parent_id=parent_id,
                iterations=iterations,
                tool_calls_count=tool_calls_count,
            )
            self._by_id[mid] = sm
            bucket = self._by_session.setdefault(session_id, [])
            bucket.append(mid)
            # LRU 淘汰:超过上限砍头部
            while len(bucket) > self._max:
                old = bucket.pop(0)
                self._by_id.pop(old, None)
            return sm

    async def get(self, msg_id: str) -> Optional[StoredMessage]:
        async with self._lock:
            return self._by_id.get(msg_id)

    async def get_in_session(self, session_id: str, msg_id: str) -> Optional[StoredMessage]:
        """查一条消息,但限定在同一 session(防跨 session 引用)。"""
        async with self._lock:
            sm = self._by_id.get(msg_id)
            if sm is None or sm.session_id != session_id:
                return None
            return sm

    async def list_session(self, session_id: str, k: int = 50) -> list[StoredMessage]:
        """列一个 session 最近 k 条(倒序 → 返回时反转让最新在前)。"""
        async with self._lock:
            ids = list(self._by_session.get(session_id, []))
            tail = ids[-k:][::-1]
            return [self._by_id[i] for i in tail if i in self._by_id]

    async def count_replies(self, session_id: str, parent_id: str) -> int:
        """数某条 parent 消息被 reply 了几次(飞书"1 条回复"那个数字)。"""
        async with self._lock:
            return sum(
                1
                for m in self._by_id.values()
                if m.session_id == session_id and m.parent_id == parent_id
            )

    async def clear_session(self, session_id: str) -> int:
        """清一个 session 的全部消息(配合 /v1/sessions DELETE)。"""
        async with self._lock:
            ids = self._by_session.pop(session_id, [])
            for i in ids:
                self._by_id.pop(i, None)
            return len(ids)

    def stats(self) -> dict[str, int]:
        return {
            "total_sessions": len(self._by_session),
            "total_messages": len(self._by_id),
        }
