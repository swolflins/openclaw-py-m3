"""ToolsConfig 安装策略审计(对应原版 install-policy.test.ts)。

检查项:
- ``shell_allowed`` 包含 ``*`` → CRITICAL(允许任意 shell)
- ``http_allowed_hosts`` 包含 ``*`` → CRITICAL(SSRF 风险)
- ``fs_root`` 是 ``/`` 或 ``~`` → CRITICAL(可访问整盘)
- ``extras`` 包含非内置 → WARN(可能引入未审计代码)
- ``shell_default_cwd`` 在 ``fs_root`` 外 → WARN(越权)
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from openclaw.audit import Finding, Severity


# 常见危险命令
_DANGEROUS_SHELL_TOKENS = {
    "rm", "rmdir", "dd", "mkfs", "mkfs.ext4", "shutdown", "reboot", "halt",
    "poweroff", "init", "kill", "pkill", "killall", "curl", "wget", "nc",
    "netcat", "ncat", "chmod", "chown", "mv", "cp", ":", "eval", "exec",
    "source", "sudo", "su", "bash", "sh", "zsh", "fish",
}


def _is_root_or_home(path: str | os.PathLike[str]) -> bool:
    """检查路径是否等同于根目录或 home。"""
    s = str(path).strip()
    if not s:
        return False
    if s in ("/", "//", "~", "~/", "/.", "/.."):
        return True
    # 解析后看是否就是 /
    try:
        resolved = Path(s).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    return str(resolved) in ("/", str(Path.home()))


def audit_install_policy(tools: Any) -> list[Finding]:
    """审计工具配置。

    Args:
        tools: ``OpenClawConfig.tools`` (ToolsConfig 实例) 或 None
    """
    if tools is None:
        return []

    findings: list[Finding] = []

    # 1. shell_allowed 含 *
    shell_allowed = getattr(tools, "shell_allowed", None) or []
    if "*" in shell_allowed:
        findings.append(Finding(
            code="IP001",
            severity=Severity.CRITICAL,
            message="ToolsConfig.shell_allowed 包含 '*' — 允许任意 shell 命令",
            remediation="使用精确 allowlist:['ls', 'cat', 'git'] 等;任何非必要都禁",
        ))

    # 2. shell_allowed 含危险命令
    dangerous_intersect = _DANGEROUS_SHELL_TOKENS & set(shell_allowed)
    if dangerous_intersect:
        findings.append(Finding(
            code="IP002",
            severity=Severity.WARN,
            message=f"shell_allowed 含危险命令: {sorted(dangerous_intersect)}",
            remediation="移除 rm/curl/wget/sudo/eval 等;如需,改用专用工具(tools/builtin/http.py)",
        ))

    # 3. http_allowed_hosts 含 *
    http_allowed = getattr(tools, "http_allowed_hosts", None) or []
    if "*" in http_allowed:
        findings.append(Finding(
            code="IP003",
            severity=Severity.CRITICAL,
            message="ToolsConfig.http_allowed_hosts 包含 '*' — SSRF 风险",
            remediation="用精确 host:['api.openai.com', 'api.github.com'] 等",
        ))

    # 4. fs_root 是 / 或 ~
    fs_root = getattr(tools, "fs_root", ".")
    if _is_root_or_home(fs_root):
        findings.append(Finding(
            code="IP004",
            severity=Severity.CRITICAL,
            message=f"ToolsConfig.fs_root={fs_root!r} — 可访问整盘",
            remediation="限制为项目子目录,如 '/srv/openclaw/workspace'",
        ))

    # 5. extras 含非内置模块
    extras = getattr(tools, "extras", None) or []
    if extras:
        findings.append(Finding(
            code="IP005",
            severity=Severity.WARN,
            message=f"ToolsConfig.extras 含 {len(extras)} 个自定义模块路径 — 需单独审计",
            remediation="为每个 extra 路径单独 review;优先用 openclaw.tools.registry 显式注册",
        ))

    # 6. shell_default_cwd 越出 fs_root
    shell_cwd = getattr(tools, "shell_default_cwd", None)
    if shell_cwd and fs_root:
        try:
            cwd_resolved = Path(shell_cwd).resolve()
            fs_resolved = Path(fs_root).resolve()
            # cwd 必须在 fs_root 之下
            if shell_cwd != "." and not str(cwd_resolved).startswith(str(fs_resolved)):
                findings.append(Finding(
                    code="IP006",
                    severity=Severity.WARN,
                    message=f"shell_default_cwd={shell_cwd!r} 越出 fs_root={fs_root!r}",
                    remediation="shell_default_cwd 应在 fs_root 内",
                ))
        except (OSError, RuntimeError):
            pass  # 解析失败不报警

    return findings
