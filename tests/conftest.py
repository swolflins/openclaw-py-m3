"""全局测试配置(autouse fixtures)。

设计目标:
- 让 TestClient 不被限流中间件触发 429(测试不该限流)
- 让所有需要 env 的模块用一致的 placeholder key
"""
from __future__ import annotations

import pytest


# Phase 29 修复:几个 test_phase12 测试用 ``from tests.test_phase8 import ...`` 这种
# 写法,依赖 test_phase8 先被 pytest collect;全量跑目录时如果 test_phase8 还没 import,
# 会出现 ``ModuleNotFoundError: No module named 'tests.test_phase8'``。
# 这里在 conftest 顶部预 import 几个被其他 test 引用 module,确保 collection 顺序
# 不影响运行时 import。
# 注意:这不会改变测试逻辑,只是给 collection order 兜底。
def _preload_inter_test_modules() -> None:
    import importlib
    for mod_name in ("tests.test_phase8",):
        try:
            importlib.import_module(mod_name)
        except Exception:  # pragma: no cover
            pass


_preload_inter_test_modules()


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
