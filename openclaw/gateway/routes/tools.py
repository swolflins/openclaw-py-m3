"""/v1/tools 列出 / 调用 / 审批。"""
from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from openclaw.gateway import metrics as m
from openclaw.gateway.deps import get_deps
from openclaw.gateway.util import to_jsonable

router = APIRouter(prefix="/tools", tags=["tools"])


def _registry():
    deps = get_deps()
    if deps.agent_loop is None:
        return None
    return getattr(deps.agent_loop, "tools", None)


def _check_admin(request: Request) -> None:
    """H2 修复:审批模式切换需要管理员 token。

    - ``OPENCLAW_GATEWAY_ADMIN_TOKEN`` 未配置 → 端点禁用(403)
    - 请求头 ``X-Admin-Token`` 不匹配 → 403
    """
    admin_token = os.environ.get("OPENCLAW_GATEWAY_ADMIN_TOKEN", "").strip()
    if not admin_token:
        raise HTTPException(
            403,
            "approver mode change is disabled: OPENCLAW_GATEWAY_ADMIN_TOKEN not configured",
        )
    provided = request.headers.get("X-Admin-Token", "").strip()
    if not provided or provided != admin_token:
        raise HTTPException(403, "admin token required to change approver mode")


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
        m.tool_calls_total.inc(tool=req.name, approved="true", channel="gateway")
    except PermissionError:
        # 危险工具(EXE/ADMIN)需要审批,会抛 PermissionError
        m.tool_calls_total.inc(tool=req.name, approved="false", channel="gateway")
        # SEC-5/SEC-11:不外露原始 exception message(可能含 token / path / 内部信息)
        raise HTTPException(409, "tool approval required or denied") from None
    except KeyError:
        # SEC-11:不外露原始 exception message
        raise HTTPException(404, "tool not found") from None
    # 其他异常(SEC-11)— 走全局 handler,不外露 stack 跟原 message
    return {"ok": True, "name": req.name, "result": to_jsonable(result)}


class ApproveRequest(BaseModel):
    approved: bool
    # SEC-5 修复:开启"一键全放行"必须传 confirm="CONFIRM" 作为人类意图证明
    confirm: str | None = None


@router.post("/approver")
async def set_approver_mode(req: ApproveRequest, request: Request) -> dict:
    """设置"是否自动批准"。

    安全(SEC-5):
    - 启用"全部放行"必须 confirm="CONFIRM" 否则 403
    - 关闭永远放行(approved=False)无 confirm 要求

    H2 修复:需要管理员 token(X-Admin-Token header)才能切换审批模式,
    防止任意有效 token 关闭全局审批。
    """
    _check_admin(request)  # H2 修复:管理员鉴权
    reg = _registry()
    if reg is None:
        raise HTTPException(503, "agent_loop / tools not attached")
    if req.approved:
        if req.confirm != "CONFIRM":
            raise HTTPException(
                403,
                "enabling 'always approve' requires confirm='CONFIRM' "
                "(SEC-5:防止误操作开启全放行)",
            )
        async def _ok(name: str, args: dict[str, Any]) -> bool:
            return True
        reg.set_approver(_ok)
    else:
        async def _no(name: str, args: dict[str, Any]) -> bool:
            return False
        reg.set_approver(_no)
    return {"approved": req.approved}
