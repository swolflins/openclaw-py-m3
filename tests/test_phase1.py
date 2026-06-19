"""Phase 1:基础设施层测试。"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from openclaw.bus import EventBus
from openclaw.core.config import ConfigLoader
from openclaw.core.logging import bind_context, get_logger, new_trace_id, setup_logging
from openclaw.core.plugin import PluginManager, Runtime


# ---------------- logging ----------------

def test_logging_setup_idempotent():
    setup_logging("INFO", json=True)
    log = get_logger("test")
    log.info("hello", key="value")
    # 不会抛异常


def test_trace_id_context():
    setup_logging("INFO", json=False)
    tid = new_trace_id()
    assert tid.startswith("tr_")
    bind_context(session="s1")
    log = get_logger("x")
    log.info("test message")
    # trace id 应当出现在记录里(我们不解析输出,只验证不抛)


# ---------------- config ----------------

def test_config_load_yaml(tmp_path: Path):
    cfg_file = tmp_path / "openclaw.yaml"
    cfg_file.write_text(textwrap.dedent("""
        default_provider: main
        agent:
          system_prompt: 你是测试 Agent
          max_tool_iterations: 3
        logging:
          level: DEBUG
        providers:
          - name: openai_compat
            model: gpt-4o-mini
            api_key: sk-test
        channels:
          - name: cli
        memory:
          dir: ./.test_mem
    """).strip())
    loader = ConfigLoader(cfg_file)
    cfg = loader.load()
    assert cfg.agent.system_prompt == "你是测试 Agent"
    assert cfg.agent.max_tool_iterations == 3
    assert cfg.logging.level == "DEBUG"
    assert cfg.providers[0].model == "gpt-4o-mini"
    assert cfg.channels[0].name == "cli"


def test_config_load_json(tmp_path: Path):
    cfg_file = tmp_path / "openclaw.json"
    cfg_file.write_text(json.dumps({
        "default_provider": "x",
        "providers": [{"name": "openai_compat", "model": "gpt-4o"}],
    }))
    cfg = ConfigLoader(cfg_file).load()
    assert cfg.providers[0].model == "gpt-4o"


def test_config_env_override(tmp_path: Path, monkeypatch):
    cfg_file = tmp_path / "openclaw.yaml"
    cfg_file.write_text("agent:\n  system_prompt: orig\n")
    monkeypatch.setenv("OPENCLAW_AGENT__SYSTEM_PROMPT", "overridden")
    cfg = ConfigLoader(cfg_file).load()
    assert cfg.agent.system_prompt == "overridden"


def test_config_hot_reload(tmp_path: Path):
    cfg_file = tmp_path / "openclaw.yaml"
    cfg_file.write_text("agent:\n  system_prompt: v1\n")
    loader = ConfigLoader(cfg_file)
    assert loader.load().agent.system_prompt == "v1"

    received: list[str] = []
    loader.watch(lambda c: received.append(c.agent.system_prompt))

    import time
    cfg_file.write_text("agent:\n  system_prompt: v2\n")
    # 等待 watchdog 触发
    for _ in range(30):
        if received:
            break
        time.sleep(0.1)
    loader.stop_watch()
    assert received and received[0] == "v2"


# ---------------- bus ----------------

@pytest.mark.asyncio
async def test_bus_subscribe_and_publish():
    bus = EventBus()
    received: list[dict] = []

    async def h(p):
        received.append(p)

    bus.subscribe("topic.a", h)
    await bus.publish("topic.a", {"x": 1})
    assert received == [{"x": 1}]


@pytest.mark.asyncio
async def test_bus_wildcard():
    bus = EventBus()
    received: list[dict] = []

    async def h(p):
        received.append(p)

    bus.subscribe("message.*", h)
    await bus.publish("message.incoming", {"k": 1})
    await bus.publish("message.outgoing", {"k": 2})
    assert len(received) == 2


@pytest.mark.asyncio
async def test_bus_isolated_handler_error():
    bus = EventBus()

    async def bad(p):
        raise RuntimeError("boom")

    good_msgs: list[dict] = []

    async def good(p):
        good_msgs.append(p)

    bus.subscribe("x", bad)
    bus.subscribe("x", good)
    await bus.publish("x", {"k": 1})  # 不应抛
    assert good_msgs == [{"k": 1}]


# ---------------- plugin ----------------

def test_local_plugin_loading(tmp_path: Path):
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "hello.py").write_text(textwrap.dedent("""
        REGISTERED = []
        def register(runtime):
            runtime.custom['hello_plugin'] = 'ok'
            REGISTERED.append(True)
    """).strip())

    rt = Runtime()
    pm = PluginManager(rt)
    n = pm.load_local(plugin_dir)
    assert n == 1
    assert rt.custom.get("hello_plugin") == "ok"
    assert "hello" in pm.loaded()
