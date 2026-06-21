"""CLI 输出格式化器。

支持三种模式:
- rich  (默认): rich Console 彩色输出 / 表格
- json  (--json): 结构化 JSON 到 stdout,日志走 stderr,无 ANSI
- plain (--plain): 纯文本,无表格框线

对齐上游 openclaw 的输出能力(rich/json/plain/table)。
"""
from __future__ import annotations

import json as _json
import sys
from typing import Any, Optional, Sequence

from rich.console import Console
from rich.table import Table

Mode = str  # "rich" | "json" | "plain"

# 可脱敏的字段名(不区分大小写包含即视为敏感)
_SECRET_FIELDS = {"api_key", "app_secret", "secret_key", "token", "password", "verification_token", "encrypt_key"}


def _mask_value(key: str, value: Any) -> Any:
    """敏感字段脱敏为 '***'(None 保持 None)。"""
    if value is None:
        return None
    lk = key.lower()
    if any(s in lk for s in _SECRET_FIELDS):
        return "***" if value else "(空)"
    return value


def _mask_dict(obj: Any) -> Any:
    """递归对 dict 中的敏感字段脱敏(用于 JSON 输出)。"""
    if isinstance(obj, dict):
        return {k: _mask_dict(_mask_value(k, v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask_dict(x) for x in obj]
    return obj


class OutputFormatter:
    """根据 mode 渲染输出。所有命令应通过它输出,而非直接 print。"""

    def __init__(self, mode: Mode = "rich", *, show_secrets: bool = False) -> None:
        self.mode = mode
        self.show_secrets = show_secrets
        # rich Console:json 模式时禁用颜色,且 stderr 用于日志
        self._console = Console(stderr=False, force_terminal=mode == "rich")
        self._err_console = Console(stderr=True)

    # ---- 通用 ----

    def print(self, obj: Any, *, title: Optional[str] = None) -> None:
        """打印对象(字符串 / dict / list)。"""
        if self.mode == "json":
            data = obj if not isinstance(obj, str) else {"message": obj}
            self._emit_json(data)
        elif self.mode == "plain":
            if title:
                print(f"== {title} ==")
            if isinstance(obj, str):
                print(obj)
            else:
                print(_json.dumps(obj, ensure_ascii=False, indent=2, default=str))
        else:  # rich
            if title:
                self._console.print(f"[bold cyan]{title}[/bold cyan]")
            if isinstance(obj, str):
                self._console.print(obj)
            elif isinstance(obj, (dict, list)):
                self._console.print_json(data=obj, default=str)
            else:
                self._console.print(str(obj))

    def table(
        self,
        columns: Sequence[str],
        rows: Sequence[Sequence[Any]],
        *,
        title: Optional[str] = None,
    ) -> None:
        """渲染表格。rows 是行列表,每行长度与 columns 对齐。"""
        if self.mode == "json":
            data = [dict(zip(columns, row)) for row in rows]
            self._emit_json({"rows": data, "count": len(data), **({"title": title} if title else {})})
            return

        if self.mode == "plain":
            if title:
                print(f"== {title} ==")
            print("\t".join(columns))
            print("\t".join("-" * len(c) for c in columns))
            for row in rows:
                print("\t".join(str(c) for c in row))
            return

        # rich
        table = Table(title=title, show_lines=False)
        for col in columns:
            table.add_column(col, overflow="fold")
        for row in rows:
            table.add_row(*[str(c) for c in row])
        self._console.print(table)

    def json(self, obj: Any) -> None:
        """强制 JSON 输出(无视 mode)。"""
        self._emit_json(obj)

    def plain(self, text: str) -> None:
        """强制纯文本输出(无视 mode)。"""
        print(text)

    def raw(self, text: str) -> None:
        """原样输出(不带格式),如 shell completion 脚本。"""
        sys.stdout.write(text)

    # ---- 消息类 ----

    def success(self, msg: str) -> None:
        if self.mode == "json":
            self._emit_json({"status": "ok", "message": msg})
        elif self.mode == "plain":
            print(f"OK: {msg}")
        else:
            self._console.print(f"[green]✓[/green] {msg}")

    def warn(self, msg: str) -> None:
        if self.mode == "json":
            self._emit_json({"status": "warn", "message": msg})
        elif self.mode == "plain":
            print(f"WARN: {msg}", file=sys.stderr)
        else:
            self._err_console.print(f"[yellow]⚠[/yellow] {msg}")

    def error(self, msg: str, *, hint: Optional[str] = None) -> None:
        """错误输出一律走 stderr。"""
        if self.mode == "plain":
            print(f"ERROR: {msg}", file=sys.stderr)
        elif self.mode == "rich":
            self._err_console.print(f"[red]✗[/red] {msg}")
        else:  # json 模式错误也走 stderr JSON
            print(_json.dumps({"status": "error", "message": msg}, ensure_ascii=False), file=sys.stderr)
        if hint:
            print(f"提示: {hint}", file=sys.stderr)

    # ---- 内部 ----

    def _emit_json(self, obj: Any) -> None:
        data = obj if self.show_secrets else _mask_dict(obj)
        print(_json.dumps(data, ensure_ascii=False, indent=2, default=str))
