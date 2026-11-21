from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import selectinload, sessionmaker
from sqlalchemy.pool import StaticPool

from review_console.access_control.permissions import (
    NO_ACCESS_REPOSITORY,
    permission_for,
    statistics_repository_params,
)
from review_console.access_control.service import find_or_create_sso_user
from review_console.config import Settings, get_settings
from review_console.db import get_db
from review_console.main import admin_user, app, current_user
from review_console.models import (
    Base,
    ConsoleUser,
    RepositoryGrant,
    RepositoryPermission,
)
from review_console.schema import apply_schema_migrations
from review_console.security import hash_password
from review_console.upstream import ReviewApiClient


def all_repository_reader() -> ConsoleUser:
    return ConsoleUser(
        id="reviewer-2",
        username="reader",
        display_name="Reader",
        password_hash="unused",
        all_repositories_read=True,
    )


def upstream_issue() -> dict:
    return {
        "id": "issue-1",
        "provider": "github",
        "project_path": "team/service",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


@pytest.fixture
def settings_cache():
    yield
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def build_client() -> tuple[TestClient, sessionmaker, ConsoleUser]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    admin = ConsoleUser(
        id="admin-1",
        username="admin",
        display_name="Admin",
        password_hash=hash_password("admin-password"),
        is_admin=True,
    )
    with sessions() as db:
        db.add(admin)
        db.commit()

    def override_db():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[admin_user] = lambda: admin
    return TestClient(app), sessions, admin


def load_user(sessions: sessionmaker, user_id: str) -> ConsoleUser:
    with sessions() as db:
        user = db.scalar(
            select(ConsoleUser)
            .options(selectinload(ConsoleUser.grants))
            .where(ConsoleUser.id == user_id)
        )
        assert user is not None
        return user


def test_new_local_user_defaults_to_all_repository_read(monkeypatch, settings_cache):
    client, sessions, _ = build_client()
    created = client.post(
        "/v1/admin/users",
        json={
            "username": "lin",
            "display_name": "Lin",
            "password": "secure-password",
        },
    )
    assert created.status_code == 201
    assert created.json()["all_repositories_read"] is True
    user = load_user(sessions, created.json()["id"])

    assert (
        permission_for(user, "gitlab", "another/service")
        == RepositoryPermission.READ
    )
    monkeypatch.setattr(
        ReviewApiClient, "issues", lambda self, params: {"items": [], "total": 0}
    )
    monkeypatch.setattr(
        ReviewApiClient, "issue", lambda self, issue_id: upstream_issue()
    )
    app.dependency_overrides[current_user] = lambda: user
    assert (
        client.get(
            "/v1/issues",
            params={"provider": "gitlab", "project_path": "another/service"},
        ).status_code
        == 200
    )
    assert (
        client.put(
            "/v1/issues/issue-1/status",
            json={
                "status": "accepted",
                "expected_updated_at": upstream_issue()["updated_at"],
            },
        ).status_code
        == 403
    )


def test_new_local_user_scope_can_be_overridden_on_creation(settings_cache):
    client, sessions, _ = build_client()
    created = client.post(
        "/v1/admin/users",
        json={
            "username": "chen",
            "display_name": "Chen",
            "password": "secure-password",
            "all_repositories_read": False,
        },
    )
    assert created.status_code == 201
    assert created.json()["all_repositories_read"] is False
    user = load_user(sessions, created.json()["id"])
    assert permission_for(user, "github", "team/service") is None


def test_default_all_repository_read_can_be_disabled_by_configuration(
    monkeypatch, settings_cache
):
    monkeypatch.setenv("REVIEW_CONSOLE_DEFAULT_ALL_REPOSITORIES_READ", "false")
    get_settings.cache_clear()
    client, sessions, _ = build_client()
    created = client.post(
        "/v1/admin/users",
        json={
            "username": "wang",
            "display_name": "Wang",
            "password": "secure-password",
        },
    )
    assert created.status_code == 201
    assert created.json()["all_repositories_read"] is False
    user = load_user(sessions, created.json()["id"])
    assert statistics_repository_params(user) == [
        ("repository", NO_ACCESS_REPOSITORY)
    ]


def test_admin_toggles_all_repository_read_in_one_request(monkeypatch, settings_cache):
    client, sessions, _ = build_client()
    created = client.post(
        "/v1/admin/users",
        json={
            "username": "zhao",
            "display_name": "Zhao",
            "password": "secure-password",
            "all_repositories_read": False,
        },
    )
    user_id = created.json()["id"]
    grant = client.put(
        f"/v1/admin/users/{user_id}/grants",
        json={
            "provider": "github",
            "project_path": "team/service",
            "permission": "read",
        },
    )
    assert grant.status_code == 200

    enabled = client.patch(
        f"/v1/admin/users/{user_id}", json={"all_repositories_read": True}
    )
    assert enabled.status_code == 200
    assert enabled.json()["all_repositories_read"] is True
    listed = client.get("/v1/admin/users").json()["items"]
    assert next(item for item in listed if item["id"] == user_id)[
        "all_repositories_read"
    ] is True

    user = load_user(sessions, user_id)
    assert statistics_repository_params(user) == []
    captured: dict = {}

    def review_records(self, params):
        captured["params"] = params
        return {"items": [], "total": 0}

    monkeypatch.setattr(ReviewApiClient, "review_records", review_records)
    app.dependency_overrides[current_user] = lambda: user
    response = client.get(
        "/v1/review-records",
        params={
            "created_from": "2026-01-01T00:00:00+00:00",
            "created_to": "2026-01-31T00:00:00+00:00",
        },
    )
    assert response.status_code == 200
    assert ("repository", "github/team/service") not in captured["params"]

    disabled = client.patch(
        f"/v1/admin/users/{user_id}", json={"all_repositories_read": False}
    )
    assert disabled.status_code == 200
    assert disabled.json()["all_repositories_read"] is False
    user = load_user(sessions, user_id)
    assert permission_for(user, "gitlab", "another/service") is None


def test_explicit_write_grant_outranks_all_repository_read(monkeypatch):
    user = all_repository_reader()
    user.grants = [
        RepositoryGrant(
            id="grant-1",
            user_id=user.id,
            provider="github",
            project_path="team/service",
            permission=RepositoryPermission.WRITE,
        )
    ]
    assert (
        permission_for(user, "github", "team/service") == RepositoryPermission.WRITE
    )
    assert (
        permission_for(user, "github", "team/other") == RepositoryPermission.READ
    )
    app.dependency_overrides[current_user] = lambda: user
    monkeypatch.setattr(
        ReviewApiClient, "issue", lambda self, issue_id: upstream_issue()
    )
    monkeypatch.setattr(
        ReviewApiClient,
        "update_status",
        lambda self, issue_id, payload, *, actor_id, actor_name: {
            **upstream_issue(),
            "status": payload["status"],
        },
    )
    try:
        response = TestClient(app).put(
            "/v1/issues/issue-1/status",
            json={
                "status": "accepted",
                "expected_updated_at": upstream_issue()["updated_at"],
            },
        )
        assert response.status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_all_repository_reader_sees_every_upstream_repository(monkeypatch):
    app.dependency_overrides[current_user] = lambda: all_repository_reader()
    monkeypatch.setattr(
        ReviewApiClient,
        "repositories",
        lambda self, *args, **kwargs: {
            "items": [
                {
                    "provider": "github",
                    "project_path": "team/service",
                    "issue_total": 0,
                    "pending_total": 0,
                },
                {
                    "provider": "gitlab",
                    "project_path": "other/service",
                    "issue_total": 1,
                    "pending_total": 1,
                },
            ]
        },
    )
    response = TestClient(app).get("/v1/repositories")
    assert response.status_code == 200
    assert [item["permission"] for item in response.json()["items"]] == [
        "read",
        "read",
    ]
    app.dependency_overrides.clear()


def sso_claims() -> dict:
    return {
        "iss": "https://identity.example",
        "sub": "employee-42",
        "preferred_username": "lin",
        "name": "Lin Reviewer",
    }


def sso_settings(**overrides) -> Settings:
    values = {
        "REVIEW_CONSOLE_SESSION_SECRET": "s" * 32,
        "REVIEW_CONSOLE_API_TOKEN": "integration-secret",
        "REVIEW_CONSOLE_SSO_ISSUER_URL": "https://identity.example",
    }
    values.update(overrides)
    return Settings(**values)


def build_sessions() -> sessionmaker:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    return sessions


def test_sso_provisioning_grants_default_all_repository_read():
    sessions = build_sessions()
    with sessions() as db:
        user = find_or_create_sso_user(db, sso_claims(), sso_settings())
        assert user.all_repositories_read is True
        assert user.is_admin is False


def test_sso_provisioning_follows_configuration_switch():
    sessions = build_sessions()
    settings = sso_settings(REVIEW_CONSOLE_DEFAULT_ALL_REPOSITORIES_READ="false")
    with sessions() as db:
        user = find_or_create_sso_user(db, sso_claims(), settings)
        assert user.all_repositories_read is False


def test_existing_sso_user_keeps_its_all_repository_read_flag():
    sessions = build_sessions()
    with sessions() as db:
        user = find_or_create_sso_user(db, sso_claims(), sso_settings())
        user.all_repositories_read = False
        db.commit()
        same_user = find_or_create_sso_user(
            db,
            sso_claims(),
            sso_settings(REVIEW_CONSOLE_DEFAULT_ALL_REPOSITORIES_READ="true"),
        )
        assert same_user.id == user.id
        assert same_user.all_repositories_read is False


def test_legacy_database_gains_all_repository_read_column():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE console_users ("
            "id VARCHAR(36) PRIMARY KEY, "
            "username VARCHAR(64) NOT NULL, "
            "display_name VARCHAR(128) NOT NULL, "
            "password_hash VARCHAR(256) NOT NULL, "
            "is_admin BOOLEAN NOT NULL, "
            "is_active BOOLEAN NOT NULL, "
            "created_at DATETIME NOT NULL, "
            "updated_at DATETIME NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO console_users (id, username, display_name, password_hash, "
            "is_admin, is_active, created_at, updated_at) VALUES "
            "('legacy-1', 'legacy', 'Legacy', 'unused', 0, 1, "
            "'2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        )

    apply_schema_migrations(engine)
    apply_schema_migrations(engine)

    with engine.connect() as connection:
        value = connection.exec_driver_sql(
            "SELECT all_repositories_read FROM console_users WHERE id = 'legacy-1'"
        ).scalar_one()
    assert bool(value) is False
