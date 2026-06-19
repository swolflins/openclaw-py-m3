"""工具函数:把对象转成 JSON-safe 字典 + 错误包装。"""
from __future__ import annotations

from typing import Any


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
        try:
            return obj.decode("utf-8", errors="replace")
        except Exception:
            return repr(obj)
    if hasattr(obj, "model_dump"):  # pydantic v2
        try:
            return to_jsonable(obj.model_dump(), _depth + 1)
        except Exception:
            pass
    if hasattr(obj, "to_dict"):
        try:
            return to_jsonable(obj.to_dict(), _depth + 1)
        except Exception:
            pass
    if hasattr(obj, "__dict__"):
        try:
            d = {k: v for k, v in vars(obj).items() if not k.startswith("_")}
        except Exception:
            d = {}
        # 如果 instance dict 空(所有属性都是 class 级),从 __class__.__dict__ 拿
        if not d:
            try:
                d = {k: v for k, v in obj.__class__.__dict__.items()
                     if not k.startswith("_") and not callable(v) and not isinstance(v, (classmethod, staticmethod, property))}
            except Exception:
                pass
        if d:
            return to_jsonable(d, _depth + 1)
    return repr(obj)
