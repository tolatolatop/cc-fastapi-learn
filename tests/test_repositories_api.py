from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cc_fastapi.api.repositories import router as repository_router
from cc_fastapi.api.review_issues import batch_router, issue_router
from cc_fastapi.api.tasks import router as task_router
from cc_fastapi.core.config import get_settings
from cc_fastapi.db.models import Base, WebhookTrigger
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
    app.include_router(task_router)
    app.include_router(batch_router)
    app.include_router(issue_router)
    app.include_router(repository_router)
    app.state.testing_session = testing_session

    def override_db():
        db = testing_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


def create_webhook_trigger(
    client: TestClient,
    *,
    provider: str,
    payload: dict,
    event_type: str = "push",
) -> None:
    with client.app.state.testing_session() as db:
        db.add(
            WebhookTrigger(
                provider=provider,
                event_type=event_type,
                payload_json=payload,
            )
        )
        db.commit()


def create_task(client: TestClient, prompt: str) -> str:
    response = client.post("/v1/agent-tasks", json={"prompt": prompt})
    assert response.status_code == 200
    return response.json()["task_id"]


def test_repository_crud_uniqueness_and_normalization():
    client = build_client()
    created = client.post(
        "/v1/repositories",
        json={
            "provider": " GitLab ",
            "project_path": "/Group/Project/",
            "web_url": "https://gitlab.example.com/Group/Project/",
            "tags": ["Backend", "重点项目", " backend "],
        },
    )
    assert created.status_code == 201
    repository = created.json()
    repository_id = repository["id"]
    assert repository["provider"] == "gitlab"
    assert repository["project_path"] == "group/project"
    assert repository["web_url"] == "https://gitlab.example.com/Group/Project"
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
            "web_url": None,
            "tags": ["Core", "核心"],
        },
    )
    assert updated.status_code == 200
    assert updated.json()["provider"] == "gitlab-corp"
    assert updated.json()["project_path"] == "team/api"
    assert updated.json()["web_url"] is None
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


def test_repository_sync_adds_distinct_webhook_repositories_with_empty_tags():
    client = build_client()
    existing = client.post(
        "/v1/repositories",
        json={
            "provider": "gitlab",
            "project_path": "group/existing",
            "web_url": "https://gitlab.example.com/group/existing",
            "tags": ["keep-me"],
        },
    ).json()
    create_webhook_trigger(
        client,
        provider="gitlab",
        payload={
            "project": {
                "path_with_namespace": "Group/Existing",
                "web_url": "https://new.example.com/group/existing",
            }
        },
    )
    create_webhook_trigger(
        client,
        provider=" GitLab ",
        payload={
            "project": {
                "path_with_namespace": "/Group/New-API/",
                "web_url": "https://gitlab.example.com/Group/New-API/",
            }
        },
    )
    create_webhook_trigger(
        client,
        provider="gitlab",
        payload={"project": {"path_with_namespace": "group/new-api"}},
        event_type="Merge Request Hook",
    )
    create_webhook_trigger(
        client,
        provider="github",
        payload={
            "repository": {
                "full_name": "Octo-Org/Frontend",
                "html_url": "https://github.com/Octo-Org/Frontend/",
            }
        },
    )
    create_webhook_trigger(client, provider="github", payload={"repository": {}})
    create_webhook_trigger(
        client,
        provider="unsupported",
        payload={"repository": {"full_name": "org/ignored"}},
    )

    synced = client.post("/v1/repositories/sync")

    assert synced.status_code == 200
    assert synced.json()["total"] == 2
    by_key = {
        (item["provider"], item["project_path"]): item
        for item in synced.json()["items"]
    }
    assert set(by_key) == {
        ("gitlab", "group/new-api"),
        ("github", "octo-org/frontend"),
    }
    assert all(item["tags"] == [] for item in by_key.values())
    assert (
        by_key[("gitlab", "group/new-api")]["web_url"]
        == "https://gitlab.example.com/Group/New-API"
    )
    assert (
        by_key[("github", "octo-org/frontend")]["web_url"]
        == "https://github.com/Octo-Org/Frontend"
    )

    listed = client.get("/v1/repositories", params={"limit": 200}).json()
    assert listed["total"] == 3
    unchanged = next(item for item in listed["items"] if item["id"] == existing["id"])
    assert unchanged["tags"] == ["keep-me"]
    assert unchanged["web_url"] == "https://gitlab.example.com/group/existing"

    repeated = client.post("/v1/repositories/sync")
    assert repeated.status_code == 200
    assert repeated.json() == {"items": [], "total": 0}


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
    assert client.post("/v1/repositories/sync").status_code == 401
    authorized = client.get(
        "/v1/repositories",
        headers={"X-API-Token": "repository-secret"},
    )
    assert authorized.status_code == 200


def test_repository_overview_aggregates_reviews_and_issues():
    client = build_client()
    repository = client.post(
        "/v1/repositories",
        json={
            "provider": "gitlab",
            "project_path": "group/project",
            "web_url": "https://gitlab.example.com/group/project",
            "tags": ["backend"],
        },
    ).json()
    assert client.post(
        "/v1/repositories",
        json={
            "provider": "github",
            "project_path": "org/empty",
            "tags": ["frontend"],
        },
    ).status_code == 201

    review_task = create_task(client, "review repository")
    verify_task = create_task(client, "verify repository")
    batch = client.post(
        "/v1/review-issue-batches",
        json={
            "provider": "gitlab",
            "project_path": "group/project",
            "pr_number": "42",
            "review_task_id": review_task,
            "review_head_sha": "review-sha",
        },
    ).json()
    issues = client.post(
        f"/v1/review-issue-batches/{batch['id']}/issues",
        json={
            "items": [
                {"severity": "high", "title": "accepted", "description": "fixed"},
                {
                    "severity": "medium",
                    "title": "unhandled",
                    "description": "not fixed",
                },
                {"severity": "low", "title": "pending", "description": "pending"},
            ]
        },
    ).json()["items"]
    assert client.patch(
        f"/v1/review-issue-batches/{batch['id']}",
        json={
            "status": "verifying",
            "merged_sha": "merged-sha",
            "verify_task_id": verify_task,
        },
    ).status_code == 200
    assert client.patch(
        f"/v1/review-issue-batches/{batch['id']}/issues",
        json={
            "items": [
                {"id": issues[0]["id"], "status": "accepted"},
                {"id": issues[1]["id"], "status": "not_accepted"},
            ]
        },
    ).status_code == 200

    zero_task = create_task(client, "second review repository")
    zero_batch = client.post(
        "/v1/review-issue-batches",
        json={
            "provider": "gitlab",
            "project_path": "group/project",
            "pr_number": "43",
            "review_task_id": zero_task,
        },
    ).json()
    assert client.post(
        f"/v1/review-issue-batches/{zero_batch['id']}/issues",
        json={"items": []},
    ).status_code == 201

    response = client.get("/v1/repositories/overview")
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert payload["summary"] == {
        "repository_total": 2,
        "review_total": 2,
        "issue_total": 3,
        "accepted_issues": 1,
        "unhandled_issues": 1,
        "pending_issues": 1,
        "providers": ["github", "gitlab"],
        "tags": ["backend", "frontend"],
    }
    by_id = {item["id"]: item for item in payload["items"]}
    assert by_id[repository["id"]]["review_statistics"] == {
        "review_total": 2,
        "issue_total": 3,
        "accepted_issues": 1,
        "unhandled_issues": 1,
        "pending_issues": 1,
    }
    empty = next(item for item in payload["items"] if item["provider"] == "github")
    assert empty["review_statistics"]["review_total"] == 0
    assert empty["review_statistics"]["issue_total"] == 0

    filtered = client.get(
        "/v1/repositories/overview",
        params={"provider": "gitlab", "tag": "backend"},
    )
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 1
    assert filtered.json()["summary"]["repository_total"] == 1
    assert filtered.json()["summary"]["issue_total"] == 3


def test_repository_tags_support_replace_remove_and_atomic_bulk_updates():
    client = build_client()
    first = client.post(
        "/v1/repositories",
        json={
            "provider": "gitlab",
            "project_path": "group/first",
            "tags": ["old", "shared"],
        },
    ).json()
    second = client.post(
        "/v1/repositories",
        json={
            "provider": "gitlab",
            "project_path": "group/second",
            "tags": ["shared"],
        },
    ).json()

    replaced = client.put(
        f"/v1/repositories/{first['id']}/tags",
        json={"tags": ["release", "重要", "release"]},
    )
    assert replaced.status_code == 200
    assert replaced.json()["tags"] == ["release", "重要"]

    bulk = client.patch(
        "/v1/repositories/tags",
        json={
            "repository_ids": [first["id"], second["id"]],
            "add_tags": ["Core", "新标签"],
            "remove_tags": ["release", "shared"],
        },
    )
    assert bulk.status_code == 200
    assert bulk.json()["total"] == 2
    by_id = {item["id"]: item for item in bulk.json()["items"]}
    assert by_id[first["id"]]["tags"] == ["重要", "core", "新标签"]
    assert by_id[second["id"]]["tags"] == ["core", "新标签"]
    listed = client.get("/v1/repositories").json()
    assert "新标签" in listed["summary"]["tags"]
    assert "shared" not in listed["summary"]["tags"]

    before_failure = client.get(f"/v1/repositories/{first['id']}").json()["tags"]
    failed = client.patch(
        "/v1/repositories/tags",
        json={
            "repository_ids": [first["id"], "missing-repository"],
            "add_tags": ["must-not-apply"],
        },
    )
    assert failed.status_code == 404
    assert client.get(f"/v1/repositories/{first['id']}").json()["tags"] == before_failure

    overlap = client.patch(
        "/v1/repositories/tags",
        json={
            "repository_ids": [first["id"]],
            "add_tags": ["same"],
            "remove_tags": ["same"],
        },
    )
    assert overlap.status_code == 422
    assert client.put(
        f"/v1/repositories/{second['id']}/tags", json={"tags": []}
    ).status_code == 200
