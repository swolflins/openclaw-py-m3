"""``openclaw mcp`` —— Model Context Protocol 服务端(轻量对齐版)。

对齐上游 openclaw 的 ``mcp`` 命令,基于 FastAPI + SSE 暴露一个最小 MCP
server,供外部 IDE / Agent 接入。依赖 ``server`` extra(含 fastapi /
uvicorn / sse-starlette)。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import typer

from openclaw.cli.context import get_ctx
from openclaw.cli.errors import EXIT_DEPENDENCY, CLIError

logger = logging.getLogger(__name__)


def _require_server() -> None:
    """懒检查 server extra 依赖。"""
    for mod in ("fastapi", "uvicorn", "sse_starlette"):
        try:
            __import__(mod)
        except ImportError as e:
            raise CLIError(
                "缺少可选依赖 [server],请运行: pip install 'openclaw-py[server]'",
                exit_code=EXIT_DEPENDENCY,
            ) from e


# 最小工具集合
_MCP_TOOLS: list[dict[str, Any]] = [
    {
        "name": "echo",
        "description": "回显输入文本",
        "inputSchema": {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]},
    },
    {
        "name": "datetime",
        "description": "返回当前 UTC 时间",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "system_info",
        "description": "返回 openclaw-py 版本与运行环境摘要",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _handle_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "echo":
        return {"content": [{"type": "text", "text": arguments.get("message", "")}]}
    if name == "datetime":
        return {"content": [{"type": "text", "text": datetime.now(timezone.utc).isoformat()}]}
    if name == "system_info":
        import openclaw

        return {
            "content": [{
                "type": "text",
                "text": json.dumps({
                    "openclaw_py": openclaw.__version__,
                    "python": os.sys.version.split()[0],
                    "platform": os.sys.platform,
                }, ensure_ascii=False),
            }],
        }
    return {"isError": True, "content": [{"type": "text", "text": f"未知工具: {name}"}]}


def _create_mcp_app() -> Any:
    """构造 FastAPI MCP app。"""
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse, StreamingResponse

    app = FastAPI(title="openclaw-py MCP")

    @app.get("/sse")
    async def sse(request: Request) -> StreamingResponse:
        from sse_starlette.sse import EventSourceResponse

        async def event_generator():
            yield {"event": "endpoint", "data": "/messages"}

        return EventSourceResponse(event_generator())

    @app.post("/messages")
    async def messages(request: Request) -> JSONResponse:
        body = await request.json()
        method = body.get("method")
        if method == "initialize":
            return JSONResponse({"jsonrpc": "2.0", "id": body.get("id"), "result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "openclaw-py-mcp", "version": "0.1.0"}}})
        if method == "tools/list":
            return JSONResponse({"jsonrpc": "2.0", "id": body.get("id"), "result": {"tools": _MCP_TOOLS}})
        if method == "tools/call":
            params = body.get("params", {})
            result = _handle_tool(params.get("name", ""), params.get("arguments", {}))
            return JSONResponse({"jsonrpc": "2.0", "id": body.get("id"), "result": {"content": result.get("content", [])}})
        return JSONResponse({"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32601, "message": f"未知方法: {method}"}})

    return app


def _mcp_app() -> typer.Typer:
    m_app = typer.Typer(help="Model Context Protocol 服务端:serve / tools", no_args_is_help=True)

    @m_app.command("tools")
    def mcp_tools(ctx: typer.Context) -> None:
        """列出 MCP 暴露的工具。"""
        cli_ctx = get_ctx(ctx.obj)
        rows = [[t["name"], t["description"]] for t in _MCP_TOOLS]
        cli_ctx.output.table(["name", "description"], rows, title=f"MCP tools ({len(_MCP_TOOLS)})")

    @m_app.command("serve")
    def mcp_serve(
        ctx: typer.Context,
        host: str = typer.Option("127.0.0.1", "--host", help="监听地址"),
        port: int = typer.Option(18789, "--port", help="监听端口"),
    ) -> None:
        """启动 MCP SSE server。"""
        cli_ctx = get_ctx(ctx.obj)
        _require_server()
        import uvicorn

        app = _create_mcp_app()
        cli_ctx.output.warn(f"启动 MCP server: http://{host}:{port}/sse")
        uvicorn.run(app, host=host, port=port)

    return m_app


def register(app: typer.Typer) -> None:
    app.add_typer(_mcp_app(), name="mcp")


__all__ = ["register"]
