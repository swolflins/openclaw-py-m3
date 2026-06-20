"""路由聚合 + app 工厂。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from openclaw.gateway.deps import GatewayDeps, get_deps
from openclaw.gateway.routes import channels, chat, health, memory, sessions, skills, tools


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    d = get_deps()
    if d.ready():
        ready_info = "ready(agent_loop=attached)"
    else:
        ready_info = "DEGRADED(agent_loop=missing,/v1/chat will 503)"
    from openclaw.core.logging import get_logger
    get_logger("gateway").info("gateway_started", status=ready_info)
    yield
    get_logger("gateway").info("gateway_stopped")


def create_app(deps: GatewayDeps | None = None) -> FastAPI:
    """工厂函数:可以注入自定义 deps(测试用)。"""
    app = FastAPI(
        title="OpenClaw Gateway",
        version="0.1.0",
        description="OpenClaw Python 的统一 HTTP 入口(Phase 8)。",
        lifespan=_lifespan,
    )
    if deps is not None:
        from openclaw.gateway.deps import set_deps
        set_deps(deps)

    # 鉴权中间件(SEC-1)— 配置 OPENCLAW_GATEWAY_TOKEN 后启用,未配置则仅 dev
    from openclaw.gateway.auth import install_auth
    install_auth(app)

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
