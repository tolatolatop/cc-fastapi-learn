from datetime import timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cc_fastapi.admin_client import (
    AdminApiClient,
    AdminConflictError,
    PullRequestIdentity,
    VerifyResult,
    parse_collect_input,
)
from cc_fastapi.api.internal import router as internal_router
from cc_fastapi.api.review_issues import batch_router, issue_router
from cc_fastapi.cli import main
from cc_fastapi.core.config import get_settings
from cc_fastapi.db.models import (
    AgentTask,
    Base,
    TaskStatus,
    WorkflowCorrelation,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowTaskLink,
    utc_now,
)
from cc_fastapi.db.session import get_db


@pytest.fixture(autouse=True)
def admin_settings(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def github_pr_payload(number: int, *, merged: bool = False) -> dict:
    return {
        "action": "closed" if merged else "opened",
        "number": number,
        "repository": {"full_name": "Org/Project"},
        "pull_request": {
            "number": number,
            "title": f"PR {number}",
            "html_url": f"https://github.com/Org/Project/pull/{number}",
            "state": "closed" if merged else "open",
            "merged": merged,
            "merge_commit_sha": f"merge-{number}" if merged else None,
            "head": {"ref": f"feature-{number}", "sha": f"head-{number}"},
            "base": {"ref": "main"},
        },
    }


def build_client() -> tuple[TestClient, sessionmaker]:
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
    app.include_router(internal_router)
    app.include_router(batch_router)
    app.include_router(issue_router)

    def override_db():
        db = testing_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    return TestClient(app), testing_session


def seed_change_requests(session_factory: sessionmaker) -> str:
    now = utc_now()
    with session_factory() as db:
        old_run = WorkflowRun(
            workflow_name="github_prompt_task",
            provider="github",
            event_type="pull_request",
            payload_json=github_pr_payload(42),
            config_json={},
            context_json={},
            status=WorkflowRunStatus.FAILED,
            error_message="old failure",
            created_at=now - timedelta(minutes=2),
            updated_at=now - timedelta(minutes=2),
            finished_at=now - timedelta(minutes=2),
        )
        latest_run = WorkflowRun(
            workflow_name="github_prompt_task",
            provider="github",
            event_type="pull_request",
            payload_json=github_pr_payload(42, merged=True),
            config_json={},
            context_json={},
            status=WorkflowRunStatus.SUCCEEDED,
            created_at=now,
            updated_at=now,
            finished_at=now,
        )
        skipped_run = WorkflowRun(
            workflow_name="gitlab_prompt_task",
            provider="gitlab",
            event_type="Merge Request Hook",
            payload_json={
                "object_kind": "merge_request",
                "project": {"path_with_namespace": "Group/Other"},
                "object_attributes": {
                    "iid": 7,
                    "state": "opened",
                    "action": "open",
                },
            },
            config_json={},
            context_json={},
            status=WorkflowRunStatus.SKIPPED,
            skip_reason="policy",
            created_at=now - timedelta(minutes=1),
            updated_at=now - timedelta(minutes=1),
            finished_at=now - timedelta(minutes=1),
        )
        db.add_all([old_run, latest_run, skipped_run])
        db.flush()
        for run in (old_run, latest_run):
            db.add(
                WorkflowCorrelation(
                    workflow_run_id=run.id,
                    provider="github",
                    resource_type="pull_request",
                    project_path="org/project",
                    resource_id="42",
                )
            )
        db.add(
            WorkflowCorrelation(
                workflow_run_id=skipped_run.id,
                provider="gitlab",
                resource_type="merge_request",
                project_path="group/other",
                resource_id="7",
            )
        )
        task = AgentTask(
            status=TaskStatus.SUCCEEDED,
            queue_name="default",
            payload={"prompt": "review PR 42", "model": ""},
            result={"output_text": "review completed"},
            queue_expire_at=now + timedelta(hours=1),
            created_at=now,
            scheduled_at=now,
            started_at=now,
            finished_at=now,
        )
        db.add(task)
        db.flush()
        db.add(
            WorkflowTaskLink(
                workflow_run_id=latest_run.id,
                task_id=task.id,
                role="primary",
                ordinal=0,
                is_active=True,
            )
        )
        db.commit()
        return task.id


def bridge_transport(client: TestClient) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.query:
            path = f"{path}?{request.url.query.decode()}"
        response = client.request(
            request.method,
            path,
            headers=dict(request.headers),
            content=request.content,
        )
        return httpx.Response(
            response.status_code,
            headers=dict(response.headers),
            content=response.content,
            request=request,
        )

    return httpx.MockTransport(handler)


def test_change_request_api_groups_filters_and_returns_successful_task_results():
    client, session_factory = build_client()
    task_id = seed_change_requests(session_factory)

    recent = client.get("/v1/internal/change-requests", params={"limit": 1})
    assert recent.status_code == 200
    assert recent.json()["total"] == 2
    assert len(recent.json()["items"]) == 1
    assert recent.json()["items"][0]["state"] == "merged"

    opened = client.get("/v1/internal/change-requests", params={"state": "open"})
    assert opened.status_code == 200
    assert opened.json()["total"] == 1
    assert opened.json()["items"][0]["latest_task"] is None
    assert opened.json()["items"][0]["latest_workflow"]["status"] == "skipped"

    detail = client.get(
        "/v1/internal/change-requests/detail",
        params={
            "provider": "github",
            "project_path": "ORG/PROJECT",
            "pr_number": "42",
            "task_status": "succeeded",
        },
    )
    assert detail.status_code == 200
    body = detail.json()
    assert body["change_request"]["merged_sha"] == "merge-42"
    assert len(body["workflow_runs"]) == 2
    assert body["task_total"] == 1
    assert body["tasks"][0]["id"] == task_id
    assert body["tasks"][0]["output_text"] == "review completed"


def test_admin_client_collects_idempotently_and_verifies_by_issue_number():
    test_client, session_factory = build_client()
    seed_change_requests(session_factory)
    identity = PullRequestIdentity("github", "org/project", "42")
    issues = parse_collect_input(
        {
            "issues": [
                {
                    "severity": "high",
                    "category": "correctness",
                    "title": "Missing check",
                    "description": "value must be checked",
                }
            ]
        }
    )
    with AdminApiClient(
        "http://testserver", transport=bridge_transport(test_client)
    ) as client:
        collected = client.collect(identity, task_id=None, issues=issues)
        assert collected["batch"]["status"] == "waiting_merge"
        assert collected["issues"][0]["issue_no"] == 1

        repeated = client.collect(identity, task_id=None, issues=issues)
        assert repeated["idempotent"] is True

        verified = client.verify(
            identity,
            batch_id=None,
            merged_sha=None,
            results=[VerifyResult(issue_no=1, status="accepted", note="fixed")],
        )
        assert verified["batch"]["status"] == "completed"
        assert verified["batch"]["merged_sha"] == "merge-42"

        repeated_verify = client.verify(
            identity,
            batch_id=None,
            merged_sha=None,
            results=[VerifyResult(issue_no=1, status="accepted", note="fixed")],
        )
        assert repeated_verify["idempotent"] is True

        with pytest.raises(AdminConflictError):
            client.verify(
                identity,
                batch_id=None,
                merged_sha=None,
                results=[
                    VerifyResult(
                        issue_no=1,
                        status="not_accepted",
                        note="still broken",
                    )
                ],
            )


def test_cli_reports_missing_connection_configuration_as_json(monkeypatch, capsys):
    monkeypatch.delenv("CC_FASTAPI_BASE_URL", raising=False)

    exit_code = main(["pr", "recent"])

    assert exit_code == 2
    assert capsys.readouterr().err == (
        '{"ok": false, "error": "CC_FASTAPI_BASE_URL is required", "exit_code": 2}\n'
    )
