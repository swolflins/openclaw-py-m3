"""Phase 14 P0 测试:install-policy / gateway-exposure 审计。

对应原版 openclaw/openclaw:
- src/security/install-policy.test.ts
- src/security/audit-gateway-exposure.test.ts
"""
from __future__ import annotations


from openclaw.audit import Finding, Severity
from openclaw.audit.gateway_exposure import audit_gateway_exposure
from openclaw.audit.install_policy import audit_install_policy
from openclaw.core.config import ToolsConfig


# =========================================================================
# 1. install_policy
# =========================================================================

class TestAuditInstallPolicy:
    def test_none_returns_empty(self):
        assert audit_install_policy(None) == []

    def test_default_config_clean(self):
        tools = ToolsConfig()  # 所有默认
        findings = audit_install_policy(tools)
        # 默认 config 应该没有 critical
        criticals = [f for f in findings if f.severity == Severity.CRITICAL]
        assert criticals == []

    def test_shell_allowed_wildcard_critical(self):
        tools = ToolsConfig(shell_allowed=["*"])
        findings = audit_install_policy(tools)
        codes = [f.code for f in findings]
        assert "IP001" in codes
        f = next(f for f in findings if f.code == "IP001")
        assert f.severity == Severity.CRITICAL

    def test_shell_allowed_dangerous_warn(self):
        tools = ToolsConfig(shell_allowed=["ls", "rm", "curl"])
        findings = audit_install_policy(tools)
        codes = [f.code for f in findings]
        assert "IP002" in codes
        f = next(f for f in findings if f.code == "IP002")
        assert f.severity == Severity.WARN
        assert "rm" in f.message
        assert "curl" in f.message

    def test_shell_allowed_safe_no_warn(self):
        tools = ToolsConfig(shell_allowed=["ls", "cat", "git", "echo"])
        findings = audit_install_policy(tools)
        codes = [f.code for f in findings]
        assert "IP002" not in codes

    def test_http_wildcard_critical(self):
        tools = ToolsConfig(http_allowed_hosts=["*"])
        findings = audit_install_policy(tools)
        codes = [f.code for f in findings]
        assert "IP003" in codes
        assert any(f.severity == Severity.CRITICAL for f in findings if f.code == "IP003")

    def test_http_explicit_hosts_ok(self):
        tools = ToolsConfig(http_allowed_hosts=["api.openai.com", "api.github.com"])
        findings = audit_install_policy(tools)
        codes = [f.code for f in findings]
        assert "IP003" not in codes

    def test_fs_root_root_critical(self):
        tools = ToolsConfig(fs_root="/")
        findings = audit_install_policy(tools)
        codes = [f.code for f in findings]
        assert "IP004" in codes

    def test_fs_root_home_critical(self):
        tools = ToolsConfig(fs_root="~")
        findings = audit_install_policy(tools)
        codes = [f.code for f in findings]
        assert "IP004" in codes

    def test_fs_root_relative_ok(self):
        tools = ToolsConfig(fs_root="./workspace")
        findings = audit_install_policy(tools)
        codes = [f.code for f in findings]
        assert "IP004" not in codes

    def test_extras_warn(self):
        tools = ToolsConfig(extras=["my.custom.tool"])
        findings = audit_install_policy(tools)
        codes = [f.code for f in findings]
        assert "IP005" in codes
        assert any(f.severity == Severity.WARN for f in findings if f.code == "IP005")

    def test_finding_dataclass(self):
        f = Finding(
            code="TEST",
            severity=Severity.INFO,
            message="msg",
            remediation="fix",
        )
        assert f.code == "TEST"
        assert f.severity == Severity.INFO
        assert "TEST" in str(f)
        assert "msg" in str(f)

    def test_combined_findings_ordering(self):
        tools = ToolsConfig(
            shell_allowed=["*"],
            http_allowed_hosts=["*"],
            fs_root="/",
            extras=["x"],
        )
        findings = audit_install_policy(tools)
        codes = [f.code for f in findings]
        # 至少包含 IP001/003/004/005
        assert all(c in codes for c in ["IP001", "IP003", "IP004", "IP005"])


# =========================================================================
# 2. gateway_exposure
# =========================================================================

class TestAuditGatewayExposure:
    def test_server_disabled_skips_most(self):
        env = {}  # server 不启用
        findings = audit_gateway_exposure(env=env)
        # 不启用 server → 不报警
        assert findings == []

    def test_server_enabled_no_token_critical(self, monkeypatch):
        monkeypatch.setenv("OPENCLAW_SERVER_ENABLED", "true")
        monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
        monkeypatch.delenv("OPENCLAW_LOG_LEVEL", raising=False)
        monkeypatch.setenv("OPENCLAW_SERVER_HOST", "127.0.0.1")
        findings = audit_gateway_exposure(env={
            "OPENCLAW_SERVER_ENABLED": "true",
            "OPENCLAW_SERVER_HOST": "127.0.0.1",
        })
        codes = [f.code for f in findings]
        assert "GW001" in codes
        f = next(f for f in findings if f.code == "GW001")
        assert f.severity == Severity.CRITICAL

    def test_server_with_token_clean(self, monkeypatch):
        monkeypatch.setenv("OPENCLAW_SERVER_ENABLED", "true")
        monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "a" * 32)
        findings = audit_gateway_exposure(env={
            "OPENCLAW_SERVER_ENABLED": "true",
            "OPENCLAW_GATEWAY_TOKEN": "a" * 32,
            "OPENCLAW_SERVER_HOST": "127.0.0.1",
        })
        codes = [f.code for f in findings]
        assert "GW001" not in codes

    def test_debug_log_in_production_warn(self):
        env = {
            "OPENCLAW_SERVER_ENABLED": "true",
            "OPENCLAW_GATEWAY_TOKEN": "x" * 32,
            "OPENCLAW_LOG_LEVEL": "DEBUG",
            "OPENCLAW_ENV": "production",
            "OPENCLAW_SERVER_HOST": "127.0.0.1",
        }
        findings = audit_gateway_exposure(env=env)
        codes = [f.code for f in findings]
        assert "GW003" in codes
        assert any(f.severity == Severity.WARN for f in findings if f.code == "GW003")

    def test_debug_log_in_dev_ok(self):
        env = {
            "OPENCLAW_SERVER_ENABLED": "true",
            "OPENCLAW_GATEWAY_TOKEN": "x" * 32,
            "OPENCLAW_LOG_LEVEL": "DEBUG",
            "OPENCLAW_ENV": "development",
            "OPENCLAW_SERVER_HOST": "127.0.0.1",
        }
        findings = audit_gateway_exposure(env=env)
        codes = [f.code for f in findings]
        assert "GW003" not in codes

    def test_bind_all_interfaces_warn(self):
        env = {
            "OPENCLAW_SERVER_ENABLED": "true",
            "OPENCLAW_GATEWAY_TOKEN": "x" * 32,
            "OPENCLAW_SERVER_HOST": "0.0.0.0",
        }
        findings = audit_gateway_exposure(env=env)
        codes = [f.code for f in findings]
        assert "GW004" in codes

    def test_bind_localhost_ok(self):
        env = {
            "OPENCLAW_SERVER_ENABLED": "true",
            "OPENCLAW_GATEWAY_TOKEN": "x" * 32,
            "OPENCLAW_SERVER_HOST": "127.0.0.1",
        }
        findings = audit_gateway_exposure(env=env)
        codes = [f.code for f in findings]
        assert "GW004" not in codes

    def test_full_secure_no_critical(self):
        env = {
            "OPENCLAW_SERVER_ENABLED": "true",
            "OPENCLAW_GATEWAY_TOKEN": "x" * 32,
            "OPENCLAW_SERVER_HOST": "127.0.0.1",
            "OPENCLAW_LOG_LEVEL": "INFO",
            "OPENCLAW_ENV": "production",
        }
        findings = audit_gateway_exposure(env=env)
        criticals = [f for f in findings if f.severity == Severity.CRITICAL]
        assert criticals == []

    def test_env_default_disabled(self, monkeypatch):
        # 显式把 env 传进去,确保不读 os
        findings = audit_gateway_exposure(env={})
        assert findings == []
