"""Gateway 鉴权:简单的 Bearer token 校验。

设计目标:
- 最小依赖,不引入完整 OAuth/JWT
- 配置驱动:从环境变量 `OPENCLAW_GATEWAY_TOKEN` 读(可多个,逗号分隔)
- 默认开启(未配置 token 时**仍允许**运行,但日志会警告 — 不破坏本地开发)
- 生产环境**必须**显式设置 token(README/启动 banner 会提示)

协议:
- Header: `Authorization: Bearer <token>`
- 也接受 `X-Gateway-Token: <token>`(方便脚本/SDK)
- 白名单端点: `/healthz`, `/`, `/docs`, `/openapi.json`, `/redoc`, `/ui/*`
- 任一配置 token 命中即通过
"""
from __future__ import annotations

import hmac
import os
from typing import Iterable

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from openclaw.core.logging import get_logger

logger = get_logger(__name__)

# 哪些路径不需要 token
_PUBLIC_PREFIXES = (
    "/healthz",
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/ui",
    "/favicon.ico",
)
_PUBLIC_EXACT = {"/", ""}

# NEW-1:环境变量名 — 当值为 "production" / "prod" 时强制要求 token
_ENV_VAR = "OPENCLAW_GATEWAY_ENV"
_PROD_VALUES = {"production", "prod"}


def is_production_mode() -> bool:
    """NEW-1:返回当前是否处于生产模式。"""
    return os.environ.get(_ENV_VAR, "").strip().lower() in _PROD_VALUES


def require_token_in_production() -> None:
    """NEW-1:生产模式下,启动时若无 token 则**直接抛错,拒绝启动**。

    dev / test 模式下不抛错(只 warning),保持向后兼容。
    """
    if not is_production_mode():
        return
    cfg = _configured_tokens()
    if not cfg:
        raise RuntimeError(
            f"[NEW-1] 检测到 {_ENV_VAR}=production,但 OPENCLAW_GATEWAY_TOKEN 未设置。"
            "为防止未鉴权部署,启动被拒绝。请设置: "
            "export OPENCLAW_GATEWAY_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
        )
    if any(len(t) < 16 for t in cfg):
        # 太短的 token 即使配置了也警告,但不阻断(避免误杀)
        logger.warning(
            "gateway_token_too_short:检测到生产环境 token 长度 < 16,建议改用 secrets.token_urlsafe(32)"
        )


def _is_public(path: str) -> bool:
    """判断 path 是否在白名单(无需 token)。"""
    if path in _PUBLIC_EXACT:
        return True
    return any(path == p or path.startswith(p + "/") for p in _PUBLIC_PREFIXES)


def _configured_tokens() -> list[str]:
    """从环境变量读取 token 列表。逗号 / 空白 分隔。"""
    raw = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "")
    if not raw.strip():
        return []
    return [t.strip() for t in raw.replace(",", " ").split() if t.strip()]


def _extract_token(request: Request) -> str | None:
    """从 Authorization Bearer 或 X-Gateway-Token 取 token。"""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    alt = request.headers.get("x-gateway-token", "").strip()
    return alt or None


def _check_token(provided: str, configured: Iterable[str]) -> bool:
    """用 hmac.compare_digest 防时序攻击。"""
    for t in configured:
        if hmac.compare_digest(provided, t):
            return True
    return False


class AuthMiddleware(BaseHTTPMiddleware):
    """请求级鉴权:未通过 → 401,公共路径放行。"""

    def __init__(self, app, *, tokens: list[str] | None = None) -> None:
        super().__init__(app)
        # 允许测试时直接传(否则走 env)
        self._tokens = tokens if tokens is not None else _configured_tokens()
        if not self._tokens:
            logger.warning(
                "gateway_auth_disabled:OPENCLAW_GATEWAY_TOKEN 未设置,"
                "所有 /v1/* 端点当前可被未认证访问(只用于本地开发)。"
            )

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if _is_public(path):
            return await call_next(request)

        # 没配置 token → 放行(仅 dev 模式);但记 warning
        if not self._tokens:
            return await call_next(request)

        provided = _extract_token(request)
        if not provided or not _check_token(provided, self._tokens):
            logger.info("gateway_auth_rejected", path=path, has_token=bool(provided))
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "missing or invalid gateway token"},
                headers={"WWW-Authenticate": 'Bearer realm="openclaw-gateway"'},
            )
        return await call_next(request)


def install_auth(app, tokens: list[str] | None = None) -> None:
    """把 AuthMiddleware 挂到 FastAPI app(简单工厂,测试用)。"""
    app.add_middleware(AuthMiddleware, tokens=tokens)


def require_token_or_403(provided: str | None) -> None:
    """同步端点使用的辅助:无 token 抛 403(适用于独立端点的二次鉴权)。"""
    cfg = _configured_tokens()
    if not cfg:
        return  # dev 模式放行
    if not provided or not _check_token(provided, cfg):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="missing or invalid gateway token",
        )
