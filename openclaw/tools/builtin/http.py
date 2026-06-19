"""HTTP 工具(子包)。

- http_get / http_post / http_request
- 带 host 白名单和超时
"""
from __future__ import annotations

from typing import Any, Optional

import httpx

from openclaw.core.logging import get_logger
from openclaw.tools.registry import ToolCategory, ToolPermission, ToolRegistry

logger = get_logger(__name__)


def register_http_tools(
    registry: ToolRegistry,
    *,
    allowed_hosts: Optional[list[str]] = None,
    timeout: float = 30.0,
    max_response_bytes: int = 200_000,
) -> None:
    """注册 HTTP 工具。allowed_hosts: 域名白名单(空=全部允许)。"""

    def _check(url: str) -> None:
        if not allowed_hosts:
            return
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
        if not any(host == h or host.endswith("." + h) for h in allowed_hosts):
            raise PermissionError(f"host {host!r} not in allow-list {allowed_hosts}")

    @registry.tool(category=ToolCategory.HTTP, permission=ToolPermission.NETWORK)
    def http_get(url: str, headers: Optional[dict[str, str]] = None) -> str:
        """HTTP GET 请求并返回响应(截断到 max_response_bytes)。url: 完整 URL; headers: 可选请求头。"""
        _check(url)
        logger.info("http_get", url=url)
        with httpx.Client(timeout=timeout) as c:
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
        _check(url)
        logger.info("http_post", url=url)
        with httpx.Client(timeout=timeout) as c:
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
        _check(url)
        method = method.upper()
        logger.info("http_request", method=method, url=url)
        with httpx.Client(timeout=timeout) as c:
            r = c.request(method, url, json=json_body, headers=headers or {})
            return _format_response(r)


def _format_response(r: httpx.Response) -> str:
    body = r.text
    if len(body) > 200_000:
        body = body[:200_000] + f"\n... [truncated, {len(r.text) - 200_000} chars omitted]"
    head = f"HTTP {r.status_code} {r.reason_phrase}\n"
    head += "Content-Type: " + r.headers.get("content-type", "?") + "\n\n"
    return head + body
