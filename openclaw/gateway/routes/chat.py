"""/v1/chat 单轮 + /v1/chat/stream SSE。

**SEC-11 修复**:
- 不再向客户端返回 ``str(e)`` 原始异常(可能含文件路径 / SQL / 密钥)
- 通过全局 exception handler 统一处理:记录 trace_id + 错误类型,客户端只看到 request_id
- SSE 错误事件也脱敏

**Phase 23 消息线程(reply / "1 条回复" 效果)**:
- ChatRequest 加 ``reply_to_id`` 字段:client 告诉后端「我的这条消息是 reply 哪条 user/assistant 消息」
- ChatResponse / SSE 事件加 ``message_id`` + ``reply_to_id`` + ``reply_count``
- assistant 消息的 ``parent_id`` = 触发它的 user 消息的 ``message_id``
- 飞书/Lark 的「1 条回复」效果在协议层:client 拿到 message_id,可用
  ``GET /v1/sessions/{sid}/messages/{msg_id}`` 反查 parent 原文
- MessageStore 存于 ``deps.extra["message_store"]``,不在主 dataclass 注入(避免 schema 改)
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from openclaw.core.logging import get_logger
from openclaw.gateway import metrics as m
from openclaw.gateway.deps import get_deps
from openclaw.gateway.message_store import MessageStore
from openclaw.gateway.util import to_jsonable

logger = get_logger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])


def _get_message_store() -> MessageStore:
    """从 deps.extra 拿 MessageStore(没有则当场建一个 in-memory 的)。

    设计:挂在 extra dict 上,不污染 GatewayDeps dataclass schema;
    同 process 内 gateway 实例共享一份。
    """
    deps = get_deps()
    ms = deps.extra.get("message_store") if isinstance(deps.extra, dict) else None
    if ms is None:
        ms = MessageStore()
        if isinstance(deps.extra, dict):
            deps.extra["message_store"] = ms
    return ms


class ChatRequest(BaseModel):
    session_id: str = Field(default="default", description="会话 id(可作 memory key)")
    message: str = Field(..., min_length=1, max_length=20000)
    system_prompt: Optional[str] = Field(default=None, description="临时覆盖 system_prompt")
    reply_to_id: Optional[str] = Field(
        default=None,
        description="Phase 23: 我的消息是 reply 哪条(同 session)消息的 msg_id;None 即普通新消息",
    )


class ChatResponse(BaseModel):
    session_id: str
    content: str
    iterations: int = 0
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    duration_ms: int = 0
    # Phase 23:thread 关联
    message_id: Optional[str] = Field(
        default=None,
        description="本条 assistant 回复的 msg_id;client 可用此 id 关联 UI 渲染",
    )
    reply_to_id: Optional[str] = Field(
        default=None,
        description="本条 assistant 回复的 parent_id(=触发它的 user 消息的 msg_id)",
    )
    reply_count: Optional[int] = Field(
        default=None,
        description="本条消息被 reply 了几次(用于飞书的 1 条回复 count 显示)",
    )


@router.post("", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request) -> ChatResponse:
    deps = get_deps()
    if not deps.ready() or deps.agent_loop is None:
        raise HTTPException(503, "agent_loop not attached;configure providers first")
    loop = deps.agent_loop
    t0 = time.time()
    # SEC-12:把 session_id 截到 32 字符,降低 metrics 基数
    sid = (req.session_id or "default")[:32]
    m.chat_total.inc(session_id=sid)
    request_id = getattr(request.state, "request_id", None) or uuid.uuid4().hex[:12]
    ms = _get_message_store()
    try:
        # Phase 23:先把 user 消息存到 store(用作 thread 根)
        user_msg = await ms.add(
            session_id=req.session_id,
            role="user",
            content=req.message,
            parent_id=req.reply_to_id,  # client 告诉我这条是 reply 哪条
        )
        resp = await loop.handle(req.session_id, req.message)
    except Exception as e:
        m.chat_errors_total.inc(error_type=type(e).__name__)
        # SEC-11 修复:不暴露 str(e),只给 request_id;完整 trace 写到 server log
        logger.exception(
            "chat_handler_error",
            request_id=request_id,
            session_id=sid,
            error_type=type(e).__name__,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "request_id": request_id,
                "type": type(e).__name__,
            },
        )

    # Phase 23:存 assistant 消息,parent_id = 触发的 user 消息 id
    asst_msg = await ms.add(
        session_id=req.session_id,
        role="assistant",
        content=resp.content or "",
        parent_id=user_msg.msg_id,  # ← assistant 是 user 消息的 reply
        iterations=resp.iterations,
        tool_calls_count=len(resp.tool_calls or []),
    )
    # 数 user 消息被 reply 了几次(飞书"1 条回复"那个数字)
    user_reply_count = await ms.count_replies(req.session_id, user_msg.msg_id)

    return ChatResponse(
        session_id=req.session_id,
        content=resp.content or "",
        iterations=resp.iterations,
        tool_calls=[to_jsonable(tc) for tc in (resp.tool_calls or [])],
        duration_ms=int((time.time() - t0) * 1000),
        message_id=asst_msg.msg_id,
        reply_to_id=user_msg.msg_id,
        reply_count=user_reply_count,
    )


# ------------- SSE 流式 -------------

def _sse_format(event: str, data: Any) -> dict:
    return {"event": event, "data": json.dumps(to_jsonable(data), ensure_ascii=False)}


@router.post("/stream")
async def chat_stream(req: ChatRequest, request: Request) -> EventSourceResponse:
    """把 AgentLoop 的 tool_calls / 最终 content 切成 SSE 事件流。

    Phase 23:新加 ``message`` SSE 事件(在 start 后发),里面含 ``message_id`` /
    ``reply_to_id``,让前端拿到"我是哪条消息 / reply 哪条"的元数据。
    """
    deps = get_deps()
    if not deps.ready() or deps.agent_loop is None:
        raise HTTPException(503, "agent_loop not attached")

    request_id = getattr(request.state, "request_id", None) or uuid.uuid4().hex[:12]

    queue: asyncio.Queue = asyncio.Queue(maxsize=64)
    loop_ref = deps.agent_loop
    # SEC-12:跟踪 SSE producer task,请求结束时取消 + 防泄漏
    task_ref: dict[str, Optional[asyncio.Task]] = {"task": None}
    # Phase 23:提前拿到 store 引用(避免 producer 里再 import)
    ms = _get_message_store()

    async def _produce() -> None:
        user_msg_id: Optional[str] = None
        asst_msg_id: Optional[str] = None
        try:
            # Phase 23:先存 user 消息(作为 thread 锚点);同时发 message 事件
            user_msg = await ms.add(
                session_id=req.session_id,
                role="user",
                content=req.message,
                parent_id=req.reply_to_id,
            )
            user_msg_id = user_msg.msg_id
            await queue.put(_sse_format("start", {
                "session_id": req.session_id,
                "request_id": request_id,
            }))
            # Phase 23:同步推一条 message 事件告诉前端"我收到 user 消息了"
            await queue.put(_sse_format("message", {
                "role": "user",
                "message_id": user_msg.msg_id,
                "reply_to_id": req.reply_to_id,  # user 消息的 parent
                "content": req.message,
            }))
            await queue.put(_sse_format("thinking", {"text": "agent is thinking..."}))
            resp = await loop_ref.handle(req.session_id, req.message)
            for tc in (resp.tool_calls or []):
                await queue.put(_sse_format("tool_call", tc))

            # Phase 23:存 assistant 消息,parent = user 消息
            asst_msg = await ms.add(
                session_id=req.session_id,
                role="assistant",
                content=resp.content or "",
                parent_id=user_msg.msg_id,
                iterations=resp.iterations,
                tool_calls_count=len(resp.tool_calls or []),
            )
            asst_msg_id = asst_msg.msg_id
            user_reply_count = await ms.count_replies(req.session_id, user_msg.msg_id)

            # Phase 23:把 assistant message 元数据也以独立事件推
            await queue.put(_sse_format("message", {
                "role": "assistant",
                "message_id": asst_msg.msg_id,
                "reply_to_id": user_msg.msg_id,  # assistant 是 user 消息的 reply
                "content_preview": (resp.content or "")[:120],
                "iterations": resp.iterations,
                "tool_calls_count": len(resp.tool_calls or []),
                "reply_count": user_reply_count,  # user 消息被 reply 几次
            }))
            await queue.put(_sse_format("delta", {"text": resp.content or ""}))
            await queue.put(
                _sse_format(
                    "done",
                    {
                        "iterations": resp.iterations,
                        "session_id": req.session_id,
                        "message_id": asst_msg_id,
                        "reply_to_id": user_msg_id,
                    },
                )
            )
        except Exception as e:
            # SEC-11 修复:不暴露 str(e) 给客户端
            logger.exception(
                "chat_stream_error",
                request_id=request_id,
                session_id=req.session_id,
                error_type=type(e).__name__,
            )
            await queue.put(
                _sse_format(
                    "error",
                    {
                        "error": "internal_error",
                        "request_id": request_id,
                        "type": type(e).__name__,
                    },
                )
            )
        finally:
            await queue.put(_sse_format("__end__", {"ok": True}))

    task_ref["task"] = asyncio.create_task(_produce())

    async def _gen():
        try:
            while True:
                item = await queue.get()
                yield item
                if item.get("event") == "__end__":
                    break
        finally:
            # SEC-12:确保 producer task 一定被取消,防止悬挂 task 泄漏
            t = task_ref.get("task")
            if t is not None and not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

    return EventSourceResponse(_gen())
