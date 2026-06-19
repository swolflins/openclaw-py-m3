"""Pydantic Settings 配置。

支持从 .env 加载,所有字段可被同名环境变量覆盖。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class OpenAISettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPENAI_", env_file=".env", extra="ignore")

    api_key: str = Field(default="sk-replace-me", description="OpenAI 兼容 API Key")
    base_url: str = Field(default="https://api.deepseek.com/v1", description="API 根 URL")
    model: str = Field(default="deepseek-chat", description="默认模型 id")
    timeout: float = Field(default=60.0, description="单次请求超时(秒)")


class LarkSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LARK_", env_file=".env", extra="ignore")

    app_id: str = Field(default="", description="飞书自建应用 App ID")
    app_secret: str = Field(default="", description="飞书 App Secret")
    verification_token: Optional[str] = Field(default=None)
    encrypt_key: Optional[str] = Field(default=None)
    use_ws: bool = Field(default=True, description="True=长连接, False=Webhook")
    webhook_url: Optional[str] = Field(default=None)


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", env_file=".env", extra="ignore")

    system_prompt: str = Field(
        default=(
            "你叫 Claw, 是一个乐于助人、简洁高效的私人 AI 助理。"
            "可以使用工具来获取信息或执行任务。"
        )
    )
    max_tool_iterations: int = Field(default=8, ge=1, le=50)
    history_window: int = Field(default=20, ge=1, le=200)


class MemorySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MEMORY_", env_file=".env", extra="ignore")

    dir: Path = Field(default=Path("./.openclaw_memory"))


class Settings(BaseSettings):
    """根配置,合并所有子配置。"""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    openai: OpenAISettings = Field(default_factory=OpenAISettings)
    lark: LarkSettings = Field(default_factory=LarkSettings)
    agent: AgentSettings = Field(default_factory=AgentSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)

    log_level: str = Field(default="INFO")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """单例 Settings,首次调用时构造,之后命中缓存。"""
    return Settings()
