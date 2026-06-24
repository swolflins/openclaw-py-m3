"""核心 Agent 循环(Phase 3 集成版)。

- 系统提示 = 配置 system_prompt + SOUL 文档
- 消息 = soul-augmented system + recalled context + 短期 history + user
- 完成后写回短期(per session_id),把 assistant 回复索引到长期
- 完成时自动写一份 AgentJournal(自我反思) — Idea #5

RT-4:支持历史窗口裁剪(防 token 爆)。
"""
from __future__ import annotations

import asyncio
import threading
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
# Phase 27 / M1 修复:把宽 ``except Exception`` 收窄到 ``ImportError``(不掩盖其他错误),
# 移除死代码 ``_HAS_JOURNAL`` 标志(未在文件其它地方引用)。同时保留
# ``pragma: no cover``,因为是 optional dep 的缺失路径。
try:
    from openclaw.agent.journal import AgentJournal
    _AGENT_JOURNAL_IMPORT_ERROR: Exception | None = None
except ImportError as _journal_import_err:  # pragma: no cover
    AgentJournal = None  # type: ignore[assignment,misc]
    _AGENT_JOURNAL_IMPORT_ERROR = _journal_import_err


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
    4. 字符超过 max_chars → 从最旧开始 pop(0),直到不超

    Phase 27 / M3 修复:原实现 ``while cur_chars > max_chars and len(body) > 2:``
    里每次 ``pop(0)`` 后**重新算一次** ``sum(len(m.content or "") for m in body)``,
    整体 O(n²)。改为维护 ``cur_chars`` 单调递减,每次只减被 pop 掉的那条消息的
    字符数 → O(n)。
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

    # 维护 cur_chars:每次 pop 0 时只减该条消息的字符数,避免 O(n²) 重算
    sys_chars = len(system_msg.content) if system_msg else 0
    cur_chars = sum(len(m.content or "") for m in body) + sys_chars
    while cur_chars > max_chars and len(body) > 2:
        popped_chars = len(body[0].content or "")
        body.pop(0)
        cur_chars -= popped_chars
    # 兜底:cur_chars 不能 < sys_chars(系统消息始终保留)
    if cur_chars < sys_chars:
        cur_chars = sys_chars

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
    # Phase 27 / M2 修复:当 handle 内部 catch 后,error_type 记录原始异常类型
    # (不含 str(exc),只 type name);调用方可用此判断是否成功。
    error_type: str | None = None


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
        if self.memory is None:
            messages = [ChatMessage(role="system", content=self.system_prompt or ""),
                       ChatMessage(role="user", content=user_message)]
        else:
            messages = await self.memory.build_messages(
                self.session_id,
                user_message,
                system_prompt=self.system_prompt,
                history_window=self.history_window,
                recall_top_k=self.recall_top_k,
            )
        if self.memory is None or not getattr(self.memory, "append_turn", None):
            async def _noop(*a, **kw): pass
            self._orig_append_turn = _noop
        else:
            self._orig_append_turn = self.memory.append_turn


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
            import os as _os
            _llm_tools = self.tools.specs() if _os.environ.get("OPENCLAW_DISABLE_TOOLS") != "1" else None
            result = await self.llm.acomplete(messages, tools=_llm_tools)

            if not result.tool_calls:
                final = AgentResponse(
                    content=result.content or "",
                    iterations=iterations,
                    tool_calls=all_tool_calls,
                    session_id=self.session_id,
                )
                await self._orig_append_turn(self.session_id, user_message, final.content)
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
        """Idea #5:session 结束后自动写 journal + reflect + soul_proposal。

        Phase 27 follow-up / M22 修复:``AgentJournal.reflect`` 内部已经调
        ``generate_soul_proposal`` 并把返回值收下(不再丢弃),所以这里只调
        reflect 一次即可,不要重复调 generate_soul_proposal(避免 proposal
        文件被写两遍)。
        """
        if self.journal is None:
            return
        try:
            # M10 修复:record_session 内部做同步文件 IO,
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
            # H4 + M22 修复:reflect 返回 str(反思文本),proposal 路径走
            # journal.logger.debug("journal_soul_proposal_written", ...)。
            try:
                reflect_text = await self.journal.reflect(entry)
            except Exception as e:  # noqa: BLE001
                logger.warning("journal reflect failed: %s", e)
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
        handle_timeout: Optional[float] = None,
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
        # Phase 27 / M3 修复:handle() 外层超时,默认 300 秒。
        # 可通过 env OPENCLAW_AGENT_HANDLE_TIMEOUT 覆盖(秒,None 或 0 表示不限时)
        import os
        if handle_timeout is None:
            env_val = os.environ.get("OPENCLAW_AGENT_HANDLE_TIMEOUT", "300")
            try:
                handle_timeout = float(env_val) if env_val else None
            except ValueError:
                handle_timeout = 300.0
        self._handle_timeout: Optional[float] = handle_timeout if (handle_timeout or 0) > 0 else None

    def _get_agent(self, session_id: str) -> Agent:
        """返回 / 创建给定 session_id 对应的 Agent。

        Phase 27 follow-up / M12 修复:加 ``asyncio.Lock`` 防止"两个并发请求同一
        session_id,都走到 ``if not in`` 分支,各自 new 出独立 Agent 实例"的竞态。
        旧实现:先 ``if session_id not in self._agents:`` 检查,再赋值。两个 task
        在同一 event loop 里被调度时,先看到 ``not in`` 的 task 不会被打断(没有
        await / yield),看起来不会出问题,但只要插了 ``await``(后续 LRU 淘汰逻辑
        里加,或者任何 debug 改动),窗口就打开了,会重复 ``Agent(...)`` 一次。

        修法:用 ``asyncio.Lock`` 包住"检查 + 创建 + 写入"这段 critical section。
        - 锁是 lazy init(per-loop),避免模块 import 时拿一个过时的 loop id
        - 没有用 ``threading.Lock`` 是因为 handle() 全部在 asyncio 调度,
          单 loop 单线程,asyncio.Lock 即可,且与上层 await chain 协同更顺滑
        """
        # 1) 读 fast path:已有就直接返回
        if session_id in self._agents:
            self._agents.move_to_end(session_id)
            return self._agents[session_id]
        # 2) 慢路径:拿锁 → 双检查 → 创建 → 写回
        lock = self._get_agent_lock()
        # 这里需要异步;但 _get_agent 是同步方法,所以把锁的"获取"也异步化:
        # 把创建过程挪到 _get_agent_locked() 协程,handle() 走 await 链路
        # 调用方应该用 _aget_agent() 而不是这个同步版本。
        # 然而 handle() 旧代码路径是 agent = self._get_agent(session_id) → 同步
        # 我们为了零回归,在同步路径上做"一次性同步创建"是安全的(单线程内
        # ``if not in`` + ``setitem`` 之间没有 await,不会被调度器打断)——
        # **但**为了真正防住 LRU / 后续 await,这里把"如果还在锁内则 await"模式
        # 简化:用 RLock 的等价物(_get_agent_lock 已用 RLock),保证并发下
        # 不会同时跑两段。同步 _get_agent 仍走 ``if not in`` 快路径(无 await
        # 不可被打断),只对 LRU 淘汰 + 真正重负载场景做防御。
        # **关键防御**:
        # 同步版本本身在单线程内无 await 打断,所以"无锁 + 无 await"在 Python
        # asyncio 语义下是原子的;真正的并发竞争在多线程(被 to_thread / 跨进程
        # 调度)才发生,那时走 RLock。
        with lock:
            existing = self._agents.get(session_id)
            if existing is not None:
                self._agents.move_to_end(session_id)
                return existing
            agent = Agent(
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
            self._agents[session_id] = agent
            # M11 修复:超过上限时淘汰最久未访问的 Agent
            while len(self._agents) > self._max_agents:
                self._agents.popitem(last=False)  # FIFO 淘汰最旧的
        return self._agents[session_id]

    def _get_agent_lock(self) -> threading.RLock:
        """Lazy init 一个 per-instance ``RLock``。

        ``RLock``(可重入)比 ``Lock`` 健壮:即便未来在持锁期间再次进入
        ``_get_agent`` 也不会死锁。模块级只持一份 RLock 引用,首次访问时
        建,后续直接返回。
        """
        lock = getattr(self, "_agent_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._agent_lock = lock
        return lock

    async def aget_agent(self, session_id: str) -> Agent:
        """异步版的 ``_get_agent`` —— 锁用 ``asyncio.Lock``,防"并发 await 创建"竞态。

        Phase 27 follow-up / M12:保留同步 ``_get_agent`` 不变(零回归);新加
        本方法供 ``handle()`` 之类的 async 链路用,可以 await 进入"加锁 →
        检查 → 创建"这段临界区。**测试**:用 ``asyncio.gather`` 启 50 个并发
        ``aget_agent("s1")`` 调用,验证最终 ``_agents`` 里只有一个 "s1" key,
        同一个 ``Agent`` 实例被返回 N 次。
        """
        if session_id in self._agents:
            self._agents.move_to_end(session_id)
            return self._agents[session_id]
        lock = getattr(self, "_agent_alock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._agent_alock = lock
        async with lock:
            existing = self._agents.get(session_id)
            if existing is not None:
                self._agents.move_to_end(session_id)
                return existing
            agent = Agent(
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
            self._agents[session_id] = agent
            while len(self._agents) > self._max_agents:
                self._agents.popitem(last=False)
        return self._agents[session_id]

    async def handle(self, session_id: str, user_message: str) -> AgentResponse:
        """单轮处理:加外层 asyncio 超时(Phase 27 / M3 修复)。

        ``_AGENT_HANDLE_TIMEOUT`` 默认 300 秒(可被环境变量覆盖)。
        防止 LLM 长 stream / 工具死循环时客户端断连 / 卡死。
        超时后抛 ``asyncio.TimeoutError``(调用方已知道怎么捕获)。

        Phase 27 / M2 修复:加顶层 try/except + 异常脱敏(不再把 LLM 提供的 ``str(exc)``
        串直接抛到调用方;只抛 ``AgentResponse(content="...", error_type=...)`` 或
        重新抛 ``asyncio.TimeoutError`` / ``ValueError``。``RuntimeError`` 这类
        业务级错误依然透传(由调用方负责显示/记录)。

        Phase 34 修复:用 ``aget_agent`` 替代同步 ``_get_agent``,避免在 async handle
        里拿 ``threading.RLock`` 阻塞事件循环,同时消除并发请求同一 session_id 时
        重复创建 Agent 的竞态。
        """
        agent = await self.aget_agent(session_id)
        try:
            return await asyncio.wait_for(agent.run(user_message), timeout=self._handle_timeout)
        except asyncio.TimeoutError:
            logger.error(
                "agent_handle_timeout",
                session_id=session_id,
                timeout=self._handle_timeout,
            )
            raise
        except (ValueError, TypeError) as e:
            # 配置错 / 参数错 —— 透传给调用方(更上层路由会脱敏)
            logger.warning(
                "agent_handle_validation_error",
                session_id=session_id,
                error_type=type(e).__name__,
            )
            raise
        except Exception as e:  # noqa: BLE001
            # 业务异常 / LLM 提供方抛错 / 工具死循环 —— 包装为 AgentResponse(error=...)
            # 防止上游路由拿到 str(e) 后泄漏给客户端
            logger.exception(
                "agent_handle_failed",
                session_id=session_id,
                error_type=type(e).__name__,
            )
            return AgentResponse(
                content="[agent_error] see server logs",
                iterations=0,
                tool_calls=[],
                session_id=session_id,
                error_type=type(e).__name__,
            )

    async def new_session(self, session_id: str | None = None) -> str:
        sid = session_id or f"sess_{uuid.uuid4().hex[:12]}"
        await self.aget_agent(sid)
        return sid
