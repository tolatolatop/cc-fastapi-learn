from datetime import datetime, timezone

from fastapi.testclient import TestClient
from review_console.db import get_db
from review_console.main import admin_user, app, current_user
from review_console.models import (
    Base,
    ConsoleUser,
    RepositoryGrant,
    RepositoryPermission,
)
from review_console.security import hash_password
from review_console.upstream import ReviewApiClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


def scoped_user(permission: RepositoryPermission) -> ConsoleUser:
    user = ConsoleUser(
        id="reviewer-1",
        username="reviewer",
        display_name="Reviewer",
        password_hash="unused",
    )
    user.grants = [
        RepositoryGrant(
            id="grant-1",
            user_id=user.id,
            provider="github",
            project_path="team/service",
            permission=permission,
        )
    ]
    return user


def upstream_issue() -> dict:
    return {
        "id": "issue-1",
        "provider": "github",
        "project_path": "team/service",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def test_read_grant_can_view_history_but_cannot_change_status(monkeypatch):
    app.dependency_overrides[current_user] = lambda: scoped_user(
        RepositoryPermission.READ
    )
    monkeypatch.setattr(
        ReviewApiClient, "issue", lambda self, issue_id: upstream_issue()
    )
    monkeypatch.setattr(
        ReviewApiClient, "history", lambda self, issue_id: {"items": []}
    )
    monkeypatch.setattr(
        ReviewApiClient,
        "pull_requests",
        lambda self, params: {"items": [], "total": 0},
    )
    client = TestClient(app)

    assert client.get("/v1/issues/issue-1/history").status_code == 200
    assert (
        client.get(
            "/v1/pull-requests",
            params={"provider": "github", "project_path": "team/service"},
        ).status_code
        == 200
    )
    denied = client.put(
        "/v1/issues/issue-1/status",
        json={
            "status": "accepted",
            "expected_updated_at": upstream_issue()["updated_at"],
        },
    )
    assert denied.status_code == 403
    app.dependency_overrides.clear()


def test_write_grant_forwards_actor_identity(monkeypatch):
    user = scoped_user(RepositoryPermission.WRITE)
    app.dependency_overrides[current_user] = lambda: user
    monkeypatch.setattr(
        ReviewApiClient, "issue", lambda self, issue_id: upstream_issue()
    )
    captured = {}

    def update(self, issue_id, payload, *, actor_id, actor_name):
        captured.update(actor_id=actor_id, actor_name=actor_name, payload=payload)
        return {
            **upstream_issue(),
            "status": payload["status"],
            "note": payload["note"],
        }

    monkeypatch.setattr(ReviewApiClient, "update_status", update)
    client = TestClient(app)
    response = client.put(
        "/v1/issues/issue-1/status",
        json={
            "status": "not_accepted",
            "reason_code": "not_reproducible",
            "note": "Not reproducible",
            "expected_updated_at": upstream_issue()["updated_at"],
        },
    )
    assert response.status_code == 200
    assert captured["actor_id"] == user.id
    assert captured["actor_name"] == user.display_name
    assert captured["payload"]["reason_code"] == "not_reproducible"
    app.dependency_overrides.clear()


def test_statistics_are_limited_to_user_repository_grants(monkeypatch):
    user = scoped_user(RepositoryPermission.READ)
    app.dependency_overrides[current_user] = lambda: user
    captured = {}

    def statistics(self, params):
        captured["params"] = params
        return {
            "created_from": None,
            "created_to": None,
            "summary": {
                "valid_opinion_total": 0,
                "confirmed_total": 0,
                "false_positive_total": 0,
            },
            "contributors": [],
            "top_false_positive_repositories": [],
        }

    monkeypatch.setattr(ReviewApiClient, "statistics", statistics)
    response = TestClient(app).get("/v1/statistics")
    assert response.status_code == 200
    assert ("repository", "github/team/service") in captured["params"]
    app.dependency_overrides.clear()


def test_statistics_with_no_grants_never_request_global_scope(monkeypatch):
    user = scoped_user(RepositoryPermission.READ)
    user.grants = []
    app.dependency_overrides[current_user] = lambda: user
    captured = {}

    def statistics(self, params):
        captured["params"] = params
        return {
            "created_from": None,
            "created_to": None,
            "summary": {
                "valid_opinion_total": 0,
                "confirmed_total": 0,
                "false_positive_total": 0,
            },
            "contributors": [],
            "top_false_positive_repositories": [],
        }

    monkeypatch.setattr(ReviewApiClient, "statistics", statistics)
    response = TestClient(app).get("/v1/statistics")
    assert response.status_code == 200
    assert captured["params"] == [
        ("repository", "__no_access__/__no_access__")
    ]
    app.dependency_overrides.clear()


def test_admin_can_create_user_and_assign_repository_grant():
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
    client = TestClient(app)
    login = client.post(
        "/v1/auth/login", json={"username": "admin", "password": "admin-password"}
    )
    assert login.status_code == 200
    assert "HttpOnly" in login.headers["set-cookie"]
    created = client.post(
        "/v1/admin/users",
        json={
            "username": "lin",
            "display_name": "Lin",
            "password": "secure-password",
            "is_admin": False,
        },
    )
    assert created.status_code == 201
    user_id = created.json()["id"]
    grant = client.put(
        f"/v1/admin/users/{user_id}/grants",
        json={
            "provider": "GitHub",
            "project_path": "/Team/Service/",
            "permission": "write",
        },
    )
    assert grant.status_code == 200
    assert grant.json()["provider"] == "github"
    assert grant.json()["project_path"] == "team/service"
    app.dependency_overrides.clear()
