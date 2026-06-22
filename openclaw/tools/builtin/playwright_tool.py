"""Playwright browser tool(Phase 21) — 浏览器自动化。

**设计原则**:
- 同步 API(playwright 是 sync-friendly);外层用 asyncio.to_thread 包装
- 单例 browser,所有调用复用(启动很慢,约 1-3 秒)
- 不强依赖 playwright — 没装时 register 抛 ImportError(让上层 catch)
- 提供 3 个核心动作:browse_url / click / extract_text
- URL 走白名单(防 agent 访问任意站点,security)
- 默认 30s timeout,防 hang

**为什么不直接用 sync_api**:让所有 tool 都 async 更一致(不混用 event loop)。
"""
from __future__ import annotations

import asyncio
import os
import urllib.parse
from typing import Any, Optional

from openclaw.core.logging import get_logger
from openclaw.tools.registry import ToolCategory, ToolPermission, ToolRegistry

logger = get_logger(__name__)

# ──────────── 默认白名单(只读常用站点)────────────
# 生产环境用户应该用 config 覆盖;这里给个安全的默认。
DEFAULT_ALLOWED_DOMAINS = [
    "github.com",         # 文档
    "stackoverflow.com",  # 文档
    "python.org",         # python 官方
    "openai.com",         # openai 文档
    "anthropic.com",      # anthropic 文档
    "docs.python.org",
    "en.wikipedia.org",
]


def _try_import_playwright():
    """尝试 import playwright — 失败时抛带 hint 的 ImportError。"""
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
        return sync_playwright
    except ImportError as e:
        raise ImportError(
            "playwright 未安装。请先运行:\n"
            "  pip install playwright\n"
            "  playwright install chromium\n"
            f"原始错误: {e}"
        )


def _is_url_allowed(url: str, allowed_domains: list[str]) -> bool:
    """校验 url 在白名单内(host 部分精确或后缀匹配)。"""
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    for d in allowed_domains:
        d = d.lower().lstrip(".")
        if host == d or host.endswith("." + d):
            return True
    return False


class _BrowserHolder:
    """单例 Browser — 跨多次调用复用(启动太慢,不能每次开新进程)。

    必须在 event loop 外启动 sync_playwright;所有调用走 asyncio.to_thread。
    """
    _pw: Any = None
    _browser: Any = None
    _lock: Optional[asyncio.Lock] = None  # 懒初始化

    @classmethod
    async def get_browser(cls):
        """懒启动 chromium,返回 (playwright instance, browser)。

        第一次调用慢(2-3 秒);之后快。
        """
        if cls._lock is None:
            cls._lock = asyncio.Lock()
        async with cls._lock:
            if cls._browser is not None and cls._browser.is_connected():
                return cls._browser
            sync_playwright = _try_import_playwright()
            # sync_playwright() 必须在 thread 里跑(它是 sync)
            def _start():
                cls._pw = sync_playwright().start()
                # M12 修复:``--no-sandbox`` 是 chromium 在 root / 受限容器里
                # 启动所必需的(否则内核 SUID sandbox 拒绝子进程);
                # 但它同时关闭浏览器自身的进程隔离,可能放大 RCE 影响。
                # 行为:
                # 1) 默认(Docker 容器场景):保留 ``--no-sandbox``(历史行为)
                # 2) ``OPENCLAW_PLAYWRIGHT_NO_SANDBOX=0`` 显式关 → 不传 --no-sandbox
                #    (用户在非 root / 已有 seccomp 配置时,可以开 sandbox 加强隔离)
                # 3) 显式 ``=1`` 与省略等价(向后兼容,留作"显式"意图)
                chromium_args = ["--disable-dev-shm-usage"]
                no_sandbox = os.environ.get(
                    "OPENCLAW_PLAYWRIGHT_NO_SANDBOX", "1"  # 历史行为:默认开
                )
                if no_sandbox.lower() not in ("0", "false", "no", "off"):
                    chromium_args.append("--no-sandbox")
                cls._browser = cls._pw.chromium.launch(
                    headless=True,
                    args=chromium_args,
                )
                return cls._browser
            browser = await asyncio.to_thread(_start)
            return browser

    @classmethod
    async def close(cls) -> None:
        if cls._browser is not None:
            try:
                await asyncio.to_thread(cls._browser.close)
            except Exception:  # noqa: BLE001
                pass
            cls._browser = None
        if cls._pw is not None:
            try:
                await asyncio.to_thread(cls._pw.stop)
            except Exception:  # noqa: BLE001
                pass
            cls._pw = None


def register_browser_tools(
    registry: ToolRegistry,
    *,
    allowed_domains: Optional[list[str]] = None,
    headless: bool = True,
    default_timeout_ms: int = 30_000,
) -> None:
    """注册 browser 工具。

    工具:
    - browse_url:打开 URL,提取页面文本(前 N 字符)+ title
    - browser_extract_text:在当前页面(或新页面)提取特定 selector 文本
    - browser_close:显式关掉 browser(节省资源,默认单例不关)
    """
    if allowed_domains is None:
        allowed_domains = list(DEFAULT_ALLOWED_DOMAINS)

    @registry.tool(category=ToolCategory.UTILITY, permission=ToolPermission.NETWORK)
    async def browse_url(url: str, max_chars: int = 5000) -> str:
        """打开一个 URL,返回 page title + 文本内容(前 max_chars 字符)。

        URL 必须在白名单内(防 agent 访问任意站点)。白名单默认含 github/python.org/wikipedia 等。
        """
        if not _is_url_allowed(url, allowed_domains):
            return f"[error] URL not in allow-list ({allowed_domains}): {url}"
        browser = await _BrowserHolder.get_browser()

        def _browse():
            page = browser.new_page()
            try:
                page.set_default_timeout(default_timeout_ms)
                resp = page.goto(url, wait_until="domcontentloaded")
                status = resp.status if resp else 0
                title = page.title()
                text = page.evaluate("() => document.body.innerText")
                if len(text) > max_chars:
                    text = text[:max_chars] + f"\n\n... (truncated at {max_chars} chars)"
                return f"title: {title}\nstatus: {status}\nurl: {page.url}\n\n{text}"
            finally:
                page.close()

        return await asyncio.to_thread(_browse)

    @registry.tool(category=ToolCategory.UTILITY, permission=ToolPermission.NETWORK)
    async def browser_extract_text(
        url: str,
        selector: str,
        max_chars: int = 3000,
    ) -> str:
        """打开 URL,提取符合 CSS selector 的第一个元素的文本。

        selector 例:'h1'、'article'、'#main'、'.content'。
        """
        if not _is_url_allowed(url, allowed_domains):
            return f"[error] URL not in allow-list: {url}"
        browser = await _BrowserHolder.get_browser()

        def _extract():
            page = browser.new_page()
            try:
                page.set_default_timeout(default_timeout_ms)
                page.goto(url, wait_until="domcontentloaded")
                element = page.query_selector(selector)
                if element is None:
                    return f"[error] selector not found: {selector}"
                text = element.inner_text()[:max_chars]
                return f"selector: {selector}\ntext:\n{text}"
            finally:
                page.close()

        return await asyncio.to_thread(_extract)

    @registry.tool(category=ToolCategory.UTILITY, permission=ToolPermission.SAFE)
    async def browser_close() -> str:
        """显式关闭 browser(默认单例,只在 end-of-session 时调)。"""
        await _BrowserHolder.close()
        return "browser closed"

    logger.info("browser tools registered", extra={"allowed_domains": allowed_domains})
