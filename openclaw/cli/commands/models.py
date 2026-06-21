"""``openclaw models`` —— LLM provider 管理。

子命令:
  list     列出已配置 provider + 工厂支持的可构造类型
  status   显示 router 状态(熔断器 / 调用统计),可选 --ping 实测连通性
"""
from __future__ import annotations


import typer

from openclaw.cli.context import get_ctx
from openclaw.cli.factory import load_config


def _models_app() -> typer.Typer:
    models_app = typer.Typer(help="LLM provider 管理:list / status", no_args_is_help=True)

    @models_app.command("list")
    def models_list(ctx: typer.Context) -> None:
        """列出已配置的 provider 及工厂支持的可构造类型。"""
        cli_ctx = get_ctx(ctx.obj)
        cfg, _ = load_config(cli_ctx.config_path)

        from openclaw.providers.factory import ProviderFactory

        supported = ProviderFactory().names()

        rows = []
        for i, p in enumerate(cfg.providers):
            api_key_set = p.api_key is not None and bool(
                p.api_key.get_secret_value() if hasattr(p.api_key, "get_secret_value") else p.api_key
            )
            rows.append([
                i,
                p.name,
                p.model,
                p.base_url or "(默认)",
                "✓ 已设" if api_key_set else "✗ 未设",
                "primary" if i == 0 else "fallback",
            ])

        cli_ctx.output.table(
            ["#", "provider", "model", "base_url", "key_status", "role"],
            rows,
            title="已配置 provider",
        )
        cli_ctx.output.print({"factory_supported": supported}, title="工厂支持的可构造类型")

    @models_app.command("status")
    def models_status(
        ctx: typer.Context,
        ping: bool = typer.Option(False, "--ping", help="对每个 provider 发一次 ping 请求实测连通性"),
    ) -> None:
        """显示 router 状态:熔断器 + 调用统计。"""
        cli_ctx = get_ctx(ctx.obj)
        cfg, _ = load_config(cli_ctx.config_path)

        from openclaw.cli.factory import build_router
        from openclaw.providers.router import ProviderRouter, _prov_key

        llm = build_router(cfg)
        # 单 provider 时 build_router 返回的是裸 provider,不是 router
        if isinstance(llm, ProviderRouter):
            router = llm
            providers = router._providers
            strategy = router.strategy
            primary_name = router.primary.__class__.__name__
        else:
            router = None
            providers = [llm]
            strategy = "(single)"
            primary_name = llm.__class__.__name__

        rows = []
        ping_results: list[dict] = []
        for p in providers:
            key = _prov_key(p)
            state = router.breaker.state_of(key) if router else "n/a"
            stat = router.stats.by_provider.get(key, {}) if router else {}
            rows.append([
                p.__class__.__name__,
                getattr(p, "model", "?"),
                key,
                state,
                stat.get("ok", 0) if isinstance(stat, dict) else 0,
                stat.get("fail", 0) if isinstance(stat, dict) else 0,
            ])

        cli_ctx.output.table(
            ["provider", "model", "breaker_key", "breaker_state", "ok", "fail"],
            rows,
            title="provider 状态",
        )
        cli_ctx.output.print({"strategy": strategy, "primary": primary_name})

        if ping:
            import asyncio
            from openclaw.llm.base import ChatMessage

            for p in providers:
                key = _prov_key(p)
                try:
                    asyncio.run(
                        p.acomplete([ChatMessage(role="user", content="ping")])
                    )
                    ping_results.append({"provider": key, "status": "ok"})
                    cli_ctx.output.success(f"ping {key}: ok")
                except Exception as e:  # noqa: BLE001
                    ping_results.append({"provider": key, "status": "fail", "error": str(e)})
                    cli_ctx.output.warn(f"ping {key}: {e}")
            cli_ctx.output.print({"ping": ping_results})

    return models_app


def register(app: typer.Typer) -> None:
    app.add_typer(_models_app(), name="models")


__all__ = ["register"]
