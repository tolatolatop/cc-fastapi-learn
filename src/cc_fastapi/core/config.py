from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_GITLAB_WEBHOOK_PROMPT_TEMPLATE_PATH = "config/templates/gitlab_webhook_prompt.j2"
DEFAULT_GITHUB_WEBHOOK_PROMPT_TEMPLATE_PATH = "config/templates/github_webhook_prompt.j2"


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
    oidc_enabled: bool = Field(default=False, alias="OIDC_ENABLED")
    oidc_issuer_url: str = Field(default="", alias="OIDC_ISSUER_URL")
    oidc_client_id: str = Field(default="", alias="OIDC_CLIENT_ID")
    oidc_client_secret: str = Field(default="", alias="OIDC_CLIENT_SECRET")
    oidc_client_auth_method: Literal[
        "client_secret_basic", "client_secret_post", "none"
    ] = Field(default="client_secret_basic", alias="OIDC_CLIENT_AUTH_METHOD")
    oidc_redirect_uri: str = Field(default="", alias="OIDC_REDIRECT_URI")
    oidc_scopes: str = Field(default="openid profile email", alias="OIDC_SCOPES")
    oidc_audience: str = Field(default="", alias="OIDC_AUDIENCE")
    oidc_signing_algorithms: str = Field(default="RS256", alias="OIDC_SIGNING_ALGORITHMS")
    oidc_session_secret: str = Field(default="", alias="OIDC_SESSION_SECRET")
    oidc_session_hours: int = Field(default=12, ge=1, le=168, alias="OIDC_SESSION_HOURS")
    oidc_cookie_secure: bool = Field(default=False, alias="OIDC_COOKIE_SECURE")
    oidc_username_claim: str = Field(default="preferred_username", alias="OIDC_USERNAME_CLAIM")
    oidc_display_name_claim: str = Field(default="name", alias="OIDC_DISPLAY_NAME_CLAIM")
    oidc_email_claim: str = Field(default="email", alias="OIDC_EMAIL_CLAIM")
    oidc_button_label: str = Field(default="使用企业账号登录", alias="OIDC_BUTTON_LABEL")
    oidc_timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0, alias="OIDC_TIMEOUT_SECONDS")
    review_console_api_token: str = Field(default="", alias="REVIEW_CONSOLE_API_TOKEN")
    gitlab_webhook_secret: str = Field(default="", alias="GITLAB_WEBHOOK_SECRET")
    gitlab_webhook_prompt_template_path: str = Field(
        default=DEFAULT_GITLAB_WEBHOOK_PROMPT_TEMPLATE_PATH,
        alias="GITLAB_WEBHOOK_PROMPT_TEMPLATE_PATH",
    )
    gitlab_webhook_queue_name: str = Field(default="", alias="GITLAB_WEBHOOK_QUEUE_NAME")
    gitlab_webhook_model: str = Field(default="", alias="GITLAB_WEBHOOK_MODEL")
    github_webhook_secret: str = Field(default="", alias="GITHUB_WEBHOOK_SECRET")
    github_webhook_prompt_template_path: str = Field(
        default=DEFAULT_GITHUB_WEBHOOK_PROMPT_TEMPLATE_PATH,
        alias="GITHUB_WEBHOOK_PROMPT_TEMPLATE_PATH",
    )
    github_webhook_queue_name: str = Field(default="", alias="GITHUB_WEBHOOK_QUEUE_NAME")
    github_webhook_model: str = Field(default="", alias="GITHUB_WEBHOOK_MODEL")
    max_attempts: int = Field(default=3, alias="MAX_ATTEMPTS")

    @property
    def resolved_database_url(self) -> str:
        external = self.postgres_external_url.strip()
        if external:
            if external.startswith("postgres://"):
                external = f"postgresql://{external.removeprefix('postgres://')}"
            if external.startswith("postgresql://"):
                return f"postgresql+psycopg://{external.removeprefix('postgresql://')}"
            return external
        return self.database_url

    @property
    def resolved_gitlab_webhook_prompt_template_path(self) -> str:
        if self.gitlab_webhook_prompt_template_path.strip():
            return self.gitlab_webhook_prompt_template_path
        return DEFAULT_GITLAB_WEBHOOK_PROMPT_TEMPLATE_PATH

    @property
    def resolved_github_webhook_prompt_template_path(self) -> str:
        if self.github_webhook_prompt_template_path.strip():
            return self.github_webhook_prompt_template_path
        return DEFAULT_GITHUB_WEBHOOK_PROMPT_TEMPLATE_PATH

    def validate_oidc_runtime(self) -> None:
        if not self.oidc_enabled:
            return
        required = {
            "OIDC_ISSUER_URL": self.oidc_issuer_url,
            "OIDC_CLIENT_ID": self.oidc_client_id,
            "OIDC_REDIRECT_URI": self.oidc_redirect_uri,
            "OIDC_SESSION_SECRET": self.oidc_session_secret,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if missing:
            raise RuntimeError(f"missing OIDC configuration: {', '.join(missing)}")
        if len(self.oidc_session_secret) < 32:
            raise RuntimeError("OIDC_SESSION_SECRET must contain at least 32 characters")
        if "openid" not in self.oidc_scopes.split():
            raise RuntimeError("OIDC_SCOPES must include openid")
        if self.oidc_client_auth_method != "none" and not self.oidc_client_secret.strip():
            raise RuntimeError(
                "OIDC_CLIENT_SECRET is required for the configured client authentication method"
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
