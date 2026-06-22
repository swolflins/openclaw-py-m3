"""RateLimiter:轻量级 token-bucket 限流器(Phase 6)。

设计:
- 进程内 + 可选持久化(写到 sqlite,跨重启保留)
- 支持多维 namespace:user_id / channel / 任意标签
- 同步 / 异步 API

**Phase 29 / M27 修复**:支持 Redis 后端,多实例部署时共享 token bucket。
- 工厂方法 ``from_redis(url=..., rate=..., burst=...)`` 返回 ``RedisRateLimiter``
- 内部用 Lua 脚本保证"读 + 改 + 写"原子(防并发下两 worker 同时扣 token)
- 失败安全:Redis 不可达 → 自动降级到本地内存版(日志 warning,但不阻断请求)

用法:
    rl = RateLimiter(rate=0.5, burst=3)        # 内存版(单进程)
    rl = RedisRateLimiter(url="redis://...", rate=0.5, burst=3)  # 共享版
    if not rl.allow("user:alice"):
        return "你说话太快啦,等一下再来"
    if not await rl.aallow("user:alice"):
        ...
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


# ---------------- Redis token-bucket Lua 脚本 ----------------
# 输入: KEYS[1] = bucket key, ARGV[1] = cost, ARGV[2] = rate (tokens/s),
#       ARGV[3] = burst, ARGV[4] = now (秒,浮点), ARGV[5] = ttl 秒
# 输出: { allowed(0/1), remaining_tokens, retry_after_seconds }
# 行为:经典 token bucket — 按 (now - last_refill) * rate 补充,封顶 burst
_REDIS_TB_LUA = """
local key    = KEYS[1]
local cost   = tonumber(ARGV[1])
local rate   = tonumber(ARGV[2])
local burst  = tonumber(ARGV[3])
local now    = tonumber(ARGV[4])
local ttl    = tonumber(ARGV[5])

local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens, last_refill
if data[1] == false then
    tokens = burst
    last_refill = now
else
    tokens = tonumber(data[1])
    last_refill = tonumber(data[2])
end

-- 补充
local elapsed = math.max(0, now - last_refill)
tokens = math.min(burst, tokens + elapsed * rate)

local allowed = 0
local retry_after = 0
if tokens >= cost then
    tokens = tokens - cost
    allowed = 1
else
    local needed = cost - tokens
    retry_after = needed / rate
end

redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, ttl)
return { allowed, tokens, retry_after }
"""


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
        max_keys: int = 100_000,
    ) -> None:
        """max_keys: SEC-12 防御 — 防恶意 / 异常 user_id 填爆内存。超过上限时拒绝新 key 创建。"""
        if rate <= 0:
            raise ValueError("rate must be > 0")
        if burst <= 0:
            raise ValueError("burst must be > 0")
        self.rate = float(rate)
        self.burst = float(burst)
        self.max_keys = int(max_keys)
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

    def try_consume(self, key: str, cost: float = 1.0) -> tuple[bool, float, float]:
        """Phase 29 / L9 修复:同步原子的"扣 token + 返 remaining/retry_after"。

        返回 (allowed, remaining_tokens, retry_after_seconds)。
        与 ``RedisRateLimiter`` 的 Lua 脚本返回结构对齐,
        让 ``RateLimitMiddleware`` 可以同时塞 L9 的 X-RateLimit-* headers。
        """
        with self._lock:
            b = self._refill(key)
            if b.tokens >= cost:
                b.tokens -= cost
                self._persist(key, b)
                return True, b.tokens, 0.0
            self._persist(key, b)
            return False, b.tokens, (cost - b.tokens) / self.rate

    # ---------------- 异步 ----------------

    async def aallow(self, key: str, cost: float = 1.0) -> bool:
        # M14 修复:用 run_in_executor 避免阻塞事件循环
        # 旧逻辑:直接调同步 allow(内部 threading.Lock + sqlite.commit),
        # 高并发或慢磁盘时阻塞整个 event loop
        import asyncio
        return await asyncio.to_thread(self.allow, key, cost)

    async def aretry_after(self, key: str, cost: float = 1.0) -> float:
        import asyncio
        return await asyncio.to_thread(self.retry_after, key, cost)

    # ---------------- 内部 ----------------

    def _refill(self, key: str) -> _Bucket:
        now = time.time()
        b = self._buckets.get(key)
        if b is None:
            # SEC-12:达到 max_keys 上限,先 LRU 淘汰最久未活动的 key
            if len(self._buckets) >= self.max_keys and key not in self._buckets:
                self._evict_lru()
            # H5 修复:仍满 → fail-closed(返回空桶,不允许通过)
            # 旧逻辑返回 tokens=burst 的临时桶 → _consume 判定 tokens>=cost 放行 = fail-open
            # 新逻辑返回 tokens=0 的桶 → _consume 判定 tokens<cost 拒绝 = fail-closed
            if len(self._buckets) >= self.max_keys and key not in self._buckets:
                return _Bucket(tokens=0, last_refill=now)
            b = _Bucket(tokens=self.burst, last_refill=now)
            self._buckets[key] = b
        else:
            elapsed = now - b.last_refill
            if elapsed > 0:
                b.tokens = min(self.burst, b.tokens + elapsed * self.rate)
                b.last_refill = now
        return b

    def _evict_lru(self) -> None:
        """SEC-12:LRU 淘汰:踢掉 last_refill 最早的 bucket(10% 留缓冲)。"""
        if not self._buckets:
            return
        # 淘汰 10%
        evict_count = max(1, self.max_keys // 10)
        sorted_keys = sorted(
            self._buckets.items(), key=lambda kv: kv[1].last_refill
        )[:evict_count]
        for k, _ in sorted_keys:
            self._buckets.pop(k, None)
            if self._db is not None:
                self._db.execute("DELETE FROM rl_bucket WHERE key = ?", (k,))
        if self._db is not None:
            self._db.commit()

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


# ---------------- Redis 版 (Phase 29 / M27) ----------------


class RedisRateLimiter:
    """跨进程共享的 token-bucket 限流器,后端 Redis。

    适用场景:
    - 容器化多副本部署
    - 期望"同一用户 IP 在所有副本共享一个桶"

    设计:
    - 同步 / 异步 API,接口与 ``RateLimiter`` 对齐(``allow`` / ``aallow`` / ``retry_after``)
    - 用 Lua 脚本保证"读 + 改 + 写"原子,防并发抢扣
    - **失败安全**:Redis 不可达时,默认 ``fallback_to_memory=True`` → 临时降级
      到本地内存版(日志 warning),不阻断请求
    - 提供 ``max_keys`` 上限保护(超出 → 调 fail-closed,refill 到 0)
    - 状态用 ``HMSET`` + ``EXPIRE`` 持久化在 Redis,key 命名 ``rl:{key}``,
      ttl 设为 ``burst / rate * 10``(让"长尾 bucket"自然过期)

    依赖:redis>=5.0
    """

    def __init__(
        self,
        client,  # redis.Redis 或 redis.asyncio.Redis
        rate: float = 1.0,
        burst: int = 5,
        *,
        key_prefix: str = "rl",
        max_keys: int = 100_000,
        fallback_to_memory: bool = True,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0")
        if burst <= 0:
            raise ValueError("burst must be > 0")
        self._client = client
        self.rate = float(rate)
        self.burst = float(burst)
        self.max_keys = int(max_keys)
        self._key_prefix = key_prefix
        # bucket key TTL:让不活跃的 key 自然过期
        self._ttl = max(60, int((self.burst / max(self.rate, 0.001)) * 10))
        self._fallback_to_memory = fallback_to_memory
        self._mem: Optional[RateLimiter] = (
            RateLimiter(rate=rate, burst=burst, max_keys=max_keys)
            if fallback_to_memory else None
        )
        # 注册 Lua 脚本(同步 client 用 Script,异步 client 用 register_script)
        self._script = self._client.register_script(_REDIS_TB_LUA)
        self._async = hasattr(client, "await")
        from openclaw.core.logging import get_logger
        self._logger = get_logger(__name__)

    # ---- 同步 ----

    def allow(self, key: str, cost: float = 1.0) -> bool:
        return self._run_sync(key, cost)[0]

    def retry_after(self, key: str, cost: float = 1.0) -> float:
        return self._run_sync(key, cost)[2]

    def try_consume(self, key: str, cost: float = 1.0) -> tuple[bool, float, float]:
        """Phase 29 / L9:同步原子"扣 + 返三元组"。

        返回 (allowed, remaining_tokens, retry_after_seconds)。
        """
        return self._run_sync(key, cost)

    def _run_sync(self, key: str, cost: float) -> tuple[bool, float, float]:
        """return (allowed, remaining, retry_after_seconds)"""
        full_key = f"{self._key_prefix}:{key}"
        try:
            res = self._script(
                keys=[full_key],
                args=[cost, self.rate, self.burst, time.time(), self._ttl],
            )
            allowed, remaining, retry_after = (
                bool(int(res[0])), float(res[1]), float(res[2]),
            )
            return allowed, remaining, retry_after
        except Exception as e:  # noqa: BLE001
            return self._on_redis_fail(key, cost, e)

    # ---- 异步 ----

    async def aallow(self, key: str, cost: float = 1.0) -> bool:
        return (await self._run_async(key, cost))[0]

    async def aretry_after(self, key: str, cost: float = 1.0) -> float:
        return (await self._run_async(key, cost))[2]

    async def atry_consume(self, key: str, cost: float = 1.0) -> tuple[bool, float, float]:
        """Phase 29 / L9:异步原子"扣 + 返三元组"。"""
        return await self._run_async(key, cost)

    async def _run_async(self, key: str, cost: float) -> tuple[bool, float, float]:
        full_key = f"{self._key_prefix}:{key}"
        try:
            res = await self._script(
                keys=[full_key],
                args=[cost, self.rate, self.burst, time.time(), self._ttl],
            )
            allowed, remaining, retry_after = (
                bool(int(res[0])), float(res[1]), float(res[2]),
            )
            return allowed, remaining, retry_after
        except Exception as e:  # noqa: BLE001
            return self._on_redis_fail(key, cost, e)

    # ---- 失败兜底 ----

    def _on_redis_fail(self, key: str, cost: float, exc: Exception) -> tuple[bool, float, float]:
        """Redis 不可达时:降级到本地内存版(若开启),或 fail-closed。"""
        self._logger.warning(
            "rate_limit_redis_unavailable",
            key=key,
            error_type=type(exc).__name__,
            error_msg=str(exc)[:200],
        )
        if self._mem is None:
            # fail-closed:拒绝请求(扣 0 token)
            return False, 0.0, 1.0
        allowed = self._mem.allow(key, cost=cost)
        retry = self._mem.retry_after(key, cost=cost) if not allowed else 0.0
        return allowed, 0.0, retry

    # ---- 调试 / 清理 ----

    def snapshot(self) -> dict[str, dict[str, float]]:
        """调试用:本地 fallback bucket 状态(Redis 端需 SCAN 单独看)。"""
        if self._mem is None:
            return {}
        return self._mem.snapshot()

    def reset(self, key: Optional[str] = None) -> None:
        """清本地 fallback;Redis 端需 SCAN+DEL(此处不实现,运维工具自己来)。"""
        if self._mem is not None:
            self._mem.reset(key=key)


# ---------------- 工厂函数 (Phase 29 / M27) ----------------


def from_redis(
    url: Optional[str] = None,
    *,
    rate: float = 1.0,
    burst: int = 5,
    client=None,
    **kwargs: Union[str, int, bool],
) -> RedisRateLimiter:
    """工厂函数:从 URL 创建 ``RedisRateLimiter``。

    优先用传入的 ``client``(测试场景);否则从 ``url`` / ``OPENCLAW_REDIS_URL``
    / 默认 ``redis://localhost:6379/0`` 创建。

    同步 vs 异步自动推断:传入 ``redis.asyncio.Redis`` 实例即可走异步 API。
    """
    if client is None:
        try:
            import redis
        except ImportError as e:
            raise ImportError(
                "redis 包未安装,Redis 限流需要 `pip install 'openclaw-py[redis]'`"
            ) from e
        url = url or os.environ.get("OPENCLAW_REDIS_URL") or "redis://localhost:6379/0"
        client = redis.Redis.from_url(url, decode_responses=True)
    return RedisRateLimiter(client=client, rate=rate, burst=burst, **kwargs)
