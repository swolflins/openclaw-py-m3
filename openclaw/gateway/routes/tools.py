"""/v1/tools 列出 / 调用 / 审批。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from openclaw.gateway.deps import get_deps
from openclaw.gateway.util import to_jsonable

router = APIRouter(prefix="/tools", tags=["tools"])


def _registry():
    deps = get_deps()
    if deps.agent_loop is None:
        return None
    return getattr(deps.agent_loop, "tools", None)


@router.get("")
async def list_tools() -> dict:
    reg = _registry()
    if reg is None:
        return {"tools": [], "count": 0}
    try:
        specs = reg.list_tools()
    except Exception as e:
        raise HTTPException(500, f"list_tools error: {e}") from e
    return {
        "count": len(specs),
        "tools": [to_jsonable(s) for s in specs],
    }


class ToolCallRequest(BaseModel):
    name: str
    arguments: dict[str, Any] = {}


@router.post("/call")
async def call_tool(req: ToolCallRequest) -> dict:
    reg = _registry()
    if reg is None:
        raise HTTPException(503, "agent_loop / tools not attached")
    try:
        result = await reg.call(req.name, req.arguments)
    except PermissionError as e:
        # 危险工具(EXE/ADMIN)需要审批,会抛 PermissionError
        raise HTTPException(409, f"approval required: {e}") from e
    except KeyError as e:
        raise HTTPException(404, f"tool not found: {e}") from e
    except Exception as e:
        raise HTTPException(500, f"call error: {type(e).__name__}: {e}") from e
    return {"name": req.name, "result": to_jsonable(result)}


class ApproveRequest(BaseModel):
    approved: bool


@router.post("/approver")
async def set_approver_mode(req: ApproveRequest) -> dict:
    """设置"是否自动批准"。

    简单实现:把 registry 的 approver 设为 (always allow / always deny)。
    生产环境应该接 Web 端审批流(WebUI 弹窗 → 提交到本端)。
    """
    reg = _registry()
    if reg is None:
        raise HTTPException(503, "agent_loop / tools not attached")
    if req.approved:
        async def _ok(name: str, args: dict[str, Any]) -> bool:
            return True
        reg.set_approver(_ok)
    else:
        async def _no(name: str, args: dict[str, Any]) -> bool:
            return False
        reg.set_approver(_no)
    return {"approved": req.approved}
