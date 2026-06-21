"""全局测试配置(autouse fixtures)。

设计目标:
- 让 TestClient 不被限流中间件触发 429(测试不该限流)
- 让所有需要 env 的模块用一致的 placeholder key
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_gateway_rate_limit(monkeypatch):
    """SEC-12:测试期间关掉 gateway 限流,避免 burst=3 触发 429。"""
    monkeypatch.setenv("OPENCLAW_GATEWAY_RL_DISABLED", "1")
    # H1 修复:测试期间显式开启 dev 模式,允许无 token 运行
    monkeypatch.setenv("OPENCLAW_GATEWAY_DEV", "1")


@pytest.fixture(autouse=True)
def _fake_llm_keys(monkeypatch):
    """给 provider factory / agent loop 提供 dummy LLM key。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-used")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-used")
