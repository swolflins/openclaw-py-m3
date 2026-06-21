"""/v1/sessions CRUD。

与 memory 路由相同:实际接口在 `ScopedMemory.short` 上。

Phase 23:加 ``GET /v1/sessions/{sid}/messages/{msg_id}`` 反查 UI 消息原文 —
- 实现飞书的 1 条回复那种引言展开效果
- client 拿到 message_id → 用此端点拉 parent 消息的 content + role
- 严格 scope 校验(防跨 session 引用)
"""
from __future__ import annotations


from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from openclaw.gateway.deps import current_user_id, get_deps
from openclaw.gateway.message_store import MessageStore
from openclaw.gateway.util import to_jsonable

router = APIRouter(prefix="/sessions", tags=["sessions"])


def _short():
    deps = get_deps()
    if deps.agent_loop is None or not hasattr(deps.agent_loop, "memory"):
        return None
    mem = deps.agent_loop.memory
    if hasattr(mem, "short"):
        return mem.short
    return mem  # fallback:顶层就是 short


def _message_store() -> MessageStore:
    """从 deps.extra 拿;没有则新建(与 chat.py 共享同一逻辑)。"""
    deps = get_deps()
    ms = deps.extra.get("message_store") if isinstance(deps.extra, dict) else None
    if ms is None:
        ms = MessageStore()
        if isinstance(deps.extra, dict):
            deps.extra["message_store"] = ms
    return ms


@router.get("")
async def list_sessions() -> dict:
    short = _short()
    if short is None:
        return {"sessions": [], "count": 0}
    scopes: list[str] = []
    if hasattr(short, "all_scopes"):
        try:
            scopes = list(short.all_scopes())
        except Exception:
            scopes = []
    return {"sessions": scopes, "count": len(scopes)}


@router.get("/{session_id}")
async def get_session(session_id: str, request: Request, k: int = Query(20, ge=1, le=200)) -> dict:
    # M7 修复:per-user 校验,防 IDOR
    uid = current_user_id(request)
    if uid != "anonymous" and not session_id.startswith(f"{uid}:"):
        raise HTTPException(403, "session does not belong to current user")
    short = _short()
    if short is None:
        raise HTTPException(503, "agent_loop not attached")
    if not hasattr(short, "recent_messages"):
        raise HTTPException(404, "memory backend doesn't support history")
    try:
        msgs = short.recent_messages(session_id, k=k)
    except Exception as e:
        raise HTTPException(500, f"read error: {e}") from e
    return {
        "session_id": session_id,
        "count": len(msgs),
        "messages": [to_jsonable(m) for m in msgs],
    }


# ---------- Phase 23:消息元数据查(UI 渲染用) ----------

@router.get("/{session_id}/messages/{msg_id}")
async def get_message(session_id: str, msg_id: str) -> dict:
    """按 msg_id 拉单条 UI 消息原文(role + content + parent_id)。

    用法:client 拿到 ``message_id``(从 chat 响应 / SSE message 事件)后,
    可用此端点查「我这条是 reply 哪条」的 parent 完整内容,
    实现飞书那种「1 条回复」展开看引言的 UI 效果。
    """
    ms = _message_store()
    sm = await ms.get_in_session(session_id, msg_id)
    if sm is None:
        raise HTTPException(404, f"message {msg_id!r} not found in session {session_id!r}")
    return {"message": sm.to_dict()}


@router.get("/{session_id}/messages")
async def list_messages(session_id: str, request: Request, k: int = Query(50, ge=1, le=500)) -> dict:
    """列一个 session 最近 k 条 UI 消息(按时间倒序,最新在前)。"""
    # M7 修复:per-user 校验,防 IDOR
    uid = current_user_id(request)
    if uid != "anonymous" and not session_id.startswith(f"{uid}:"):
        raise HTTPException(403, "session does not belong to current user")
    ms = _message_store()
    msgs = await ms.list_session(session_id, k=k)
    return {
        "session_id": session_id,
        "count": len(msgs),
        "messages": [m.to_dict() for m in msgs],
    }


@router.delete("/{session_id}")
async def clear_session(session_id: str, request: Request) -> dict:
    """清一个 session:既清 LLM memory,也清 UI 消息元数据。

    两个 sub-system 解耦:可能没 agent_loop(只清 UI),可能没 message_store(只清 memory)。
    """
    # M7 修复:per-user 校验,防 IDOR
    uid = current_user_id(request)
    if uid != "anonymous" and not session_id.startswith(f"{uid}:"):
        raise HTTPException(403, "session does not belong to current user")
    short = _short()
    memory_cleared = False
    if short is not None and hasattr(short, "clear"):
        try:
            short.clear(session_id)
            memory_cleared = True
        except Exception as e:
            raise HTTPException(500, f"clear error: {e}") from e
    # Phase 23:同步清 UI 消息元数据
    ms = _message_store()
    cleared = await ms.clear_session(session_id)
    return {
        "session_id": session_id,
        "cleared": memory_cleared,
        "ui_messages_cleared": cleared,
    }


class NewSessionRequest(BaseModel):
    session_id: str | None = None


@router.post("")
async def new_session(req: NewSessionRequest) -> dict:
    deps = get_deps()
    if deps.agent_loop is None:
        raise HTTPException(503, "agent_loop not attached")
    if not hasattr(deps.agent_loop, "new_session"):
        raise HTTPException(404, "AgentLoop has no new_session()")
    try:
        sid = await deps.agent_loop.new_session(req.session_id)
    except Exception as e:
        raise HTTPException(500, f"new_session error: {e}") from e
    return {"session_id": sid, "created": True}
