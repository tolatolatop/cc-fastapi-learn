from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cc_fastapi.api.repositories import router
from cc_fastapi.core.config import get_settings
from cc_fastapi.db.models import Base
from cc_fastapi.db.session import get_db


@pytest.fixture(autouse=True)
def repository_api_settings(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def build_client() -> TestClient:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    testing_session = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    Base.metadata.create_all(bind=engine)

    app = FastAPI()
    app.include_router(router)

    def override_db():
        db = testing_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


def test_repository_crud_uniqueness_and_normalization():
    client = build_client()
    created = client.post(
        "/v1/repositories",
        json={
            "provider": " GitLab ",
            "project_path": "/Group/Project/",
            "tags": ["Backend", "重点项目", " backend "],
        },
    )
    assert created.status_code == 201
    repository = created.json()
    repository_id = repository["id"]
    assert repository["provider"] == "gitlab"
    assert repository["project_path"] == "group/project"
    assert repository["tags"] == ["backend", "重点项目"]

    fetched = client.get(f"/v1/repositories/{repository_id}")
    assert fetched.status_code == 200
    assert fetched.json() == repository

    duplicate = client.post(
        "/v1/repositories",
        json={"provider": "GITLAB", "project_path": "group/project"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "repository already exists for this provider"

    other = client.post(
        "/v1/repositories",
        json={
            "provider": "github",
            "project_path": "org/frontend",
            "tags": ["frontend", "backend"],
        },
    )
    assert other.status_code == 201

    conflict = client.patch(
        f"/v1/repositories/{repository_id}",
        json={"provider": "github", "project_path": "org/frontend"},
    )
    assert conflict.status_code == 409
    assert client.get(f"/v1/repositories/{repository_id}").json()["provider"] == "gitlab"

    updated = client.patch(
        f"/v1/repositories/{repository_id}",
        json={
            "provider": "GitLab-Corp",
            "project_path": "/Team/API/",
            "tags": ["Core", "核心"],
        },
    )
    assert updated.status_code == 200
    assert updated.json()["provider"] == "gitlab-corp"
    assert updated.json()["project_path"] == "team/api"
    assert updated.json()["tags"] == ["core", "核心"]
    assert updated.json()["updated_at"] >= updated.json()["created_at"]

    deleted = client.delete(f"/v1/repositories/{repository_id}")
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert client.get(f"/v1/repositories/{repository_id}").status_code == 404
    assert client.delete(f"/v1/repositories/{repository_id}").status_code == 404


def test_repository_list_filters_tags_and_paginates():
    client = build_client()
    fixtures = [
        ("gitlab", "group/api", ["backend", "critical"]),
        ("gitlab", "group/worker", ["backend"]),
        ("github", "org/web", ["frontend"]),
    ]
    for provider, project_path, tags in fixtures:
        response = client.post(
            "/v1/repositories",
            json={"provider": provider, "project_path": project_path, "tags": tags},
        )
        assert response.status_code == 201

    filtered = client.get(
        "/v1/repositories",
        params=[
            ("provider", "GITLAB"),
            ("tag", " Backend "),
            ("tag", "CRITICAL"),
        ],
    )
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["project_path"] == "group/api"
    assert filtered.json()["summary"] == {
        "total": 3,
        "providers": ["github", "gitlab"],
        "tags": ["backend", "critical", "frontend"],
    }

    searched = client.get("/v1/repositories", params={"q": "WORK"})
    assert searched.status_code == 200
    assert searched.json()["total"] == 1
    assert searched.json()["items"][0]["project_path"] == "group/worker"

    paged = client.get("/v1/repositories", params={"offset": 1, "limit": 1})
    assert paged.status_code == 200
    assert paged.json()["total"] == 3
    assert len(paged.json()["items"]) == 1


@pytest.mark.parametrize(
    ("payload", "expected_status"),
    [
        ({"provider": " ", "project_path": "group/project"}, 422),
        ({"provider": "gitlab", "project_path": "/"}, 422),
        ({"provider": "gitlab", "project_path": "group/project", "tags": [" "]}, 422),
        (
            {
                "provider": "gitlab",
                "project_path": "group/project",
                "tags": [str(index) for index in range(51)],
            },
            422,
        ),
        (
            {
                "provider": "gitlab",
                "project_path": "group/project",
                "tags": ["x" * 65],
            },
            422,
        ),
    ],
)
def test_repository_create_validation(payload, expected_status):
    client = build_client()
    assert client.post("/v1/repositories", json=payload).status_code == expected_status


def test_repository_update_and_filter_validation():
    client = build_client()
    created = client.post(
        "/v1/repositories",
        json={"provider": "gitlab", "project_path": "group/project"},
    ).json()

    assert client.patch(f"/v1/repositories/{created['id']}", json={}).status_code == 422
    assert client.patch(
        f"/v1/repositories/{created['id']}", json={"tags": None}
    ).status_code == 422
    assert client.patch(
        f"/v1/repositories/{created['id']}", json={"tags": []}
    ).status_code == 200
    assert client.get("/v1/repositories", params={"provider": " "}).status_code == 422
    assert client.get("/v1/repositories", params={"tag": " "}).status_code == 422
    assert client.get("/v1/repositories", params={"limit": 201}).status_code == 422


def test_repository_api_requires_configured_token(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "repository-secret")
    get_settings.cache_clear()
    client = build_client()

    assert client.get("/v1/repositories").status_code == 401
    authorized = client.get(
        "/v1/repositories",
        headers={"X-API-Token": "repository-secret"},
    )
    assert authorized.status_code == 200
