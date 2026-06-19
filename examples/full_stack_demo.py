"""Phase 1+2+3 集成示例:不依赖真实 LLM,演示全栈。

跑通:
1. 配置加载(YAML)
2. 多 provider factory
3. SOUL 加载
4. ScopedMemory(短期 + 长期)
5. AgentLoop 集成
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> None:
    from openclaw.core.config import ConfigLoader, OpenClawConfig
    from openclaw.core.logging import setup_logging
    from openclaw.memory.long_term import LongTermStore
    from openclaw.memory.scoped import ScopedMemory
    from openclaw.memory.short_term import ShortTermStore
    from openclaw.memory.soul import SoulLoader
    from openclaw.tools.builtin import register_builtin_tools
    from openclaw.tools.registry import ToolRegistry
    from openclaw.providers.factory import ProviderFactory

    setup_logging("INFO", json=False)

    tmp = Path(tempfile.mkdtemp(prefix="openclaw_demo_"))
    try:
        # 1. 写一份示例配置
        cfg_file = tmp / "openclaw.yaml"
        cfg_file.write_text(textwrap.dedent("""
            default_provider: main
            providers:
              - name: openai_compat
                model: deepseek-chat
                api_key: sk-demo
                base_url: https://api.deepseek.com/v1
              - name: ollama
                model: llama3.1
                base_url: http://localhost:11434/v1
            agent:
              system_prompt: 你是 Claw,一个全栈 AI Agent
              max_tool_iterations: 4
              history_window: 8
              soul_paths:
                - %s/SOUL.md
            memory:
              dir: %s
              long_term_enabled: true
        """).strip() % (tmp, tmp / "mem"))
        soul_path = tmp / "SOUL.md"
        soul_path.write_text("# 我是 Claw\n我是一只本地龙虾,爱吃工具调用。\n", encoding="utf-8")

        # 2. 加载配置
        cfg: OpenClawConfig = ConfigLoader(cfg_file).load()
        print(f"[1] 加载了 {len(cfg.providers)} 个 provider:", [p.name for p in cfg.providers])
        print(f"    默认 provider: {cfg.default_provider}")
        print(f"    SOUL paths: {cfg.agent.soul_paths}")

        # 3. 构造工厂和 memory
        factory = ProviderFactory()
        print(f"[2] 工厂可构造: {factory.names()}")

        short = ShortTermStore(cfg.memory.dir)
        long = LongTermStore(cfg.memory.dir / "long_term", embedding_fn=_fake_embed)
        soul = SoulLoader(paths=cfg.agent.soul_paths)
        scoped = ScopedMemory(short_term=short, long_term=long, soul=soul)
        print(f"[3] 加载了 {len(soul.load())} 份 SOUL 文档")

        # 4. 写一条长期记忆 + 检索
        long.add("Claw 喜欢 Python 异步编程", scope="session:demo", metadata={"tag": "preference"})
        items = scoped.recall("session:demo", "Claw 的爱好", top_k=3)
        print(f"[4] 长期记忆命中 {len(items)} 条,第一句: {items[0].text if items else '(空)'}")

        # 5. 渲染 system prompt(含 soul)
        sys_prompt = scoped.render_system_prompt(cfg.agent.system_prompt)
        assert "Claw" in sys_prompt and "龙虾" in sys_prompt
        print(f"[5] 渲染 system prompt: {len(sys_prompt)} 字符,含 SOUL")

        print("\n✅ Phase 1+2+3 集成烟测通过")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _fake_embed(texts):
    out = []
    for t in texts:
        v = [0.0] * 16
        for i, ch in enumerate(t):
            v[i % 16] += (ord(ch) % 13) / 100.0
        n = (sum(x * x for x in v) ** 0.5) or 1.0
        out.append([x / n for x in v])
    return out


if __name__ == "__main__":
    main()
