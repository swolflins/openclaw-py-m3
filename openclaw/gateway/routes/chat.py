"""/v1/chat 单轮 + /v1/chat/stream SSE。

**SEC-11 修复**:
- 不再向客户端返回 ``str(e)`` 原始异常(可能含文件路径 / SQL / 密钥)
- 通过全局 exception handler 统一处理:记录 trace_id + 错误类型,客户端只看到 request_id
- SSE 错误事件也脱敏
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
from openclaw.gateway.util import to_jsonable

logger = get_logger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])


class ChatRequest(BaseModel):
    session_id: str = Field(default="default", description="会话 id(可作 memory key)")
    message: str = Field(..., min_length=1, max_length=20000)
    system_prompt: Optional[str] = Field(default=None, description="临时覆盖 system_prompt")


class ChatResponse(BaseModel):
    session_id: str
    content: str
    iterations: int = 0
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    duration_ms: int = 0


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
    try:
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
    return ChatResponse(
        session_id=req.session_id,
        content=resp.content or "",
        iterations=resp.iterations,
        tool_calls=[to_jsonable(tc) for tc in (resp.tool_calls or [])],
        duration_ms=int((time.time() - t0) * 1000),
    )


# ------------- SSE 流式 -------------

def _sse_format(event: str, data: Any) -> dict:
    return {"event": event, "data": json.dumps(to_jsonable(data), ensure_ascii=False)}


@router.post("/stream")
async def chat_stream(req: ChatRequest, request: Request) -> EventSourceResponse:
    """把 AgentLoop 的 tool_calls / 最终 content 切成 SSE 事件流。"""
    deps = get_deps()
    if not deps.ready() or deps.agent_loop is None:
        raise HTTPException(503, "agent_loop not attached")

    request_id = getattr(request.state, "request_id", None) or uuid.uuid4().hex[:12]

    queue: asyncio.Queue = asyncio.Queue(maxsize=64)
    loop_ref = deps.agent_loop
    # SEC-12:跟踪 SSE producer task,请求结束时取消 + 防泄漏
    task_ref: dict[str, Optional[asyncio.Task]] = {"task": None}

    async def _produce() -> None:
        try:
            await queue.put(_sse_format("start", {"session_id": req.session_id, "request_id": request_id}))
            # 监听 AgentLoop 内部 — 我们没法直接 hook,所以走"半流式":
            # 先发一个 thinking,然后拿到最终回复
            await queue.put(_sse_format("thinking", {"text": "agent is thinking..."}))
            resp = await loop_ref.handle(req.session_id, req.message)
            for tc in (resp.tool_calls or []):
                await queue.put(_sse_format("tool_call", tc))
            await queue.put(_sse_format("delta", {"text": resp.content or ""}))
            await queue.put(
                _sse_format(
                    "done",
                    {
                        "iterations": resp.iterations,
                        "session_id": req.session_id,
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
