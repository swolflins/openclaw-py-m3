"""最小冒烟测试,验证各模块可以正常 import。

为了支持"分阶段上传"(每个 commit 只含当前 phase 的代码),
所有非本阶段的子模块都通过 `pytest.importorskip` 软跳过 ——
这样在 phase 1 的 commit 里,`pytest tests/` 也只会跑当前阶段涉及的断言。
"""
from __future__ import annotations

import pytest


def test_openclaw_version():
    import openclaw
    assert openclaw.__version__ == "0.1.0"


def test_settings_import():
    from openclaw.config import get_settings
    from openclaw.config.settings import Settings
    assert Settings is not None
    assert get_settings is not None


def test_core_imports():
    from openclaw.core import (
        setup_logging,
        OpenClawError,
    )
    assert setup_logging is not None
    assert issubclass(OpenClawError, Exception)


def test_bus_imports():
    from openclaw.bus import EventBus
    assert EventBus is not None


def test_llm_base_imports():
    from openclaw.llm.base import ChatMessage, LLMResult
    assert ChatMessage is not None
    assert LLMResult is not None


# ---- 以下为分阶段 soft-import 验证,缺模块时跳过 ----

@pytest.mark.parametrize("name", [
    "openclaw.providers",
    "openclaw.tools.registry",
    "openclaw.memory",
    "openclaw.agent.loop",
    "openclaw.channels",
])
def test_optional_submodules_import(name):
    """每个 phase commit 后,对应的子模块应当存在并可 import。"""
    pytest.importorskip(name)
    import importlib
    mod = importlib.import_module(name)
    assert mod is not None


def test_providers_phase2():
    pytest.importorskip("openclaw.providers")
    from openclaw.providers import (
        ProviderFactory,
    )
    assert ProviderFactory is not None


def test_tools_phase4():
    pytest.importorskip("openclaw.tools.registry")
    from openclaw.tools import ToolRegistry
    assert ToolRegistry is not None


def test_memory_phase3():
    pytest.importorskip("openclaw.memory")
    from openclaw.memory import ShortTermStore
    assert ShortTermStore is not None


def test_agent_phase5():
    pytest.importorskip("openclaw.agent.loop")
    from openclaw.agent import AgentLoop
    assert AgentLoop is not None


def test_channels_phase7():
    pytest.importorskip("openclaw.channels")
    from openclaw.channels import CLIChannel
    assert CLIChannel is not None
