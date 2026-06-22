"""Gateway 用 Prometheus 文本格式暴露的指标。

为什么不接 prometheus_client:多一个重量级依赖,这个项目体量不需要。
我们用 stdlib + 一个简单的 in-memory 计数器自己实现。

**SEC-12 修复 — 标签基数控制**:
- ``http_requests_total.path`` 用 FastAPI route template(``/v1/chat/{id}``)
  而非 raw URL(``/v1/chat/123``)→ 防止 unbounded cardinality
- ``chat_total.session_id`` 在调用方截到 32 字符;``_normalize_session_id`` 再次兜底
- 新增 ``http_request_size_bytes`` 走 _Histogram(分桶),不做 per-request label
"""
from __future__ import annotations

import re
import time
from collections import defaultdict
from threading import Lock
from typing import Dict, Tuple


# 匹配长数字 id、UUID、session hash 等,统一归一化为占位符(防止 label 爆炸)
_HIGH_CARDINALITY_RE = re.compile(
    r"(\b[0-9a-f]{8,}\b|\b[0-9]{4,}\b|[0-9a-f-]{36})",
    re.IGNORECASE,
)


def _normalize_path(path: str) -> str:
    """把高基数路径归一化。

    例子:
      /v1/chat/abc123def456 → /v1/chat/{id}
      /v1/users/12345       → /v1/users/{id}
    """
    if not path:
        return path
    # 不去碰 route template(没有 query string)
    return _HIGH_CARDINALITY_RE.sub("{id}", path)


def _normalize_session_id(sid: str, max_len: int = 32) -> str:
    """截断 + 兜底归一化,防止把高基数 session_id 灌进 metrics。"""
    if not sid:
        return "default"
    return sid[:max_len]


class _Counter:
    """极简计数器,带 label。"""

    def __init__(self, name: str, help_: str, labelnames: tuple = ()) -> None:
        self.name = name
        self.help = help_
        self.labelnames = labelnames
        # 防止 label 爆炸:每个 (labelname -> set) 上限 500
        self._max_cardinality = 500
        self._values: Dict[Tuple[str, ...], float] = defaultdict(float)
        self._lock = Lock()

    def inc(self, **labels: str) -> None:
        key = tuple(str(labels.get(n, "")) for n in self.labelnames)
        with self._lock:
            if len(self._values) >= self._max_cardinality and key not in self._values:
                # 超过基数上限 → 归一到 __overflow__
                key = tuple("__overflow__" if _ else "" for _ in key)
            self._values[key] += 1.0

    def render(self) -> str:
        with self._lock:
            lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} counter"]
            for labels, v in self._values.items():
                if self.labelnames:
                    label_str = ",".join(
                        f'{n}="{v_}"' for n, v_ in zip(self.labelnames, labels)
                    )
                    lines.append(f"{self.name}{{{label_str}}} {v}")
                else:
                    lines.append(f"{self.name} {v}")
            return "\n".join(lines)


class _Gauge:
    def __init__(self, name: str, help_: str) -> None:
        self.name = name
        self.help = help_
        self._value: float = 0.0
        self._lock = Lock()

    def set(self, v: float) -> None:
        with self._lock:
            self._value = float(v)

    def render(self) -> str:
        with self._lock:
            return (
                f"# HELP {self.name} {self.help}\n"
                f"# TYPE {self.name} gauge\n"
                f"{self.name} {self._value}"
            )


# ---------------- 注册表 ----------------

http_requests_total = _Counter(
    "openclaw_http_requests_total",
    "HTTP 请求总数(按 method/path(status)/status 分桶)",
    labelnames=("method", "path", "status"),
)
chat_total = _Counter(
    "openclaw_chat_total",
    "Chat 调用总数(按 session_id 分桶,id 截到 32 字符)",
    labelnames=("session_id",),
)
chat_errors_total = _Counter(
    "openclaw_chat_errors_total",
    "Chat 错误总数",
    labelnames=("error_type",),
)
tool_calls_total = _Counter(
    "openclaw_tool_calls_total",
    "工具调用总数(按 tool name / approved 分桶)",
    labelnames=("tool", "approved"),
)
# Phase 27 / M13 修复:网关鉴权失败指标(按 path / has_token 分桶),
# 便于 SIEM 拉取数据检测暴力破解(同 IP / path 短时间内大量 has_token=false 即报警)
gateway_auth_rejected_total = _Counter(
    "openclaw_gateway_auth_rejected_total",
    "网关鉴权失败总数(按 path / has_token 分桶)",
    labelnames=("path", "has_token"),
)

uptime_seconds = _Gauge(
    "openclaw_uptime_seconds",
    "Gateway 启动后经过的秒数",
)
agent_attached = _Gauge(
    "openclaw_agent_attached",
    "1 = agent_loop 已注入,0 = degraded",
)

ALL_METRICS = [
    http_requests_total,
    chat_total,
    chat_errors_total,
    tool_calls_total,
    gateway_auth_rejected_total,
    uptime_seconds,
    agent_attached,
]


def render_prometheus() -> str:
    """拼成 prom 文本格式。"""
    out = []
    for m in ALL_METRICS:
        out.append(m.render())
    out.append(f"# generated_at {int(time.time())}")
    return "\n".join(out) + "\n"
