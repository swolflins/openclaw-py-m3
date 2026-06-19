"""/healthz + /readyz + /metrics + /version。"""
from __future__ import annotations

from fastapi import APIRouter, Header, Response

from openclaw.gateway import metrics as m
from openclaw.gateway.deps import get_deps

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict:
    """Liveness — 进程是否在跑(永远 200,除非 server 崩了)。"""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(response: Response) -> dict:
    """Readiness — agent_loop 是否就绪。"""
    deps = get_deps()
    if not deps.ready():
        response.status_code = 503
        return {
            "status": "degraded",
            "reason": "agent_loop not attached",
            "uptime_s": round(deps.uptime(), 2),
        }
    return {
        "status": "ready",
        "uptime_s": round(deps.uptime(), 2),
    }


@router.get("/metrics")
async def metrics(
    response: Response,
    accept: str | None = Header(default=None),
) -> Response:
    """双格式:Prometheus 抓取(Accept: text/plain) → prom 文本,否则 → JSON。

    也更新 gauge(openclaw_uptime_seconds / openclaw_agent_attached)。
    """
    deps = get_deps()
    m.uptime_seconds.set(deps.uptime())
    m.agent_attached.set(1.0 if deps.ready() else 0.0)

    wants_prom = bool(accept and "text/plain" in accept.lower())
    if wants_prom:
        return Response(content=m.render_prometheus(), media_type="text/plain; version=0.0.4")

    out: dict = {
        "uptime_s": round(deps.uptime(), 2),
        "agent_attached": deps.ready(),
        "config_loaded": deps.config is not None,
    }
    if deps.config is not None:
        try:
            out["providers"] = [
                getattr(p, "name", None) for p in getattr(deps.config, "providers", [])
            ]
        except Exception:
            pass
        try:
            out["memory_dir"] = str(getattr(deps.config.memory, "dir", ""))
        except Exception:
            pass
    return out


@router.get("/version")
async def version() -> dict:
    import openclaw
    return {"openclaw_py": openclaw.__version__}
