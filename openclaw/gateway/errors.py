"""全局异常处理 — 防止外露内部异常细节(SEC-11)。

策略:
- 未处理异常 → 500 + 通用消息(不外露 e / 堆栈)
- HTTPException → 透传(由路由 raise)
- RequestValidationError → 422 + 简化字段
- 详细异常 → 写 server log(供调试)
- **SEC-11 修复**:与 RequestIDMiddleware 共享 request_id,客户端反馈时给一个
"""
from __future__ import annotations

import traceback
import uuid

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from openclaw.core.logging import get_logger

logger = get_logger(__name__)


def _err_id(request: Request) -> str:
    return getattr(request.state, "request_id", None) or uuid.uuid4().hex[:12]


def register_error_handlers(app: FastAPI) -> None:
    """注册到 FastAPI app。"""

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        # 422 保持原样 — 这是用户输入错,需要告诉他们
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": exc.errors()},
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        err_id = _err_id(request)
        logger.error(
            "gateway_unhandled_error",
            request_id=err_id,
            path=request.url.path,
            method=request.method,
            exc_type=type(exc).__name__,
            exc_message=str(exc),
            traceback=traceback.format_exc(),
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "detail": "internal server error",
                "request_id": err_id,   # 新名字(SEC-11)
                "error_id": err_id,     # 旧名字 — 旧测试/客户端兼容
            },
        )
