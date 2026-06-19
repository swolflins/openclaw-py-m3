"""飞书长连接 bot 启动器 — 真监听、真回信。

用法:
  # 1) 凭据(env 或 env 文件)
  export LARK_APP_ID=cli_xxx
  export LARK_APP_SECRET=xxx
  # (可选)AgentLoop 后端: 默认 echo;设置 AGENT_BACKEND=openai 或 anthropic 走真 LLM
  # export OPENAI_API_KEY=sk-...
  # export ANTHROPIC_API_KEY=sk-ant-...

  python examples/lark_run.py
  # Ctrl+C 退出

行为:
  1) 先探一遍凭据(如果 app 不可用直接报错退出)
  2) 起 WS 监听 im.message.receive_v1
  3) 收到消息 → 走 AgentLoop → reply 原文
  4) 所有 reply 调用会被记到 /tmp/lark_run.log(http / code / 错误)
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 提前 import 一下,触发 _HAS_LARK 探测
from openclaw.channels.lark import LarkChannel, _HAS_LARK  # noqa: E402
from openclaw.config.settings import LarkSettings  # noqa: E402

LOG_FILE = Path("/tmp/lark_run.log")


def _setup_logging() -> None:
    """日志同时写终端和 /tmp/lark_run.log。"""
    fmt = "%(asctime)s %(levelname)-5s %(name)s: %(message)s"
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8"),
    ]
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers, force=True)
    # httpx 也开 INFO
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _make_agent():
    """根据 AGENT_BACKEND 选 agent,默认 echo。"""
    backend = os.environ.get("AGENT_BACKEND", "echo").lower()

    if backend == "echo":
        class _Echo:
            async def handle(self, session_id, text, **kw):
                class R:
                    content = f"🤖 echo: {text}"
                    tool_calls = []
                    iterations = 1
                return R()

            async def new_session(self, sid=None):
                return sid or "echo-session"

            @property
            def tools(self): return None
            @property
            def memory(self): return None
        return _Echo()

    if backend in ("openai", "anthropic", "gemini", "ollama"):
        print(
            f"⚠️  AGENT_BACKEND={backend!r} 模式需要手动写 AgentLoop(provider=...)\n"
            "   (examples/lark_run.py 默认走 echo,够测试 channel 通路)\n"
            "   编辑本文件,把 _make_agent() 里 backend=='echo' 那段换成你 provider 即可。",
            file=sys.stderr,
        )
        sys.exit(2)

    print(f"❌ 未知 AGENT_BACKEND={backend!r}(echo/openai/anthropic/gemini/ollama)", file=sys.stderr)
    sys.exit(2)


async def main() -> None:
    _setup_logging()
    log = logging.getLogger("lark_run")

    app_id = os.environ.get("LARK_APP_ID", "")
    app_secret = os.environ.get("LARK_APP_SECRET", "")
    if not app_id or not app_secret:
        print("❌ 需要 LARK_APP_ID / LARK_APP_SECRET", file=sys.stderr)
        sys.exit(1)

    if not _HAS_LARK:
        print("❌ lark-oapi 未安装:pip install lark-oapi", file=sys.stderr)
        sys.exit(2)

    settings = LarkSettings(app_id=app_id, app_secret=app_secret, use_ws=True)
    agent = _make_agent()
    ch = LarkChannel(agent, settings)

    log.info("=" * 60)
    log.info("lark_run 启动 app_id=%s backend=%s", app_id, os.environ.get("AGENT_BACKEND", "echo"))
    log.info("事件订阅:打开飞书 app https://open.feishu.cn/app")
    log.info("  → 事件订阅 → 订阅方式:长连接接收 → 添加 im.message.receive_v1")
    log.info("  → 权限管理 → im:message(发消息) + im:message:readonly")
    log.info("  → 版本管理与发布 → 至少'自建应用仅自用'")
    log.info("Ctrl+C 退出")
    log.info("=" * 60)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass  # Windows / 没 tty

    try:
        await ch.start()
    except KeyboardInterrupt:
        pass
    finally:
        await ch.stop()
        log.info("退出")


if __name__ == "__main__":
    asyncio.run(main())
