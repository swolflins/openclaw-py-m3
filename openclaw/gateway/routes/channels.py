"""/v1/channels 列出 / 启停 / 重启。

**SEC-11 修复**:
- ``channel_send`` 不再回传 ``str(e)`` 给客户端
- 错误细节写 server log,客户端只看到 request_id
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from openclaw.core.logging import get_logger
from openclaw.gateway.deps import get_deps

logger = get_logger(__name__)
router = APIRouter(prefix="/channels", tags=["channels"])


# 由 create_app 注入(避免循环 import)
_MANAGER_KEY = "channel_manager"


def set_channel_manager(manager: Any) -> None:
    deps = get_deps()
    deps.extra[_MANAGER_KEY] = manager


def get_channel_manager() -> Any:
    return get_deps().extra.get(_MANAGER_KEY)


@router.get("")
async def list_channels() -> dict:
    mgr = get_channel_manager()
    if mgr is None:
        return {"channels": [], "count": 0}
    items = []
    for ch in mgr.channels():
        items.append({
            "name": getattr(ch, "name", "?"),
            "running": not getattr(ch, "_stopped", type("X", (), {"is_set": lambda s: True}))().is_set(),
            "agent_attached": getattr(ch, "agent_loop", None) is not None,
            "auto_reply_attached": getattr(ch, "auto_reply", None) is not None,
        })
    return {"count": len(items), "channels": items}


class SendRequest(BaseModel):
    name: str
    session_id: str
    text: str


@router.post("/send")
async def channel_send(req: SendRequest, request: Request) -> dict:
    """通过指定 channel 主动发一条(测试 / 通知用)。"""
    mgr = get_channel_manager()
    if mgr is None:
        raise HTTPException(503, "channel_manager not attached")
    target = None
    for ch in mgr.channels():
        if getattr(ch, "name", None) == req.name:
            target = ch
            break
    if target is None:
        raise HTTPException(404, f"channel {req.name!r} not registered")
    request_id = getattr(request.state, "request_id", None) or uuid.uuid4().hex[:12]
    try:
        await target.send(req.session_id, req.text)
    except Exception as e:
        # SEC-11 修复:不暴露 str(e) 给客户端;详细 trace 入 server log
        logger.exception(
            "channel_send_error",
            request_id=request_id,
            channel=req.name,
            session_id=req.session_id,
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
    return {"ok": True, "channel": req.name, "session_id": req.session_id}
