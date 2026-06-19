"""/v1/sessions CRUD。

与 memory 路由相同:实际接口在 `ScopedMemory.short` 上。
"""
from __future__ import annotations


from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from openclaw.gateway.deps import get_deps
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
async def get_session(session_id: str, k: int = Query(20, ge=1, le=200)) -> dict:
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


@router.delete("/{session_id}")
async def clear_session(session_id: str) -> dict:
    short = _short()
    if short is None:
        raise HTTPException(503, "agent_loop not attached")
    if not hasattr(short, "clear"):
        raise HTTPException(404, "memory backend doesn't support clear")
    try:
        short.clear(session_id)
    except Exception as e:
        raise HTTPException(500, f"clear error: {e}") from e
    return {"session_id": session_id, "cleared": True}


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
