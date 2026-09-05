from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field(
        default="sqlite:///./data/review_console.db",
        alias="REVIEW_CONSOLE_DATABASE_URL",
    )
    upstream_url: str = Field(default="http://app:8000", alias="REVIEW_API_URL")
    upstream_token: str = Field(default="", alias="REVIEW_CONSOLE_API_TOKEN")
    session_secret: str = Field(default="", alias="REVIEW_CONSOLE_SESSION_SECRET")
    session_hours: int = Field(default=12, alias="REVIEW_CONSOLE_SESSION_HOURS")
    cookie_secure: bool = Field(default=False, alias="REVIEW_CONSOLE_COOKIE_SECURE")
    bootstrap_username: str = Field(
        default="admin", alias="REVIEW_CONSOLE_ADMIN_USERNAME"
    )
    bootstrap_password: str = Field(default="", alias="REVIEW_CONSOLE_ADMIN_PASSWORD")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
