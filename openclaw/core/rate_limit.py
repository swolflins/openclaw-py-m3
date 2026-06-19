"""RateLimiter:轻量级 token-bucket 限流器(Phase 6)。

设计:
- 进程内 + 可选持久化(写到 sqlite,跨重启保留)
- 支持多维 namespace:user_id / channel / 任意标签
- 同步 / 异步 API

用法:
    rl = RateLimiter(rate=0.5, burst=3)        # 每 2s 1 个,突发 3
    if not rl.allow("user:alice"):
        return "你说话太快啦,等一下再来"
    if not await rl.aallow("user:alice"):
        ...
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class RateLimiter:
    """单进程 token-bucket 限流器。

    rate:    每秒补充的 token 数
    burst:   桶容量(单次最多能连续通过多少请求)
    persist_path: 非 None 时,bucket 状态写 sqlite,跨重启保留
    """

    def __init__(
        self,
        rate: float = 1.0,
        burst: int = 5,
        persist_path: Optional[Path] = None,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        if burst <= 0:
            raise ValueError("burst must be > 0")
        self.rate = float(rate)
        self.burst = float(burst)
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()
        self._persist_path = persist_path
        self._db: Optional[sqlite3.Connection] = None
        if persist_path is not None:
            persist_path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(persist_path), check_same_thread=False)
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS rl_bucket (
                       key TEXT PRIMARY KEY,
                       tokens REAL NOT NULL,
                       last_refill REAL NOT NULL
                   )"""
            )
            self._db.commit()
            self._load()

    # ---------------- 同步 ----------------

    def allow(self, key: str, cost: float = 1.0) -> bool:
        """同步版本;返回是否放行。"""
        with self._lock:
            return self._consume(key, cost)

    def retry_after(self, key: str, cost: float = 1.0) -> float:
        """还需多少秒才能放行(>=0)。"""
        with self._lock:
            b = self._refill(key)
            if b.tokens >= cost:
                return 0.0
            return (cost - b.tokens) / self.rate

    # ---------------- 异步 ----------------

    async def aallow(self, key: str, cost: float = 1.0) -> bool:
        return self.allow(key, cost)

    async def aretry_after(self, key: str, cost: float = 1.0) -> float:
        return self.retry_after(key, cost)

    # ---------------- 内部 ----------------

    def _refill(self, key: str) -> _Bucket:
        now = time.time()
        b = self._buckets.get(key)
        if b is None:
            b = _Bucket(tokens=self.burst, last_refill=now)
            self._buckets[key] = b
        else:
            elapsed = now - b.last_refill
            if elapsed > 0:
                b.tokens = min(self.burst, b.tokens + elapsed * self.rate)
                b.last_refill = now
        return b

    def _consume(self, key: str, cost: float) -> bool:
        b = self._refill(key)
        if b.tokens >= cost:
            b.tokens -= cost
            self._persist(key, b)
            return True
        self._persist(key, b)
        return False

    # ---------------- 持久化 ----------------

    def _load(self) -> None:
        assert self._db is not None
        for row in self._db.execute("SELECT key, tokens, last_refill FROM rl_bucket"):
            self._buckets[row[0]] = _Bucket(tokens=float(row[1]), last_refill=float(row[2]))

    def _persist(self, key: str, b: _Bucket) -> None:
        if self._db is None:
            return
        self._db.execute(
            "INSERT OR REPLACE INTO rl_bucket(key, tokens, last_refill) VALUES (?, ?, ?)",
            (key, b.tokens, b.last_refill),
        )
        self._db.commit()

    def reset(self, key: Optional[str] = None) -> None:
        with self._lock:
            if key is None:
                self._buckets.clear()
                if self._db is not None:
                    self._db.execute("DELETE FROM rl_bucket")
                    self._db.commit()
            else:
                self._buckets.pop(key, None)
                if self._db is not None:
                    self._db.execute("DELETE FROM rl_bucket WHERE key = ?", (key,))
                    self._db.commit()

    def snapshot(self) -> dict[str, dict[str, float]]:
        """调试用:导出所有 bucket 当前状态。"""
        with self._lock:
            return {
                k: {"tokens": v.tokens, "last_refill": v.last_refill}
                for k, v in self._buckets.items()
            }

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None
