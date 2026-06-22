"""``openclaw agents`` —— 多 Agent 管理(Phase 5 multi_agent 体系)。

对 Python 端 ``openclaw.agent.multi_agent`` 的 CLI 暴露。Python 端 multi_agent
是单进程内角色编排(Planner/Executor/Critic/Reflector),不像 TS 端有完整多
agent routing,但本命令组给运营一个可观察的入口。

子命令:
  list              列出已注册 agent 配置(cfg.agents.list)
  show AGENT        查看某 agent 详情(name / role / model / tools)
  add NAME          新增 agent 到配置
  delete NAME       从配置移除 agent
  run NAME -m MSG   用指定 agent 单轮调用
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from openclaw.cli.context import get_ctx
from openclaw.cli.errors import CLIError, EXIT_CONFIG, EXIT_NOT_FOUND
from openclaw.cli.factory import load_config


def _resolve_agents(cfg) -> list[dict]:
    """从 OpenClawConfig.agents 解析 agent 列表,容错空配置。"""
    agents = getattr(cfg, "agents", None)
    if agents is None:
        return []
    if isinstance(agents, list):
        return [a for a in agents if isinstance(a, dict)]
    if isinstance(agents, dict):
        return [agents]
    return []


def _save_agents(cfg, agents: list[dict], config_path: Optional[Path]) -> None:
    """把 agents 列表写回配置文件(原子写:tmp + os.replace)。"""
    import json
    import os
    import tempfile

    import yaml

    raw: dict = {}
    if config_path and config_path.exists():
        text = config_path.read_text(encoding="utf-8")
        if config_path.suffix in (".yaml", ".yml"):
            raw = yaml.safe_load(text) or {}
        elif config_path.suffix == ".json":
            raw = json.loads(text)
        elif config_path.suffix == ".toml":
            import tomllib

            raw = tomllib.loads(text)
    raw["agents"] = agents

    target = config_path or Path("openclaw.yaml")
    if target.suffix in (".yaml", ".yml"):
        out = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
    elif target.suffix == ".json":
        out = json.dumps(raw, ensure_ascii=False, indent=2)
    else:
        out = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False)
    # 原子写:写 tmp,fsync,os.replace
    fd, tmp = tempfile.mkstemp(prefix=".openclaw_", dir=str(target.parent or Path(".")))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(out)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _agents_app() -> typer.Typer:
    ag_app = typer.Typer(help="多 Agent 管理:list / show / add / delete / run", no_args_is_help=True)

    @ag_app.command("list")
    def agents_list(ctx: typer.Context) -> None:
        """列出已注册 agent(从 cfg.agents)。"""
        cli_ctx = get_ctx(ctx.obj)
        cfg, _ = load_config(cli_ctx.config_path)
        agents = _resolve_agents(cfg)

        if not agents:
            cli_ctx.output.warn("未配置 agent。运行 `openclaw agents add NAME` 添加。")
            return

        rows = [
            [a.get("name", "?"), a.get("role", "default"), a.get("model", "default"), ",".join(a.get("tools", []) or [])[:50]]
            for a in agents
        ]
        cli_ctx.output.table(["name", "role", "model", "tools"], rows, title=f"agents ({len(agents)})")

    @ag_app.command("show")
    def agents_show(
        ctx: typer.Context,
        name: str = typer.Argument(..., help="agent 名"),
    ) -> None:
        """查看某 agent 详情。"""
        cli_ctx = get_ctx(ctx.obj)
        cfg, _ = load_config(cli_ctx.config_path)
        agents = _resolve_agents(cfg)
        for a in agents:
            if a.get("name") == name:
                cli_ctx.output.print(a, title=f"agent: {name}")
                return
        raise CLIError(f"agent 不存在: {name}", exit_code=EXIT_NOT_FOUND)

    @ag_app.command("add")
    def agents_add(
        ctx: typer.Context,
        name: str = typer.Argument(..., help="agent 名(唯一)"),
        role: str = typer.Option("default", "--role", help="角色:default / planner / executor / critic"),
        model: Optional[str] = typer.Option(None, "--model", help="模型(provider/model),默认用全局 router"),
        tools: Optional[str] = typer.Option(None, "--tools", help="工具列表(逗号分隔),空=全部"),
    ) -> None:
        """新增 agent 到配置。"""
        cli_ctx = get_ctx(ctx.obj)
        cfg, cfg_path = load_config(cli_ctx.config_path)
        agents = _resolve_agents(cfg)

        if any(a.get("name") == name for a in agents):
            raise CLIError(f"agent 已存在: {name}", exit_code=EXIT_CONFIG)

        new = {"name": name, "role": role}
        if model:
            new["model"] = model
        if tools:
            new["tools"] = [t.strip() for t in tools.split(",") if t.strip()]
        agents.append(new)
        _save_agents(cfg, agents, cfg_path)
        cli_ctx.output.success(f"已添加 agent: {name} (role={role})")

    @ag_app.command("delete")
    def agents_delete(
        ctx: typer.Context,
        name: str = typer.Argument(..., help="agent 名"),
    ) -> None:
        """从配置删除 agent。"""
        cli_ctx = get_ctx(ctx.obj)
        cfg, cfg_path = load_config(cli_ctx.config_path)
        agents = _resolve_agents(cfg)
        new_agents = [a for a in agents if a.get("name") != name]
        if len(new_agents) == len(agents):
            raise CLIError(f"agent 不存在: {name}", exit_code=EXIT_NOT_FOUND)
        _save_agents(cfg, new_agents, cfg_path)
        cli_ctx.output.success(f"已删除 agent: {name}")

    @ag_app.command("run")
    def agents_run(
        ctx: typer.Context,
        name: str = typer.Argument(..., help="agent 名(必须已 add 过)"),
        message: str = typer.Option(..., "--message", "-m", help="用户消息"),
        session: str = typer.Option("default", "--session", "-s"),
    ) -> None:
        """用指定 agent 单轮调用(走 AgentLoop.handle,不经 gateway)。"""
        cli_ctx = get_ctx(ctx.obj)
        from openclaw.cli.factory import build_agent_loop
        from openclaw.llm.base import ChatMessage

        cfg, _ = load_config(cli_ctx.config_path)
        agents = _resolve_agents(cfg)
        agent_cfg = next((a for a in agents if a.get("name") == name), None)
        if agent_cfg is None:
            raise CLIError(f"agent 不存在: {name}", exit_code=EXIT_NOT_FOUND)

        try:
            loop, _ = build_agent_loop(config_path=cli_ctx.config_path)
        except Exception as e:  # noqa: BLE001
            raise CLIError(f"agent_loop 构建失败: {e}", exit_code=EXIT_CONFIG) from e

        # 若 agent_cfg 有 system_prompt/tools,临时 patch loop
        if agent_cfg.get("system_prompt"):
            loop.system_prompt = agent_cfg["system_prompt"]
        if agent_cfg.get("tools"):
            from openclaw.tools.registry import ToolRegistry

            sub_registry = ToolRegistry()
            for t_name in agent_cfg["tools"]:
                if loop.tools is not None and t_name in [t.name for t in loop.tools.list_tools()]:
                    # Phase 25 fix: copy tool to sub-registry (只引用同名)
                    from openclaw.tools.registry import ToolSpec

                    full = next(s for s in loop.tools.list_tools() if s.name == t_name)
                    sub_registry.register(ToolSpec(
                        name=full.name,
                        description=full.description,
                        parameters=full.parameters,
                        handler=full.handler,
                        category=full.category,
                        permission=full.permission,
                    ))
            loop.tools = sub_registry

        import asyncio

        async def _run() -> str:
            # memory 走 cfg 默认 scope
            scope = f"agent:{name}:{session}"
            messages = [ChatMessage(role="user", content=message)]
            if loop.memory is not None:
                try:
                    history = await loop.memory.build_messages(scope, max_messages=10)
                    messages = history + messages
                except Exception:  # noqa: BLE001
                    pass
            result = await loop.llm.acomplete(messages, tools=loop.tools.list_tools() if loop.tools else None)
            if loop.memory is not None:
                try:
                    await loop.memory.append(scope, role="user", content=message)
                    await loop.memory.append(scope, role="assistant", content=result.content)
                except Exception:  # noqa: BLE001
                    pass
            return result.content

        content = asyncio.run(_run())
        cli_ctx.output.print({"agent": name, "session": session, "response": content}, title=f"agent {name} run")

    return ag_app


def register(app: typer.Typer) -> None:
    app.add_typer(_agents_app(), name="agents")


__all__ = ["register"]
