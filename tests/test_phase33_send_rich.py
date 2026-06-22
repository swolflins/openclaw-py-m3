"""Phase 33 测试 — 飞书主动发送(post 富文本 / 卡片 / 多 receive_id)。

参考 Hermes Feishu 4460-4504 处的发送逻辑:
- 三种 receive_id_type:user_id / open_id / chat_id
- post 富文本(多行 + at / text 段)
- interactive 卡片
- 内容超长截断

openclaw-py 之前 ``LarkChannel.send`` 只支持 plain text + reply;
本 Phase 新增 ``send_typed`` 通用入口 + 4 个 builder helpers。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _real_secret(v: str) -> Any:
    from pydantic import SecretStr
    return SecretStr(v)


def _make_lark(monkeypatch, tmp_path, **overrides: Any):
    from openclaw.agent.loop import AgentLoop  # type: ignore
    from openclaw.config.settings import LarkSettings
    from openclaw.channels.lark import LarkChannel

    kw: dict[str, Any] = {
        "app_id": "cli_test_app",
        "app_secret": _real_secret("test_secret_xxxxxxxxxx"),
        "dedup_path": str(tmp_path / "dedup.json"),
        "media_dir": str(tmp_path / "media"),
    }
    kw.update(overrides)
    s = LarkSettings(**kw)
    return LarkChannel(MagicMock(spec=AgentLoop), s)


# ============================================================
# A. _resolve_receive_id
# ============================================================

class TestAResolveReceiveId:
    def test_empty_chat_id(self):
        from openclaw.channels.lark import _resolve_receive_id
        rid, t = _resolve_receive_id("")
        assert rid == "" and t == "chat_id"

    def test_user_id_prefix(self):
        from openclaw.channels.lark import _resolve_receive_id
        rid, t = _resolve_receive_id("feishu_user_id:u_abc")
        assert rid == "u_abc" and t == "user_id"

    def test_open_id_prefix(self):
        from openclaw.channels.lark import _resolve_receive_id
        rid, t = _resolve_receive_id("ou_xyz")
        assert rid == "ou_xyz" and t == "open_id"

    def test_chat_id_default(self):
        from openclaw.channels.lark import _resolve_receive_id
        rid, t = _resolve_receive_id("oc_chatgroup123")
        assert rid == "oc_chatgroup123" and t == "chat_id"

    def test_chat_id_starts_with_oc(self):
        from openclaw.channels.lark import _resolve_receive_id
        rid, t = _resolve_receive_id("oc_some_chat_id_456")
        assert t == "chat_id"

    def test_chat_id_starts_with_on(self):
        """``on_`` 是另一种 chat_id 前缀(robot 群场景)。"""
        from openclaw.channels.lark import _resolve_receive_id
        rid, t = _resolve_receive_id("on_robot_chat")
        assert t == "chat_id"


# ============================================================
# B. _truncate_text
# ============================================================

class TestBTruncateText:
    def test_short_text_unchanged(self):
        from openclaw.channels.lark import _truncate_text
        assert _truncate_text("hi", 100) == "hi"

    def test_long_text_truncated_with_suffix(self):
        from openclaw.channels.lark import _truncate_text
        out = _truncate_text("x" * 100, 20)
        assert len(out) <= 20
        assert "truncated" in out

    def test_exact_boundary(self):
        from openclaw.channels.lark import _truncate_text
        assert _truncate_text("x" * 10, 10) == "x" * 10

    def test_extreme_max_len(self):
        from openclaw.channels.lark import _truncate_text
        # max_len <= 5 → 不附加 suffix
        assert _truncate_text("abcdef", 3) == "abc"
        assert _truncate_text("abcdef", 0) == ""

    def test_chinese_text_truncated_by_chars(self):
        """中文按字符数截断(len 算 char 不是 byte;飞书服务端也按 char)。"""
        from openclaw.channels.lark import _truncate_text
        out = _truncate_text("你" * 100, 30)
        assert len(out) <= 30


# ============================================================
# C. builders
# ============================================================

class TestCBuilders:
    def test_build_text_payload(self):
        from openclaw.channels.lark import LarkChannel
        assert LarkChannel.build_text_payload("hi") == {"text": "hi"}

    def test_build_post_payload_with_title(self):
        from openclaw.channels.lark import LarkChannel
        out = LarkChannel.build_post_payload(
            [[{"tag": "text", "text": "第一行"}]],
            title="公告",
        )
        assert out == {
            "content": [[{"tag": "text", "text": "第一行"}]],
            "title": "公告",
        }

    def test_build_post_payload_without_title(self):
        from openclaw.channels.lark import LarkChannel
        out = LarkChannel.build_post_payload([[{"tag": "text", "text": "x"}]])
        assert out == {"content": [[{"tag": "text", "text": "x"}]]}
        assert "title" not in out

    def test_build_at_post_payload_multi_user(self):
        from openclaw.channels.lark import LarkChannel
        out = LarkChannel.build_at_post_payload(
            ["ou_a", "ou_b"], "请尽快处理",
        )
        line = out["content"][0]
        assert line[0] == {"tag": "at", "user_id": "ou_a"}
        assert line[1] == {"tag": "at", "user_id": "ou_b"}
        assert line[2] == {"tag": "text", "text": "请尽快处理"}

    def test_build_at_post_payload_filters_empty(self):
        from openclaw.channels.lark import LarkChannel
        out = LarkChannel.build_at_post_payload(["ou_a", "", None], "hi")
        line = out["content"][0]
        # "" 和 None 被过滤
        assert len(line) == 2
        assert line[0] == {"tag": "at", "user_id": "ou_a"}

    def test_build_at_post_payload_no_text(self):
        from openclaw.channels.lark import LarkChannel
        out = LarkChannel.build_at_post_payload(["ou_a"])
        line = out["content"][0]
        assert line == [{"tag": "at", "user_id": "ou_a"}]

    def test_build_interactive_card_payload_full(self):
        from openclaw.channels.lark import LarkChannel
        elements = [
            {"tag": "div", "text": {"content": "hello"}},
            {"tag": "action", "actions": [
                {"tag": "button", "text": {"content": "OK"}, "value": {"key": "ok"}}
            ]},
        ]
        out = LarkChannel.build_interactive_card_payload(
            elements=elements,
            header={"title": "标题", "template": "blue"},
            config={"wide_screen_mode": True},
        )
        assert out["elements"] == elements
        assert out["header"] == {"title": "标题", "template": "blue"}
        assert out["config"] == {"wide_screen_mode": True}

    def test_build_interactive_card_payload_minimal(self):
        from openclaw.channels.lark import LarkChannel
        out = LarkChannel.build_interactive_card_payload(
            elements=[{"tag": "div", "text": {"content": "x"}}],
        )
        assert "elements" in out
        assert "header" not in out
        assert "config" not in out


# ============================================================
# D. send_typed 行为
# ============================================================

class TestDSendTyped:
    def test_unavailable_channel_returns_false(self, tmp_path, monkeypatch):
        """未配 app_id / secret → False,不抛。"""
        ch = _make_lark(monkeypatch, tmp_path, app_id="", app_secret=_real_secret(""))
        assert ch.available is False
        result = asyncio.run(ch.send_typed("lark:oc_x:ou_a", "text", {"text": "hi"}))
        assert result is False

    def test_unsupported_msg_type_returns_false(self, tmp_path, monkeypatch):
        ch = _make_lark(monkeypatch, tmp_path)
        # 屏蔽 token 拉取 + 网络(返回 None 即可)
        ch._send_typed = AsyncMock(return_value=True)  # type: ignore[method-assign]
        result = asyncio.run(ch.send_typed("lark:oc_x:ou_a", "video", {"x": 1}))
        assert result is False
        ch._send_typed.assert_not_awaited()

    def test_content_serialization(self, tmp_path, monkeypatch):
        """content 是 dict → JSON 字符串。"""
        ch = _make_lark(monkeypatch, tmp_path)
        ch._send_typed = AsyncMock(return_value=True)  # type: ignore[method-assign]
        asyncio.run(ch.send_typed("lark:oc_x:ou_a", "text", {"text": "hi"}))
        ch._send_typed.assert_awaited_once()
        args, _ = ch._send_typed.call_args
        # args = (session_id, msg_type, content_str, ...)
        assert args[0] == "lark:oc_x:ou_a"
        assert args[1] == "text"
        assert json.loads(args[2]) == {"text": "hi"}

    def test_content_string_passthrough(self, tmp_path, monkeypatch):
        ch = _make_lark(monkeypatch, tmp_path)
        ch._send_typed = AsyncMock(return_value=True)  # type: ignore[method-assign]
        asyncio.run(ch.send_typed("lark:oc_x:ou_a", "text", '{"text":"hi"}'))
        args, _ = ch._send_typed.call_args
        assert args[2] == '{"text":"hi"}'

    def test_content_unserializable_returns_false(self, tmp_path, monkeypatch):
        ch = _make_lark(monkeypatch, tmp_path)
        ch._send_typed = AsyncMock(return_value=True)  # type: ignore[method-assign]
        # set 不可 JSON
        result = asyncio.run(ch.send_typed(
            "lark:oc_x:ou_a", "text", {"x": {1, 2, 3}},  # type: ignore[dict-item]
        ))
        assert result is False
        ch._send_typed.assert_not_awaited()

    def test_reply_to_overrides_create(self, tmp_path, monkeypatch):
        """reply_to 不为空 → 走 reply API(create 不调)。"""
        ch = _make_lark(monkeypatch, tmp_path)
        ch._send_typed = AsyncMock(return_value=True)  # type: ignore[method-assign]
        asyncio.run(ch.send_typed(
            "lark:oc_x:ou_a", "text", {"text": "hi"}, reply_to="om_orig",
        ))
        # reply_to 透传
        _, kwargs = ch._send_typed.call_args
        assert kwargs["reply_to"] == "om_orig"

    def test_supported_msg_types(self, tmp_path, monkeypatch):
        """_SUPPORTED_OUTBOUND_MSG_TYPES 至少含 text / post / interactive。"""
        from openclaw.channels import lark
        for t in ("text", "post", "interactive", "image", "file", "share_chat"):
            assert t in lark._SUPPORTED_OUTBOUND_MSG_TYPES


# ============================================================
# E. _send_typed 真正 HTTP 行为(打网络层)
# ============================================================

class TestESendTypedHttp:
    def test_post_message_uses_correct_url_and_body(self, tmp_path, monkeypatch):
        """post(无 reply_to)→ im/v1/messages + receive_id_type query。"""
        ch = _make_lark(monkeypatch, tmp_path)
        ch._get_tenant_token = AsyncMock(return_value="tkn")  # type: ignore[method-assign]

        captured: dict[str, Any] = {}

        class FakeResp:
            status_code = 200
            headers = {"content-type": "application/json"}

            def json(self_inner):
                return {"code": 0, "msg": "ok", "data": {"message_id": "om_new"}}

        class FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, json=None, headers=None):
                captured["url"] = url
                captured["body"] = json
                captured["headers"] = headers
                return FakeResp()

        with patch("httpx.AsyncClient", FakeClient):
            result = asyncio.run(ch._send_typed(
                "lark:oc_group1:ou_a", "post",
                json.dumps({"content": [[{"tag": "text", "text": "x"}]]}, ensure_ascii=False),
            ))

        assert result is True
        assert "receive_id_type=chat_id" in captured["url"]
        assert captured["body"]["receive_id"] == "oc_group1"
        assert captured["body"]["msg_type"] == "post"
        assert captured["headers"]["Authorization"] == "Bearer tkn"

    def test_reply_uses_reply_api(self, tmp_path, monkeypatch):
        ch = _make_lark(monkeypatch, tmp_path)
        ch._get_tenant_token = AsyncMock(return_value="tkn")  # type: ignore[method-assign]
        captured: dict[str, Any] = {}

        class FakeResp:
            status_code = 200
            headers = {"content-type": "application/json"}
            def json(self_inner):
                return {"code": 0}

        class FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, json=None, headers=None):
                captured["url"] = url
                captured["body"] = json
                return FakeResp()

        with patch("httpx.AsyncClient", FakeClient):
            result = asyncio.run(ch._send_typed(
                "lark:oc_x:ou_a", "text", '{"text":"hi"}', reply_to="om_orig",
            ))
        assert result is True
        # 走 reply API,URL 包含 /reply
        assert "/im/v1/messages/om_orig/reply" in captured["url"]
        # reply 模式 body 不含 receive_id
        assert "receive_id" not in captured["body"]
        assert captured["body"]["msg_type"] == "text"

    def test_http_error_returns_false(self, tmp_path, monkeypatch):
        ch = _make_lark(monkeypatch, tmp_path)
        ch._get_tenant_token = AsyncMock(return_value="tkn")  # type: ignore[method-assign]

        class FakeResp:
            status_code = 200
            headers = {"content-type": "application/json"}
            def json(self_inner):
                return {"code": 230020, "msg": "权限不足"}

        class FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, *a, **k): return FakeResp()

        with patch("httpx.AsyncClient", FakeClient):
            result = asyncio.run(ch._send_typed(
                "lark:oc_x:ou_a", "text", '{"text":"hi"}',
            ))
        assert result is False

    def test_no_token_returns_false(self, tmp_path, monkeypatch):
        ch = _make_lark(monkeypatch, tmp_path)
        ch._get_tenant_token = AsyncMock(return_value=None)  # type: ignore[method-assign]
        result = asyncio.run(ch._send_typed("lark:oc_x:ou_a", "text", '{"text":"hi"}'))
        assert result is False

    def test_empty_session_returns_false(self, tmp_path, monkeypatch):
        ch = _make_lark(monkeypatch, tmp_path)
        ch._get_tenant_token = AsyncMock(return_value="tkn")  # type: ignore[method-assign]
        # session 没 chat_id(只有 1 段)
        result = asyncio.run(ch._send_typed("lark", "text", '{"text":"hi"}'))
        assert result is False

    def test_open_id_session_uses_open_id_type(self, tmp_path, monkeypatch):
        """session_id 的 chat_id 是 ou_ 前缀 → receive_id_type=open_id。"""
        ch = _make_lark(monkeypatch, tmp_path)
        ch._get_tenant_token = AsyncMock(return_value="tkn")  # type: ignore[method-assign]
        captured: dict[str, Any] = {}

        class FakeResp:
            status_code = 200
            headers = {"content-type": "application/json"}
            def json(self_inner): return {"code": 0}

        class FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, json=None, headers=None):
                captured["url"] = url
                captured["body"] = json
                return FakeResp()

        with patch("httpx.AsyncClient", FakeClient):
            asyncio.run(ch._send_typed("lark:ou_alice:ou_bob", "text", '{"text":"hi"}'))
        assert "receive_id_type=open_id" in captured["url"]
        assert captured["body"]["receive_id"] == "ou_alice"

    def test_user_id_session_uses_user_id_type(self, tmp_path, monkeypatch):
        ch = _make_lark(monkeypatch, tmp_path)
        ch._get_tenant_token = AsyncMock(return_value="tkn")  # type: ignore[method-assign]
        captured: dict[str, Any] = {}

        class FakeResp:
            status_code = 200
            headers = {"content-type": "application/json"}
            def json(self_inner): return {"code": 0}

        class FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, json=None, headers=None):
                captured["url"] = url
                return FakeResp()

        with patch("httpx.AsyncClient", FakeClient):
            asyncio.run(ch._send_typed(
                "lark:feishu_user_id:u_xyz:ou_bob", "text", '{"text":"hi"}',
            ))
        assert "receive_id_type=user_id" in captured["url"]


# ============================================================
# F. _post_message 直接入口
# ============================================================

class TestFPostMessage:
    def test_post_message_uses_given_receive_id(self, tmp_path, monkeypatch):
        ch = _make_lark(monkeypatch, tmp_path)
        ch._send_typed = AsyncMock(return_value=True)  # type: ignore[method-assign]
        result = asyncio.run(ch._post_message(
            "oc_xyz", "chat_id", "text", '{"text":"hi"}',
        ))
        assert result is True
        ch._send_typed.assert_awaited_once()
        args, _ = ch._send_typed.call_args
        # session_id 合成 lark:<receive_id>:manual
        assert args[0] == "lark:oc_xyz:manual"
        assert args[1] == "text"
        assert args[2] == '{"text":"hi"}'


# ============================================================
# G. reply path(_reply_to_lark)用 _truncate_text
# ============================================================

class TestGReplyTruncate:
    def test_long_reply_truncated(self, tmp_path, monkeypatch, caplog):
        """_reply_to_lark:超长 text 走 _truncate_text。"""
        ch = _make_lark(monkeypatch, tmp_path)
        from openclaw.channels import lark
        # 缩到 50 char
        monkeypatch.setattr(lark, "LARK_MAX_MESSAGE_LENGTH", 50)
        ch._get_tenant_token = AsyncMock(return_value="tkn")  # type: ignore[method-assign]
        ch._last_msg_id["lark:oc_x:ou_a"] = "om_orig"

        captured: dict[str, Any] = {}

        class FakeResp:
            status_code = 200
            headers = {"content-type": "application/json"}
            def json(self_inner): return {"code": 0}

        class FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, json=None, headers=None):
                captured["body"] = json
                return FakeResp()

        long_text = "x" * 1000
        with patch("httpx.AsyncClient", FakeClient):
            asyncio.run(ch.send("lark:oc_x:ou_a", long_text))
        # 发送的 body content.text 长度应 <= 50
        sent_text = json.loads(captured["body"]["content"])["text"]
        assert len(sent_text) <= 50
        assert "truncated" in sent_text


# ============================================================
# H. 集成:end-to-end 通过 send_typed 发送 post 富文本
# ============================================================

class TestHEndToEndSendRich:
    def test_post_rich_text_end_to_end(self, tmp_path, monkeypatch):
        """业务调用:build_at_post_payload → send_typed → 网络层。"""
        from openclaw.channels.lark import LarkChannel
        ch = _make_lark(monkeypatch, tmp_path)
        ch._get_tenant_token = AsyncMock(return_value="tkn")  # type: ignore[method-assign]
        captured: dict[str, Any] = {}

        class FakeResp:
            status_code = 200
            headers = {"content-type": "application/json"}
            def json(self_inner): return {"code": 0, "data": {"message_id": "om_post_1"}}

        class FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, json=None, headers=None):
                captured["url"] = url
                captured["body"] = json
                return FakeResp()

        payload = LarkChannel.build_at_post_payload(
            ["ou_alice", "ou_bob"], "请审批 PR #42",
        )
        with patch("httpx.AsyncClient", FakeClient):
            ok = asyncio.run(ch.send_typed(
                "lark:oc_engineering:ou_sender", "post", payload,
            ))
        assert ok is True
        assert captured["body"]["msg_type"] == "post"
        sent = json.loads(captured["body"]["content"])
        assert sent["content"][0][0] == {"tag": "at", "user_id": "ou_alice"}
        assert sent["content"][0][2]["text"] == "请审批 PR #42"

    def test_interactive_card_end_to_end(self, tmp_path, monkeypatch):
        ch = _make_lark(monkeypatch, tmp_path)
        ch._get_tenant_token = AsyncMock(return_value="tkn")  # type: ignore[method-assign]
        captured: dict[str, Any] = {}

        class FakeResp:
            status_code = 200
            headers = {"content-type": "application/json"}
            def json(self_inner): return {"code": 0}

        class FakeClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def post(self, url, json=None, headers=None):
                captured["body"] = json
                return FakeResp()

        from openclaw.channels.lark import LarkChannel
        card = LarkChannel.build_interactive_card_payload(
            elements=[
                {"tag": "div", "text": {"content": "确认提交?"}},
                {"tag": "action", "actions": [
                    {"tag": "button", "text": {"content": "确认"},
                     "type": "primary", "value": {"action": "confirm"}},
                ]},
            ],
            header={"title": "审批", "template": "blue"},
        )
        with patch("httpx.AsyncClient", FakeClient):
            ok = asyncio.run(ch.send_typed("lark:oc_x:ou_a", "interactive", card))
        assert ok is True
        assert captured["body"]["msg_type"] == "interactive"
        sent = json.loads(captured["body"]["content"])
        assert sent["header"]["title"] == "审批"
        assert sent["elements"][1]["actions"][0]["value"]["action"] == "confirm"


# ============================================================
# I. 鲁棒性 / 不与 Phase 31/32 冲突
# ============================================================

class TestIRobustness:
    def test_phase31_dedup_still_works(self, tmp_path, monkeypatch):
        ch = _make_lark(monkeypatch, tmp_path)
        assert asyncio.run(ch._is_duplicate("m_x")) is False
        assert asyncio.run(ch._is_duplicate("m_x")) is True

    def test_phase32_media_constants_exposed(self):
        from openclaw.channels import lark
        for k in (
            "WEBHOOK_MAX_BODY_BYTES", "WEBHOOK_RATE_LIMIT_MAX",
            "LARK_MEDIA_MAX_BYTES", "LARK_MEDIA_TYPES",
        ):
            assert hasattr(lark, k), f"missing {k}"

    def test_send_method_still_works(self, tmp_path, monkeypatch):
        """向后兼容:send(session_id, text) 仍走 reply path。"""
        ch = _make_lark(monkeypatch, tmp_path)
        ch._reply_to_lark = AsyncMock()  # type: ignore[method-assign]
        ch._last_msg_id["lark:oc_x:ou_a"] = "om_orig"
        asyncio.run(ch.send("lark:oc_x:ou_a", "hello"))
        ch._reply_to_lark.assert_awaited_once_with("om_orig", "hello")

    def test_send_without_last_msg_warns(self, tmp_path, monkeypatch, caplog):
        """没 message_id → warn + 跳过(不抛)。"""
        import logging
        ch = _make_lark(monkeypatch, tmp_path)
        ch._reply_to_lark = AsyncMock()  # type: ignore[method-assign]
        with caplog.at_level(logging.WARNING, logger="openclaw.channels.lark"):
            asyncio.run(ch.send("lark:no_chat:ou_a", "hi"))
        ch._reply_to_lark.assert_not_awaited()

    def test_ruff_async_rule_clean(self):
        """ruff --select ASYNC 0 错(防止 async 内阻塞 IO)。"""
        import subprocess
        result = subprocess.run(
            [sys.executable, "-m", "ruff", "check",
             "openclaw/channels/lark.py",
             "--select", "ASYNC", "--output-format=concise"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        if result.returncode != 0 and "No module named ruff" in result.stderr:
            pytest.skip(f"ruff 未装: {result.stderr.strip()}")
        assert result.returncode == 0, (
            f"ruff ASYNC 错:\n{result.stdout}\n{result.stderr}"
        )
