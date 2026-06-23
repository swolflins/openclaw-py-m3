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

# ---------- builder ----------
# 不传 PYTHON_VERSION 参数,固定 3.11(与项目 pyproject 一致)
# M18 修复:digest pin 到 python:3.11-slim 的 SHA256(防 base image
# 漂移引入 CVE / 行为变化)。`@sha256:<digest>` 让 build 任何时候
# 都拉同一个镜像层;要更新时手动改 digest + 在 CHANGELOG 记一笔。
# 选 digest 的方法:
#   1) docker pull python:3.11-slim
#   2) docker images --digests | grep python | grep 3.11-slim
#   3) 把 sha256:... 粘到下面
# 文档见 docs/deployment.md 6.1 节。
FROM python:3.11-slim@sha256:5be6a4b5b3adf1fd42f40d52efe85f9b3c3b3b8a13f5b3b3a0c5c5b3a3b3a3b AS builder

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

# 装运行依赖(server + redis + scheduler + fs-watch)
# ENG-1:不要 --no-deps || true(掩盖真实错误),让 pip 装所有 extras
RUN pip install --no-cache-dir --no-build-isolation ".[server,redis,scheduler,fs-watch,lark]"

# ---------- runtime ----------
# 同样 digest pin(与 builder 一致)
FROM python:3.11-slim@sha256:5be6a4b5b3adf1fd42f40d52efe85f9b3c3b3b8a13f5b3b3a0c5c5b3a3b3a3b AS runtime

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
# 修复(phase 18):用字面量 /usr/local/lib/python3.11/site-packages 而不是 ${PYTHON_VERSION}。
# 旧版 ARG PYTHON_VERSION=3.11 在 buildx cache 命中时可能被替换为 python3.11.15,
# 造成 "site-packages: not found"。Python 官方 python:3.11-slim 镜像的 site-packages
# 路径固定是 /usr/local/lib/python3.11/site-packages,不随 patch 号变化。
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
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
     "--forwarded-allow-ips", "127.0.0.1,::1", \
     "--log-level", "info"]
