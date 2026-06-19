"""Phase 0+1 示例:演示 logging + 事件总线,不依赖任何 LLM/工具。

运行:`python examples/hello_agent.py`
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openclaw.bus import EventBus, get_bus
from openclaw.core.logging import bind_context, get_logger, setup_logging


async def main() -> None:
    setup_logging("INFO", json=False)
    log = get_logger("hello")

    bus: EventBus = get_bus()

    async def on_msg(payload: dict) -> None:
        log.info("msg_received", text=payload.get("text"))

    bus.subscribe("hello.msg", on_msg)
    log.info("hello_start")
    await bus.publish("hello.msg", {"text": "world"})
    await bus.publish("hello.msg", {"text": "openclaw"})
    log.info("hello_end", history=len(bus.history("hello.msg")))

    # 测试 bind_context(trace_id) — structlog 风格,直接调用
    bind_context(trace_id="abc123")
    log.info("within_trace", value=42)
    import structlog
    structlog.contextvars.clear_contextvars()


if __name__ == "__main__":
    asyncio.run(main())
