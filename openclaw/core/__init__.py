"""core 子包:基础设施(配置/日志/插件加载/事件总线 通用辅助)。

子模块的导入采用 **lazy attribute** 风格(模块级 `__getattr__`),
这样分阶段上传时,某 phase 未到时 `from openclaw.core import X` 不会整体失败。
"""
from __future__ import annotations

from typing import Any

from openclaw.core.logging import get_logger, setup_logging, new_trace_id, bind_context
from openclaw.core.errors import OpenClawError, ConfigError, PluginError, ProviderError

__version_phase = "0.1.0"


_LAZY_EXPORTS = {
    # phase 6
    "RateLimiter": ("openclaw.core.rate_limit", "RateLimiter"),
    "AutoReplyConfig": ("openclaw.core.auto_reply", "AutoReplyConfig"),
    "AutoReplyDecision": ("openclaw.core.auto_reply", "AutoReplyDecision"),
    "AutoReplyManager": ("openclaw.core.auto_reply", "AutoReplyManager"),
    "Skill": ("openclaw.core.skills", "Skill"),
    "SkillAPI": ("openclaw.core.skills", "SkillAPI"),
    "SkillRegistry": ("openclaw.core.skills", "SkillRegistry"),
    "load_skills": ("openclaw.core.skills", "load_skills"),
}


def __getattr__(name: str) -> Any:
    spec = _LAZY_EXPORTS.get(name)
    if spec is None:
        raise AttributeError(f"module 'openclaw.core' has no attribute {name!r}")
    import importlib
    mod = importlib.import_module(spec[0])
    value = getattr(mod, spec[1])
    globals()[name] = value
    return value


__all__ = [
    "get_logger",
    "setup_logging",
    "new_trace_id",
    "bind_context",
    "OpenClawError",
    "ConfigError",
    "PluginError",
    "ProviderError",
    # lazy
    "RateLimiter",
    "AutoReplyConfig",
    "AutoReplyDecision",
    "AutoReplyManager",
    "Skill",
    "SkillAPI",
    "SkillRegistry",
    "load_skills",
]
