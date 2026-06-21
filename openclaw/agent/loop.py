"""核心 Agent 循环(Phase 3 集成版)。

- 系统提示 = 配置 system_prompt + SOUL 文档
- 消息 = soul-augmented system + recalled context + 短期 history + user
- 完成后写回短期(per session_id),把 assistant 回复索引到长期
- 完成时自动写一份 AgentJournal(自我反思) — Idea #5

RT-4:支持历史窗口裁剪(防 token 爆)。
"""
from __future__ import annotations

import asyncio
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from openclaw.core.logging import get_logger
from openclaw.core.sanitize import strip_external_content
from openclaw.llm.base import BaseLLMProvider, ChatMessage, ToolCall
from openclaw.memory.scoped import ScopedMemory
from openclaw.tools.registry import ToolRegistry

logger = get_logger(__name__)

# 可选:AgentJournal 懒加载
try:
    from openclaw.agent.journal import AgentJournal
    _HAS_JOURNAL = True
except Exception:  # pragma: no cover
    AgentJournal = None  # type: ignore[assignment]
    _HAS_JOURNAL = False


def trim_history(
    messages: list[ChatMessage],
    *,
    soft_window: int,
    max_chars: int,
) -> list[ChatMessage]:
    """RT-4:在多轮 ReAct 后裁剪 messages,防 token 爆。

    规则:
    1. 保留 system 消息(假定 0 位置是 system)
    2. 数量 ≤ soft_window 且字符 ≤ max_chars → 原样
    3. 否则:system + 头部 N 旧 + 中间 1 条 note + 最新 M
    """
    if not messages:
        return messages

    total_chars = sum(len(m.content or "") for m in messages)
    if len(messages) <= soft_window and total_chars <= max_chars:
        return messages

    system_msg: ChatMessage | None = None
    body: list[ChatMessage] = []
    if messages and messages[0].role == "system":
        system_msg = messages[0]
        body = messages[1:]
    else:
        body = list(messages)

    if len(body) > soft_window:
        head_n = max(1, soft_window // 3)
        tail_n = max(1, soft_window - head_n - 1)
        kept_head = body[:head_n]
        kept_tail = body[-tail_n:]
        note = ChatMessage(
            role="system",
            content=f"[trimmed: {len(body) - head_n - tail_n} older messages collapsed to save context]",
        )
        body = kept_head + [note] + kept_tail

    cur_chars = sum(len(m.content or "") for m in body) + (len(system_msg.content) if system_msg else 0)
    while cur_chars > max_chars and len(body) > 2:
        body.pop(0)
        cur_chars = sum(len(m.content or "") for m in body) + (len(system_msg.content) if system_msg else 0)

    if system_msg:
        return [system_msg] + body
    return body




@dataclass
class AgentResponse:
    """单轮 Agent 调用结果。"""
    content: str
    iterations: int
    tool_calls: list[ToolCall]
    session_id: str


class Agent:
    """单个会话的 Agent。"""

    def __init__(
        self,
        llm: BaseLLMProvider,
        tools: ToolRegistry,
        memory: ScopedMemory,
        session_id: str,
        *,
        system_prompt: str = "",
        max_tool_iterations: int = 8,
        history_window: int = 20,
        history_max_chars: int = 200_000,
        # 软窗口:超过后裁剪掉"中间"的历史(保留 system + 最新 N 轮)
        history_soft_window: int = 40,
        recall_top_k: int = 3,
        # Idea #5:可选 journal — 完成后自动写反思
        journal: Optional["AgentJournal"] = None,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.memory = memory
        self.session_id = session_id
        self.system_prompt = system_prompt
        self.max_tool_iterations = max_tool_iterations
        self.history_window = history_window
        self.history_max_chars = history_max_chars
        self.history_soft_window = history_soft_window
        self.recall_top_k = recall_top_k
        self.journal = journal

    async def run(self, user_message: str) -> AgentResponse:
        # 1. 拼装 messages(注入 soul + recall)
        # RT-1: build_messages 已改 async
        messages = await self.memory.build_messages(
            self.session_id,
            user_message,
            system_prompt=self.system_prompt,
            history_window=self.history_window,
            recall_top_k=self.recall_top_k,
        )

        iterations = 0
        all_tool_calls: list[ToolCall] = []
        tool_results: list[dict] = []  # for journal
        started_at = datetime.now(timezone.utc)

        for i in range(self.max_tool_iterations):
            iterations += 1
            # RT-4:每次送 LLM 前裁剪历史,防 token 爆
            messages = trim_history(
                messages,
                soft_window=self.history_soft_window,
                max_chars=self.history_max_chars,
            )
            logger.debug("[agent %s] iter=%d send to LLM, %d msgs", self.session_id, i, len(messages))
            result = await self.llm.acomplete(messages, tools=self.tools.specs())

            if not result.tool_calls:
                final = AgentResponse(
                    content=result.content or "",
                    iterations=iterations,
                    tool_calls=all_tool_calls,
                    session_id=self.session_id,
                )
                await self.memory.append_turn(self.session_id, user_message, final.content)
                await self._maybe_journal(user_message, final, tool_results, started_at)
                return final

            # tool_calls 分支
            assistant_msg = ChatMessage(
                role="assistant", content=result.content or "", tool_calls=result.tool_calls
            )
            messages.append(assistant_msg)
            all_tool_calls.extend(result.tool_calls)

            for tc in result.tool_calls:
                logger.info("[agent %s] tool call: %s(%s)", self.session_id, tc.name, tc.arguments)
                # SEC-2 修复:必须走 registry.call(),触发 approver 审批流 + 限流 + 审计
                # 旧实现 self.tools.get(tc.name)(**tc.arguments) 绕过了一切审批
                # Phase 25 / a4:registry.call() 会先用 JSON Schema 强校验 arguments,
                # 不合法直接抛 ToolValidationError,落到下面 except 走 sanitize + 截断回灌
                try:
                    output = await self.tools.call(tc.name, dict(tc.arguments or {}))
                    tool_content = str(output)
                    tool_results.append({"name": tc.name, "result": tool_content})
                except Exception as e:
                    logger.exception("tool %s failed", tc.name)
                    # Phase 25 / a4:工具异常信息可能含不可信外部内容(参数本身、traceback),
                    # 回灌到 messages 给 LLM 前必须过 sanitize 防提示词注入,
                    # 并截到 200 字符防 token 爆 / 防恶意大字段冲掉上下文
                    raw = f"[tool error] {type(e).__name__}: {e}"
                    safe = strip_external_content(raw)
                    if len(safe) > 200:
                        safe = safe[:200] + "..."
                    tool_content = safe
                    tool_results.append({"name": tc.name, "result": tool_content, "error": True})

                messages.append(
                    ChatMessage(role="tool", content=tool_content, tool_call_id=tc.id, name=tc.name)
                )

        # 达到最大迭代
        logger.warning("[agent %s] max iterations reached", self.session_id)
        final = AgentResponse(
            content="(达到最大工具调用轮次,未完成推理)",
            iterations=iterations,
            tool_calls=all_tool_calls,
            session_id=self.session_id,
        )
        # L7 修复:不再把"达到最大迭代"提示写回 memory(污染下一轮 context)
        # 旧逻辑:await self.memory.append_turn(...) 会把这句提示存入 memory,
        # 下一轮 LLM 会看到它,误以为上轮已正常完成。
        await self._maybe_journal(user_message, final, tool_results, started_at)
        return final

    async def _maybe_journal(
        self,
        user_message: str,
        response: AgentResponse,
        tool_results: list[dict],
        started_at: datetime,
    ) -> None:
        """Idea #5:session 结束后自动写 journal + reflect + soul_proposal。"""
        if self.journal is None:
            return
        try:
            # M10 修复:record_session / generate_soul_proposal 内部做同步文件 IO,
            # 用 asyncio.to_thread 包装避免阻塞事件循环
            entry = await asyncio.to_thread(
                self.journal.record_session,
                session_id=self.session_id,
                user_message=user_message,
                response=response,
                started_at=started_at,
                tool_results=tool_results,
            )
            # reflect 已经是 async(内部文件 IO 也需要 to_thread,但 reflect 本身
            # 可能调 LLM 是 async 的,所以不能简单 to_thread;journal.py 内部的
            # 文件 IO 在后续 M10-journal 修复中处理)
            try:
                await self.journal.reflect(entry)
            except Exception as e:  # noqa: BLE001
                logger.warning("journal reflect failed: %s", e)
            # 生成 SOUL proposal
            try:
                await asyncio.to_thread(self.journal.generate_soul_proposal, entry)
            except Exception as e:  # noqa: BLE001
                logger.warning("journal soul_proposal failed: %s", e)
        except Exception as e:  # noqa: BLE001
            logger.warning("journal record failed (不影响主流程): %s", e)


class AgentLoop:
    """跨多个 session 的 Agent 管理器。"""

    def __init__(
        self,
        llm: BaseLLMProvider,
        tools: ToolRegistry,
        memory: ScopedMemory,
        *,
        system_prompt: str = "",
        max_tool_iterations: int = 8,
        history_window: int = 20,
        history_max_chars: int = 200_000,
        history_soft_window: int = 40,
        recall_top_k: int = 3,
        journal: Optional["AgentJournal"] = None,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.memory = memory
        self.system_prompt = system_prompt
        self.max_tool_iterations = max_tool_iterations
        self.history_window = history_window
        self.history_max_chars = history_max_chars
        self.history_soft_window = history_soft_window
        self.recall_top_k = recall_top_k
        self.journal = journal
        # M11 修复:用 OrderedDict + maxsize 实现 LRU 淘汰
        # 旧逻辑:普通 dict 只追加不淘汰,IM bot 每个 chat 一个 session_id,
        # 长期运行 Agent(持有 tools/memory/llm 引用)无界增长,内存只增不减。
        self._agents: OrderedDict[str, Agent] = OrderedDict()
        self._max_agents = 128  # 最大缓存的 Agent 数量

    def _get_agent(self, session_id: str) -> Agent:
        if session_id not in self._agents:
            self._agents[session_id] = Agent(
                llm=self.llm,
                tools=self.tools,
                memory=self.memory,
                session_id=session_id,
                system_prompt=self.system_prompt,
                max_tool_iterations=self.max_tool_iterations,
                history_window=self.history_window,
                recall_top_k=self.recall_top_k,
                journal=self.journal,
            )
            # M11 修复:超过上限时淘汰最久未访问的 Agent
            while len(self._agents) > self._max_agents:
                self._agents.popitem(last=False)  # FIFO 淘汰最旧的
        else:
            # M11 修复:移到末尾(标记为最近访问)
            self._agents.move_to_end(session_id)
        return self._agents[session_id]

    async def handle(self, session_id: str, user_message: str) -> AgentResponse:
        agent = self._get_agent(session_id)
        return await agent.run(user_message)

    async def new_session(self, session_id: str | None = None) -> str:
        sid = session_id or f"sess_{uuid.uuid4().hex[:12]}"
        self._get_agent(sid)
        return sid
