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
import hashlib
import json
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

# H1 修复:dev 模式显式 opt-in — 未配置 token 时必须设 OPENCLAW_GATEWAY_DEV=1
# 才允许无 token 运行(防止 uvicorn --host 0.0.0.0 绕过 host 检查)
_DEV_ENV = "OPENCLAW_GATEWAY_DEV"


def is_production_mode() -> bool:
    """NEW-1:返回当前是否处于生产模式。"""
    return os.environ.get(_ENV_VAR, "").strip().lower() in _PROD_VALUES


def is_dev_mode() -> bool:
    """H1 修复:返回是否显式开启了 dev 模式(允许无 token)。"""
    return os.environ.get(_DEV_ENV, "").strip() in {"1", "true", "yes"}


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


# Phase 25 review follow-up:稳定的 user_id 来源,解决 "token 轮换 → per-user
# 隔离蒸发" 的问题(旧 fallback 用 token[:16] 当 user_id,token 改 1 字符就换用户)。
_USER_ID_ENV = "OPENCLAW_GATEWAY_USER_ID"
_TOKEN_TO_USER_ENV = "OPENCLAW_GATEWAY_TOKEN_TO_USER"


def _configured_user_id() -> str | None:
    """``OPENCLAW_GATEWAY_USER_ID``:稳定的单用户 user_id(轮换 token 也不变)。

    适用单用户部署:设了之后所有通过 gateway token 鉴权的请求都用同一个
    user_id,token 轮换不会丢 memory/sessions。多用户部署请改用
    ``OPENCLAW_GATEWAY_TOKEN_TO_USER`` 显式映射。
    """
    val = os.environ.get(_USER_ID_ENV, "").strip()
    return val or None


def _configured_token_to_user() -> dict[str, str]:
    """``OPENCLAW_GATEWAY_TOKEN_TO_USER``:JSON 对象形式的 token→user_id 映射。

    多用户部署用这个给每个 token 配稳定 user_id,避免 fallback。例::

        OPENCLAW_GATEWAY_TOKEN_TO_USER='{"token-a...":"alice","token-b...":"bob"}'

    解析失败 / 非 JSON 对象时记录 warning 并返回空 dict(不阻断启动)。
    """
    raw = os.environ.get(_TOKEN_TO_USER_ENV, "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        logger.warning(
            "gateway_token_to_user_parse_failed:%s 解析失败,期望 JSON 对象;已忽略",
            _TOKEN_TO_USER_ENV,
        )
        return {}
    if not isinstance(data, dict):
        logger.warning(
            "gateway_token_to_user_not_object:%s 不是 JSON 对象;已忽略",
            _TOKEN_TO_USER_ENV,
        )
        return {}
    return {str(k): str(v) for k, v in data.items()}


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

    Phase 25 review follow-up:增加 ``user_id`` 稳定标识(来自
    ``OPENCLAW_GATEWAY_USER_ID``)。token 轮换时,旧 fallback ``token[:16]``
    会变 → per-user 隔离蒸发。现在优先级为:
    1) 显式 ``user_id``(单用户稳定,轮换安全);
    2) ``token_to_user`` 映射(多用户显式映射);
    3) ``token[:16]`` fallback(仅当以上都没配,记 warning 提示轮换不稳定)。
    """

    def __init__(
        self,
        app,
        *,
        tokens: list[str] | None = None,
        host: str | None = None,
        token_to_user: dict[str, str] | None = None,
        user_id: str | None = None,
    ) -> None:
        super().__init__(app)
        # 允许测试时直接传(否则走 env)
        self._tokens = tokens if tokens is not None else _configured_tokens()
        # H1 修复:默认 fail-closed — 无 token 时必须显式 OPENCLAW_GATEWAY_DEV=1
        # 旧逻辑:仅当显式传 host="0.0.0.0" 才 fail-fast,但 uvicorn --host 0.0.0.0
        # 时 create_app() 不感知绑定地址,检查被绕过。
        # 新逻辑:无 token + 非 dev 模式 → 直接拒绝启动(不论 host)
        # 注意:dev 模式下允许 0.0.0.0 + 无 token(本地开发/Docker smoke test)
        if not self._tokens:
            if not is_dev_mode():
                if host is not None and host == "0.0.0.0":
                    raise RuntimeError(
                        "[Phase 25] 检测到 host=0.0.0.0 但 OPENCLAW_GATEWAY_TOKEN 未设置。"
                        "为防止未鉴权部署,启动被拒绝。请设置: "
                        "export OPENCLAW_GATEWAY_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
                        "\n  或本地开发:export OPENCLAW_GATEWAY_DEV=1"
                    )
                raise RuntimeError(
                    "[H1] OPENCLAW_GATEWAY_TOKEN 未设置且未显式开启 dev 模式。"
                    "为防止未鉴权部署,启动被拒绝。请执行以下任一操作:\n"
                    "  1. 设置 token:export OPENCLAW_GATEWAY_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')\n"
                    "  2. 本地开发:export OPENCLAW_GATEWAY_DEV=1"
                )
            logger.warning(
                "gateway_auth_disabled:OPENCLAW_GATEWAY_DEV=1 已显式开启,"
                "所有 /v1/* 端点当前可被未认证访问(仅用于本地开发)。"
            )
        # Phase 25 review follow-up:稳定的 user_id 优先(轮换安全)。
        # 显式传参优先,否则读 OPENCLAW_GATEWAY_USER_ID。
        self._user_id = user_id if user_id is not None else _configured_user_id()
        # Phase 25/a5:token → user_id 映射(多用户)。显式传参优先,否则读 env。
        self._token_to_user = (
            dict(token_to_user) if token_to_user is not None else _configured_token_to_user()
        )
        if self._user_id and self._token_to_user:
            logger.warning(
                "gateway_user_id_and_token_to_user_both_set:"
                "%s 与 %s 同时设置,user_id(单用户)优先,token_to_user 映射将被忽略。",
                _USER_ID_ENV, _TOKEN_TO_USER_ENV,
            )
        if self._tokens and not self._user_id and not self._token_to_user:
            # 仍走 token[:16] fallback —— 提醒运维:这是轮换不稳定的,生产应改用
            # OPENCLAW_GATEWAY_USER_ID(单用户)或 OPENCLAW_GATEWAY_TOKEN_TO_USER(多用户)。
            logger.warning(
                "gateway_user_id_fallback_unstable:未配置 %s / %s,"
                "user_id 退化为 token[:16] —— token 轮换会导致 memory/sessions 切换用户。"
                "生产环境请设置 OPENCLAW_GATEWAY_USER_ID(单用户)或"
                " OPENCLAW_GATEWAY_TOKEN_TO_USER(多用户)以获得轮换稳定的 user_id。",
                _USER_ID_ENV, _TOKEN_TO_USER_ENV,
            )

    @staticmethod
    def _resolve_user_id(
        token: str | None,
        token_to_user: dict[str, str],
        user_id: str | None = None,
    ) -> str:
        """根据 token 解析 user_id(轮换稳定优先)。

        优先级:
        - 显式 ``user_id``(OPENCLAW_GATEWAY_USER_ID)→ 用它(单用户,轮换安全)
        - token 为 None(无 token)→ "anonymous"
        - token 在 ``token_to_user`` 映射里 → 用映射值(多用户,轮换安全)
        - 都不命中 → ``sha256(token)[:16]``(同 token 同 user,不泄露原始 token)
        """
        if user_id:
            return user_id
        if token is None:
            return "anonymous"
        if token in token_to_user:
            return token_to_user[token]
        # L5 修复:用 sha256 hash 替代 token[:16],防止日志泄露 token 前 16 字符
        return "h_" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]

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
        # 鉴权通过 → 挂 user_id 到 request.state(轮换稳定优先)
        request.state.user_id = self._resolve_user_id(
            provided, self._token_to_user, self._user_id
        )
        return await call_next(request)


def install_auth(
    app,
    tokens: list[str] | None = None,
    host: str | None = None,
    token_to_user: dict[str, str] | None = None,
    user_id: str | None = None,
) -> None:
    """把 AuthMiddleware 挂到 FastAPI app(简单工厂,测试用)。

    Phase 25:``host`` 透传给 AuthMiddleware — 测试用 127.0.0.1 即可绕过 fail-fast。

    Phase 25/a5:``token_to_user`` 透传给 AuthMiddleware — 给 token 配 user_id,
    缺省/未命中时 dispatch 用 ``token[:16]`` 作为 user_id。

    Phase 25 review follow-up:``user_id`` 透传给 AuthMiddleware — 显式稳定
    user_id(单用户,轮换安全)。``tokens`` / ``token_to_user`` / ``user_id``
    未传(None)时各自走对应 env(OPENCLAW_GATEWAY_TOKEN /
    OPENCLAW_GATEWAY_TOKEN_TO_USER / OPENCLAW_GATEWAY_USER_ID)。
    """
    app.add_middleware(
        AuthMiddleware,
        tokens=tokens,
        host=host,
        token_to_user=token_to_user,
        user_id=user_id,
    )


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
