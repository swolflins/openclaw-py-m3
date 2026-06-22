"""P10: 飞书配置向导单测。

不联网:monkeypatch httpx 的所有调用,用 stub 替换响应。
覆盖:
- ERROR_TABLE:所有 (domain, code) 都能 lookup_error 命中
- lookup_error:fallback 到 (common, code)
- lookup_error:未知 code 返 None
- probe_all 失败:app_secret 错 → abort_reason 里有 fix
- probe_all 成功:bot open_id / app name / chats 都填好
- probe_all contact/v1/scope/get 99991672 + "No permission" → 用 contact 专用 hint
- probe_all im/v1/chats 返 0 chat → 有"搜 bot 名字"提示
- probe_all event/v1/subscriptions 走 degraded + manual_check
- render_report 不带颜色(非 tty)也能输出
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openclaw.channels.lark_wizard import (  # noqa: E402
    ERROR_TABLE,
    lookup_error,
    probe_all,
    render_report,
)
# ─────── ERROR_TABLE 覆盖 ───────

def test_error_table_has_critical_codes():
    critical = [
        ("auth", 10014), ("auth", 20001), ("auth", 20002),
        ("im", 230002), ("im", 230006), ("im", 230013),
        ("im", 230020), ("im", 230022), ("im", 230025),
        ("common", 99991663), ("common", 99991672),
    ]
    for key in critical:
        assert key in ERROR_TABLE, f"missing {key}"
        entry = ERROR_TABLE[key]
        for f in ("title", "fix", "url"):
            assert f in entry and entry[f], f"{key} missing {f!r}"


def test_error_table_size():
    """至少 18 条 — 任何时候不能丢太多。"""
    assert len(ERROR_TABLE) >= 18, f"only {len(ERROR_TABLE)} entries"


# ─────── lookup_error ───────

def test_lookup_exact_hit():
    e = lookup_error("im", 230013)
    assert e is not None
    assert "可用性" in e["title"] or "可用" in e["title"]


def test_lookup_common_fallback():
    """im/99991663 没具体条目,应 fallback 到 common/99991663。"""
    e = lookup_error("im", 99991663)
    assert e is not None
    assert "审核" in e["title"] or "内容" in e["title"]


def test_lookup_unknown_code():
    assert lookup_error("im", 99999) is None
    assert lookup_error("unknown_domain", 1) is None


# ─────── probe_all 网络 stub ───────

class _StubResp:
    def __init__(self, http: int, body: dict[str, Any]):
        self.status_code = http
        self._body = body

    def json(self):
        return self._body


class _StubClient:
    """替换 httpx.AsyncClient。"""
    def __init__(self, routes: dict[str, tuple[int, dict[str, Any]]]):
        self._routes = routes  # url substring -> (http, body)
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, **kw):
        self.calls.append((url, json or {}))
        for k, (http, body) in self._routes.items():
            if k in url:
                return _StubResp(http, body)
        return _StubResp(500, {"_miss": url})

    async def get(self, url, headers=None, params=None, timeout=None, **kw):
        self.calls.append((url, params or {}))
        for k, (http, body) in self._routes.items():
            if k in url:
                return _StubResp(http, body)
        return _StubResp(404, {"_miss": url})


@pytest.fixture
def stub_httpx(monkeypatch):
    def _factory(routes: dict[str, tuple[int, dict[str, Any]]]):
        stub = _StubClient(routes)

        class _CtxMgr:
            async def __aenter__(self_inner):
                return stub

            async def __aexit__(self_inner, *a):
                return False

        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **kw: _CtxMgr())
        return stub
    return _factory


def test_probe_all_credential_error_aborts(stub_httpx):
    """凭据错 → abort_reason + 不再跑后续。"""
    stub_httpx({
        "tenant_access_token/internal": (200, {"code": 10014, "msg": "invalid secret"}),
    })
    report = asyncio.run(probe_all("cli_x", "wrong"))
    assert "abort_reason" in report
    assert "10014" in report["abort_reason"]["fix"] or "secret" in report["abort_reason"]["fix"].lower() or "重置" in report["abort_reason"]["fix"]


def test_probe_all_success_with_but_zero_chats(stub_httpx):
    """凭据 OK + bot 0 chat → 探针全部走完,chats=0 有提示。"""
    stub_httpx({
        "tenant_access_token/internal": (200, {"code": 0, "tenant_access_token": "t-12345", "expire": 7200}),
        "bot/v3/info": (200, {"code": 0, "bot": {"open_id": "ou_abc", "app_name": "my-bot"}}),
        "application/v6/applications/cli_x": (400, {"code": 99992402, "msg": "field validation failed"}),
        "contact/v1/scope/get": (200, {"code": 0, "data": {"authed_open_departments": ["od_1"], "authed_open_user_ids": []}}),
        "im/v1/chats": (200, {"code": 0, "data": {"items": []}}),
    })
    report = asyncio.run(probe_all("cli_x", "ok"))
    assert "abort_reason" not in report
    assert report["bot"]["open_id"] == "ou_abc"
    assert report["bot_chats"]["count"] == 0
    # chats 提示
    chats_probe = next(p for p in report["probes"] if p["name"] == "im/v1/chats")
    assert "搜 bot 名字" in chats_probe["hint"] or "私聊" in chats_probe["hint"]
    # application 端点 degraded(99992402 视为未发布,不是 error)
    app_probe = next(p for p in report["probes"] if p["name"] == "application/v6/applications/:app_id")
    assert app_probe["status"] == "degraded"
    # event 端点 manual_check
    event_probe = next(p for p in report["probes"] if p["name"] == "event/v1/subscriptions")
    assert event_probe["status"] == "degraded"
    assert event_probe["hint"]
    # 至少跑了 5 个端点
    assert len(report["probes"]) >= 5


def test_probe_all_contact_no_permission(stub_httpx):
    """contact 端点 99991672 + 'No permission' 触发专用 hint。"""
    stub_httpx({
        "tenant_access_token/internal": (200, {"code": 0, "tenant_access_token": "t-1", "expire": 7200}),
        "bot/v3/info": (200, {"code": 0, "bot": {"open_id": "ou_1", "app_name": "b"}}),
        "application/v6/applications/cli_x": (400, {"code": 99992402, "msg": "f"}),
        "contact/v1/scope/get": (400, {"code": 99991672, "msg": "No permission"}),
        "im/v1/chats": (200, {"code": 0, "data": {"items": []}}),
    })
    report = asyncio.run(probe_all("cli_x", "ok"))
    cp = next(p for p in report["probes"] if p["name"] == "contact/v1/scope/get")
    assert "无 contact:contact:readonly" in cp["hint"]


def test_probe_all_empty_creds():
    """空 app_id/secret → 直接返 error。"""
    report = asyncio.run(probe_all("", ""))
    assert "error" in report
    assert report["error"]


# ─────── render_report ───────

def test_render_report_includes_todo(stub_httpx):
    """报告含 '后台操作清单' 段 + 至少 4 步。"""
    stub_httpx({
        "tenant_access_token/internal": (200, {"code": 0, "tenant_access_token": "t-1", "expire": 7200}),
        "bot/v3/info": (200, {"code": 0, "bot": {"open_id": "ou_1", "app_name": "b"}}),
        "application/v6/applications/cli_x": (400, {"code": 99992402, "msg": "f"}),
        "contact/v1/scope/get": (200, {"code": 0, "data": {}}),
        "im/v1/chats": (200, {"code": 0, "data": {"items": []}}),
    })
    report = asyncio.run(probe_all("cli_x", "ok"))
    out = render_report(report, force_color=False)
    assert "飞书后台配置诊断报告" in out
    assert "后台操作清单" in out
    # 关键步骤
    assert "im.message.receive_v1" in out
    assert "im:message" in out
    assert "可见范围" in out
    assert "版本管理" in out or "上线" in out
    # 显式 force_color=False → 不应有 ANSI
    assert "\033[" not in out


def test_render_report_color_when_forced(stub_httpx):
    """force_color=True 才有 ANSI。"""
    stub_httpx({
        "tenant_access_token/internal": (200, {"code": 0, "tenant_access_token": "t-1", "expire": 7200}),
        "bot/v3/info": (200, {"code": 0, "bot": {"open_id": "ou_1", "app_name": "b"}}),
        "application/v6/applications/cli_x": (400, {"code": 99992402, "msg": "f"}),
        "contact/v1/scope/get": (200, {"code": 0, "data": {}}),
        "im/v1/chats": (200, {"code": 0, "data": {"items": []}}),
    })
    report = asyncio.run(probe_all("cli_x", "ok"))
    out = render_report(report, force_color=True)
    assert "\033[" in out


def test_render_report_abort(stub_httpx):
    """凭据错 → 报告带 '凭据不可用,后续探测中止'。"""
    stub_httpx({
        "tenant_access_token/internal": (200, {"code": 10014, "msg": "invalid"}),
    })
    report = asyncio.run(probe_all("cli_x", "bad"))
    out = render_report(report)
    assert "凭据不可用" in out


# ─────── LarkChannel.send → reply 路径 ───────

class _FakeAgent:
    """极简 agent,AgentLoop 接口 stub。"""

    async def handle(self, session_id, text, **kw):
        class R:
            content = f"echo:{text}"
            tool_calls = []
            iterations = 1
        return R()

    async def new_session(self, sid=None):
        return sid or "s"

    @property
    def tools(self): return None

    @property
    def memory(self): return None

    @property
    def auto_reply(self): return None


def test_lark_channel_send_uses_cached_message_id(monkeypatch):
    """LarkChannel.send 应从 _last_msg_id 取 message_id 调 reply 接口。"""
    from openclaw.channels.lark import LarkChannel
    from openclaw.config.settings import LarkSettings

    captured: dict = {}

    class _Resp:
        status_code = 200
        def json(self_inner):
            return {"code": 0, "msg": "success", "data": {"message_id": "om_new"}}

    async def _post(self_inner, url, json=None, headers=None, **kw):
        captured["url"] = url
        captured["body"] = json
        captured["headers"] = headers
        return _Resp()

    async def _noop(*a, **kw):
        return None

    # tenant token stub
    async def _tok(self_inner):
        return "t-abc"

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)
    monkeypatch.setattr(LarkChannel, "_get_tenant_token", _tok)

    ch = LarkChannel(_FakeAgent(), LarkSettings(app_id="cli_x", app_secret="s", dedup_path=""))
    ch._last_msg_id["lark:oc_c1:ou_u1"] = "om_orig"
    asyncio.run(ch.send("lark:oc_c1:ou_u1", "你好"))
    assert "/im/v1/messages/om_orig/reply" in captured["url"]
    assert "你好" in captured["body"]["content"]


def test_lark_channel_send_no_message_id_warns(monkeypatch, caplog):
    """session 没 message_id → send 应只 warn 不抛、不发。"""
    from openclaw.channels.lark import LarkChannel
    from openclaw.config.settings import LarkSettings

    sent: dict = {}

    async def _post(self_inner, url, **kw):
        sent["hit"] = True
        class R:
            status_code = 200
            def json(self_inner): return {}
        return R()

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)

    ch = LarkChannel(_FakeAgent(), LarkSettings(app_id="cli_x", app_secret="s", dedup_path=""))
    # 没 _last_msg_id[session] → 应不发
    import logging
    caplog.set_level(logging.WARNING)
    asyncio.run(ch.send("lark:oc_unknown:ou_x", "ping"))
    assert "hit" not in sent
    assert any("message_id" in r.message for r in caplog.records)


def test_lark_channel_reply_logs_failure(monkeypatch, caplog):
    """reply 失败 → 记录 http/code, 不抛。"""
    from openclaw.channels.lark import LarkChannel
    from openclaw.config.settings import LarkSettings
    import logging

    class _Resp:
        status_code = 400
        headers = {"content-type": "application/json"}
        def json(self_inner):
            return {"code": 230013, "msg": "Bot has NO availability"}

    async def _post(self_inner, url, **kw):
        return _Resp()

    async def _tok(self_inner):
        return "t-1"

    monkeypatch.setattr(httpx.AsyncClient, "post", _post)
    monkeypatch.setattr(LarkChannel, "_get_tenant_token", _tok)

    ch = LarkChannel(_FakeAgent(), LarkSettings(app_id="cli_x", app_secret="s", dedup_path=""))
    ch._last_msg_id["lark:oc_1:ou_1"] = "om_1"

    caplog.set_level(logging.ERROR)
    asyncio.run(ch._reply_to_lark("om_1", "hi"))
    assert any("230013" in r.message for r in caplog.records)


# ─────── P11 端到端 mock(WS 不可达时也能跑) ───────

def _make_lark_event(chat_id: str, open_id: str, message_id: str, text: str):
    """构造一个真 P2ImMessageReceiveV1,直接喂给 LarkChannel._handle_event。"""
    from lark_oapi.api.im.v1 import (
        P2ImMessageReceiveV1,
        P2ImMessageReceiveV1Data,
    )
    from lark_oapi.api.im.v1.model.event_sender import EventSender
    from lark_oapi.api.im.v1.model.event_message import EventMessage

    evt = P2ImMessageReceiveV1()
    evt.event = P2ImMessageReceiveV1Data()
    # init() 走 dict-only 路径 — 内嵌对象必须也是 dict
    evt.event.sender = EventSender(
        d={"sender_id": {"open_id": open_id, "union_id": "uu", "user_id": "u"},
           "sender_type": "user", "tenant_key": "tk"}
    )
    evt.event.message = EventMessage(
        d={
            "message_id": message_id,
            "chat_id": chat_id,
            "chat_type": "p2p",
            "message_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
    )
    return evt


def test_lark_e2e_echo_agent_replies_with_cached_message_id(monkeypatch):
    """真 P2ImMessageReceiveV1 → _handle_event → dispatch(echo agent) → _reply_to_lark。"""
    from openclaw.channels.lark import LarkChannel
    from openclaw.config.settings import LarkSettings

    replies: list[tuple[str, str]] = []

    async def _fake_reply(self, message_id, text):
        replies.append((message_id, text))

    monkeypatch.setattr(LarkChannel, "_reply_to_lark", _fake_reply)

    ch = LarkChannel(_FakeAgent(), LarkSettings(app_id="cli_x", app_secret="s", dedup_path=""))
    evt = _make_lark_event(chat_id="oc_c1", open_id="ou_u1", message_id="om_xyz", text="ping")

    asyncio.run(ch._handle_event(evt))

    assert len(replies) == 1, f"应回 1 条,实际 {len(replies)}"
    msg_id, text = replies[0]
    assert msg_id == "om_xyz", f"reply 的 message_id 应等于入站 message_id,实际 {msg_id}"
    assert "echo:ping" in text or "ping" in text
    # session_id 格式
    assert "lark:oc_c1:ou_u1" in ch._last_msg_id
    assert ch._last_msg_id["lark:oc_c1:ou_u1"] == "om_xyz"
    # LarkChannel 收到消息 + 回信 的中间产物
    assert len(ch.received) == 1
    assert ch.received[0].text == "ping"
    assert ch.received[0].session_id == "lark:oc_c1:ou_u1"
    assert ch.received[0].metadata["is_dm"] is True
    assert ch.received[0].metadata["message_id"] == "om_xyz"


def test_lark_e2e_multiturn_keeps_message_id_for_session(monkeypatch):
    """同一个 session 连续两条消息 → 都 reply 到各自的 message_id。"""
    from openclaw.channels.lark import LarkChannel
    from openclaw.config.settings import LarkSettings

    replies: list[tuple[str, str]] = []

    async def _fake_reply(self, message_id, text):
        replies.append((message_id, text))

    monkeypatch.setattr(LarkChannel, "_reply_to_lark", _fake_reply)

    ch = LarkChannel(_FakeAgent(), LarkSettings(app_id="cli_x", app_secret="s", dedup_path=""))
    # 第一条
    e1 = _make_lark_event("oc_c", "ou_u", "om_001", "hi")
    asyncio.run(ch._handle_event(e1))
    # 第二条
    e2 = _make_lark_event("oc_c", "ou_u", "om_002", "there")
    asyncio.run(ch._handle_event(e2))

    assert [m for m, _ in replies] == ["om_001", "om_002"]
    # 缓存更新到最后一条
    assert ch._last_msg_id["lark:oc_c:ou_u"] == "om_002"


def test_lark_e2e_drops_empty_text(monkeypatch):
    """空文本(其它 message_type 没解析出 text)→ 不发 reply。"""
    from openclaw.channels.lark import LarkChannel
    from openclaw.config.settings import LarkSettings

    replies: list[tuple[str, str]] = []

    async def _fake_reply(self, message_id, text):
        replies.append((message_id, text))

    monkeypatch.setattr(LarkChannel, "_reply_to_lark", _fake_reply)

    ch = LarkChannel(_FakeAgent(), LarkSettings(app_id="cli_x", app_secret="s", dedup_path=""))
    evt = _make_lark_event("oc_c", "ou_u", "om_x", "")  # 空
    asyncio.run(ch._handle_event(evt))

    assert replies == [], f"空文本应被 drop,实际回了 {replies}"
    # 空文本时,_handle_event 提前 return,连 message_id 都不缓存(更干净)
    assert ch._last_msg_id == {}


def test_lark_e2e_post_message_extracts_text(monkeypatch):
    """post 类型消息(富文本)能正确提取第一段纯文本。"""
    from openclaw.channels.lark import LarkChannel
    from openclaw.config.settings import LarkSettings

    replies: list[tuple[str, str]] = []

    async def _fake_reply(self, message_id, text):
        replies.append((message_id, text))

    monkeypatch.setattr(LarkChannel, "_reply_to_lark", _fake_reply)

    ch = LarkChannel(_FakeAgent(), LarkSettings(app_id="cli_x", app_secret="s", dedup_path=""))
    # post 类型 content
    from lark_oapi.api.im.v1 import (
        P2ImMessageReceiveV1, P2ImMessageReceiveV1Data,
    )
    from lark_oapi.api.im.v1.model.event_sender import EventSender
    from lark_oapi.api.im.v1.model.event_message import EventMessage

    evt = P2ImMessageReceiveV1()
    evt.event = P2ImMessageReceiveV1Data()
    evt.event.sender = EventSender(
        d={"sender_id": {"open_id": "ou_u", "union_id": "u", "user_id": "u"},
           "sender_type": "user", "tenant_key": "tk"}
    )
    post_content = json.dumps({
        "content": [[{"tag": "text", "text": "求和 1+1"}]]
    }, ensure_ascii=False)
    evt.event.message = EventMessage(
        d={"message_id": "om_post", "chat_id": "oc_c", "chat_type": "p2p",
           "message_type": "post", "content": post_content}
    )
    asyncio.run(ch._handle_event(evt))
    assert len(replies) == 1
    assert "1+1" in replies[0][1]
