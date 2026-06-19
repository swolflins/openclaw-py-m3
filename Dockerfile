# syntax=docker/dockerfile:1.7
# ──────────────────────────────────────────────────────────────
# OpenClaw-py 镜像(Phase 9)
#
# 阶段:
#   builder  : 安装 build 依赖,预编译 wheel
#   runtime  : 只装运行依赖,non-root user
#
# 目标:
#   - < 300MB(用 python:3.11-slim)
#   - 不缓存 pip / pytest / .git
#   - 监听 0.0.0.0:8080
#   - HEALTHCHECK 走 /healthz
# ──────────────────────────────────────────────────────────────

ARG PYTHON_VERSION=3.11

# ---------- builder ----------
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 仅装构建期依赖(wheel / setuptools)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# 先 copy 依赖描述,最大化 Docker 缓存
COPY pyproject.toml README.md ./
COPY openclaw ./openclaw

# 装运行依赖(gateway / redis / apscheduler / docker)
RUN pip install --no-cache-dir \
        "fastapi>=0.110" "uvicorn>=0.29" "sse-starlette>=2.0" \
        "redis>=5.0" "apscheduler>=3.10" "pydantic>=2.6" \
        "pydantic-settings>=2.2" "structlog>=24.1" \
        "httpx>=0.27" "rich>=13.7" "typer>=0.12" \
        "watchdog>=4.0" "orjson>=3.10" "tenacity>=8.2" \
        "pyyaml>=6.0" "aiofiles>=23.2" \
    && pip install --no-cache-dir --no-deps . 2>/dev/null || true

# ---------- runtime ----------
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    OPENCLAW_HOME=/data \
    OPENCLAW_CONFIG=/data/openclaw.yaml \
    OPENCLAW_LOG_LEVEL=INFO \
    OPENCLAW_GATEWAY_HOST=0.0.0.0 \
    OPENCLAW_GATEWAY_PORT=8080

# tini:信号转发(Ctrl-C / SIGTERM)
RUN apt-get update && apt-get install -y --no-install-recommends tini curl \
    && rm -rf /var/lib/apt/lists/*

# non-root user
RUN groupadd -r -g 1000 openclaw \
    && useradd -r -u 1000 -g openclaw -d /data -s /sbin/nologin openclaw \
    && mkdir -p /data /app /app/examples /app/skills /app/.openclaw \
    && chown -R openclaw:openclaw /data /app

WORKDIR /app

# 从 builder 复制 site-packages
COPY --from=builder /usr/local/lib/python${PYTHON_VERSION}/site-packages /usr/local/lib/python${PYTHON_VERSION}/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
# 应用代码
COPY --chown=openclaw:openclaw pyproject.toml README.md ./
COPY --chown=openclaw:openclaw openclaw ./openclaw
COPY --chown=openclaw:openclaw examples ./examples

# SOUL / skills 目录(可挂卷覆盖)
VOLUME ["/data"]
EXPOSE 8080

USER openclaw

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${OPENCLAW_GATEWAY_PORT}/healthz" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "openclaw.gateway.app:app", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*", \
     "--log-level", "info"]
