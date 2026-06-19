"""插件加载器:支持 entry_points 第三方扩展 + 本地目录扫描。

插件协议:
    # my_plugin/__init__.py
    def register(runtime) -> None:
        # runtime 暴露: register_tool / register_channel / register_provider / subscribe
        ...

    # pyproject.toml
    [project.entry-points."openclaw.plugins"]
    my_plugin = "my_plugin:register"

或者本地目录:
    ./openclaw_plugins/<name>.py   -> 内部定义 register(runtime)
"""
from __future__ import annotations

import importlib
import importlib.metadata as importlib_metadata
import inspect
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from openclaw.core.errors import PluginError
from openclaw.core.logging import get_logger

logger = get_logger(__name__)

# 4 类扩展点
ENTRY_POINT_GROUPS = {
    "plugin": "openclaw.plugins",
    "channel": "openclaw.channels",
    "provider": "openclaw.providers",
    "tool": "openclaw.tools",
}


@dataclass
class Runtime:
    """暴露给插件的注册接口。"""
    tool_registry: Any = None
    channel_registry: Any = None
    provider_factory: Any = None
    bus: Any = None
    custom: dict[str, Any] = field(default_factory=dict)

    def register_tool(self, tool: Any) -> None:
        if self.tool_registry is None:
            raise PluginError("tool_registry not bound")
        self.tool_registry.register(tool)

    def register_channel(self, channel_cls: Any) -> None:
        if self.channel_registry is None:
            raise PluginError("channel_registry not bound")
        self.channel_registry.register_class(channel_cls)

    def register_provider(self, name: str, factory: Callable[..., Any]) -> None:
        if self.provider_factory is None:
            raise PluginError("provider_factory not bound")
        self.provider_factory.register(name, factory)

    def subscribe(self, topic: str, handler: Any) -> None:
        if self.bus is None:
            raise PluginError("bus not bound")
        self.bus.subscribe(topic, handler)


class PluginManager:
    """统一加载 entry_points + 本地目录插件。"""

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self._loaded: list[str] = []

    def load_entry_points(self, group: str = ENTRY_POINT_GROUPS["plugin"]) -> int:
        """加载所有 openclaw.plugins 组里的 entry_points。"""
        try:
            eps = importlib_metadata.entry_points()
        except Exception:
            return 0

        # py3.10+ EntryPoints 是 Selection,可 group(name=...); 老版本 dict
        try:
            entries = eps.select(group=group)  # type: ignore[attr-defined]
        except AttributeError:
            entries = eps.get(group, [])  # type: ignore[union-attr]

        count = 0
        for ep in entries:
            try:
                self._invoke(ep.name, ep.load())
                self._loaded.append(ep.name)
                count += 1
            except Exception:
                logger.exception("plugin_load_failed", plugin=ep.name)
        return count

    def load_local(self, directory: Path | str) -> int:
        """扫描目录里所有 .py 文件,执行其 module-level `register(runtime)`。"""
        d = Path(directory)
        if not d.exists():
            return 0
        count = 0
        for path in sorted(d.glob("*.py")):
            if path.name.startswith("_"):
                continue
            mod_name = f"_openclaw_local_plugin_{path.stem}"
            spec = importlib.util.spec_from_file_location(mod_name, path)  # type: ignore[attr-defined]
            if not spec or not spec.loader:
                continue
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = mod
            try:
                spec.loader.exec_module(mod)
            except Exception:
                logger.exception("local_plugin_import_failed", path=str(path))
                continue
            reg = getattr(mod, "register", None)
            if reg is None:
                continue
            try:
                self._invoke(path.stem, reg)
                self._loaded.append(path.stem)
                count += 1
            except Exception:
                logger.exception("local_plugin_register_failed", plugin=path.stem)
        return count

    def loaded(self) -> list[str]:
        return list(self._loaded)

    def _invoke(self, name: str, fn: Any) -> None:
        """调用 register(runtime);支持同步/异步。"""
        sig = inspect.signature(fn)
        if len(sig.parameters) == 0:
            result = fn()
        else:
            result = fn(self.runtime)
        if inspect.iscoroutine(result):
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # 在已运行的 loop 中 spawn task
                    loop.create_task(result)
                else:
                    loop.run_until_complete(result)
            except RuntimeError:
                # 无 loop 时新建
                asyncio.run(result)


def discover_entry_points(group: str) -> list[tuple[str, Any]]:
    """辅助函数:列出所有 entry_points(不执行)。"""
    try:
        eps = importlib_metadata.entry_points()
        try:
            entries = list(eps.select(group=group))  # type: ignore[attr-defined]
        except AttributeError:
            entries = list(eps.get(group, []))  # type: ignore[union-attr]
        return [(e.name, e) for e in entries]
    except Exception:
        return []
