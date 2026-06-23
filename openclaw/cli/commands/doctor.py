"""``openclaw doctor`` —— 配置校验与修复助手。

检查项:
  1. 配置文件校验(OpenClawConfig.model_validate)
  2. 可选依赖(fastapi/uvicorn/chromadb/anthropic/gemini/redis/docker/playwright)
  3. provider 配置(api_key 是否设、base_url 可达性)
  4. memory 目录可写性
  5. gateway 暴露面审计(复用 audit_gateway_exposure)
  6. skills 目录存在性
  7. --fix:自动修复可修复项(建目录、生成 token 建议)
"""
from __future__ import annotations

import importlib
import os
import secrets
from pathlib import Path

import typer

from openclaw.cli.context import get_ctx
from openclaw.cli.errors import CLIError
from openclaw.cli.factory import load_config

# 可选依赖清单:(module, extra, 说明)
_OPTIONAL_DEPS = [
    ("fastapi", "server", "Gateway HTTP 服务"),
    ("uvicorn", "server", "Gateway ASGI 服务器"),
    ("chromadb", "chroma", "长期向量记忆"),
    ("anthropic", "anthropic", "Anthropic Claude provider"),
    ("google.generativeai", "gemini", "Google Gemini provider"),
    ("redis", "redis", "Redis 事件总线"),
    ("docker", "docker", "Docker 沙箱工具"),
    ("playwright", "playwright", "Playwright 浏览器工具"),
    ("apscheduler", "scheduler", "定时任务"),
    ("watchdog", "fs-watch", "配置热重载"),
]


def _check_dependencies() -> dict[str, dict]:
    deps = {}
    for mod, extra, desc in _OPTIONAL_DEPS:
        try:
            importlib.import_module(mod)
            deps[mod] = {"installed": True, "extra": extra, "desc": desc}
        except ImportError:
            deps[mod] = {"installed": False, "extra": extra, "desc": desc}
    return deps


def _check_providers(cfg, fix: bool) -> list[dict]:
    findings = []
    for i, p in enumerate(cfg.providers):
        api_key_set = p.api_key is not None and bool(
            p.api_key.get_secret_value() if hasattr(p.api_key, "get_secret_value") else p.api_key
        )
        if not api_key_set:
            findings.append({
                "severity": "warn",
                "code": f"PROV{i}_NO_KEY",
                "message": f"provider {p.name} (model={p.model}) 未设 api_key",
                "remediation": f"在配置中设置 providers.{i}.api_key 或用 ${'{ENV}'} 引用环境变量",
            })
        # base_url 可达性(仅当非本地)
        if p.base_url and not p.base_url.startswith("http://localhost") and not p.base_url.startswith("http://127"):
            try:
                import httpx

                with httpx.Client(timeout=3.0) as c:
                    r = c.head(p.base_url)  # noqa: F841
            except Exception as e:  # noqa: BLE001
                findings.append({
                    "severity": "warn",
                    "code": f"PROV{i}_UNREACHABLE",
                    "message": f"provider {p.name} base_url 不可达: {p.base_url} ({e})",
                    "remediation": "检查网络或 base_url 配置",
                })
    return findings


def _check_memory(cfg, fix: bool) -> list[dict]:
    findings = []
    mem_dir = cfg.memory.dir
    try:
        mem_dir.mkdir(parents=True, exist_ok=True)
        # 测试可写
        test_file = mem_dir / ".openclaw_doctor_wtest"
        test_file.write_text("ok")
        test_file.unlink()
    except Exception as e:  # noqa: BLE001
        if fix:
            try:
                mem_dir.mkdir(parents=True, exist_ok=True)
            except Exception:  # noqa: BLE001
                findings.append({
                    "severity": "critical",
                    "code": "MEM_DIR_NOT_WRITABLE",
                    "message": f"memory 目录不可写且无法修复: {mem_dir} ({e})",
                    "remediation": "修改 memory.dir 到可写路径",
                })
        else:
            findings.append({
                "severity": "critical",
                "code": "MEM_DIR_NOT_WRITABLE",
                "message": f"memory 目录不可写: {mem_dir} ({e})",
                "remediation": "用 --fix 尝试创建,或修改 memory.dir",
            })
    return findings


def _check_skills(cfg, fix: bool) -> list[dict]:
    findings = []
    for d in cfg.skills.directories:
        d = Path(d)
        if not d.exists():
            if fix:
                try:
                    d.mkdir(parents=True, exist_ok=True)
                except Exception as e:  # noqa: BLE001
                    findings.append({
                        "severity": "warn",
                        "code": "SKILL_DIR_MISSING",
                        "message": f"技能目录无法创建: {d} ({e})",
                        "remediation": "手动创建或修改 skills.directories",
                    })
            else:
                findings.append({
                    "severity": "info",
                    "code": "SKILL_DIR_MISSING",
                    "message": f"技能目录不存在: {d}",
                    "remediation": "用 --fix 创建,或安装技能:openclaw skills install",
                })
    return findings


def doctor(
    ctx: typer.Context,
    fix: bool = typer.Option(False, "--fix", help="自动修复可修复项(建目录等)"),
    check: str = typer.Option("all", "--check", help="检查范围:all/config/deps/providers/memory/gateway/skills"),
) -> None:
    """运行健康检查与配置校验。"""
    cli_ctx = get_ctx(ctx.obj)
    checks = ["config", "deps", "providers", "memory", "gateway", "skills"] if check == "all" else [check]

    result: dict = {"findings": [], "fix_applied": fix}
    all_findings: list[dict] = []

    # 1. 配置校验
    try:
        cfg, cfg_path = load_config(cli_ctx.config_path)
        result["config_path"] = str(cfg_path) if cfg_path else None
    except Exception as e:  # noqa: BLE001
        all_findings.append({
            "severity": "critical", "code": "CONFIG_LOAD_FAIL",
            "message": f"配置加载失败: {e}", "remediation": "修正配置文件后重试",
        })
        cli_ctx.output.print({"findings": all_findings, "summary": {"total": len(all_findings), "failed": True}})
        return

    if "config" in checks:
        from pydantic import ValidationError

        from openclaw.core.config import OpenClawConfig
        try:
            OpenClawConfig.model_validate(cfg.model_dump())
        except ValidationError as e:
            for err in e.errors():
                all_findings.append({
                    "severity": "critical", "code": "CONFIG_INVALID",
                    "message": f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}",
                    "remediation": "openclaw config set 修正对应字段",
                })

    if "deps" in checks:
        result["dependencies"] = _check_dependencies()
        missing = [m for m, info in result["dependencies"].items() if not info["installed"]]
        for m in missing:
            info = result["dependencies"][m]
            all_findings.append({
                "severity": "info", "code": f"DEP_{m.upper()}_MISSING",
                "message": f"可选依赖未安装: {m} ({info['desc']})",
                "remediation": f"pip install 'openclaw-py[{info['extra']}]'",
            })

    if "providers" in checks:
        all_findings.extend(_check_providers(cfg, fix))

    if "memory" in checks:
        all_findings.extend(_check_memory(cfg, fix))

    if "gateway" in checks:
        try:
            # audit __init__.py 的 __all__ 声明了但未实际 import,故从子模块导入
            from openclaw.audit.gateway_exposure import audit_gateway_exposure

            findings = audit_gateway_exposure(cfg, env=dict(os.environ))
            for f in findings:
                all_findings.append({
                    "severity": f.severity.value, "code": f.code,
                    "message": f.message, "remediation": f.remediation,
                })
                # --fix:gateway token 缺失时生成建议
                if fix and f.code == "GW001":
                    token = secrets.token_urlsafe(32)
                    all_findings.append({
                        "severity": "info", "code": "GW001_FIX",
                        "message": f"建议执行(已生成 token): export OPENCLAW_GATEWAY_TOKEN={token}",
                        "remediation": "将此命令加入 shell 配置或 .env",
                    })
        except Exception as e:  # noqa: BLE001
            all_findings.append({
                "severity": "warn", "code": "AUDIT_FAIL",
                "message": f"gateway 审计失败: {e}", "remediation": "检查 audit 模块",
            })

    if "skills" in checks:
        all_findings.extend(_check_skills(cfg, fix))

    result["findings"] = all_findings
    crit = sum(1 for f in all_findings if f["severity"] == "critical")
    warn = sum(1 for f in all_findings if f["severity"] == "warn")
    info = sum(1 for f in all_findings if f["severity"] == "info")
    result["summary"] = {"total": len(all_findings), "critical": crit, "warn": warn, "info": info, "healthy": crit == 0}

    # 渲染
    if cli_ctx.output.mode == "json":
        cli_ctx.output.print(result)
    else:
        rows = [[f["severity"].upper(), f["code"], f["message"]] for f in all_findings]
        cli_ctx.output.table(["severity", "code", "message"], rows, title=f"doctor ({len(all_findings)} 项)")
        cli_ctx.output.print(result["summary"], title="汇总")
        if crit == 0:
            cli_ctx.output.success("无严重问题")
        else:
            cli_ctx.output.error(f"发现 {crit} 个严重问题")

    if crit > 0:
        raise CLIError(f"doctor 发现 {crit} 个严重问题", exit_code=2)


def register(app: typer.Typer) -> None:
    app.command("doctor")(doctor)


__all__ = ["doctor", "register"]
