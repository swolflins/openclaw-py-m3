"""/v1/channels 列出 / 启停 / 重启。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from openclaw.gateway.deps import get_deps

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
async def channel_send(req: SendRequest) -> dict:
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
    try:
        await target.send(req.session_id, req.text)
    except Exception as e:
        raise HTTPException(500, f"send error: {type(e).__name__}: {e}") from e
    return {"ok": True, "channel": req.name, "session_id": req.session_id}
