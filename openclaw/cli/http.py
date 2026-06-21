"""Gateway HTTP 客户端(供 sessions / skills reload / gateway health 复用)。

封装 base_url + token + httpx,统一错误处理。
"""
from __future__ import annotations

import os
from typing import Any, Optional

import httpx

from openclaw.cli.errors import CLIError, EXIT_NETWORK, EXIT_NOT_FOUND


class GatewayClient:
    """对 openclaw gateway REST API 的轻量封装。"""

    def __init__(
        self,
        url: Optional[str] = None,
        token: Optional[str] = None,
        *,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = (url or os.environ.get("OPENCLAW_GATEWAY_URL") or "http://127.0.0.1:8088").rstrip("/")
        self.token = token or os.environ.get("OPENCLAW_GATEWAY_TOKEN")
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
            h["X-Gateway-Token"] = self.token
        return h

    def _request(self, method: str, path: str, *, json_body: Any = None, params: Any = None) -> Any:
        url = f"{self.base_url}{path}"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.request(method, url, json=json_body, params=params, headers=self._headers())
        except httpx.ConnectError as e:
            raise CLIError(
                f"无法连接 gateway: {self.base_url}",
                exit_code=EXIT_NETWORK,
                hint="请确认 gateway 已启动:openclaw serve",
            ) from e
        except httpx.TimeoutException as e:
            raise CLIError(f"gateway 请求超时: {self.base_url}", exit_code=EXIT_NETWORK) from e

        if resp.status_code == 404:
            raise CLIError(f"未找到: {path}", exit_code=EXIT_NOT_FOUND)
        if resp.status_code == 401:
            raise CLIError("认证失败:token 无效", exit_code=EXIT_NETWORK, hint="用 --token 或设 OPENCLAW_GATEWAY_TOKEN")
        if resp.status_code == 503:
            raise CLIError(
                "gateway 未挂载 agent_loop(503)",
                exit_code=EXIT_NETWORK,
                hint="启动时不要带 --no-agent:openclaw serve",
            )
        if resp.status_code >= 400:
            # 尝试解析错误信息
            try:
                err = resp.json()
                msg = err.get("detail") or err.get("message") or resp.text
            except Exception:  # noqa: BLE001
                msg = resp.text
            raise CLIError(f"gateway 返回 {resp.status_code}: {msg}", exit_code=EXIT_NETWORK)

        if resp.status_code == 204 or not resp.content:
            return None
        try:
            return resp.json()
        except Exception:  # noqa: BLE001
            return resp.text

    def get(self, path: str, *, params: Any = None) -> Any:
        return self._request("GET", path, params=params)

    def post(self, path: str, *, json_body: Any = None) -> Any:
        return self._request("POST", path, json_body=json_body)

    def delete(self, path: str) -> Any:
        return self._request("DELETE", path)


__all__ = ["GatewayClient"]
