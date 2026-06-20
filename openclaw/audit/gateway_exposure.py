"""Gateway 暴露面审计(对应原版 audit-gateway-exposure.test.ts)。

检查项:
- 缺少 ``OPENCLAW_GATEWAY_TOKEN`` env(启用 server 时) → CRITICAL
- 启用 server 但 allowed_origins=["*"] + 鉴权关闭 → CRITICAL
- LOG_LEVEL=DEBUG 在生产(env=production) → WARN
- 启用 server 但绑定 0.0.0.0 → WARN
- 缺 auth.secret_key 同时 token 鉴权关闭 → CRITICAL
"""
from __future__ import annotations

import os
from typing import Any

from openclaw.audit import Finding, Severity


def audit_gateway_exposure(
    config: Any = None,
    *,
    env: dict[str, str] | None = None,
) -> list[Finding]:
    """审计 gateway 暴露面。

    Args:
        config: ``OpenClawConfig`` 实例(可选)
        env: 环境变量字典(默认读 os.environ,便于测试 mock)
    """
    if env is None:
        env = dict(os.environ)

    findings: list[Finding] = []

    # server 是否启用:从 config 或 env 判断
    server_enabled = False
    if config is not None:
        # 约定:config.server = OpenClawServerConfig 或类似
        server_cfg = getattr(config, "server", None)
        if server_cfg is not None:
            server_enabled = bool(getattr(server_cfg, "enabled", False))
    # env 兜底
    if env.get("OPENCLAW_SERVER_ENABLED", "").lower() in ("1", "true", "yes"):
        server_enabled = True

    if not server_enabled:
        # server 未启用 → 大部分规则不适用
        return findings

    # 1. 缺 gateway token
    has_token = bool(env.get("OPENCLAW_GATEWAY_TOKEN"))
    if not has_token:
        findings.append(Finding(
            code="GW001",
            severity=Severity.CRITICAL,
            message="OPENCLAW_GATEWAY_TOKEN 未设置 — gateway 端口可被任意访问",
            remediation="export OPENCLAW_GATEWAY_TOKEN=$(openssl rand -hex 32)",
        ))

    # 2. allowed_origins=* + token 缺失
    if config is not None:
        server_cfg = getattr(config, "server", None)
        if server_cfg is not None:
            origins = getattr(server_cfg, "allowed_origins", None) or []
            if "*" in origins and not has_token:
                findings.append(Finding(
                    code="GW002",
                    severity=Severity.CRITICAL,
                    message="allowed_origins=['*'] 同时 token 鉴权关闭 — CORS 任意源可访问",
                    remediation="设置 allowed_origins 为精确域名,且必须配合 token",
                ))

    # 3. LOG_LEVEL=DEBUG + production
    log_level = env.get("OPENCLAW_LOG_LEVEL", "").upper()
    is_prod = env.get("OPENCLAW_ENV", "development").lower() == "production"
    if log_level == "DEBUG" and is_prod:
        findings.append(Finding(
            code="GW003",
            severity=Severity.WARN,
            message="OPENCLAW_LOG_LEVEL=DEBUG 在生产环境 — 敏感信息可能写入日志",
            remediation="生产改 LOG_LEVEL=INFO 或 WARN",
        ))

    # 4. 绑定 0.0.0.0
    bind_addr = env.get("OPENCLAW_SERVER_HOST", "127.0.0.1")
    if bind_addr == "0.0.0.0":
        findings.append(Finding(
            code="GW004",
            severity=Severity.WARN,
            message="OPENCLAW_SERVER_HOST=0.0.0.0 — 服务暴露到所有网络接口",
            remediation="默认 127.0.0.1;若需公网,必须配合 token + TLS + 防火墙",
        ))

    # 5. 缺 auth.secret_key(框架内鉴权关闭)
    if config is not None:
        auth_cfg = getattr(config, "auth", None)
        if auth_cfg is not None:
            sk = getattr(auth_cfg, "secret_key", None)
            if not sk and not has_token:
                findings.append(Finding(
                    code="GW005",
                    severity=Severity.CRITICAL,
                    message="config.auth.secret_key 未配置 且 OPENCLAW_GATEWAY_TOKEN 未设置",
                    remediation="配置任一鉴权机制:auth.secret_key 或 OPENCLAW_GATEWAY_TOKEN",
                ))

    return findings
