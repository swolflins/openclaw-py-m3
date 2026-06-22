"""工具函数:把对象转成 JSON-safe 字典 + 错误包装。

Phase 27 / M5 修复:
- 5 处 ``except Exception: pass`` 改为 ``logger.debug`` 记录(便于排查序列化失败)
- ``bytes.decode`` 失败的 fall-through 加注释(``errors="replace"`` 实际不会抛)
"""
from __future__ import annotations

from typing import Any

from openclaw.core.logging import get_logger

logger = get_logger(__name__)


def to_jsonable(obj: Any, _depth: int = 0) -> Any:
    """递归把对象转成 JSON 安全类型(防 BaseModel / dataclass / set / bytes)。"""
    if _depth > 6:
        return repr(obj)
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(x, _depth + 1) for x in obj]
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (set, frozenset)):
        return [to_jsonable(x, _depth + 1) for x in sorted(obj, key=repr)]
    if isinstance(obj, bytes):
        # errors="replace" 让 decode 不抛,这里 except 实际几乎不可达(防御性)
        try:
            return obj.decode("utf-8", errors="replace")
        except Exception as e:  # pragma: no cover
            logger.debug("to_jsonable_bytes_decode_failed", error=str(e))
            return repr(obj)
    if hasattr(obj, "model_dump"):  # pydantic v2
        try:
            return to_jsonable(obj.model_dump(), _depth + 1)
        except Exception as e:  # pragma: no cover
            logger.debug(
                "to_jsonable_model_dump_failed",
                type=type(obj).__name__,
                error=str(e),
            )
            # fall-through to to_dict / __dict__ 分支
    if hasattr(obj, "to_dict"):
        try:
            return to_jsonable(obj.to_dict(), _depth + 1)
        except Exception as e:  # pragma: no cover
            logger.debug(
                "to_jsonable_to_dict_failed",
                type=type(obj).__name__,
                error=str(e),
            )
    if hasattr(obj, "__dict__"):
        try:
            d = {k: v for k, v in vars(obj).items() if not k.startswith("_")}
        except Exception as e:  # pragma: no cover
            logger.debug(
                "to_jsonable_vars_failed",
                type=type(obj).__name__,
                error=str(e),
            )
            d = {}
        # 如果 instance dict 空(所有属性都是 class 级),从 __class__.__dict__ 拿
        if not d:
            try:
                d = {k: v for k, v in obj.__class__.__dict__.items()
                     if not k.startswith("_") and not callable(v) and not isinstance(v, (classmethod, staticmethod, property))}
            except Exception as e:  # pragma: no cover
                logger.debug(
                    "to_jsonable_class_dict_failed",
                    type=type(obj).__name__,
                    error=str(e),
                )
        if d:
            return to_jsonable(d, _depth + 1)
    return repr(obj)
