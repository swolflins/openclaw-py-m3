"""/v1/chat 单轮 + /v1/chat/stream SSE。"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from openclaw.gateway import metrics as m
from openclaw.gateway.deps import get_deps
from openclaw.gateway.util import to_jsonable

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
async def chat(req: ChatRequest) -> ChatResponse:
    deps = get_deps()
    if not deps.ready() or deps.agent_loop is None:
        raise HTTPException(503, "agent_loop not attached;configure providers first")
    loop = deps.agent_loop
    t0 = time.time()
    m.chat_total.inc(session_id=req.session_id)
    try:
        resp = await loop.handle(req.session_id, req.message)
    except Exception as e:
        m.chat_errors_total.inc(error_type=type(e).__name__)
        raise HTTPException(500, f"agent error: {type(e).__name__}: {e}") from e
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
async def chat_stream(req: ChatRequest) -> EventSourceResponse:
    """把 AgentLoop 的 tool_calls / 最终 content 切成 SSE 事件流。"""
    deps = get_deps()
    if not deps.ready() or deps.agent_loop is None:
        raise HTTPException(503, "agent_loop not attached")

    queue: asyncio.Queue = asyncio.Queue(maxsize=64)
    loop_ref = deps.agent_loop

    async def _produce() -> None:
        try:
            await queue.put(_sse_format("start", {"session_id": req.session_id}))
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
            await queue.put(_sse_format("error", {"message": str(e), "type": type(e).__name__}))
        finally:
            await queue.put(_sse_format("__end__", {"ok": True}))

    asyncio.create_task(_produce())

    async def _gen():
        while True:
            item = await queue.get()
            yield item
            if item.get("event") == "__end__":
                break

    return EventSourceResponse(_gen())
