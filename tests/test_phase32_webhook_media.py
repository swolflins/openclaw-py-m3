"""Phase 32 测试 — 飞书 Webhook 完整路由 + 入站媒体下载。

参考 Hermes Feishu 4594-4608(HTTP 入口)与 4609+ (媒体下载),
openclaw-py 现在落地了:
- aiohttp webhook 端点(URL verification / signature / rate-limit / body size)
- 入站媒体(image / file / audio / video)从飞书拉资源,落盘到本地缓存
- 滑窗限流 + Webhook 异常追踪 + 401-token / 401-sig / 415 / 413 / 429 / 408 / 400 全部覆盖
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


# ============================================================
# 共享 fixture
# ============================================================

def _real_secret(v: str) -> Any:
    from pydantic import SecretStr
    return SecretStr(v)


def _make_lark(monkeypatch, tmp_path, **overrides: Any):
    """构造 LarkChannel(媒体目录指向 tmp_path/dedup 也指向 tmp_path)。"""
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


def _make_request(
    *,
    remote: str = "1.2.3.4",
    body: bytes = b"{}",
    content_type: str = "application/json",
    content_length: int | None = None,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    """造一个 aiohttp-like request(供 _handle_webhook_request 用)。

    我们的代码只用 ``request.remote`` / ``request.headers`` / ``request.content_length`` /
    ``await request.read()``,所以 aiohttp.web.Request 之外的 stub 即可。
    """
    if content_length is None:
        content_length = len(body)

    class _Req:
        pass

    r = _Req()
    r.remote = remote
    headers: dict[str, str] = {"Content-Type": content_type}
    if extra_headers:
        headers.update(extra_headers)
    r.headers = headers
    r.content_length = content_length
    r._body = body

    async def _read() -> bytes:
        return r._body

    r.read = _read
    return r


# ============================================================
# A. Webhook 路由层 — 守卫 / 状态码
# ============================================================

class TestAWebhookGuards:
    def test_unknown_content_type_returns_415(self, tmp_path, monkeypatch):
        ch = _make_lark(monkeypatch, tmp_path)
        req = _make_request(content_type="text/plain", body=b"hi")
        resp = asyncio.run(ch._handle_webhook_request(req))
        assert resp.status == 415

    def test_oversize_content_length_returns_413(self, tmp_path, monkeypatch):
        ch = _make_lark(monkeypatch, tmp_path)
        req = _make_request(
            content_type="application/json",
            body=b"{}",
            content_length=10 * 1024 * 1024,  # 10 MiB,>1 MiB
        )
        resp = asyncio.run(ch._handle_webhook_request(req))
        assert resp.status == 413

    def test_oversize_actual_body_returns_413(self, tmp_path, monkeypatch):
        """Content-Length 撒谎时,实测 body 仍能 413。"""
        ch = _make_lark(monkeypatch, tmp_path)
        big = b"x" * (2 * 1024 * 1024)
        req = _make_request(content_type="application/json", body=big, content_length=10)
        resp = asyncio.run(ch._handle_webhook_request(req))
        assert resp.status == 413

    def test_invalid_json_returns_400(self, tmp_path, monkeypatch):
        ch = _make_lark(monkeypatch, tmp_path)
        req = _make_request(content_type="application/json", body=b"not json")
        resp = asyncio.run(ch._handle_webhook_request(req))
        assert resp.status == 400

    def test_rate_limit_triggers_429(self, tmp_path, monkeypatch):
        """打满窗口后,第 N+1 次返回 429。"""
        ch = _make_lark(monkeypatch, tmp_path)
        # 让 _check_webhook_rate_limit 总是返 False
        ch._check_webhook_rate_limit = lambda key: False  # type: ignore[method-assign]
        req = _make_request()
        resp = asyncio.run(ch._handle_webhook_request(req))
        assert resp.status == 429

    def test_url_verification_echoes_challenge(self, tmp_path, monkeypatch):
        """type=url_verification → 200 + echo challenge,不走 token 校验(优先于其他守卫)。"""
        # 注意:Phase 32 设计是 token 校验在 verification 之前,所以无 token 配置也能过
        ch = _make_lark(monkeypatch, tmp_path)
        req = _make_request(body=json.dumps({
            "type": "url_verification",
            "challenge": "echo-me-back",
        }).encode())
        resp = asyncio.run(ch._handle_webhook_request(req))
        assert resp.status == 200
        body = json.loads(resp.text)
        assert body["challenge"] == "echo-me-back"

    def test_encrypt_payload_rejected(self, tmp_path, monkeypatch):
        """encrypt= 字段(加密回调)→ 400,暂未支持。"""
        ch = _make_lark(monkeypatch, tmp_path)
        req = _make_request(body=json.dumps({
            "encrypt": "ciphertext-xyz",
        }).encode())
        resp = asyncio.run(ch._handle_webhook_request(req))
        assert resp.status == 400

    def test_valid_event_returns_200(self, tmp_path, monkeypatch):
        """有效 im.message.receive_v1 事件 → 200。"""
        ch = _make_lark(monkeypatch, tmp_path)
        # 屏蔽 dispatch 副作用
        ch._dispatch_webhook_event = AsyncMock()  # type: ignore[method-assign]
        req = _make_request(body=json.dumps({
            "header": {"event_type": "im.message.receive_v1"},
            "event": {"sender": {"sender_id": {"open_id": "ou_x"}},
                      "message": {"message_id": "om_xx", "chat_id": "oc_c",
                                  "chat_type": "p2p", "message_type": "text",
                                  "content": json.dumps({"text": "hi"})}},
        }).encode())
        resp = asyncio.run(ch._handle_webhook_request(req))
        assert resp.status == 200
        body = json.loads(resp.text)
        assert body["code"] == 0
        ch._dispatch_webhook_event.assert_awaited_once()


# ============================================================
# B. Verification token + 签名
# ============================================================

class TestBWebhookAuth:
    def test_missing_token_returns_401(self, tmp_path, monkeypatch):
        ch = _make_lark(monkeypatch, tmp_path,
                        verification_token=_real_secret("vtok_xyz"))
        req = _make_request(body=json.dumps({
            "header": {"event_type": "im.message.receive_v1", "token": "wrong"},
        }).encode())
        resp = asyncio.run(ch._handle_webhook_request(req))
        assert resp.status == 401

    def test_correct_token_passes(self, tmp_path, monkeypatch):
        ch = _make_lark(monkeypatch, tmp_path,
                        verification_token=_real_secret("vtok_xyz"))
        ch._dispatch_webhook_event = AsyncMock()  # type: ignore[method-assign]
        req = _make_request(body=json.dumps({
            "header": {"event_type": "im.message.receive_v1", "token": "vtok_xyz"},
            "event": {"sender": {"sender_id": {"open_id": "ou_x"}},
                      "message": {"message_id": "om_xx", "chat_id": "oc_c",
                                  "chat_type": "p2p", "message_type": "text",
                                  "content": json.dumps({"text": "hi"})}},
        }).encode())
        resp = asyncio.run(ch._handle_webhook_request(req))
        assert resp.status == 200

    def test_signature_wrong_returns_401(self, tmp_path, monkeypatch):
        ch = _make_lark(monkeypatch, tmp_path,
                        verification_token=_real_secret("vtok_xyz"),
                        encrypt_key=_real_secret("ek_secret"))
        body_str = json.dumps({"header": {"event_type": "im.message.receive_v1"}})
        req = _make_request(
            body=body_str.encode(),
            extra_headers={
                "x-lark-request-timestamp": "1700000000",
                "x-lark-request-nonce": "abc",
                "x-lark-signature": "bogus-signature",
            },
        )
        resp = asyncio.run(ch._handle_webhook_request(req))
        assert resp.status == 401

    def test_signature_correct_passes(self, tmp_path, monkeypatch):
        ch = _make_lark(monkeypatch, tmp_path,
                        verification_token=_real_secret("vtok_xyz"),
                        encrypt_key=_real_secret("ek_secret"))
        body_str = json.dumps({
            "header": {"event_type": "im.message.receive_v1", "token": "vtok_xyz"},
            "event": {"sender": {"sender_id": {"open_id": "ou_x"}},
                      "message": {"message_id": "om_xx", "chat_id": "oc_c",
                                  "chat_type": "p2p", "message_type": "text",
                                  "content": json.dumps({"text": "hi"})}},
        })
        ts, nonce, ek = "1700000000", "abc", "ek_secret"
        sig = base64.b64encode(
            hashlib.sha256(f"{ts}{nonce}{ek}{body_str}".encode()).digest()
        ).decode("ascii")
        ch._dispatch_webhook_event = AsyncMock()  # type: ignore[method-assign]
        req = _make_request(
            body=body_str.encode(),
            extra_headers={
                "x-lark-request-timestamp": ts,
                "x-lark-request-nonce": nonce,
                "x-lark-signature": sig,
            },
        )
        resp = asyncio.run(ch._handle_webhook_request(req))
        assert resp.status == 200

    def test_token_compares_with_hmac(self, tmp_path, monkeypatch):
        """verify_webhook_token 走 hmac.compare_digest。"""
        from openclaw.channels.lark import verify_webhook_token
        assert verify_webhook_token("a", "a") is True
        assert verify_webhook_token("a", "b") is False
        assert verify_webhook_token("", "a") is False
        assert verify_webhook_token(None, "a") is False


# ============================================================
# C. 限流 / 异常追踪
# ============================================================

class TestCRateLimitAndAnomaly:
    def test_rate_limit_sliding_window(self, tmp_path, monkeypatch):
        """窗口内连续 N 次放行,第 N+1 拒。"""
        ch = _make_lark(monkeypatch, tmp_path)
        from openclaw.channels import lark
        monkeypatch.setattr(lark, "WEBHOOK_RATE_LIMIT_MAX", 3)
        monkeypatch.setattr(lark, "WEBHOOK_RATE_LIMIT_WINDOW_SECONDS", 60)
        key = "cli_test_app:/lark/webhook:1.2.3.4"
        assert ch._check_webhook_rate_limit(key) is True
        assert ch._check_webhook_rate_limit(key) is True
        assert ch._check_webhook_rate_limit(key) is True
        # 第 4 次应拒
        assert ch._check_webhook_rate_limit(key) is False

    def test_rate_limit_window_expiry(self, tmp_path, monkeypatch):
        """窗口外的旧记录被忽略。"""
        ch = _make_lark(monkeypatch, tmp_path)
        from openclaw.channels import lark
        monkeypatch.setattr(lark, "WEBHOOK_RATE_LIMIT_MAX", 2)
        monkeypatch.setattr(lark, "WEBHOOK_RATE_LIMIT_WINDOW_SECONDS", 1)
        key = "k1"
        # 手动塞两条过期记录
        ch._webhook_rate[key] = [time.time() - 100, time.time() - 100]
        # 应放行(过期的被清掉)
        assert ch._check_webhook_rate_limit(key) is True

    def test_rate_limit_max_keys_eviction(self, tmp_path, monkeypatch):
        """超 keys 上限 → 驱逐最旧。"""
        ch = _make_lark(monkeypatch, tmp_path)
        from openclaw.channels import lark
        monkeypatch.setattr(lark, "WEBHOOK_RATE_LIMIT_MAX_KEYS", 3)
        for k in ["k1", "k2", "k3", "k4", "k5"]:
            ch._check_webhook_rate_limit(k)
        assert len(ch._webhook_rate) <= 3

    def test_rate_limit_429_records_anomaly(self, tmp_path, monkeypatch):
        """429 路径应 record_webhook_anomaly。"""
        ch = _make_lark(monkeypatch, tmp_path)
        ch._check_webhook_rate_limit = lambda k: False  # type: ignore[method-assign]
        with patch("openclaw.channels.lark.record_webhook_anomaly") as mock_rec:
            req = _make_request()
            asyncio.run(ch._handle_webhook_request(req))
        mock_rec.assert_called_once()
        args, _ = mock_rec.call_args
        assert args[1] == "429"

    def test_success_clears_anomaly(self, tmp_path, monkeypatch):
        """成功的事件应 clear_webhook_anomaly(防止计数累计)。"""
        ch = _make_lark(monkeypatch, tmp_path)
        ch._dispatch_webhook_event = AsyncMock()  # type: ignore[method-assign]
        with patch("openclaw.channels.lark.clear_webhook_anomaly") as mock_clr, \
             patch("openclaw.channels.lark.record_webhook_anomaly") as mock_rec:
            req = _make_request(body=json.dumps({
                "header": {"event_type": "im.message.receive_v1"},
                "event": {"sender": {"sender_id": {"open_id": "ou_x"}},
                          "message": {"message_id": "om_xx", "chat_id": "oc_c",
                                      "chat_type": "p2p", "message_type": "text",
                                      "content": json.dumps({"text": "hi"})}},
            }).encode())
            asyncio.run(ch._handle_webhook_request(req))
        mock_clr.assert_called_once()
        mock_rec.assert_not_called()


# ============================================================
# D. webhook 事件分发(dedup / allowlist / reaction 复用)
# ============================================================

class TestDWebhookEventDispatch:
    def test_message_event_calls_handle_event(self, tmp_path, monkeypatch):
        """im.message.receive_v1 → _handle_event。"""
        ch = _make_lark(monkeypatch, tmp_path)
        ch._handle_event = AsyncMock()  # type: ignore[method-assign]
        payload = {
            "header": {"event_type": "im.message.receive_v1"},
            "event": {"sender": {"sender_id": {"open_id": "ou_x"}},
                      "message": {"message_id": "om_xx", "chat_id": "oc_c",
                                  "chat_type": "p2p", "message_type": "text",
                                  "content": json.dumps({"text": "hi"})}},
        }
        asyncio.run(ch._dispatch_webhook_event("im.message.receive_v1", payload))
        ch._handle_event.assert_awaited_once()

    def test_reaction_event_calls_reaction_handler(self, tmp_path, monkeypatch):
        ch = _make_lark(monkeypatch, tmp_path)
        ch._handle_reaction_event = AsyncMock()  # type: ignore[method-assign]
        asyncio.run(ch._dispatch_webhook_event(
            "im.message.reaction.created_v1", {"event": {}}
        ))
        ch._handle_reaction_event.assert_awaited_once_with(
            "im.message.reaction.created_v1", ch._handle_reaction_event.call_args[0][1],
        )

    def test_card_action_calls_card_handler(self, tmp_path, monkeypatch):
        ch = _make_lark(monkeypatch, tmp_path)
        ch._handle_card_action = AsyncMock()  # type: ignore[method-assign]
        asyncio.run(ch._dispatch_webhook_event("card.action.trigger", {"event": {}}))
        ch._handle_card_action.assert_awaited_once()

    def test_unknown_event_silently_ignored(self, tmp_path, monkeypatch):
        ch = _make_lark(monkeypatch, tmp_path)
        ch._handle_event = AsyncMock()
        ch._handle_reaction_event = AsyncMock()
        ch._handle_card_action = AsyncMock()
        # 不抛
        asyncio.run(ch._dispatch_webhook_event("something.unmapped", {"event": {}}))
        ch._handle_event.assert_not_awaited()
        ch._handle_reaction_event.assert_not_awaited()
        ch._handle_card_action.assert_not_awaited()

    def test_dispatch_failure_does_not_crash(self, tmp_path, monkeypatch):
        ch = _make_lark(monkeypatch, tmp_path)
        ch._handle_event = AsyncMock(side_effect=RuntimeError("boom"))
        # 不抛
        asyncio.run(ch._dispatch_webhook_event("im.message.receive_v1", {"event": {}}))


# ============================================================
# E. 媒体下载
# ============================================================

class TestEMediaDownload:
    def test_media_dir_disabled_when_empty_string(self, tmp_path, monkeypatch):
        """media_dir='' → 完全关闭。"""
        ch = _make_lark(monkeypatch, tmp_path, media_dir="")
        assert ch._media_dir is None

    def test_media_dir_default_created(self, tmp_path, monkeypatch):
        """media_dir 不传 → 默认 ~/.openclaw/lark_media(或 env 覆盖)。"""
        monkeypatch.setenv("OPENCLAW_LARK_MEDIA_DIR", str(tmp_path / "media_default"))
        from openclaw.config.settings import LarkSettings
        from openclaw.agent.loop import AgentLoop  # type: ignore
        from openclaw.channels.lark import LarkChannel
        s = LarkSettings(app_id="cli_x", app_secret=_real_secret("sec"),
                        dedup_path=str(tmp_path / "d.json"))
        ch = LarkChannel(MagicMock(spec=AgentLoop), s)
        assert ch._media_dir is not None
        assert (tmp_path / "media_default").exists()

    def test_guess_media_ext(self):
        from openclaw.channels.lark import LarkChannel
        assert LarkChannel._guess_media_ext("image", "img_abc") == ".jpg"
        assert LarkChannel._guess_media_ext("audio", "audio_xyz") == ".mp3"
        assert LarkChannel._guess_media_ext("video", "v_1") == ".mp4"
        assert LarkChannel._guess_media_ext("file", "report.pdf") == ".pdf"
        assert LarkChannel._guess_media_ext("file", "weird.xyz.tar") == ".tar"
        # 太长扩展名 → 回退
        assert LarkChannel._guess_media_ext("file", "a.toolongext") == ".bin"

    def test_extract_file_key(self):
        from openclaw.channels.lark import LarkChannel
        assert LarkChannel._extract_file_key(SimpleNamespace(
            content=json.dumps({"image_key": "img_1"})
        )) == "img_1"
        assert LarkChannel._extract_file_key(SimpleNamespace(
            content=json.dumps({"file_key": "f_2", "file_name": "x.pdf"})
        )) == "f_2"
        # 无 key
        assert LarkChannel._extract_file_key(SimpleNamespace(content="{}")) == ""
        # 非法 JSON
        assert LarkChannel._extract_file_key(SimpleNamespace(content="not json")) == ""

    def test_download_returns_none_when_disabled(self, tmp_path, monkeypatch):
        ch = _make_lark(monkeypatch, tmp_path, media_dir="")
        result = asyncio.run(ch._download_inbound_media("om_1", "img_1", message_type="image"))
        assert result is None

    def test_download_returns_none_on_http_error(self, tmp_path, monkeypatch):
        ch = _make_lark(monkeypatch, tmp_path)
        # 拿不到 token(因为 app_id/secret 是 fake)→ 返回 None,不抛
        result = asyncio.run(ch._download_inbound_media("om_1", "img_1", message_type="image"))
        assert result is None

    def test_download_returns_none_on_empty_keys(self, tmp_path, monkeypatch):
        ch = _make_lark(monkeypatch, tmp_path)
        result = asyncio.run(ch._download_inbound_media("", "", message_type="image"))
        assert result is None

    def test_download_oversize_rejected(self, tmp_path, monkeypatch):
        ch = _make_lark(monkeypatch, tmp_path)
        from openclaw.channels import lark
        monkeypatch.setattr(lark, "LARK_MEDIA_MAX_BYTES", 100)  # 100 B 上限
        # 拿不到 token 不会真下载,所以这个测试只是覆盖"代码路径不崩"
        result = asyncio.run(ch._download_inbound_media("om_b", "k_b", message_type="image"))
        assert result is None

    def test_download_uses_https_only(self, tmp_path, monkeypatch):
        """_download_inbound_media 内部 URL 应是 HTTPS(SSRF 防御)。"""
        ch = _make_lark(monkeypatch, tmp_path)
        # 拦截 _get_tenant_token
        ch._get_tenant_token = AsyncMock(return_value="t")  # type: ignore[method-assign]
        captured_urls: list[str] = []

        class FakeAsyncClient:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            def stream(self, method, url, **kw):
                captured_urls.append(url)
                class FakeStream:
                    async def __aenter__(self_inner): return self_inner
                    async def __aexit__(self_inner, *a): return False
                    status_code = 404
                    headers = {}
                return FakeStream()
        with patch("httpx.AsyncClient", FakeAsyncClient):
            asyncio.run(ch._download_inbound_media("om_u", "k_u", message_type="image"))
        assert len(captured_urls) == 1
        assert captured_urls[0].startswith("https://open.feishu.cn/")


# ============================================================
# F. 入站媒体集成到 _handle_event
# ============================================================

class TestFMediaInHandleEvent:
    def test_image_message_downloads_and_dispatches(self, tmp_path, monkeypatch):
        """image 类型 → 走 _download_inbound_media → metadata 拿到路径 + 占位符文本。"""
        ch = _make_lark(monkeypatch, tmp_path)
        ch._bot_open_id = "ou_bot"
        ch._get_tenant_token = AsyncMock(return_value="t")  # type: ignore[method-assign]

        # 拦截下载,返回伪造路径
        fake_path = tmp_path / "image_om_1_img_k1.jpg"
        ch._download_inbound_media = AsyncMock(return_value=fake_path)  # type: ignore[method-assign]
        # 屏蔽 reaction 副作用
        ch._add_processing_reaction = AsyncMock()  # type: ignore[method-assign]
        ch._remove_processing_reaction = AsyncMock()  # type: ignore[method-assign]
        ch._replace_processing_reaction = AsyncMock()  # type: ignore[method-assign]

        captured: list[Any] = []

        async def fake_dispatch(msg):
            captured.append(msg)

        ch.dispatch = fake_dispatch  # type: ignore[method-assign]

        evt = SimpleNamespace(event=SimpleNamespace(
            sender=SimpleNamespace(sender_id=SimpleNamespace(open_id="ou_a")),
            message=SimpleNamespace(
                message_id="om_1", chat_id="oc_c", chat_type="p2p",
                message_type="image",
                content=json.dumps({"image_key": "img_k1"}),
                mentions=[],
            ),
        ))
        asyncio.run(ch._handle_event(evt))

        ch._download_inbound_media.assert_awaited_once_with(
            "om_1", "img_k1", message_type="image",
        )
        assert len(captured) == 1
        msg = captured[0]
        assert msg.text == "[image:img_k1]"  # 占位符
        assert msg.metadata["media_paths"] == [str(fake_path)]
        assert msg.metadata["media_type"] == "image"

    def test_image_download_failure_dispatches_with_empty_paths(self, tmp_path, monkeypatch):
        """下载失败 → metadata.media_paths=[] 占位符文本仍 dispatch。"""
        ch = _make_lark(monkeypatch, tmp_path)
        ch._bot_open_id = "ou_bot"
        ch._download_inbound_media = AsyncMock(return_value=None)  # type: ignore[method-assign]
        ch._add_processing_reaction = AsyncMock()  # type: ignore[method-assign]
        ch._remove_processing_reaction = AsyncMock()  # type: ignore[method-assign]
        ch._replace_processing_reaction = AsyncMock()  # type: ignore[method-assign]

        captured: list[Any] = []

        async def fake_dispatch(msg):
            captured.append(msg)

        ch.dispatch = fake_dispatch  # type: ignore[method-assign]

        evt = SimpleNamespace(event=SimpleNamespace(
            sender=SimpleNamespace(sender_id=SimpleNamespace(open_id="ou_a")),
            message=SimpleNamespace(
                message_id="om_2", chat_id="oc_c", chat_type="p2p",
                message_type="image",
                content=json.dumps({"image_key": "img_k2"}),
                mentions=[],
            ),
        ))
        asyncio.run(ch._handle_event(evt))
        # 仍 dispatch,只是 media_paths 空
        assert len(captured) == 1
        assert captured[0].metadata["media_paths"] == []

    def test_non_media_type_no_download_called(self, tmp_path, monkeypatch):
        """text 类型 → 不调 _download_inbound_media。"""
        ch = _make_lark(monkeypatch, tmp_path)
        ch._bot_open_id = "ou_bot"
        ch._download_inbound_media = AsyncMock()  # type: ignore[method-assign]
        ch._add_processing_reaction = AsyncMock()  # type: ignore[method-assign]
        ch._remove_processing_reaction = AsyncMock()  # type: ignore[method-assign]
        ch._replace_processing_reaction = AsyncMock()  # type: ignore[method-assign]

        captured: list[Any] = []

        async def fake_dispatch(msg):
            captured.append(msg)

        ch.dispatch = fake_dispatch  # type: ignore[method-assign]

        evt = SimpleNamespace(event=SimpleNamespace(
            sender=SimpleNamespace(sender_id=SimpleNamespace(open_id="ou_a")),
            message=SimpleNamespace(
                message_id="om_3", chat_id="oc_c", chat_type="p2p",
                message_type="text",
                content=json.dumps({"text": "hi"}),
                mentions=[],
            ),
        ))
        asyncio.run(ch._handle_event(evt))
        ch._download_inbound_media.assert_not_awaited()
        assert len(captured) == 1
        assert captured[0].metadata["media_paths"] == []
        assert captured[0].metadata["media_type"] is None


# ============================================================
# G. Webhook start() fail-fast & 启动后 stop
# ============================================================

class TestGWebhookStartStop:
    def test_start_fails_without_verification_token(self, tmp_path, monkeypatch):
        """LARK_USE_WS=False 且 verification_token=None → RuntimeError。"""
        ch = _make_lark(monkeypatch, tmp_path, use_ws=False, verification_token=None)
        with pytest.raises(RuntimeError, match="VERIFICATION_TOKEN"):
            asyncio.run(ch.start())

    def test_start_fails_when_aiohttp_missing(self, tmp_path, monkeypatch):
        """LARK_USE_WS=False + aiohttp 没装 → 提示安装 aiohttp。"""
        ch = _make_lark(
            monkeypatch, tmp_path,
            use_ws=False, verification_token=_real_secret("vtok_xyz"),
        )
        # 把 aiohttp import 弄挂
        with patch.dict(sys.modules, {"aiohttp": None, "aiohttp.web": None}):
            with pytest.raises(RuntimeError, match="aiohttp"):
                asyncio.run(ch.start())

    def test_stop_cleans_up_runner(self, tmp_path, monkeypatch):
        """stop() 应调 webhook runner.cleanup。"""
        ch = _make_lark(
            monkeypatch, tmp_path,
            use_ws=False, verification_token=_real_secret("vtok_xyz"),
        )
        fake_runner = MagicMock()
        fake_runner.cleanup = AsyncMock()  # type: ignore[method-assign]
        ch._webhook_runner = fake_runner

        async def main() -> None:
            await ch.stop()
        asyncio.run(main())
        fake_runner.cleanup.assert_awaited_once()
        assert ch._webhook_runner is None


# ============================================================
# H. 鲁棒性 / 配置 / 不与 Phase 31 冲突
# ============================================================

class TestHRobustness:
    def test_lark_settings_webhook_fields_exist(self):
        from openclaw.config.settings import LarkSettings
        s = LarkSettings()
        assert hasattr(s, "webhook_host")
        assert hasattr(s, "webhook_port")
        assert hasattr(s, "webhook_path")
        assert hasattr(s, "media_dir")
        assert s.webhook_port == 9000
        assert s.webhook_path == "/lark/webhook"

    def test_phase31_dedup_still_works(self, tmp_path, monkeypatch):
        """Phase 32 没有破坏 Phase 31 的去重 + per-chat 锁。"""
        ch = _make_lark(monkeypatch, tmp_path)
        assert asyncio.run(ch._is_duplicate("m_test")) is False
        assert asyncio.run(ch._is_duplicate("m_test")) is True
        lock = ch._get_chat_lock("c_test")
        assert isinstance(lock, asyncio.Lock)

    def test_webhook_mode_constants_exported(self):
        from openclaw.channels import lark
        for k in (
            "WEBHOOK_DEFAULT_HOST", "WEBHOOK_DEFAULT_PORT",
            "WEBHOOK_DEFAULT_PATH", "WEBHOOK_MAX_BODY_BYTES",
            "WEBHOOK_BODY_TIMEOUT_SECONDS",
            "WEBHOOK_RATE_LIMIT_MAX", "WEBHOOK_RATE_LIMIT_WINDOW_SECONDS",
            "WEBHOOK_RATE_LIMIT_MAX_KEYS",
            "LARK_MEDIA_DEFAULT_DIR", "LARK_MEDIA_MAX_BYTES",
            "LARK_MEDIA_CACHE_TTL_SECONDS", "LARK_MEDIA_TYPES",
        ):
            assert hasattr(lark, k), f"missing {k}"

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
