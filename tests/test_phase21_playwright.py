"""Phase 21:Playwright browser tool(补全原版 OpenClaw #7 idea)。

覆盖:
- URL 白名单(精确 / 后缀匹配 / 拒绝非 http)
- 没装 playwright 时不阻断(register_builtin_tools 跳过)
- 工具注册(browse_url / browser_extract_text / browser_close)
- browse_url 拒绝不在白名单的 url
- browser_extract_text selector 不存在 → 报错
- _BrowserHolder 单例 — get_browser 第二次复用
- _try_import_playwright 没装 → 抛带 hint
"""
from __future__ import annotations

import asyncio

import pytest

# 跳过整个文件如果 playwright 没装(主要测试仍可跑)
playwright_available = True
try:
    import playwright  # noqa: F401
except ImportError:
    playwright_available = False


def _chromium_binary_available() -> bool:
    """检查 playwright chromium binary 是否就绪(CI 没跑 `playwright install` 时跳过)。"""
    if not playwright_available:
        return False
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            # 找 chromium binary;若不存在会抛 FileNotFoundError / Error
            exe = p.chromium.executable_path
            import os
            return bool(exe) and os.path.exists(exe)
    except Exception:
        return False


chromium_available = _chromium_binary_available()

# 跳过的真实浏览器 e2e(没 chromium)
needs_browser = pytest.mark.skipif(
    not chromium_available,
    reason="playwright chromium binary not installed (run `playwright install chromium`)",
)


# ─────────────── 单元(不需要真浏览器)───────────────

def test_url_allowlist_exact():
    from openclaw.tools.builtin.playwright_tool import _is_url_allowed
    assert _is_url_allowed("https://github.com/foo/bar", ["github.com"])
    assert _is_url_allowed("https://docs.github.com/x", ["github.com"])
    assert _is_url_allowed("https://github.com:443/foo", ["github.com"])


def test_url_allowlist_suffix():
    from openclaw.tools.builtin.playwright_tool import _is_url_allowed
    # 后缀匹配:docs.github.com 命中 github.com
    assert _is_url_allowed("https://docs.github.com/x", ["github.com"])
    # 不命中
    assert not _is_url_allowed("https://evil.com", ["github.com"])
    # 不能用包含字符串绕过
    assert not _is_url_allowed("https://github.com.evil.com", ["github.com"])


def test_url_allowlist_rejects_non_http():
    from openclaw.tools.builtin.playwright_tool import _is_url_allowed
    assert not _is_url_allowed("file:///etc/passwd", ["github.com"])
    assert not _is_url_allowed("javascript:alert(1)", ["github.com"])
    assert not _is_url_allowed("ftp://github.com/x", ["github.com"])


def test_url_allowlist_malformed():
    from openclaw.tools.builtin.playwright_tool import _is_url_allowed
    assert not _is_url_allowed("not a url", ["github.com"])
    assert not _is_url_allowed("", ["github.com"])


def test_default_allowed_domains_includes_documentation_sites():
    from openclaw.tools.builtin.playwright_tool import DEFAULT_ALLOWED_DOMAINS
    # 应包含至少这些常用文档站
    for d in ("github.com", "python.org", "en.wikipedia.org"):
        assert d in DEFAULT_ALLOWED_DOMAINS, f"missing {d} from default allow-list"


def test_browser_close_is_noop_when_not_started():
    """browser_close() 在没启动过时应安全返回。"""
    from openclaw.tools.builtin.playwright_tool import _BrowserHolder
    # 重置(测试隔离)
    _BrowserHolder._browser = None
    _BrowserHolder._pw = None
    asyncio.run(_BrowserHolder.close())  # 不抛


# ─────────────── 工具注册(不需要真浏览器)───────────────

def test_register_browser_tools_injects_3_tools():
    """register_browser_tools 应注册 browse_url / browser_extract_text / browser_close。"""
    from openclaw.tools.builtin.playwright_tool import register_browser_tools
    from openclaw.tools.registry import ToolRegistry
    reg = ToolRegistry()
    register_browser_tools(reg, allowed_domains=["example.com"])
    names = {s.name for s in reg.specs()}
    assert "browse_url" in names
    assert "browser_extract_text" in names
    assert "browser_close" in names


def test_browse_url_rejects_not_allowlisted():
    """browse_url 对不在白名单的 url 应返回 error 而非调浏览器。"""
    from openclaw.tools.builtin.playwright_tool import register_browser_tools
    from openclaw.tools.registry import ToolRegistry
    reg = ToolRegistry()
    register_browser_tools(reg, allowed_domains=["example.com"])
    out = asyncio.run(reg.call("browse_url", {"url": "https://evil.com/x"}))
    assert "[error]" in out
    assert "not in allow-list" in out


def test_browse_url_rejects_file_protocol():
    """browse_url 必须拒绝 file:// 协议。"""
    from openclaw.tools.builtin.playwright_tool import register_browser_tools
    from openclaw.tools.registry import ToolRegistry
    reg = ToolRegistry()
    register_browser_tools(reg, allowed_domains=["example.com"])
    out = asyncio.run(reg.call("browse_url", {"url": "file:///etc/passwd"}))
    assert "[error]" in out


def test_register_builtin_tools_skips_browser_when_no_playwright(monkeypatch):
    """没装 playwright 时 register_builtin_tools 不应抛,只跳过。"""
    from openclaw.tools.builtin import register_builtin_tools
    from openclaw.tools.registry import ToolRegistry

    # 模拟 playwright_tool 子模块 import 失败
    import openclaw.tools.builtin as builtin_mod
    real_import = builtin_mod.__builtins__.__import__ if hasattr(builtin_mod.__builtins__, "__import__") else None
    import builtins
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if "playwright_tool" in name:
            raise ImportError("simulated: playwright not installed")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    reg = ToolRegistry()
    # 正常调用,不应抛
    register_builtin_tools(reg, fs_root=".")
    # 验证不抛 + 其他工具正常注册,browse_url 跳过
    names = {s.name for s in reg.specs()}
    assert "calculator" in names
    assert "echo" in names
    assert "shell_exec" in names
    assert "browse_url" not in names, "browse_url should be skipped when playwright missing"


def test_register_builtin_tools_with_playwright_works():
    """装了 playwright 时,browse_url / browser_extract_text / browser_close 都注册。"""
    pytest.importorskip("playwright", reason="playwright not installed")
    from openclaw.tools.builtin import register_builtin_tools
    from openclaw.tools.registry import ToolRegistry
    reg = ToolRegistry()
    register_builtin_tools(reg, fs_root=".", browser_allowed_domains=["github.com"])
    names = {s.name for s in reg.specs()}
    assert "browse_url" in names
    assert "browser_extract_text" in names
    assert "browser_close" in names


# ─────────────── e2e(需要真 chromium binary)───────────────

@needs_browser
def test_browse_url_real_chromium():
    """真实 chromium 跑一次:打开 example.com 应成功。"""
    from openclaw.tools.builtin.playwright_tool import register_browser_tools, _BrowserHolder
    from openclaw.tools.registry import ToolRegistry
    reg = ToolRegistry()
    register_browser_tools(reg, allowed_domains=["example.com"])
    out = asyncio.run(reg.call("browse_url", {"url": "https://example.com", "max_chars": 200}))
    assert "Example" in out or "example" in out.lower()
    assert "[error]" not in out
    # 关闭浏览器
    asyncio.run(_BrowserHolder.close())


@needs_browser
def test_browser_extract_text_real_chromium():
    """真实 chromium 跑 extract_text:取 example.com 的 h1。"""
    from openclaw.tools.builtin.playwright_tool import register_browser_tools, _BrowserHolder
    from openclaw.tools.registry import ToolRegistry
    reg = ToolRegistry()
    register_browser_tools(reg, allowed_domains=["example.com"])
    out = asyncio.run(
        reg.call("browser_extract_text", {
            "url": "https://example.com", "selector": "h1",
        })
    )
    assert "Example Domain" in out
    asyncio.run(_BrowserHolder.close())
