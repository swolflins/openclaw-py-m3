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
from pydantic import BaseModel, Field, SecretStr, field_validator

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
    # M8 修复:走 Pydantic field_validator 校验 — 模块路径必须是合法的
    # Python import 路径(dotted module,无 .. / 绝对路径 / 非法字符),
    # 否则构造期就拒。同目录 / 相对路径都不允许(防任意本地 .py 加载)。
    extras: list[str] = Field(default_factory=list)

    @field_validator("extras")
    @classmethod
    def _validate_extras_paths(cls, v: list[str]) -> list[str]:
        import re

        if not v:
            return v
        # Python module path 规范:字母/数字/下划线 + 点分隔,起头不能是点
        # (M8 防止 ``../../../etc/passwd`` / ``/abs/path`` / ``os`` / ``__import__``)
        mod_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
        bad: list[str] = []
        for path in v:
            if not isinstance(path, str) or not path:
                bad.append(f"<non-str: {type(path).__name__}>")
                continue
            if not mod_re.match(path):
                bad.append(path)
        if bad:
            raise ValueError(
                f"tools.extras 含非法模块路径 {bad!r};"
                f"必须是合法 Python module path,如 'mypkg.tools.mymod'"
            )
        # 去重
        return list(dict.fromkeys(v))


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
    # M10 修复:webhook_host 默认值从 "0.0.0.0"(对外暴露)→ "127.0.0.1"(仅本地)
    # 旧默认与 OPENCLAW_GATEWAY_DEV 联动时,会无意中把 webhook 端点暴露到
    # 公网;运维如需对外监听,必须显式设 0.0.0.0(意图明显)。
    # 注:webhook 路径要走 FastAPI 路由层(M/H1 修复),默认 localhost 与
    # gateway 默认 127.0.0.1 一致。
    webhook_host: str = "127.0.0.1"
    webhook_port: int = 8088
    webhook_path: str = "/webhook/{channel}"
    # 单独的 channel 配置(免去每个 channel 单独写 env 解析)
    telegram: dict[str, Any] = Field(default_factory=dict)
    discord: dict[str, Any] = Field(default_factory=dict)
    slack: dict[str, Any] = Field(default_factory=dict)
    whatsapp: dict[str, Any] = Field(default_factory=dict)
    signal: dict[str, Any] = Field(default_factory=dict)
    imessage: dict[str, Any] = Field(default_factory=dict)
    # Phase 26:channel 凭据落盘的根目录(login/logout/creds.json 用)
    fs_root: str = Field(default_factory=lambda: str(Path("~/.openclaw/channels").expanduser()))


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

    # ----- Phase 26:多 agent 配置(list[dict] 由 CLI 写/读) -----
    agents: list[dict] = Field(default_factory=list)

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
                cfg = ConfigLoader.merge_with_env(OpenClawConfig.model_validate(raw))
            else:
                cfg = ConfigLoader.merge_with_env(cfg)
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
            # Phase 27 follow-up / M18:把 path 一起传进去,str(exc) 即可看到
            # 出错的配置文件,方便排查
            raise ConfigError(f"unsupported config format: {suffix}", path=p)
        # SEC-4:支持 ${ENV_VAR} 插值(防泄漏明文 secret)
        return _interp_env(data)

    @classmethod
    def merge_with_env(cls, cfg: "OpenClawConfig") -> "OpenClawConfig":
        """环境变量覆盖:OPENCLAW_<KEY>__<SUB>__<KEY>

        例: OPENCLAW_AGENT__SYSTEM_PROMPT="..."
            OPENCLAW_LOGGING__LEVEL=DEBUG

        Phase 27 / C4 修复:
        旧的 ``cfg.model_dump()`` 路径会把 ``SecretStr`` 序列化为 ``"**********"`` 占位符,
        再 ``model_validate`` 时真值已丢失,生产部署 yaml + env 注入路径下会触发
        鉴权静默失败。修法:用 ``model_dump(mode="python")`` 保留 Python 对象引用,
        在 deep_merge 阶段手动跳过 secret 字段(避免占位符覆盖原值),并允许
        ``OPENCLAW_<PATH>`` 显式覆盖 secret 字段(走 SecretStr 真值注入)。
        """
        # mode="python" 保留 SecretStr 引用(而非 dump 成 "**********" 占位符)
        data = cfg.model_dump(mode="python")
        env_overlay = _env_to_dict("OPENCLAW_")
        _deep_merge_secretsafe(data, env_overlay)
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


# Phase 27 / C4:SecretStr 安全版 deep_merge。
# 跳过空字符串覆盖(常见占位符 ""/None 会把 SecretStr 误清空);
# 保留 SecretStr 真值,只有当 env 显式提供新值时才替换。
_SECRET_FIELD_NAMES = frozenset({
    "api_key", "app_secret", "secret_key", "encrypt_key",
    "verification_token", "password", "token",
})


def _deep_merge_secretsafe(dst: dict[str, Any], src: dict[str, Any]) -> None:
    """类似 ``_deep_merge`` 但对 secret 字段(以 _SECRET_FIELD_NAMES 匹配 key)做保护:

    1. 若 dst[k] 已经是 SecretStr 且 src[k] 是空字符串 / None → 跳过(不覆盖)
    2. 若 dst[k] 是 SecretStr 且 src[k] 是非空 str → 用 SecretStr(src[k]) 替换
    3. 其他情况走标准 deep_merge
    """
    from pydantic import SecretStr

    for k, v in src.items():
        # 递归 dict
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _deep_merge_secretsafe(dst[k], v)
            continue

        # secret 字段保护
        if k.lower() in _SECRET_FIELD_NAMES:
            existing = dst.get(k)
            # 1) 已存在 SecretStr,env 给空 → 跳过
            if isinstance(existing, SecretStr) and (v is None or v == ""):
                continue
            # 2) 已存在 SecretStr,env 给非空 → 真值覆盖
            if isinstance(existing, SecretStr) and isinstance(v, str) and v:
                dst[k] = SecretStr(v)
                continue
            # 3) 其他情况(无现有 / 非 SecretStr / 非 str 覆盖)
            if v is None or v == "":
                # 空值不引入新 SecretStr,直接跳过
                continue
            if isinstance(v, str):
                dst[k] = SecretStr(v)
                continue
            # 非 str 真值 → 走普通赋值(让 pydantic 校验报错)
            dst[k] = v
            continue

        # 非 secret 字段走标准语义
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
