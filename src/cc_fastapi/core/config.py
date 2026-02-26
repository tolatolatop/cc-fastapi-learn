from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Claude Agent Queue API"
    database_url: str = Field(default="sqlite:///./cc_fastapi.db", alias="DATABASE_URL")
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-sonnet-4-20250514", alias="ANTHROPIC_MODEL")
    worker_concurrency: int = Field(default=1, alias="WORKER_CONCURRENCY")
    poll_interval_ms: int = Field(default=1000, alias="POLL_INTERVAL_MS")
    queue_ttl_hours: int = Field(default=24, alias="QUEUE_TTL_HOURS")
    running_ttl_hours: int = Field(default=4, alias="RUNNING_TTL_HOURS")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    api_token: str = Field(default="", alias="API_TOKEN")
    max_attempts: int = Field(default=3, alias="MAX_ATTEMPTS")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

