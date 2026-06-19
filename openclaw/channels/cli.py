"""CLI 渠道:REPL 风格的本地交互,用于调试和 Hello Agent 烟测。"""
from __future__ import annotations

import asyncio

from openclaw.agent.loop import AgentLoop
from openclaw.channels.base import BaseChannel, IncomingMessage


class CLIChannel(BaseChannel):
    """终端 REPL。

    - 每行输入是一条用户消息
    - 输入 :exit / :quit 退出
    """

    name = "cli"

    def __init__(self, agent_loop: AgentLoop) -> None:
        super().__init__(agent_loop)
        self._stopped = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        print("=== OpenClaw CLI ===")
        print("输入你的问题,回车发送。:exit 退出。")
        self._task = asyncio.create_task(self._run())
        await self._stopped.wait()

    async def stop(self) -> None:
        self._stopped.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=2)
            except asyncio.TimeoutError:
                self._task.cancel()

    async def send(self, session_id: str, text: str) -> None:
        print(f"\n[bot -> {session_id}]\n{text}\n> ", end="", flush=True)

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            while not self._stopped.is_set():
                line = await loop.run_in_executor(None, lambda: input("> "))
                line = line.strip()
                if not line:
                    continue
                if line in (":exit", ":quit"):
                    break
                # 走统一管道
                await self.dispatch(IncomingMessage(
                    channel=self.name,
                    session_id="cli",
                    user_id="local",
                    text=line,
                    metadata={"is_dm": True},
                ))
        except (EOFError, KeyboardInterrupt):
            pass
        finally:
            self._stopped.set()
