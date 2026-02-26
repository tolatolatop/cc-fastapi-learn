from cc_fastapi.core.config import get_settings


def test_resolved_database_url_falls_back_to_database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./fallback.db")
    monkeypatch.delenv("POSTGRES_EXTERNAL_URL", raising=False)
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
