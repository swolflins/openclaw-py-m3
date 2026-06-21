"""``openclaw skills`` —— 技能管理。

子命令:
  list            列出已加载技能(扫描配置的 skills 目录)
  info NAME       查看某技能详情
  install SRC     安装技能(复制含 SKILL.md 的目录到技能目录)
  reload          通知 gateway 重新加载技能(走 REST)
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

import typer

from openclaw.cli.context import get_ctx
from openclaw.cli.errors import CLIError, EXIT_CONFIG, EXIT_NOT_FOUND
from openclaw.cli.factory import load_config
from openclaw.cli.http import GatewayClient


def _resolve_skill_dirs(config_path: Optional[Path]) -> list[Path]:
    cfg, _ = load_config(config_path)
    return [Path(d) for d in cfg.skills.directories] if cfg.skills.enabled else []


def _skills_app() -> typer.Typer:
    sk_app = typer.Typer(help="技能管理:list / info / install / reload", no_args_is_help=True)

    @sk_app.command("list")
    def skills_list(
        ctx: typer.Context,
        directory: Optional[list[Path]] = typer.Option(None, "--dir", "-d", help="额外扫描目录(可多次)"),
    ) -> None:
        """列出已加载技能。"""
        cli_ctx = get_ctx(ctx.obj)
        dirs = _resolve_skill_dirs(cli_ctx.config_path)
        if directory:
            dirs.extend(directory)
        if not dirs:
            cli_ctx.output.warn("未配置 skills 目录(或 skills.enabled=false)")
            return

        from openclaw.core.skills import load_skills

        # 过滤存在的目录
        existing = [d for d in dirs if d.exists()]
        if not existing:
            cli_ctx.output.warn(f"配置的技能目录均不存在: {dirs}")
            return

        sreg = load_skills(*existing)
        skills = sreg.skills()
        rows = [
            [s.name, s.version, (s.description or "")[:50], ", ".join(s.triggers) if s.triggers else "", str(s.path or "")]
            for s in skills
        ]
        cli_ctx.output.table(["name", "version", "description", "triggers", "path"], rows, title=f"技能 ({len(skills)})")

    @sk_app.command("info")
    def skills_info(
        ctx: typer.Context,
        name: str = typer.Argument(..., help="技能名"),
    ) -> None:
        """查看某技能详情。"""
        cli_ctx = get_ctx(ctx.obj)
        dirs = _resolve_skill_dirs(cli_ctx.config_path)
        existing = [d for d in dirs if d.exists()]
        if not existing:
            raise CLIError("无可用技能目录", exit_code=EXIT_NOT_FOUND)

        from openclaw.core.skills import load_skills

        sreg = load_skills(*existing)
        skill = sreg.get(name)
        if skill is None:
            raise CLIError(f"未找到技能: {name}", exit_code=EXIT_NOT_FOUND)
        info = {
            "name": skill.name,
            "version": skill.version,
            "description": skill.description,
            "triggers": skill.triggers,
            "requires_tools": skill.requires_tools,
            "path": str(skill.path) if skill.path else None,
            "prompt_injections": skill.prompt_injections,
        }
        cli_ctx.output.print(info, title=f"技能: {name}")

    @sk_app.command("install")
    def skills_install(
        ctx: typer.Context,
        src: Path = typer.Argument(..., help="技能源目录(须含 SKILL.md)"),
        force: bool = typer.Option(False, "--force", help="目标已存在时覆盖"),
    ) -> None:
        """安装技能:复制含 SKILL.md 的目录到技能目录。"""
        cli_ctx = get_ctx(ctx.obj)
        if not src.is_dir():
            raise CLIError(f"源路径不是目录: {src}", exit_code=EXIT_CONFIG)
        if not (src / "SKILL.md").exists():
            raise CLIError(f"源目录缺少 SKILL.md: {src}", exit_code=EXIT_CONFIG)

        dirs = _resolve_skill_dirs(cli_ctx.config_path)
        if not dirs:
            raise CLIError("未配置 skills 目录,无法安装", exit_code=EXIT_CONFIG)
        target_root = dirs[0]
        target_root.mkdir(parents=True, exist_ok=True)
        target = target_root / src.name
        if target.exists() and not force:
            raise CLIError(f"目标已存在: {target},用 --force 覆盖", exit_code=EXIT_CONFIG)
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(src, target)
        cli_ctx.output.success(f"已安装技能: {src.name} -> {target}")

    @sk_app.command("reload")
    def skills_reload(
        ctx: typer.Context,
        url: Optional[str] = typer.Option(None, "--url"),
        token: Optional[str] = typer.Option(None, "--token"),
    ) -> None:
        """通知 gateway 重新加载技能。"""
        cli_ctx = get_ctx(ctx.obj)
        data = GatewayClient(url, token).post("/v1/skills/reload", json_body={})
        cli_ctx.output.success("已通知 gateway 重新加载技能")
        if data:
            cli_ctx.output.print(data)

    return sk_app


def register(app: typer.Typer) -> None:
    app.add_typer(_skills_app(), name="skills")


__all__ = ["register"]
