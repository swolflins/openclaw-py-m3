"""配置审计(P0 安全基线)。

对应原版 openclaw/openclaw:
- src/security/install-policy.test.ts
- src/security/audit-gateway-exposure.test.ts

子模块:
- ``install_policy``:审计 ``ToolsConfig`` 是否允许高危操作
- ``gateway_exposure``:审计 ``OpenClawConfig`` + 环境变量是否暴露未鉴权入口
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Severity(str, Enum):
    """审计严重度。"""
    INFO = "info"
    WARN = "warn"
    CRITICAL = "critical"


@dataclass(frozen=True)
class Finding:
    """单条审计发现。"""
    code: str           # 短 code,e.g. "GW001"
    severity: Severity
    message: str
    remediation: str    # 修复建议

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"[{self.severity.value.upper()}] {self.code}: {self.message}"


__all__ = [
    "Severity",
    "Finding",
    "audit_install_policy",
    "audit_gateway_exposure",
]
