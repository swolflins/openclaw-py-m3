"""Phase 6 真实模型烟测:Auto-Reply + Skills 端到端。

跑法:
    python examples/phase6_smoke.py
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
    from openclaw.agent import AgentLoop
    from openclaw.core.auto_reply import AutoReplyConfig, AutoReplyManager
    from openclaw.core.config import ConfigLoader
    from openclaw.core.logging import setup_logging
    from openclaw.core.rate_limit import RateLimiter
    from openclaw.core.skills import load_skills
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

    # ==== 1) 工具 + skills ====
    tools = ToolRegistry()
    register_builtin_tools(
        tools,
        fs_root=str(ROOT),
        shell_default_cwd=str(ROOT),
        shell_allowed=["ls", "echo", "cat", "date"],
        include=[
            "calculator", "echo",
            "shell_exec", "read_file", "list_dir",
            "get_current_time",
        ],
    )
    skills_dir = ROOT / "examples" / "skills"
    sreg = load_skills(skills_dir, registry=tools)
    print(f"[setup] builtin tools: {len([t for t in tools.list_tools()])}")
    print(f"[setup] loaded skills: {[s.name for s in sreg.skills()]}")
    print(f"[setup] prompt injections:\n{sreg.prompt_injections()[:300]}")

    # 把 skill 提示拼到 system_prompt
    sys_prompt = cfg.agent.system_prompt + "\n\n# 加载的 Skills\n" + sreg.prompt_injections()

    # ==== 2) 记忆 ====
    work = Path(tempfile.mkdtemp(prefix="openclaw_p6_smoke_"))
    cfg.memory.dir = work / "memory"
    cfg.memory.dir.mkdir(parents=True, exist_ok=True)
    short = ShortTermStore(cfg.memory.dir)
    scoped = ScopedMemory(short_term=short, long_term=None, soul=SoulLoader(paths=cfg.agent.soul_paths))

    # ==== 3) Agent ====
    loop = AgentLoop(
        llm=llm, tools=tools, memory=scoped,
        system_prompt=sys_prompt,
        max_tool_iterations=cfg.agent.max_tool_iterations,
        history_window=cfg.agent.history_window,
    )

    # ==== 4) Auto-Reply 决策器 ====
    arm = AutoReplyManager(AutoReplyConfig(
        triggers=["bot", "claw"],
        blacklist=[r"rm\s+-rf", r"格式化"],
        templates={"ping": "pong"},
        auto_in_dm=True,
        rate_per_user=RateLimiter(rate=0.5, burst=2),
        quiet_hours=("00:00", "00:01"),  # 基本不触发,只为演示字段
    ))

    scenarios = [
        ("user1", "feishu", "bot 给我讲个笑话", False, True),
        ("user2", "feishu", "rm -rf / 别动", False, False),
        ("user3", "feishu", "claw 查 beijing 天气", False, True),
        ("user4", "feishu", "ping", False, True),   # 模板回复
        ("user5", "feishu", "闲聊一句,谁都别说", False, False),  # not addressed
        ("user6", "feishu", "bot 查本机状态", False, True),
    ]

    try:
        for uid, ch, text, _is_dm, expected_passthrough in scenarios:
            decision = asyncio.run(arm.decide(uid, ch, text, metadata={"is_dm": False}))
            mark = "✓" if decision.passthrough == expected_passthrough else "✗"
            print(f"\n{mark} [{uid}] {text!r} -> passthrough={decision.passthrough} reply={decision.reply!r} reason={decision.reason!r}")
            if decision.reply:
                print(f"    [直接发模板] {decision.reply}")
                continue
            if not decision.passthrough:
                print(f"    [丢弃] {decision.reason}")
                continue
            full_prompt = (decision.prompt_prefix or "") + text
            resp = asyncio.run(loop.handle(uid, full_prompt))
            print(f"    [iter={resp.iterations} tools={len(resp.tool_calls)}]")
            for tc in resp.tool_calls:
                args = tc.arguments if isinstance(tc.arguments, dict) else {"_": str(tc.arguments)}
                print(f"      -> {tc.name}({args})")
            print(f"    ANSWER: {resp.content[:200]}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
