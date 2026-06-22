"""``openclaw sessions`` —— 会话管理(走 Gateway REST API)。

子命令:
  list                       列出所有 session
  show SESSION_ID            查看某 session 的消息历史
  new                        创建新 session
  clear SESSION_ID           清空某 session
  tail SESSION_ID            tail 消息历史(--follow 持续跟踪)
  export-trajectory SESSION_ID  导出脱敏包
  compact SESSION_ID         压缩老消息
"""
from __future__ import annotations

from typing import Optional

import typer

from openclaw.cli.context import get_ctx
from openclaw.cli.errors import CLIError
from openclaw.cli.http import GatewayClient


def _client(url: Optional[str], token: Optional[str]) -> GatewayClient:
    return GatewayClient(url, token)


def _sessions_app() -> typer.Typer:
    s_app = typer.Typer(help="会话管理(走 Gateway /v1/sessions)", no_args_is_help=True)

    @s_app.command("list")
    def sessions_list(
        ctx: typer.Context,
        url: Optional[str] = typer.Option(None, "--url", help="gateway 地址(默认 http://127.0.0.1:8088)"),
        token: Optional[str] = typer.Option(None, "--token", help="gateway 鉴权 token"),
    ) -> None:
        """列出所有 session。"""
        cli_ctx = get_ctx(ctx.obj)
        data = _client(url, token).get("/v1/sessions")
        sessions = data if isinstance(data, list) else data.get("sessions", []) if isinstance(data, dict) else []
        rows = []
        for s in sessions:
            if isinstance(s, dict):
                rows.append([s.get("session_id", s.get("id", "?")), s.get("messages", s.get("count", "?"))])
            else:
                rows.append([str(s), ""])
        cli_ctx.output.table(["session_id", "messages"], rows, title="sessions")

    @s_app.command("show")
    def sessions_show(
        ctx: typer.Context,
        session_id: str = typer.Argument(..., help="session id"),
        url: Optional[str] = typer.Option(None, "--url"),
        token: Optional[str] = typer.Option(None, "--token"),
        k: int = typer.Option(20, "--k", help="返回最近 k 条消息"),
    ) -> None:
        """查看某 session 的消息历史。"""
        cli_ctx = get_ctx(ctx.obj)
        data = _client(url, token).get(f"/v1/sessions/{session_id}/messages", params={"k": k})
        msgs = data if isinstance(data, list) else data.get("messages", []) if isinstance(data, dict) else []
        rows = []
        for m in msgs:
            if isinstance(m, dict):
                rows.append([m.get("role", "?"), str(m.get("content", ""))[:80]])
        cli_ctx.output.table(["role", "content"], rows, title=f"session {session_id}")

    @s_app.command("new")
    def sessions_new(
        ctx: typer.Context,
        session_id: Optional[str] = typer.Option(None, "--session-id", help="指定 id(默认随机)"),
        url: Optional[str] = typer.Option(None, "--url"),
        token: Optional[str] = typer.Option(None, "--token"),
    ) -> None:
        """创建新 session。"""
        cli_ctx = get_ctx(ctx.obj)
        import uuid

        sid = session_id or f"cli_{uuid.uuid4().hex[:12]}"
        data = _client(url, token).post("/v1/sessions", json_body={"session_id": sid})
        cli_ctx.output.success(f"已创建 session: {sid}")
        if data:
            cli_ctx.output.print(data)

    @s_app.command("clear")
    def sessions_clear(
        ctx: typer.Context,
        session_id: str = typer.Argument(..., help="session id"),
        url: Optional[str] = typer.Option(None, "--url"),
        token: Optional[str] = typer.Option(None, "--token"),
    ) -> None:
        """清空某 session 的消息历史。"""
        cli_ctx = get_ctx(ctx.obj)
        _client(url, token).delete(f"/v1/sessions/{session_id}")
        cli_ctx.output.success(f"已清空 session: {session_id}")

    @s_app.command("tail")
    def sessions_tail(
        ctx: typer.Context,
        session_id: str = typer.Argument(..., help="session id"),
        follow: bool = typer.Option(False, "--follow", "-f", help="持续跟踪(类似 tail -f)"),
        url: Optional[str] = typer.Option(None, "--url"),
        token: Optional[str] = typer.Option(None, "--token"),
    ) -> None:
        """tail session 消息历史(--follow 持续输出新增消息)。"""
        import time

        cli_ctx = get_ctx(ctx.obj)
        client = _client(url, token)
        seen_ids: set[str] = set()
        offset = 0

        while True:
            try:
                data = client.get(f"/v1/sessions/{session_id}/messages", params={"k": 50, "offset": offset})
            except CLIError as e:
                cli_ctx.output.warn(f"tail 错误: {e.message}")
                if not follow:
                    return
                time.sleep(2)
                continue

            msgs = data if isinstance(data, list) else data.get("messages", []) if isinstance(data, dict) else []
            new_count = 0
            for m in msgs:
                if not isinstance(m, dict):
                    continue
                mid = m.get("id", f"{m.get('role','?')}-{m.get('ts','')}-{new_count}")
                if mid in seen_ids:
                    continue
                seen_ids.add(mid)
                ts = m.get("ts", m.get("timestamp", ""))
                cli_ctx.output.plain(f"[{ts}] {m.get('role','?')}: {str(m.get('content',''))[:200]}")
                new_count += 1
            offset += len(msgs)

            if not follow:
                if new_count == 0:
                    cli_ctx.output.warn("无新消息")
                return
            time.sleep(2)

    @s_app.command("export-trajectory")
    def sessions_export_trajectory(
        ctx: typer.Context,
        session_id: str = typer.Argument(..., help="session id"),
        output: str = typer.Option("trajectory.json", "--output", "-o", help="输出文件路径"),
        redact: bool = typer.Option(True, "--redact/--no-redact", help="脱敏(默认 true):把 api_key/token/secret 替换为 ***"),
        url: Optional[str] = typer.Option(None, "--url"),
        token: Optional[str] = typer.Option(None, "--token"),
    ) -> None:
        """导出 session 为可分享的脱敏包(JSON + 元信息)。"""
        import json
        from pathlib import Path

        cli_ctx = get_ctx(ctx.obj)
        client = _client(url, token)
        data = client.get(f"/v1/sessions/{session_id}/messages", params={"k": 10000})

        msgs = data if isinstance(data, list) else data.get("messages", []) if isinstance(data, dict) else []

        if redact:
            import re
            keys = ("api_key", "token", "password", "secret", "app_secret", "encrypt_key")
            pattern = re.compile(r"(" + "|".join(keys) + r")\s*[:=]\s*[\"']?([^\s\"',}]+)", re.IGNORECASE)
            msgs = [
                {**m, "content": pattern.sub(lambda mo: f"{mo.group(1)}=***", str(m.get("content", "")))}
                for m in msgs
            ]

        bundle = {
            "session_id": session_id,
            "exported_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
            "message_count": len(msgs),
            "redacted": redact,
            "messages": msgs,
        }

        out = Path(output)
        out.write_text(json.dumps(bundle, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        cli_ctx.output.success(f"已导出 {len(msgs)} 条消息到 {out}")

    @s_app.command("compact")
    def sessions_compact(
        ctx: typer.Context,
        session_id: str = typer.Argument(..., help="session id"),
        keep: int = typer.Option(20, "--keep", "-k", help="保留最近 N 条"),
        url: Optional[str] = typer.Option(None, "--url"),
        token: Optional[str] = typer.Option(None, "--token"),
    ) -> None:
        """压缩老消息,只保留最近 N 条。"""
        cli_ctx = get_ctx(ctx.obj)
        client = _client(url, token)
        data = client.get(f"/v1/sessions/{session_id}/messages", params={"k": 10000})
        msgs = data if isinstance(data, list) else data.get("messages", []) if isinstance(data, dict) else []
        if len(msgs) <= keep:
            cli_ctx.output.warn(f"无需压缩({len(msgs)} <= {keep})")
            return
        to_delete = msgs[: len(msgs) - keep]
        deleted = 0
        for m in to_delete:
            mid = m.get("id") if isinstance(m, dict) else None
            if mid is None:
                continue
            try:
                client.delete(f"/v1/sessions/{session_id}/messages/{mid}")
                deleted += 1
            except CLIError:
                continue
        cli_ctx.output.success(f"已删除 {deleted} 条老消息,保留最近 {keep}")

    return s_app


def register(app: typer.Typer) -> None:
    app.add_typer(_sessions_app(), name="sessions")


__all__ = ["register"]
