"""``openclaw serve`` / ``openclaw gateway`` —— Gateway 服务管理。

  openclaw serve [--host H] [--port P] [--reload] [--no-agent]
  openclaw gateway run        # serve 的别名
  openclaw gateway health     # 健康检查(GET /healthz + /readyz)
  openclaw gateway status     # 运行状态(GET /metrics + /version)

关键:懒导入 uvicorn/fastapi;绝不 import 模块级 app(会触发 create_app 副作用),
而是自己调 create_app(deps=..., host=host)。
"""
from __future__ import annotations

import os
from typing import Optional

import typer

from openclaw.cli.context import get_ctx
from openclaw.cli.errors import EXIT_DEPENDENCY, CLIError
from openclaw.cli.factory import build_agent_loop, load_config
from openclaw.cli.http import GatewayClient


def _require(extra: str, modules: list[str]) -> None:
    """懒导入可选依赖,缺失给出清晰提示。"""
    import importlib

    for m in modules:
        try:
            importlib.import_module(m)
        except ImportError as e:
            raise CLIError(
                f"缺少可选依赖 [{extra}],请运行: pip install 'openclaw-py[{extra}]'\n原因: {e}",
                exit_code=EXIT_DEPENDENCY,
            ) from e


def _resolve_host_port(cli_ctx, host: Optional[str], port: Optional[int]) -> tuple[str, int]:
    cfg, _ = load_config(cli_ctx.config_path)
    h = host or os.environ.get("OPENCLAW_GATEWAY_HOST") or cfg.channels_runtime.webhook_host or "127.0.0.1"
    p = port or int(os.environ.get("OPENCLAW_GATEWAY_PORT", 0)) or cfg.channels_runtime.webhook_port or 8088
    return h, p


def _do_serve(ctx: typer.Context, host: Optional[str], port: Optional[int], reload: bool, no_agent: bool) -> None:
    """启动 uvicorn gateway。"""
    cli_ctx = get_ctx(ctx.obj)
    _require("server", ["fastapi", "uvicorn"])
    import uvicorn

    h, p = _resolve_host_port(cli_ctx, host, port)

    from openclaw.gateway.app import create_app  # 只 import 函数,不 import 模块级 app
    from openclaw.gateway.deps import GatewayDeps, set_deps

    cfg, cfg_path = load_config(cli_ctx.config_path)
    deps = GatewayDeps(config=cfg, config_path=cfg_path)
    if not no_agent:
        try:
            loop, _ = build_agent_loop(cli_ctx.config_path)
            deps.agent_loop = loop
        except Exception as e:  # noqa: BLE001
            cli_ctx.output.warn(f"agent_loop 构建失败,将以 --no-agent 模式启动: {e}")
    set_deps(deps)

    # host=0.0.0.0 + 无 token 会在 create_app 内 fail-fast
    try:
        app = create_app(deps=deps, host=h)
    except RuntimeError as e:
        raise CLIError(
            f"启动被拒: {e}",
            exit_code=2,
            hint="若监听 0.0.0.0,必须设置 token:export OPENCLAW_GATEWAY_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(32))')",
        ) from e

    cli_ctx.output.warn(f"启动 gateway: http://{h}:{p} (agent={'off' if no_agent else 'on'})")
    uvicorn.run(app, host=h, port=p, reload=reload)


# ---- 顶层 serve 命令 ----

def serve(
    ctx: typer.Context,
    host: Optional[str] = typer.Option(None, "--host", help="监听地址(默认 127.0.0.1)"),
    port: Optional[int] = typer.Option(None, "--port", help="监听端口(默认 8088)"),
    reload: bool = typer.Option(False, "--reload", help="热重载(开发模式)"),
    no_agent: bool = typer.Option(False, "--no-agent", help="不挂载 agent_loop(/v1/chat 将返回 503)"),
) -> None:
    """启动 Gateway HTTP 服务(封装 uvicorn)。"""
    _do_serve(ctx, host, port, reload, no_agent)


# ---- gateway 子命令组 ----

def _gateway_app() -> typer.Typer:
    gw_app = typer.Typer(help="Gateway 服务管理:run / health / status", no_args_is_help=True)

    @gw_app.command("run")
    def gateway_run(
        ctx: typer.Context,
        host: Optional[str] = typer.Option(None, "--host"),
        port: Optional[int] = typer.Option(None, "--port"),
        reload: bool = typer.Option(False, "--reload"),
        no_agent: bool = typer.Option(False, "--no-agent"),
    ) -> None:
        """启动 gateway(serve 的别名)。"""
        _do_serve(ctx, host, port, reload, no_agent)

    @gw_app.command("health")
    def gateway_health(
        ctx: typer.Context,
        url: Optional[str] = typer.Option(None, "--url", help="gateway 地址"),
        token: Optional[str] = typer.Option(None, "--token"),
    ) -> None:
        """健康检查(GET /healthz + /readyz)。"""
        cli_ctx = get_ctx(ctx.obj)
        client = GatewayClient(url, token, timeout=5.0)
        results = {}
        for path in ("/healthz", "/readyz"):
            try:
                data = client.get(path)
                results[path] = data
            except CLIError as e:
                results[path] = {"error": e.message}
        cli_ctx.output.print(results, title="health")

    @gw_app.command("status")
    def gateway_status(
        ctx: typer.Context,
        url: Optional[str] = typer.Option(None, "--url"),
        token: Optional[str] = typer.Option(None, "--token"),
    ) -> None:
        """运行状态(GET /metrics + /version)。"""
        cli_ctx = get_ctx(ctx.obj)
        client = GatewayClient(url, token, timeout=5.0)
        results = {}
        for path in ("/metrics", "/version"):
            try:
                data = client.get(path)
                results[path] = data
            except CLIError as e:
                results[path] = {"error": e.message}
        cli_ctx.output.print(results, title="status")

    @gw_app.command("call")
    def gateway_call(
        ctx: typer.Context,
        method: str = typer.Argument(..., help="RPC 方法名,如 'sessions.list' / 'agents.list'"),
        params: Optional[str] = typer.Option(None, "--params", help="JSON 字符串参数(默认 '{}')"),
        url: Optional[str] = typer.Option(None, "--url"),
        token: Optional[str] = typer.Option(None, "--token"),
    ) -> None:
        """直接调一个 Gateway RPC 方法(POST /v1/rpc)。"""
        import json

        cli_ctx = get_ctx(ctx.obj)
        try:
            params_obj = json.loads(params) if params else {}
        except json.JSONDecodeError as e:
            raise CLIError(f"params 不是合法 JSON: {e}", exit_code=2) from e

        data = GatewayClient(url, token).post("/v1/rpc", json_body={"method": method, "params": params_obj})
        cli_ctx.output.print(data, title=f"rpc {method}")

    @gw_app.command("probe")
    def gateway_probe(
        ctx: typer.Context,
        url: Optional[str] = typer.Option(None, "--url"),
        token: Optional[str] = typer.Option(None, "--token"),
        deep: bool = typer.Option(False, "--deep", help="深入探测(检查 channels / sessions 端点)"),
    ) -> None:
        """探测 gateway 可达性 + auth + 能力(不调 RPC,只打 GET)。"""
        cli_ctx = get_ctx(ctx.obj)
        client = GatewayClient(url, token, timeout=3.0)
        results: dict = {"url": client.base_url}

        # 1. 可达性
        try:
            client.get("/healthz")
            results["reachable"] = True
        except CLIError as e:
            results["reachable"] = False
            results["reachable_error"] = e.message
            cli_ctx.output.print(results, title="probe")
            return

        # 2. auth 能力
        try:
            data = client.get("/version")
            results["auth_ok"] = True
            results["version"] = data.get("version", "?") if isinstance(data, dict) else data
        except CLIError as e:
            results["auth_ok"] = False
            results["auth_error"] = e.message

        # 3. 能力探测
        if deep:
            caps = {}
            for ep, name in (
                ("/v1/sessions", "sessions"),
                ("/v1/channels", "channels"),
                ("/v1/tools", "tools"),
            ):
                try:
                    client.get(ep)
                    caps[name] = "ok"
                except CLIError as e:
                    caps[name] = f"err: {e.message}"
            results["endpoints"] = caps

        cli_ctx.output.print(results, title="probe")

    return gw_app


def register(app: typer.Typer) -> None:
    app.command("serve")(serve)
    app.add_typer(_gateway_app(), name="gateway")


__all__ = ["register"]
