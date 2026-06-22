"""P11 飞书端到端 mock 演示。

在沙箱(WS 出不去)也能跑通完整 dispatch 链路:
  用户发消息 → LarkChannel 解析 → AutoReply → AgentLoop(echo/真 LLM) → send() → reply

跟 examples/lark_run.py 的区别:
  lark_run.py       起真 WS,需要沙箱外运行(本地)
  lark_e2e_mock.py  不起 WS,直接 inject 入站事件,在沙箱里就能跑

支持两种模式:
  1) echo(默认,无需任何 API key)
     python examples/lark_e2e_mock.py

  2) 真 LLM 全链路(沙箱内可看 LLM 真实响应,飞书侧 mock)
     AGENT_BACKEND=openai OPENAI_API_KEY=sk-... python examples/lark_e2e_mock.py
     AGENT_BACKEND=deepseek OPENAI_API_KEY=sk-... python examples/lark_e2e_mock.py
     AGENT_BACKEND=anthropic ANTHROPIC_API_KEY=sk-ant-... python examples/lark_e2e_mock.py
     AGENT_BACKEND=gemini GEMINI_API_KEY=... python examples/lark_e2e_mock.py

输出:
  入站消息 → 中间产物(session_id / message_id / agent 输入)
  → LLM provider 调用详情(model / prompt tokens / latency)
  → tool call(可选)
  → agent 输出 → reply 调用(message_id + text)→ 标记
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lark_oapi.api.im.v1 import (  # noqa: E402
    P2ImMessageReceiveV1,
    P2ImMessageReceiveV1Data,
)
from lark_oapi.api.im.v1.model.event_sender import EventSender  # noqa: E402
from lark_oapi.api.im.v1.model.event_message import EventMessage  # noqa: E402

from openclaw.channels.lark import LarkChannel  # noqa: E402
from openclaw.config.settings import LarkSettings  # noqa: E402


# ---------------- 演示用 echo agent ----------------


class _EchoAgent:
    """演示用 agent:简单 echo。"""

    async def handle(self, session_id, text, **kw):
        class R:
            content = f"🤖 echo: {text}"
            tool_calls = []
            iterations = 1
        return R()

    async def new_session(self, sid=None):
        return sid or "echo"

    @property
    def tools(self): return None
    @property
    def memory(self): return None
    @property
    def auto_reply(self): return None


# ---------------- 真 LLM agent(带 trace 包装) ----------------


def _wrap_provider_with_trace(provider, name: str):
    """包一层 acomplete 的 trace,显示请求/响应 timing 和内容。

    通过 monkey-patch provider.acomplete,显示中间状态,不影响 provider 逻辑。
    """
    orig_acomplete = provider.acomplete
    async def traced_acomplete(messages, tools=None, *, temperature=0.7, max_tokens=None):
        t0 = time.perf_counter()
        sys.stderr.write(
            f"\n  🤖 LLM 请求  provider={name} model={provider.model}\n"
            f"     messages={len(messages)} 条  temperature={temperature}  max_tokens={max_tokens}\n"
        )
        # 找 system prompt + 最后一条 user 消息
        system_msg = next(
            (m.content for m in messages if m.role == "system"), ""
        )
        sys.stderr.write(f"     system={system_msg[:80]!r}\n")
        last_user = next(
            (m.content for m in reversed(messages) if m.role == "user"), ""
        )
        sys.stderr.write(f"     user(last)={last_user[:120]!r}\n")
        if tools:
            tool_names = [t.name for t in tools]
            sys.stderr.write(f"     tools 可用={len(tools)} 个: {tool_names[:5]}{'...' if len(tool_names)>5 else ''}\n")
        sys.stderr.flush()

        result = await orig_acomplete(
            messages, tools,
            temperature=temperature, max_tokens=max_tokens,
        )

        dt = (time.perf_counter() - t0) * 1000
        sys.stderr.write(
            f"  🤖 LLM 响应  latency={dt:.0f}ms\n"
            f"     content={result.content[:200]!r}\n"
            f"     tool_calls={len(result.tool_calls)} 个"
        )
        if result.tool_calls:
            for tc in result.tool_calls:
                sys.stderr.write(f"\n       → {tc.name}({json.dumps(tc.arguments, ensure_ascii=False)[:80]})")
        sys.stderr.write("\n")
        sys.stderr.flush()
        return result

    provider.acomplete = traced_acomplete  # type: ignore[method-assign]
    return provider


def _make_real_agent():
    """根据 AGENT_BACKEND 选真 LLM provider,接 AgentLoop(llm + tools + memory)。"""
    backend = os.environ.get("AGENT_BACKEND", "echo").lower()

    if backend == "echo":
        return _EchoAgent()

    if backend not in ("openai", "anthropic", "gemini", "ollama", "deepseek", "mock"):
        print(f"❌ 未知 AGENT_BACKEND={backend!r}", file=sys.stderr)
        sys.exit(2)

    from openclaw.agent.loop import AgentLoop
    from openclaw.providers.openai_compat import OpenAICompatProvider
    from openclaw.providers.anthropic import AnthropicProvider
    from openclaw.providers.gemini import GeminiProvider
    from openclaw.tools.builtin import register_builtin_tools
    from openclaw.tools.registry import ToolRegistry
    from openclaw.llm.base import BaseLLMProvider, LLMResult
    from openclaw.memory import ShortTermStore, ScopedMemory

    # ----- mock 后端:不需要任何 API key,用本地假 LLM 演示全链路 -----
    if backend == "mock":
        class _MockProvider(BaseLLMProvider):
            """假 LLM:基于关键字模式匹配生成响应,演示全链路 trace。

            关键:看上一条消息如果是 tool role,就生成总结回复(否则无限循环)。
            """
            _iter = 0  # 类级迭代计数

            async def acomplete(self, messages, tools=None, *, temperature=0.7, max_tokens=None):
                import asyncio
                await asyncio.sleep(0.2)  # 模拟网络延迟

                # 看最后一条消息的角色
                last = messages[-1] if messages else None
                last_role = getattr(last, "role", "user") if last else "user"

                # 拿到上一轮 user 消息(可能是 tool_results 中的原始 query)
                user = next(
                    (m.content for m in reversed(messages) if m.role == "user"), ""
                ).lower()

                # 工具执行完毕 → 生成总结
                if last_role == "tool":
                    _MockProvider._iter += 1
                    return LLMResult(
                        content=f"我已经通过 shell_exec 查看了目录,完成了你的请求。(mock 模式,迭代 {_MockProvider._iter})",
                        tool_calls=[],
                    )

                # 工具调用检测:问"文件"或"目录"时,假装调 shell_exec
                if "文件" in user or "目录" in user or "list" in user:
                    if tools:
                        for t in tools:
                            if t.name == "shell_exec":
                                return LLMResult(
                                    content="",
                                    tool_calls=[__import__(
                                        "openclaw.llm.base", fromlist=["ToolCall"]
                                    ).ToolCall(
                                        id="call_1",
                                        name="shell_exec",
                                        arguments={"command": "ls -la", "timeout": 5},
                                    )],
                                )
                # 关键字回复
                replies = {
                    "你好": "你好!我是 mock LLM,在沙箱里演示全链路 trace。",
                    "求和": "1 + 1 = 2 (mock 模式不真算)",
                    "时间": "现在时间:2026-06-22 (mock 数据)",
                }
                for k, v in replies.items():
                    if k in user:
                        return LLMResult(content=v, tool_calls=[])
                return LLMResult(
                    content=f"[mock LLM 收到] {user[:100]!r}", tool_calls=[]
                )

        llm = _MockProvider(model="mock-1.0")
        sys.stderr.write("\n  🔧 启动 Mock provider(无需 API key,本地假 LLM 演示全链路)\n")
        sys.stderr.flush()
        _wrap_provider_with_trace(llm, "Mock")

        fs_root = os.environ.get("OPENCLAW_FS_ROOT", os.getcwd())
        shell_cwd = os.environ.get("OPENCLAW_SHELL_CWD", os.getcwd())
        registry = ToolRegistry()
        register_builtin_tools(registry, fs_root=fs_root, shell_default_cwd=shell_cwd)
        # mock 模式:给 registry 设置 always-approve 避免 fail-closed
        async def _approve(name, args):
            return True
        registry.set_approver(_approve)
        # mock 模式:用临时目录的 ShortTermStore 当 memory
        import tempfile
        tmpdir = tempfile.mkdtemp(prefix="openclaw_mock_")
        memory = ScopedMemory(short_term=ShortTermStore(tmpdir))
        agent = AgentLoop(
            llm=llm,
            tools=registry,
            memory=memory,
            system_prompt="你是一个简洁的中文助手。",
        )
        sys.stderr.write(f"  ✅ AgentLoop 就绪  tools={len(registry.list_tools())} 个  memory={tmpdir}\n\n")
        sys.stderr.flush()
        return agent

    backend_cfg = {
        "openai":    ("OpenAI",   OpenAICompatProvider, "gpt-4o-mini",        "https://api.openai.com/v1"),
        "deepseek":  ("DeepSeek", OpenAICompatProvider, "deepseek-chat",      "https://api.deepseek.com/v1"),
        "ollama":    ("Ollama",   OpenAICompatProvider, "qwen2.5:7b",         "http://127.0.0.1:11434/v1"),
        "anthropic": ("Anthropic",AnthropicProvider,    "claude-haiku-4-5",   None),
        "gemini":    ("Gemini",   GeminiProvider,      "gemini-2.0-flash",   None),
    }[backend]

    name, ProviderCls, default_model, default_url = backend_cfg
    model = os.environ.get("AGENT_MODEL", default_model)
    api_key = (
        os.environ.get("OPENAI_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("GEMINI_API_KEY")
        or "ollama"
    )

    sys.stderr.write(f"\n  🔧 启动 {name} provider  model={model}\n")
    sys.stderr.flush()

    if ProviderCls is OpenAICompatProvider:
        llm = OpenAICompatProvider(
            api_key=api_key,
            base_url=os.environ.get("AGENT_BASE_URL", default_url),
            model=model,
        )
    elif ProviderCls is AnthropicProvider:
        llm = AnthropicProvider(api_key=api_key, model=model)
    else:  # GeminiProvider
        llm = GeminiProvider(api_key=api_key, model=model)

    # 包一层 trace
    _wrap_provider_with_trace(llm, name)

    # 全链路:tools + memory
    fs_root = os.environ.get("OPENCLAW_FS_ROOT", os.getcwd())
    shell_cwd = os.environ.get("OPENCLAW_SHELL_CWD", os.getcwd())
    # 注意:沙箱里 /workspace 是真实 cwd(别用 os.getcwd() 跑根目录)
    registry = ToolRegistry()
    register_builtin_tools(registry, fs_root=fs_root, shell_default_cwd=shell_cwd)

    try:
        from openclaw.memory import create_memory
        memory = create_memory(backend=os.environ.get("OPENCLAW_MEMORY", "memory"))
    except Exception as e:
        sys.stderr.write(f"  ⚠️  memory 启动失败,退回 None: {e}\n")
        memory = None

    agent = AgentLoop(
        llm=llm,
        tools=registry,
        memory=memory,
        system_prompt=os.environ.get(
            "AGENT_SYSTEM_PROMPT",
            "你是一个简洁的中文助手,回答控制在 80 字以内。",
        ),
    )
    sys.stderr.write(f"  ✅ AgentLoop 就绪  tools={len(registry.list_tools())} 个\n\n")
    sys.stderr.flush()
    return agent


# ---------------- 事件构造 + 主流程(不变)----------------


def _make_event(chat_id: str, open_id: str, message_id: str, text: str) -> P2ImMessageReceiveV1:
    evt = P2ImMessageReceiveV1()
    evt.event = P2ImMessageReceiveV1Data()
    evt.event.sender = EventSender(
        d={"sender_id": {"open_id": open_id, "union_id": "u", "user_id": "u"},
           "sender_type": "user", "tenant_key": "tk"}
    )
    evt.event.message = EventMessage(
        d={
            "message_id": message_id,
            "chat_id": chat_id,
            "chat_type": "p2p",
            "message_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
    )
    return evt


def _print_banner(text: str) -> None:
    print()
    print("─" * 60)
    print(f"  模拟用户发: {text!r}")
    print("─" * 60)


async def main_async(text: str, chat_id: str, open_id: str, message_id: str) -> None:
    _print_banner(text)

    replies: list[tuple[str, str]] = []

    async def _fake_reply(self, msg_id, body):
        replies.append((msg_id, body))

    from openclaw.channels import lark as _lark_mod
    _real = _lark_mod.LarkChannel._reply_to_lark
    _lark_mod.LarkChannel._reply_to_lark = _fake_reply  # type: ignore[assignment]

    try:
        # 关键改动:agent 可选 echo 或 真 LLM
        agent = _make_real_agent()
        ch = LarkChannel(
            agent,
            LarkSettings(app_id="cli_aabf7da5e178dbb5", app_secret="mock"),
        )

        evt = _make_event(chat_id, open_id, message_id, text)
        t0 = time.perf_counter()
        await ch._handle_event(evt)
        dt_total = (time.perf_counter() - t0) * 1000

        if not ch.received:
            print("  ❌ 消息被 AutoReply drop(可能不是 @bot)")
            return

        r = ch.received[0]
        print(f"  session_id       = {r.session_id}")
        print(f"  user_id          = {r.user_id}")
        print(f"  text             = {r.text!r}")
        print(f"  metadata.is_dm   = {r.metadata.get('is_dm')}")
        print(f"  metadata.msg_id  = {r.metadata.get('message_id')}")
        print(f"  ⏱  整链路耗时     = {dt_total:.0f}ms (含 LLM 调用)")

        if replies:
            msg_id, body = replies[0]
            print(f"  → reply 到 msg_id = {msg_id}")
            print(f"  → reply text     = {body!r}")
            print("  ✅ 端到端链路通:WS 收到 → dispatch → agent → reply")
        else:
            print("  ❌ 没生成 reply(被 AutoReply drop 或 agent 返空)")
    finally:
        _lark_mod.LarkChannel._reply_to_lark = _real


def main() -> None:
    p = argparse.ArgumentParser(description="飞书 e2e mock 演示(支持 echo / 真 LLM)")
    p.add_argument("--text", default="ping", help="模拟用户发的消息")
    p.add_argument("--chat-id", default="oc_demo_chat")
    p.add_argument("--open-id", default="ou_demo_user")
    p.add_argument("--message-id", default="om_demo_msg")
    args = p.parse_args()

    backend = os.environ.get("AGENT_BACKEND", "echo")
    print("\n=== 飞书 e2e mock(沙箱可跑)===")
    print(f"  backend = {backend}")
    print("  模拟飞书 WS 收到一条 P2ImMessageReceiveV1 事件")
    print("  走 LarkChannel._handle_event → dispatch → agent → reply")
    if backend == "echo":
        print("  走 echo 模式(不调任何 API)")
    else:
        print(f"  走真 LLM({backend}),reply 调用被拦下打印,不真发飞书")

    asyncio.run(main_async(args.text, args.chat_id, args.open_id, args.message_id))
    print("\n  下一步:本地跑 examples/lark_run.py,真接 WS → 真发飞书\n")


if __name__ == "__main__":
    main()
