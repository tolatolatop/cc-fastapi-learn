from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import json
from threading import Barrier

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cc_fastapi.api.internal import router as internal_router
from cc_fastapi.api.webhooks import router
from cc_fastapi.core.config import get_settings
from cc_fastapi.core.queue_config import get_queue_config
from cc_fastapi.db.models import (
    AgentTask,
    AgentTaskLog,
    Base,
    TaskStatus,
    WebhookDeduplicationKey,
    WebhookTrigger,
    WorkflowCorrelation,
    WorkflowResourceLock,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowStepRun,
    WorkflowStepStatus,
    WorkflowTaskLink,
)
from cc_fastapi.db.session import get_db
from cc_fastapi.services.queue import TaskQueueService
from cc_fastapi.services.webhooks import WebhookService
from cc_fastapi.workflows import build_default_workflow_engine


@pytest.fixture(autouse=True)
def webhook_settings(monkeypatch, tmp_path):
    cfg = tmp_path / "queues.yaml"
    cfg.write_text(
        "default_queue: default\nqueues:\n  default:\n    workers: 1\n  hooks:\n    workers: 1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("QUEUES_CONFIG_PATH", str(cfg))
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "gitlab-secret")
    monkeypatch.setenv("GITLAB_WEBHOOK_QUEUE_NAME", "hooks")
    template = tmp_path / "gitlab_webhook_prompt.j2"
    template.write_text(
        "Review {{ event_type }} for {{ project.path_with_namespace }} on {{ ref }} ({{ commits | length }} commits)",
        encoding="utf-8",
    )
    monkeypatch.setenv("GITLAB_WEBHOOK_PROMPT_TEMPLATE_PATH", str(template))
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "github-secret")
    monkeypatch.setenv("GITHUB_WEBHOOK_QUEUE_NAME", "hooks")
    github_template = tmp_path / "github_webhook_prompt.j2"
    github_template.write_text(
        "Review {{ event_type }} for {{ repository.full_name }} by {{ sender.login }}",
        encoding="utf-8",
    )
    monkeypatch.setenv("GITHUB_WEBHOOK_PROMPT_TEMPLATE_PATH", str(github_template))
    monkeypatch.setenv("API_TOKEN", "")
    get_settings.cache_clear()
    get_queue_config.cache_clear()
    yield template
    get_settings.cache_clear()
    get_queue_config.cache_clear()


def build_client():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    TestingSessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(bind=engine)

    app = FastAPI()
    app.include_router(router)
    app.include_router(internal_router)

    def override_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    return TestClient(app), TestingSessionLocal


def build_concurrent_session_factory(tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'webhook-concurrency.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)


def gitlab_headers(**overrides):
    headers = {
        "X-Gitlab-Token": "gitlab-secret",
        "X-Gitlab-Event": "Push Hook",
        "X-Gitlab-Event-UUID": "event-uuid-1",
        "X-Gitlab-Webhook-UUID": "webhook-uuid-1",
        "X-Gitlab-Instance": "https://gitlab.example.com",
    }
    headers.update(overrides)
    return headers


def gitlab_payload(index: int = 1):
    return {
        "object_kind": "push",
        "ref": f"refs/heads/feature-{index}",
        "project": {"path_with_namespace": "group/project"},
        "commits": [{"id": f"commit-{index}"}],
    }


def github_payload(index: int = 1):
    return {
        "ref": f"refs/heads/feature-{index}",
        "repository": {"full_name": "octo-org/octo-repo"},
        "sender": {"login": "octocat"},
        "commits": [{"id": f"commit-{index}"}],
    }


def github_pull_request_payload(*, number: int = 7, action: str = "opened"):
    return {
        "action": action,
        "number": number,
        "repository": {"full_name": "octo-org/octo-repo"},
        "sender": {"login": "octocat"},
        "pull_request": {
            "number": number,
            "head": {"ref": f"feature-{number}", "sha": f"head-{number}"},
            "base": {"ref": "main"},
        },
    }


def post_github_webhook(
    client: TestClient,
    payload: dict,
    *,
    event_type: str = "push",
    delivery_id: str | None = "github-delivery-1",
    secret: str = "github-secret",
    **header_overrides: str,
):
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": event_type,
        "X-GitHub-Hook-ID": "321",
        "X-Hub-Signature-256": "sha256="
        + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest(),
    }
    if delivery_id is not None:
        headers["X-GitHub-Delivery"] = delivery_id
    headers.update(header_overrides)
    return client.post("/v1/webhooks/github", headers=headers, content=raw_body)


def gitlab_merge_request_payload(
    *,
    merge_request_iid: int = 7,
    action: str = "open",
    project_path: str = "group/project",
):
    return {
        "object_kind": "merge_request",
        "ref": f"refs/heads/feature-{merge_request_iid}",
        "project": {"path_with_namespace": project_path},
        "object_attributes": {
            "iid": merge_request_iid,
            "action": action,
            "source_branch": f"feature-{merge_request_iid}",
            "target_branch": "main",
        },
        "commits": [],
    }


def merge_request_headers(sequence: str):
    return gitlab_headers(
        **{
            "X-Gitlab-Event": "Merge Request Hook",
            "X-Gitlab-Event-UUID": f"mr-event-{sequence}",
            "X-Gitlab-Webhook-UUID": f"mr-webhook-{sequence}",
        }
    )


def trigger_merge_request(
    session_factory,
    *,
    sequence: str,
    merge_request_iid: int,
    action: str,
    project_path: str = "group/project",
):
    settings = get_settings()
    with session_factory() as db:
        return WebhookService().trigger_gitlab_task(
            db,
            payload=gitlab_merge_request_payload(
                merge_request_iid=merge_request_iid,
                action=action,
                project_path=project_path,
            ),
            event_type="Merge Request Hook",
            event_uuid=f"concurrent-event-{sequence}",
            webhook_uuid=f"concurrent-webhook-{sequence}",
            instance_url="https://gitlab.example.com",
            prompt_template_path=settings.resolved_gitlab_webhook_prompt_template_path,
            queue_name=settings.gitlab_webhook_queue_name or None,
        )


def test_gitlab_webhook_renders_prompt_creates_task_and_records_metadata():
    client, session_factory = build_client()
    payload = gitlab_payload()

    response = client.post("/v1/webhooks/gitlab", headers=gitlab_headers(), json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    assert body["queue_name"] == "hooks"
    assert body["deduplicated"] is False
    assert body["workflow_status"] == "running"
    assert body["skip_reason"] is None

    with session_factory() as db:
        task = db.get(AgentTask, body["task_id"])
        trigger = db.get(WebhookTrigger, body["webhook_id"])
        workflow_run = db.get(WorkflowRun, body["workflow_run_id"])
        assert task is not None
        assert trigger is not None
        assert workflow_run is not None
        assert task.status == TaskStatus.QUEUED
        assert task.payload["prompt"] == "Review Push Hook for group/project on refs/heads/feature-1 (1 commits)"
        assert task.metadata_json == {
            "trigger": "gitlab_webhook",
            "gitlab": {
                "event_type": "Push Hook",
                "event_uuid": "event-uuid-1",
                "webhook_uuid": "webhook-uuid-1",
                "instance_url": "https://gitlab.example.com",
            },
        }
        assert trigger.task_id == task.id
        assert trigger.payload_json == payload
        assert workflow_run.workflow_name == "gitlab_prompt_task"
        assert workflow_run.status == WorkflowRunStatus.RUNNING
        assert workflow_run.webhook_trigger_id == trigger.id
        link = db.scalar(select(WorkflowTaskLink).where(WorkflowTaskLink.workflow_run_id == workflow_run.id))
        assert link is not None
        assert link.task_id == task.id
        step = db.scalar(select(WorkflowStepRun).where(WorkflowStepRun.workflow_run_id == workflow_run.id))
        assert step is not None
        assert step.status == WorkflowStepStatus.SUCCEEDED

    listed = client.get("/v1/webhooks")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["task_id"] == body["task_id"]
    assert listed.json()["items"][0]["task_status"] == "queued"
    assert listed.json()["items"][0]["payload"] == payload
    assert listed.json()["items"][0]["workflow_run_id"] == body["workflow_run_id"]
    assert listed.json()["items"][0]["workflow_status"] == "running"


def test_gitlab_webhook_reuses_task_for_duplicate_webhook_uuid(webhook_settings):
    client, session_factory = build_client()
    headers = gitlab_headers()

    first = client.post("/v1/webhooks/gitlab", headers=headers, json=gitlab_payload())
    webhook_settings.unlink()
    duplicate = client.post("/v1/webhooks/gitlab", headers=headers, json=gitlab_payload(2))

    assert first.status_code == 200
    assert duplicate.status_code == 200
    assert first.json()["deduplicated"] is False
    assert duplicate.json()["deduplicated"] is True
    assert duplicate.json()["webhook_id"] == first.json()["webhook_id"]
    assert duplicate.json()["task_id"] == first.json()["task_id"]
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AgentTask)) == 1
        assert db.scalar(select(func.count()).select_from(WebhookTrigger)) == 1
        assert db.scalar(select(func.count()).select_from(WebhookDeduplicationKey)) == 1
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 1
        assert db.scalar(select(func.count()).select_from(WorkflowTaskLink)) == 1


def test_github_webhook_verifies_signature_creates_task_and_records_metadata():
    client, session_factory = build_client()
    payload = github_payload()

    response = post_github_webhook(client, payload)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    assert body["queue_name"] == "hooks"
    assert body["deduplicated"] is False
    with session_factory() as db:
        task = db.get(AgentTask, body["task_id"])
        trigger = db.get(WebhookTrigger, body["webhook_id"])
        run = db.get(WorkflowRun, body["workflow_run_id"])
        assert task is not None
        assert trigger is not None
        assert run is not None
        assert task.payload["prompt"] == "Review push for octo-org/octo-repo by octocat"
        assert task.metadata_json == {
            "trigger": "github_webhook",
            "github": {
                "event_type": "push",
                "event_uuid": "github-delivery-1",
                "webhook_uuid": "github-delivery-1",
                "instance_url": "https://github.com",
                "delivery_id": "github-delivery-1",
                "hook_id": "321",
            },
        }
        assert trigger.provider == "github"
        assert trigger.event_uuid == "github-delivery-1"
        assert trigger.webhook_uuid == "github-delivery-1"
        assert trigger.instance_url == "https://github.com"
        assert trigger.payload_json == payload
        assert run.workflow_name == "github_prompt_task"
        assert run.provider == "github"
        assert run.webhook_trigger_id == trigger.id


def test_github_webhook_reuses_task_for_duplicate_delivery_id():
    client, session_factory = build_client()

    first = post_github_webhook(client, github_payload(1), delivery_id="same-delivery")
    duplicate = post_github_webhook(client, github_payload(2), delivery_id="same-delivery")

    assert first.status_code == 200
    assert duplicate.status_code == 200
    assert duplicate.json()["deduplicated"] is True
    assert duplicate.json()["webhook_id"] == first.json()["webhook_id"]
    assert duplicate.json()["task_id"] == first.json()["task_id"]
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AgentTask)) == 1
        assert db.scalar(select(func.count()).select_from(WebhookTrigger)) == 1
        assert db.scalar(select(func.count()).select_from(WebhookDeduplicationKey)) == 1


def test_github_delivery_ids_are_scoped_separately_from_gitlab_webhook_uuids():
    client, session_factory = build_client()
    shared_id = "shared-provider-id"

    gitlab = client.post(
        "/v1/webhooks/gitlab",
        headers=gitlab_headers(**{"X-Gitlab-Webhook-UUID": shared_id}),
        json=gitlab_payload(),
    )
    github = post_github_webhook(client, github_payload(), delivery_id=shared_id)

    assert gitlab.status_code == 200
    assert github.status_code == 200
    assert github.json()["deduplicated"] is False
    assert github.json()["task_id"] != gitlab.json()["task_id"]
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(WebhookDeduplicationKey)) == 2


def test_github_pull_request_synchronize_supersedes_active_workflow():
    client, session_factory = build_client()
    opened = post_github_webhook(
        client,
        github_pull_request_payload(action="opened"),
        event_type="pull_request",
        delivery_id="pr-opened",
    )
    synchronized = post_github_webhook(
        client,
        github_pull_request_payload(action="synchronize"),
        event_type="pull_request",
        delivery_id="pr-synchronize",
    )

    assert opened.status_code == 200
    assert synchronized.status_code == 200
    with session_factory() as db:
        old_task = db.get(AgentTask, opened.json()["task_id"])
        old_run = db.get(WorkflowRun, opened.json()["workflow_run_id"])
        new_run = db.get(WorkflowRun, synchronized.json()["workflow_run_id"])
        assert old_task is not None and old_task.status == TaskStatus.CANCELLED
        assert old_run is not None and old_run.status == WorkflowRunStatus.SUPERSEDED
        assert new_run is not None and new_run.status == WorkflowRunStatus.RUNNING
        correlations = list(db.scalars(select(WorkflowCorrelation).order_by(WorkflowCorrelation.id)))
        assert len(correlations) == 2
        assert all(item.provider == "github" for item in correlations)
        assert all(item.resource_type == "pull_request" for item in correlations)
        assert all(item.project_path == "octo-org/octo-repo" for item in correlations)
        assert all(item.resource_id == "7" for item in correlations)


def test_github_webhook_rejects_invalid_signature_without_creating_records():
    client, session_factory = build_client()

    response = post_github_webhook(client, github_payload(), secret="wrong-secret")

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid github webhook signature"
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AgentTask)) == 0
        assert db.scalar(select(func.count()).select_from(WebhookTrigger)) == 0
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 0


def test_github_webhook_records_enterprise_instance_url():
    client, session_factory = build_client()

    response = post_github_webhook(
        client,
        github_payload(),
        **{"X-GitHub-Enterprise-Host": "github.example.com"},
    )

    assert response.status_code == 200
    with session_factory() as db:
        trigger = db.get(WebhookTrigger, response.json()["webhook_id"])
        assert trigger is not None
        assert trigger.instance_url == "https://github.example.com"


def test_gitlab_prompt_workflow_completes_after_task_success():
    client, session_factory = build_client()
    response = client.post("/v1/webhooks/gitlab", headers=gitlab_headers(), json=gitlab_payload())
    body = response.json()

    with session_factory() as db:
        task = db.get(AgentTask, body["task_id"])
        assert task is not None
        task.status = TaskStatus.SUCCEEDED
        task.result = {"message": "done"}
        db.commit()

        updated = build_default_workflow_engine().handle_task_terminal(db, task.id)
        assert len(updated) == 1
        run = db.get(WorkflowRun, body["workflow_run_id"])
        assert run is not None
        assert run.status == WorkflowRunStatus.SUCCEEDED
        assert run.context_json["last_task_id"] == task.id
        assert run.context_json["last_task_status"] == "succeeded"
        assert db.scalar(
            select(func.count()).select_from(WorkflowStepRun).where(WorkflowStepRun.workflow_run_id == run.id)
        ) == 2


def test_gitlab_webhook_without_webhook_uuid_is_not_deduplicated():
    client, session_factory = build_client()
    headers = gitlab_headers()
    headers.pop("X-Gitlab-Webhook-UUID")

    first = client.post("/v1/webhooks/gitlab", headers=headers, json=gitlab_payload())
    second = client.post("/v1/webhooks/gitlab", headers=headers, json=gitlab_payload())

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["deduplicated"] is False
    assert second.json()["deduplicated"] is False
    assert second.json()["task_id"] != first.json()["task_id"]
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AgentTask)) == 2
        assert db.scalar(select(func.count()).select_from(WebhookTrigger)) == 2
        assert db.scalar(select(func.count()).select_from(WebhookDeduplicationKey)) == 0


def test_merge_request_update_supersedes_active_workflow_and_cancels_task():
    client, session_factory = build_client()
    opened = client.post(
        "/v1/webhooks/gitlab",
        headers=merge_request_headers("open"),
        json=gitlab_merge_request_payload(action="open"),
    )
    assert opened.status_code == 200

    with session_factory() as db:
        old_task = db.get(AgentTask, opened.json()["task_id"])
        assert old_task is not None
        old_task.status = TaskStatus.RUNNING
        old_task.worker_id = "test-worker"
        db.commit()

    updated = client.post(
        "/v1/webhooks/gitlab",
        headers=merge_request_headers("update"),
        json=gitlab_merge_request_payload(action="update"),
    )

    assert updated.status_code == 200
    assert updated.json()["status"] == "queued"
    assert updated.json()["workflow_status"] == "running"
    with session_factory() as db:
        old_task = db.get(AgentTask, opened.json()["task_id"])
        old_run = db.get(WorkflowRun, opened.json()["workflow_run_id"])
        new_task = db.get(AgentTask, updated.json()["task_id"])
        new_run = db.get(WorkflowRun, updated.json()["workflow_run_id"])
        assert old_task is not None and old_task.status == TaskStatus.CANCELLED
        assert old_run is not None and old_run.status == WorkflowRunStatus.SUPERSEDED
        assert new_task is not None and new_task.status == TaskStatus.QUEUED
        assert new_run is not None and new_run.status == WorkflowRunStatus.RUNNING
        assert old_run.context_json["superseded_by_workflow_run_id"] == new_run.id
        assert db.scalar(select(func.count()).select_from(WorkflowCorrelation)) == 2
        cancellation_log = db.scalar(
            select(AgentTaskLog)
            .where(AgentTaskLog.task_id == old_task.id, AgentTaskLog.event_type == "cancelled")
            .limit(1)
        )
        assert cancellation_log is not None
        assert cancellation_log.metadata_json == {"reason": f"superseded_by_workflow:{new_run.id}"}


def test_concurrent_updates_leave_only_one_running_workflow_for_merge_request(tmp_path):
    session_factory = build_concurrent_session_factory(tmp_path)
    trigger_merge_request(
        session_factory,
        sequence="initial",
        merge_request_iid=41,
        action="open",
    )
    barrier = Barrier(2)

    def update_once(sequence: str):
        barrier.wait()
        return trigger_merge_request(
            session_factory,
            sequence=sequence,
            merge_request_iid=41,
            action="update",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(update_once, ["update-a", "update-b"]))

    with session_factory() as db:
        runs = list(
            db.scalars(
                select(WorkflowRun)
                .join(WorkflowCorrelation, WorkflowCorrelation.workflow_run_id == WorkflowRun.id)
                .where(
                    WorkflowCorrelation.provider == "gitlab",
                    WorkflowCorrelation.resource_type == "merge_request",
                    WorkflowCorrelation.project_path == "group/project",
                    WorkflowCorrelation.resource_id == "41",
                )
            )
        )
        tasks = list(
            db.scalars(
                select(AgentTask)
                .join(WorkflowTaskLink, WorkflowTaskLink.task_id == AgentTask.id)
                .join(WorkflowCorrelation, WorkflowCorrelation.workflow_run_id == WorkflowTaskLink.workflow_run_id)
                .where(WorkflowCorrelation.resource_id == "41")
            )
        )
        assert len(runs) == 3
        assert sum(run.status == WorkflowRunStatus.RUNNING for run in runs) == 1
        assert sum(run.status == WorkflowRunStatus.SUPERSEDED for run in runs) == 2
        assert sum(task.status == TaskStatus.QUEUED for task in tasks) == 1
        assert sum(task.status == TaskStatus.CANCELLED for task in tasks) == 2
        assert db.scalar(select(func.count()).select_from(WorkflowResourceLock)) == 1


def test_concurrent_independent_merge_requests_do_not_modify_each_other(tmp_path):
    session_factory = build_concurrent_session_factory(tmp_path)
    barrier = Barrier(2)

    def open_once(merge_request_iid: int):
        barrier.wait()
        return trigger_merge_request(
            session_factory,
            sequence=f"independent-{merge_request_iid}",
            merge_request_iid=merge_request_iid,
            action="open",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(open_once, [51, 52]))

    with session_factory() as db:
        runs = list(db.scalars(select(WorkflowRun)))
        tasks = list(db.scalars(select(AgentTask)))
        assert len(runs) == 2
        assert all(run.status == WorkflowRunStatus.RUNNING for run in runs)
        assert len(tasks) == 2
        assert all(task.status == TaskStatus.QUEUED for task in tasks)
        assert db.scalar(select(func.count()).select_from(WorkflowResourceLock)) == 2


def test_merge_request_update_does_not_cancel_other_merge_requests_or_completed_tasks():
    client, session_factory = build_client()
    completed = client.post(
        "/v1/webhooks/gitlab",
        headers=merge_request_headers("completed"),
        json=gitlab_merge_request_payload(merge_request_iid=7, action="open"),
    ).json()
    other = client.post(
        "/v1/webhooks/gitlab",
        headers=merge_request_headers("other"),
        json=gitlab_merge_request_payload(merge_request_iid=8, action="open"),
    ).json()
    with session_factory() as db:
        completed_task = db.get(AgentTask, completed["task_id"])
        assert completed_task is not None
        completed_task.status = TaskStatus.SUCCEEDED
        completed_task.result = {"review": "done"}
        db.commit()
        build_default_workflow_engine().handle_task_terminal(db, completed_task.id)

    updated = client.post(
        "/v1/webhooks/gitlab",
        headers=merge_request_headers("completed-update"),
        json=gitlab_merge_request_payload(merge_request_iid=7, action="update"),
    )
    assert updated.status_code == 200

    with session_factory() as db:
        completed_task = db.get(AgentTask, completed["task_id"])
        completed_run = db.get(WorkflowRun, completed["workflow_run_id"])
        other_task = db.get(AgentTask, other["task_id"])
        other_run = db.get(WorkflowRun, other["workflow_run_id"])
        assert completed_task is not None and completed_task.status == TaskStatus.SUCCEEDED
        assert completed_run is not None and completed_run.status == WorkflowRunStatus.SUCCEEDED
        assert other_task is not None and other_task.status == TaskStatus.QUEUED
        assert other_run is not None and other_run.status == WorkflowRunStatus.RUNNING


def test_internal_api_lists_task_contents_for_exact_merge_request(monkeypatch):
    client, _ = build_client()
    opened = client.post(
        "/v1/webhooks/gitlab",
        headers=merge_request_headers("api-open"),
        json=gitlab_merge_request_payload(merge_request_iid=21, action="open"),
    ).json()
    updated = client.post(
        "/v1/webhooks/gitlab",
        headers=merge_request_headers("api-update"),
        json=gitlab_merge_request_payload(merge_request_iid=21, action="update"),
    ).json()
    client.post(
        "/v1/webhooks/gitlab",
        headers=merge_request_headers("api-other-project"),
        json=gitlab_merge_request_payload(
            merge_request_iid=21,
            action="open",
            project_path="group/other-project",
        ),
    )

    monkeypatch.setenv("API_TOKEN", "internal-secret")
    get_settings.cache_clear()
    unauthorized = client.get(
        "/v1/internal/gitlab/merge-request-tasks",
        params={"project_path": "group/project", "merge_request_iid": 21},
    )
    response = client.get(
        "/v1/internal/gitlab/merge-request-tasks",
        params={"project_path": "group/project", "merge_request_iid": 21},
        headers={"X-API-Token": "internal-secret"},
    )

    assert unauthorized.status_code == 401
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert [item["id"] for item in body["items"]] == [updated["task_id"], opened["task_id"]]
    assert body["items"][0]["status"] == "queued"
    assert body["items"][0]["workflow_status"] == "running"
    assert body["items"][0]["prompt"].startswith("Review Merge Request Hook")
    assert body["items"][0]["payload"]["prompt"] == body["items"][0]["prompt"]
    assert body["items"][0]["webhook_id"] == updated["webhook_id"]
    assert body["items"][1]["status"] == "cancelled"
    assert body["items"][1]["workflow_status"] == "superseded"
    assert body["items"][1]["superseded_by_workflow_run_id"] == updated["workflow_run_id"]


def test_duplicate_legacy_trigger_is_adopted_into_workflow():
    client, session_factory = build_client()
    with session_factory() as db:
        task = TaskQueueService().create_task(
            db,
            prompt="legacy prompt",
            model=None,
            queue_name="hooks",
            metadata={"trigger": "gitlab_webhook"},
            priority=0,
            agent_mode=True,
            unattended=True,
            max_attempts=None,
        )
        trigger = WebhookTrigger(
            provider="gitlab",
            event_type="Push Hook",
            event_uuid="legacy-event",
            webhook_uuid="legacy-webhook",
            instance_url="https://gitlab.example.com",
            task_id=task.id,
            payload_json=gitlab_payload(),
        )
        db.add(trigger)
        db.flush()
        db.add(
            WebhookDeduplicationKey(
                provider="gitlab",
                webhook_uuid="legacy-webhook",
                webhook_trigger_id=trigger.id,
            )
        )
        db.commit()

    response = client.post(
        "/v1/webhooks/gitlab",
        headers=gitlab_headers(**{"X-Gitlab-Webhook-UUID": "legacy-webhook"}),
        json=gitlab_payload(2),
    )

    assert response.status_code == 200
    assert response.json()["deduplicated"] is True
    assert response.json()["task_id"] == task.id
    assert response.json()["workflow_status"] == "running"
    with session_factory() as db:
        run = db.get(WorkflowRun, response.json()["workflow_run_id"])
        assert run is not None
        assert run.context_json == {"legacy_adopted": True}
        assert run.webhook_trigger_id == trigger.id


def test_gitlab_webhook_rejects_invalid_token_without_creating_records():
    client, session_factory = build_client()

    response = client.post(
        "/v1/webhooks/gitlab",
        headers=gitlab_headers(**{"X-Gitlab-Token": "wrong"}),
        json=gitlab_payload(),
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid gitlab webhook token"
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AgentTask)) == 0
        assert db.scalar(select(func.count()).select_from(WebhookTrigger)) == 0
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 0


def test_gitlab_webhook_template_error_does_not_create_task(webhook_settings):
    webhook_settings.write_text("{{ missing.value }}", encoding="utf-8")
    client, session_factory = build_client()

    response = client.post("/v1/webhooks/gitlab", headers=gitlab_headers(), json=gitlab_payload())

    assert response.status_code == 400
    assert "failed to render webhook prompt" in response.json()["detail"]
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AgentTask)) == 0
        assert db.scalar(select(func.count()).select_from(WebhookTrigger)) == 0
        workflow_run = db.scalar(select(WorkflowRun))
        assert workflow_run is not None
        assert workflow_run.status == WorkflowRunStatus.FAILED
        assert "failed to render webhook prompt" in (workflow_run.error_message or "")


def test_gitlab_webhook_missing_template_file_does_not_create_task(monkeypatch, tmp_path):
    missing_template = tmp_path / "missing.j2"
    monkeypatch.setenv("GITLAB_WEBHOOK_PROMPT_TEMPLATE_PATH", str(missing_template))
    get_settings.cache_clear()
    client, session_factory = build_client()

    response = client.post("/v1/webhooks/gitlab", headers=gitlab_headers(), json=gitlab_payload())

    assert response.status_code == 400
    assert "failed to load webhook prompt template" in response.json()["detail"]
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AgentTask)) == 0
        assert db.scalar(select(func.count()).select_from(WebhookTrigger)) == 0


def test_list_webhook_triggers_supports_pagination():
    client, _ = build_client()
    for index in range(3):
        event_type = "Merge Request Hook" if index == 2 else "Push Hook"
        response = client.post(
            "/v1/webhooks/gitlab",
            headers=gitlab_headers(
                **{
                    "X-Gitlab-Event": event_type,
                    "X-Gitlab-Event-UUID": f"event-uuid-{index}",
                    "X-Gitlab-Webhook-UUID": f"webhook-uuid-{index}",
                }
            ),
            json=gitlab_payload(index),
        )
        assert response.status_code == 200

    response = client.get("/v1/webhooks", params={"offset": 1, "limit": 2})

    assert response.status_code == 200
    assert response.json()["total"] == 3
    assert len(response.json()["items"]) == 2
    assert response.json()["summary"] == {
        "total": 3,
        "event_types": ["Merge Request Hook", "Push Hook"],
        "providers": ["gitlab"],
    }

    searched = client.get("/v1/webhooks", params={"q": "feature-1"})
    assert searched.status_code == 200
    assert searched.json()["total"] == 1
    assert searched.json()["items"][0]["payload"]["ref"] == "refs/heads/feature-1"

    filtered = client.get("/v1/webhooks", params={"event_type": "Merge Request Hook"})
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 1
    assert filtered.json()["items"][0]["event_type"] == "Merge Request Hook"


def test_list_webhook_triggers_filters_by_provider():
    client, _ = build_client()
    client.post("/v1/webhooks/gitlab", headers=gitlab_headers(), json=gitlab_payload())
    post_github_webhook(client, github_payload())

    response = client.get("/v1/webhooks", params={"provider": "github"})

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["provider"] == "github"
    assert response.json()["summary"]["providers"] == ["github", "gitlab"]


def test_list_webhook_triggers_uses_api_token(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "list-secret")
    get_settings.cache_clear()
    client, _ = build_client()

    unauthorized = client.get("/v1/webhooks")
    authorized = client.get("/v1/webhooks", headers={"X-Api-Token": "list-secret"})

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
