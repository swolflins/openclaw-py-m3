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
    """请求级鉴权:未通过 → 401,公共路径放行。

    Phase 25:增加 ``host`` 显式参数 — 当 host="0.0.0.0" 且 tokens 为空时,
    直接抛 RuntimeError(双层保险)。127.0.0.1 仍允许无 token(纯本地开发)。

    Phase 25/a5:增加 ``token_to_user`` 映射 — 鉴权通过后,
    把 ``request.state.user_id`` 设为该映射的 user_id;
    缺省/找不到时,把 token 自身[:16] 作为 user_id(同 token 同 user)。
    """

    def __init__(
        self,
        app,
        *,
        tokens: list[str] | None = None,
        host: str | None = None,
        token_to_user: dict[str, str] | None = None,
    ) -> None:
        super().__init__(app)
        # 允许测试时直接传(否则走 env)
        self._tokens = tokens if tokens is not None else _configured_tokens()
        # 显式传了 host 才用,否则保持 dev 兼容(允许 0.0.0.0 + 空 token)
        if host is not None and host == "0.0.0.0" and not self._tokens:
            raise RuntimeError(
                "[Phase 25] 检测到 host=0.0.0.0 但 OPENCLAW_GATEWAY_TOKEN 未设置。"
                "为防止未鉴权部署,启动被拒绝。请设置: "
                "export OPENCLAW_GATEWAY_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
            )
        if not self._tokens:
            logger.warning(
                "gateway_auth_disabled:OPENCLAW_GATEWAY_TOKEN 未设置,"
                "所有 /v1/* 端点当前可被未认证访问(只用于本地开发)。"
            )
        # Phase 25/a5:token → user_id 映射(可选)。
        # 没传或没命中映射时,dispatch 里用 token[:16] 作为 user_id。
        self._token_to_user = dict(token_to_user) if token_to_user else {}

    @staticmethod
    def _resolve_user_id(token: str | None, token_to_user: dict[str, str]) -> str:
        """根据 token 解析 user_id。

        - token 为 None(无 token)→ "anonymous"
        - token 在映射里 → 用映射值
        - 都不命中 → 用 ``token[:16]``(同 token 同 user,简单稳定)
        """
        if token is None:
            return "anonymous"
        if token in token_to_user:
            return token_to_user[token]
        return token[:16]

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if _is_public(path):
            # 公开路径也挂一个 user_id(anonymous)以保持下游一致性
            request.state.user_id = "anonymous"
            return await call_next(request)

        # 没配置 token → 放行(仅 dev 模式);但记 warning
        if not self._tokens:
            request.state.user_id = "anonymous"
            return await call_next(request)

        provided = _extract_token(request)
        if not provided or not _check_token(provided, self._tokens):
            logger.info("gateway_auth_rejected", path=path, has_token=bool(provided))
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "missing or invalid gateway token"},
                headers={"WWW-Authenticate": 'Bearer realm="openclaw-gateway"'},
            )
        # 鉴权通过 → 挂 user_id 到 request.state
        request.state.user_id = self._resolve_user_id(provided, self._token_to_user)
        return await call_next(request)


def install_auth(
    app,
    tokens: list[str] | None = None,
    host: str | None = None,
    token_to_user: dict[str, str] | None = None,
) -> None:
    """把 AuthMiddleware 挂到 FastAPI app(简单工厂,测试用)。

    Phase 25:``host`` 透传给 AuthMiddleware — 测试用 127.0.0.1 即可绕过 fail-fast。

    Phase 25/a5:``token_to_user`` 透传给 AuthMiddleware — 给 token 配 user_id,
    缺省/未命中时 dispatch 用 ``token[:16]`` 作为 user_id。
    """
    app.add_middleware(AuthMiddleware, tokens=tokens, host=host, token_to_user=token_to_user)


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
