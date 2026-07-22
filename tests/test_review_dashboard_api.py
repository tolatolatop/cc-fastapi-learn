from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cc_fastapi.api.review_dashboard import router as dashboard_router
from cc_fastapi.api.repositories import router as repository_router
from cc_fastapi.api.review_issues import batch_router, issue_router
from cc_fastapi.api.tasks import router as task_router
from cc_fastapi.core.config import get_settings
from cc_fastapi.core.queue_config import get_queue_config
from cc_fastapi.db.models import Base
from cc_fastapi.db.session import get_db


@pytest.fixture(autouse=True)
def review_dashboard_settings(monkeypatch, tmp_path):
    cfg = tmp_path / "queues.yaml"
    cfg.write_text(
        "default_queue: default\nqueues:\n  default:\n    workers: 1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("QUEUES_CONFIG_PATH", str(cfg))
    monkeypatch.setenv("API_TOKEN", "")
    get_settings.cache_clear()
    get_queue_config.cache_clear()
    yield
    get_settings.cache_clear()
    get_queue_config.cache_clear()


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
    app.include_router(dashboard_router)

    def override_db():
        db = testing_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


def create_task(client: TestClient, prompt: str) -> str:
    response = client.post("/v1/agent-tasks", json={"prompt": prompt})
    assert response.status_code == 200
    return response.json()["task_id"]


def create_batch(
    client: TestClient,
    *,
    pr_number: str,
    review_task_id: str,
    extract_task_id: str | None = None,
    verify_task_id: str | None = None,
    project_path: str = "group/project",
) -> dict:
    response = client.post(
        "/v1/review-issue-batches",
        json={
            "provider": "gitlab",
            "project_path": project_path,
            "pr_number": pr_number,
            "pr_url": f"https://gitlab.example.com/{project_path}/-/merge_requests/{pr_number}",
            "review_task_id": review_task_id,
            "extract_task_id": extract_task_id,
            "review_head_sha": f"review-{pr_number}",
        },
    )
    assert response.status_code == 201
    batch = response.json()
    if verify_task_id:
        batch["_verify_task_id"] = verify_task_id
    return batch


def test_review_dashboard_aggregates_outcomes_and_pull_request_tasks():
    client = build_client()
    review_task = create_task(client, "review PR 42")
    extract_task = create_task(client, "extract PR 42")
    verify_task = create_task(client, "verify PR 42")
    batch = create_batch(
        client,
        pr_number="42",
        review_task_id=review_task,
        extract_task_id=extract_task,
        verify_task_id=verify_task,
    )
    created = client.post(
        f"/v1/review-issue-batches/{batch['id']}/issues",
        json={
            "items": [
                {"severity": "high", "title": "accepted", "description": "fixed"},
                {"severity": "medium", "title": "unhandled", "description": "not fixed"},
                {"severity": "low", "title": "pending", "description": "not checked"},
            ]
        },
    )
    assert created.status_code == 201
    issues = created.json()["items"]
    verifying = client.patch(
        f"/v1/review-issue-batches/{batch['id']}",
        json={
            "status": "verifying",
            "merged_sha": "merged-42",
            "verify_task_id": verify_task,
        },
    )
    assert verifying.status_code == 200
    verified = client.patch(
        f"/v1/review-issue-batches/{batch['id']}/issues",
        json={
            "items": [
                {"id": issues[0]["id"], "status": "accepted"},
                {"id": issues[1]["id"], "status": "not_accepted"},
            ]
        },
    )
    assert verified.status_code == 200

    other_task = create_task(client, "review PR 43")
    other_batch = create_batch(
        client,
        pr_number="43",
        review_task_id=other_task,
    )
    assert client.post(
        f"/v1/review-issue-batches/{other_batch['id']}/issues",
        json={
            "items": [
                {"severity": "info", "title": "waiting", "description": "await merge"}
            ]
        },
    ).status_code == 201

    response = client.get("/v1/review-dashboard")
    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"] == {
        "pull_request_total": 2,
        "batch_total": 2,
        "issue_total": 4,
        "accepted_issues": 1,
        "merged_unhandled_issues": 1,
        "pending_issues": 2,
        "acceptance_rate": 0.5,
    }
    assert payload["total"] == 2
    assert len(payload["timeline"]) == 1
    assert payload["repositories"] == [
        {
            "provider": "gitlab",
            "project_path": "group/project",
            "pull_request_total": 2,
            "issue_total": 4,
        }
    ]

    attention = client.get(
        "/v1/review-dashboard",
        params={"project_path": "group/project", "outcome": "unhandled"},
    )
    assert attention.status_code == 200
    assert attention.json()["total"] == 1
    pull_request = attention.json()["items"][0]
    assert pull_request["pr_number"] == "42"
    assert pull_request["merged_unhandled_issues"] == 1
    assert pull_request["task_total"] == 3
    assert pull_request["task_status_counts"]["queued"] == 3

    detail = client.get(
        "/v1/review-dashboard/pull-request",
        params={
            "provider": "gitlab",
            "project_path": "group/project",
            "pr_number": "42",
        },
    )
    assert detail.status_code == 200
    detail_payload = detail.json()
    assert detail_payload["pull_request"]["issue_total"] == 3
    assert len(detail_payload["batches"]) == 1
    assert [task["role"] for task in detail_payload["tasks"]] == [
        "review",
        "extract",
        "verify",
    ]
    assert {task["id"] for task in detail_payload["tasks"]} == {
        review_task,
        extract_task,
        verify_task,
    }

    filtered_issues = client.get(
        "/v1/review-issues",
        params={
            "project_path": "group/project",
            "pr_number": "42",
            "batch_created_from": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        },
    )
    assert filtered_issues.status_code == 200
    assert filtered_issues.json()["total"] == 3


def test_review_dashboard_validates_dates_and_paginates_pull_requests():
    client = build_client()
    for number in ("1", "2"):
        task = create_task(client, f"review PR {number}")
        batch = create_batch(client, pr_number=number, review_task_id=task)
        assert client.post(
            f"/v1/review-issue-batches/{batch['id']}/issues",
            json={"items": []},
        ).status_code == 201

    paged = client.get("/v1/review-dashboard", params={"limit": 1, "offset": 1})
    assert paged.status_code == 200
    assert paged.json()["total"] == 2
    assert len(paged.json()["items"]) == 1

    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
    empty = client.get(
        "/v1/review-dashboard",
        params={"created_from": tomorrow.isoformat()},
    )
    assert empty.status_code == 200
    assert empty.json()["summary"]["issue_total"] == 0
    assert empty.json()["items"] == []

    invalid = client.get(
        "/v1/review-dashboard",
        params={
            "created_from": tomorrow.isoformat(),
            "created_to": (tomorrow - timedelta(days=2)).isoformat(),
        },
    )
    assert invalid.status_code == 422

    missing = client.get(
        "/v1/review-dashboard/pull-request",
        params={
            "provider": "gitlab",
            "project_path": "group/project",
            "pr_number": "404",
        },
    )
    assert missing.status_code == 404


def test_review_dashboard_filters_by_repository_tag():
    client = build_client()
    repositories = [
        ("group/project", ["core", "重点项目"]),
        ("group/other", ["frontend"]),
    ]
    for project_path, tags in repositories:
        created = client.post(
            "/v1/repositories",
            json={
                "provider": "gitlab",
                "project_path": project_path,
                "tags": tags,
            },
        )
        assert created.status_code == 201

        task = create_task(client, f"review {project_path}")
        batch = create_batch(
            client,
            pr_number="1",
            review_task_id=task,
            project_path=project_path,
        )
        issues = client.post(
            f"/v1/review-issue-batches/{batch['id']}/issues",
            json={
                "items": [
                    {
                        "severity": "medium",
                        "title": f"issue in {project_path}",
                        "description": "tag filter fixture",
                    }
                ]
            },
        )
        assert issues.status_code == 201

    unfiltered = client.get("/v1/review-dashboard")
    assert unfiltered.status_code == 200
    assert unfiltered.json()["tags"] == ["core", "frontend", "重点项目"]
    assert unfiltered.json()["total"] == 2

    filtered = client.get("/v1/review-dashboard", params={"tag": " CORE "})
    assert filtered.status_code == 200
    payload = filtered.json()
    assert payload["total"] == 1
    assert payload["summary"]["issue_total"] == 1
    assert payload["items"][0]["project_path"] == "group/project"
    assert payload["tags"] == ["core", "frontend", "重点项目"]

    unknown = client.get("/v1/review-dashboard", params={"tag": "unknown"})
    assert unknown.status_code == 200
    assert unknown.json()["total"] == 0
    assert unknown.json()["summary"]["issue_total"] == 0
    assert unknown.json()["items"] == []
    assert unknown.json()["tags"] == ["core", "frontend", "重点项目"]

    invalid = client.get("/v1/review-dashboard", params={"tag": " "})
    assert invalid.status_code == 422
    assert invalid.json()["detail"] == "tag must not be blank"
