"""Phase 8 端到端烟测:启 FastAPI + curl 各路由。

不需要真 LLM — 用 stub agent。覆盖:
  /healthz  /readyz  /metrics  /version
  /v1/chat  /v1/chat/stream
  /v1/sessions CRUD
  /v1/memory/short {get,post,delete}
  /v1/memory/long {add,recall}
  /v1/memory/soul {get,reload}
  /v1/tools {list,call,approver}
  /v1/skills {list,reload}
  /v1/channels {list,send}
  /ui/  静态页面
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ---- 准备 stub agent(同 test_phase8.py) ----

from openclaw.gateway import deps as deps_mod
from openclaw.gateway.routes.channels import set_channel_manager


class StubMsg:
    def __init__(self, role, content):
        self.role = role
        self.content = content


class StubShort:
    def __init__(self):
        self.store: dict[str, list[StubMsg]] = {}
    def all_scopes(self):
        return list(self.store.keys())
    def recent_messages(self, scope, k=20):
        return self.store.get(scope, [])[-k:]
    async def append_turn(self, scope, role, content, name=None, tool_call_id=None):
        self.store.setdefault(scope, []).append(StubMsg(role, content))
    def clear(self, scope):
        self.store.pop(scope, None)


class StubLong:
    def __init__(self):
        self._id = 0
        self.items = []
    def add(self, scope, text, metadata=None):
        self._id += 1
        it = type("X", (), {"id": self._id, "text": text, "metadata": metadata or {}, "score": 0.9})()
        self.items.append(it)
        return it.id
    def recall(self, scope, query, top_k=5):
        return [it for it in self.items if query in it.text][:top_k]


class StubSoul:
    def __init__(self):
        self.paths = [Path("/tmp/SOUL.md")]
        self.doc = "You are a helpful assistant."
    def render_system_prompt(self, base=""):
        return base + "\n\n[SOUL]\n" + self.doc
    def reload(self):
        return self.paths


class StubScoped:
    short = StubShort()
    long = StubLong()
    soul = StubSoul()


class StubSpec:
    def __init__(self, name, description, category, permission):
        self.name, self.description, self.category, self.permission = name, description, category, permission


class StubRegistry:
    specs = [StubSpec("get_time", "时间", "datetime", "SAFE"), StubSpec("shell_exec", "shell", "shell", "EXEC")]
    _approver = None
    def list_tools(self):
        return self.specs
    async def call(self, name, args):
        if name == "shell_exec" and self._approver is None:
            raise PermissionError("needs approval")
        return {"ok": True, "echo": f"{name}({args})"}
    def set_approver(self, fn):
        self._approver = fn


class StubAgentLoop:
    memory = StubScoped()
    tools = StubRegistry()
    system_prompt = "你是一个 OpenClaw 助手。"
    async def handle(self, session_id, text, **kw):
        class R:
            content = ""
            tool_calls = []
            iterations = 1
        r = R()
        r.content = f"[echo:{session_id}] {text}"
        return r
    async def new_session(self, sid=None):
        return sid or f"sess-{int(time.time()*1000)}"


class StubChannel:
    name = "echo"
    async def send(self, session_id, text):
        return f"sent: {text}"


class StubMgr:
    def channels(self):
        return [StubChannel()]


# ---- 装 deps ----

agent = StubAgentLoop()
deps_mod.reset_deps()
deps_mod.set_deps(deps_mod.GatewayDeps(agent_loop=agent, config_path=Path("/tmp/openclaw.yaml")))
set_channel_manager(StubMgr())

# ---- 启 uvicorn ----

import uvicorn

app = None
from openclaw.gateway.app import create_app
app = create_app(deps=deps_mod.get_deps())


def _run_server() -> None:
    cfg = uvicorn.Config(app, host="127.0.0.1", port=18181, log_level="warning", loop="asyncio")
    s = uvicorn.Server(cfg)
    s.run()


t = threading.Thread(target=_run_server, daemon=True)
t.start()
time.sleep(1.2)  # wait for server up

BASE = "http://127.0.0.1:18181"


def _check(label, ok):
    mark = "✅" if ok else "❌"
    print(f"  {mark} {label}")


# ---- 烟测 ----

print("\n=== Phase 8 Gateway 烟测 ===\n")

# 1. health
print("[1] health")
with httpx.Client(base_url=BASE, timeout=5.0) as c:
    r = c.get("/healthz")
    _check("/healthz → ok", r.status_code == 200 and r.json() == {"status": "ok"})
    r = c.get("/readyz")
    _check("/readyz → ready", r.status_code == 200 and r.json()["status"] == "ready")
    r = c.get("/metrics")
    _check("/metrics has uptime", "uptime_s" in r.json() and r.json()["agent_attached"] is True)
    r = c.get("/version")
    _check("/version", r.json()["openclaw_py"] == "0.1.0")

# 2. chat
print("\n[2] chat")
with httpx.Client(base_url=BASE, timeout=5.0) as c:
    r = c.post("/v1/chat", json={"session_id": "u1", "message": "hi"})
    _check("/v1/chat echo", r.status_code == 200 and "[echo:u1] hi" == r.json()["content"])

    # SSE 流
    with c.stream("POST", "/v1/chat/stream", json={"session_id": "u1", "message": "hello"}) as r:
        evts = []
        for line in r.iter_lines():
            if line and line.startswith("event:"):
                evts.append(line)
        _check("/v1/chat/stream events", "event: start" in "\n".join(evts) and "event: done" in "\n".join(evts))

# 3. sessions
print("\n[3] sessions")
with httpx.Client(base_url=BASE, timeout=5.0) as c:
    r = c.get("/v1/sessions")
    _check("/v1/sessions 空", r.json()["count"] == 0)
    r = c.post("/v1/sessions", json={})
    sid = r.json()["session_id"]
    _check(f"/v1/sessions POST new → {sid[:10]}...", r.json()["created"] is True)
    r = c.get(f"/v1/sessions/{sid}")
    _check("/v1/sessions/{id} empty list", r.json()["count"] == 0)
    r = c.delete(f"/v1/sessions/{sid}")
    _check("/v1/sessions/{id} DELETE", r.json()["cleared"] is True)

# 4. memory short
print("\n[4] memory/short")
with httpx.Client(base_url=BASE, timeout=5.0) as c:
    r = c.post("/v1/memory/short", json={"scope": "u9", "role": "user", "content": "manual"})
    _check("/v1/memory/short POST", r.status_code == 200)
    r = c.get("/v1/memory/short", params={"scope": "u9", "k": 5})
    _check("/v1/memory/short GET 1 条", r.json()["count"] == 1)
    r = c.delete("/v1/memory/short/u9")
    _check("/v1/memory/short DELETE", r.status_code == 200 and r.json()["ok"] is True)

# 5. memory long
print("\n[5] memory/long")
with httpx.Client(base_url=BASE, timeout=5.0) as c:
    r = c.post("/v1/memory/long", json={"scope": "u1", "text": "apple"})
    r2 = c.post("/v1/memory/long", json={"scope": "u1", "text": "banana"})
    _check("add 2 条", r.status_code == 200 and r2.status_code == 200)
    r = c.get("/v1/memory/long", params={"scope": "u1", "query": "apple"})
    _check("recall 命中 apple", r.json()["count"] == 1 and r.json()["items"][0]["text"] == "apple")

# 6. soul
print("\n[6] memory/soul")
with httpx.Client(base_url=BASE, timeout=5.0) as c:
    r = c.get("/v1/memory/soul")
    _check("/v1/memory/soul 渲染", "[SOUL]" in r.json()["rendered"])
    r = c.post("/v1/memory/soul/reload")
    _check("/v1/memory/soul/reload", r.json()["reloaded"] is True)

# 7. tools
print("\n[7] tools")
with httpx.Client(base_url=BASE, timeout=5.0) as c:
    r = c.get("/v1/tools")
    _check("/v1/tools 列出 2 个", r.json()["count"] == 2)
    r = c.post("/v1/tools/call", json={"name": "get_time", "arguments": {}})
    _check("/v1/tools/call safe 通过", r.status_code == 200 and r.json()["result"]["ok"] is True)
    r = c.post("/v1/tools/call", json={"name": "shell_exec", "arguments": {"cmd": "ls"}})
    _check("/v1/tools/call dangerous 409", r.status_code == 409)
    r = c.post("/v1/tools/approver", json={"approved": True})
    _check("/v1/tools/approver=True", r.status_code == 200)
    r = c.post("/v1/tools/call", json={"name": "shell_exec", "arguments": {"cmd": "ls"}})
    _check("/v1/tools/call 通过审批", r.status_code == 200)

# 8. channels
print("\n[8] channels")
with httpx.Client(base_url=BASE, timeout=5.0) as c:
    r = c.get("/v1/channels")
    _check("/v1/channels 列出 echo", r.json()["count"] == 1)
    r = c.post("/v1/channels/send", json={"name": "echo", "session_id": "u1", "text": "hi"})
    _check("/v1/channels/send echo", r.status_code == 200)

# 9. UI
print("\n[9] UI")
with httpx.Client(base_url=BASE, timeout=5.0) as c:
    r = c.get("/ui/")
    _check("/ui/ HTML 200", r.status_code == 200 and "OpenClaw Gateway" in r.text)
    r = c.get("/")
    _check("/ 根 JSON", r.json()["name"] == "openclaw-gateway" and r.json()["ui"] == "/ui/")

print("\n=== 烟测完成 ===")
