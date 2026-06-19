"""Gateway 用 Prometheus 文本格式暴露的指标。

为什么不接 prometheus_client:多一个重量级依赖,这个项目体量不需要。
我们用 stdlib + 一个简单的 in-memory 计数器自己实现。
"""
from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock
from typing import Dict, Tuple


class _Counter:
    """极简计数器,带 label。"""

    def __init__(self, name: str, help_: str, labelnames: tuple = ()) -> None:
        self.name = name
        self.help = help_
        self.labelnames = labelnames
        self._values: Dict[Tuple[str, ...], float] = defaultdict(float)
        self._lock = Lock()

    def inc(self, **labels: str) -> None:
        key = tuple(str(labels.get(n, "")) for n in self.labelnames)
        with self._lock:
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
    "HTTP 请求总数(按 method/path/status 分桶)",
    labelnames=("method", "path", "status"),
)
chat_total = _Counter(
    "openclaw_chat_total",
    "Chat 调用总数(按 session_id 分桶)",
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
