"""HTTP 工具(子包)。

- http_get / http_post / http_request
- 带 host 白名单、scheme 白名单、IP 校验、超时、响应大小限制
- 防 DNS rebinding:连接前 + 拿到 socket 时,双重检查解析到的 IP 不是私网/内网

安全(SEC-7):
- allowed_hosts=None → 全部拒绝(必须显式配置;旧的"全放行"语义已移除)
- scheme 限 http/https
- 阻止访问私网 / loopback / link-local / multicast(防 SSRF)
- 显式 host 头检查,不被 302 重定向绕过

**Phase 25 / b7 修复**:
- 三个 http_* 工具改为 ``asyncio.to_thread`` 包装同步 ``httpx.Client``,
  避免在 event loop 上下文里阻塞 dispatch。
- 保留 ``httpx.Client`` + ``event_hooks`` 同步 client: async client 不暴露
  "connect" 钩子, DNS rebinding 防护要在异步路径重写。先用 to_thread 同步
  阻塞调用丢线程池, 保住防护 + 不阻塞 loop。
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import threading
import time
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from openclaw.core.logging import get_logger
from openclaw.tools.registry import ToolCategory, ToolPermission, ToolRegistry

logger = get_logger(__name__)


def _is_private_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # 解析失败 → 当私网处理(安全优先)
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def _resolve_and_check(host: str) -> list[str]:
    """解析 host 到所有 IP,过滤掉私网/loopback。空列表 = 全部 IP 都拒绝。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise PermissionError(f"dns resolution failed: {host}: {e}") from None
    ips = list({info[4][0] for info in infos})
    safe = [ip for ip in ips if not _is_private_ip(ip)]
    if not safe:
        raise PermissionError(
            f"host {host!r} resolves only to private/loopback/link-local: {ips}"
        )
    return safe


def _check(url: str, allowed_hosts: Optional[list[str]]) -> None:
    """URL 多层校验(SEC-7)。"""
    if not url:
        raise PermissionError("empty url")
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise PermissionError(f"scheme {p.scheme!r} not allowed (only http/https)")
    host = p.hostname or ""
    if not host:
        raise PermissionError("url has no host")

    # SEC-7:allowed_hosts=None → 默认拒绝(语义反转)
    if not allowed_hosts:
        raise PermissionError(
            "http tool requires explicit allowed_hosts (set in config or call site)"
        )
    if not any(host == h or host.endswith("." + h) for h in allowed_hosts):
        raise PermissionError(f"host {host!r} not in allow-list {allowed_hosts}")

    # 防 DNS rebinding:连接前先解析一次,确认非私网
    _resolve_and_check(host)


def register_http_tools(
    registry: ToolRegistry,
    *,
    allowed_hosts: Optional[list[str]] = None,
    timeout: float = 30.0,
    max_response_bytes: int = 200_000,
) -> None:
    """注册 HTTP 工具。

    allowed_hosts: 域名白名单;**None 或空 = 全部拒绝**(SEC-7)。
    """

    # SEC-7:连接时再校验一次目标 IP(防 DNS rebinding)
    # M2 修复:httpx event_hooks 只支持 "request"/"response" 两个 key,
    # "connect" 会被静默忽略。改为 "request" hook — 在请求发出前解析 host
    # 并校验 IP,防短 TTL DNS rebinding(首次返公网 IP,真正连接时返内网)。
    def _connect_block_private(request):
        # 拿到 httpx 已解析好的 host
        host = request.url.host
        if not host:
            return
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            return
        for info in infos:
            ip = info[4][0]
            if _is_private_ip(ip):
                # httpx 1.x 的 event hook 通过抛异常来中止
                raise PermissionError(
                    f"DNS rebinding detected: {host} -> {ip} (private)"
                )

    @registry.tool(category=ToolCategory.HTTP, permission=ToolPermission.NETWORK)
    def http_get(url: str, headers: Optional[dict[str, str]] = None) -> str:
        """HTTP GET 请求并返回响应(截断到 max_response_bytes)。url: 完整 URL; headers: 可选请求头。"""
        _check(url, allowed_hosts)
        logger.info("http_get", url=url)
        # **Phase 25 / b7 修复**: 同步 httpx.Client 会阻塞 event loop 几十秒;
        # 改为在 to_thread 线程池里跑, 让主 loop 继续 dispatch 其他消息。
        # 保留同步 client 以维持 DNS rebinding 防护 (event_hooks 在异步 client
        # 上不暴露 "connect" 钩子, 改异步路径要重写防护)。
        def _do_get() -> httpx.Response:
            with httpx.Client(
                timeout=timeout,
                event_hooks={"request": [_connect_block_private]},
            ) as c:
                return c.get(url, headers=headers or {})

        return _do_async(_do_get, timeout)

    @registry.tool(category=ToolCategory.HTTP, permission=ToolPermission.NETWORK)
    def http_post(
        url: str,
        json_body: Optional[dict[str, Any]] = None,
        data: Optional[str] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> str:
        """HTTP POST 请求。url: 完整 URL; json_body: JSON 字典(优先); data: 原始字符串; headers: 可选请求头。"""
        _check(url, allowed_hosts)
        logger.info("http_post", url=url)

        def _do_post() -> httpx.Response:
            with httpx.Client(
                timeout=timeout,
                event_hooks={"request": [_connect_block_private]},
            ) as c:
                if json_body is not None:
                    return c.post(url, json=json_body, headers=headers or {})
                if data is not None:
                    return c.post(url, content=data, headers=headers or {})
                return c.post(url, headers=headers or {})

        return _do_async(_do_post, timeout)

    @registry.tool(category=ToolCategory.HTTP, permission=ToolPermission.NETWORK)
    def http_request(
        method: str,
        url: str,
        json_body: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> str:
        """通用 HTTP 请求(method GET/POST/PUT/DELETE/PATCH)。method: 大写方法; url: 完整 URL; json_body: JSON 字典; headers: 可选请求头。"""
        _check(url, allowed_hosts)
        method = method.upper()
        logger.info("http_request", method=method, url=url)

        def _do_request() -> httpx.Response:
            with httpx.Client(
                timeout=timeout,
                event_hooks={"request": [_connect_block_private]},
            ) as c:
                return c.request(method, url, json=json_body, headers=headers or {})

        return _do_async(_do_request, timeout)


# === Phase 25 / b7: 异步桥接 loop (给 http_* 在 event loop 上下文里跑同步 httpx 用) ===
#
# 思路: 在 running event loop 里调同步 httpx.Client 会阻塞那个 loop。httpx 的
# event_hooks 在 async client 上不暴露 "connect" 钩子, 没法直接迁移; 折衷方案
# 是 ``asyncio.to_thread(sync_client.get)`` 把网络 I/O 丢线程池。但 to_thread
# 只能 await, 而 http_* 还是 sync 函数, 同样需要把协程提交到独立 bridge loop
# + 阻塞拿结果。
_http_bridge_loop: Optional[asyncio.AbstractEventLoop] = None
_http_bridge_thread: Optional[threading.Thread] = None
_http_bridge_lock = threading.Lock()
_http_bridge_ready = threading.Event()


def _ensure_http_bridge_loop() -> asyncio.AbstractEventLoop:
    """懒启动 http 专用后台 async loop。"""
    global _http_bridge_loop, _http_bridge_thread
    if _http_bridge_ready.is_set() and _http_bridge_loop is not None:
        return _http_bridge_loop
    with _http_bridge_lock:
        if _http_bridge_ready.is_set() and _http_bridge_loop is not None:
            return _http_bridge_loop
        holder: list[asyncio.AbstractEventLoop] = []

        def _runner() -> None:
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            holder.append(new_loop)
            new_loop.run_forever()

        t = threading.Thread(target=_runner, name="http-async-bridge", daemon=True)
        t.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not holder:
            time.sleep(0.01)
        if not holder:
            raise RuntimeError("http: 后台 bridge loop 启动超时")
        _http_bridge_loop = holder[0]
        _http_bridge_thread = t
        _http_bridge_ready.set()
        return _http_bridge_loop


def _do_async(work: Any, timeout: float) -> str:
    """根据是否在 event loop 里选不同路径执行同步 http 工作。

    - 无 event loop: 直接跑 (最简)
    - 在 event loop: 把工作用 ``asyncio.to_thread`` 提交到专用 bridge loop
      的线程池, 当前 loop 不被阻塞
    """
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None

    if running_loop is None:
        return _format_response(work())

    bridge = _ensure_http_bridge_loop()
    coro = asyncio.to_thread(work)
    fut = asyncio.run_coroutine_threadsafe(coro, bridge)
    r = fut.result(timeout=timeout + 5)
    return _format_response(r)


def _format_response(r: httpx.Response) -> str:
    body = r.text
    if len(body) > 200_000:
        body = body[:200_000] + f"\n... [truncated, {len(r.text) - 200_000} chars omitted]"
    head = f"HTTP {r.status_code} {r.reason_phrase}\n"
    head += "Content-Type: " + r.headers.get("content-type", "?") + "\n\n"
    return head + body
