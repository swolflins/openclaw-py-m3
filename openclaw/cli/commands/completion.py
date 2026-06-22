"""``openclaw completion`` —— 生成 / 安装 shell 补全脚本。

子命令:
  show [SHELL]          打印补全脚本到 stdout(可重定向)
  install [SHELL]       自动安装补全到 ~/.bashrc / ~/.zshrc / ~/.config/fish/...
  uninstall [SHELL]     卸载已装补全

支持:bash / zsh / fish / powershell(后两个 install 行为不同)。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

import typer

from openclaw.cli.errors import CLIError, EXIT_CONFIG, EXIT_UNKNOWN


def _detect_shell() -> str:
    sh = os.environ.get("SHELL", "")
    if "zsh" in sh:
        return "zsh"
    if "bash" in sh:
        return "bash"
    if "fish" in sh:
        return "fish"
    raise CLIError(
        "未指定 shell 且无法自动检测,请显式传入:bash / zsh / fish / powershell",
        exit_code=EXIT_CONFIG,
    )


def _generate_completion_script(shell: str) -> str:
    """生成补全脚本文本。失败抛 CLIError。"""
    from openclaw.cli import app as _app
    from typer.main import get_command

    shell = shell.lower()
    if shell == "powershell":
        return (
            "# PowerShell 补全:请运行 `openclaw --install-completion powershell`\n"
            "# 或在 PowerShell 配置文件中加:\n"
            "#   Import-Module PSReadLine\n"
            "#   Register-ArgumentCompleter -Native -CommandName openclaw -ScriptBlock ...\n"
        )
    from click.shell_completion import _available_shells

    comp_cls = _available_shells.get(shell)
    if comp_cls is None:
        raise CLIError(f"click 不支持生成 {shell} 补全", exit_code=EXIT_UNKNOWN)
    click_cmd = get_command(_app)
    comp = comp_cls(
        cli=click_cmd, ctx_args={}, prog_name="openclaw",
        complete_var="_OPENCLAW_COMPLETE",
    )
    return comp.source()


# 各种 shell 的安装路径
_INSTALL_PATHS = {
    "bash": "~/.bashrc",
    "zsh": "~/.zshrc",
    "fish": "~/.config/fish/completions/openclaw.fish",
}

_INSTALL_MARK = "# >>> openclaw completion >>>"
_INSTALL_END = "# <<< openclaw completion <<<"


def _install_completion(shell: str, yes: bool) -> tuple[str, str]:
    """安装补全到 shell rc 文件。返回 (rc_path, action)。"""
    shell = shell.lower()
    if shell not in _INSTALL_PATHS:
        raise CLIError(f"不支持自动安装的 shell: {shell!r}(仅支持 {list(_INSTALL_PATHS)})", exit_code=EXIT_CONFIG)

    rc_path = Path(os.path.expanduser(_INSTALL_PATHS[shell]))
    if shell == "fish":
        # fish 的 completions 目录是独立文件,不放 rc
        rc_path.parent.mkdir(parents=True, exist_ok=True)
        script = _generate_completion_script(shell)
        rc_path.write_text(script, encoding="utf-8")
        return (str(rc_path), "已写入 fish completions 文件")

    rc_path.parent.mkdir(parents=True, exist_ok=True)
    script = _generate_completion_script(shell)
    block = f"{_INSTALL_MARK}\n{_INSTALL_END}\n"
    # 检查是否已装
    existing = rc_path.read_text(encoding="utf-8") if rc_path.exists() else ""
    if _INSTALL_MARK in existing:
        # 替换
        before, _, after = existing.partition(_INSTALL_MARK)
        _, _, after = after.partition(_INSTALL_END)
        new_text = before.rstrip() + "\n" + block.replace("__SCRIPT__", script).replace(
            _INSTALL_MARK + "\n" + _INSTALL_END,
            f"{_INSTALL_MARK}\n{script}\n{_INSTALL_END}",
        ) + after.lstrip("\n")
        action = "已替换已有 block"
    else:
        new_text = existing.rstrip() + "\n\n" + block.replace("__SCRIPT__", script).replace(
            _INSTALL_MARK + "\n" + _INSTALL_END,
            f"{_INSTALL_MARK}\n{script}\n{_INSTALL_END}",
        )
        action = "已追加 block"
    if not yes and sys.stdin.isatty():
        cli = input(f"将修改 {rc_path}? [y/N] ").strip().lower()
        if cli not in ("y", "yes"):
            return (str(rc_path), "用户取消")
    rc_path.write_text(new_text, encoding="utf-8")
    return (str(rc_path), action)


def _uninstall_completion(shell: str) -> str:
    """卸载补全。返回 rc_path。"""
    shell = shell.lower()
    if shell == "fish":
        p = Path(os.path.expanduser(_INSTALL_PATHS[shell]))
        if p.exists():
            p.unlink()
            return f"已删除: {p}"
        return f"无文件: {p}"
    rc_path = Path(os.path.expanduser(_INSTALL_PATHS[shell]))
    if not rc_path.exists():
        return f"无 rc 文件: {rc_path}"
    existing = rc_path.read_text(encoding="utf-8")
    if _INSTALL_MARK not in existing:
        return f"未找到 openclaw completion block in {rc_path}"
    before, _, after = existing.partition(_INSTALL_MARK)
    _, _, after = after.partition(_INSTALL_END)
    new_text = before.rstrip() + after.lstrip("\n")
    rc_path.write_text(new_text, encoding="utf-8")
    return f"已移除 block from {rc_path}"


def _completion_app() -> typer.Typer:
    cm_app = typer.Typer(help="shell 补全:show / install / uninstall", no_args_is_help=True)

    @cm_app.command("show")
    def completion_show(
        ctx: typer.Context,
        shell: Optional[str] = typer.Argument(None, help="目标 shell:bash/zsh/fish/powershell(默认自动检测)"),
    ) -> None:
        """打印补全脚本到 stdout。"""
        if shell is None:
            shell = _detect_shell()
        sys.stdout.write(_generate_completion_script(shell))

    @cm_app.command("install")
    def completion_install(
        ctx: typer.Context,
        shell: Optional[str] = typer.Argument(None, help="目标 shell(默认自动检测)"),
        yes: bool = typer.Option(False, "--yes", "-y", help="非交互模式,默认 y"),
    ) -> None:
        """自动安装补全到 shell rc 文件。"""
        from openclaw.cli.context import get_ctx

        cli_ctx = get_ctx(ctx.obj)
        if shell is None:
            shell = _detect_shell()
        try:
            rc_path, action = _install_completion(shell, yes)
        except CLIError:
            raise
        cli_ctx.output.success(f"{action}: {rc_path}")
        if shell in ("bash", "zsh"):
            cli_ctx.output.warn(f"重新加载: source {rc_path} 或开新 shell")

    @cm_app.command("uninstall")
    def completion_uninstall(
        ctx: typer.Context,
        shell: Optional[str] = typer.Argument(None, help="目标 shell(默认自动检测)"),
    ) -> None:
        """卸载已装补全。"""
        from openclaw.cli.context import get_ctx

        cli_ctx = get_ctx(ctx.obj)
        if shell is None:
            shell = _detect_shell()
        msg = _uninstall_completion(shell)
        cli_ctx.output.success(msg)

    return cm_app


def register(app: typer.Typer) -> None:
    # 不再用 "completion" 作为子 typer 名字(typer 限制:同名 group + command 冲突)
    # 改用 "shell-completion" 作为子命令组,顶层 `completion` 仍为根命令
    def _top_completion(
        ctx: typer.Context,
        shell: Optional[str] = typer.Argument(None, help="目标 shell(默认自动检测)"),
    ) -> None:
        """生成 shell 补全脚本(兼容旧用法,等价于 `openclaw shell-completion show`)。"""
        if shell is None:
            shell = _detect_shell()
        sys.stdout.write(_generate_completion_script(shell))

    app.command("completion")(_top_completion)
    app.add_typer(_completion_app(), name="shell-completion")


__all__ = ["register"]
