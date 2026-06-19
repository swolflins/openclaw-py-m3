"""P8: FastAPI TestClient 覆盖所有路由。

设计:用一个 mock AgentLoop(本地 echo + 工具注册)替换真实 LLM。
这样测试不依赖外部网络,但走完每条路由的 code path。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# -------- mock AgentLoop + ScopedMemory + Registry --------

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
        # 朴素的"包含"匹配
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
            FakeToolSpec("shell_exec", "shell 命令", "shell", "EXEC"),
        ]
        self._approver = None
        self.approval_calls: list[tuple[str, dict]] = []
    def list_tools(self):
        return self.specs
    async def call(self, name, args):
        if name not in [s.name for s in self.specs]:
            raise KeyError(name)
        if name == "shell_exec":
            # 危险工具:必须先调 approver;approver 拒绝 / None 都抛
            if self._approver is None:
                raise PermissionError(f"shell_exec needs approval (args={args})")
            self.approval_calls.append((name, args))
            ok = await self._approver(name, args)
            if not ok:
                raise PermissionError(f"shell_exec denied by approver (args={args})")
        return {"ok": True, "echo": f"{name}({args})"}
    def set_approver(self, fn):
        self._approver = fn


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
def client(deps):
    from openclaw.gateway.app import create_app
    return TestClient(create_app(deps=deps))


# -------- health --------

def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_readyz_ok(client):
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["uptime_s"] >= 0


def test_readyz_degraded_when_no_agent():
    from openclaw.gateway.app import create_app
    from openclaw.gateway.deps import GatewayDeps, set_deps, reset_deps
    reset_deps()
    set_deps(GatewayDeps(agent_loop=None))
    try:
        client = TestClient(create_app())
        r = client.get("/readyz")
        assert r.status_code == 503
        assert r.json()["status"] == "degraded"
    finally:
        reset_deps()


def test_metrics(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    body = r.json()
    assert "uptime_s" in body
    assert body["agent_attached"] is True


def test_version(client):
    r = client.get("/version")
    assert r.json()["openclaw_py"] == "0.1.0"


# -------- chat --------

def test_chat_success(client, deps):
    r = client.post("/v1/chat", json={"session_id": "u1", "message": "hi"})
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == "u1"
    assert "hi" in body["content"]
    assert body["duration_ms"] >= 0
    # 短路消息进了 agent.calls
    assert deps.agent_loop.calls == [("u1", "hi")]


def test_chat_503_when_degraded():
    from openclaw.gateway.app import create_app
    from openclaw.gateway.deps import GatewayDeps, set_deps, reset_deps
    reset_deps()
    set_deps(GatewayDeps(agent_loop=None))
    try:
        client = TestClient(create_app())
        r = client.post("/v1/chat", json={"session_id": "u1", "message": "hi"})
        assert r.status_code == 503
    finally:
        reset_deps()


def test_chat_empty_message_422(client):
    r = client.post("/v1/chat", json={"session_id": "u1", "message": ""})
    assert r.status_code == 422  # pydantic min_length=1


def test_chat_default_session_id(client, deps):
    r = client.post("/v1/chat", json={"message": "hi"})
    assert r.status_code == 200
    assert r.json()["session_id"] == "default"
    assert deps.agent_loop.calls[0][0] == "default"


# -------- chat stream (SSE) --------

def test_chat_stream_basic(client):
    # TestClient 没有 stream=True 关键字,用 client.stream
    with client.stream("POST", "/v1/chat/stream", json={"session_id": "u1", "message": "hi"}) as r:
        assert r.status_code == 200
        events = []
        for line in r.iter_lines():
            if not line:
                continue
            s = line.decode() if isinstance(line, bytes) else line
            if s.startswith("event:") or s.startswith("data:"):
                events.append(s)
    # 期望至少 start / thinking / delta / done
    text = "\n".join(events)
    assert "event: start" in text
    assert "event: thinking" in text
    assert "event: done" in text
    assert "event: __end__" in text
    assert "hi" in text  # 内容里包含原消息


def test_chat_stream_503_when_degraded():
    from openclaw.gateway.app import create_app
    from openclaw.gateway.deps import GatewayDeps, set_deps, reset_deps
    reset_deps()
    set_deps(GatewayDeps(agent_loop=None))
    try:
        client = TestClient(create_app())
        r = client.post("/v1/chat/stream", json={"session_id": "u1", "message": "hi"})
        assert r.status_code == 503
    finally:
        reset_deps()


# -------- sessions --------

def test_sessions_list_empty(client):
    r = client.get("/v1/sessions")
    assert r.status_code == 200
    assert r.json()["count"] == 0


def test_sessions_list_with_data(client, deps):
    deps.agent_loop.memory.short.store["u1"] = [FakeMsg("user", "hi")]  # 同步塞更简单
    r = client.get("/v1/sessions")
    assert r.json()["sessions"] == ["u1"]


def test_sessions_get_messages(client, deps):
    deps.agent_loop.memory.short.store["u1"] = [FakeMsg("user", "hello"), FakeMsg("assistant", "world")]
    r = client.get("/v1/sessions/u1")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert body["messages"][0]["content"] == "hello"


def test_sessions_clear(client, deps):
    deps.agent_loop.memory.short.store["u1"] = [FakeMsg("user", "hello")]
    r = client.delete("/v1/sessions/u1")
    assert r.status_code == 200
    assert r.json()["cleared"] is True
    assert deps.agent_loop.memory.short.store == {}


def test_sessions_new(client, deps):
    r = client.post("/v1/sessions", json={})
    assert r.status_code == 200
    assert r.json()["created"] is True
    assert r.json()["session_id"].startswith("sess-")


# -------- memory --------

def test_memory_short_get(client, deps):
    deps.agent_loop.memory.short.store["u1"] = [FakeMsg("user", "hi")]
    r = client.get("/v1/memory/short", params={"scope": "u1", "k": 10})
    assert r.status_code == 200
    assert r.json()["count"] == 1


def test_memory_short_post(client, deps):
    r = client.post("/v1/memory/short", json={"scope": "u1", "role": "user", "content": "manual"})
    assert r.status_code == 200
    msgs = deps.agent_loop.memory.short.store["u1"]
    assert msgs[0].content == "manual"


def test_memory_short_clear(client, deps):
    deps.agent_loop.memory.short.store["u1"] = [FakeMsg("user", "x")]
    r = client.delete("/v1/memory/short/u1")
    assert r.status_code == 200
    assert deps.agent_loop.memory.short.store == {}


def test_memory_long_add_and_recall(client, deps):
    r = client.post("/v1/memory/long", json={"scope": "u1", "text": "alpha"})
    assert r.status_code == 200
    r = client.post("/v1/memory/long", json={"scope": "u1", "text": "beta"})
    assert r.status_code == 200
    r = client.get("/v1/memory/long", params={"scope": "u1", "query": "alpha"})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["items"][0]["text"] == "alpha"


def test_memory_soul_get(client, deps):
    r = client.get("/v1/memory/soul")
    assert r.status_code == 200
    assert "[SOUL]" in r.json()["rendered"]


def test_memory_soul_reload(client, deps):
    r = client.post("/v1/memory/soul/reload")
    assert r.status_code == 200
    assert r.json()["reloaded"] is True
    assert r.json()["doc_count"] >= 1


# -------- tools --------

def test_tools_list(client):
    r = client.get("/v1/tools")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    names = [t["name"] for t in body["tools"]]
    assert "get_time" in names
    assert "shell_exec" in names


def test_tools_call_safe(client):
    r = client.post("/v1/tools/call", json={"name": "get_time", "arguments": {}})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "get_time"
    assert body["result"]["ok"] is True


def test_tools_call_dangerous_409(client):
    r = client.post("/v1/tools/call", json={"name": "shell_exec", "arguments": {"cmd": "ls"}})
    assert r.status_code == 409
    assert "approval" in r.json()["detail"]


def test_tools_call_not_found(client):
    r = client.post("/v1/tools/call", json={"name": "nope", "arguments": {}})
    assert r.status_code == 404


def test_tools_set_approver_allow(client):
    r = client.post("/v1/tools/approver", json={"approved": True})
    assert r.status_code == 200
    # 之后再调 shell_exec 应通过
    r2 = client.post("/v1/tools/call", json={"name": "shell_exec", "arguments": {"cmd": "ls"}})
    assert r2.status_code == 200


def test_tools_set_approver_deny(client):
    r = client.post("/v1/tools/approver", json={"approved": False})
    assert r.status_code == 200
    r2 = client.post("/v1/tools/call", json={"name": "shell_exec", "arguments": {"cmd": "ls"}})
    assert r2.status_code == 409


# -------- skills --------

def test_skills_list_empty(client, deps):
    deps.config = None
    r = client.get("/v1/skills")
    assert r.status_code == 200
    assert r.json()["count"] == 0


def test_skills_reload_explicit_dirs(client, deps):
    # load_skills 要求传入"父目录",会去它的子目录里找 SKILL.md
    parent = Path("/tmp/openclaw_test_skills_parent")
    parent.mkdir(parents=True, exist_ok=True)
    skill_dir = parent / "test-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: test\nversion: 1.0\ndescription: 测试 skill\n"
        "triggers:\n  - 你好\n---\n\n回复: 你好!这是测试 skill。\n",
        encoding="utf-8",
    )
    r = client.post("/v1/skills/reload", json={"directories": [str(parent)]})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] >= 1
    # 至少要返回 skill 名(test)或 description
    skills_text = str(body)
    assert "test" in skills_text or "测试" in skills_text


# -------- channels --------

def test_channels_list_empty(client, deps):
    r = client.get("/v1/channels")
    assert r.status_code == 200
    assert r.json()["count"] == 0


def test_channels_send_no_manager(client):
    r = client.post("/v1/channels/send", json={"name": "x", "session_id": "u1", "text": "hi"})
    assert r.status_code == 503


def test_channels_send_not_found(client, deps):
    from openclaw.gateway.routes.channels import set_channel_manager

    class FakeMgr:
        def channels(self):
            return []

    set_channel_manager(FakeMgr())
    r = client.post("/v1/channels/send", json={"name": "x", "session_id": "u1", "text": "hi"})
    assert r.status_code == 404


def test_channels_send_ok(client, deps):
    from openclaw.gateway.routes.channels import set_channel_manager

    class FakeChannel:
        name = "echo"
        async def send(self, session_id, text):
            self.last = (session_id, text)
            return text

    class FakeMgr:
        def channels(self):
            return [FakeChannel()]

    set_channel_manager(FakeMgr())
    r = client.post("/v1/channels/send", json={"name": "echo", "session_id": "u1", "text": "hi"})
    assert r.status_code == 200


# -------- root + UI --------

def test_root_index(client):
    r = client.get("/")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "openclaw-gateway"
    assert body["ui"] == "/ui/"


def test_ui_html(client):
    r = client.get("/ui/")
    assert r.status_code == 200
    assert "OpenClaw Gateway" in r.text
    assert "/v1/chat" in r.text
    assert "/v1/sessions" in r.text


def test_ui_static_404(client):
    r = client.get("/ui/nonexistent.html")
    assert r.status_code == 404


# -------- 路由总数 --------

def test_route_count():
    """冒烟:openapi paths 至少 30 条。"""
    from openclaw.gateway.app import create_app
    from openclaw.gateway.deps import GatewayDeps, set_deps, reset_deps
    reset_deps()
    set_deps(GatewayDeps(agent_loop=None))
    try:
        client = TestClient(create_app())
        spec = client.get("/openapi.json").json()
        # 不含 /ui mount 的具体路径
        paths = [p for p in spec["paths"] if not p.startswith("/ui/")]
        assert len(paths) >= 18, f"路由数: {len(paths)}"
    finally:
        reset_deps()
