"""ProviderRouter:多 provider 编排(Phase 5 增强)。

策略:
- fallback_only(默认):主失败 → 按 fallback 列表依次重试
- round_robin:        每次调用把 primary 推到队尾,均衡负载
- cost_aware:         按 provider 的 cost_per_1k 字段从低到高,失败后再切下一个
- priority:           按 provider.priority 数字升序(数字小优先)

step-粒度 fallback:
- 提供 acomplete_with_step() 包装,支持 per-step 策略(plan-execute 时)
- 单次失败时累计 attempts,超过 max_attempts 才升级到"换 provider"
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


class ProviderRouter(BaseLLMProvider):
    def __init__(
        self,
        primary: BaseLLMProvider,
        fallbacks: Iterable[BaseLLMProvider] = (),
        *,
        strategy: Strategy = "fallback_only",
        metas: Optional[dict[str, ProviderMeta]] = None,
    ) -> None:
        super().__init__(model=primary.model)
        self.strategy: Strategy = strategy
        self._metas: dict[str, ProviderMeta] = metas or {}
        all_providers = [primary, *fallbacks]
        for p in all_providers:
            if p.__class__.__name__ not in self._metas:
                self._metas[p.__class__.__name__ + ":" + getattr(p, "model", "?")] = ProviderMeta(
                    provider=p
                )
        # 建立 index
        self._providers: list[BaseLLMProvider] = all_providers
        self.primary = primary
        self.fallbacks = list(fallbacks)
        self._rr_index = 0
        self.stats = RouterStats()

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
        self._metas[f"{p.__class__.__name__}:{getattr(p, 'model', '?')}"] = nm
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
            name = f"{type(prov).__name__}:{getattr(prov, 'model', '?')}"
            t0 = time.time()
            try:
                logger.info("router_dispatch", provider=name, strategy=self.strategy)
                res = await prov.acomplete(
                    messages, tools=tools, temperature=temperature, max_tokens=max_tokens
                )
                self.stats.record(name, ok=True, ms=int((time.time() - t0) * 1000))
                return res
            except Exception as e:  # noqa: BLE001
                last_err = e
                self.stats.record(name, ok=False, ms=int((time.time() - t0) * 1000))
                logger.warning("router_provider_failed", provider=name, error=str(e))
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
    ) -> LLMResult:
        """在当前 router 排序下,每个 provider 内部再重试 max_attempts_per_step 次,
        再切下一个 provider。
        """
        last_err: Exception | None = None
        for prov in self._order():
            meta = self._meta_of(prov)
            name = f"{type(prov).__name__}:{getattr(prov, 'model', '?')}"
            attempts = max(1, max_attempts_per_step or meta.max_attempts)
            for k in range(attempts):
                t0 = time.time()
                try:
                    res = await prov.acomplete(
                        messages, tools=tools, temperature=temperature, max_tokens=max_tokens
                    )
                    self.stats.record(name, ok=True, ms=int((time.time() - t0) * 1000))
                    return res
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    self.stats.record(name, ok=False, ms=int((time.time() - t0) * 1000))
                    logger.warning(
                        "router_retry", provider=name, attempt=k + 1, max=attempts, error=str(e)
                    )
                    await asyncio.sleep(0.1 * (k + 1))
        raise ProviderError(f"all providers failed after retries: {last_err!r}")

    async def aclose(self) -> None:
        for p in self._providers:
            try:
                await p.aclose()
            except Exception:
                pass
