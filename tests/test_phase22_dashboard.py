"""Phase 22:Gateway Journal routes + Dashboard HTML。

覆盖:
- /v1/journal/entries 列出最近 entries
- /v1/journal/entries/read 读单个(path 越界拦截)
- /v1/journal/weekly 生成周报
- /v1/journal/soul-proposals 读 proposals
- 没 journal 时返回 503(不假装 200)
- dashboard.html 是自包含 HTML(无外链)
- index.html 是新版 dashboard.html(老 demo 不破坏)
- 路由注册的 4 个 endpoint 都进 app
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# ─────────────── Fixtures ───────────────

@pytest.fixture
def tmp_journal(tmp_path: Path):
    """构造一个真 AgentJournal + 3 个 sessions。"""
    from openclaw.agent.journal import AgentJournal
    j = AgentJournal(root=tmp_path / "j")
    for i in range(3):
        e = j.record_session(
            session_id=f"sess_{i}",
            user_message=f"q{i}: 你好?",
            response=type("R", (), {
                "content": f"reply {i}",
                "iterations": 1 + i,
                "tool_calls": [],
                "session_id": f"sess_{i}",
            })(),
        )
        asyncio.run(j.reflect(e))
    return j


@pytest.fixture
def client(tmp_journal):
    """TestClient with journal attached;agent_loop not set → /v1/chat 503。"""
    from openclaw.gateway.deps import GatewayDeps, set_deps, reset_deps
    from openclaw.gateway.app import create_app

    deps = GatewayDeps(agent_loop=None, config=None, journal=tmp_journal)
    set_deps(deps)
    application = create_app(rate_limiter=None)
    yield TestClient(application)
    reset_deps()


# ─────────────── 路由注册 ───────────────

def test_journal_router_registers_4_routes():
    """journal router 暴露 4 个 endpoint:entries / entries/read / weekly / soul-proposals。"""
    from openclaw.gateway.routes import journal as jmod
    # FastAPI router.routes[i].path 包含 prefix
    paths = sorted(r.path for r in jmod.router.routes)
    assert "/journal/entries" in paths
    assert "/journal/entries/read" in paths
    assert "/journal/weekly" in paths
    assert "/journal/soul-proposals" in paths


def test_journal_router_included_in_app(client):
    """journal router 应在 main app 中(4 个 endpoint 可见)。"""
    r = client.get("/v1/journal/entries?days=7")
    assert r.status_code == 200


# ─────────────── /entries ───────────────

def test_entries_returns_recent(client):
    r = client.get("/v1/journal/entries?days=7")
    assert r.status_code == 200
    j = r.json()
    assert j["count"] == 3
    assert len(j["entries"]) == 3
    # 元数据解析
    e = j["entries"][0]
    assert "session_id" in e
    assert "path" in e
    assert "iterations" in e
    assert "tags" in e


def test_entries_rejects_too_long_range(tmp_path):
    """days > 90 应 422(Pydantic 校验)。"""
    from openclaw.gateway.deps import GatewayDeps, set_deps, reset_deps
    from openclaw.gateway.app import create_app
    from openclaw.agent.journal import AgentJournal

    j = AgentJournal(root=tmp_path / "test_long_range")
    set_deps(GatewayDeps(journal=j))
    app = create_app(rate_limiter=None)
    c = TestClient(app)
    r = c.get("/v1/journal/entries?days=999")
    assert r.status_code == 422
    reset_deps()


def test_entries_returns_empty_when_no_entries(tmp_path):
    from openclaw.agent.journal import AgentJournal
    from openclaw.gateway.deps import GatewayDeps, set_deps, reset_deps
    from openclaw.gateway.app import create_app

    j = AgentJournal(root=tmp_path / "empty")
    set_deps(GatewayDeps(journal=j))
    app = create_app(rate_limiter=None)
    c = TestClient(app)
    r = c.get("/v1/journal/entries?days=7")
    assert r.status_code == 200
    assert r.json() == {"entries": [], "count": 0}
    reset_deps()


# ─────────────── /entries/read ───────────────

def test_read_entry_returns_content(client):
    listing = client.get("/v1/journal/entries?days=7").json()
    path = listing["entries"][0]["path"]
    r = client.get(f"/v1/journal/entries/read?path={path}")
    assert r.status_code == 200
    j = r.json()
    assert j["path"] == path
    assert "Session" in j["content"]


def test_read_entry_blocks_path_escape(client):
    """path 越界应 400,而非读到 journal 外的文件。"""
    r = client.get("/v1/journal/entries/read?path=../../etc/passwd")
    assert r.status_code == 400
    assert "escapes" in r.json()["detail"].lower()


def test_read_entry_404_when_missing(client):
    r = client.get("/v1/journal/entries/read?path=2099-01-01/sess_xxx.md")
    assert r.status_code == 404


# ─────────────── /weekly ───────────────

def test_weekly_generates_report(client):
    r = client.post("/v1/journal/weekly")
    assert r.status_code == 200
    j = r.json()
    assert "weekly_report" in j
    assert j["weekly_report"].startswith("weekly_")
    assert "周报" in j["content"]


# ─────────────── /soul-proposals ───────────────

def test_soul_proposals_exists_after_session(client):
    """先 reflect 已经做了;trigger weekly;读 proposals(exists 字段必须有)。"""
    client.post("/v1/journal/weekly")
    r = client.get("/v1/journal/soul-proposals")
    assert r.status_code == 200
    j = r.json()
    assert "exists" in j
    assert "proposals" in j


def test_soul_proposals_returns_content(tmp_path):
    """手动写一个 _soul_proposals.md 进去,route 应读出来。"""
    from openclaw.agent.journal import AgentJournal, JournalEntry
    from openclaw.gateway.deps import GatewayDeps, set_deps, reset_deps
    from openclaw.gateway.app import create_app

    j = AgentJournal(root=tmp_path / "j2")
    e = JournalEntry(
        session_id="sess_p", timestamp="2026-06-20T10:00:00+00:00",
        user_message="q", final_content="a", iterations=1,
    )
    j.generate_soul_proposal(e)
    set_deps(GatewayDeps(journal=j))
    app = create_app(rate_limiter=None)
    c = TestClient(app)
    r = c.get("/v1/journal/soul-proposals")
    assert r.status_code == 200
    j_resp = r.json()
    assert j_resp["exists"] is True
    assert "sess_p" in j_resp["proposals"]
    reset_deps()


# ─────────────── 没 journal 时的 503 ───────────────

def test_no_journal_returns_503():
    from openclaw.gateway.deps import GatewayDeps, set_deps, reset_deps
    from openclaw.gateway.app import create_app

    set_deps(GatewayDeps(journal=None))
    app = create_app(rate_limiter=None)
    c = TestClient(app)
    assert c.get("/v1/journal/entries").status_code == 503
    assert c.post("/v1/journal/weekly").status_code == 503
    # /soul-proposals 永远 200(无 journal 时返回空)
    assert c.get("/v1/journal/soul-proposals").status_code == 200
    reset_deps()


# ─────────────── Dashboard HTML ───────────────

def test_dashboard_html_exists_and_self_contained():
    """index.html(= 新 dashboard)应是自包含 HTML,无外链。"""
    p = Path(__file__).resolve().parent.parent / "openclaw/gateway/static/index.html"
    assert p.is_file()
    text = p.read_text(encoding="utf-8")
    assert "<html" in text
    # 关键 UI 元素
    for kw in ("Journal", "Soul Proposals", "browse_url", "weekly", "soul-proposals", "/v1/journal/entries"):
        assert kw in text, f"dashboard.html 缺少关键字: {kw}"
    # 不应有外链 CDN / script
    assert "cdn.jsdelivr.net" not in text
    assert "googleapis.com" not in text
    assert 'src="http' not in text


def test_legacy_html_preserved():
    """原 index.html 备份为 legacy.html,保留可访问。"""
    p = Path(__file__).resolve().parent.parent / "openclaw/gateway/static/legacy.html"
    assert p.is_file()
    assert "OpenClaw Gateway" in p.read_text(encoding="utf-8")


def test_dashboard_html_size_reasonable():
    """dashboard 应该在 500-1000 行(自包含)。"""
    p = Path(__file__).resolve().parent.parent / "openclaw/gateway/static/index.html"
    lines = len(p.read_text(encoding="utf-8").splitlines())
    assert 500 <= lines <= 1000, f"dashboard size 异常: {lines} 行"
