"""Phase 5 真实模型烟测:Planner→Executor→Critic 端到端。

覆盖:
- Planner 拆解用户问题
- Executor 并行执行 tool + llm
- Critic 校验答案
- Router 策略(fallback_only,默认)

跑法:
    python examples/phase5_smoke.py
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> None:
    from openclaw.agent import MultiAgentRoles
    from openclaw.core.config import ConfigLoader
    from openclaw.core.logging import setup_logging
    from openclaw.memory.scoped import ScopedMemory
    from openclaw.memory.short_term import ShortTermStore
    from openclaw.memory.soul import SoulLoader
    from openclaw.providers.factory import ProviderFactory
    from openclaw.providers.router import ProviderRouter
    from openclaw.tools.builtin import register_builtin_tools
    from openclaw.tools.registry import ToolRegistry

    setup_logging("INFO", json=False)

    cfg = ConfigLoader(ROOT / "openclaw.agnes.yaml").load()
    factory = ProviderFactory()
    providers = [factory.build(p) for p in cfg.providers]
    primary, fallbacks = providers[0], providers[1:]
    llm = ProviderRouter(primary, fallbacks, strategy="fallback_only")

    work = Path(tempfile.mkdtemp(prefix="openclaw_p5_smoke_"))
    (work / "team").mkdir()
    (work / "team" / "members.md").write_text(
        "# 团队\n- Alice (PM)\n- Bob (Engineer)\n- Carol (Designer)\n", encoding="utf-8"
    )
    print(f"[setup] fs_root = {work}")

    short = ShortTermStore(cfg.memory.dir)
    soul = SoulLoader(paths=cfg.agent.soul_paths)
    scoped = ScopedMemory(short_term=short, long_term=None, soul=soul)

    tools = ToolRegistry()
    register_builtin_tools(
        tools,
        fs_root=str(work),
        shell_default_cwd=str(work),
        shell_allowed=["ls", "echo", "cat", "date"],
        include=[
            "calculator", "echo",
            "shell_exec", "read_file", "list_dir", "search_files",
            "get_current_time", "date_diff",
        ],
    )
    print(f"[setup] 注册了 {len(tools.list_tools())} 个工具")

    ma = MultiAgentRoles(llm, tools, scoped, session_id="p5-smoke",
                         enable_critic=True, enable_reflector=True,
                         max_reflection_loops=1)

    scenarios = [
        ("Q1", "现在 UTC 几点?然后用计算器把当前小时数乘以 60 给我。"),
        ("Q2", f"用 shell_exec 跑 `cat {work/'team'/'members.md'}` 给我看团队成员"),
    ]

    try:
        for tag, q in scenarios:
            print(f"\n{'=' * 70}\n[{tag}] USER: {q}\n{'=' * 70}")
            res = asyncio.run(ma.run(q))
            print(f"[{tag}] 计划: {len(res.plan.steps)} 步, "
                  f"执行: {len(res.execution.steps)} 项, "
                  f"finished={res.execution.finished}, "
                  f"reflections={len(res.reflections)}")
            for r in res.execution.steps:
                smap = res.plan.step_map()
                s = smap.get(r.step_id)
                kind = s.kind.value if s else "?"
                name = s.name or "?" if s else "?"
                status = r.status.value
                out = str(r.output)[:80] if r.output is not None else ""
                print(f"   {status:8s} {kind:5s} {name:18s} -> {out}")
            if res.critic:
                print(f"[{tag}] Critic: ok={res.critic.get('ok')} "
                      f"score={res.critic.get('score')} issues={res.critic.get('issues')}")
            print(f"[{tag}] ANSWER:\n{res.final_answer}\n")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
