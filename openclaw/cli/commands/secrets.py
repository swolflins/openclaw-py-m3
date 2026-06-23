"""``openclaw secrets`` —— 本地 secret 管理。

对齐上游 openclaw 的 ``secrets`` 命令,提供对 ``.env`` 风格本地凭据的
list / get / set / unset(不直接对接 KMS,仅做本地文件管理)。
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import typer

from openclaw.cli.context import get_ctx
from openclaw.cli.errors import EXIT_CONFIG, EXIT_NOT_FOUND, CLIError

logger = logging.getLogger(__name__)


def _secrets_path() -> Path:
    raw = os.environ.get("OPENCLAW_SECRETS_PATH", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(".env")


def _load_env(path: Path) -> dict[str, str]:
    """解析 KEY=VALUE 格式,忽略注释与空行。"""
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                out[key.strip()] = value.strip().strip('"').strip("'")
    except Exception as exc:  # noqa: BLE001
        logger.debug("读取 secrets 文件失败: %s", exc)
    return out


def _save_env(path: Path, env: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f'{k}="{v}"' for k, v in sorted(env.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _is_valid_key(key: str) -> bool:
    return bool(re.fullmatch(r"[A-Z_][A-Z0-9_]*", key.upper()))


def _secrets_app() -> typer.Typer:
    s_app = typer.Typer(help="本地 secret 管理:list / get / set / unset", no_args_is_help=True)

    @s_app.command("list")
    def secrets_list(ctx: typer.Context) -> None:
        """列出已配置的 secret key(值默认脱敏)。"""
        cli_ctx = get_ctx(ctx.obj)
        env = _load_env(_secrets_path())
        rows = [[k, "***" if v else "(空)"] for k, v in sorted(env.items())]
        cli_ctx.output.table(["key", "value"], rows, title=f"secrets ({len(env)})")

    @s_app.command("get")
    def secrets_get(
        ctx: typer.Context,
        key: str = typer.Argument(..., help="secret key"),
    ) -> None:
        """获取某个 secret 的值(默认脱敏,加 --show-secrets 显示明文)。"""
        cli_ctx = get_ctx(ctx.obj)
        env = _load_env(_secrets_path())
        value = env.get(key)
        if value is None:
            raise CLIError(f"secret 不存在: {key}", exit_code=EXIT_NOT_FOUND)
        if cli_ctx.output.show_secrets:
            cli_ctx.output.print({key: value})
        else:
            cli_ctx.output.print({key: "***"})

    @s_app.command("set")
    def secrets_set(
        ctx: typer.Context,
        key: str = typer.Argument(..., help="secret key(仅大写/下划线/数字)"),
        value: str = typer.Argument(..., help="secret value"),
    ) -> None:
        """设置 secret。"""
        cli_ctx = get_ctx(ctx.obj)
        if not _is_valid_key(key):
            raise CLIError(
                f"非法 secret key: {key!r}(应匹配 [A-Z_][A-Z0-9_]*)",
                exit_code=EXIT_CONFIG,
            )
        path = _secrets_path()
        env = _load_env(path)
        env[key.upper()] = value
        _save_env(path, env)
        cli_ctx.output.success(f"已设置 secret: {key.upper()}")

    @s_app.command("unset")
    def secrets_unset(
        ctx: typer.Context,
        key: str = typer.Argument(..., help="secret key"),
    ) -> None:
        """删除 secret。"""
        cli_ctx = get_ctx(ctx.obj)
        path = _secrets_path()
        env = _load_env(path)
        if key.upper() not in env:
            raise CLIError(f"secret 不存在: {key}", exit_code=EXIT_NOT_FOUND)
        del env[key.upper()]
        _save_env(path, env)
        cli_ctx.output.success(f"已删除 secret: {key.upper()}")

    return s_app


def register(app: typer.Typer) -> None:
    app.add_typer(_secrets_app(), name="secrets")


__all__ = ["register"]
