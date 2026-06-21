"""``openclaw config`` —— 配置 CRUD。

子命令:
  get KEY          读取配置项(点号路径,如 agent.system_prompt)
  set KEY VALUE    设置配置项(原子写回,校验后落盘)
  patch FILE       用 JSON/YAML 文件批量合并
  unset KEY        删除配置项
  validate         校验配置文件
  schema           打印 OpenClawConfig JSON Schema
  file             打印当前配置文件路径

关键安全处理:
- SecretStr 字段(api_key 等)默认脱敏为 ***,--show-secrets 才显示明文
- set/unset/patch 读 raw 文件不走 env 插值,避免把 ${ENV} 展开后的真 key 写回
- 写回用原子写(.tmp + os.replace),校验失败不落盘
"""
from __future__ import annotations

import json as _json
import os
import re
from pathlib import Path
from typing import Any, Optional

import typer
import yaml

from openclaw.cli.context import get_ctx
from openclaw.cli.errors import CLIError, EXIT_CONFIG, EXIT_NOT_FOUND

# 敏感字段名(子串匹配,不区分大小写)
_SECRET_PATTERNS = ("api_key", "app_secret", "secret_key", "token", "password", "verification_token", "encrypt_key")

# 默认配置文件搜索顺序
_DEFAULT_CONFIG_NAMES = ("openclaw.yaml", "openclaw.yml", "openclaw.json", "openclaw.toml")


# ---------------------------------------------------------------------------
# 路径解析 / raw 读写
# ---------------------------------------------------------------------------

def _resolve_config_path(explicit: Optional[Path]) -> Path:
    if explicit:
        if not explicit.exists():
            raise CLIError(f"配置文件不存在: {explicit}", exit_code=EXIT_NOT_FOUND)
        return explicit
    # 搜索默认位置
    for name in _DEFAULT_CONFIG_NAMES:
        p = Path.cwd() / name
        if p.exists():
            return p
    # 没有 config 文件也算正常(get 用默认值)
    return Path.cwd() / "openclaw.yaml"


def _read_raw(path: Optional[Path]) -> dict[str, Any]:
    """读 raw 配置(不走 env 插值),保留 ${ENV} 占位符。"""
    if path is None or not path.exists():
        return {}
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    if suffix in (".yaml", ".yml"):
        return yaml.safe_load(text) or {}
    if suffix == ".json":
        return _json.loads(text) if text.strip() else {}
    if suffix == ".toml":
        try:
            import tomllib  # py3.11+
        except ImportError:
            import tomli as tomllib  # type: ignore[no-redef]
        return tomllib.loads(text)
    raise CLIError(f"不支持的配置格式: {suffix}", exit_code=EXIT_CONFIG)


def _dump_raw(data: dict[str, Any], path: Path) -> str:
    """按文件后缀序列化。返回文本。"""
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    if suffix == ".json":
        return _json.dumps(data, ensure_ascii=False, indent=2)
    if suffix == ".toml":
        try:
            import tomli_w
        except ImportError:
            raise CLIError(
                "TOML 写入需要 tomli-w,请运行: pip install tomli-w",
                exit_code=EXIT_CONFIG,
            )
        return tomli_w.dumps(data)
    raise CLIError(f"不支持的配置格式: {suffix}", exit_code=EXIT_CONFIG)


def _atomic_write(path: Path, content: str) -> None:
    """原子写:先写 .tmp 再 os.replace。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 点号路径 deep get/set/unset
# ---------------------------------------------------------------------------

def _parse_path(path: str) -> list[Any]:
    """解析 'providers.0.name' -> ['providers', 0, 'name']。"""
    parts: list[Any] = []
    for seg in path.split("."):
        if seg.isdigit():
            parts.append(int(seg))
        else:
            parts.append(seg)
    return parts


def _deep_get(data: Any, parts: list[Any]) -> Any:
    cur = data
    for p in parts:
        if isinstance(p, int):
            if not isinstance(cur, list) or p >= len(cur):
                raise CLIError(f"路径不存在: 数组索引 {p} 越界", exit_code=EXIT_NOT_FOUND)
            cur = cur[p]
        else:
            if not isinstance(cur, dict) or p not in cur:
                raise CLIError(f"路径不存在: 键 {p!r} 不存在", exit_code=EXIT_NOT_FOUND)
            cur = cur[p]
    return cur


def _deep_set(data: dict[str, Any], parts: list[Any], value: Any) -> None:
    cur = data
    # L10 修复:用 enumerate 替代 parts.index(p),避免重复路径段取错索引
    for i, p in enumerate(parts[:-1]):
        nxt = parts[i + 1]
        if isinstance(p, int):
            cur = cur[p]  # type: ignore[index]
        else:
            if p not in cur or cur[p] is None:
                cur[p] = [] if isinstance(nxt, int) else {}
            cur = cur[p]  # type: ignore[index]
    last = parts[-1]
    if isinstance(last, int):
        while len(cur) <= last:  # type: ignore[arg-type]
            cur.append(None)  # type: ignore[attr-defined]
        cur[last] = value  # type: ignore[index]
    else:
        cur[last] = value  # type: ignore[index]


def _deep_unset(data: dict[str, Any], parts: list[Any]) -> None:
    cur = data
    for p in parts[:-1]:
        if isinstance(p, int):
            cur = cur[p]  # type: ignore[index]
        else:
            if p not in cur:
                raise CLIError(f"路径不存在: 键 {p!r}", exit_code=EXIT_NOT_FOUND)
            cur = cur[p]  # type: ignore[index]
    last = parts[-1]
    if isinstance(last, int):
        if isinstance(cur, list) and last < len(cur):
            cur.pop(last)  # type: ignore[attr-defined]
    else:
        if isinstance(cur, dict) and last in cur:
            del cur[last]
        else:
            raise CLIError(f"路径不存在: 键 {last!r}", exit_code=EXIT_NOT_FOUND)


def _coerce_value(raw: str) -> Any:
    """把命令行字符串转为合适的 Python 值。"""
    low = raw.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "none", "~"):
        return None
    # 数字
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    if re.fullmatch(r"-?\d+\.\d+", raw):
        return float(raw)
    # JSON 数组/对象
    if raw.startswith("[") or raw.startswith("{"):
        try:
            return _json.loads(raw)
        except _json.JSONDecodeError:
            return raw
    return raw


def _mask_secret(key: str, value: Any) -> Any:
    """敏感字段脱敏。"""
    if value is None:
        return None
    # pydantic SecretStr 对象
    try:
        from pydantic import SecretStr
        if isinstance(value, SecretStr):
            return "***" if value.get_secret_value() else "(空)"
    except ImportError:
        pass
    lk = key.lower()
    if any(s in lk for s in _SECRET_PATTERNS):
        return "***" if value else "(空)"
    return value


def _serialize_config(data: Any, show_secrets: bool) -> Any:
    """递归把 pydantic 对象转为可 JSON 序列化的 dict,处理 SecretStr。"""
    from pydantic import SecretStr

    def conv(v: Any, key: str = "") -> Any:
        if isinstance(v, SecretStr):
            if show_secrets:
                return v.get_secret_value()
            return "***" if v.get_secret_value() else "(空)"
        if isinstance(v, dict):
            return {k: conv(val, k) for k, val in v.items()}
        if isinstance(v, list):
            return [conv(x, key) for x in v]
        if isinstance(v, Path):
            return str(v)
        return v

    return conv(data)


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def _config_app() -> typer.Typer:
    cfg_app = typer.Typer(help="配置 CRUD: get/set/patch/unset/validate/schema/file", no_args_is_help=True)

    @cfg_app.command("get")
    def config_get(
        ctx: typer.Context,
        key: Optional[str] = typer.Argument(None, help="点号路径,如 agent.system_prompt;省略则打印全部"),
    ) -> None:
        """读取配置项。SecretStr 字段默认脱敏。"""
        cli_ctx = get_ctx(ctx.obj)
        path = _resolve_config_path(cli_ctx.config_path)
        from openclaw.core.config import ConfigLoader

        cfg = ConfigLoader(path if path.exists() else None).load()
        # 用 python mode 拿到 SecretStr 对象,再手动序列化(可控制是否脱敏)
        data = cfg.model_dump()

        if key is None:
            # 全部输出(按 show_secrets 决定脱敏)
            serialized = _serialize_config(data, cli_ctx.output.show_secrets)
            cli_ctx.output.print(serialized, title=f"配置: {path.name}")
            return

        parts = _parse_path(key)
        try:
            value = _deep_get(data, parts)
        except CLIError:
            raise
        value = _serialize_config(value, cli_ctx.output.show_secrets)
        cli_ctx.output.print({"key": key, "value": value})

    @cfg_app.command("set")
    def config_set(
        ctx: typer.Context,
        key: str = typer.Argument(..., help="点号路径,如 agent.system_prompt 或 providers.0.model"),
        value: str = typer.Argument(..., help="值(自动推断类型:true/false/null/数字/JSON)"),
    ) -> None:
        """设置配置项(校验 + 原子写回)。"""
        cli_ctx = get_ctx(ctx.obj)
        path = _resolve_config_path(cli_ctx.config_path)
        # 读 raw(不走插值,保留 ${ENV})
        data = _read_raw(path if path.exists() else None)
        parts = _parse_path(key)
        new_val = _coerce_value(value)
        _deep_set(data, parts, new_val)

        # 校验
        from openclaw.core.config import OpenClawConfig
        from pydantic import ValidationError

        try:
            OpenClawConfig.model_validate(data)
        except ValidationError as e:
            raise CLIError(f"校验失败,未写入:\n{e}", exit_code=EXIT_CONFIG) from e

        # 原子写
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        content = _dump_raw(data, path)
        _atomic_write(path, content)
        cli_ctx.output.success(f"已设置 {key} = {_json.dumps(new_val, ensure_ascii=False)} -> {path}")

    @cfg_app.command("patch")
    def config_patch(
        ctx: typer.Context,
        patch_file: Path = typer.Option(..., "--file", "-f", help="JSON/YAML 补丁文件"),
    ) -> None:
        """用补丁文件批量合并配置(浅合并顶层 key,dict 深合并)。"""
        cli_ctx = get_ctx(ctx.obj)
        path = _resolve_config_path(cli_ctx.config_path)
        data = _read_raw(path if path.exists() else None)
        patch = _read_raw(patch_file)
        if not isinstance(patch, dict):
            raise CLIError(f"补丁文件必须是对象/字典,实际: {type(patch).__name__}", exit_code=EXIT_CONFIG)
        _deep_merge(data, patch)

        from openclaw.core.config import OpenClawConfig
        from pydantic import ValidationError

        try:
            OpenClawConfig.model_validate(data)
        except ValidationError as e:
            raise CLIError(f"校验失败,未写入:\n{e}", exit_code=EXIT_CONFIG) from e

        content = _dump_raw(data, path)
        _atomic_write(path, content)
        cli_ctx.output.success(f"已合并补丁 -> {path}")

    @cfg_app.command("unset")
    def config_unset(
        ctx: typer.Context,
        key: str = typer.Argument(..., help="点号路径"),
    ) -> None:
        """删除配置项。"""
        cli_ctx = get_ctx(ctx.obj)
        path = _resolve_config_path(cli_ctx.config_path)
        data = _read_raw(path if path.exists() else None)
        if not data:
            raise CLIError("配置为空,无可删除项", exit_code=EXIT_NOT_FOUND)
        parts = _parse_path(key)
        _deep_unset(data, parts)

        from openclaw.core.config import OpenClawConfig
        from pydantic import ValidationError

        try:
            OpenClawConfig.model_validate(data)
        except ValidationError as e:
            raise CLIError(f"删除后校验失败,未写入:\n{e}", exit_code=EXIT_CONFIG) from e

        content = _dump_raw(data, path)
        _atomic_write(path, content)
        cli_ctx.output.success(f"已删除 {key} -> {path}")

    @cfg_app.command("validate")
    def config_validate(
        ctx: typer.Context,
    ) -> None:
        """校验配置文件。"""
        cli_ctx = get_ctx(ctx.obj)
        path = _resolve_config_path(cli_ctx.config_path)
        if not path.exists():
            cli_ctx.output.warn(f"配置文件不存在: {path}(将使用默认值)")
            return
        raw = _read_raw(path)
        from openclaw.core.config import OpenClawConfig
        from pydantic import ValidationError

        try:
            OpenClawConfig.model_validate(raw)
            cli_ctx.output.success(f"配置有效: {path}")
        except ValidationError as e:
            errors = [{"loc": ".".join(str(x) for x in err["loc"]), "msg": err["msg"]} for err in e.errors()]
            cli_ctx.output.error(f"配置校验失败: {path}")
            cli_ctx.output.print({"errors": errors, "count": len(errors)})
            raise CLIError("配置校验失败", exit_code=EXIT_CONFIG)

    @cfg_app.command("schema")
    def config_schema(ctx: typer.Context) -> None:
        """打印 OpenClawConfig 的 JSON Schema。"""
        cli_ctx = get_ctx(ctx.obj)
        from openclaw.core.config import OpenClawConfig

        schema = OpenClawConfig.model_json_schema()
        cli_ctx.output.print(schema)

    @cfg_app.command("file")
    def config_file(ctx: typer.Context) -> None:
        """打印当前配置文件路径。"""
        cli_ctx = get_ctx(ctx.obj)
        path = _resolve_config_path(cli_ctx.config_path)
        exists = path.exists()
        cli_ctx.output.print({"path": str(path), "exists": exists})

    return cfg_app


def _mask_dict_recursive(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _mask_dict_recursive(_mask_secret(k, v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask_dict_recursive(x) for x in obj]
    return obj


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> None:
    """深合并 patch 到 base(原地修改 base)。"""
    for k, v in patch.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def register(app: typer.Typer) -> None:
    app.add_typer(_config_app(), name="config")


__all__ = ["register"]
