"""Phase 25 / A2: Discord webhook 验签 pynacl fail-closed 单测。

**背景(安全洞)**:
``openclaw/channels/discord.py`` 旧版的 ``verify_signature`` 在 pynacl 缺失时 ``return True``
→ 放行所有 webhook。生产部署忘装 pynacl 时,任何人都能伪造 Discord interaction
(把恶意指令推进 IncomingMessage 管道)。

**修复**:
1. ``verify_signature``: pynacl 缺失时 ``return False`` + log error(fail-closed)
2. ``__init__`` / ``start``: production 模式 + 配了 public_key + pynacl 缺失 → RuntimeError

**测试覆盖**:
- test_pynacl_missing_valid_sig_rejects     → pynacl missing + 看似合法 sig → False(返 400)
- test_pynacl_missing_production_init_raises → production + pynacl missing + public_key → RuntimeError
- test_pynacl_available_valid_sig_accepts    → pynacl in + 合法 Ed25519 sig → True(返 200)
- test_pynacl_available_invalid_sig_rejects  → pynacl in + 错误 sig → False(返 400)
- test_pynacl_missing_dev_mode_no_raise      → 额外: dev 模式 + missing pynacl 不应阻断 init
- test_pynacl_missing_no_public_key_still_open → 额外: 配 public_key=False → 始终放行(本地 dev)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ─────── Helpers ───────

def _make_fake_agent():
    """替身 AgentLoop,只满足 BaseChannel 构造需要。"""

    async def handle(self, session_id, text, **kw):
        return MagicMock(content=f"echo:{text}", tool_calls=[], iterations=1)

    async def new_session(self, sid=None):
        return sid or "s"

    return MagicMock(handle=handle, new_session=new_session)


def _gen_ed25519_keypair() -> tuple[bytes, bytes]:
    """生成一对 Ed25519 密钥,返回 (private_seed_hex, public_key_hex)。"""
    from nacl.signing import SigningKey

    sk = SigningKey.generate()
    return sk.encode().hex(), sk.verify_key.encode().hex()


def _sign(private_seed_hex: str, message: bytes) -> str:
    """用 private seed 签 message,返 hex 字符串。"""
    from nacl.signing import SigningKey

    sk = SigningKey(bytes.fromhex(private_seed_hex))
    return sk.sign(message).signature.hex()


# ─────── 核心 4 个测试 ───────


def test_pynacl_missing_valid_sig_rejects(monkeypatch):
    """pynacl 缺失 + 看似合法的 sig → 必须 return False(不再 fail-open)。

    修复前会 return True(放行所有 webhook),这是 **安全洞**。
    修复后: 即便 signature 长得很像,也会被拒(400)。
    """
    # 让 _has_pynacl() 返 False,且 'from nacl.signing import VerifyKey' 抛 ImportError
    import openclaw.channels.discord as dmod

    monkeypatch.setattr(dmod, "_has_pynacl", lambda: False)

    # 把 nacl 整个屏蔽掉(保险: 即便 _has_pynacl 被绕过,import 也会失败)
    for mod_name in list(sys.modules):
        if mod_name == "nacl" or mod_name.startswith("nacl."):
            monkeypatch.delitem(sys.modules, mod_name, raising=False)
    # 让 'import nacl' / 'from nacl.signing import X' 抛 ImportError
    import builtins

    real_import = builtins.__import__

    def _blocked(name, *a, **kw):
        if name == "nacl" or name.startswith("nacl."):
            raise ImportError(f"[phase25-test] simulated missing pynacl ({name})")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _blocked)

    # 用 mock logger 验证"有 error 日志被发出"(structlog → stdlib,caplog 不一定兜得住,
    # 所以直接 patch logger 对象)。
    error_calls: list[dict] = {}

    class _FakeLogger:
        def error(self, *a, **kw):
            error_calls.setdefault("error", []).append((a, kw))

        def warning(self, *a, **kw):
            error_calls.setdefault("warning", []).append((a, kw))

        def info(self, *a, **kw):
            pass

        def exception(self, *a, **kw):
            error_calls.setdefault("error", []).append((a, kw))

    monkeypatch.setattr(dmod, "logger", _FakeLogger())

    ch = dmod.DiscordChannel(
        token="x", agent_loop=_make_fake_agent(),
        public_key="0" * 64,  # 配了公钥,触发验签路径
    )

    # 给一个"看起来合法"的 sig + timestamp(64 hex chars)
    result = ch.verify_signature(b'{"type":2}', "a" * 128, "1700000000")
    assert result is False, (
        f"FAIL-OPEN 回归! pynacl 缺失时 verify_signature 返 {result!r} "
        f"(应返 False 触发 400)。"
    )
    # 应该有 error log(便于定位)
    assert "error" in error_calls, (
        "应记录 error log 说明 reject 原因,"
        f"实际 logger 调用: {list(error_calls.keys())}"
    )
    flat = " ".join(str(a) for args, _ in error_calls["error"] for a in args)
    assert "pynacl" in flat.lower(), (
        f"error log 应提到 pynacl,实际: {flat!r}"
    )


def test_pynacl_missing_production_init_raises(monkeypatch):
    """production 模式 + pynacl 缺失 + 配了 public_key → __init__ 抛 RuntimeError。

    这是修复 #2 — 启动期 fail-closed,运维忘装 pynacl 时直接拒,服务起不来。
    """
    import openclaw.channels.discord as dmod

    monkeypatch.setattr(dmod, "_has_pynacl", lambda: False)
    monkeypatch.setenv("OPENCLAW_ENV", "production")
    # 双保险:屏蔽掉所有 nacl.* import
    for mod_name in list(sys.modules):
        if mod_name == "nacl" or mod_name.startswith("nacl."):
            monkeypatch.delitem(sys.modules, mod_name, raising=False)
    import builtins

    real_import = builtins.__import__

    def _blocked(name, *a, **kw):
        if name == "nacl" or name.startswith("nacl."):
            raise ImportError(f"[phase25-test] simulated missing pynacl ({name})")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _blocked)

    with pytest.raises(RuntimeError) as ei:
        dmod.DiscordChannel(
            token="x",
            agent_loop=_make_fake_agent(),
            public_key="0" * 64,
        )
    msg = str(ei.value)
    assert "phase25" in msg or "pynacl" in msg.lower(), (
        f"RuntimeError 应明确提到 phase25/pynacl,实际: {msg}"
    )


def test_pynacl_available_valid_sig_accepts():
    """pynacl 装好 + 合法 Ed25519 sig → return True(放行 → 200)。"""
    import openclaw.channels.discord as dmod

    priv_hex, pub_hex = _gen_ed25519_keypair()
    ch = dmod.DiscordChannel(
        token="x", agent_loop=_make_fake_agent(),
        public_key=pub_hex,
    )
    body = b'{"type":2,"data":{"name":"ping"}}'
    ts = "1700000000"
    sig = _sign(priv_hex, ts.encode() + body)
    assert ch.verify_signature(body, sig, ts) is True


def test_pynacl_available_invalid_sig_rejects():
    """pynacl 装好 + 错误 sig → return False(拒 → 400)。"""
    import openclaw.channels.discord as dmod

    _, pub_hex = _gen_ed25519_keypair()
    ch = dmod.DiscordChannel(
        token="x", agent_loop=_make_fake_agent(),
        public_key=pub_hex,
    )
    body = b'{"type":2,"data":{"name":"evil"}}'
    # 用另一个 keypair 签(签名方 ≠ public_key 对应方)
    other_priv, _ = _gen_ed25519_keypair()
    bad_sig = _sign(other_priv, b"1700000000" + body)
    assert ch.verify_signature(body, bad_sig, "1700000000") is False
    # 长度异常的 hex
    assert ch.verify_signature(body, "not_hex_!!", "1700000000") is False


# ─────── 边界 / 回归测试 ───────


def test_pynacl_missing_dev_mode_no_raise(monkeypatch):
    """dev 模式(无 OPENCLAW_ENV)+ pynacl 缺失 + public_key 已配 → 不应抛错。

    修复 #2 只在 production 阻断,本地开发保持兼容。
    """
    import openclaw.channels.discord as dmod

    monkeypatch.delenv("OPENCLAW_ENV", raising=False)
    monkeypatch.delenv("OPENCLAW_GATEWAY_ENV", raising=False)
    monkeypatch.setattr(dmod, "_has_pynacl", lambda: False)

    # 不应抛
    ch = dmod.DiscordChannel(
        token="x", agent_loop=_make_fake_agent(),
        public_key="0" * 64,
    )
    # 但 verify_signature 仍应 fail-closed
    assert ch.verify_signature(b"x", "a" * 128, "1") is False


def test_pynacl_missing_no_public_key_keeps_open(monkeypatch):
    """public_key=None(本地 dev 默认)→ 验签函数始终 return True,行为不变。"""
    import openclaw.channels.discord as dmod

    monkeypatch.setattr(dmod, "_has_pynacl", lambda: False)
    ch = dmod.DiscordChannel(token="x", agent_loop=_make_fake_agent(), public_key=None)
    assert ch.verify_signature(b"any-body", "any-sig", "any-ts") is True


def test_prod_mode_alt_env_var_also_works(monkeypatch):
    """OPENCLAW_GATEWAY_ENV=production 也应触发启动期 fail-closed。

    与 audit/gateway 模块的 env var 习惯保持一致(兼容两种命名)。
    """
    import openclaw.channels.discord as dmod

    monkeypatch.delenv("OPENCLAW_ENV", raising=False)
    monkeypatch.setenv("OPENCLAW_GATEWAY_ENV", "prod")  # 短别名
    monkeypatch.setattr(dmod, "_has_pynacl", lambda: False)

    with pytest.raises(RuntimeError):
        dmod.DiscordChannel(
            token="x",
            agent_loop=_make_fake_agent(),
            public_key="0" * 64,
        )


def test_prod_mode_with_pynacl_present_does_not_raise():
    """production + pynacl 装好 + public_key 配好 → __init__ 不抛。

    防止误伤:正常生产部署应该能起来。
    """
    import openclaw.channels.discord as dmod

    os.environ["OPENCLAW_ENV"] = "production"
    try:
        _, pub_hex = _gen_ed25519_keypair()
        ch = dmod.DiscordChannel(
            token="x", agent_loop=_make_fake_agent(),
            public_key=pub_hex,
        )
        # pynacl 装好 → verify 路径正常
        priv_hex, _ = _gen_ed25519_keypair()
        # 用错误 keypair 签,验证 verify 路径未被绕过
        assert ch.verify_signature(b"x", "0" * 128, "1") is False
    finally:
        os.environ.pop("OPENCLAW_ENV", None)


def test_start_also_checks_pynacl_in_production(monkeypatch):
    """start() 路径上也应 fail-closed(双保险)。"""
    import openclaw.channels.discord as dmod

    monkeypatch.setenv("OPENCLAW_ENV", "production")
    monkeypatch.setattr(dmod, "_has_pynacl", lambda: False)

    # 跳过 __init__ 的检查,直接构造一个已就绪对象
    ch = dmod.DiscordChannel(token="x", agent_loop=_make_fake_agent())
    ch.public_key = "0" * 64  # 模拟配了公钥

    import asyncio
    with pytest.raises(RuntimeError) as ei:
        asyncio.run(ch.start())
    assert "phase25" in str(ei.value).lower() or "pynacl" in str(ei.value).lower()
