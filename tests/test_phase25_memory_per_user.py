"""Phase 25/a5:/v1/memory/* per-user 隔离(防横向越权)。

设计意图:
- 之前 /v1/memory/* 直接用 caller 给的 scope 读/写 memory backend,
  导致任何 token 都能读/写/清任意 scope → 横向越权。
- 修复:AuthMiddleware 在鉴权通过后,把 user_id 挂到 request.state;
  路由层(``routes/memory.py``)用 ``current_user_id(request)`` 给 scope 加前缀
  (``f"{user_id}:{scope}"``),使不同 token 的数据物理隔离。
- 本测试覆盖 4 个场景:
  1. token A 写 scope=X,token B 读 scope=X → 拿空(防越权)
  2. token A 写 + token A 读 → 拿到自己写的(自己仍能用)
  3. token A DELETE scope=X,token B 仍能读到(用户隔离)
  4. anonymous(无 token) 写 → 写到 anonymous scope(向后兼容)
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# -------- mock AgentLoop(与 test_phase8 / test_phase25_auth_failfast 保持一致) --------

class FakeMsg:
    def __init__(self, role: str, content: str):
        self.role = role
        self.content = content

    def __repr__(self):
        return f"FakeMsg({self.role!r}, {self.content[:30]!r})"


class FakeShort:
    """简单 in-memory short-term store:scope → list[FakeMsg]。

    路由层会把 user_id 拼到 scope 前(``alice:X`` / ``bob:X``),所以这里
    直接拿 ``store.get(scope, [])`` 即可 — 不同 user 的 key 不会冲突。
    """

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
        # 按 scope 区分,实现物理隔离
        it._scope = scope
        self.items.append(it)
        return it.id

    def recall(self, scope, query, top_k=5):
        hits = [it for it in self.items if it._scope == scope and query in it.text]
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
        return sid or f"sess-{int(time.time() * 1000)}"


# -------- fixtures --------

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
def _clear_token_env(monkeypatch):
    """让 ``AuthMiddleware`` 不从环境读 token,完全靠传参控制。"""
    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("OPENCLAW_GATEWAY_HOST", raising=False)


def _make_client(deps, tokens: list[str], token_to_user: dict[str, str] | None = None):
    """构造一个启用鉴权 + token_to_user 的 TestClient。

    注意:``create_app`` 内部会读 env 拿 tokens;但 ``token_to_user`` 只能靠
    显式传(我们重新 reset middleware + 重新 install_auth 注入)。
    """
    from openclaw.gateway.app import create_app
    from openclaw.gateway import auth as auth_mod
    import os

    # 走 env 让 create_app 拿到 tokens(再用 install_auth 重置挂我们的 token_to_user)
    prev = os.environ.get("OPENCLAW_GATEWAY_TOKEN")
    if tokens:
        os.environ["OPENCLAW_GATEWAY_TOKEN"] = ",".join(tokens)
    else:
        os.environ.pop("OPENCLAW_GATEWAY_TOKEN", None)
    try:
        app = create_app(deps=deps, host="127.0.0.1", rate_limiter=None)
        # create_app 已 install_auth(env 读 token),但 env 不带 token_to_user。
        # 重置 middleware 栈,重新 install_auth 把 token_to_user 注入。
        app.middleware_stack = None  # type: ignore[attr-defined]
        app.user_middleware = []  # type: ignore[attr-defined]
        auth_mod.install_auth(app, tokens=tokens, host="127.0.0.1", token_to_user=token_to_user)
        return TestClient(app)
    finally:
        # 还原 env,避免污染后续 test
        if prev is None:
            os.environ.pop("OPENCLAW_GATEWAY_TOKEN", None)
        else:
            os.environ["OPENCLAW_GATEWAY_TOKEN"] = prev


# 用一个固定 token 列表 + 映射
TOK_A = "token-A-padded-to-32-chars-aaaa"
TOK_B = "token-B-padded-to-32-chars-bbbb"
USER_A = "alice"
USER_B = "bob"
TOKENS = [TOK_A, TOK_B]
MAPPING = {TOK_A: USER_A, TOK_B: USER_B}


def _hdr(token: str | None) -> dict[str, str]:
    if token is None:
        return {}
    return {"Authorization": f"Bearer {token}"}


# -------- 场景 1:横向越权 — token A 写,token B 读,拿空 --------

def test_token_b_cannot_read_token_a_short_term(deps, _clear_token_env):
    """A 写到 alice:X,B 读 bob:X(同一原始 scope X)→ 拿不到 A 的数据。"""
    client = _make_client(deps, TOKENS, MAPPING)

    # A 写两条
    r = client.post(
        "/v1/memory/short",
        json={"scope": "X", "role": "user", "content": "A's secret message"},
        headers=_hdr(TOK_A),
    )
    assert r.status_code == 200, r.text
    r = client.post(
        "/v1/memory/short",
        json={"scope": "X", "role": "assistant", "content": "A's reply"},
        headers=_hdr(TOK_A),
    )
    assert r.status_code == 200, r.text

    # B 读 scope=X(应拿空,因为 B 的 user_id 是 bob,scope 实际是 bob:X)
    r = client.get("/v1/memory/short", params={"scope": "X"}, headers=_hdr(TOK_B))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scope"] == "X"
    assert body["user_id"] == USER_B  # 路由回写的 user_id
    assert body["count"] == 0
    assert body["messages"] == []


# -------- 场景 2:自己写自己读 → 拿到自己写的 --------

def test_token_a_can_read_own_short_term(deps, _clear_token_env):
    """A 写 scope=X,A 读 scope=X → 拿到 A 自己写的两条。"""
    client = _make_client(deps, TOKENS, MAPPING)

    r = client.post(
        "/v1/memory/short",
        json={"scope": "X", "role": "user", "content": "hello from A"},
        headers=_hdr(TOK_A),
    )
    assert r.status_code == 200
    r = client.post(
        "/v1/memory/short",
        json={"scope": "X", "role": "assistant", "content": "hi A"},
        headers=_hdr(TOK_A),
    )
    assert r.status_code == 200

    r = client.get("/v1/memory/short", params={"scope": "X"}, headers=_hdr(TOK_A))
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == USER_A
    assert body["count"] == 2
    roles = [m["role"] for m in body["messages"]]
    contents = [m["content"] for m in body["messages"]]
    assert roles == ["user", "assistant"]
    assert contents == ["hello from A", "hi A"]


# -------- 场景 3:token A DELETE scope=X,token B 仍能读到 --------

def test_token_a_clear_does_not_affect_token_b(deps, _clear_token_env):
    """A 清空自己的 scope,A 自己读拿空;B 写后 B 仍能读到自己写的。"""
    client = _make_client(deps, TOKENS, MAPPING)

    # A 写一条
    client.post(
        "/v1/memory/short",
        json={"scope": "X", "role": "user", "content": "A's data"},
        headers=_hdr(TOK_A),
    )
    # A 清空自己 alice:X
    r = client.delete("/v1/memory/short/X", headers=_hdr(TOK_A))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["user_id"] == USER_A

    # A 再读 → 拿空(自己清空了)
    r = client.get("/v1/memory/short", params={"scope": "X"}, headers=_hdr(TOK_A))
    assert r.status_code == 200
    assert r.json()["count"] == 0

    # B 写一条到 bob:X
    r = client.post(
        "/v1/memory/short",
        json={"scope": "X", "role": "user", "content": "B's data"},
        headers=_hdr(TOK_B),
    )
    assert r.status_code == 200

    # B 读 → 拿到自己写的
    r = client.get("/v1/memory/short", params={"scope": "X"}, headers=_hdr(TOK_B))
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == USER_B
    assert body["count"] == 1
    assert body["messages"][0]["content"] == "B's data"


# -------- 场景 4:anonymous(无 token) 写 → 写到 anonymous scope --------

def test_anonymous_user_writes_to_anonymous_scope(deps, _clear_token_env):
    """无 token(无 token 配置时 dev 模式)→ user_id='anonymous',写读都落到 anonymous scope。"""
    # 关键:不传 token 给 AuthMiddleware(让 _tokens=[] 放行 + 强制 anonymous)
    client = _make_client(deps, tokens=[], token_to_user=None)

    # 写一条
    r = client.post(
        "/v1/memory/short",
        json={"scope": "anon-scope", "role": "user", "content": "anon message"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user_id"] == "anonymous"

    # 读 → 拿到自己写的(写到 anonymous:anon-scope)
    r = client.get("/v1/memory/short", params={"scope": "anon-scope"})
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == "anonymous"
    assert body["count"] == 1
    assert body["messages"][0]["content"] == "anon message"

    # 长期也跑一遍同样的路径(防止只 short 写对了)
    r = client.post(
        "/v1/memory/long",
        json={"scope": "anon-scope", "text": "anon long-term fact", "metadata": {}},
    )
    assert r.status_code == 200, r.text
    r = client.get(
        "/v1/memory/long",
        params={"scope": "anon-scope", "query": "anon"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == "anonymous"
    assert body["count"] == 1
    assert "anon" in body["items"][0]["text"]


# -------- 额外:long-term 也隔离(防 short 走对了 long 没走对) --------

def test_long_term_isolated_between_users(deps, _clear_token_env):
    """long-term 也要 per-user 隔离。"""
    client = _make_client(deps, TOKENS, MAPPING)

    # A add 一条
    r = client.post(
        "/v1/memory/long",
        json={"scope": "topic", "text": "A's diary entry about cats"},
        headers=_hdr(TOK_A),
    )
    assert r.status_code == 200, r.text

    # B 拿同一原始 scope=topic 查 'cats' → 应拿空
    r = client.get(
        "/v1/memory/long",
        params={"scope": "topic", "query": "cats"},
        headers=_hdr(TOK_B),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == USER_B
    assert body["count"] == 0
    assert body["items"] == []

    # A 自己查 → 拿到 1 条
    r = client.get(
        "/v1/memory/long",
        params={"scope": "topic", "query": "cats"},
        headers=_hdr(TOK_A),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == USER_A
    assert body["count"] == 1
    assert "cats" in body["items"][0]["text"]


# -------- 额外:无 token_to_user 映射时,token 自身[:16] 当 user_id --------

def test_default_user_id_uses_token_prefix_when_no_mapping(deps, _clear_token_env):
    """没传 token_to_user 时,user_id = token[:16](同 token 同 user)。"""
    client = _make_client(deps, tokens=TOKENS, token_to_user=None)

    # token-A-...[:16] = "token-A-padded-t"
    expected_user = TOK_A[:16]
    assert expected_user == "token-A-padded-t"

    r = client.post(
        "/v1/memory/short",
        json={"scope": "S", "role": "user", "content": "x"},
        headers=_hdr(TOK_A),
    )
    assert r.status_code == 200
    assert r.json()["user_id"] == expected_user

    r = client.get("/v1/memory/short", params={"scope": "S"}, headers=_hdr(TOK_A))
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == expected_user
    assert body["count"] == 1
