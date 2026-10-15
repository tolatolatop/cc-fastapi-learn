from cc_fastapi.core.config import DEFAULT_GITLAB_WEBHOOK_PROMPT_TEMPLATE_PATH, get_settings


def test_resolved_database_url_falls_back_to_database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./fallback.db")
    # Explicitly override any value loaded from local .env
    monkeypatch.setenv("POSTGRES_EXTERNAL_URL", "")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.resolved_database_url == "sqlite:///./fallback.db"
    finally:
        get_settings.cache_clear()


def test_resolved_database_url_prefers_postgres_external_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./fallback.db")
    monkeypatch.setenv("POSTGRES_EXTERNAL_URL", "postgresql+psycopg://u:p@db:5432/app")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.resolved_database_url == "postgresql+psycopg://u:p@db:5432/app"
    finally:
        get_settings.cache_clear()


def test_resolved_database_url_normalizes_plain_postgres_url_for_psycopg(monkeypatch):
    monkeypatch.setenv("POSTGRES_EXTERNAL_URL", "postgres://u:p@db:5432/app")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.resolved_database_url == "postgresql+psycopg://u:p@db:5432/app"
    finally:
        get_settings.cache_clear()


def test_blank_gitlab_webhook_prompt_template_path_uses_default(monkeypatch):
    monkeypatch.setenv("GITLAB_WEBHOOK_PROMPT_TEMPLATE_PATH", "")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert settings.resolved_gitlab_webhook_prompt_template_path == DEFAULT_GITLAB_WEBHOOK_PROMPT_TEMPLATE_PATH
    finally:
        get_settings.cache_clear()
