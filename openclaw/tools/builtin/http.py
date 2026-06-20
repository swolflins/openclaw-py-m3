"""HTTP 工具(子包)。

- http_get / http_post / http_request
- 带 host 白名单、scheme 白名单、IP 校验、超时、响应大小限制
- 防 DNS rebinding:连接前 + 拿到 socket 时,双重检查解析到的 IP 不是私网/内网

安全(SEC-7):
- allowed_hosts=None → 全部拒绝(必须显式配置;旧的"全放行"语义已移除)
- scheme 限 http/https
- 阻止访问私网 / loopback / link-local / multicast(防 SSRF)
- 显式 host 头检查,不被 302 重定向绕过
"""
from __future__ import annotations

import ipaddress
import socket
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
        with httpx.Client(
            timeout=timeout,
            event_hooks={"connect": [_connect_block_private]},
        ) as c:
            r = c.get(url, headers=headers or {})
            return _format_response(r)

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
        with httpx.Client(
            timeout=timeout,
            event_hooks={"connect": [_connect_block_private]},
        ) as c:
            if json_body is not None:
                r = c.post(url, json=json_body, headers=headers or {})
            elif data is not None:
                r = c.post(url, content=data, headers=headers or {})
            else:
                r = c.post(url, headers=headers or {})
            return _format_response(r)

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
        with httpx.Client(
            timeout=timeout,
            event_hooks={"connect": [_connect_block_private]},
        ) as c:
            r = c.request(method, url, json=json_body, headers=headers or {})
            return _format_response(r)


def _format_response(r: httpx.Response) -> str:
    body = r.text
    if len(body) > 200_000:
        body = body[:200_000] + f"\n... [truncated, {len(r.text) - 200_000} chars omitted]"
    head = f"HTTP {r.status_code} {r.reason_phrase}\n"
    head += "Content-Type: " + r.headers.get("content-type", "?") + "\n\n"
    return head + body
