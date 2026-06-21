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
from starlette.middleware.cors import CORSMiddleware
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
    """**SEC-12 修复**:用 route template(``/v1/chat/{id}``)做 label,而非 raw URL。

    **Phase 25 / b8 修复**:
    修复前 ``MetricsMiddleware.dispatch`` 在异常分支后访问
    ``response.headers[...]`` 会 NameError,导致 500 响应完全丢失耗时 header
    和 X-Request-Id 透传 — 上层兜底的 500 包既无 trace_id 也无耗时,排查困难。

    修法:用 ``try/except/finally`` 包住 metrics 写入:
    1. **不**再访问不存在的 ``response`` 局部变量(避免 NameError)
    2. 异常时构造一个 500 ``JSONResponse`` 注入 ``X-Request-Id`` +
       ``X-Response-Time-Ms``,让客户端始终能拿到 trace_id
    3. 异常仍被记录(metrics counter status=500)+ logger.exception 落 traceback
    """

    async def dispatch(self, request, call_next):
        t0 = time.time()
        status: int = 500
        response = None
        try:
            response = await call_next(request)
            status = response.status_code
            # 成功路径:在 finally 之前先注入耗时 header
            try:
                response.headers["X-Response-Time-Ms"] = str(
                    int((time.time() - t0) * 1000)
                )
            except Exception:  # pragma: no cover
                logger.exception("failed to set X-Response-Time-Ms header")
            return response
        except Exception as exc:
            # 异常路径:不访问 ``response``(可能未构造)→ 避免 NameError
            status = 500
            # 与 ``errors.register_error_handlers`` 中的 unhandled 异常处理
            # 保持一致:1) logger.error 带 traceback;2) 返回 500 + 通用
            # ``detail`` + ``error_id``(供客户端反馈)+ ``request_id``(新名)
            # 3) 挂上 ``X-Request-Id`` + ``X-Response-Time-Ms`` 头。
            err_id = (
                getattr(request.state, "request_id", None) or uuid.uuid4().hex[:12]
            )
            logger.error(
                "gateway_unhandled_error",
                request_id=err_id,
                path=request.url.path,
                method=request.method,
                exc_type=type(exc).__name__,
                exc_message=str(exc),
            )
            headers = {
                "X-Response-Time-Ms": str(int((time.time() - t0) * 1000)),
                "X-Request-Id": err_id,
            }
            return JSONResponse(
                status_code=500,
                content={
                    "detail": "internal server error",
                    "request_id": err_id,
                    "error_id": err_id,  # 旧名字 — 旧测试/客户端兼容(SEC-11)
                },
                headers=headers,
            )
        finally:
            # 无论成功还是异常,都要写 metrics 计数器
            try:
                path = _normalize_path(request.url.path)
                m.http_requests_total.inc(
                    method=request.method,
                    path=path,
                    status=str(status),
                )
            except Exception:  # pragma: no cover
                logger.exception("metrics inc failed")


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


def _validate_cors_origin(origin: str) -> str:
    """校验单个 CORS origin 格式,返回清洗后的 origin;非法则抛 ValueError。

    **Phase 25 review follow-up**:
    之前 ``OPENCLAW_CORS_ORIGINS`` 直接 ``split(",")`` 拼进 ``allow_origins``,
    无任何校验。配合 ``allow_credentials=True`` 使用时,一个误配(尤其是
    ``*`` 通配)就会变成"任意源 + 携带凭据"的经典 CSRF 跨源组合拳。

    规则:
    - 拒绝 ``*`` 通配 —— 它与 ``allow_credentials=True`` 互斥(浏览器会拒,
      但 starlette 仍可能回显具体 origin),启动期硬拒最安全;
    - 必须是合法 URL,scheme 限 ``http`` / ``https``;
    - 必须有 host(netloc);端口 / 路径可选。
    """
    o = origin.strip()
    if not o:
        raise ValueError("CORS origin 为空")
    if o == "*":
        raise ValueError(
            "CORS origin 不允许使用 '*' 通配:它与 allow_credentials=True 互斥"
            "(任意源 + 携带凭据 = CSRF)。请改为显式列出可信 origin。"
        )
    from urllib.parse import urlparse

    parsed = urlparse(o)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"CORS origin 格式非法 {o!r}:scheme 必须是 http/https"
            f"(例:'https://app.example.com')"
        )
    if not parsed.netloc or not parsed.hostname:
        raise ValueError(
            f"CORS origin 格式非法 {o!r}:缺少 host(例:'https://app.example.com')"
        )
    # 拒绝 host 里的通配(如 https://*.example.com):配合 allow_credentials=True
    # 同样是跨源 CSRF 风险。要做"模式匹配"请走 allow_origin_regex(dev 默认
    # 已对 localhost/127.0.0.1 开了端口通配),不要把通配塞进 allow_origins。
    if "*" in parsed.netloc:
        raise ValueError(
            f"CORS origin 格式非法 {o!r}:host 不允许通配 '*'。"
            "需要模式匹配请用 allow_origin_regex(而非 allow_origins)。"
        )
    return o


def _resolve_cors_origins() -> list[str]:
    """解析允许的 CORS origin 列表。

    行为(Phase 25/b9 + review follow-up):
    - **生产模式** ``is_production_mode()`` → 空列表(禁 CORS,任何跨域直接拒)。
    - dev / test 模式:
      - 默认允许 ``http://localhost:*`` + ``http://127.0.0.1:*``(具体端口由浏览器匹配)。
      - 额外 origin 通过 ``OPENCLAW_CORS_ORIGINS`` 注入(逗号分隔)。
    - **review follow-up**:env 注入的 origin 走 ``_validate_cors_origin`` 强校验
      (拒绝 ``*`` 与格式非法值),misconfig 启动期 fail-fast 而不是静默放行。
    """
    from openclaw.gateway.auth import is_production_mode
    if is_production_mode():
        # 生产模式:关 CORS(任何跨域浏览器请求会被拒)
        return []
    origins: list[str] = [
        # 用正则形式更准确,但 starlette 的 CORSMiddleware 不支持 regex list,
        # 走 allow_origin_regex 才支持模式。这里给个常用 http(s) 形式作为兜底;
        # 想更严格可只放具体 host(不指定端口)。
        "http://localhost",
        "http://127.0.0.1",
    ]
    extra = os.environ.get("OPENCLAW_CORS_ORIGINS", "").strip()
    if extra:
        for o in extra.split(","):
            o = o.strip()
            if not o:
                continue
            # 强校验:非法 origin(含 '*')启动期 fail-fast,避免 CSRF 误配。
            origins.append(_validate_cors_origin(o))
    return origins


def _resolve_cors_origin_regex() -> str:
    """dev 模式下用正则兜底,允许任何端口的 localhost / 127.0.0.1。

    例:http://localhost:3000 / http://127.0.0.1:5173 都会被放行。
    """
    from openclaw.gateway.auth import is_production_mode
    if is_production_mode():
        # 生产模式:不挂正则(等效 allow_origins=[])
        return ""
    # 匹配 http(s)://localhost[:port] 与 http(s)://127.0.0.1[:port]
    return r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$"


def create_app(
    deps: GatewayDeps | None = None,
    *,
    rate_limiter: RateLimiter | None | type(...) = ...,  # ... = use default
    host: str | None = None,
) -> FastAPI:
    """工厂函数:可以注入自定义 deps(测试用)。

    ``rate_limiter`` 控制限流中间件行为:
    - 传 ``None`` → 关掉限流(测试/本地开发)
    - 传 ``RateLimiter`` 实例 → 用这个 limiter
    - 不传 / 传 Ellipsis → 走 module-level 单例(env 控制)

    ``host``(Phase 25):显式传入监听地址 — 0.0.0.0 + 无 token 视为生产部署,
    启动期 fail-fast。默认从 ``OPENCLAW_GATEWAY_HOST`` env 读;测试可显式传。

    Phase 25/b9:
    - 挂 CORS 中间件:dev 默认允许 ``localhost / 127.0.0.1``,prod 关闭。
    - 启动时若 ``is_production_mode()`` → ``app.docs_url / redoc_url / openapi_url`` 全部置 None。
    """
    # NEW-1:生产模式无 token → 启动期直接拒绝
    from openclaw.gateway.auth import require_token_in_production, is_production_mode
    require_token_in_production()

    # Phase 25:host=0.0.0.0 但 token 未配置 → 启动期 fail-fast
    # 0.0.0.0 视为对外暴露(类似 production),127.0.0.1 仍允许 dev。
    _host = host if host is not None else os.environ.get("OPENCLAW_GATEWAY_HOST", "127.0.0.1")
    _token_raw = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "").strip()
    if _host == "0.0.0.0" and not _token_raw:
        raise RuntimeError(
            "[Phase 25] 检测到 host=0.0.0.0 但 OPENCLAW_GATEWAY_TOKEN 未设置。"
            "为防止未鉴权部署,启动被拒绝。请设置: "
            "export OPENCLAW_GATEWAY_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
        )

    # Phase 25/b9:prod 模式关 docs(避免暴露内部 API 描述)
    _docs_url: str | None = "/docs"
    _redoc_url: str | None = "/redoc"
    _openapi_url: str | None = "/openapi.json"
    if is_production_mode():
        _docs_url = None
        _redoc_url = None
        _openapi_url = None

    app = FastAPI(
        title="OpenClaw Gateway",
        version="0.1.0",
        description="OpenClaw Python 的统一 HTTP 入口(Phase 8)。",
        lifespan=_lifespan,
        docs_url=_docs_url,
        redoc_url=_redoc_url,
        openapi_url=_openapi_url,
    )
    if deps is not None:
        from openclaw.gateway.deps import set_deps
        set_deps(deps)

    # 中间件:顺序很重要
    # 1. 鉴权(SEC-1)— 配置 OPENCLAW_GATEWAY_TOKEN 后启用,未配置则仅 dev
    # 2. 限流(SEC-12)— 对 /v1/chat / /v1/channels/send 生效
    # 3. RequestID(SEC-11)— 注入 request_id
    # 4. Metrics(SEC-12)— 收集 path/method/status,降基数
    from openclaw.gateway.auth import install_auth
    # 启动期已 fail-fast,这里把 host 透传给 AuthMiddleware 做双层保险
    install_auth(app, host=_host)
    # 限流:用户显式 None 关掉;否则走 module-level 单例
    rl = _RATE_LIMITER if rate_limiter is ... else rate_limiter
    if rl is None:
        # 传 None 时挂一个 None 限流的 middleware(等同禁掉)
        app.add_middleware(RateLimitMiddleware, rate_limiter=None)
    else:
        app.add_middleware(RateLimitMiddleware, rate_limiter=rl)
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(MetricsMiddleware)

    # Phase 25/b9:CORS — dev 默认允许 localhost / 127.0.0.1,prod 关闭。
    # 注意:必须放在最后一个 ``add_middleware`` —— Starlette 中间件以"后入先出"顺序执行,
    # 我们的目标是让 CORS 的 OPTIONS 预检**先**到达(必须在 AuthMiddleware / RateLimitMiddleware 之前),
    # 所以逻辑上 CORS 是"最外层",即最后 add。
    _cors_origins = _resolve_cors_origins()
    _cors_regex = _resolve_cors_origin_regex()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_origin_regex=_cors_regex or None,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
        allow_headers=["*"],
        max_age=600,
    )

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
