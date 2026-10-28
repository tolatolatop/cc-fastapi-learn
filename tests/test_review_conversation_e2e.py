import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from cc_fastapi.api.review_issues import batch_router, issue_router
from cc_fastapi.api.webhooks import router as webhook_router
from cc_fastapi.core.config import get_settings
from cc_fastapi.core.queue_config import get_queue_config
from cc_fastapi.db.models import (
    AgentTask,
    Base,
    ReviewIssue,
    ReviewIssueBatch,
    TaskStatus,
    WebhookTrigger,
    WorkflowRun,
    WorkflowRunStatus,
)
from cc_fastapi.db.session import get_db
from cc_fastapi.services.queue import TaskQueueService
from cc_fastapi.services.worker import WorkerManager

PROJECT_PATH = "group/review-demo"
MR_NUMBER = 27
MR_URL = f"https://gitlab.example.com/{PROJECT_PATH}/-/merge_requests/{MR_NUMBER}"
INITIAL_SHA = "a" * 40
UPDATED_SHA = "b" * 40
MERGED_SHA = "c" * 40


@pytest.fixture
def isolated_database(tmp_path: Path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'review-e2e.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.fixture
def webhook_client(monkeypatch, tmp_path: Path, isolated_database):
    queues = tmp_path / "queues.yaml"
    queues.write_text(
        "default_queue: hooks\nqueues:\n  hooks:\n    workers: 1\n",
        encoding="utf-8",
    )
    template = tmp_path / "gitlab-e2e-prompt.j2"
    template.write_text("{{ payload | tojson }}", encoding="utf-8")
    monkeypatch.setenv("QUEUES_CONFIG_PATH", str(queues))
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "e2e-secret")
    monkeypatch.setenv("GITLAB_WEBHOOK_QUEUE_NAME", "hooks")
    monkeypatch.setenv("GITLAB_WEBHOOK_PROMPT_TEMPLATE_PATH", str(template))
    monkeypatch.setenv("API_TOKEN", "")
    get_settings.cache_clear()
    get_queue_config.cache_clear()

    app = FastAPI()
    app.include_router(webhook_router)
    app.include_router(batch_router)
    app.include_router(issue_router)

    def override_db():
        with isolated_database() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        yield client
    get_settings.cache_clear()
    get_queue_config.cache_clear()


@dataclass
class SimulatedGitLab:
    client: TestClient
    event_sequence: int = 0
    note_sequence: int = 0
    threads: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    webhook_results: list[dict[str, Any]] = field(default_factory=list)

    def _post(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.event_sequence += 1
        response = self.client.post(
            "/v1/webhooks/gitlab",
            headers={
                "X-Gitlab-Token": "e2e-secret",
                "X-Gitlab-Event": event_type,
                "X-Gitlab-Event-UUID": f"e2e-event-{self.event_sequence}",
                "X-Gitlab-Webhook-UUID": f"e2e-webhook-{self.event_sequence}",
                "X-Gitlab-Instance": "https://gitlab.example.com",
            },
            json=payload,
        )
        assert response.status_code == 200
        result = response.json()
        self.webhook_results.append(result)
        return result

    @staticmethod
    def _merge_request(*, action: str, head_sha: str, state: str = "opened") -> dict[str, Any]:
        return {
            "iid": MR_NUMBER,
            "title": "Exercise review conversation",
            "url": MR_URL,
            "state": state,
            "action": action,
            "source_branch": "feature/review-e2e",
            "target_branch": "main",
            "last_commit": {"id": head_sha},
            "merge_commit_sha": MERGED_SHA if state == "merged" else None,
        }

    def code_event(self, *, action: str, head_sha: str, state: str = "opened") -> dict[str, Any]:
        return self._post(
            "Merge Request Hook",
            {
                "object_kind": "merge_request",
                "project": {
                    "path_with_namespace": PROJECT_PATH,
                    "web_url": f"https://gitlab.example.com/{PROJECT_PATH}",
                },
                "user": {"name": "Developer", "username": "developer"},
                "object_attributes": self._merge_request(
                    action=action,
                    head_sha=head_sha,
                    state=state,
                ),
            },
        )

    def _note_webhook(self, *, author: str, body: str, thread_id: str) -> dict[str, Any]:
        self.note_sequence += 1
        return self._post(
            "Note Hook",
            {
                "object_kind": "note",
                "project": {"path_with_namespace": PROJECT_PATH},
                "user": {"name": author, "username": author},
                "object_attributes": {
                    "id": self.note_sequence,
                    "note": body,
                    "noteable_type": "MergeRequest",
                    "discussion_id": thread_id,
                },
                "merge_request": self._merge_request(
                    action="update",
                    head_sha=UPDATED_SHA,
                ),
            },
        )

    def create_agent_thread(self, body: str, operation_id: str) -> str:
        thread_id = "thread-1"
        marked_body = f"{body}\n\n<!-- cc-platform-operation:{operation_id} -->"
        self.threads[thread_id] = [{"author": "review-agent", "body": marked_body}]
        self._note_webhook(author="review-agent", body=marked_body, thread_id=thread_id)
        return thread_id

    def reply(self, thread_id: str, *, author: str, body: str, operation_id: str | None = None) -> None:
        if operation_id is not None:
            body = f"{body}\n\n<!-- cc-platform-operation:{operation_id} -->"
        self.threads[thread_id].append({"author": author, "body": body})
        self._note_webhook(author=author, body=body, thread_id=thread_id)


@pytest.fixture
def simulated_platform(webhook_client: TestClient) -> SimulatedGitLab:
    return SimulatedGitLab(webhook_client)


@dataclass
class SimulatedUser:
    platform: SimulatedGitLab

    def reply(self, thread_id: str, body: str) -> None:
        self.platform.reply(thread_id, author="developer", body=body)

    def refresh_code(self) -> dict[str, Any]:
        return self.platform.code_event(action="update", head_sha=UPDATED_SHA)

    def merge(self) -> dict[str, Any]:
        return self.platform.code_event(
            action="merge",
            head_sha=UPDATED_SHA,
            state="merged",
        )


@pytest.fixture
def simulated_user(simulated_platform: SimulatedGitLab) -> SimulatedUser:
    return SimulatedUser(simulated_platform)


@dataclass
class FakeReviewLLM:
    platform: SimulatedGitLab
    client: TestClient
    invocations: list[dict[str, Any]] = field(default_factory=list)
    initial_task_id: str | None = None
    batch_id: str | None = None

    def run_agent_task(self, *, prompt: str, **_kwargs: Any) -> dict[str, Any]:
        payload = json.loads(prompt)
        self.invocations.append(payload)
        kind = payload["object_kind"]
        if kind == "note":
            round_number = sum(item["object_kind"] == "note" for item in self.invocations)
            self.platform.reply(
                "thread-1",
                author="review-agent",
                body=f"Agent review reply {round_number}",
                operation_id=f"reply-{round_number}",
            )
        else:
            action = payload["object_attributes"]["action"]
            if action == "open":
                self.platform.create_agent_thread(
                    "Potential authorization bypass",
                    "initial-review",
                )
            elif action == "update":
                self.platform.reply(
                    "thread-1",
                    author="review-agent",
                    body="Updated code removes the unsafe path",
                    operation_id="updated-code-review",
                )
            elif action == "merge":
                self.platform.reply(
                    "thread-1",
                    author="review-agent",
                    body="Merged revision verified; collecting the final report",
                    operation_id="merged-report",
                )
        return {"session_id": f"fake-session-{len(self.invocations)}", "message": "done"}

    def after_success(self, task_id: str, payload: dict[str, Any], workflow_run_id: str) -> None:
        action = payload.get("object_attributes", {}).get("action")
        if action == "open":
            self.initial_task_id = task_id
            created = self.client.post(
                "/v1/review-issue-batches",
                json={
                    "provider": "gitlab",
                    "instance_url": "https://gitlab.example.com",
                    "project_path": PROJECT_PATH,
                    "pr_number": str(MR_NUMBER),
                    "pr_url": MR_URL,
                    "review_workflow_run_id": workflow_run_id,
                    "review_task_id": task_id,
                    "review_head_sha": INITIAL_SHA,
                },
            )
            assert created.status_code == 201
            self.batch_id = created.json()["id"]
            collected = self.client.post(
                f"/v1/review-issue-batches/{self.batch_id}/issues",
                json={
                    "items": [
                        {
                            "severity": "high",
                            "category": "security",
                            "title": "Potential authorization bypass",
                            "description": "The submitted branch skips the ownership check.",
                            "file_path": "src/access.py",
                            "line_number": 42,
                        }
                    ]
                },
            )
            assert collected.status_code == 201
        elif action == "merge":
            assert self.batch_id is not None
            verifying = self.client.patch(
                f"/v1/review-issue-batches/{self.batch_id}",
                json={
                    "status": "verifying",
                    "merged_sha": MERGED_SHA,
                    "verify_task_id": task_id,
                },
            )
            assert verifying.status_code == 200
            issues = self.client.get(
                "/v1/review-issues",
                params={"batch_id": self.batch_id},
            ).json()["items"]
            verified = self.client.patch(
                f"/v1/review-issue-batches/{self.batch_id}/issues",
                json={
                    "items": [
                        {
                            "id": issues[0]["id"],
                            "status": "accepted",
                            "note": "The merged revision restores the ownership check.",
                        }
                    ]
                },
            )
            assert verified.status_code == 200


@pytest.fixture
def fake_llm(simulated_platform: SimulatedGitLab, webhook_client: TestClient) -> FakeReviewLLM:
    return FakeReviewLLM(simulated_platform, webhook_client)


def run_task(
    session_factory,
    fake_llm: FakeReviewLLM,
    webhook_result: dict[str, Any],
) -> None:
    task_id = webhook_result["task_id"]
    assert task_id is not None
    manager = WorkerManager()
    manager.client = fake_llm
    with session_factory() as db:
        claimed = TaskQueueService().claim_next_task(db, "e2e-worker", "hooks")
        assert claimed is not None and claimed.id == task_id
        manager._run_task(db, task_id)
        task = db.get(AgentTask, task_id)
        run = db.get(WorkflowRun, webhook_result["workflow_run_id"])
        assert task is not None and task.status == TaskStatus.SUCCEEDED
        assert run is not None and run.status == WorkflowRunStatus.SUCCEEDED
        payload = dict(run.payload_json)
    fake_llm.after_success(task_id, payload, webhook_result["workflow_run_id"])


def test_review_conversation_from_submission_through_merged_report(
    isolated_database,
    simulated_platform: SimulatedGitLab,
    simulated_user: SimulatedUser,
    fake_llm: FakeReviewLLM,
):
    submitted = simulated_platform.code_event(action="open", head_sha=INITIAL_SHA)
    run_task(isolated_database, fake_llm, submitted)

    for user_reply in (
        "Is the missing ownership check definitely reachable?",
        "I can add the guard in the service layer.",
        "Please verify it after I push the next revision.",
    ):
        simulated_user.reply("thread-1", user_reply)
        run_task(isolated_database, fake_llm, simulated_platform.webhook_results[-1])

    refreshed = simulated_user.refresh_code()
    run_task(isolated_database, fake_llm, refreshed)
    merged = simulated_user.merge()
    run_task(isolated_database, fake_llm, merged)

    agent_notes = [
        note["body"]
        for note in simulated_platform.threads["thread-1"]
        if note["author"] == "review-agent"
    ]
    assert len(agent_notes) == 6
    assert [payload["object_kind"] for payload in fake_llm.invocations] == [
        "merge_request",
        "note",
        "note",
        "note",
        "merge_request",
        "merge_request",
    ]
    assert all("cc-platform-operation:" in body for body in agent_notes)

    with isolated_database() as db:
        assert db.scalar(select(func.count()).select_from(AgentTask)) == 6
        assert db.scalar(select(func.count()).select_from(WebhookTrigger)) == 12
        skipped = list(
            db.scalars(
                select(WorkflowRun).where(
                    WorkflowRun.skip_reason == "agent_generated_note"
                )
            )
        )
        assert len(skipped) == 6
        assert all(run.status == WorkflowRunStatus.SKIPPED for run in skipped)
        batch = db.scalar(select(ReviewIssueBatch))
        issue = db.scalar(select(ReviewIssue))
        assert batch is not None
        assert batch.status.value == "completed"
        assert batch.review_task_id == fake_llm.initial_task_id
        assert batch.merged_sha == MERGED_SHA
        assert issue is not None
        assert issue.verification_status.value == "accepted"
        assert issue.verification_note == "The merged revision restores the ownership check."
