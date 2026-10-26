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


def test_queue_paginates_over_3000_records_and_searches_all_pages():
    client, sessions = build_client()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with sessions() as db:
        db.add_all(
            [
                AgentTask(
                    id=f"queue-task-{index}",
                    status=TaskStatus.SUCCEEDED,
                    queue_name="default",
                    payload={},
                    queue_expire_at=now,
                )
                for index in range(3001)
            ]
        )
        db.flush()
        db.add_all(
            [
                ReviewIssueBatch(
                    id=f"queue-batch-{index}",
                    provider="gitlab",
                    project_path="team/large",
                    pr_number=str(index),
                    pr_url=f"https://gitlab.example/team/large/-/merge_requests/{index}",
                    review_task_id=f"queue-task-{index}",
                    status=ReviewBatchStatus.COMPLETED,
                    updated_at=now + timedelta(seconds=index),
                )
                for index in range(3001)
            ]
        )
        db.commit()
    params = {"repository": "gitlab/team/large", "limit": 25}
    first = client.get("/v1/review-console/queue", params=params, headers=headers())
    assert first.status_code == 200
    assert first.json()["total"] == 3001
    assert len(first.json()["items"]) == 25
    assert first.json()["items"][0]["pr_number"] == "3000"
    assert len(first.content) < 15000
    second = client.get(
        "/v1/review-console/queue", params={**params, "offset": 25}, headers=headers()
    ).json()
    assert not {item["pr_number"] for item in first.json()["items"]} & {
        item["pr_number"] for item in second["items"]
    }
    last = client.get(
        "/v1/review-console/queue", params={**params, "offset": 3000}, headers=headers()
    ).json()
    assert [item["pr_number"] for item in last["items"]] == ["0"]
    found = client.get(
        "/v1/review-console/queue",
        params={**params, "q": "team/large #42"},
        headers=headers(),
    ).json()
    assert found["total"] == 11
    assert all(item["pr_number"].startswith("42") for item in found["items"])
    url = client.get(
        "/v1/review-console/queue",
        params={
            **params,
            "q": "https://gitlab.example/team/large/-/merge_requests/3000",
        },
        headers=headers(),
    ).json()
    assert url["total"] == 1
    for query in ["%", "mergeXrequests", "not-a-repo"]:
        assert (
            client.get(
                "/v1/review-console/queue",
                params={**params, "q": query},
                headers=headers(),
            ).json()["total"]
            == 0
        )
    empty = client.get(
        "/v1/review-console/queue",
        params={"repository": "github/forbidden"},
        headers=headers(),
    ).json()
    assert empty == {"items": [], "total": 0}
    assert (
        client.get("/v1/review-console/queue", headers=headers()).json()["total"]
        == 3003
    )


def test_queue_aggregates_all_batches_and_preserves_completion_states():
    client, sessions = build_client()
    with sessions() as db:
        original = db.get(ReviewIssueBatch, "batch-1")
        original.pr_url = "https://github.example/team/service/pull/17"
        db.add(
            AgentTask(
                id="new-task",
                status=TaskStatus.SUCCEEDED,
                queue_name="default",
                payload={},
                queue_expire_at=datetime.now(timezone.utc),
            )
        )
        db.flush()
        db.add(
            ReviewIssueBatch(
                id="new-batch",
                provider="github",
                project_path="team/service",
                pr_number="17",
                review_task_id="new-task",
                status=ReviewBatchStatus.COLLECTING,
                updated_at=datetime.now(timezone.utc) + timedelta(seconds=1),
            )
        )
        db.commit()
    params = {"repository": "github/team/service", "q": "github.example"}
    item = client.get(
        "/v1/review-console/queue", headers=headers(), params=params
    ).json()["items"][0]
    assert item["batch_total"] == 2
    assert item["completion_status"] == "processing"
    assert item["pending_total"] == item["issue_total"] == 1
    assert item["pr_url"] == "https://github.example/team/service/pull/17"
    for state in ReviewBatchStatus:
        with sessions() as db:
            for batch in db.query(ReviewIssueBatch).all():
                batch.status = state
            db.commit()
        queue = client.get("/v1/review-console/queue", headers=headers()).json()[
            "items"
        ]
        by_number = {item["pr_number"]: item for item in queue}
        assert by_number["17"]["completion_status"] == (
            "processing" if state == ReviewBatchStatus.COLLECTING else "pending"
        )
        assert by_number["18"]["completion_status"] == (
            "processing"
            if state
            in {
                ReviewBatchStatus.COLLECTING,
                ReviewBatchStatus.WAITING_MERGE,
                ReviewBatchStatus.VERIFYING,
            }
            else "failed"
            if state == ReviewBatchStatus.FAILED
            else "no_issues"
        )
        expected = client.get(
            "/v1/review-console/pull-requests",
            headers=headers(),
            params={"provider": "github", "project_path": "team/service"},
        ).json()["items"]
        assert sorted(queue, key=lambda item: item["pr_number"]) == sorted(
            expected, key=lambda item: item["pr_number"]
        )


def test_issue_sort_and_severity_filter_apply_before_pagination():
    client, sessions = build_client()
    with sessions() as db:
        issue = db.get(ReviewIssue, "issue-1")
        issue.file_path = "z.py"
        issue.line_number = 1
        db.add_all(
            [
                ReviewIssue(
                    id=f"paged-{index}",
                    batch_id="batch-1",
                    issue_no=index + 2,
                    severity=ReviewIssueSeverity.LOW
                    if index == 0
                    else ReviewIssueSeverity.HIGH,
                    title=f"Issue {index}",
                    description="Details",
                    file_path="a.py",
                    line_number=100 - index,
                    decision_status=ReviewIssueDecisionStatus.ACCEPTED,
                )
                for index in range(30)
            ]
        )
        db.commit()
    params = {
        "provider": "github",
        "project_path": "team/service",
        "pr_number": "17",
        "severity": "high",
        "limit": 2,
        "sort": "location",
    }
    first = client.get(
        "/v1/review-console/issues", headers=headers(), params=params
    ).json()
    assert first["total"] == 30
    assert [item["line_number"] for item in first["items"]] == [71, 72]
    next_page = client.get(
        "/v1/review-console/issues", headers=headers(), params={**params, "offset": 2}
    ).json()
    assert [item["line_number"] for item in next_page["items"]] == [73, 74]
    pending = client.get(
        "/v1/review-console/issues",
        headers=headers(),
        params={**params, "sort": "pending_first"},
    ).json()
    assert pending["items"][0]["id"] == "issue-1"
    assert pending["items"][1]["line_number"] == 71
    assert (
        client.get(
            "/v1/review-console/issues",
            headers=headers(),
            params={**params, "sort": "invalid"},
        ).status_code
        == 422
    )


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 101},
        {"limit": 0},
        {"offset": -1},
        {"q": "x" * 201},
        {"repository": "invalid"},
    ],
)
def test_queue_rejects_invalid_paging_and_filters(params):
    client, _ = build_client()
    assert (
        client.get(
            "/v1/review-console/queue", headers=headers(), params=params
        ).status_code
        == 422
    )


def test_legacy_pull_request_status_filter_applies_before_pagination():
    client, _ = build_client()
    result = client.get(
        "/v1/review-console/pull-requests",
        headers=headers(),
        params={
            "provider": "github",
            "project_path": "team/service",
            "status": "pending",
            "limit": 1,
        },
    ).json()
    assert result["total"] == 1
    assert result["items"][0]["pr_number"] == "17"
    assert client.get("/v1/review-console/queue").status_code == 401


def test_queue_repository_scopes_follow_case_insensitive_grant_matching():
    client, sessions = build_client()
    with sessions() as db:
        batch = db.get(ReviewIssueBatch, "batch-1")
        batch.project_path = "Team/Service"
        db.commit()
    result = client.get(
        "/v1/review-console/queue",
        headers=headers(),
        params=[
            ("repository", "github/team/service"),
            ("repository", "gitlab/other/repo"),
        ],
    ).json()
    assert result["total"] == 2
    assert {item["pr_number"] for item in result["items"]} == {"17", "18"}
