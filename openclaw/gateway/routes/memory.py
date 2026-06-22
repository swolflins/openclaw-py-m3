"""/v1/memory 短期 / 长期 / SOUL。

注意:AgentLoop.memory 实际上是 `ScopedMemory` 实例,
它本身没有 recent_messages / clear / append_turn / recall,
这些方法都在它的子模块上:.short / .long / .soul。
本路由按"先试子模块,没有再试顶层"的策略兼容两种接口。

**Phase 25/a5 per-user 隔离(防横向越权)**:
- 路由层从 request.state 拿 user_id(由 AuthMiddleware 注入),
- 把每个 ``scope`` 自动加上 ``f"{user_id}:{scope}"`` 前缀写入 memory backend。
- 读时只读自己 user_id scope 下的数据。
- 没传 token 时,user_id = "anonymous"(强制隔离,防止无 token 拿到别人的数据)。

**Phase 27 / M5 修复**(SEC-11 对齐):
所有 endpoint 不再 ``raise HTTPException(500, f"X error: {e}")`` 暴露底层异常字符串,
改为 ``logger.exception`` 写完整 traceback + 客户端拿到 ``{detail, error_id}``
(无 traceback)。``error_id`` 来自 ``request.state.request_id``,便于运维 grep 日志。
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from openclaw.core.logging import get_logger
from openclaw.gateway.deps import current_user_id, get_deps
from openclaw.gateway.util import to_jsonable

logger = get_logger(__name__)

router = APIRouter(prefix="/memory", tags=["memory"])


def _user_scoped(request: Request, scope: str) -> str:
    """Phase 25/a5:把 caller 的 user_id 拼到 scope 前,形成 ``user_id:scope``。

    例:user_id="alice", scope="session:42" → "alice:session:42"。
    这样 backend 不知道有 per-user 的事也无所谓(它只认 scope key)。
    """
    return f"{current_user_id(request)}:{scope}"


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


# Phase 27 / M5:统一异常脱敏辅助
def _safe_http_500(request: Request, op: str, exc: Exception) -> "HTTPException":
    """生成 500 响应 + 服务端 logger.exception 记录。

    不再把 ``str(exc)`` 直接塞进 detail(可能泄漏栈/路径/凭据),
    而是返回 ``{detail, error_id}``,error_id 来自 request.state.request_id(RequestIDMiddleware 注入)。
    """
    err_id = getattr(request.state, "request_id", None) or uuid.uuid4().hex[:12]
    logger.exception(
        "memory_route_failed",
        op=op,
        error_id=err_id,
        error_type=type(exc).__name__,
    )
    return HTTPException(
        status_code=500,
        detail={
            "message": "internal error",
            "error_id": err_id,
            "op": op,
        },
    )


@router.get("/short")
async def get_short(
    request: Request, scope: str = Query(...), k: int = Query(20, ge=1, le=200)
) -> dict:
    scoped = _scoped()
    short = _short(scoped)
    if short is None:
        raise HTTPException(503, "agent_loop not attached")
    if not hasattr(short, "recent_messages"):
        raise HTTPException(404, "no short_term memory")
    # Phase 25/a5:per-user 隔离 — 只读 caller 的 scope
    user_scope = _user_scoped(request, scope)
    try:
        msgs = short.recent_messages(user_scope, k=k)
    except Exception as e:
        raise _safe_http_500(request, "memory.short.get", e) from e
    return {
        "scope": scope,
        "user_id": current_user_id(request),
        "count": len(msgs),
        "messages": [to_jsonable(m) for m in msgs],
    }


class ShortAppendRequest(BaseModel):
    scope: str
    role: str
    content: str
    name: str | None = None
    tool_call_id: str | None = None


@router.post("/short")
async def append_short(req: ShortAppendRequest, request: Request) -> dict:
    scoped = _scoped()
    short = _short(scoped)
    if short is None:
        raise HTTPException(503, "agent_loop not attached")
    if not hasattr(short, "append_turn"):
        raise HTTPException(404, "no short_term memory")
    # Phase 25/a5:per-user 隔离 — 写到 caller 自己的 scope
    user_scope = _user_scoped(request, req.scope)
    try:
        await short.append_turn(
            scope=user_scope, role=req.role, content=req.content,
            name=req.name, tool_call_id=req.tool_call_id,
        )
    except Exception as e:
        raise _safe_http_500(request, "memory.short.append", e) from e
    return {"ok": True, "scope": req.scope, "user_id": current_user_id(request)}


@router.delete("/short/{scope}")
async def clear_short(scope: str, request: Request) -> dict:
    scoped = _scoped()
    short = _short(scoped)
    if short is None:
        raise HTTPException(503, "agent_loop not attached")
    if not hasattr(short, "clear"):
        raise HTTPException(404, "no short_term memory")
    # Phase 25/a5:per-user 隔离 — 只清 caller 自己的 scope
    user_scope = _user_scoped(request, scope)
    try:
        short.clear(user_scope)
    except Exception as e:
        raise _safe_http_500(request, "memory.short.clear", e) from e
    return {"ok": True, "scope": scope, "user_id": current_user_id(request)}


# ---------------- 长期 ----------------

@router.get("/long")
async def long_query(
    request: Request,
    scope: str = Query(...),
    query: str = Query(...),
    top_k: int = Query(5, ge=1, le=50),
) -> dict:
    long = _long(_scoped())
    if long is None:
        raise HTTPException(503, "no long_term memory")
    if not hasattr(long, "recall"):
        raise HTTPException(404, "no long_term memory")
    # Phase 25/a5:per-user 隔离 — 只在 caller 的 scope 内 recall
    user_scope = _user_scoped(request, scope)
    try:
        items = long.recall(user_scope, query, top_k=top_k)
    except Exception as e:
        raise _safe_http_500(request, "memory.long.query", e) from e
    return {
        "scope": scope,
        "user_id": current_user_id(request),
        "query": query,
        "count": len(items),
        "items": [to_jsonable(i) for i in items],
    }


class LongAddRequest(BaseModel):
    scope: str
    text: str
    metadata: dict[str, Any] = {}


@router.post("/long")
async def long_add(req: LongAddRequest, request: Request) -> dict:
    long = _long(_scoped())
    if long is None:
        raise HTTPException(503, "no long_term memory")
    if not hasattr(long, "add"):
        raise HTTPException(404, "no long_term memory")
    # Phase 25/a5:per-user 隔离 — 写到 caller 自己的 scope
    user_scope = _user_scoped(request, req.scope)
    try:
        item_id = long.add(scope=user_scope, text=req.text, metadata=req.metadata)
    except Exception as e:
        raise _safe_http_500(request, "memory.long.add", e) from e
    return {
        "ok": True,
        "id": item_id,
        "scope": req.scope,
        "user_id": current_user_id(request),
    }


# ---------------- SOUL ----------------

@router.get("/soul")
async def get_soul(request: Request, base: str = "") -> dict:
    soul = _soul(_scoped())
    if soul is None:
        raise HTTPException(404, "no soul loader")
    try:
        rendered = soul.render_system_prompt(base=base or "")
    except Exception as e:
        raise _safe_http_500(request, "memory.soul.get", e) from e
    return {"rendered": rendered}


@router.post("/soul/reload")
async def reload_soul(request: Request) -> dict:
    soul = _soul(_scoped())
    if soul is None:
        raise HTTPException(404, "no soul loader")
    try:
        docs = soul.reload()
    except Exception as e:
        raise _safe_http_500(request, "memory.soul.reload", e) from e
    paths = [str(getattr(d, "path", d)) for d in docs]
    return {"reloaded": True, "doc_count": len(docs), "paths": paths}
