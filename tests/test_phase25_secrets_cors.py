"""P25 / b9:三件安全/体验修复的端到端测试。

覆盖:
1. **api_key 改 SecretStr** — ``OpenClawConfig`` / ``ProviderConfig`` / ``LarkSettings`` / ``OpenAISettings``
   内部存的是 ``SecretStr``,``repr`` / ``str`` / ``model_dump`` 默认不泄漏明文,
   只能 ``.get_secret_value()`` 取原值。

2. **CORS 中间件** — dev 模式默认允许 ``http://localhost:*`` + ``http://127.0.0.1:*``,
   生产模式 ``allow_origins=[]`` 直接拒跨域。

3. **prod 模式关 docs** — ``create_app`` 在 ``is_production_mode()`` 为真时把
   ``app.docs_url / redoc_url / openapi_url`` 置 ``None``,``/docs`` ``/redoc``
   ``/openapi.json`` 全部 404。
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# ─────────────────────────── 1) SecretStr ───────────────────────────


def test_api_key_not_in_repr():
    """Phase 25/b9: ``OpenClawConfig`` 内的 ``api_key`` 不应被 repr/str 泄漏明文。

    用 ``ProviderConfig(openai_compat, model=gpt-4o-mini, api_key="sk-secret-xxx")``
    构造,repr 出来**不能**包含 "sk-secret-xxx",但 ``.get_secret_value()`` 仍能拿到原值。
    """
    from openclaw.core.config import OpenClawConfig, ProviderConfig

    cfg = OpenClawConfig(
        providers=[
            ProviderConfig(
                name="openai_compat",
                model="gpt-4o-mini",
                api_key="sk-secret-XXX-do-not-leak",
            ),
        ],
    )

    r = repr(cfg)
    s = str(cfg)
    assert "sk-secret-XXX-do-not-leak" not in r, (
        f"OpenClawConfig.__repr__ leaked api_key: {r!r}"
    )
    assert "sk-secret-XXX-do-not-leak" not in s, (
        f"OpenClawConfig.__str__ leaked api_key: {s!r}"
    )

    # 但 .get_secret_value() 仍要能拿到原值(否则业务侧没法用)
    pv = cfg.providers[0].api_key.get_secret_value()
    assert pv == "sk-secret-XXX-do-not-leak"

    # ProviderConfig 单飞也要有同样保护
    pr = repr(cfg.providers[0])
    assert "sk-secret-XXX-do-not-leak" not in pr, (
        f"ProviderConfig.__repr__ leaked api_key: {pr!r}"
    )
    ps = str(cfg.providers[0])
    assert "sk-secret-XXX-do-not-leak" not in ps


def test_lark_app_secret_not_in_repr():
    """Phase 25/b9: ``LarkSettings.app_secret`` 同样应被 SecretStr 保护。"""
    from openclaw.config.settings import LarkSettings

    s = LarkSettings(app_id="cli_xxx", app_secret="lark-secret-XXX-do-not-leak")
    r = repr(s)
    assert "lark-secret-XXX-do-not-leak" not in r, (
        f"LarkSettings.__repr__ leaked app_secret: {r!r}"
    )
    # 取值仍要工作
    assert s.app_secret.get_secret_value() == "lark-secret-XXX-do-not-leak"


def test_openai_settings_api_key_not_in_repr():
    """Phase 25/b9: ``OpenAISettings.api_key`` 同样应被 SecretStr 保护。"""
    from openclaw.config.settings import OpenAISettings

    s = OpenAISettings(api_key="sk-openai-secret-XXX")
    r = repr(s)
    assert "sk-openai-secret-XXX" not in r, (
        f"OpenAISettings.__repr__ leaked api_key: {r!r}"
    )
    assert s.api_key.get_secret_value() == "sk-openai-secret-XXX"


def test_provider_factory_uses_get_secret_value():
    """``ProviderFactory`` 拿到 ``ProviderConfig(api_key=SecretStr)`` 后必须能正确
    传给底层 provider;验证工厂构造的 OpenAICompatProvider.api_key 与原 secret 一致。
    """
    from openclaw.core.config import ProviderConfig
    from openclaw.providers.factory import ProviderFactory

    f = ProviderFactory()
    p = f.build(
        ProviderConfig(
            name="openai_compat",
            model="gpt-4o-mini",
            api_key="sk-factory-XXX",
        )
    )
    # api_key 真的传到底层 provider
    assert p.api_key == "sk-factory-XXX"


# ─────────────────────────── mock agent (与 phase25_auth_failfast 一致) ───────────────────────────


class FakeMsg:
    def __init__(self, role: str, content: str):
        self.role = role
        self.content = content


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
        self.specs = [FakeToolSpec("get_time", "获取当前时间", "datetime", "SAFE")]

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
def _dev_mode(monkeypatch):
    """清掉会触发 prod 模式 / 强制 token 的 env,确保 create_app 走 dev 分支。"""
    monkeypatch.delenv("OPENCLAW_GATEWAY_ENV", raising=False)
    monkeypatch.delenv("OPENCLAW_GATEWAY_HOST", raising=False)
    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("OPENCLAW_CORS_ORIGINS", raising=False)


# ─────────────────────────── 2) CORS ───────────────────────────


def test_cors_allows_localhost_in_dev(deps, _dev_mode):
    """dev 模式 + localhost origin → 200 + 正确的 CORS 头。"""
    from openclaw.gateway.app import create_app

    app = create_app(deps=deps, host="127.0.0.1")
    client = TestClient(app)

    # 带 Origin 头的健康检查
    r = client.get("/healthz", headers={"Origin": "http://localhost:3000"})
    assert r.status_code == 200
    # CORSMiddleware 应把 Origin 回显
    assert r.headers.get("access-control-allow-origin") in {
        "http://localhost:3000",  # echo
        "*",
    }
    # 不同端口 127.0.0.1:5173 也应被放行(走 origin regex 兜底)
    r2 = client.get("/healthz", headers={"Origin": "http://127.0.0.1:5173"})
    assert r2.status_code == 200
    assert r2.headers.get("access-control-allow-origin") in {
        "http://127.0.0.1:5173",
        "*",
    }


def test_cors_disallowed_origin_in_dev_returns_no_allow_origin(deps, _dev_mode):
    """dev 模式 + 非白名单 origin → 请求仍可达(200),但 CORS 头不会 echo 该 origin。

    注:浏览器 CORS 的"403 拒绝"是在客户端判断的 — 服务端只负责不返回 ``allow-origin``。
    starlette CORSMiddleware 对非白名单 origin 不会主动返 403,
    我们只需验证:echo 不出现 + allow-origin 不匹配这个 origin。
    """
    from openclaw.gateway.app import create_app

    app = create_app(deps=deps, host="127.0.0.1")
    client = TestClient(app)

    r = client.get("/healthz", headers={"Origin": "https://evil.example.com"})
    # 请求本身能通(starlette 不会主动 403)
    assert r.status_code == 200
    # 但 access-control-allow-origin 不能是 evil origin(浏览器会拒绝)
    assert r.headers.get("access-control-allow-origin") != "https://evil.example.com"


def test_cors_disallowed_in_prod(deps, monkeypatch):
    """prod 模式 + 任意 origin → 不会有 ``access-control-allow-origin`` 头,浏览器拒绝。

    prod 启动要求 token,先设上;然后校验 CORS 头为空。
    """
    # Phase 27 / M9:prod + dev=1 是矛盾,清掉 conftest autouse 注入
    monkeypatch.delenv("OPENCLAW_GATEWAY_DEV", raising=False)
    monkeypatch.setenv("OPENCLAW_GATEWAY_ENV", "production")
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "phase25-cors-prod-test-32chars-please-ignore")
    # Phase 27 / H3:prod 模式必须有 user_id 或 token_to_user 之一
    monkeypatch.setenv("OPENCLAW_GATEWAY_USER_ID", "tester")
    from openclaw.gateway.app import create_app

    app = create_app(deps=deps, host="127.0.0.1")
    client = TestClient(app)

    r = client.get("/healthz", headers={"Origin": "http://localhost:3000"})
    # 请求可达
    assert r.status_code == 200
    # 但 prod 模式 allow_origins=[] → 不会有 allow-origin 头(浏览器判定 CORS 失败)
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


def test_cors_explicit_origins_env_var(deps, _dev_mode, monkeypatch):
    """``OPENCLAW_CORS_ORIGINS`` 应注入到 allow_origins,允许额外 origin。"""
    monkeypatch.setenv("OPENCLAW_CORS_ORIGINS", "https://app.example.com, https://admin.example.com")
    from openclaw.gateway.app import create_app

    app = create_app(deps=deps, host="127.0.0.1")
    client = TestClient(app)

    r = client.get("/healthz", headers={"Origin": "https://app.example.com"})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") in {
        "https://app.example.com",
        "*",
    }


# ─────────────────────────── 3) prod docs 关闭 ───────────────────────────


def test_docs_disabled_in_production(deps, monkeypatch):
    """prod 模式 GET /docs → 404(``app.docs_url=None``),``/redoc`` / ``/openapi.json`` 同理。"""
    # Phase 27 / M9:prod + dev=1 是矛盾,清掉 conftest autouse 注入
    monkeypatch.delenv("OPENCLAW_GATEWAY_DEV", raising=False)
    monkeypatch.setenv("OPENCLAW_GATEWAY_ENV", "production")
    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", "phase25-docs-prod-test-32chars-please-ignore")
    # Phase 27 / H3:prod 模式必须有 user_id 或 token_to_user 之一(防 token 轮换换 user)
    monkeypatch.setenv("OPENCLAW_GATEWAY_USER_ID", "tester")
    from openclaw.gateway.app import create_app

    app = create_app(deps=deps, host="127.0.0.1")
    # 三个 doc 端点的 url 必须为 None
    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None

    client = TestClient(app)
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_docs_enabled_in_dev(deps, _dev_mode):
    """dev 模式 /docs /redoc /openapi.json 应可达(默认行为不被破坏)。"""
    from openclaw.gateway.app import create_app

    app = create_app(deps=deps, host="127.0.0.1")
    assert app.docs_url == "/docs"
    assert app.redoc_url == "/redoc"
    assert app.openapi_url == "/openapi.json"

    client = TestClient(app)
    # dev 模式无 token,/docs 是公开路径,直接 200
    r = client.get("/docs")
    assert r.status_code == 200
    # openapi.json 返回 dict(JSON)
    r2 = client.get("/openapi.json")
    assert r2.status_code == 200
    assert "openapi" in r2.json()
