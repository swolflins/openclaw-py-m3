"""Phase 25: dev 模式无 token 在 0.0.0.0 监听时必须 fail-fast。

设计意图:
- ``AuthMiddleware.dispatch`` line 122-124 的 ``if not self._tokens: return await call_next(request)``
  是已知的 dev 模式"无 token 即放行"安全陷阱。
- 修复方式:启动期在 ``create_app`` 中检查 host;0.0.0.0 + 无 token → 抛 RuntimeError。
- 127.0.0.1 仍允许无 token(纯本地开发)。
- 0.0.0.0 + 有 token → 正常启动,/v1/* 无 token 返回 401。

参考 tests/test_phase8.py 的 mock fixture(FakeAgentLoop + GatewayDeps + TestClient)。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# -------- mock AgentLoop(与 test_phase8 保持一致) --------

class FakeMsg:
    def __init__(self, role: str, content: str):
        self.role = role
        self.content = content
    def __repr__(self):
        return f"FakeMsg({self.role!r}, {self.content[:30]!r})"


class FakeShort:
    def __init__(self):
        self.store: dict[str, list[FakeMsg]] = {}
    def all_scopes(self) -> list[str]:
        return list(self.store.keys())
    def recent_messages(self, scope, k=20):
        return self.store.get(scope, [])[-k:]
    async def append_turn(self, scope, role, content, name=None, tool_call_id=None):
        self.store.setdefault(scope, []).append(FakeMsg(role, content))
    def clear(self, scope):
        self.store.pop(scope, None)


class FakeLongItem:
    def __init__(self, id, text, metadata=None, score=0.9):
        self.id = id
        self.text = text
        self.metadata = metadata or {}
        self.score = score


class FakeLong:
    def __init__(self):
        self._id = 0
        self.items: list[FakeLongItem] = []
    def add(self, scope, text, metadata=None):
        self._id += 1
        it = FakeLongItem(self._id, text, metadata or {})
        self.items.append(it)
        return it.id
    def recall(self, scope, query, top_k=5):
        hits = [it for it in self.items if query in it.text]
        return hits[:top_k]


class FakeSoul:
    def __init__(self, doc="You are a helpful assistant."):
        self.doc = doc
        self.paths: list[Path] = [Path("/tmp/SOUL.md")]
    def render_system_prompt(self, base=""):
        return base + "\n\n[SOUL]\n" + self.doc
    def reload(self):
        return self.paths


class FakeScoped:
    def __init__(self):
        self.short = FakeShort()
        self.long = FakeLong()
        self.soul = FakeSoul()


class FakeToolSpec:
    def __init__(self, name, description="", category="", permission="SAFE"):
        self.name = name
        self.description = description
        self.category = category
        self.permission = permission


class FakeRegistry:
    def __init__(self):
        self.specs = [
            FakeToolSpec("get_time", "获取当前时间", "datetime", "SAFE"),
        ]
    def list_tools(self):
        return self.specs
    async def call(self, name, args):
        return {"ok": True, "echo": f"{name}({args})"}


class FakeAgentLoop:
    def __init__(self):
        self.memory = FakeScoped()
        self.tools = FakeRegistry()
        self.system_prompt = "你是一个 OpenClaw 助手。"
        self.calls: list[tuple[str, str]] = []
    async def handle(self, session_id, text, **kw):
        class R:
            content: str = ""
            tool_calls: list = []
            iterations: int = 1
        self.calls.append((session_id, text))
        r = R()
        r.content = f"[echo:{session_id}] {text}"
        r.tool_calls = []
        return r
    async def new_session(self, sid=None):
        return sid or f"sess-{int(time.time()*1000)}"


@pytest.fixture
def deps(monkeypatch):
    from openclaw.gateway import deps as deps_mod
    deps_mod.reset_deps()
    agent = FakeAgentLoop()
    d = deps_mod.GatewayDeps(agent_loop=agent, config_path=Path("/tmp/openclaw_test.yaml"))
    deps_mod.set_deps(d)
    yield d
    deps_mod.reset_deps()


@pytest.fixture
def _no_token(monkeypatch):
    """清空 token,确保不依赖外部环境。"""
    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
    # 也不要让 OPENCLAW_GATEWAY_HOST 干扰(host 参数显式传)
    monkeypatch.delenv("OPENCLAW_GATEWAY_HOST", raising=False)
    # H1 修复:conftest.py 的 autouse fixture 设了 OPENCLAW_GATEWAY_DEV=1
    # 默认保留 dev 模式(127.0.0.1 测试需要它);fail-fast 测试单独删


# -------- 1) host=0.0.0.0 + no token → RuntimeError --------

def test_create_app_failfast_when_0_0_0_0_without_token(deps, _no_token, monkeypatch):
    """0.0.0.0 + 无 token + 非 dev 模式 → 启动期必须 fail-fast。"""
    # H1 修复:conftest.py 的 autouse fixture 设了 OPENCLAW_GATEWAY_DEV=1,需显式关闭
    # 注意:必须先 import create_app(触发模块级 app=create_app()),
    # 再删 OPENCLAW_GATEWAY_DEV,否则 import 时模块级 create_app() 会因 dev 关闭而失败
    from openclaw.gateway.app import create_app
    monkeypatch.delenv("OPENCLAW_GATEWAY_DEV", raising=False)
    with pytest.raises(RuntimeError) as excinfo:
        create_app(deps=deps, host="0.0.0.0")
    # 错误信息要包含建议命令
    msg = str(excinfo.value)
    assert "OPENCLAW_GATEWAY_TOKEN" in msg
    assert "secrets.token_urlsafe" in msg
    assert "0.0.0.0" in msg


# -------- 2) host=127.0.0.1 + no token → 启动 OK, /v1/* 无 token 也能 200 --------

def test_create_app_ok_when_127_0_0_1_without_token(deps, _no_token):
    """127.0.0.1 + 无 token 仍允许(纯本地开发),/v1/* 也能裸调。"""
    from openclaw.gateway.app import create_app
    app = create_app(deps=deps, host="127.0.0.1")
    client = TestClient(app)
    # 无 token 访问 /v1/chat 应直接 200(dev 放行)
    r = client.post("/v1/chat", json={"session_id": "u1", "message": "hi"})
    assert r.status_code == 200
    assert "hi" in r.json()["content"]


# -------- 3) host=0.0.0.0 + token → 启动 OK, /v1/* 无 token 返回 401 --------

def test_create_app_ok_when_0_0_0_0_with_token_but_rejects_unauth(deps, monkeypatch):
    """0.0.0.0 + 有 token → 正常启动;无 token 访问 /v1/* 必须 401。"""
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "phase25-test-token-please-ignore-32chars")
    monkeypatch.delenv("OPENCLAW_GATEWAY_HOST", raising=False)
    from openclaw.gateway.app import create_app
    app = create_app(deps=deps, host="0.0.0.0")
    client = TestClient(app)
    # 无 token → 401
    r = client.post("/v1/chat", json={"session_id": "u1", "message": "hi"})
    assert r.status_code == 401
    assert "token" in r.json()["detail"].lower()
    # 有 token → 200
    r2 = client.post(
        "/v1/chat",
        json={"session_id": "u1", "message": "hi"},
        headers={"Authorization": "Bearer phase25-test-token-please-ignore-32chars"},
    )
    assert r2.status_code == 200
    assert "hi" in r2.json()["content"]


# -------- 4) AuthMiddleware __init__ 直接校验(双层保险) --------

def test_auth_middleware_init_raises_when_0_0_0_0_no_token(monkeypatch):
    """Phase 25 双层保险:AuthMiddleware(host='0.0.0.0', tokens=[]) 必抛 RuntimeError。"""
    # H1 修复:conftest.py 的 autouse fixture 设了 OPENCLAW_GATEWAY_DEV=1,需显式关闭
    monkeypatch.delenv("OPENCLAW_GATEWAY_DEV", raising=False)
    from starlette.applications import Starlette
    from openclaw.gateway.auth import AuthMiddleware
    inner = Starlette()
    with pytest.raises(RuntimeError) as excinfo:
        AuthMiddleware(inner, tokens=[], host="0.0.0.0")
    msg = str(excinfo.value)
    assert "OPENCLAW_GATEWAY_TOKEN" in msg
    assert "0.0.0.0" in msg


def test_auth_middleware_init_ok_when_127_0_0_1_no_token():
    """127.0.0.1 + 无 token → AuthMiddleware 应正常构造(只 warn)。"""
    from starlette.applications import Starlette
    from openclaw.gateway.auth import AuthMiddleware
    inner = Starlette()
    # 不应抛错
    mw = AuthMiddleware(inner, tokens=[], host="127.0.0.1")
    assert mw._tokens == []


def test_auth_middleware_init_ok_when_0_0_0_0_with_token():
    """0.0.0.0 + 有 token → AuthMiddleware 应正常构造。"""
    from starlette.applications import Starlette
    from openclaw.gateway.auth import AuthMiddleware
    inner = Starlette()
    mw = AuthMiddleware(inner, tokens=["sometoken"], host="0.0.0.0")
    assert mw._tokens == ["sometoken"]


def test_auth_middleware_init_no_host_keeps_dev_compat():
    """Phase 25 兼容:不传 host(host=None)时,保持原 dev 行为 — 不阻断。"""
    from starlette.applications import Starlette
    from openclaw.gateway.auth import AuthMiddleware
    inner = Starlette()
    # 即使 tokens=[] 也不应抛错(host=None 保持 dev 兼容)
    mw = AuthMiddleware(inner, tokens=[])
    assert mw._tokens == []
