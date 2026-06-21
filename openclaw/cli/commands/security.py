"""``openclaw security`` —— 安全审计。

复用内部 audit 模块:
- audit_gateway_exposure:gateway 暴露面(token / CORS / host / 日志级别)
- audit_install_policy:工具配置(shell_allowed / http_allowed_hosts 等高危项)

  openclaw security              完整审计(gateway + 工具策略)
  openclaw security --check gateway    仅 gateway 暴露面
  openclaw security --check tools      仅工具安装策略
"""
from __future__ import annotations


import typer

from openclaw.cli.context import get_ctx
from openclaw.cli.factory import load_config


def security(
    ctx: typer.Context,
    check: str = typer.Option("all", "--check", help="审计范围:all / gateway / tools"),
) -> None:
    """运行安全审计。"""
    cli_ctx = get_ctx(ctx.obj)
    import os

    cfg, _ = load_config(cli_ctx.config_path)
    all_findings: list[dict] = []

    if check in ("all", "gateway"):
        try:
            from openclaw.audit.gateway_exposure import audit_gateway_exposure

            for f in audit_gateway_exposure(cfg, env=dict(os.environ)):
                all_findings.append({
                    "scope": "gateway",
                    "code": f.code,
                    "severity": f.severity.value,
                    "message": f.message,
                    "remediation": f.remediation,
                })
        except Exception as e:  # noqa: BLE001
            all_findings.append({
                "scope": "gateway", "code": "AUDIT_FAIL",
                "severity": "warn", "message": f"gateway 审计失败: {e}", "remediation": "",
            })

    if check in ("all", "tools"):
        try:
            from openclaw.audit.install_policy import audit_install_policy

            for f in audit_install_policy(cfg.tools):
                all_findings.append({
                    "scope": "tools",
                    "code": f.code,
                    "severity": f.severity.value,
                    "message": f.message,
                    "remediation": f.remediation,
                })
        except Exception as e:  # noqa: BLE001
            all_findings.append({
                "scope": "tools", "code": "AUDIT_FAIL",
                "severity": "warn", "message": f"工具策略审计失败: {e}", "remediation": "",
            })

    crit = sum(1 for f in all_findings if f["severity"] == "critical")
    warn = sum(1 for f in all_findings if f["severity"] == "warn")

    if cli_ctx.output.mode == "json":
        cli_ctx.output.print({"findings": all_findings, "summary": {"total": len(all_findings), "critical": crit, "warn": warn, "secure": crit == 0}})
    else:
        rows = [[f["scope"], f["severity"].upper(), f["code"], f["message"]] for f in all_findings]
        cli_ctx.output.table(["scope", "severity", "code", "message"], rows, title=f"安全审计 ({len(all_findings)} 项)")
        if crit == 0:
            cli_ctx.output.success(f"无严重问题(warn={warn})")
        else:
            cli_ctx.output.error(f"发现 {crit} 个严重问题")


def register(app: typer.Typer) -> None:
    app.command("security")(security)


__all__ = ["security", "register"]
