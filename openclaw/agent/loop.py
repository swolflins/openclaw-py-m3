"""核心 Agent 循环(Phase 3 集成版)。

- 系统提示 = 配置 system_prompt + SOUL 文档
- 消息 = soul-augmented system + recalled context + 短期 history + user
- 完成后写回短期(per session_id),把 assistant 回复索引到长期
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from openclaw.llm.base import BaseLLMProvider, ChatMessage, ToolCall
from openclaw.memory.scoped import ScopedMemory
from openclaw.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


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
        recall_top_k: int = 3,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.memory = memory
        self.session_id = session_id
        self.system_prompt = system_prompt
        self.max_tool_iterations = max_tool_iterations
        self.history_window = history_window
        self.recall_top_k = recall_top_k

    async def run(self, user_message: str) -> AgentResponse:
        # 1. 拼装 messages(注入 soul + recall)
        messages = self.memory.build_messages(
            self.session_id,
            user_message,
            system_prompt=self.system_prompt,
            history_window=self.history_window,
            recall_top_k=self.recall_top_k,
        )

        iterations = 0
        all_tool_calls: list[ToolCall] = []

        for i in range(self.max_tool_iterations):
            iterations += 1
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
                try:
                    output = await self.tools.call(tc.name, **(tc.arguments or {}))
                    tool_content = str(output)
                except Exception as e:
                    logger.exception("tool %s failed", tc.name)
                    tool_content = f"[tool error] {type(e).__name__}: {e}"

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
        await self.memory.append_turn(self.session_id, user_message, final.content)
        return final


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
        recall_top_k: int = 3,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.memory = memory
        self.system_prompt = system_prompt
        self.max_tool_iterations = max_tool_iterations
        self.history_window = history_window
        self.recall_top_k = recall_top_k
        self._agents: dict[str, Agent] = {}

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
            )
        return self._agents[session_id]

    async def handle(self, session_id: str, user_message: str) -> AgentResponse:
        agent = self._get_agent(session_id)
        return await agent.run(user_message)

    async def new_session(self, session_id: str | None = None) -> str:
        sid = session_id or f"sess_{uuid.uuid4().hex[:12]}"
        self._get_agent(sid)
        return sid
