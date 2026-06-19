"""Signal 渠道(Phase 7)。

走 signal-cli 的 REST RPC:
- 入站:长轮询 /v1/receive/{account}
- 出站:/v2/send

依赖:本机或 docker 跑 signal-cli,开 JSON-RPC 模式。
环境变量:
    SIGNAL_CLI_URL      e.g. http://localhost:8080
    SIGNAL_ACCOUNT      +8613800138000 或 uuid
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

import httpx

from openclaw.agent.loop import AgentLoop
from openclaw.channels.base import BaseChannel, IncomingMessage
from openclaw.core.logging import get_logger

logger = get_logger(__name__)


class SignalChannel(BaseChannel):
    name = "signal"

    def __init__(
        self,
        base_url: str,
        account: str,
        agent_loop: AgentLoop,
    ) -> None:
        super().__init__(agent_loop)
        self.base_url = base_url.rstrip("/")
        self.account = account
        self._client: Optional[httpx.AsyncClient] = None
        self._task: Optional[asyncio.Task] = None

    @property
    def available(self) -> bool:
        return bool(self.base_url) and bool(self.account)

    async def _get_client(self) -> httpx.AsyncClient:
        current_loop_id = id(asyncio.get_running_loop())
        if self._client is None or getattr(self, "_loop_id", None) != current_loop_id or self._client.is_closed:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=35)
            self._loop_id = current_loop_id
        return self._client

    async def start(self) -> None:
        if not self.available:
            raise RuntimeError("Signal 未配置 (SIGNAL_CLI_URL / SIGNAL_ACCOUNT)")
        self._task = asyncio.create_task(self._recv_loop(), name="signal-recv")
        await self._stopped.wait()

    async def stop(self) -> None:
        self._stopped.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    async def send(self, session_id: str, text: str) -> None:
        # session_id: signal:<recipient>
        recipient = session_id.split(":", 1)[1] if ":" in session_id else session_id
        client = await self._get_client()
        try:
            await client.post(
                "/v2/send",
                json={"message": text, "number": self.account, "recipients": [recipient]},
            )
        except Exception:
            logger.exception("signal send failed")

    async def _recv_loop(self) -> None:
        backoff = 1.0
        client = await self._get_client()
        while not self._stopped.is_set():
            try:
                r = await client.get(f"/v1/receive/{self.account}", timeout=30)
                if r.status_code == 200:
                    arr = r.json()
                    for raw in arr or []:
                        try:
                            await self._handle_envelope(raw)
                        except Exception:
                            logger.exception("handle signal envelope failed")
                    backoff = 1.0
                else:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("signal recv error, backoff=%ss", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _handle_envelope(self, env: dict[str, Any]) -> None:
        dm = env.get("dataMessage") or {}
        if not dm:
            return
        text = (dm.get("message") or "").strip()
        if not text:
            return
        envelope = env.get("envelope") or {}
        source = (
            envelope.get("source")
            or env.get("source")
            or env.get("sourceNumber")
            or envelope.get("sourceNumber")
        )
        if not source:
            return
        # 群(在 signal 协议里通过 groupInfo 体现)暂不支持
        await self.dispatch(IncomingMessage(
            channel=self.name,
            session_id=f"signal:{source}",
            user_id=str(source),
            text=text,
            raw=env,
            metadata={
                "is_dm": True,
                "mentioned": True,
                "timestamp": env.get("timestamp"),
            },
        ))


def from_env(agent_loop: AgentLoop) -> Optional[SignalChannel]:
    url = os.environ.get("SIGNAL_CLI_URL")
    acc = os.environ.get("SIGNAL_ACCOUNT")
    if not url or not acc:
        return None
    return SignalChannel(base_url=url, account=acc, agent_loop=agent_loop)
