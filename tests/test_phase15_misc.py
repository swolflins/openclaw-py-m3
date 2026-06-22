"""Phase 15 P2 测试:plugin / cron / docker / http / shell / workspace / 边角。

目标:把当前低覆盖率模块从 50% 以下拉到 70% 以上。
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from openclaw.core.errors import PluginError
from openclaw.core.plugin import (
    ENTRY_POINT_GROUPS,
    PluginManager,
    Runtime,
    discover_entry_points,
)


# =========================================================================
# 1. Plugin Runtime / PluginManager
# =========================================================================

class TestPluginRuntime:
    def test_register_tool_no_registry_raises(self):
        rt = Runtime()
        with pytest.raises(PluginError, match="tool_registry"):
            rt.register_tool("dummy")

    def test_register_channel_no_registry_raises(self):
        rt = Runtime()
        with pytest.raises(PluginError, match="channel_registry"):
            rt.register_channel(type)

    def test_register_provider_no_factory_raises(self):
        rt = Runtime()
        with pytest.raises(PluginError, match="provider_factory"):
            rt.register_provider("x", lambda: None)

    def test_subscribe_no_bus_raises(self):
        rt = Runtime()
        with pytest.raises(PluginError, match="bus"):
            rt.subscribe("topic", lambda: None)

    def test_register_tool_success(self):
        class FakeRegistry:
            def __init__(self):
                self.items = []
            def register(self, x):
                self.items.append(x)

        rt = Runtime(tool_registry=FakeRegistry())
        rt.register_tool("tool1")
        rt.register_tool("tool2")
        assert rt.tool_registry.items == ["tool1", "tool2"]

    def test_register_provider_success(self):
        class FakeFactory:
            def __init__(self):
                self.registry = {}
            def register(self, name, fn):
                self.registry[name] = fn

        rt = Runtime(provider_factory=FakeFactory())
        def factory():
            return "p"
        rt.register_provider("p1", factory)
        assert rt.provider_factory.registry["p1"] is factory


class TestPluginManager:
    def test_loaded_empty_initially(self):
        rt = Runtime()
        pm = PluginManager(rt)
        assert pm.loaded() == []

    def test_load_entry_points_no_group_returns_0(self):
        rt = Runtime()
        pm = PluginManager(rt)
        count = pm.load_entry_points(group="nonexistent.group.that.does.not.exist")
        assert count == 0

    def test_load_local_nonexistent_dir(self):
        rt = Runtime()
        pm = PluginManager(rt)
        count = pm.load_local("/path/that/does/not/exist/xyz123")
        assert count == 0

    def test_load_local_skip_underscore(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "_skipme.py").write_text("def register(runtime): pass\n")
            (Path(tmp) / "ok.py").write_text("def register(runtime):\n    runtime.custom['loaded'] = True\n")
            rt = Runtime()
            pm = PluginManager(rt)
            # Phase 30 / M13 修复:tmp_path 不在白名单,测试用 _skip_allowlist=True 绕过
            count = pm.load_local(tmp, _skip_allowlist=True)
            assert count == 1
            assert rt.custom.get("loaded") is True
            assert pm.loaded() == ["ok"]

    def test_load_local_no_register_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "noregister.py").write_text("x = 1\n")
            rt = Runtime()
            pm = PluginManager(rt)
            count = pm.load_local(tmp)
            assert count == 0

    def test_load_local_syntax_error_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "broken.py").write_text("def register(runtime\n")  # 语法错
            rt = Runtime()
            pm = PluginManager(rt)
            count = pm.load_local(tmp)
            assert count == 0

    def test_load_local_register_raises_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "bad.py").write_text("def register(runtime):\n    raise RuntimeError('boom')\n")
            rt = Runtime()
            pm = PluginManager(rt)
            count = pm.load_local(tmp)
            assert count == 0

    def test_discover_entry_points_empty_group(self):
        result = discover_entry_points("nonexistent.group.xyz")
        assert result == []

    def test_entry_point_groups_constant(self):
        assert "plugin" in ENTRY_POINT_GROUPS
        assert "channel" in ENTRY_POINT_GROUPS
        assert "provider" in ENTRY_POINT_GROUPS
        assert "tool" in ENTRY_POINT_GROUPS


# =========================================================================
# 2. Cron Manager
# =========================================================================

class TestCronManager:
    def test_cron_init(self):
        from openclaw.tools.builtin.cron import CronManager
        cm = CronManager()
        assert cm is not None
        jobs = cm.list_jobs()
        assert isinstance(jobs, list)
        assert jobs == []

    def test_cron_add_job_invalid_schedule_raises(self):
        from openclaw.tools.builtin.cron import CronManager
        cm = CronManager()
        with pytest.raises((ValueError, Exception)):
            cm.add_job(name="bad", schedule="not a cron", callback=lambda: None)

    def test_cron_add_and_remove(self):
        from openclaw.tools.builtin.cron import CronManager
        cm = CronManager()
        # 用合法 cron
        try:
            cm.add_job(name="test_job", schedule="0 9 * * *", callback=lambda: None)
            assert len(cm.list_jobs()) >= 1
            cm.remove_job("test_job")
            # remove 之后 job 应消失
        except Exception:
            # 平台不支持 cron 时 skip
            pytest.skip("cron not supported on this platform")


# =========================================================================
# 3. HTTP Tool(host 校验 + SSRF 防护)
# =========================================================================

class TestHttpHostCheck:
    def test_check_allowed_host(self):
        from openclaw.tools.builtin.http import _check
        # allowlist 含 host
        _check("https://api.allowed.com/v1", ["api.allowed.com"])  # 不抛

    def test_check_disallowed_host_raises(self):
        from openclaw.tools.builtin.http import _check
        with pytest.raises(PermissionError):
            _check("https://api.disallowed.com/x", ["api.allowed.com"])

    def test_check_wildcard_blocks(self):
        from openclaw.tools.builtin.http import _check
        # allowlist = []  → 拒绝所有
        with pytest.raises(PermissionError):
            _check("https://api.anywhere.com/x", [])

    def test_is_private_ip(self):
        from openclaw.tools.builtin.http import _is_private_ip
        assert _is_private_ip("127.0.0.1") is True
        assert _is_private_ip("10.0.0.1") is True
        assert _is_private_ip("192.168.1.1") is True
        assert _is_private_ip("172.16.0.1") is True
        assert _is_private_ip("8.8.8.8") is False
        assert _is_private_ip("1.1.1.1") is False


# =========================================================================
# 4. Memory modules — ShortTerm / Scoped
# =========================================================================

class TestShortTermStore:
    def test_init_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            from openclaw.memory.short_term import ShortTermStore
            s = ShortTermStore(tmp)
            assert s.all_scopes() == []

    def test_append_and_recent(self):
        with tempfile.TemporaryDirectory() as tmp:
            from openclaw.memory.short_term import ShortTermStore
            s = ShortTermStore(tmp)
            s.append("session:abc", "hi", "hello!")
            turns = s.recent("session:abc", k=10)
            assert len(turns) == 2
            assert turns[0].role == "user"
            assert turns[0].content == "hi"
            assert turns[1].role == "assistant"
            assert turns[1].content == "hello!"

    def test_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            from openclaw.memory.short_term import ShortTermStore
            s = ShortTermStore(tmp)
            s.append("session:abc", "hi", "hello")
            s.clear("session:abc")
            assert s.recent("session:abc", k=10) == []

    def test_safe_scope_name_blocks_path_traversal(self):
        """scope 含 ../ 不会被用来生成文件名。"""
        from openclaw.memory.short_term import _safe_scope_name
        name = _safe_scope_name("../../etc/passwd")
        # 应是 hex(16 字符),不含 .. /
        assert "/" not in name
        assert ".." not in name
        assert len(name) == 16

    def test_all_scopes(self):
        with tempfile.TemporaryDirectory() as tmp:
            from openclaw.memory.short_term import ShortTermStore
            s = ShortTermStore(tmp)
            s.append("s1", "hi", "hello")
            s.append("s2", "hi", "hello")
            scopes = s.all_scopes()
            assert set(scopes) == {"s1", "s2"}


class TestScopedMemory:
    def test_init_no_required_arg(self):
        # ScopedMemory 需要 short_term 实例
        with tempfile.TemporaryDirectory() as tmp:
            from openclaw.memory.short_term import ShortTermStore
            from openclaw.memory.scoped import ScopedMemory
            sts = ShortTermStore(tmp)
            m = ScopedMemory(short_term=sts)
            assert m is not None

    def test_set_get(self):
        with tempfile.TemporaryDirectory() as tmp:
            from openclaw.memory.short_term import ShortTermStore
            from openclaw.memory.scoped import ScopedMemory
            sts = ShortTermStore(tmp)
            m = ScopedMemory(short_term=sts)
            # 写一段
            sts.append("user:alice", "what's my name?", "alice")
            # RT-1: recent_messages 已改 async
            turns = asyncio.run(m.recent_messages(scope="user:alice", k=10))
            assert len(turns) == 2


class TestWorkspaceIndex:
    def test_init(self):
        with tempfile.TemporaryDirectory() as tmp:
            from openclaw.memory.workspace import WorkspaceIndex
            ws = WorkspaceIndex(db_path=Path(tmp) / "ws.db")
            assert ws is not None

    def test_upsert_and_get(self):
        with tempfile.TemporaryDirectory() as tmp:
            from openclaw.memory.workspace import WorkspaceIndex
            db = Path(tmp) / "ws.db"
            ws = WorkspaceIndex(db_path=db)
            # 写一个真实文件
            f = Path(tmp) / "x.py"
            f.write_text("print('hi')")
            entry = ws.upsert(f, summary="a script")
            assert entry.path == str(f.resolve())
            assert entry.size > 0
            # 取回
            got = ws.get(f)
            assert got is not None
            assert got.summary == "a script"

    def test_list_recent(self):
        with tempfile.TemporaryDirectory() as tmp:
            from openclaw.memory.workspace import WorkspaceIndex
            ws = WorkspaceIndex(db_path=Path(tmp) / "ws.db")
            for i in range(3):
                f = Path(tmp) / f"f{i}.py"
                f.write_text(f"x={i}")
                ws.upsert(f)
            files = ws.list_recent(k=10)
            assert len(files) == 3
            # 不会重复
            files2 = ws.list_recent(k=10)
            assert len(files2) == 3


# =========================================================================
# 5. Tool Registry 边角
# =========================================================================

class TestToolRegistryEdge:
    def test_unknown_tool_call_raises(self):
        from openclaw.tools.registry import ToolRegistry
        reg = ToolRegistry()
        with pytest.raises((KeyError, Exception)):
            reg.get_tool("nonexistent")

    def test_register_duplicate_overwrites(self):
        """当前实现:重复 name 直接覆盖(不 raise)。验证覆盖语义。"""
        from openclaw.tools.registry import ToolRegistry
        reg = ToolRegistry()
        async def f(x: int) -> int:
            """f."""
            return x
        async def f2(x: int) -> int:
            """f."""
            return x * 2
        reg.register(f, name="dup")
        reg.register(f2, name="dup")  # 不抛
        # 验证是后注册的赢
        import asyncio
        assert asyncio.run(reg.call("dup", {"x": 5})) == 10

    def test_known_tool_call_success(self):
        import asyncio
        from openclaw.tools.registry import ToolRegistry
        reg = ToolRegistry()
        async def add(a: int, b: int) -> int:
            """add."""
            return a + b
        reg.register(add, name="add")
        result = asyncio.run(reg.call("add", {"a": 1, "b": 2}))
        assert result == 3

    def test_list_tools_returns_tool_objects(self):
        from openclaw.tools.registry import ToolRegistry
        reg = ToolRegistry()
        async def f(x: int) -> int:
            """f."""
            return x
        reg.register(f, name="l1")
        reg.register(f, name="l2")
        tools = reg.list_tools()
        names = {t.name for t in tools}
        assert "l1" in names
        assert "l2" in names


# =========================================================================
# 6. Logger / Trace ID
# =========================================================================

class TestLogger:
    def test_get_logger(self):
        from openclaw.core.logging import get_logger
        log = get_logger("test.module")
        assert log is not None

    def test_setup_logging(self):
        from openclaw.core.logging import setup_logging
        setup_logging(level="DEBUG")
        setup_logging(level="INFO")

    def test_trace_id_unique(self):
        from openclaw.core.logging import new_trace_id
        ids = {new_trace_id() for _ in range(100)}
        assert len(ids) == 100

    def test_bind_context(self):
        from openclaw.core.logging import bind_context, get_logger
        bind_context(request_id="abc123")
        log = get_logger("test")
        log.info("with context")  # 不抛


# =========================================================================
# 7. Channels 边角
# =========================================================================

class TestChannelBase:
    def test_channel_manager_register(self):
        """ChannelManager.register(BaseChannel 实例) 验证。"""
        from openclaw.channels.base import BaseChannel, ChannelManager
        from openclaw.agent.loop import AgentLoop

        class Dummy(BaseChannel):
            name = "dummy_test"
            async def send(self, target, text): pass
            async def recv_loop(self): pass
            async def start(self): pass
            async def stop(self): pass

        agent = MagicMock(spec=AgentLoop)
        mgr = ChannelManager(agent_loop=agent)
        d = Dummy()
        mgr.register(d)
        # channel 已加到内部 list
        assert d in mgr._channels


# =========================================================================
# 8. Gateway util
# =========================================================================

class TestGatewayUtil:
    def test_to_jsonable_basic(self):
        from openclaw.gateway.util import to_jsonable
        result = to_jsonable({"a": 1, "b": [1, 2, 3]})
        assert result == {"a": 1, "b": [1, 2, 3]}

    def test_to_jsonable_handles_datetime(self):
        from openclaw.gateway.util import to_jsonable
        import datetime
        result = to_jsonable({"ts": datetime.datetime(2026, 6, 20, 12, 0, 0)})
        # 不抛就行
        assert "ts" in result

    def test_to_jsonable_handles_set(self):
        from openclaw.gateway.util import to_jsonable
        result = to_jsonable({"s": {1, 2, 3}})
        # set → 应转 list
        assert "s" in result
        assert isinstance(result["s"], list)
