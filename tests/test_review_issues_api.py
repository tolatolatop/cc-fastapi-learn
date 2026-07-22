from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cc_fastapi.api.review_issues import batch_router, issue_router
from cc_fastapi.api.tasks import router as task_router
from cc_fastapi.core.config import get_settings
from cc_fastapi.core.queue_config import get_queue_config
from cc_fastapi.db.models import Base
from cc_fastapi.db.session import get_db


@pytest.fixture(autouse=True)
def review_api_settings(monkeypatch, tmp_path):
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


def create_batch(client: TestClient, review_task_id: str, **overrides):
    payload = {
        "provider": "gitlab",
        "instance_url": "https://gitlab.example.com",
        "project_path": "group/project",
        "pr_number": "42",
        "pr_url": "https://gitlab.example.com/group/project/-/merge_requests/42",
        "review_task_id": review_task_id,
        "review_head_sha": "review-sha",
    }
    payload.update(overrides)
    return client.post("/v1/review-issue-batches", json=payload)


def test_review_issue_collection_verification_and_statistics():
    client = build_client()
    review_task_id = create_task(client, "review PR 42")
    extract_task_id = create_task(client, "extract review issues")
    verify_task_id = create_task(client, "verify review issues")

    created = create_batch(
        client,
        review_task_id,
        extract_task_id=extract_task_id,
    )
    assert created.status_code == 201
    batch = created.json()
    batch_id = batch["id"]
    assert batch["status"] == "collecting"
    assert batch["issue_count"] == 0

    duplicate = create_batch(client, review_task_id)
    assert duplicate.status_code == 409

    collected = client.post(
        f"/v1/review-issue-batches/{batch_id}/issues",
        json={
            "items": [
                {
                    "severity": "high",
                    "category": "correctness",
                    "title": "Missing null check",
                    "description": "user may be null before accessing its name",
                    "file_path": "src/user.py",
                    "line_number": 42,
                },
                {
                    "severity": "low",
                    "category": "testing",
                    "title": "Missing regression test",
                    "description": "the changed branch is not covered by a test",
                },
            ]
        },
    )
    assert collected.status_code == 201
    issues = collected.json()["items"]
    assert [item["issue_no"] for item in issues] == [1, 2]
    assert all(item["verification_status"] == "unverified" for item in issues)

    batch_after_collection = client.get(f"/v1/review-issue-batches/{batch_id}").json()
    assert batch_after_collection["status"] == "waiting_merge"
    assert batch_after_collection["issue_count"] == 2
    assert batch_after_collection["extracted_at"] is not None

    premature_verification = client.patch(
        f"/v1/review-issue-batches/{batch_id}/issues",
        json={"items": [{"id": issues[0]["id"], "status": "accepted"}]},
    )
    assert premature_verification.status_code == 409

    missing_merge_sha = client.patch(
        f"/v1/review-issue-batches/{batch_id}",
        json={"status": "verifying"},
    )
    assert missing_merge_sha.status_code == 409

    verifying = client.patch(
        f"/v1/review-issue-batches/{batch_id}",
        json={
            "status": "verifying",
            "merged_sha": "merged-sha",
            "verify_task_id": verify_task_id,
        },
    )
    assert verifying.status_code == 200
    assert verifying.json()["status"] == "verifying"

    first_result = client.patch(
        f"/v1/review-issues/{issues[0]['id']}",
        json={
            "status": "accepted",
            "note": "a null guard was added",
        },
    )
    assert first_result.status_code == 200
    assert first_result.json()["verification_status"] == "accepted"
    assert client.get(f"/v1/review-issue-batches/{batch_id}").json()["status"] == "verifying"

    second_result = client.patch(
        f"/v1/review-issue-batches/{batch_id}/issues",
        json={
            "items": [
                {
                    "id": issues[1]["id"],
                    "status": "not_accepted",
                    "note": "no matching test was added",
                }
            ]
        },
    )
    assert second_result.status_code == 200
    completed_batch = client.get(f"/v1/review-issue-batches/{batch_id}").json()
    assert completed_batch["status"] == "completed"
    assert completed_batch["verified_at"] is not None

    filtered = client.get(
        "/v1/review-issues",
        params={
            "project_path": "group/project",
            "pr_number": "42",
            "severity": "high",
            "status": "accepted",
        },
    )
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["title"] == "Missing null check"

    summary = client.get(
        "/v1/review-issues/summary",
        params={"project_path": "group/project", "pr_number": "42"},
    )
    assert summary.status_code == 200
    assert summary.json() == {
        "batch_total": 1,
        "zero_issue_batches": 0,
        "batch_status_counts": {
            "collecting": 0,
            "waiting_merge": 0,
            "verifying": 0,
            "completed": 1,
            "failed": 0,
            "cancelled": 0,
        },
        "issue_total": 2,
        "verified_issues": 2,
        "accepted_issues": 1,
        "acceptance_rate": 0.5,
        "verification_status_counts": {
            "unverified": 0,
            "accepted": 1,
            "not_accepted": 1,
        },
        "severity_counts": {
            "critical": 0,
            "high": 1,
            "medium": 0,
            "low": 1,
            "info": 0,
        },
    }


def test_zero_issue_batch_is_recorded_and_reference_errors_are_reported():
    client = build_client()

    missing_reference = create_batch(client, "missing-task")
    assert missing_reference.status_code == 404
    assert missing_reference.json()["detail"] == "review task not found"

    review_task_id = create_task(client, "review PR without issues")
    created = create_batch(client, review_task_id, pr_number="43")
    batch_id = created.json()["id"]

    collected = client.post(
        f"/v1/review-issue-batches/{batch_id}/issues",
        json={"items": []},
    )
    assert collected.status_code == 201
    assert collected.json() == {"items": [], "total": 0}

    repeated = client.post(
        f"/v1/review-issue-batches/{batch_id}/issues",
        json={"items": []},
    )
    assert repeated.status_code == 409

    completed = client.patch(
        f"/v1/review-issue-batches/{batch_id}",
        json={"status": "completed", "merged_sha": "merged-zero-sha"},
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"

    summary = client.get(
        "/v1/review-issues/summary",
        params={"project_path": "group/project", "pr_number": "43"},
    )
    assert summary.status_code == 200
    assert summary.json()["batch_total"] == 1
    assert summary.json()["zero_issue_batches"] == 1
    assert summary.json()["issue_total"] == 0
    assert summary.json()["acceptance_rate"] is None


def test_batch_status_transitions_are_guarded_and_review_task_is_filterable():
    client = build_client()
    review_task_id = create_task(client, "review guarded transitions")
    batch = create_batch(client, review_task_id, pr_number="44").json()

    skipped_collection = client.patch(
        f"/v1/review-issue-batches/{batch['id']}",
        json={"status": "completed", "merged_sha": "merge-44"},
    )
    assert skipped_collection.status_code == 409
    assert "cannot transition" in skipped_collection.json()["detail"]

    filtered = client.get(
        "/v1/review-issue-batches",
        params={"review_task_id": review_task_id},
    )
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["id"] == batch["id"]

    assert client.post(
        f"/v1/review-issue-batches/{batch['id']}/issues",
        json={"items": []},
    ).status_code == 201
    completed = client.patch(
        f"/v1/review-issue-batches/{batch['id']}",
        json={"status": "completed", "merged_sha": "merge-44"},
    )
    assert completed.status_code == 200

    reopened = client.patch(
        f"/v1/review-issue-batches/{batch['id']}",
        json={"status": "verifying"},
    )
    assert reopened.status_code == 409
    assert reopened.json()["detail"] == "terminal review issue batches are immutable"


def test_review_issue_list_and_batch_list_are_paginated_and_filterable():
    client = build_client()
    first_task = create_task(client, "review PR 1")
    second_task = create_task(client, "review PR 2")
    first_batch = create_batch(client, first_task, pr_number="1").json()
    second_batch = create_batch(client, second_task, pr_number="2").json()

    for batch, severity in [(first_batch, "critical"), (second_batch, "info")]:
        response = client.post(
            f"/v1/review-issue-batches/{batch['id']}/issues",
            json={
                "items": [
                    {
                        "severity": severity,
                        "title": f"Issue in PR {batch['pr_number']}",
                        "description": "description",
                    }
                ]
            },
        )
        assert response.status_code == 201

    batches = client.get(
        "/v1/review-issue-batches",
        params={"pr_number": "2", "status": "waiting_merge", "offset": 0, "limit": 1},
    )
    assert batches.status_code == 200
    assert batches.json()["total"] == 1
    assert batches.json()["items"][0]["pr_number"] == "2"

    issues = client.get(
        "/v1/review-issues",
        params={"severity": "critical", "offset": 0, "limit": 1},
    )
    assert issues.status_code == 200
    assert issues.json()["total"] == 1
    issue_id = issues.json()["items"][0]["id"]
    detail = client.get(f"/v1/review-issues/{issue_id}")
    assert detail.status_code == 200
    assert detail.json()["severity"] == "critical"

    assert client.get("/v1/review-issue-batches/not-found").status_code == 404
    assert client.get("/v1/review-issues/not-found").status_code == 404


def test_pull_request_issue_records_include_commits_batches_and_tasks():
    client = build_client()
    review_task_id = create_task(client, "review PR records")
    extract_task_id = create_task(client, "extract PR records")
    verify_task_id = create_task(client, "verify PR records")
    first_batch = create_batch(
        client,
        review_task_id,
        pr_number="77",
        pr_url="https://gitlab.example.com/group/project/-/merge_requests/77",
        review_head_sha="review-first",
        extract_task_id=extract_task_id,
    ).json()
    created = client.post(
        f"/v1/review-issue-batches/{first_batch['id']}/issues",
        json={
            "items": [
                {
                    "severity": "high",
                    "category": "correctness",
                    "title": "accepted issue",
                    "description": "fixed before merge",
                    "file_path": "src/accepted.py",
                    "line_number": 10,
                },
                {
                    "severity": "medium",
                    "title": "unhandled issue",
                    "description": "still present after merge",
                },
                {
                    "severity": "low",
                    "title": "pending issue",
                    "description": "verification pending",
                },
            ]
        },
    )
    assert created.status_code == 201
    issues = created.json()["items"]
    assert client.patch(
        f"/v1/review-issue-batches/{first_batch['id']}",
        json={
            "status": "verifying",
            "merged_sha": "merged-first",
            "verify_task_id": verify_task_id,
        },
    ).status_code == 200
    assert client.patch(
        f"/v1/review-issue-batches/{first_batch['id']}/issues",
        json={
            "items": [
                {
                    "id": issues[0]["id"],
                    "status": "accepted",
                    "note": "guard added",
                },
                {
                    "id": issues[1]["id"],
                    "status": "not_accepted",
                    "note": "no matching change",
                },
            ]
        },
    ).status_code == 200

    second_review_task_id = create_task(client, "second review PR records")
    second_batch = create_batch(
        client,
        second_review_task_id,
        pr_number="77",
        pr_url="https://gitlab.example.com/group/project/-/merge_requests/77",
        review_head_sha="review-second",
    ).json()
    assert client.post(
        f"/v1/review-issue-batches/{second_batch['id']}/issues",
        json={
            "items": [
                {
                    "severity": "info",
                    "title": "second pass issue",
                    "description": "found in a later review pass",
                }
            ]
        },
    ).status_code == 201

    response = client.get(
        "/v1/review-issues/pull-request",
        params={
            "provider": "gitlab",
            "project_path": "group/project",
            "pr_number": "77",
            "limit": 200,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["pull_request"] == {
        "provider": "gitlab",
        "project_path": "group/project",
        "pr_number": "77",
        "pr_url": "https://gitlab.example.com/group/project/-/merge_requests/77",
    }
    assert payload["total"] == 4
    assert payload["summary"]["batch_total"] == 2
    assert payload["summary"]["issue_total"] == 4
    assert payload["summary"]["verification_status_counts"] == {
        "unverified": 2,
        "accepted": 1,
        "not_accepted": 1,
    }
    assert payload["summary"]["batch_status_counts"]["verifying"] == 1
    assert payload["summary"]["batch_status_counts"]["waiting_merge"] == 1
    assert {item["review_head_sha"] for item in payload["items"]} == {
        "review-first",
        "review-second",
    }
    first_pass_items = [
        item for item in payload["items"] if item["review_head_sha"] == "review-first"
    ]
    assert len(first_pass_items) == 3
    assert all(item["merged_sha"] == "merged-first" for item in first_pass_items)
    accepted = next(
        item for item in payload["items"] if item["verification_status"] == "accepted"
    )
    assert accepted["verification_note"] == "guard added"
    assert accepted["file_path"] == "src/accepted.py"
    assert accepted["review_task"] == {
        "id": review_task_id,
        "status": "queued",
        "session_id": None,
    }
    assert accepted["extract_task"]["id"] == extract_task_id
    assert accepted["verify_task"]["id"] == verify_task_id
    assert accepted["batch_status"] == "verifying"
    assert accepted["batch_created_at"] is not None

    for params, expected_total in [
        ({"status": "accepted"}, 1),
        ({"severity": "info"}, 1),
        ({"category": "correctness"}, 1),
        ({"batch_status": "waiting_merge"}, 1),
        ({"commit_sha": "review-first"}, 3),
        ({"commit_sha": "merged-first"}, 3),
        ({"commit_sha": "review-second"}, 1),
    ]:
        filtered = client.get(
            "/v1/review-issues/pull-request",
            params={
                "provider": "gitlab",
                "project_path": "group/project",
                "pr_number": "77",
                **params,
            },
        )
        assert filtered.status_code == 200
        assert filtered.json()["total"] == expected_total

    paged = client.get(
        "/v1/review-issues/pull-request",
        params={
            "provider": "gitlab",
            "project_path": "group/project",
            "pr_number": "77",
            "offset": 1,
            "limit": 1,
        },
    )
    assert paged.status_code == 200
    assert paged.json()["total"] == 4
    assert len(paged.json()["items"]) == 1


def test_pull_request_issue_records_report_empty_and_missing_pull_requests():
    client = build_client()
    review_task_id = create_task(client, "review PR without findings")
    batch = create_batch(client, review_task_id, pr_number="88").json()
    assert client.post(
        f"/v1/review-issue-batches/{batch['id']}/issues", json={"items": []}
    ).status_code == 201

    empty = client.get(
        "/v1/review-issues/pull-request",
        params={
            "provider": "gitlab",
            "project_path": "group/project",
            "pr_number": "88",
        },
    )
    assert empty.status_code == 200
    assert empty.json()["items"] == []
    assert empty.json()["total"] == 0
    assert empty.json()["summary"]["batch_total"] == 1

    missing = client.get(
        "/v1/review-issues/pull-request",
        params={
            "provider": "gitlab",
            "project_path": "group/project",
            "pr_number": "404",
        },
    )
    assert missing.status_code == 404
    invalid = client.get(
        "/v1/review-issues/pull-request",
        params={
            "provider": "gitlab",
            "project_path": "group/project",
            "pr_number": "88",
            "commit_sha": " ",
        },
    )
    assert invalid.status_code == 422
