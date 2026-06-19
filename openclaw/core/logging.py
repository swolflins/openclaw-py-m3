"""结构化日志(基于 structlog)。

设计:
- 默认输出 JSON(便于聚合),也可切到控制台彩色输出
- 支持 trace_id 上下文绑定(每次请求/事件一条)
- 与 stdlib logging 互通,标准库日志会被路由到 structlog
"""
from __future__ import annotations

import logging
import sys
import uuid
from contextvars import ContextVar
from typing import Any, Optional

import structlog

_trace_id: ContextVar[Optional[str]] = ContextVar("openclaw_trace_id", default=None)


def new_trace_id() -> str:
    """生成一个 trace id 并绑定到当前上下文。"""
    tid = f"tr_{uuid.uuid4().hex[:12]}"
    _trace_id.set(tid)
    return tid


def current_trace_id() -> Optional[str]:
    return _trace_id.get()


def bind_context(**kwargs: Any) -> None:
    """把字段绑定到当前 structlog 上下文(整个调用链可见)。"""
    structlog.contextvars.bind_contextvars(**kwargs)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()


def _add_trace_id(_, __, event_dict: dict[str, Any]) -> dict[str, Any]:
    tid = _trace_id.get()
    if tid and "trace_id" not in event_dict:
        event_dict["trace_id"] = tid
    return event_dict


def setup_logging(
    level: str = "INFO",
    *,
    json: bool = True,
    show_colors: bool | None = None,
) -> None:
    """初始化 structlog + stdlib logging。

    json=True: JSON 行(生产/CI)
    json=False: 控制台可读(本地开发)
    """
    if show_colors is None:
        show_colors = not json and sys.stderr.isatty()

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        _add_trace_id,
        timestamper,
        structlog.processors.StackInfoRenderer(),
    ]

    if json:
        processors: list[Any] = [
            *shared_processors,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
    else:
        processors = [
            *shared_processors,
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(colors=show_colors),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper())),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # 拦截 stdlib 日志
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(message)s") if json else logging.Formatter(
            "[%(asctime)s] %(levelname)s %(name)s: %(message)s"
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    # 噪音降级
    for noisy in ("httpx", "httpcore", "urllib3", "chromadb"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """获取一个 structlog logger。"""
    return structlog.get_logger(name)
