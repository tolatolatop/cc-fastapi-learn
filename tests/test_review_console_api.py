from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cc_fastapi.api.review_console import router
from cc_fastapi.core.config import get_settings
from cc_fastapi.db.models import (
    AgentTask,
    Base,
    ReviewBatchStatus,
    ReviewIssue,
    ReviewIssueBatch,
    ReviewIssueDecisionStatus,
    ReviewIssueSeverity,
    TaskStatus,
)
from cc_fastapi.db.session import get_db


@pytest.fixture(autouse=True)
def integration_token(monkeypatch):
    monkeypatch.setenv("REVIEW_CONSOLE_API_TOKEN", "integration-secret")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def build_client() -> tuple[TestClient, sessionmaker]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    with sessions() as db:
        task = AgentTask(
            id="task-1",
            status=TaskStatus.SUCCEEDED,
            queue_name="default",
            payload={},
            queue_expire_at=datetime.now(timezone.utc),
        )
        batch = ReviewIssueBatch(
            id="batch-1",
            provider="github",
            project_path="team/service",
            pr_number="17",
            review_task_id=task.id,
            status=ReviewBatchStatus.COMPLETED,
            issue_count=1,
        )
        issue = ReviewIssue(
            id="issue-1",
            batch_id=batch.id,
            issue_no=1,
            severity=ReviewIssueSeverity.HIGH,
            title="Unsafe retry",
            description="The write may run twice.",
        )
        empty_task = AgentTask(
            id="task-2",
            status=TaskStatus.SUCCEEDED,
            queue_name="default",
            payload={},
            queue_expire_at=datetime.now(timezone.utc),
        )
        empty_batch = ReviewIssueBatch(
            id="batch-2",
            provider="github",
            project_path="team/service",
            pr_number="18",
            review_task_id=empty_task.id,
            status=ReviewBatchStatus.COMPLETED,
            issue_count=0,
        )
        db.add_all([task, batch, issue, empty_task, empty_batch])
        db.commit()

    app = FastAPI()
    app.include_router(router)

    def override_db():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    return TestClient(app), sessions


def headers() -> dict[str, str]:
    return {
        "X-Review-Console-Token": "integration-secret",
        "X-Review-Actor-Id": "reviewer-1",
        "X-Review-Actor-Name": "Lin Reviewer",
    }


def test_console_api_requires_dedicated_token():
    client, _ = build_client()
    assert client.get("/v1/review-console/repositories").status_code == 401


def test_status_update_requires_rejection_reason_and_records_history():
    client, _ = build_client()
    issue = client.get("/v1/review-console/issues/issue-1", headers=headers()).json()
    rejected_without_reason = client.put(
        "/v1/review-console/issues/issue-1/status",
        headers=headers(),
        json={"status": "not_accepted", "expected_updated_at": issue["updated_at"]},
    )
    assert rejected_without_reason.status_code == 422

    updated = client.put(
        "/v1/review-console/issues/issue-1/status",
        headers=headers(),
        json={
            "status": "not_accepted",
            "reason_code": "protected_by_control",
            "note": "The reported path is protected by an idempotency key.",
            "expected_updated_at": issue["updated_at"],
        },
    )
    assert updated.status_code == 200
    assert updated.json()["status"] == "not_accepted"
    assert updated.json()["reason_code"] == "protected_by_control"
    assert updated.json()["verification_status"] == "unverified"

    history = client.get(
        "/v1/review-console/issues/issue-1/history", headers=headers()
    ).json()["items"]
    assert len(history) == 1
    assert history[0]["previous_status"] == "unverified"
    assert history[0]["new_status"] == "not_accepted"
    assert history[0]["new_reason_code"] == "protected_by_control"
    assert history[0]["dimension"] == "decision"
    assert history[0]["actor_name"] == "Lin Reviewer"
    assert history[0]["new_note"].startswith("The reported")


def test_status_update_rejects_stale_write():
    client, _ = build_client()
    issue = client.get("/v1/review-console/issues/issue-1", headers=headers()).json()
    payload = {
        "status": "accepted",
        "note": "Confirmed",
        "expected_updated_at": issue["updated_at"],
    }
    assert (
        client.put(
            "/v1/review-console/issues/issue-1/status", headers=headers(), json=payload
        ).status_code
        == 200
    )
    conflict = client.put(
        "/v1/review-console/issues/issue-1/status", headers=headers(), json=payload
    )
    assert conflict.status_code == 200
    history = client.get(
        "/v1/review-console/issues/issue-1/history", headers=headers()
    ).json()["items"]
    assert len(history) == 1
    stale_change = client.put(
        "/v1/review-console/issues/issue-1/status",
        headers=headers(),
        json={
            "status": "needs_info",
            "note": "Need more context",
            "expected_updated_at": issue["updated_at"],
        },
    )
    assert stale_change.status_code == 409


def test_repository_and_issue_listing():
    client, _ = build_client()
    repositories = client.get(
        "/v1/review-console/repositories", headers=headers()
    ).json()["items"]
    assert repositories == [
        {
            "provider": "github",
            "project_path": "team/service",
            "issue_total": 1,
            "pending_total": 1,
        }
    ]
    response = client.get(
        "/v1/review-console/issues",
        headers=headers(),
        params={"provider": "github", "project_path": "team/service"},
    )
    assert response.status_code == 200
    assert response.json()["items"][0]["title"] == "Unsafe retry"


def test_pull_request_listing_detail_and_issue_filter():
    client, _ = build_client()
    response = client.get(
        "/v1/review-console/pull-requests",
        headers=headers(),
        params={"provider": "github", "project_path": "team/service"},
    )
    assert response.status_code == 200
    by_number = {item["pr_number"]: item for item in response.json()["items"]}
    assert by_number["17"]["completion_status"] == "pending"
    assert by_number["17"]["pending_total"] == 1
    assert by_number["18"]["completion_status"] == "no_issues"

    detail = client.get(
        "/v1/review-console/pull-request",
        headers=headers(),
        params={
            "provider": "github",
            "project_path": "team/service",
            "pr_number": "17",
        },
    )
    assert detail.status_code == 200
    assert detail.json()["issue_total"] == 1

    issues = client.get(
        "/v1/review-console/issues",
        headers=headers(),
        params={
            "provider": "github",
            "project_path": "team/service",
            "pr_number": "18",
        },
    )
    assert issues.status_code == 200
    assert issues.json() == {"items": [], "total": 0}


def test_needs_info_keeps_pull_request_pending_and_allows_detail_later():
    client, _ = build_client()
    issue = client.get("/v1/review-console/issues/issue-1", headers=headers()).json()
    updated = client.put(
        "/v1/review-console/issues/issue-1/status",
        headers=headers(),
        json={"status": "needs_info", "expected_updated_at": issue["updated_at"]},
    )
    assert updated.status_code == 200
    with_detail = client.put(
        "/v1/review-console/issues/issue-1/status",
        headers=headers(),
        json={
            "status": "needs_info",
            "note": "Please confirm the retry boundary.",
            "expected_updated_at": updated.json()["updated_at"],
        },
    )
    assert with_detail.status_code == 200
    assert with_detail.json()["note"] == "Please confirm the retry boundary."
    pull_request = client.get(
        "/v1/review-console/pull-request",
        headers=headers(),
        params={
            "provider": "github",
            "project_path": "team/service",
            "pr_number": "17",
        },
    ).json()
    assert pull_request["completion_status"] == "pending"
    assert pull_request["pending_total"] == 1


def test_statistics_use_current_decisions_actor_and_repository_scope():
    client, sessions = build_client()
    first = client.get("/v1/review-console/issues/issue-1", headers=headers()).json()
    accepted = client.put(
        "/v1/review-console/issues/issue-1/status",
        headers=headers(),
        json={
            "status": "accepted",
            "note": "Confirmed",
            "expected_updated_at": first["updated_at"],
        },
    )
    assert accepted.status_code == 200

    with sessions() as db:
        task = AgentTask(
            id="task-3",
            status=TaskStatus.SUCCEEDED,
            queue_name="default",
            payload={},
            queue_expire_at=datetime.now(timezone.utc),
        )
        batch = ReviewIssueBatch(
            id="batch-3",
            provider="gitlab",
            project_path="team/other",
            pr_number="9",
            review_task_id=task.id,
            status=ReviewBatchStatus.COMPLETED,
            issue_count=1,
        )
        issue = ReviewIssue(
            id="issue-2",
            batch_id=batch.id,
            issue_no=1,
            severity=ReviewIssueSeverity.MEDIUM,
            title="False alarm",
            description="The control already exists.",
            decision_status=ReviewIssueDecisionStatus.NOT_ACCEPTED,
            decision_reason_code="false_positive",
            decision_note="Confirmed false positive",
            decided_by_id="reviewer-2",
            decided_by_name="Wang Reviewer",
            decided_at=datetime.now(timezone.utc),
        )
        db.add_all([task, batch, issue])
        db.commit()

    result = client.get("/v1/review-console/statistics", headers=headers())
    assert result.status_code == 200
    payload = result.json()
    assert payload["summary"] == {
        "valid_opinion_total": 1,
        "confirmed_total": 2,
        "false_positive_total": 1,
    }
    assert [item["actor_name"] for item in payload["contributors"]] == [
        "Lin Reviewer",
        "Wang Reviewer",
    ]
    assert payload["top_false_positive_repositories"][0]["project_path"] == "team/other"

    scoped = client.get(
        "/v1/review-console/statistics",
        headers=headers(),
        params={"repository": "github/team/service"},
    ).json()
    assert scoped["summary"]["valid_opinion_total"] == 1
    assert scoped["summary"]["false_positive_total"] == 0

    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    filtered = client.get(
        "/v1/review-console/statistics",
        headers=headers(),
        params={"created_from": future},
    ).json()
    assert filtered["summary"] == {
        "valid_opinion_total": 0,
        "confirmed_total": 0,
        "false_positive_total": 0,
    }


def test_review_records_export_current_decisions_with_scope_and_time_filter():
    client, sessions = build_client()
    issue = client.get("/v1/review-console/issues/issue-1", headers=headers()).json()
    accepted = client.put(
        "/v1/review-console/issues/issue-1/status",
        headers=headers(),
        json={
            "status": "accepted",
            "note": "Confirmed",
            "expected_updated_at": issue["updated_at"],
        },
    )
    assert accepted.status_code == 200

    decided_at = datetime.now(timezone.utc) + timedelta(seconds=5)
    with sessions() as db:
        task = AgentTask(
            id="task-4",
            status=TaskStatus.SUCCEEDED,
            queue_name="default",
            payload={},
            queue_expire_at=datetime.now(timezone.utc),
        )
        batch = ReviewIssueBatch(
            id="batch-4",
            provider="gitlab",
            project_path="team/other",
            pr_number="9",
            pr_url="https://gitlab.example.com/team/other/-/merge_requests/9",
            review_task_id=task.id,
            status=ReviewBatchStatus.COMPLETED,
            issue_count=1,
        )
        rejected = ReviewIssue(
            id="issue-4",
            batch_id=batch.id,
            issue_no=2,
            severity=ReviewIssueSeverity.LOW,
            title="False alarm",
            description="The control already exists.",
            file_path="app/service.py",
            line_number=42,
            decision_status=ReviewIssueDecisionStatus.NOT_ACCEPTED,
            decision_reason_code="false_positive",
            decision_note="Confirmed false positive",
            decided_by_id="reviewer-2",
            decided_by_name="Wang Reviewer",
            decided_at=decided_at,
        )
        db.add_all([task, batch, rejected])
        db.commit()

    listing = client.get("/v1/review-console/review-records", headers=headers())
    assert listing.status_code == 200
    payload = listing.json()
    assert payload["total"] == 2
    first, second = payload["items"]
    exported_at_value = first.pop("decided_at")
    assert first == {
        "issue_id": "issue-4",
        "provider": "gitlab",
        "project_path": "team/other",
        "pr_number": "9",
        "pr_url": "https://gitlab.example.com/team/other/-/merge_requests/9",
        "issue_no": 2,
        "severity": "low",
        "file_path": "app/service.py",
        "line_number": 42,
        "title": "False alarm",
        "status": "not_accepted",
        "reason_code": "false_positive",
        "note": "Confirmed false positive",
        "decided_by_id": "reviewer-2",
        "decided_by_name": "Wang Reviewer",
    }
    exported_at = datetime.fromisoformat(exported_at_value.replace("Z", "+00:00"))
    if exported_at.tzinfo is None:
        exported_at = exported_at.replace(tzinfo=timezone.utc)
    assert exported_at == decided_at
    assert second["issue_id"] == "issue-1"
    assert second["status"] == "accepted"
    assert second["decided_by_name"] == "Lin Reviewer"
    assert second["reason_code"] is None

    scoped = client.get(
        "/v1/review-console/review-records",
        headers=headers(),
        params={"repository": "github/team/service"},
    ).json()
    assert [item["issue_id"] for item in scoped["items"]] == ["issue-1"]

    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    filtered = client.get(
        "/v1/review-console/review-records",
        headers=headers(),
        params={"created_from": future},
    ).json()
    assert filtered == {"items": [], "total": 0}

    invalid_scope = client.get(
        "/v1/review-console/review-records",
        headers=headers(),
        params={"repository": "github"},
    )
    assert invalid_scope.status_code == 422
