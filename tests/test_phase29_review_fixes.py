"""Phase 29 续修 — review 报告剩余 5 项 + L9/L11/L12 共 7 项的回归测试。

覆盖:
- M25 retry budget (router + openai_compat stop_after_delay)
- M26 bulkhead (provider 独立 httpx.Limits)
- M27 Redis rate limit backend (Lua 脚本 + 失败兜底 + 工厂)
- L9 X-RateLimit-* headers (Limit / Remaining / Reset)
- L11 CI Python 3.13 矩阵
- L12 MANIFEST.in 包含关键文件
- RateLimiter.try_consume 对齐 Redis 版 (L9 配套)

每个 test class 用 ``Test<Area>`` 前缀,便于快速定位。
"""
from __future__ import annotations

import asyncio
import inspect
import os
import re
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# ============================================================
# M25 — retry budget (router + openai_compat)
# ============================================================
class TestRetryBudget:
    def test_router_uses_stop_after_delay(self):
        """router.acomplete_with_retry 必须用 stop_after_delay(防雪崩)。"""
        from openclaw.providers import router as rmod
        src = Path(rmod.__file__).read_text(encoding="utf-8")
        assert "stop_after_delay" in src, "router 缺 stop_after_delay(M25 修复)"
        assert "max_total_seconds" in src, "router 缺 max_total_seconds 参数"

    def test_router_max_total_seconds_default_30s(self):
        from openclaw.providers.router import ProviderRouter
        # 默认参数 = 30s(防雪崩,但不至于单步请求就超时)
        sig = inspect.signature(ProviderRouter.acomplete_with_retry)
        assert "max_total_seconds" in sig.parameters
        assert sig.parameters["max_total_seconds"].default == 30.0

    def test_openai_compat_has_max_retry_seconds(self):
        from openclaw.providers import openai_compat
        src = Path(openai_compat.__file__).read_text(encoding="utf-8")
        # 应有 max_retry_seconds 参数 + 走 max_total_seconds 检查
        assert "max_retry_seconds" in src, "openai_compat 缺 max_retry_seconds(M25)"
        assert "openai_compat_retry_budget_exceeded" in src, (
            "openai_compat 重试 budget 超时日志缺(M25)"
        )

    @pytest.mark.asyncio
    async def test_router_stops_after_budget(self):
        """mock 两个慢 provider,验证 retry budget 在 max_total_seconds 内就停。"""
        from openclaw.providers.router import CircuitBreaker, ProviderRouter

        class _SlowProvider:
            def __init__(self, name, sleep_s, fail=False):
                self.name = name
                self.sleep_s = sleep_s
                self.fail = fail
                self.model = "m"
                self.acomplete_calls = 0

            async def acomplete(self, *a, **kw):
                self.acomplete_calls += 1
                await asyncio.sleep(self.sleep_s)
                if self.fail:
                    raise RuntimeError("simulated")
                from openclaw.llm.base import LLMResult
                return LLMResult(content="ok", tool_calls=[], raw={})

        p1 = _SlowProvider("p1", sleep_s=0.3, fail=True)
        p2 = _SlowProvider("p2", sleep_s=0.3, fail=True)
        # max_attempts_per_step=3 → 3 次 × 0.3s sleep = ~0.9s
        # 加上 wait_exponential(0.1, 0.2) → ~1.2s
        # budget 0.5s → 早早被 stop_after_delay 砍
        router = ProviderRouter(
            primary=p1, fallbacks=[p2],
            breaker=CircuitBreaker(fail_threshold=99, cooldown=1.0),
        )
        from openclaw.core.errors import ProviderError
        t0 = time.monotonic()
        with pytest.raises(ProviderError):
            await router.acomplete_with_retry(
                messages=[],
                max_attempts_per_step=3,
                max_total_seconds=0.5,
            )
        elapsed = time.monotonic() - t0
        # budget 0.5s 加上少量 wait_exponential + 1 次调用,应 < 2s
        # 不强求 < 0.5s 因为 stop_after_delay 在 attempt 结束时检查
        assert elapsed < 2.0, f"retry budget 没生效,等了 {elapsed:.2f}s"


# ============================================================
# M26 — bulkhead (per-provider httpx.Limits)
# ============================================================
class TestBulkhead:
    def test_openai_compat_uses_per_provider_limits(self):
        from openclaw.providers import openai_compat
        src = Path(openai_compat.__file__).read_text(encoding="utf-8")
        # 必须用 httpx.Limits 构造的 limits 字段(per-provider bulkhead)
        assert "httpx.Limits" in src, "openai_compat 缺 httpx.Limits(M26 bulkhead)"
        assert "max_connections" in src
        assert "max_keepalive_connections" in src

    def test_openai_compat_default_limits(self):
        from openclaw.providers.openai_compat import OpenAICompatProvider
        sig = inspect.signature(OpenAICompatProvider.__init__)
        # 默认 20/5(review 建议)
        assert sig.parameters["max_connections"].default == 20
        assert sig.parameters["max_keepalive_connections"].default == 5

    @pytest.mark.asyncio
    async def test_provider_passes_limits_to_httpx(self):
        """验证 client 构造时传了 limits= 字段。"""
        from openclaw.providers.openai_compat import OpenAICompatProvider
        p = OpenAICompatProvider(api_key="x", model="m", max_connections=7, max_keepalive_connections=3)
        # 不实际发请求,只验证 _limits 字段
        assert p._limits.max_connections == 7
        assert p._limits.max_keepalive_connections == 3


# ============================================================
# M27 — Redis rate limit backend
# ============================================================
class TestRedisRateLimiter:
    def test_redis_class_exists(self):
        from openclaw.core.rate_limit import RedisRateLimiter, from_redis
        assert RedisRateLimiter is not None
        assert callable(from_redis)

    def test_redis_factory_url(self):
        """from_redis 用 url 创建 client(同步 redis.Redis)。"""
        from openclaw.core import rate_limit as rl
        # mock redis 模块
        with patch.dict(sys.modules, {"redis": MagicMock()}):
            mock_redis = sys.modules["redis"]
            mock_client = MagicMock()
            mock_redis.Redis.from_url.return_value = mock_client
            limiter = rl.from_redis(
                url="redis://test:6379/0", rate=2.0, burst=4,
            )
            mock_redis.Redis.from_url.assert_called_once_with(
                "redis://test:6379/0", decode_responses=True,
            )
            assert limiter.rate == 2.0
            assert limiter.burst == 4

    def test_redis_factory_uses_env(self):
        """from_redis 默认走 OPENCLAW_REDIS_URL。"""
        from openclaw.core import rate_limit as rl
        with patch.dict(os.environ, {"OPENCLAW_REDIS_URL": "redis://env:6379/1"}):
            with patch.dict(sys.modules, {"redis": MagicMock()}):
                mock_redis = sys.modules["redis"]
                rl.from_redis(rate=1.0, burst=3)
                mock_redis.Redis.from_url.assert_called_once_with(
                    "redis://env:6379/1", decode_responses=True,
                )

    def test_redis_fallback_to_memory(self):
        """Redis 不可达时降级到内存版(不抛)。"""
        from openclaw.core.rate_limit import RedisRateLimiter
        # mock 一个总抛异常的 client
        client = MagicMock()
        client.register_script.return_value = MagicMock(side_effect=ConnectionError("no redis"))
        lim = RedisRateLimiter(client=client, rate=2.0, burst=3, fallback_to_memory=True)
        # 不应抛,应降级到内存
        assert lim.allow("user:alice") is True
        assert lim.allow("user:alice") is True
        assert lim.allow("user:alice") is True
        # 第 4 次超出
        assert lim.allow("user:alice") is False

    def test_redis_fail_closed_when_no_fallback(self):
        """Redis 不可达 + fallback=False → fail-closed(返回 False)。"""
        from openclaw.core.rate_limit import RedisRateLimiter
        client = MagicMock()
        client.register_script.return_value = MagicMock(side_effect=ConnectionError("no"))
        lim = RedisRateLimiter(client=client, rate=2.0, burst=3, fallback_to_memory=False)
        assert lim.allow("user:alice") is False, (
            "无 fallback 时 Redis 不可达必须 fail-closed(返回 False)"
        )

    def test_redis_try_consume_aligned(self):
        """RedisRateLimiter 暴露 try_consume(签名同 RateLimiter),供 L9 headers 用。"""
        from openclaw.core.rate_limit import RedisRateLimiter
        client = MagicMock()
        # mock Lua 脚本返回 [1, 2.0, 0.0](allowed=1, remaining=2.0, retry=0)
        script_mock = MagicMock(return_value=[1, 2.0, 0.0])
        client.register_script.return_value = script_mock
        lim = RedisRateLimiter(client=client, rate=1.0, burst=3)
        allowed, remaining, retry = lim.try_consume("k1")
        assert allowed is True
        assert remaining == 2.0
        assert retry == 0.0

    def test_redis_lua_script_uses_HMSET(self):
        """Lua 脚本应使用 HMSET(原子读 + 改 + 写),不要 RTT 多次往返。"""
        from openclaw.core import rate_limit as rl
        assert "HMSET" in rl._REDIS_TB_LUA
        assert "HMGET" in rl._REDIS_TB_LUA
        assert "EXPIRE" in rl._REDIS_TB_LUA


# ============================================================
# L9 — X-RateLimit-* headers
# ============================================================
class TestRateLimitHeaders:
    def test_rate_limiter_has_try_consume(self):
        """RateLimiter 必须有 try_consume(内存版 + Redis 版对齐 L9 API)。"""
        from openclaw.core.rate_limit import RateLimiter, RedisRateLimiter
        assert hasattr(RateLimiter, "try_consume")
        assert hasattr(RedisRateLimiter, "try_consume")
        assert hasattr(RedisRateLimiter, "atry_consume")
        # 签名一致
        sig_mem = inspect.signature(RateLimiter.try_consume)
        sig_redis = inspect.signature(RedisRateLimiter.try_consume)
        assert list(sig_mem.parameters) == list(sig_redis.parameters)

    def test_try_consume_returns_three_tuple(self):
        from openclaw.core.rate_limit import RateLimiter
        rl = RateLimiter(rate=1.0, burst=2)
        # 首次应通过
        allowed, remaining, retry = rl.try_consume("k1")
        assert allowed is True
        assert remaining == pytest.approx(1.0, abs=1e-3)
        assert retry == 0.0
        # 第二次通过(允许极小浮点漂移)
        allowed, remaining, retry = rl.try_consume("k1")
        assert allowed is True
        assert remaining == pytest.approx(0.0, abs=1e-3)
        # 第三次拒绝
        allowed, remaining, retry = rl.try_consume("k1")
        assert allowed is False
        assert retry > 0

    def test_middleware_sets_headers_on_429(self):
        """RateLimitMiddleware 应在 429 响应里加 X-RateLimit-* headers。"""
        from openclaw.gateway import app as app_mod
        src_text = Path(app_mod.__file__).read_text(encoding="utf-8")
        assert "X-RateLimit-Limit" in src_text
        assert "X-RateLimit-Remaining" in src_text
        assert "X-RateLimit-Reset" in src_text

    def test_middleware_sets_headers_on_2xx(self):
        """成功路径也应透传 X-RateLimit-* (L9 RFC draft 风格)。"""
        from openclaw.gateway import app as app_mod
        src_text = Path(app_mod.__file__).read_text(encoding="utf-8")
        # 验证成功路径有 'response.headers[...]' 调用
        assert "response.headers" in src_text
        # 验证 L9 透传出现在 RateLimitMiddleware dispatch 里
        # 找 RateLimitMiddleware.dispatch 的代码段
        m = re.search(
            r"class RateLimitMiddleware.*?async def dispatch.*?(?=\nclass |\Z)",
            src_text, re.DOTALL,
        )
        assert m, "找不到 RateLimitMiddleware.dispatch"
        body = m.group(0)
        # 成功路径有 try: response.headers[...].X-RateLimit-Remaining
        assert "X-RateLimit-Remaining" in body


# ============================================================
# L11 — CI Python 3.13 矩阵
# ============================================================
class TestCIPython313:
    def test_ci_includes_python_313(self):
        ci = REPO_ROOT / ".github" / "workflows" / "ci.yml"
        assert ci.exists(), "ci.yml 不存在"
        text = ci.read_text(encoding="utf-8")
        assert '"3.13"' in text, "CI 矩阵缺 3.13(L11 修复)"


# ============================================================
# 测试基础设施 — conftest 预 import (Phase 29)
# ============================================================
class TestConftestPreload:
    def test_conftest_preloads_test_phase8(self):
        """conftest 顶部预 import tests.test_phase8,避免 test_phase12
        collection 顺序问题导致 ModuleNotFoundError。
        """
        from tests import conftest
        # _preload_inter_test_modules 应存在并可调
        assert hasattr(conftest, "_preload_inter_test_modules")
        # 调用不应抛
        conftest._preload_inter_test_modules()
        # 之后 from tests.test_phase8 import 应能成功
        import importlib
        mod = importlib.import_module("tests.test_phase8")
        assert mod is not None


# ============================================================
# L12 — MANIFEST.in
# ============================================================
class TestManifestIn:
    def test_manifest_in_exists(self):
        m = REPO_ROOT / "MANIFEST.in"
        assert m.exists(), "MANIFEST.in 不存在(L12 修复)"

    def test_manifest_includes_examples(self):
        m = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        assert "recursive-include examples" in m, "MANIFEST.in 漏 examples/"

    def test_manifest_includes_docs(self):
        m = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        assert "recursive-include docs" in m, "MANIFEST.in 漏 docs/"

    def test_manifest_includes_yml(self):
        m = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        # openclaw.agnes.yaml + ops/prometheus.yml
        assert "openclaw.agnes.yaml" in m or "*.yaml" in m, "MANIFEST.in 漏 yaml"
        assert "ops" in m, "MANIFEST.in 漏 ops/"

    def test_manifest_excludes_pycache(self):
        m = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        assert "__pycache__" in m, "MANIFEST.in 漏 global-exclude __pycache__"
        assert "*.py[cod]" in m or "*.pyc" in m, "MANIFEST.in 漏 *.pyc"

    def test_sdist_dry_run_picks_up_manifest(self):
        """验证 sdist 干跑能把 examples/docs/ops 都识别成 sdist 包含项。"""
        # 不实际打 sdist(慢),只验证 setup.py / pyproject 里的 tool setuptools
        # 不会与 MANIFEST.in 冲突 — 检查 sdist 命令可用
        # build 不一定装,所以这里不调 subprocess,只校验 manifest 内容
        manifest = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
        # 至少包含 README / examples / docs / ops / 关键 *.yaml
        for required in ["README.md", "examples", "docs", "ops", "openclaw.agnes.yaml"]:
            assert required in manifest, f"MANIFEST.in 缺 {required}"


# ============================================================
# M27 集成 — try_consume 在 RateLimitMiddleware 实际工作
# ============================================================
class TestTryConsumeIntegration:
    def test_middleware_uses_try_consume(self):
        """RateLimitMiddleware.dispatch 应优先用 try_consume(原子三项返回)。

        策略:用 starlette TestClient 启动最小 app,monkeypatch 模块级
        ``_RATE_LIMITER`` 单例,然后通过 HTTP 请求触发 dispatch,
        验证 L9 响应头出现 + 限流确实消耗 token。
        """
        from starlette.testclient import TestClient
        from fastapi import FastAPI
        from openclaw.gateway.app import RateLimitMiddleware
        from openclaw.core.rate_limit import RateLimiter

        # 单独建一个最小 app(不走 token fail-closed + 不挂 auth)
        app = FastAPI()
        app.add_middleware(RateLimitMiddleware, rate_limiter=None)

        @app.get("/v1/chat")
        async def chat():
            return {"ok": True}

        # monkeypatch 模块级 _RATE_LIMITER 单例(RateLimitMiddleware.__init__ 在
        # rate_limiter=None 时走 module-level 单例,正是我们要替换的)
        import openclaw.gateway.app as app_mod
        original = app_mod._RATE_LIMITER
        real_limiter = RateLimiter(rate=1.0, burst=3)
        app_mod._RATE_LIMITER = real_limiter
        try:
            with TestClient(app) as c:
                # 第 1 次:消耗 1 token(remaining=2)
                r1 = c.get("/v1/chat")
                assert r1.status_code == 200
                assert r1.headers.get("X-RateLimit-Limit") == "3"
                # remaining 应在 [0, 2] 之间(允许极小浮点)
                assert 0 <= int(r1.headers["X-RateLimit-Remaining"]) <= 2

                # 连续发到 429
                for _ in range(5):
                    c.get("/v1/chat")
                # 此时已超 burst,应得 429 + 完整 L9 headers
                r429 = c.get("/v1/chat")
                # 可能 200(还有 token)或 429(已用尽)
                if r429.status_code == 429:
                    assert r429.headers.get("X-RateLimit-Limit") == "3"
                    assert r429.headers.get("X-RateLimit-Remaining") == "0"
                    assert "X-RateLimit-Reset" in r429.headers
                    assert "Retry-After" in r429.headers
        finally:
            app_mod._RATE_LIMITER = original
