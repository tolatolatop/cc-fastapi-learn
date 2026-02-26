from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Claude Agent Queue API"
    database_url: str = Field(default="sqlite:///./cc_fastapi.db", alias="DATABASE_URL")
    postgres_external_url: str = Field(default="", alias="POSTGRES_EXTERNAL_URL")
    queues_config_path: str = Field(default="config/queues.yaml", alias="QUEUES_CONFIG_PATH")
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    anthropic_base_url: str = Field(default="", alias="ANTHROPIC_BASE_URL")
    api_timeout_ms: int = Field(default=3000000, alias="API_TIMEOUT_MS")
    anthropic_default_opus_model: str = Field(default="", alias="ANTHROPIC_DEFAULT_OPUS_MODEL")
    anthropic_default_sonnet_model: str = Field(default="", alias="ANTHROPIC_DEFAULT_SONNET_MODEL")
    anthropic_default_haiku_model: str = Field(default="", alias="ANTHROPIC_DEFAULT_HAIKU_MODEL")
    anthropic_model: str = Field(default="claude-sonnet-4-20250514", alias="ANTHROPIC_MODEL")
    claude_permission_mode: str = Field(default="bypassPermissions", alias="CLAUDE_PERMISSION_MODE")
    claude_max_turns: int = Field(default=16, alias="CLAUDE_MAX_TURNS")
    claude_cwd: str = Field(default=".", alias="CLAUDE_CWD")
    claude_allowed_tools: str = Field(default="", alias="CLAUDE_ALLOWED_TOOLS")
    claude_disallowed_tools: str = Field(default="", alias="CLAUDE_DISALLOWED_TOOLS")
    worker_concurrency: int = Field(default=1, alias="WORKER_CONCURRENCY")
    poll_interval_ms: int = Field(default=1000, alias="POLL_INTERVAL_MS")
    queue_ttl_hours: int = Field(default=24, alias="QUEUE_TTL_HOURS")
    running_ttl_hours: int = Field(default=4, alias="RUNNING_TTL_HOURS")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_dir: str = Field(default="logs", alias="LOG_DIR")
    debug_log_enabled: bool = Field(default=True, alias="DEBUG_LOG_ENABLED")
    debug_log_backup_days: int = Field(default=14, alias="DEBUG_LOG_BACKUP_DAYS")
    debug_log_filename: str = Field(default="debug.log", alias="DEBUG_LOG_FILENAME")
    debug_log_utc: bool = Field(default=True, alias="DEBUG_LOG_UTC")
    api_token: str = Field(default="", alias="API_TOKEN")
    max_attempts: int = Field(default=3, alias="MAX_ATTEMPTS")

    @property
    def resolved_database_url(self) -> str:
        external = self.postgres_external_url.strip()
        if external:
            return external
        return self.database_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

