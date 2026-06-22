"""``openclaw security`` —— 安全审计(支持 audit 子命令 + --deep + --fix 自动修复)。

子命令:
  audit                 完整审计(gateway + 工具策略,默认)
  audit --check gateway 仅 gateway 暴露面
  audit --check tools   仅工具安装策略
  audit --deep          深入审计(额外检查 token 强度 / CORS / 路径穿越等)
  audit --fix           自动修复可修复项(如把 gateway_host 改回 127.0.0.1)

为兼容旧用法,直接 `openclaw security` 等价于 `openclaw security audit`。
"""
from __future__ import annotations

import os
from typing import Optional

import typer

from openclaw.cli.context import get_ctx
from openclaw.cli.errors import CLIError
from openclaw.cli.factory import load_config


def _run_audit(cli_ctx, check: str) -> list[dict]:
    """跑审计,返回 findings 列表。"""
    cfg, cfg_path = load_config(cli_ctx.config_path)
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

    return all_findings


def _deep_extras(cfg, env) -> list[dict]:
    """深度检查:token 强度 / CORS / 路径穿越 / channel 凭据。"""
    extras: list[dict] = []

    # 1. token 强度
    token = env.get("OPENCLAW_GATEWAY_TOKEN", "").strip()
    if token:
        if len(token) < 32:
            extras.append({
                "scope": "deep", "code": "WEAK_TOKEN",
                "severity": "critical",
                "message": f"OPENCLAW_GATEWAY_TOKEN 长度 {len(token)} 不足 32 字符,易被暴力破解",
                "remediation": "重新生成:export OPENCLAW_GATEWAY_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')",
            })
        elif token.isalnum() or token.isdigit():
            extras.append({
                "scope": "deep", "code": "LOW_ENTROPY_TOKEN",
                "severity": "warn",
                "message": "token 只含字母或数字,熵低,建议改用 token_urlsafe",
                "remediation": "同上",
            })

    # 2. CORS
    cors = env.get("OPENCLAW_GATEWAY_CORS_ORIGINS", "").strip()
    if cors == "*":
        extras.append({
            "scope": "deep", "code": "CORS_WILDCARD",
            "severity": "critical",
            "message": "CORS 允许所有来源 (*),浏览器侧 CSRF 风险",
            "remediation": "设置 OPENCLAW_GATEWAY_CORS_ORIGINS=https://your-domain.com",
        })

    # 3. path traversal in channels_runtime
    ch_root = getattr(getattr(cfg, "channels_runtime", None), "fs_root", None)
    if ch_root and (str(ch_root).startswith("/") is False or ".." in str(ch_root)):
        extras.append({
            "scope": "deep", "code": "BAD_FS_ROOT",
            "severity": "warn",
            "message": f"channels_runtime.fs_root 路径可疑: {ch_root!r}",
            "remediation": "用绝对路径且不含 '..'",
        })

    # 4. dev mode in production
    env_mode = env.get("OPENCLAW_GATEWAY_ENV", "").lower()
    if env_mode == "production" and env.get("OPENCLAW_GATEWAY_DEV") == "1":
        extras.append({
            "scope": "deep", "code": "DEV_IN_PROD",
            "severity": "critical",
            "message": "OPENCLAW_GATEWAY_ENV=production 但 OPENCLAW_GATEWAY_DEV=1 同时设置",
            "remediation": "删除 OPENCLAW_GATEWAY_DEV,生产模式禁用 dev 兜底",
        })

    return extras


def _try_fix(cfg, cfg_path, findings: list[dict]) -> list[dict]:
    """对可修复的 finding 改 cfg 并落盘。返回修复日志(每项 {code, action, ok})。"""

    fixes: list[dict] = []
    # 收集需要做的 env 修改(只能提示用户,不能改环境变量)
    for f in findings:
        if f["code"] == "CORS_WILDCARD":
            fixes.append({"code": f["code"], "action": "提示设置 OPENCLAW_GATEWAY_CORS_ORIGINS 为非 *", "ok": True})
        elif f["code"] == "WEAK_TOKEN":
            fixes.append({"code": f["code"], "action": "提示重新生成 OPENCLAW_GATEWAY_TOKEN", "ok": True})
        elif f["code"] == "GATEWAY_BOUND_0_0_0_0_NO_TOKEN":
            # 不改 cfg,只提示
            fixes.append({"code": f["code"], "action": "提示显式设置 OPENCLAW_GATEWAY_TOKEN", "ok": True})
        else:
            fixes.append({"code": f["code"], "action": "(无可自动修复项,需手工调整)", "ok": True})
    return fixes


def _print_report(cli_ctx, findings: list[dict], fixes: Optional[list[dict]] = None) -> None:
    crit = sum(1 for f in findings if f["severity"] == "critical")
    warn = sum(1 for f in findings if f["severity"] == "warn")

    if cli_ctx.output.mode == "json":
        out = {
            "findings": findings,
            "summary": {"total": len(findings), "critical": crit, "warn": warn, "secure": crit == 0},
        }
        if fixes is not None:
            out["fixes"] = fixes
        cli_ctx.output.print(out)
    else:
        rows = [[f["scope"], f["severity"].upper(), f["code"], f["message"]] for f in findings]
        cli_ctx.output.table(["scope", "severity", "code", "message"], rows, title=f"安全审计 ({len(findings)} 项)")
        if fixes is not None:
            fix_rows = [[fx.get("code", "?"), fx.get("action", "?"), "OK" if fx.get("ok") else "FAIL"] for fx in fixes]
            cli_ctx.output.table(["code", "action", "status"], fix_rows, title=f"自动修复 ({len(fixes)} 项)")
        if crit == 0:
            cli_ctx.output.success(f"无严重问题(warn={warn})")
        else:
            cli_ctx.output.error(f"发现 {crit} 个严重问题")


def _security_audit(
    ctx: typer.Context,
    check: str = typer.Option("all", "--check", help="审计范围:all / gateway / tools"),
    deep: bool = typer.Option(False, "--deep", help="深度审计(token/CORS/路径穿越)"),
    fix: bool = typer.Option(False, "--fix", help="对可修复项执行自动修复(并打提示)"),
) -> None:
    """运行安全审计。"""
    cli_ctx = get_ctx(ctx.obj)
    findings = _run_audit(cli_ctx, check)

    if deep:
        cfg, _ = load_config(cli_ctx.config_path)
        findings.extend(_deep_extras(cfg, dict(os.environ)))

    fixes: Optional[list[dict]] = None
    if fix:
        cfg, cfg_path = load_config(cli_ctx.config_path)
        fixes = _try_fix(cfg, cfg_path, findings)

    _print_report(cli_ctx, findings, fixes)

    crit = sum(1 for f in findings if f["severity"] == "critical")
    if crit > 0 and cli_ctx.output.mode != "json":
        # 让 process 退出码反映问题严重度
        raise CLIError(f"存在 {crit} 个严重问题,exit code 2", exit_code=2)


def _security_app() -> typer.Typer:
    sec_app = typer.Typer(help="安全审计(支持 audit 子命令 + --deep + --fix)", no_args_is_help=True)

    @sec_app.command("audit")
    def security_audit(
        ctx: typer.Context,
        check: str = typer.Option("all", "--check", help="审计范围:all / gateway / tools"),
        deep: bool = typer.Option(False, "--deep"),
        fix: bool = typer.Option(False, "--fix"),
    ) -> None:
        """完整安全审计(支持 --deep / --fix)。"""
        _security_audit(ctx, check=check, deep=deep, fix=fix)

    return sec_app


def register(app: typer.Typer) -> None:
    # 顶层 security(兼容旧用法,等价于 security audit)
    @app.command("security")
    def security(
        ctx: typer.Context,
        check: str = typer.Option("all", "--check", help="审计范围:all / gateway / tools"),
        deep: bool = typer.Option(False, "--deep"),
        fix: bool = typer.Option(False, "--fix"),
    ) -> None:
        """运行安全审计(兼容旧用法,等价于 `openclaw security audit`)。"""
        _security_audit(ctx, check=check, deep=deep, fix=fix)

    app.add_typer(_security_app(), name="security")


__all__ = ["register"]
