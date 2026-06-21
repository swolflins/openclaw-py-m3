"""统一配置加载。

支持:
- YAML / JSON / TOML (按文件后缀自动选择)
- 环境变量覆盖(OPENCLAW_xxx__yyy)
- 热重载(watchdog)
- Pydantic 校验

优先级: 默认值 < 配置文件 < 环境变量
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable, Optional

import yaml
from pydantic import BaseModel, Field, SecretStr

from openclaw.core.errors import ConfigError
from openclaw.core.logging import get_logger

logger = get_logger(__name__)

try:
    import tomllib  # py3.11+
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]


# ------------------- 数据模型 -------------------

class ProviderConfig(BaseModel):
    name: str  # openai_compat / anthropic / gemini / ollama / router
    model: str
    # Phase 25/b9:api_key 改用 SecretStr,避免日志 / repr 泄漏明文。
    # 只在工厂 / 真正发请求时调 .get_secret_value() 取出字符串。
    api_key: Optional[SecretStr] = None
    base_url: Optional[str] = None
    extra: dict[str, Any] = Field(default_factory=dict)


class ChannelConfig(BaseModel):
    name: str  # cli / lark / telegram / ...
    enabled: bool = True
    options: dict[str, Any] = Field(default_factory=dict)


class MemoryConfig(BaseModel):
    dir: Path = Path("./.openclaw_memory")
    short_term_window: int = 20
    long_term_enabled: bool = False
    embedding_model: str = "text-embedding-3-small"


class ToolsConfig(BaseModel):
    """工具注册配置。"""
    fs_root: str = "."
    shell_default_cwd: str = "."
    shell_allowed: list[str] | None = None
    http_allowed_hosts: list[str] | None = None
    include: list[str] | None = None
    exclude: list[str] | None = None
    # 允许非内置工具以模块路径方式注入(Phase 5+)
    extras: list[str] = Field(default_factory=list)


class RateLimitConfig(BaseModel):
    """Auto-Reply 限流配置(每个 limit 是 token bucket 参数)。"""
    enabled: bool = False
    # per-user: 每秒 0.2 个,突发 3
    per_user_rate: float = 0.2
    per_user_burst: int = 3
    # per-channel
    per_channel_rate: float = 5.0
    per_channel_burst: int = 10
    # bucket 状态持久化路径(None = 内存)
    persist_path: Optional[Path] = None


class AutoReplyConfigSection(BaseModel):
    """Auto-Reply 行为配置(关键词触发 / 模板 / 静默时段)。"""
    enabled: bool = False
    triggers: list[str] = Field(default_factory=list)
    blacklist: list[str] = Field(default_factory=list)
    templates: dict[str, str] = Field(default_factory=dict)
    auto_in_dm: bool = True
    auto_when_mentioned: bool = True
    quiet_hours: Optional[list[str]] = None   # ["23:00", "07:00"]
    prompt_prefix_template: str = "[上下文] channel={channel} user={user_id} ts={ts}\n"


class SkillsConfig(BaseModel):
    """Skill 加载配置。"""
    enabled: bool = True
    # 扫描的根目录(相对项目根 / 绝对路径均可)
    directories: list[Path] = Field(default_factory=lambda: [Path("./openclaw_skills")])


class ChannelRuntimeConfig(BaseModel):
    """Phase 7:多 channel 启动配置。"""
    # 启用的 channel 列表(channel 名 = 渠道类型,见 openclaw.channels)
    enabled: list[str] = Field(default_factory=lambda: ["cli"])
    # 通用 webhook 入口(走 FastAPI 路由的 channel 用,Phase 8 接进来)
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8088
    webhook_path: str = "/webhook/{channel}"
    # 单独的 channel 配置(免去每个 channel 单独写 env 解析)
    telegram: dict[str, Any] = Field(default_factory=dict)
    discord: dict[str, Any] = Field(default_factory=dict)
    slack: dict[str, Any] = Field(default_factory=dict)
    whatsapp: dict[str, Any] = Field(default_factory=dict)
    signal: dict[str, Any] = Field(default_factory=dict)
    imessage: dict[str, Any] = Field(default_factory=dict)


class AgentConfig(BaseModel):
    system_prompt: str = "你叫 Claw,是一个乐于助人、简洁高效的私人 AI 助理。"
    max_tool_iterations: int = 8
    history_window: int = 20
    soul_paths: list[Path] = Field(default_factory=lambda: [
        Path("./SOUL.md"), Path("./AGENTS.md"), Path("./.openclaw/SOUL.md"),
    ])
    # ----- Phase 5 增强 -----
    # router 策略:fallback_only | round_robin | cost_aware | priority
    router_strategy: str = "fallback_only"
    # 单步内部每个 provider 的最大重试次数
    step_max_attempts: int = 2
    # 是否启用 Multi-Agent(Planner/Executor/Critic/Reflector)
    multi_agent: bool = False
    multi_agent_critic: bool = True
    multi_agent_reflector: bool = True
    multi_agent_max_reflection_loops: int = 1
    # plan 执行器并发上限
    plan_max_parallel: int = 4
    # 各 provider 成本权重(用于 cost_aware),单位 USD/1k token
    provider_costs: dict[str, float] = Field(default_factory=dict)
    # 各 provider 优先级(用于 priority),数字越小越优先
    provider_priorities: dict[str, int] = Field(default_factory=dict)


class LoggingConfig(BaseModel):
    model_config = {"protected_namespaces": ()}
    level: str = "INFO"
    json_output: bool = Field(default=True, alias="json")


class OpenClawConfig(BaseModel):
    """根配置:所有子配置集中。"""
    providers: list[ProviderConfig] = Field(default_factory=list)
    channels: list[ChannelConfig] = Field(default_factory=list)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    # ----- Phase 6 -----
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    auto_reply: AutoReplyConfigSection = Field(default_factory=AutoReplyConfigSection)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    # ----- Phase 7 -----
    channels_runtime: ChannelRuntimeConfig = Field(default_factory=ChannelRuntimeConfig)

    default_provider: Optional[str] = None
    router_fallback: list[str] = Field(default_factory=list)


# ------------------- 加载器 -------------------

class ConfigLoader:
    """加载 + 解析 + 监听热重载。"""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else None
        self._config: Optional[OpenClawConfig] = None
        self._lock = threading.RLock()
        self._watcher: _ConfigWatcher | None = None
        self._on_reload: list[Callable[[OpenClawConfig], None]] = []

    # ----- 公共 API -----

    def load(self) -> OpenClawConfig:
        with self._lock:
            cfg = OpenClawConfig()
            if self.path and self.path.exists():
                raw = self._read_file(self.path)
                cfg = self._merge_env(OpenClawConfig.model_validate(raw))
            else:
                cfg = self._merge_env(cfg)
            self._config = cfg
            return cfg

    def current(self) -> OpenClawConfig:
        if self._config is None:
            return self.load()
        return self._config

    def watch(self, on_reload: Callable[[OpenClawConfig], None]) -> None:
        self._on_reload.append(on_reload)
        if self.path and self.path.exists():
            self._watcher = _ConfigWatcher(self.path, self._on_reload_safe)
            self._watcher.start()

    def stop_watch(self) -> None:
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None

    # ----- 内部 -----

    def _on_reload_safe(self) -> None:
        try:
            cfg = self.load()
            for cb in self._on_reload:
                cb(cfg)
        except Exception:
            logger.exception("config reload failed")

    @staticmethod
    def _read_file(p: Path) -> dict[str, Any]:
        suffix = p.suffix.lower()
        text = p.read_text(encoding="utf-8")
        if suffix in (".yaml", ".yml"):
            data = yaml.safe_load(text) or {}
        elif suffix == ".json":
            data = json.loads(text)
        elif suffix == ".toml":
            data = tomllib.loads(text)
        else:
            raise ConfigError(f"unsupported config format: {suffix}")
        # SEC-4:支持 ${ENV_VAR} 插值(防泄漏明文 secret)
        return _interp_env(data)

    @staticmethod
    def _merge_env(cfg: OpenClawConfig) -> OpenClawConfig:
        """环境变量覆盖:OPENCLAW_<KEY>__<SUB>__<KEY>

        例: OPENCLAW_AGENT__SYSTEM_PROMPT="..."
            OPENCLAW_LOGGING__LEVEL=DEBUG
        """
        data = cfg.model_dump()
        _deep_merge(data, _env_to_dict("OPENCLAW_"))
        return OpenClawConfig.model_validate(data)


def _env_to_dict(prefix: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in os.environ.items():
        if not k.startswith(prefix):
            continue
        path = k[len(prefix):].lower().split("__")
        cur: Any = out
        for part in path[:-1]:
            cur = cur.setdefault(part, {})  # type: ignore[assignment]
        cur[path[-1]] = _coerce(v)
    return out


# SEC-4:支持 ${ENV_VAR} / ${ENV_VAR:-default} 插值
_ENV_INTERP_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}")


def _interp_env(obj: Any) -> Any:
    """递归把 dict/list/str 里的 ${ENV_VAR} 替换为 os.environ 值。

    - 形式 1:`${NAME}` → os.environ["NAME"](没设时原样保留 + 警告)
    - 形式 2:`${NAME:-default}` → os.environ["NAME"] 或 default
    """
    if isinstance(obj, dict):
        return {k: _interp_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_interp_env(x) for x in obj]
    if isinstance(obj, str):
        def _sub(m: re.Match) -> str:
            name, default = m.group(1), m.group(2)
            val = os.environ.get(name, default)
            if val is None:
                logger.warning(
                    "config_env_missing: %s not set and no default", name
                )
                return ""
            return val
        return _ENV_INTERP_RE.sub(_sub, obj)
    return obj


def _coerce(v: str) -> Any:
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    try:
        if "." in v:
            return float(v)
        return int(v)
    except ValueError:
        return v


def _deep_merge(dst: dict[str, Any], src: dict[str, Any]) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge(dst[k], v)
        else:
            dst[k] = v


# ------------------- 热重载 -------------------

class _ConfigWatcher:
    """基于 watchdog 的文件监听(单独线程)。"""

    def __init__(self, path: Path, on_change: Callable[[], None]) -> None:
        self.path = path
        self._on_change = on_change
        self._observer = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        parent = self.path.parent.resolve()

        class _Handler(FileSystemEventHandler):
            def on_modified(inner, event):  # noqa: N805
                if Path(event.src_path).resolve() == self.path.resolve():
                    self._on_change()

        self._observer = Observer()
        self._observer.schedule(_Handler(), str(parent), recursive=False)
        self._observer.start()

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2)
            self._observer = None
