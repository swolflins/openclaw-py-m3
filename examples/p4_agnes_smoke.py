"""Phase 4 真实模型烟测:用 agnes-2.0-flash 跑 6 个工具调用场景。

- calculator (Phase 0)
- shell_exec (Phase 4 新)
- read_file + write_file (Phase 4 新)
- search_files (Phase 4 新)
- get_current_time + date_diff (Phase 4 新)
- cron_add (Phase 4 新)

每跑一个 session 隔离,跑完打印 tool_calls 和最终回复。
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
    from openclaw.agent.loop import AgentLoop
    from openclaw.core.config import ConfigLoader
    from openclaw.core.logging import setup_logging
    from openclaw.memory.long_term import LongTermStore
    from openclaw.memory.scoped import ScopedMemory
    from openclaw.memory.short_term import ShortTermStore
    from openclaw.memory.soul import SoulLoader
    from openclaw.providers.factory import ProviderFactory
    from openclaw.providers.router import ProviderRouter
    from openclaw.tools.builtin import register_builtin_tools
    from openclaw.tools.registry import ToolRegistry

    setup_logging("INFO", json=False)

    cfg_path = ROOT / "openclaw.agnes.yaml"
    cfg = ConfigLoader(cfg_path).load()

    # 准备 fs 沙箱
    work = Path(tempfile.mkdtemp(prefix="openclaw_p4_smoke_"))
    (work / "notes").mkdir()
    (work / "notes" / "todo.md").write_text(
        "# 待办\n- [x] 买菜\n- [ ] 写周报\n- [ ] 跑步 30 分钟\n", encoding="utf-8"
    )
    (work / "data.json").write_text('{"name": "openclaw", "phase": 4}\n', encoding="utf-8")
    print(f"[setup] fs_root = {work}\n")

    factory = ProviderFactory()
    providers = [factory.build(p) for p in cfg.providers]
    primary, fallbacks = providers[0], providers[1:]
    llm = ProviderRouter(primary, fallbacks) if fallbacks else primary

    short = ShortTermStore(cfg.memory.dir)
    long = None
    if cfg.memory.long_term_enabled:
        long = LongTermStore(cfg.memory.dir / "long_term")
    scoped = ScopedMemory(short_term=short, long_term=long, soul=SoulLoader(paths=cfg.agent.soul_paths))

    tools = ToolRegistry()
    register_builtin_tools(
        tools,
        fs_root=str(work),
        shell_default_cwd=str(work),
        shell_allowed=["echo", "ls", "cat", "date", "pwd"],
        http_allowed_hosts=["example.com"],
        include=[
            "calculator", "echo",
            "shell_exec", "read_file", "list_dir", "search_files", "file_stat",
            "get_current_time", "date_diff", "format_time", "parse_time",
            "cron_add", "cron_list", "cron_remove",
        ],
    )
    def _s(x):
        return x.value if hasattr(x, "value") else str(x)

    print(f"[setup] 注册了 {len(tools.list_tools())} 个工具:")
    for t in tools.list_tools():
        print(f"        - {t.name:18s}  cat={_s(t.category):8s}  perm={_s(t.permission)}")
    print()

    loop = AgentLoop(
        llm=llm,
        tools=tools,
        memory=scoped,
        system_prompt=cfg.agent.system_prompt,
        max_tool_iterations=cfg.agent.max_tool_iterations,
        history_window=cfg.agent.history_window,
    )

    scenarios = [
        ("calc",     "用 calculator 计算 137 * 89 + 256,然后告诉我结果是多少"),
        ("shell",    f"在 {work} 目录下用 shell_exec 跑 `ls -la`,把结果原文贴出来"),
        ("fs",       f"用 read_file 读取 {work/'notes'/'todo.md'},再用 echo 把'周报完成情况:待完成 2 项'返回"),
        ("time",     "用 get_current_time 取 UTC 当前时间,然后用 date_diff 算它和 2026-06-19T00:00:00 差多少秒"),
        ("cron",     "用 cron_add 加一个每 300 秒触发一次的周期任务,payload 写 'heartbeat',然后 cron_list 把结果贴出来"),
    ]

    try:
        for tag, q in scenarios:
            print(f"\n{'=' * 70}\n[{tag}] USER: {q}\n{'=' * 70}")
            resp = asyncio.run(loop.handle(f"p4-{tag}", q))
            print(f"[{tag}] iter={resp.iterations}  tool_calls={len(resp.tool_calls)}")
            for tc in resp.tool_calls:
                args = tc.arguments if isinstance(tc.arguments, dict) else {"_": str(tc.arguments)}
                print(f"  -> {tc.name}({args})")
            print(f"[{tag}] ASSISTANT:\n{resp.content}\n")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
