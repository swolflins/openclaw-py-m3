"""Phase 25 review follow-up — 第二轮代码审查报告里点名的 P1 残留风险修复的回归测试。

覆盖 5 项修复(对应审查报告 §04 PARTIAL + §05 4 项新 P1 + §07 roadmap):
1. ``agent/journal.py`` ``reflect`` 死代码 + 占位段 N+1 重复(§04 PARTIAL / §07-1)
2. ``memory/short_term.py`` ``_backup`` 锁重入死锁(§05-2 / §07-3)
3. ``gateway/app.py`` CORS origins 无格式校验(§05-3 / §07-4)
4. ``gateway/auth.py`` token 轮换 → user_id 蒸发(§05-4 / §07-5)
5. ``tools/builtin/docker.py`` ``docker.from_env()`` 4 处无 close(§07-6)

注:报告 §05-1 "Lark app_secret 明文 dump" 经核实为**误报** —— lark.py:407
是飞书 ``tenant_access_token`` API 调用(必须用明文 secret),不是 dump/log
路径;仓库里不存在 ``_env_dict`` 方法,repr 已被 SecretStr 保护(见
``test_phase25_secrets_cors.py::test_lark_app_secret_not_in_repr``)。若按报告
建议把 ``get_secret_value()`` 换成 SecretStr 对象,会直接破坏飞书鉴权,故不改。
"""
from __future__ import annotations

import asyncio
import contextlib
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent


# C1 修复后,requires_approval 工具在无 approver 时 fail-closed。
# 测试中需要一个 always-approve 的 approver。
def _set_test_approver(reg: ToolRegistry) -> None:
    async def _ok(name, args):
        return True
    reg.set_approver(_ok)


# ────────────────────────────────────────────────────────────────
# 1. AgentJournal.reflect — 占位段不再 N+1 重复 + seen 真去重
# ────────────────────────────────────────────────────────────────


@dataclass
class _FakeResponse:
    content: str
    iterations: int
    tool_calls: list
    session_id: str


class _StaticReflector:
    """固定返回同一段反思,模拟 LLM 重复调用 / 重复触发。"""

    def __init__(self, text: str) -> None:
        self.text = text

    async def reflect(self, entry) -> str:  # noqa: ANN001  # H4: async
        return self.text


def test_journal_reflect_placeholder_appears_once_after_repeats(tmp_path: Path):
    """``reflect`` 多次调用后,占位段 ``<!-- 反思将追加在下方 -->`` 只出现一次。

    修复前:``tail = "---" + existing.split("---", 1)[1]`` 把已落盘的 ``---`` +
    占位段一起拼回去,head 自身又带一份占位段 → 每调一次 reflect 多一份占位段
    (报告 §04 "N+1 份占位段")。修复后:占位段只随 head 生成一次。
    """
    from openclaw.agent.journal import AgentJournal

    j = AgentJournal(root=tmp_path / "j", reflector=_StaticReflector("# 反思 A\n\n内容A"))
    e = j.record_session(
        session_id="sess_placeholder",
        user_message="hi",
        response=_FakeResponse("reply", 1, [], "sess_placeholder"),
    )
    for _ in range(4):
        asyncio.run(j.reflect(e))

    files = list((tmp_path / "j").rglob("sess_*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert text.count("<!-- 反思将追加在下方 -->") == 1, (
        f"占位段应只出现 1 次,实际 {text.count('<!-- 反思将追加在下方 -->')} 次\n"
        f"--- file ---\n{text}\n--- end ---"
    )
    # "---" 分隔行(head 与反思区之间)也只应有一处独立分隔行
    assert text.count("\n---\n") == 1


def test_journal_reflect_seen_set_actually_dedupes(tmp_path: Path):
    """``seen`` set 真正参与去重:同一反思反复调只落盘一份。

    修复前:``for existing_refl in entry.reflections: seen.add(...)`` 是死代码
    (只填 seen 不读取),去重全靠循环外的 ``if reflection not in tail``。修复后:
    seen 对 "已落盘反思 + 本次新反思" 统一去重,保序。
    """
    from openclaw.agent.journal import AgentJournal

    fixed = "# 固定反思\n\n这是一段不变化的反思内容。"
    j = AgentJournal(root=tmp_path / "j", reflector=_StaticReflector(fixed))
    e = j.record_session(
        session_id="sess_dedup",
        user_message="hi",
        response=_FakeResponse("reply", 1, [], "sess_dedup"),
    )
    for _ in range(5):
        asyncio.run(j.reflect(e))

    text = list((tmp_path / "j").rglob("sess_*.md"))[0].read_text(encoding="utf-8")
    assert text.count(fixed) == 1, f"反思应去重为 1 份,实际 {text.count(fixed)} 份"


def test_journal_reflect_keeps_distinct_reflections_in_order(tmp_path: Path):
    """多次 reflect 写入**不同**反思时,按写入顺序全部保留(去重不影响新内容)。"""
    from openclaw.agent.journal import AgentJournal

    texts = ["# 反思 一\n\n第一个", "# 反思 二\n\n第二个", "# 反思 三\n\n第三个"]
    seq = iter(texts)

    class _SeqReflector:
        async def reflect(self, entry):  # noqa: ANN001  # H4: async
            return next(seq)

    j = AgentJournal(root=tmp_path / "j", reflector=_SeqReflector())
    e = j.record_session(
        session_id="sess_order",
        user_message="hi",
        response=_FakeResponse("reply", 1, [], "sess_order"),
    )
    for _ in texts:
        asyncio.run(j.reflect(e))

    text = list((tmp_path / "j").rglob("sess_*.md"))[0].read_text(encoding="utf-8")
    # 三段都保留,且顺序与写入一致
    pos = [text.index(t) for t in texts]
    assert pos == sorted(pos), f"反思顺序错乱: {pos}"
    for t in texts:
        assert text.count(t) == 1


def test_journal_extract_reflections_skips_placeholder_and_separator():
    """``_extract_reflections`` 跳过 ``---`` 分隔行与占位段,只抽真正反思块。"""
    from openclaw.agent.journal import AgentJournal

    text = (
        "# Session x\n\n## 工具调用\n\n_(无)_\n\n---\n\n"
        "<!-- 反思将追加在下方 -->\n"
        "---\n<!-- 反思将追加在下方 -->\n"  # 旧 bug 残留的重复占位段
        "# 反思 一\n\n内容一\n\n"
        "# 反思 二\n\n内容二\n"
    )
    blocks = AgentJournal._extract_reflections(text)
    assert blocks == ["# 反思 一\n\n内容一", "# 反思 二\n\n内容二"]
    # 没有反思块时返回空
    assert AgentJournal._extract_reflections("# head\n\n---\n\n<!-- 反思将追加在下方 -->\n") == []
    assert AgentJournal._extract_reflections("") == []


# ────────────────────────────────────────────────────────────────
# 2. ShortTermStore — RLock 可重入,_backup 在锁内不死锁
# ────────────────────────────────────────────────────────────────


def test_short_term_store_uses_rlock():
    """``ShortTermStore._lock`` 必须是 RLock(可重入),而非 Lock。"""
    import threading

    from openclaw.memory.short_term import ShortTermStore

    with tempfile_dir() as d:
        s = ShortTermStore(d)
        assert isinstance(s._lock, type(threading.RLock())), (
            f"_lock 应为 RLock,实际 {type(s._lock)}"
        )


def test_short_term_backup_inside_lock_no_deadlock():
    """``_backup`` 移入锁内 + ``recent()`` 二次获取锁 → RLock 下不死锁。

    修复前若用不可重入 Lock 且 _backup 在锁内,会死锁;RLock 允许同线程重入。
    本测试同时验证:多线程并发 append 不会卡死(超时即视为死锁失败)。
    """
    from openclaw.memory.short_term import ShortTermStore

    with tempfile_dir() as d:
        s = ShortTermStore(d)

        def worker(n: int) -> None:
            for i in range(20):
                s.append(f"scope-{n % 3}", f"u{n}-{i}", f"a{n}-{i}")

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)  # 死锁会触发 timeout → t.is_alive() True
        assert not any(t.is_alive() for t in threads), "并发 append 死锁(超时未结束)"

        # 数据完整性:每个 scope 的 turn 数 = (线程数覆盖该 scope) * 20 * 2(user+assistant)
        scopes = s.all_scopes()
        assert len(scopes) == 3
        for sc in scopes:
            msgs = s.recent(sc, k=10000)
            # 8 线程按 n%3 分到 3 个 scope;每个 scope 被 ~2-3 个线程写,每线程 20 轮 * 2 条
            assert len(msgs) > 0 and len(msgs) % 2 == 0, f"scope {sc} turn 数异常: {len(msgs)}"


def test_short_term_backup_atomic_with_write():
    """``_backup`` 在锁内 → 写 + 备份是原子快照,备份文件反映已提交的 turns。"""
    import json

    from openclaw.memory.short_term import ShortTermStore, _safe_scope_name

    with tempfile_dir() as d:
        s = ShortTermStore(d)
        s.append("sc", "u1", "a1")
        s.append("sc", "u2", "a2")
        backup = json.loads((d / f"{_safe_scope_name('sc')}.json").read_text())
        assert len(backup) == 4  # 2 轮 * (user + assistant)
        roles = [m["role"] for m in backup]
        assert roles == ["user", "assistant", "user", "assistant"]


# ────────────────────────────────────────────────────────────────
# 3. CORS origins 格式校验 — 拒绝 '*' / 非法 origin(启动期 fail-fast)
# ────────────────────────────────────────────────────────────────


def test_validate_cors_origin_accepts_valid():
    from openclaw.gateway.app import _validate_cors_origin

    for o in [
        "https://app.example.com",
        "http://localhost:3000",
        "https://admin.example.com",
        "http://127.0.0.1",
        "http://localhost",
        "https://api.foo.bar:8080",
    ]:
        assert _validate_cors_origin(o) == o


@pytest.mark.parametrize("bad", [
    "*",
    "https://*.com",
    "https://*",
    "not-a-url",
    "ftp://x.com",
    "http://",
    "",
    "evil.com",
    "http//x",
    "javascript:alert(1)",
])
def test_validate_cors_origin_rejects_invalid(bad):
    from openclaw.gateway.app import _validate_cors_origin

    with pytest.raises(ValueError):
        _validate_cors_origin(bad)


def test_resolve_cors_origins_raises_on_wildcard_env(monkeypatch):
    """``OPENCLAW_CORS_ORIGINS='*'`` → ``_resolve_cors_origins`` 启动期抛错(fail-fast)。"""
    from openclaw.gateway.app import _resolve_cors_origins

    monkeypatch.delenv("OPENCLAW_GATEWAY_ENV", raising=False)
    monkeypatch.setenv("OPENCLAW_CORS_ORIGINS", "*")
    with pytest.raises(ValueError):
        _resolve_cors_origins()


def test_resolve_cors_origins_raises_on_malformed_env(monkeypatch):
    """``OPENCLAW_CORS_ORIGINS`` 含非法 origin → 启动期抛错,不静默放行。"""
    from openclaw.gateway.app import _resolve_cors_origins

    monkeypatch.delenv("OPENCLAW_GATEWAY_ENV", raising=False)
    monkeypatch.setenv("OPENCLAW_CORS_ORIGINS", "https://ok.com, not-a-url")
    with pytest.raises(ValueError):
        _resolve_cors_origins()


def test_resolve_cors_origins_accepts_valid_env(monkeypatch):
    """合法 origin 列表 → 正常返回(不破坏已有 dev 行为)。"""
    from openclaw.gateway.app import _resolve_cors_origins

    monkeypatch.delenv("OPENCLAW_GATEWAY_ENV", raising=False)
    monkeypatch.setenv("OPENCLAW_CORS_ORIGINS", "https://app.example.com, https://admin.example.com")
    origins = _resolve_cors_origins()
    assert "https://app.example.com" in origins
    assert "https://admin.example.com" in origins


def test_create_app_fails_fast_on_wildcard_cors_env(monkeypatch, tmp_path):
    """端到端:dev 模式下 ``OPENCLAW_CORS_ORIGINS='*'`` → ``create_app`` 启动抛错。"""
    from openclaw.gateway import deps as deps_mod
    from openclaw.gateway.app import create_app

    monkeypatch.delenv("OPENCLAW_GATEWAY_ENV", raising=False)
    monkeypatch.delenv("OPENCLAW_GATEWAY_HOST", raising=False)
    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
    monkeypatch.setenv("OPENCLAW_CORS_ORIGINS", "*")

    deps_mod.reset_deps()
    d = deps_mod.GatewayDeps(agent_loop=None, config_path=tmp_path / "x.yaml")
    deps_mod.set_deps(d)
    try:
        with pytest.raises(ValueError):
            create_app(deps=d, host="127.0.0.1", rate_limiter=None)
    finally:
        deps_mod.reset_deps()


# ────────────────────────────────────────────────────────────────
# 4. token 轮换 → user_id 稳定(OPENCLAW_GATEWAY_USER_ID / _TOKEN_TO_USER)
# ────────────────────────────────────────────────────────────────


TOK_OLD = "oldtoken-padded-32-chars-aa"
TOK_NEW = "newtoken-padded-32-chars-bb"


def test_resolve_user_id_priority():
    """``_resolve_user_id`` 优先级:user_id(稳定)> token_to_user 映射 > sha256(token)[:16] fallback。

    L5 修复:fallback 从 token[:16] 改为 "h_"+sha256(token)[:16],不泄露原始 token。
    """
    import hashlib
    from openclaw.gateway.auth import AuthMiddleware

    R = AuthMiddleware._resolve_user_id
    # 1) 稳定 user_id 优先(即使有映射)
    assert R("any-token", {"any-token": "bob"}, "alice") == "alice"
    # 2) 无 user_id 时走映射
    assert R("t1", {"t1": "alice"}, None) == "alice"
    # 3) 都没有 → sha256(token)[:16] fallback (L5 修复)
    expected = "h_" + hashlib.sha256("token-A-padded-to-32".encode()).hexdigest()[:16]
    assert R("token-A-padded-to-32", {}, None) == expected
    # 4) 无 token → anonymous
    assert R(None, {}, None) == "anonymous"


def test_token_rotation_keeps_user_id_with_gateway_user_id_env(monkeypatch):
    """设了 ``OPENCLAW_GATEWAY_USER_ID`` → token 轮换后 user_id 不变(per-user 隔离不蒸发)。"""
    from openclaw.gateway.auth import install_auth

    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", f"{TOK_OLD},{TOK_NEW}")
    monkeypatch.setenv("OPENCLAW_GATEWAY_USER_ID", "alice")

    app = FastAPI()

    @app.get("/v1/whoami")
    async def whoami(request: Request):
        return {"user_id": request.state.user_id}

    install_auth(app, host="127.0.0.1")
    client = TestClient(app)

    r_old = client.get("/v1/whoami", headers={"Authorization": f"Bearer {TOK_OLD}"})
    r_new = client.get("/v1/whoami", headers={"Authorization": f"Bearer {TOK_NEW}"})
    assert r_old.status_code == 200 and r_new.status_code == 200
    assert r_old.json()["user_id"] == r_new.json()["user_id"] == "alice"


def test_token_rotation_without_user_id_env_changes_user_id(monkeypatch):
    """没设 ``OPENCLAW_GATEWAY_USER_ID`` → token 轮换后 user_id 变化(回退到 sha256(token)[:16],不稳定)。

    这是修复**保留**的向后兼容行为,但生产应避免(启动期会 warning)。
    L5 修复:user_id 从 token[:16] 改为 "h_"+sha256(token)[:16],不泄露原始 token。
    """
    import hashlib
    from openclaw.gateway.auth import install_auth

    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", f"{TOK_OLD},{TOK_NEW}")
    monkeypatch.delenv("OPENCLAW_GATEWAY_USER_ID", raising=False)
    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN_TO_USER", raising=False)

    app = FastAPI()

    @app.get("/v1/whoami")
    async def whoami(request: Request):
        return {"user_id": request.state.user_id}

    install_auth(app, host="127.0.0.1")
    client = TestClient(app)

    r_old = client.get("/v1/whoami", headers={"Authorization": f"Bearer {TOK_OLD}"})
    r_new = client.get("/v1/whoami", headers={"Authorization": f"Bearer {TOK_NEW}"})
    # L5 修复:user_id = "h_" + sha256(token)[:16]
    expected_old = "h_" + hashlib.sha256(TOK_OLD.encode()).hexdigest()[:16]
    expected_new = "h_" + hashlib.sha256(TOK_NEW.encode()).hexdigest()[:16]
    assert r_old.json()["user_id"] == expected_old
    assert r_new.json()["user_id"] == expected_new
    assert r_old.json()["user_id"] != r_new.json()["user_id"]  # 轮换 → 蒸发(旧行为)


def test_token_to_user_env_mapping(monkeypatch):
    """``OPENCLAW_GATEWAY_TOKEN_TO_USER`` JSON 映射 → 每个 token 各自稳定 user_id。"""
    from openclaw.gateway.auth import _configured_token_to_user, install_auth

    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN", f"{TOK_OLD},{TOK_NEW}")
    monkeypatch.setenv(
        "OPENCLAW_GATEWAY_TOKEN_TO_USER",
        f'{{"{TOK_OLD}":"alice","{TOK_NEW}":"bob"}}',
    )
    monkeypatch.delenv("OPENCLAW_GATEWAY_USER_ID", raising=False)
    assert _configured_token_to_user() == {TOK_OLD: "alice", TOK_NEW: "bob"}

    app = FastAPI()

    @app.get("/v1/whoami")
    async def whoami(request: Request):
        return {"user_id": request.state.user_id}

    install_auth(app, host="127.0.0.1")
    client = TestClient(app)

    r_old = client.get("/v1/whoami", headers={"Authorization": f"Bearer {TOK_OLD}"})
    r_new = client.get("/v1/whoami", headers={"Authorization": f"Bearer {TOK_NEW}"})
    assert r_old.json()["user_id"] == "alice"
    assert r_new.json()["user_id"] == "bob"


def test_token_to_user_invalid_json_returns_empty(monkeypatch):
    """``OPENCLAW_GATEWAY_TOKEN_TO_USER`` 非 JSON → 返回空 dict(不阻断启动)。"""
    from openclaw.gateway.auth import _configured_token_to_user

    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN_TO_USER", "not-json-{}")
    assert _configured_token_to_user() == {}

    monkeypatch.setenv("OPENCLAW_GATEWAY_TOKEN_TO_USER", "[1,2,3]")  # 非 dict
    assert _configured_token_to_user() == {}


# 注:per-user 隔离(显式 token_to_user 映射路径)的端到端覆盖由
# tests/test_phase25_memory_per_user.py 提供(本套件已验证其全绿);
# 这里不再重复造 fake gateway 栈,避免与 tests 包导入约定耦合。


# ────────────────────────────────────────────────────────────────
# 5. docker.from_env() client 用完 close()(防连接泄漏)
# ────────────────────────────────────────────────────────────────


class _FakeContainer:
    def __init__(self) -> None:
        self.removed = False

    def wait(self, timeout=None):  # noqa: ANN001
        return {"StatusCode": 0}

    def logs(self, stdout=True, stderr=True):  # noqa: ANN001
        return b"hello-from-container"

    def remove(self, force=True) -> None:  # noqa: ANN001
        self.removed = True


class _FakeImages:
    def __init__(self) -> None:
        self.pulled: list[str] = []

    def pull(self, img: str) -> None:
        self.pulled.append(img)

    def list(self):
        return []


class _FakeDockerClient:
    """记录 close() 调用的假 DockerClient。"""

    def __init__(self) -> None:
        self.images = _FakeImages()
        self._container = _FakeContainer()
        self.closed = False

    @property
    def containers(self):
        return SimpleNamespace(run=lambda *a, **k: self._container)

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def _patch_docker_from_env(monkeypatch):
    """把 ``docker.from_env()`` 替换成假 client,并返回"最近创建的 client"引用。"""
    import openclaw.tools.builtin.docker as docker_mod

    created: list[_FakeDockerClient] = []

    def _fake_from_env():
        c = _FakeDockerClient()
        created.append(c)
        return c

    # docker_mod.docker 是 import 进来的 docker 包对象;patch 它的 from_env
    monkeypatch.setattr(docker_mod.docker, "from_env", _fake_from_env)
    monkeypatch.setattr(docker_mod, "_HAS_DOCKER", True, raising=False)
    return created


def _registry_with_docker_tools():
    from openclaw.tools.registry import ToolRegistry
    from openclaw.tools.builtin import register_builtin_tools

    reg = ToolRegistry()
    register_builtin_tools(
        reg,
        include=["docker_run_python", "docker_exec", "docker_pull", "docker_list_images"],
        fs_root=".",
    )
    return reg


def test_docker_pull_closes_client(_patch_docker_from_env):
    reg = _registry_with_docker_tools()
    _set_test_approver(reg)  # C1: docker_pull requires approval
    out = asyncio.run(reg.call("docker_pull", {"image": "python:3.11-slim"}))
    assert "pulled" in out
    assert _patch_docker_from_env, "client 应被创建"
    assert _patch_docker_from_env[-1].closed, "docker_pull 未 close client(连接泄漏)"
    assert _patch_docker_from_env[-1].images.pulled == ["python:3.11-slim"]


def test_docker_list_images_closes_client(_patch_docker_from_env):
    reg = _registry_with_docker_tools()
    out = asyncio.run(reg.call("docker_list_images", {}))
    assert out == "(no images)"
    assert _patch_docker_from_env[-1].closed, "docker_list_images 未 close client"


def test_docker_run_python_closes_client(_patch_docker_from_env):
    reg = _registry_with_docker_tools()
    _set_test_approver(reg)  # C1: docker_run_python requires approval
    out = asyncio.run(reg.call("docker_run_python", {"code": "print(1)", "image": "python:3.11-slim"}))
    assert "[exit=0]" in out
    assert "hello-from-container" in out
    assert _patch_docker_from_env[-1].closed, "docker_run_python 未 close client"


def test_docker_exec_closes_client(_patch_docker_from_env):
    reg = _registry_with_docker_tools()
    _set_test_approver(reg)  # C1: docker_exec requires approval
    out = asyncio.run(reg.call("docker_exec", {"command": "echo hi", "image": "python:3.11-slim"}))
    assert "[exit=0]" in out
    assert _patch_docker_from_env[-1].closed, "docker_exec 未 close client"


def test_docker_list_images_returns_error_when_from_env_raises(monkeypatch):
    """``docker.from_env()`` 自身抛错时(无 daemon)返回友好错误,不崩。"""
    import openclaw.tools.builtin.docker as docker_mod

    def _boom():
        raise PermissionError("no /var/run/docker.sock")

    monkeypatch.setattr(docker_mod.docker, "from_env", _boom)
    monkeypatch.setattr(docker_mod, "_HAS_DOCKER", True, raising=False)
    reg = _registry_with_docker_tools()
    out = asyncio.run(reg.call("docker_list_images", {}))
    assert out.startswith("[error] 连接 docker daemon 失败")


# ────────────────────────────────────────────────────────────────
# helpers
# ────────────────────────────────────────────────────────────────


@contextlib.contextmanager
def tempfile_dir():
    d = Path(tempfile.mkdtemp())
    try:
        yield d
    finally:
        import shutil

        shutil.rmtree(d, ignore_errors=True)
