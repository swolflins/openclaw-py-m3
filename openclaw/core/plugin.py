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
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from openclaw.core.errors import PluginError
from openclaw.core.logging import get_logger

logger = get_logger(__name__)


# ---------------- Phase 30 / M13 加载隔离 ----------------

# 1 MiB 上限 — 防 10GB 文件被 exec_module 阻塞(RCE 攻击放大面)。
_MAX_PLUGIN_BYTES = 1 * 1024 * 1024


def _get_allowed_plugin_dirs() -> list[Path]:
    """插件目录白名单(Phase 30 / M13)。

    优先级:
    1. 显式 ``OPENCLAW_PLUGIN_DIR`` env 目录(可多次 ``:`` 分隔,生产部署推荐)
    2. ``~/.openclaw/plugins/``(用户主目录)
    3. ``/etc/openclaw/plugins/``(系统级,只读)

    任意一个匹配即放行;**完全不在白名单** → 不加载(日志 CRITICAL)。
    """
    roots: list[Path] = []
    env = os.environ.get("OPENCLAW_PLUGIN_DIR", "").strip()
    if env:
        for p in env.split(":"):
            try:
                roots.append(Path(p).expanduser().resolve())
            except (OSError, ValueError):
                continue
    home = Path.home() / ".openclaw" / "plugins"
    if home.exists():
        try:
            roots.append(home.resolve())
        except (OSError, ValueError):
            pass
    sys_root = Path("/etc/openclaw/plugins")
    if sys_root.exists():
        try:
            roots.append(sys_root.resolve())
        except (OSError, ValueError):
            pass
    return roots


def _is_under_any(d: Path, allowed: list[Path]) -> bool:
    """判断 ``d`` 是否是 ``allowed`` 中任一目录的子目录。

    用途:Phase 30 / M13 目录白名单校验。
    """
    if not allowed:
        return False
    try:
        d_resolved = d.resolve()
    except (OSError, ValueError):
        return False
    for root in allowed:
        try:
            d_resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False

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

    def load_local(self, directory: Path | str, *, _skip_allowlist: bool = False) -> int:
        """扫描目录里所有 .py 文件,执行其 module-level `register(runtime)`。

        **Phase 30 / M13 修复 — 加载隔离**:
        1. **目录白名单** — 拒绝在白名单外的目录加载插件。
           白名单:``~/.openclaw/plugins/``、``/etc/openclaw/plugins/``、
           显式 ``OPENCLAW_PLUGIN_DIR`` env 目录。
           防止攻击者把插件写到任意目录(如 ``/tmp/evil/``)后诱导加载。
        2. **文件大小上限** — 单个 .py 插件不得超过 1 MiB(防 10GB 文件阻塞 exec_module)。
        3. **owner 校验** — 文件 owner uid 必须 == 当前进程 euid(防同主机其他用户写入)。
        4. **绝对路径** — ``Path(directory).resolve()`` 拿到绝对路径后再比对白名单。

        ``_skip_allowlist``:仅供**测试**使用(``True`` 时跳过白名单校验)。
        仓库里 ``test_phase1`` / ``test_phase15_misc`` 用 ``tmp_path`` 作
        测试 plugin 目录,这类测试需要绕过白名单,但生产代码**不允许**
        传 ``_skip_allowlist=True``(只接受 keyword-only 形式)。
        """
        d = Path(directory).resolve()  # M13 修复:解析为绝对路径
        if not d.exists():
            return 0
        # M13 修复:校验目录路径,防止加载任意路径的代码
        if not d.is_dir():
            logger.warning("local_plugin_path_not_dir: %s", d)
            return 0
        # M13 / Phase 30:目录白名单校验(测试可通过 _skip_allowlist 绕过)
        if not _skip_allowlist:
            allowed_roots = _get_allowed_plugin_dirs()
            if not _is_under_any(d, allowed_roots):
                logger.critical(
                    "local_plugin_path_not_in_allowlist: %s not under any of %s",
                    d, allowed_roots,
                )
                return 0
        count = 0
        for path in sorted(d.glob("*.py")):
            if path.name.startswith("_"):
                continue
            # M13 / Phase 30:文件大小上限(1 MiB)
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > _MAX_PLUGIN_BYTES:
                logger.warning(
                    "local_plugin_too_large: %s (%d bytes, max %d)",
                    path, size, _MAX_PLUGIN_BYTES,
                )
                continue
            # M13 / Phase 30:owner 校验
            try:
                st = path.stat()
                if st.st_uid != os.geteuid():
                    logger.warning(
                        "local_plugin_owner_mismatch: %s owner uid=%d, current euid=%d",
                        path, st.st_uid, os.geteuid(),
                    )
                    continue
            except OSError:
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
                # M13 修复:用 get_running_loop() 替代废弃的 get_event_loop()
                loop = asyncio.get_running_loop()
                loop.create_task(result)
            except RuntimeError:
                # 无运行中的 loop 时新建
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
