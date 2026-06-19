"""/v1/memory 短期 / 长期 / SOUL。

注意:AgentLoop.memory 实际上是 `ScopedMemory` 实例,
它本身没有 recent_messages / clear / append_turn / recall,
这些方法都在它的子模块上:.short / .long / .soul。
本路由按"先试子模块,没有再试顶层"的策略兼容两种接口。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from openclaw.gateway.deps import get_deps
from openclaw.gateway.util import to_jsonable

router = APIRouter(prefix="/memory", tags=["memory"])


def _scoped():
    deps = get_deps()
    if deps.agent_loop is None or not hasattr(deps.agent_loop, "memory"):
        return None
    return deps.agent_loop.memory


def _short(scoped):
    if scoped is None:
        return None
    if hasattr(scoped, "short"):
        return scoped.short
    return scoped  # fallback:顶层就是 short


def _long(scoped):
    if scoped is None:
        return None
    return getattr(scoped, "long", None)


def _soul(scoped):
    if scoped is None:
        return None
    return getattr(scoped, "soul", None)


@router.get("/short")
async def get_short(scope: str = Query(...), k: int = Query(20, ge=1, le=200)) -> dict:
    scoped = _scoped()
    short = _short(scoped)
    if short is None:
        raise HTTPException(503, "agent_loop not attached")
    if not hasattr(short, "recent_messages"):
        raise HTTPException(404, "no short_term memory")
    try:
        msgs = short.recent_messages(scope, k=k)
    except Exception as e:
        raise HTTPException(500, f"read error: {e}") from e
    return {"scope": scope, "count": len(msgs), "messages": [to_jsonable(m) for m in msgs]}


class ShortAppendRequest(BaseModel):
    scope: str
    role: str
    content: str
    name: str | None = None
    tool_call_id: str | None = None


@router.post("/short")
async def append_short(req: ShortAppendRequest) -> dict:
    scoped = _scoped()
    short = _short(scoped)
    if short is None:
        raise HTTPException(503, "agent_loop not attached")
    if not hasattr(short, "append_turn"):
        raise HTTPException(404, "no short_term memory")
    try:
        await short.append_turn(
            scope=req.scope, role=req.role, content=req.content,
            name=req.name, tool_call_id=req.tool_call_id,
        )
    except Exception as e:
        raise HTTPException(500, f"append error: {e}") from e
    return {"ok": True}


@router.delete("/short/{scope}")
async def clear_short(scope: str) -> dict:
    scoped = _scoped()
    short = _short(scoped)
    if short is None:
        raise HTTPException(503, "agent_loop not attached")
    if not hasattr(short, "clear"):
        raise HTTPException(404, "no short_term memory")
    try:
        short.clear(scope)
    except Exception as e:
        raise HTTPException(500, f"clear error: {e}") from e
    return {"ok": True, "scope": scope}


# ---------------- 长期 ----------------

@router.get("/long")
async def long_query(
    scope: str = Query(...),
    query: str = Query(...),
    top_k: int = Query(5, ge=1, le=50),
) -> dict:
    long = _long(_scoped())
    if long is None:
        raise HTTPException(503, "no long_term memory")
    if not hasattr(long, "recall"):
        raise HTTPException(404, "no long_term memory")
    try:
        items = long.recall(scope, query, top_k=top_k)
    except Exception as e:
        raise HTTPException(500, f"recall error: {e}") from e
    return {"scope": scope, "query": query, "count": len(items), "items": [to_jsonable(i) for i in items]}


class LongAddRequest(BaseModel):
    scope: str
    text: str
    metadata: dict[str, Any] = {}


@router.post("/long")
async def long_add(req: LongAddRequest) -> dict:
    long = _long(_scoped())
    if long is None:
        raise HTTPException(503, "no long_term memory")
    if not hasattr(long, "add"):
        raise HTTPException(404, "no long_term memory")
    try:
        item_id = long.add(scope=req.scope, text=req.text, metadata=req.metadata)
    except Exception as e:
        raise HTTPException(500, f"add error: {e}") from e
    return {"ok": True, "id": item_id}


# ---------------- SOUL ----------------

@router.get("/soul")
async def get_soul(base: str = "") -> dict:
    soul = _soul(_scoped())
    if soul is None:
        raise HTTPException(404, "no soul loader")
    try:
        rendered = soul.render_system_prompt(base=base or "")
    except Exception as e:
        raise HTTPException(500, f"render error: {e}") from e
    return {"rendered": rendered}


@router.post("/soul/reload")
async def reload_soul() -> dict:
    soul = _soul(_scoped())
    if soul is None:
        raise HTTPException(404, "no soul loader")
    try:
        docs = soul.reload()
    except Exception as e:
        raise HTTPException(500, f"reload error: {e}") from e
    paths = [str(getattr(d, "path", d)) for d in docs]
    return {"reloaded": True, "doc_count": len(docs), "paths": paths}
