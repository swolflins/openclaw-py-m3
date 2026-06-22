"""Phase 30 续修 — 新 review 报告 (7559d475-...) 剩余 5 项未修 + 3 项工程化。

报告 304 行,绝大多数 P0/P1 已在 Phase 25/27/28 修过。本测试覆盖:
- H4 类型清理 (journal.reflect 返回 str,签名与实际一致)
- M10 ASYNC rule hard-fail (journal add_reflection / routes journal 用 to_thread)
- M13 插件加载隔离 (目录白名单 + 大小上限 + owner 校验)
- L1 long_term RLock (允许重入)
- L3 redis_bus aclose + XAUTOCLAIM
- L5 auth user_id sha256 (防日志泄露)
- E6 ruff 规则集 (B/S/ASYNC) + ASYNC hard-fail
- H2 verify (per-token approver admin_token 已生效)
- M14 verify (RateLimiter aallow 用 to_thread)
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# ============================================================
# H4 — journal.reflect 返回类型统一为 str
# ============================================================
class TestH4JournalReflectType:
    def test_signature_is_str_not_list(self):
        """签名必须是 str,不再误述为 list。"""
        from openclaw.agent.journal import AgentJournal
        sig = inspect_signature(AgentJournal.reflect)  # type: ignore[arg-type]
        # annotation 在 Python 3.10 是字符串 'str' 而非 str 类本身
        ann = sig.return_annotation
        assert ann == str or ann == "str", (
            f"AgentJournal.reflect 签名应是 str,实际 {ann!r}"
        )

    def test_docstring_no_longer_says_list(self):
        """docstring 不应再误导为返回 list。"""
        from openclaw.agent.journal import AgentJournal
        # 反射读源文件
        import inspect
        src = inspect.getsource(AgentJournal.reflect)
        assert "返回 ``[reflection, proposal_path]``" not in src, (
            "旧 docstring 误述为 [reflection, proposal_path]"
        )
        assert "-> list[str]" not in src, "签名残留 list[str]"

    def test_journal_module_log_uses_proposal_path(self):
        """proposal 路径走 logger.debug,不是返回值。"""
        from openclaw.agent import journal as jmod
        src = Path(jmod.__file__).read_text(encoding="utf-8")
        # Phase 28 提的方案要落实
        assert "journal_soul_proposal_written" in src, (
            "proposal_path 应走 logger.debug (journal_soul_proposal_written)"
        )


# ============================================================
# M10 — ASYNC rule hard-fail
# ============================================================
class TestM10AsyncFileIO:
    def test_journal_add_reflection_uses_to_thread(self):
        """add_reflection 必须在 async 内走 asyncio.to_thread。"""
        from openclaw.agent.journal import AgentJournal
        import inspect
        src = inspect.getsource(AgentJournal.add_reflection)
        assert "asyncio.to_thread" in src, (
            "AgentJournal.add_reflection 必须用 asyncio.to_thread 调同步文件 IO"
        )
        # 验证 path.open 不在 async 函数内直接出现
        assert ".open(" not in src or "_append_to_file" in src, (
            "add_reflection 不应直接 path.open(...),应走 _append_to_file"
        )

    def test_routes_journal_uses_to_thread_for_resolve(self):
        """routes/journal.py 的 (j.root / path).resolve() 走 to_thread。"""
        from openclaw.gateway.routes import journal as rj
        src = Path(rj.__file__).read_text(encoding="utf-8")
        # resolve() 必须 to_thread
        assert "to_thread(lambda: (j.root / path).resolve())" in src or (
            "to_thread" in src and ".resolve()" in src
        ), "routes/journal 的 Path.resolve() 应走 to_thread"

    def test_ruff_async_rule_clean(self):
        """ruff --select ASYNC 必须 0 错(防 async 内阻塞 IO)。"""
        import subprocess
        result = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "openclaw/",
             "--select", "ASYNC", "--output-format=concise"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        # exit 0 = 0 violations
        assert result.returncode == 0, (
            f"ruff ASYNC 错:\n{result.stdout}\n{result.stderr}"
        )


# ============================================================
# M13 — 插件加载隔离
# ============================================================
class TestM13PluginLoadingIsolation:
    def test_allowlist_helpers_exist(self):
        from openclaw.core import plugin
        assert hasattr(plugin, "_get_allowed_plugin_dirs")
        assert hasattr(plugin, "_is_under_any")
        assert hasattr(plugin, "_MAX_PLUGIN_BYTES")
        assert plugin._MAX_PLUGIN_BYTES == 1 * 1024 * 1024, (
            "M13 修复:1 MiB 上限必须生效"
        )

    def test_env_dir_takes_priority(self, monkeypatch, tmp_path):
        from openclaw.core import plugin
        env_dir = tmp_path / "env_plugins"
        env_dir.mkdir()
        monkeypatch.setenv("OPENCLAW_PLUGIN_DIR", str(env_dir))
        roots = plugin._get_allowed_plugin_dirs()
        assert env_dir.resolve() in [r.resolve() for r in roots], (
            "OPENCLAW_PLUGIN_DIR 必须出现在白名单"
        )

    def test_is_under_any_blocks_outside(self, tmp_path):
        from openclaw.core.plugin import _is_under_any
        allowed = [(tmp_path / "allowed").resolve()]
        # 在白名单内
        inside = (tmp_path / "allowed" / "sub").resolve()
        inside.mkdir(parents=True)
        assert _is_under_any(inside, allowed) is True
        # 在白名单外
        outside = (tmp_path / "evil").resolve()
        outside.mkdir()
        assert _is_under_any(outside, allowed) is False

    def test_load_local_blocks_disallowed_dir(self, tmp_path, monkeypatch):
        """目录不在白名单时 load_local 必须返回 0 + 记 CRITICAL。"""
        from openclaw.core.plugin import PluginManager
        evil_dir = tmp_path / "evil"
        evil_dir.mkdir()
        # 不设 OPENCLAW_PLUGIN_DIR,只让默认 ~/.openclaw/plugins 生效
        monkeypatch.delenv("OPENCLAW_PLUGIN_DIR", raising=False)
        # 写个 fake plugin
        (evil_dir / "evil_plugin.py").write_text("def register(r): pass")
        mgr = PluginManager(runtime=MagicMock())
        # 应该不加载(返回 0),记 CRITICAL
        with patch("openclaw.core.plugin.logger") as mock_logger:
            n = mgr.load_local(evil_dir)
        assert n == 0, "白名单外目录不应加载"
        assert mock_logger.critical.called, "应记 CRITICAL 日志"

    def test_load_local_blocks_oversize(self, tmp_path, monkeypatch):
        """文件 > 1 MiB 不加载。"""
        from openclaw.core.plugin import (
            PluginManager, _MAX_PLUGIN_BYTES,
        )
        # 强制让白名单包含 tmp_path
        monkeypatch.setenv("OPENCLAW_PLUGIN_DIR", str(tmp_path))
        big = tmp_path / "big.py"
        # 写一个 1.5 MiB 文件
        big.write_text("x" * (_MAX_PLUGIN_BYTES + 100_000))
        mgr = PluginManager(runtime=MagicMock())
        with patch("openclaw.core.plugin.logger") as mock_logger:
            n = mgr.load_local(tmp_path)
        assert n == 0, "超大文件不应加载"
        # 验证 warning 日志
        warn_msgs = " ".join(
            str(call) for call in mock_logger.warning.call_args_list
        )
        assert "too_large" in warn_msgs or "1 MiB" in warn_msgs

    def test_load_local_owner_check(self, tmp_path, monkeypatch):
        """owner != current euid 不加载。"""
        from openclaw.core.plugin import PluginManager
        monkeypatch.setenv("OPENCLAW_PLUGIN_DIR", str(tmp_path))
        # 写一个正常 plugin
        (tmp_path / "ok.py").write_text("def register(r): pass")
        mgr = PluginManager(runtime=MagicMock())
        # mock path.stat 返回 owner != euid;path.is_dir() 也要走 stat
        # 我们只 mock 走 os.geteuid 时拿到 fake st_uid
        real_stat = Path.stat
        def fake_stat(self, *a, **kw):
            r = real_stat(self, *a, **kw)
            # 用 SimpleNamespace 包装覆盖 st_uid,保留原 st_mode / st_size
            from types import SimpleNamespace
            return SimpleNamespace(
                st_mode=r.st_mode, st_size=r.st_size,
                st_uid=99999, st_gid=r.st_gid,
            )
        with patch.object(Path, "stat", fake_stat):
            n = mgr.load_local(tmp_path)
        assert n == 0, "owner 不匹配应被拒"


# ============================================================
# L1 — long_term RLock
# ============================================================
class TestL1LongTermRLock:
    def test_lock_is_rlock(self):
        """_lock 必须是 RLock(允许同线程重入),不是 Lock。"""
        import threading
        from openclaw.memory.long_term import LongTermStore
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            store = LongTermStore(d)
            assert isinstance(store._lock, type(threading.RLock())), (
                f"_lock 应是 RLock,实际 {type(store._lock).__name__}"
            )

    def test_rlock_works_in_nested_context(self):
        """RLock 支持同线程嵌套 acquire。"""
        import threading
        with tempfile.TemporaryDirectory() as d:
            from openclaw.memory.long_term import LongTermStore
            store = LongTermStore(d)
            # RLock 支持同线程二次 acquire;Lock 不支持
            with store._lock:
                with store._lock:
                    pass
            # 不抛 → RLock 生效


# ============================================================
# L3 — redis_bus aclose + XAUTOCLAIM
# ============================================================
class TestL3RedisBusAclose:
    def test_aclose_method_exists(self):
        from openclaw.bus.redis_bus import RedisBus
        import inspect
        assert hasattr(RedisBus, "aclose")
        assert inspect.iscoroutinefunction(RedisBus.aclose)

    def test_reclaim_stale_method_exists(self):
        from openclaw.bus.redis_bus import RedisBus
        import inspect
        assert hasattr(RedisBus, "_reclaim_stale")
        assert inspect.iscoroutinefunction(RedisBus._reclaim_stale)

    def test_aclose_closes_client(self):
        """aclose 应当把 _client 设为 None。"""
        from openclaw.bus.redis_bus import RedisBus
        # 跳过 _HAS_REDIS 检查,直接构造 stub
        bus = RedisBus.__new__(RedisBus)
        bus._url = "redis://test"
        bus._prefix = "x"
        bus._client = MagicMock()
        bus._client.aclose = AsyncMock()
        asyncio.run(bus.aclose())
        assert bus._client is None

    def test_reclaim_calls_xautoclaim(self):
        """_reclaim_stale 应调 xautoclaim。"""
        from openclaw.bus.redis_bus import RedisBus
        bus = RedisBus.__new__(RedisBus)
        bus._url = "redis://test"
        bus._prefix = "x"
        client = MagicMock()
        # xautoclaim 返回 (next_cursor, claimed_entries, deleted_ids)
        client.xautoclaim = AsyncMock(return_value=("0-0", [], []))
        bus._client = client
        n = asyncio.run(bus._reclaim_stale("s", "g", "c", stale_idle_ms=1000))
        assert n == 0
        client.xautoclaim.assert_awaited_once()


# ============================================================
# L5 — auth user_id sha256 (verify)
# ============================================================
class TestL5UserIdSha256:
    def test_user_id_starts_with_h_underscore(self):
        """fallback user_id 应是 ``h_`` + sha256[:16],不是 token[:16]。"""
        from openclaw.gateway.auth import AuthMiddleware
        # 不需要实例化整个 middleware,只调 _resolve_user_id 静态/实例
        m = AuthMiddleware.__new__(AuthMiddleware)
        m._user_id = None
        m._token_to_user = {}
        uid = m._resolve_user_id("super-secret-token-abc", {})
        assert uid.startswith("h_"), f"应 h_ 开头,实际 {uid!r}"
        # 不应包含 token 前 16 字符
        assert "super-secret" not in uid
        # 验证是 sha256(token)[:16]
        expected = "h_" + hashlib.sha256(b"super-secret-token-abc").hexdigest()[:16]
        assert uid == expected

    def test_user_id_collision_resistant(self):
        """不同 token 应产生不同 user_id(高概率)。"""
        from openclaw.gateway.auth import AuthMiddleware
        m = AuthMiddleware.__new__(AuthMiddleware)
        m._user_id = None
        m._token_to_user = {}
        u1 = m._resolve_user_id("token-aaaaaaaaaaaa", {})
        u2 = m._resolve_user_id("token-bbbbbbbbbbbb", {})
        assert u1 != u2


# ============================================================
# H2 — per-token approver verify (Phase 25 已修,只验证)
# ============================================================
class TestH2AdminToken:
    def test_admin_token_check_exists(self):
        """_check_admin 必须存在并被 /v1/tools/approver 调。"""
        from openclaw.gateway.routes import tools as rt
        src = Path(rt.__file__).read_text(encoding="utf-8")
        assert "_check_admin" in src
        # 应在 set_global / clear_global 等敏感端点里被调
        # 至少应出现 2 次(定义 + 调用)
        assert src.count("_check_admin") >= 2, (
            "_check_admin 应被敏感端点调用"
        )

    def test_admin_token_env_var(self):
        """应有 OPENCLAW_GATEWAY_ADMIN_TOKEN env 读取。"""
        from openclaw.gateway.routes import tools as rt
        src = Path(rt.__file__).read_text(encoding="utf-8")
        assert "OPENCLAW_GATEWAY_ADMIN_TOKEN" in src, (
            "应支持 OPENCLAW_GATEWAY_ADMIN_TOKEN env 显式配"
        )


# ============================================================
# M14 — RateLimiter aallow 走 to_thread
# ============================================================
class TestM14RateLimiterAallow:
    def test_aallow_uses_to_thread(self):
        """aallow 内部必须 to_thread 调 sync allow,避免阻塞事件循环。"""
        from openclaw.core.rate_limit import RateLimiter
        import inspect
        src = inspect.getsource(RateLimiter.aallow)
        assert "to_thread" in src, (
            "RateLimiter.aallow 必须用 asyncio.to_thread 包装 sync allow"
        )

    def test_redis_aallow_uses_lua_or_to_thread(self):
        """RedisRateLimiter.aallow 走 _run_async → Lua 脚本(已在 Redis 端 atomic)。"""
        from openclaw.core.rate_limit import RedisRateLimiter
        import inspect
        # 整个类(包含 aallow / _run_async)读出来
        src = inspect.getsource(RedisRateLimiter)
        assert "_run_async" in src, "RedisRateLimiter 应走 _run_async (Phase 29)"
        # _run_async 内部用 self._script(...) — Lua 脚本
        run_async_src = inspect.getsource(RedisRateLimiter._run_async)
        assert "_script" in run_async_src, (
            "RedisRateLimiter._run_async 内部用 self._script 调 Lua 脚本(atomic)"
        )


# ============================================================
# E6 — ruff 规则集 + ASYNC hard-fail
# ============================================================
class TestE6RuffRules:
    def test_ruff_lint_section_exists(self):
        toml_text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert "[tool.ruff.lint]" in toml_text, "E6 修复:需启用 [tool.ruff.lint]"
        # 必须启用 B/S/ASYNC
        assert '"B"' in toml_text or "'B'" in toml_text
        assert '"S"' in toml_text or "'S'" in toml_text
        assert '"ASYNC"' in toml_text or "'ASYNC'" in toml_text

    def test_ruff_async_hard_fails_in_ci(self):
        """CI workflow 必须让 ASYNC 错 hard-fail,其他 soft。"""
        ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        assert "ASYNC" in ci, "CI 缺 ASYNC hard-fail 检查(E6 修复)"
        assert "ASYNC_EXIT" in ci or "hard-fail" in ci.lower(), (
            "CI 应区分 ASYNC hard-fail 与其他 soft"
        )


# ============================================================
# helpers
# ============================================================
def inspect_signature(obj):
    import inspect
    return inspect.signature(obj)
