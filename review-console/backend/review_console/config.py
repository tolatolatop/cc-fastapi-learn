from functools import lru_cache
from typing import Literal

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
    local_login_enabled: bool = Field(
        default=True, alias="REVIEW_CONSOLE_LOCAL_LOGIN_ENABLED"
    )
    sso_enabled: bool = Field(default=False, alias="REVIEW_CONSOLE_SSO_ENABLED")
    sso_issuer_url: str = Field(default="", alias="REVIEW_CONSOLE_SSO_ISSUER_URL")
    sso_client_id: str = Field(default="", alias="REVIEW_CONSOLE_SSO_CLIENT_ID")
    sso_client_secret: str = Field(
        default="", alias="REVIEW_CONSOLE_SSO_CLIENT_SECRET"
    )
    sso_client_auth_method: Literal[
        "client_secret_basic", "client_secret_post", "none"
    ] = Field(
        default="client_secret_basic",
        alias="REVIEW_CONSOLE_SSO_CLIENT_AUTH_METHOD",
    )
    sso_redirect_uri: str = Field(
        default="", alias="REVIEW_CONSOLE_SSO_REDIRECT_URI"
    )
    sso_scopes: str = Field(
        default="openid profile email", alias="REVIEW_CONSOLE_SSO_SCOPES"
    )
    sso_signing_algorithms: str = Field(
        default="RS256", alias="REVIEW_CONSOLE_SSO_SIGNING_ALGORITHMS"
    )
    sso_username_claim: str = Field(
        default="preferred_username", alias="REVIEW_CONSOLE_SSO_USERNAME_CLAIM"
    )
    sso_display_name_claim: str = Field(
        default="name", alias="REVIEW_CONSOLE_SSO_DISPLAY_NAME_CLAIM"
    )
    sso_groups_claim: str = Field(
        default="groups", alias="REVIEW_CONSOLE_SSO_GROUPS_CLAIM"
    )
    sso_admin_group: str = Field(
        default="", alias="REVIEW_CONSOLE_SSO_ADMIN_GROUP"
    )
    sso_button_label: str = Field(
        default="使用企业账号登录", alias="REVIEW_CONSOLE_SSO_BUTTON_LABEL"
    )
    sso_timeout_seconds: float = Field(
        default=10.0, ge=1.0, le=60.0, alias="REVIEW_CONSOLE_SSO_TIMEOUT_SECONDS"
    )

    def validate_runtime(self) -> None:
        if len(self.session_secret) < 32:
            raise RuntimeError(
                "REVIEW_CONSOLE_SESSION_SECRET must contain at least 32 characters"
            )
        if not self.upstream_token:
            raise RuntimeError("REVIEW_CONSOLE_API_TOKEN must be configured")
        if not self.local_login_enabled and not self.sso_enabled:
            raise RuntimeError("at least one review console login method must be enabled")
        if not self.sso_enabled:
            return
        required = {
            "REVIEW_CONSOLE_SSO_ISSUER_URL": self.sso_issuer_url,
            "REVIEW_CONSOLE_SSO_CLIENT_ID": self.sso_client_id,
            "REVIEW_CONSOLE_SSO_REDIRECT_URI": self.sso_redirect_uri,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if missing:
            raise RuntimeError(f"missing SSO configuration: {', '.join(missing)}")
        if "openid" not in self.sso_scopes.split():
            raise RuntimeError("REVIEW_CONSOLE_SSO_SCOPES must include openid")
        if (
            self.sso_client_auth_method != "none"
            and not self.sso_client_secret.strip()
        ):
            raise RuntimeError(
                "REVIEW_CONSOLE_SSO_CLIENT_SECRET is required for the configured "
                "client authentication method"
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
