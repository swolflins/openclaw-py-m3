"""路由聚合 + app 工厂。

**SEC-12 修复**:
- 全局限流中间件:对 ``/v1/chat`` / ``/v1/chat/stream`` 走 RateLimiter(默认 1 req/s burst=3)
- Metrics middleware:用 FastAPI route template(``/v1/chat/{id}``)而非 raw URL,降基数
- Request-ID 中间件:每个请求生成短 id,所有异常 handler 共享

**Phase 27 / C1 修复**:
``create_app`` 的 ``rate_limiter`` 默认值从 ``type(...)``(ellipsis 类型对象,
仅作为哨兵)改为模块级 ``_DEFAULT_RATE_LIMITER`` 哨兵对象 + 独立
``use_default_rate_limiter: bool`` 开关。消除"用类型对象作 sentinel"的反模式
与"RateLimiter | None | type(...)"异质联合类型告警。
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
_RATE_LIMITER: RateLimiter | None = RateLimiter(rate=_RL_RATE, burst=_RL_BURST) if _RL_ENABLED else None


# Phase 27 / C1:独立 sentinel 对象,替代 type(...) 哨兵。
class _DefaultRateLimiterSentinel:
    """占位符:表示 ``create_app`` 走 module-level 默认限流单例。

    用类对象(而非 ``...`` / ``Ellipsis``)作 sentinel 可以让签名表达为
    ``RateLimiter | None | _DefaultRateLimiterSentinel``,避免 ``type(...)``
    这种"类型对象作值"的反模式 + 异质联合告警。
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover
        return "<DEFAULT_RATE_LIMITER>"


_DEFAULT_RATE_LIMITER = _DefaultRateLimiterSentinel()

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
        # M11 修复:限流 key 优先用 ``X-Forwarded-For``(链首,反代后的真实客户端 IP),
        # 回退 ``X-Real-IP``,再回退 ``client.host``。这让反代后能继续按真实 IP 限流,
        # 避免"同 proxy_addr 多用户共享一桶"绕过。
        # 攻击者可控 X-Forwarded-For 仍然能让 IP 变成攻击者想要的值;但
        # 1) gateway 应只接受可信反代的 X-Forwarded-For(部署文档明示);
        # 2) 即使不设 XFF,原 client.host 行为保留作为最后兜底。
        # **生产部署必须**设 ``OPENCLAW_GATEWAY_TRUSTED_PROXY=1`` 显式开启 XFF 解析,
        # 否则仍用 client.host(防止误信任伪造 XFF)。
        sid = self._resolve_client_id(request)
        # Phase 29 / L9 修复:用 try_consume 同时拿 (allowed, remaining, retry_after)
        # 一次原子操作同时计算三项,避免 allow + retry_after 两次加锁的"非原子读"
        # 内存版与 Redis 版都实现了 ``try_consume``(签名对齐)。
        try:
            allowed, remaining, retry = self._limiter.try_consume(sid)
        except AttributeError:
            # BC 兜底:旧实现(没 try_consume)走 allow + retry_after
            allowed = self._limiter.allow(sid)
            retry = self._limiter.retry_after(sid) if not allowed else 0.0
            remaining = 0.0
        if not allowed:
            headers = {
                "Retry-After": str(max(1, int(retry) + 1)),
                # L9:标准 rate limit headers — 让客户端能感知配额状态
                "X-RateLimit-Limit": str(int(self._limiter.burst)),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(max(1, int(retry) + 1)),
            }
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limited",
                    "retry_after_seconds": round(retry, 3),
                },
                headers=headers,
            )
        # 成功路径:把 X-RateLimit-* 透传到响应头,让客户端做退避
        response = await call_next(request)
        try:
            response.headers["X-RateLimit-Limit"] = str(int(self._limiter.burst))
            response.headers["X-RateLimit-Remaining"] = str(max(0, int(remaining)))
        except Exception:  # pragma: no cover
            logger.exception("rate_limit_headers_set_failed")
        return response

    @staticmethod
    def _resolve_client_id(request) -> str:
        """解析限流用的客户端标识。

        优先级:
        1. ``OPENCLAW_GATEWAY_TRUSTED_PROXY=1`` + ``X-Forwarded-For`` 取首项
           (反代后真实客户端 IP;裸 starlette/FastAPI 不解析,需运维显式开)
        2. ``X-Real-IP``(nginx 等单层反代常用)
        3. ``request.client.host``(直接连接,最后兜底)

        攻击者可在请求里塞任意 ``X-Forwarded-For``;但必须先有 ``TRUSTED_PROXY=1``,
        否则仍走 client.host(攻击者改不了 TCP 源 IP)。
        """
        import os
        if os.environ.get("OPENCLAW_GATEWAY_TRUSTED_PROXY", "").lower() in ("1", "true", "yes"):
            xff = request.headers.get("X-Forwarded-For")
            if xff:
                first = xff.split(",", 1)[0].strip()
                if first:
                    return f"xff:{first}"
            real = request.headers.get("X-Real-IP")
            if real:
                return f"xreal:{real}"
        return f"peer:{request.client.host}" if request.client else "anon"


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
    """Phase 27 / H9 修复:graceful shutdown — 关闭阶段调 channel_manager.stop_all()。

    启动期:
    - 拿 deps,记 ready 状态

    关闭期:
    - stop ChannelManager(若有)→ 走 SIGTERM 风格的 cancel + wait
    - 关 httpx 客户端(providers / channels 都用)→ 防 socket fd 泄露
    - 全程 try/except 防 shutdown 异常阻断 process exit
    """
    import asyncio
    d = get_deps()
    if d.ready():
        ready_info = "ready(agent_loop=attached)"
    else:
        ready_info = "DEGRADED(agent_loop=missing,/v1/chat will 503)"
    logger.info("gateway_started", status=ready_info)
    try:
        yield
    finally:
        # shutdown 阶段
        # 1) 停 channels(若有)
        cm = getattr(d, "channel_manager", None) if d else None
        if cm is not None and hasattr(cm, "stop_all"):
            try:
                await asyncio.wait_for(cm.stop_all(), timeout=10.0)
                logger.info("gateway_shutdown_channels_stopped")
            except asyncio.TimeoutError:
                logger.warning("gateway_shutdown_channels_timeout(10s)")
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "gateway_shutdown_channels_failed",
                    error_type=type(e).__name__,
                    error=str(e)[:200],
                )

        # 2) 关 agent_loop 内的 httpx(若暴露 close 接口)
        agent_loop = getattr(d, "agent_loop", None) if d else None
        if agent_loop is not None and hasattr(agent_loop, "aclose"):
            try:
                await asyncio.wait_for(agent_loop.aclose(), timeout=5.0)
                logger.info("gateway_shutdown_agent_loop_closed")
            except asyncio.TimeoutError:
                logger.warning("gateway_shutdown_agent_loop_timeout(5s)")
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "gateway_shutdown_agent_loop_failed",
                    error_type=type(e).__name__,
                    error=str(e)[:200],
                )

        # 3) 关 providers(若在 deps 上挂)
        providers = getattr(d, "providers", None) if d else None
        if providers:
            for prov in providers:
                aclose = getattr(prov, "aclose", None)
                if aclose is None:
                    continue
                try:
                    res = aclose()
                    if asyncio.iscoroutine(res):
                        await asyncio.wait_for(res, timeout=3.0)
                except asyncio.TimeoutError:
                    logger.warning("gateway_shutdown_provider_timeout", provider=getattr(prov, "name", "?"))
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        "gateway_shutdown_provider_failed",
                        provider=getattr(prov, "name", "?"),
                        error_type=type(e).__name__,
                    )
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


def _install_middlewares(
    app: FastAPI,
    *,
    host: str,
    rate_limiter: RateLimiter | None,
    cors_origins: list[str],
    cors_regex: str,
) -> None:
    """按正确顺序装配中间件(Phase 27 / M15 抽出)。

    顺序(后入先出,最末 add 最先执行):
    1. 鉴权(``install_auth``)— 401 在最内
    2. 限流
    3. RequestID
    4. Metrics
    5. CORS(最外)— OPTIONS 预检先到达,不被鉴权/限流挡
    """
    from openclaw.gateway.auth import install_auth

    install_auth(app, host=host)
    app.add_middleware(RateLimitMiddleware, rate_limiter=rate_limiter)
    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_origin_regex=cors_regex or None,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
        allow_headers=["*"],
        max_age=600,
    )


def _validate_startup_security(host: str) -> str:
    """启动期安全校验(Phase 27 / M15 抽出)。

    输入 ``host``(默认从 env 读),返回校验后的 host(供 ``create_app`` 用)。
    抛 ``RuntimeError`` 表示启动期 fail-fast — 阻止未鉴权部署。
    """
    from openclaw.gateway.auth import is_dev_mode
    _token_raw = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "").strip()
    # H1 修复:无 token + 非 dev 模式 → fail-closed(不论 host)
    if not _token_raw and not is_dev_mode():
        if host == "0.0.0.0":
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
    return host


def _resolve_docs_urls() -> dict[str, str | None]:
    """解析 docs / redoc / openapi URL(prod 模式全关,Phase 25/b9)。"""
    from openclaw.gateway.auth import is_production_mode
    urls = {"/docs": "/docs", "/redoc": "/redoc", "/openapi.json": "/openapi.json"}
    if is_production_mode():
        urls = {k: None for k in urls}
    return urls


def _register_routes(app: FastAPI) -> None:
    """注册所有网关路由(Phase 27 / M15 抽出)。"""
    from fastapi.staticfiles import StaticFiles
    from pathlib import Path

    app.include_router(health.router)
    app.include_router(chat.router, prefix="/v1")
    app.include_router(sessions.router, prefix="/v1")
    app.include_router(memory.router, prefix="/v1")
    app.include_router(tools.router, prefix="/v1")
    app.include_router(skills.router, prefix="/v1")
    app.include_router(channels.router, prefix="/v1")
    app.include_router(journal.router, prefix="/v1")

    # C2 修复:root_index 路由从 if 块内提升到顶层(无论 static_dir 是否存在)。
    static_dir = Path(__file__).resolve().parent / "static"
    if static_dir.exists():
        try:
            app.mount("/ui", StaticFiles(directory=str(static_dir), html=True), name="ui")
        except Exception as e:  # pragma: no cover
            logger.warning("static_files_mount_failed", error=str(e))

    @app.get("/", include_in_schema=False)
    async def root_index() -> dict:
        return {
            "name": "openclaw-gateway",
            "version": "0.1.0",
            "ui": "/ui/",
            "docs": "/docs",
            "healthz": "/healthz",
        }


def create_app(
    deps: GatewayDeps | None = None,
    *,
    rate_limiter: RateLimiter | None | _DefaultRateLimiterSentinel = _DEFAULT_RATE_LIMITER,
    host: str | None = None,
) -> FastAPI:
    """工厂函数(Phase 27 / M15:主函数只剩 ~50 行流程)。

    流程:
    1. 启动期安全校验(token / host / production 模式)
    2. 构造 FastAPI 实例 + lifespan
    3. 注入 deps
    4. 装中间件(走 ``_install_middlewares``)
    5. 注册异常 handler + 路由(走 ``_register_routes``)

    ``rate_limiter`` 行为(Phase 27 / C1):
    - 传 ``_DEFAULT_RATE_LIMITER`` 哨兵 → module-level 单例(env 控制)
    - 传 ``None`` → 关闭
    - 传 ``RateLimiter`` 实例 → 用之

    ``host`` 行为(Phase 25):0.0.0.0 视为对外暴露 + 启动期 fail-fast。
    """
    from openclaw.gateway.auth import require_token_in_production
    require_token_in_production()

    _host = host if host is not None else os.environ.get("OPENCLAW_GATEWAY_HOST", "127.0.0.1")
    _validate_startup_security(_host)
    _docs = _resolve_docs_urls()

    app = FastAPI(
        title="OpenClaw Gateway",
        version="0.1.0",
        description="OpenClaw Python 的统一 HTTP 入口(Phase 8)。",
        lifespan=_lifespan,
        docs_url=_docs["/docs"],
        redoc_url=_docs["/redoc"],
        openapi_url=_docs["/openapi.json"],
    )
    if deps is not None:
        from openclaw.gateway.deps import set_deps
        set_deps(deps)

    _install_middlewares(
        app,
        host=_host,
        rate_limiter=(
            _RATE_LIMITER if isinstance(rate_limiter, _DefaultRateLimiterSentinel) else rate_limiter
        ),
        cors_origins=_resolve_cors_origins(),
        cors_regex=_resolve_cors_origin_regex(),
    )

    from openclaw.gateway.errors import register_error_handlers
    register_error_handlers(app)
    _register_routes(app)
    return app


# uvicorn 入口:uvicorn openclaw.gateway.app:app
app = create_app()
