"""ProviderRouter:多 provider 编排(Phase 5 增强)。

策略:
- fallback_only(默认):主失败 → 按 fallback 列表依次重试
- round_robin:        每次调用把 primary 推到队尾,均衡负载
- cost_aware:         按 provider 的 cost_per_1k 字段从低到高,失败后再切下一个
- priority:           按 provider.priority 数字升序(数字小优先)

step-粒度 fallback:
- 提供 acomplete_with_step() 包装,支持 per-step 策略(plan-execute 时)
- 单次失败时累计 attempts,超过 max_attempts 才升级到"换 provider"

**RT-7 修复**:
- 加熔断器(CircuitBreaker):连续 N 次失败 → open 状态 → 短期跳过
- 用 ``tenacity`` 替代手写指数退避重试(可控、可观测)
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Optional

from openclaw.core.errors import ProviderError
from openclaw.core.logging import get_logger
from openclaw.llm.base import BaseLLMProvider, ChatMessage, LLMResult, ToolSpec

logger = get_logger(__name__)

Strategy = Literal["fallback_only", "round_robin", "cost_aware", "priority"]


# ---------------------------------------------------------------------------
# RT-7: Circuit Breaker
# ---------------------------------------------------------------------------

@dataclass
class _BreakerState:
    """单个 provider 的熔断状态。

    状态机:
      CLOSED (正常) → 连续 N 次失败 → OPEN(熔断)
      OPEN(熔断) → 冷却 K 秒 → HALF_OPEN(放一次探针)
      HALF_OPEN → 成功 → CLOSED;失败 → OPEN
    """
    fail_count: int = 0
    open_until: float = 0.0
    state: str = "closed"  # closed / open / half_open

    def is_open(self) -> bool:
        if self.state == "open":
            if time.time() >= self.open_until:
                self.state = "half_open"
                return False
            return True
        return False

    def record_success(self) -> None:
        self.fail_count = 0
        self.state = "closed"

    def record_failure(self, fail_threshold: int, cooldown: float) -> None:
        self.fail_count += 1
        if self.fail_count >= fail_threshold or self.state == "half_open":
            self.state = "open"
            self.open_until = time.time() + cooldown


class CircuitBreaker:
    """Provider 维度的熔断器。

    Args:
        fail_threshold: 连续失败次数,达此值熔断
        cooldown: 熔断持续秒数
    """

    def __init__(self, fail_threshold: int = 5, cooldown: float = 30.0) -> None:
        self.fail_threshold = fail_threshold
        self.cooldown = cooldown
        self._states: dict[str, _BreakerState] = {}

    def is_open(self, key: str) -> bool:
        st = self._states.setdefault(key, _BreakerState())
        return st.is_open()

    def record_success(self, key: str) -> None:
        st = self._states.setdefault(key, _BreakerState())
        st.record_success()

    def record_failure(self, key: str) -> None:
        st = self._states.setdefault(key, _BreakerState())
        st.record_failure(self.fail_threshold, self.cooldown)

    def state_of(self, key: str) -> str:
        return self._states.setdefault(key, _BreakerState()).state


@dataclass
class ProviderMeta:
    """Provider 的元信息,用于 cost_aware / priority 策略。"""
    provider: BaseLLMProvider
    cost_per_1k: float = 1.0       # 用于 cost_aware,单位任意(USD/1k token)
    priority: int = 100            # 用于 priority,数字越小越优先
    max_attempts: int = 2          # 单步失败时,本 provider 自身重试次数


@dataclass
class RouterStats:
    """累计统计,便于调试 + cost_aware 选优。"""
    calls: int = 0
    failures: int = 0
    total_ms: int = 0
    by_provider: dict[str, dict[str, int]] = field(default_factory=dict)

    def record(self, name: str, ok: bool, ms: int) -> None:
        self.calls += 1
        self.total_ms += ms
        if not ok:
            self.failures += 1
        st = self.by_provider.setdefault(name, {"ok": 0, "fail": 0, "ms": 0})
        st["ms"] += ms
        if ok:
            st["ok"] += 1
        else:
            st["fail"] += 1


def _prov_key(p: BaseLLMProvider) -> str:
    return f"{p.__class__.__name__}:{getattr(p, 'model', '?')}"


class ProviderRouter(BaseLLMProvider):
    def __init__(
        self,
        primary: BaseLLMProvider,
        fallbacks: Iterable[BaseLLMProvider] = (),
        *,
        strategy: Strategy = "fallback_only",
        metas: Optional[dict[str, ProviderMeta]] = None,
        breaker: Optional[CircuitBreaker] = None,
    ) -> None:
        super().__init__(model=primary.model)
        self.strategy: Strategy = strategy
        self._metas: dict[str, ProviderMeta] = metas or {}
        all_providers = [primary, *fallbacks]
        for p in all_providers:
            if p.__class__.__name__ not in self._metas:
                self._metas[_prov_key(p)] = ProviderMeta(provider=p)
        # 建立 index
        self._providers: list[BaseLLMProvider] = all_providers
        self.primary = primary
        self.fallbacks = list(fallbacks)
        self._rr_index = 0
        self.stats = RouterStats()
        # RT-7:熔断器(共享单例)
        self.breaker = breaker or CircuitBreaker()

    # ----- 排序 -----

    def _order(self) -> list[BaseLLMProvider]:
        ps = list(self._providers)
        if self.strategy == "fallback_only":
            return [self.primary, *self.fallbacks]
        if self.strategy == "round_robin":
            rotated = ps[self._rr_index:] + ps[:self._rr_index]
            self._rr_index = (self._rr_index + 1) % len(ps)
            return rotated
        if self.strategy == "cost_aware":
            return sorted(ps, key=lambda p: self._meta_of(p).cost_per_1k)
        if self.strategy == "priority":
            return sorted(ps, key=lambda p: self._meta_of(p).priority)
        return ps

    def _meta_of(self, p: BaseLLMProvider) -> ProviderMeta:
        # 通过 (class, model) 唯一定位
        for m in self._metas.values():
            if m.provider is p:
                return m
        # 兜底
        nm = ProviderMeta(provider=p)
        self._metas[_prov_key(p)] = nm
        return nm

    def set_meta(self, provider: BaseLLMProvider, **kw: Any) -> None:
        m = self._meta_of(provider)
        for k, v in kw.items():
            if hasattr(m, k):
                setattr(m, k, v)

    # ----- 单次补全(单 provider,无 fallback) -----

    async def acomplete(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[ToolSpec]] = None,
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> LLMResult:
        last_err: Exception | None = None
        for prov in self._order():
            key = _prov_key(prov)
            # RT-7:熔断中 → 跳过
            if self.breaker.is_open(key):
                logger.warning("router_provider_skipped_breaker_open", provider=key)
                continue
            t0 = time.time()
            try:
                logger.info("router_dispatch", provider=key, strategy=self.strategy)
                res = await prov.acomplete(
                    messages, tools=tools, temperature=temperature, max_tokens=max_tokens
                )
                self.stats.record(key, ok=True, ms=int((time.time() - t0) * 1000))
                self.breaker.record_success(key)
                return res
            except Exception as e:  # noqa: BLE001
                last_err = e
                self.stats.record(key, ok=False, ms=int((time.time() - t0) * 1000))
                self.breaker.record_failure(key)
                logger.warning("router_provider_failed", provider=key, error=str(e))
                await asyncio.sleep(0.1)
        raise ProviderError(f"all providers failed: {last_err!r}")

    # ----- step 粒度(per-step retries) -----

    async def acomplete_with_retry(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[ToolSpec]] = None,
        *,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
        max_attempts_per_step: int = 1,
        # M25 修复:retry budget — 总耗时上限,防雪崩(下游慢/全失败时不能无限重试)
        # 默认 30s 与 review 建议对齐;单元测试可传 0 禁用超时(但仍受 attempts 约束)
        max_total_seconds: float = 30.0,
    ) -> LLMResult:
        """在当前 router 排序下,每个 provider 内部再重试 max_attempts_per_step 次,
        再切下一个 provider。

        **RT-7 修复**:用 tenacity 实现指数退避重试;成功 → breaker reset,失败 → breaker 累计。
        **Phase 29 / M25 修复**:加 ``stop_after_delay(max_total_seconds)`` 总耗时上限
        + 退避 ``max=2.0``(原 1.0 略紧),防雪崩。
        """
        from tenacity import (
            AsyncRetrying,
            retry_if_exception_type,
            stop_after_attempt,
            stop_after_delay,
            stop_any,
            wait_exponential,
        )

        last_err: Exception | None = None
        for prov in self._order():
            key = _prov_key(prov)
            if self.breaker.is_open(key):
                logger.warning("router_retry_skipped_breaker_open", provider=key)
                continue
            meta = self._meta_of(prov)
            attempts = max(1, max_attempts_per_step or meta.max_attempts)
            t0 = time.time()
            try:
                # RT-7:tenacity 指数退避(0.1s, 0.2s, 0.4s…)
                # M25:加 stop_after_delay 防雪崩;wait_exponential max=2 略宽于原 1.0
                # stop_after_delay(0) 等效"不限时间",只在 max_total_seconds > 0 时生效
                # 用 stop_any 组合 attempt + delay(任一先到就停)
                if max_total_seconds > 0:
                    stop = stop_any(
                        stop_after_attempt(attempts),
                        stop_after_delay(max_total_seconds),
                    )
                else:
                    stop = stop_after_attempt(attempts)
                async for attempt in AsyncRetrying(
                    stop=stop,
                    wait=wait_exponential(multiplier=0.1, max=2.0),
                    retry=retry_if_exception_type(Exception),
                    reraise=True,
                ):
                    with attempt:
                        res = await prov.acomplete(
                            messages, tools=tools, temperature=temperature, max_tokens=max_tokens
                        )
                        self.stats.record(key, ok=True, ms=int((time.time() - t0) * 1000))
                        self.breaker.record_success(key)
                        return res
            except Exception as e:  # noqa: BLE001
                last_err = e
                self.stats.record(key, ok=False, ms=int((time.time() - t0) * 1000))
                self.breaker.record_failure(key)
                logger.warning(
                    "router_retry_exhausted", provider=key, attempts=attempts, error=str(e)
                )
        raise ProviderError(f"all providers failed after retries: {last_err!r}")

    async def aclose(self) -> None:
        for p in self._providers:
            try:
                await p.aclose()
            except Exception:
                pass
