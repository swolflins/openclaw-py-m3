"""路由聚合 + app 工厂。

**SEC-12 修复**:
- 全局限流中间件:对 ``/v1/chat`` / ``/v1/chat/stream`` 走 RateLimiter(默认 1 req/s burst=3)
- Metrics middleware:用 FastAPI route template(``/v1/chat/{id}``)而非 raw URL,降基数
- Request-ID 中间件:每个请求生成短 id,所有异常 handler 共享
"""
from __future__ import annotations

import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from openclaw.core.logging import get_logger
from openclaw.core.rate_limit import RateLimiter
from openclaw.gateway import metrics as m
from openclaw.gateway.deps import GatewayDeps, get_deps
from openclaw.gateway.metrics import _normalize_path
from openclaw.gateway.routes import channels, chat, health, journal, memory, sessions, skills, tools

logger = get_logger(__name__)

# 默认限流:每秒 1 个,突发 3。可通过 env OPENCLAW_GATEWAY_RL_RATE / _BURST 覆盖
_RL_RATE = float(os.environ.get("OPENCLAW_GATEWAY_RL_RATE", "1.0"))
_RL_BURST = int(os.environ.get("OPENCLAW_GATEWAY_RL_BURST", "3"))
_RL_ENABLED = os.environ.get("OPENCLAW_GATEWAY_RL_DISABLED", "").lower() not in ("1", "true", "yes")
_RATE_LIMITER = RateLimiter(rate=_RL_RATE, burst=_RL_BURST) if _RL_ENABLED else None

# 需要限流的路径前缀
_LIMITED_PREFIXES = ("/v1/chat", "/v1/channels/send")


class RateLimitMiddleware(BaseHTTPMiddleware):
    """**SEC-12**: 限流中间件,默认 1 req/s burst=3。

    可通过 env 关掉(测试用):``OPENCLAW_GATEWAY_RL_DISABLED=1``。
    也可通过 ``rate_limiter=None`` 传 None 关闭。
    """

    def __init__(self, app, *, rate_limiter: RateLimiter | None = None) -> None:
        super().__init__(app)
        # 优先用外部注入,否则用 module-level 单例
        self._limiter = rate_limiter if rate_limiter is not None else _RATE_LIMITER

    async def dispatch(self, request, call_next):
        if self._limiter is None:
            return await call_next(request)
        path = request.url.path
        if not any(path.startswith(p) for p in _LIMITED_PREFIXES):
            return await call_next(request)
        # 用 session_id 或 remote addr 作为 key
        sid = request.headers.get("X-Session-Id") or (request.client.host if request.client else "anon")
        if not self._limiter.allow(sid):
            retry = self._limiter.retry_after(sid)
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limited",
                    "retry_after_seconds": round(retry, 3),
                },
                headers={"Retry-After": str(max(1, int(retry) + 1))},
            )
        return await call_next(request)


class RequestIDMiddleware(BaseHTTPMiddleware):
    """每个请求挂一个短 request_id 到 ``request.state.request_id``。"""

    async def dispatch(self, request, call_next):
        rid = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:12]
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-Id"] = rid
        return response


class MetricsMiddleware(BaseHTTPMiddleware):
    """**SEC-12 修复**:用 route template(``/v1/chat/{id}``)做 label,而非 raw URL。"""

    async def dispatch(self, request, call_next):
        t0 = time.time()
        try:
            response = await call_next(request)
            status = response.status_code
        except Exception:
            status = 500
            raise
        finally:
            path = _normalize_path(request.url.path)
            m.http_requests_total.inc(
                method=request.method,
                path=path,
                status=str(status),
            )
        # 响应头带耗时
        response.headers["X-Response-Time-Ms"] = str(int((time.time() - t0) * 1000))
        return response


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    d = get_deps()
    if d.ready():
        ready_info = "ready(agent_loop=attached)"
    else:
        ready_info = "DEGRADED(agent_loop=missing,/v1/chat will 503)"
    logger.info("gateway_started", status=ready_info)
    yield
    logger.info("gateway_stopped")


def create_app(
    deps: GatewayDeps | None = None,
    *,
    rate_limiter: RateLimiter | None | type(...) = ...,  # ... = use default
) -> FastAPI:
    """工厂函数:可以注入自定义 deps(测试用)。

    ``rate_limiter`` 控制限流中间件行为:
    - 传 ``None`` → 关掉限流(测试/本地开发)
    - 传 ``RateLimiter`` 实例 → 用这个 limiter
    - 不传 / 传 Ellipsis → 走 module-level 单例(env 控制)
    """
    # NEW-1:生产模式无 token → 启动期直接拒绝
    from openclaw.gateway.auth import require_token_in_production
    require_token_in_production()

    app = FastAPI(
        title="OpenClaw Gateway",
        version="0.1.0",
        description="OpenClaw Python 的统一 HTTP 入口(Phase 8)。",
        lifespan=_lifespan,
    )
    if deps is not None:
        from openclaw.gateway.deps import set_deps
        set_deps(deps)

    # 中间件:顺序很重要
    # 1. 鉴权(SEC-1)— 配置 OPENCLAW_GATEWAY_TOKEN 后启用,未配置则仅 dev
    # 2. 限流(SEC-12)— 对 /v1/chat /v1/channels/send 生效
    # 3. RequestID(SEC-11)— 注入 request_id
    # 4. Metrics(SEC-12)— 收集 path/method/status,降基数
    from openclaw.gateway.auth import install_auth
    install_auth(app)
    # 限流:用户显式 None 关掉;否则走 module-level 单例
    rl = _RATE_LIMITER if rate_limiter is ... else rate_limiter
    if rl is None:
        # 传 None 时挂一个 None 限流的 middleware(等同禁掉)
        app.add_middleware(RateLimitMiddleware, rate_limiter=None)
    else:
        app.add_middleware(RateLimitMiddleware, rate_limiter=rl)
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(MetricsMiddleware)

    # 全局异常处理(SEC-11)— 500 错误不外露原始异常消息
    from openclaw.gateway.errors import register_error_handlers
    register_error_handlers(app)

    # 注册路由
    app.include_router(health.router)
    app.include_router(chat.router, prefix="/v1")
    app.include_router(sessions.router, prefix="/v1")
    app.include_router(memory.router, prefix="/v1")
    app.include_router(tools.router, prefix="/v1")
    app.include_router(skills.router, prefix="/v1")
    app.include_router(channels.router, prefix="/v1")
    app.include_router(journal.router, prefix="/v1")

    # 静态 Web UI(挂在 /ui)
    from fastapi.staticfiles import StaticFiles
    from pathlib import Path
    static_dir = Path(__file__).resolve().parent / "static"
    if static_dir.exists():
        app.mount("/ui", StaticFiles(directory=str(static_dir), html=True), name="ui")

        @app.get("/", include_in_schema=False)
        async def root_index() -> dict:
            return {
                "name": "openclaw-gateway",
                "version": "0.1.0",
                "ui": "/ui/",
                "docs": "/docs",
                "healthz": "/healthz",
            }

    return app


# uvicorn 入口:uvicorn openclaw.gateway.app:app
app = create_app()
