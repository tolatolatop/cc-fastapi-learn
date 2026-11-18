from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cc_fastapi.core.config import get_settings
from cc_fastapi.core.queue_config import get_queue_config
from cc_fastapi.db.models import Base, TaskStatus
from cc_fastapi.hooks import TaskStopHookEngine, resolve_review_report_hook
from cc_fastapi.services.comment_publisher import (
    CommentPublication,
    CommentPublishError,
    CommentPublisher,
    CommentTarget,
)
from cc_fastapi.services.queue import TaskQueueService
from cc_fastapi.services.worker import WorkerManager


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    config = tmp_path / "queues.yaml"
    config.write_text("default_queue: default\nqueues:\n  default:\n    workers: 1\n")
    monkeypatch.setenv("QUEUES_CONFIG_PATH", str(config))
    monkeypatch.setenv("CLAUDE_CWD", str(tmp_path))
    get_settings.cache_clear()
    get_queue_config.cache_clear()
    engine = create_engine(
        f"sqlite:///{tmp_path / 'hooks.db'}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    engine.dispose()
    get_settings.cache_clear()
    get_queue_config.cache_clear()


def enqueue(db, metadata, *, max_attempts=3):
    return TaskQueueService().create_task(
        db,
        prompt="review the merge request",
        model=None,
        queue_name=None,
        metadata=metadata,
        priority=0,
        agent_mode=True,
        unattended=True,
        max_attempts=max_attempts,
    )


def hook_metadata(report_path, **overrides):
    config = {
        "enabled": True,
        "report_path": str(report_path),
        "publish_to_comment": False,
        "max_continues": 3,
    }
    config.update(overrides)
    return {"hooks": {"review_report": config}}


def run_agent(manager, db, task_id, *, write_report=None, captured=None):
    def run(**kwargs):
        if captured is not None:
            captured.update(kwargs)
        kwargs["on_session_id"]("session-1")
        if write_report is not None:
            write_report()
        return {"session_id": "session-1", "output_text": "review finished"}

    manager.client.run_agent_task = run
    manager._run_task(db, task_id)


def test_hook_is_disabled_without_metadata():
    engine = TaskStopHookEngine()
    evaluation = engine.evaluate(
        task_id="task-1",
        session_id="s",
        started_at=None,
        metadata=None,
        payload={},
        output_text="done",
    )

    assert evaluation.passed is True
    assert evaluation.enabled is False


def test_metadata_can_toggle_and_configure_hook(tmp_path):
    report = tmp_path / "report.md"
    disabled = resolve_review_report_hook(
        {"hooks": {"review_report": False}}, task_id="t1"
    )
    enabled = resolve_review_report_hook(
        {
            "hooks": {
                "review_report": {
                    "enabled": True,
                    "report_path": str(report),
                    "max_retries": 5,
                }
            }
        },
        task_id="t1",
    )
    templated = resolve_review_report_hook(
        {"hooks": {"review_report": {"enabled": True, "report_path": "reports/{task_id}.md"}}},
        task_id="t1",
        base_cwd=tmp_path,
    )

    assert disabled.enabled is False
    assert enabled.enabled is True
    assert enabled.report_path == report.resolve()
    assert enabled.max_continues == 5
    assert templated.report_path == (tmp_path / "reports" / "t1.md").resolve()


def test_missing_report_continues_same_session_then_succeeds(sessions, tmp_path):
    queue = TaskQueueService()
    report = tmp_path / "review.md"
    with sessions() as db:
        task = enqueue(db, hook_metadata(report))
        queue.claim_next_task(db, "worker", "default")
        manager = WorkerManager()

        run_agent(manager, db, task.id)
        db.refresh(task)
        assert task.status == TaskStatus.QUEUED
        assert task.resume_pending is True
        assert task.continuation_count == 1
        assert task.payload["hook_state"]["review_report"]["continue_count"] == 1
        assert str(report) in task.payload["hook_feedback"]

        logs, _ = queue.list_logs(db, task.id, 0, 100)
        assert any(log.event_type == "hook_continue" for log in logs)

        queue.claim_next_task(db, "worker", "default")
        captured = {}
        run_agent(
            manager,
            db,
            task.id,
            write_report=lambda: report.write_text("# Review\nLooks good.", encoding="utf-8"),
            captured=captured,
        )
        db.refresh(task)
        assert task.status == TaskStatus.SUCCEEDED
        assert task.result["hook_results"]["enabled"] is True
        assert task.result["hook_results"]["published"] is False
        assert str(report) in captured["resume_prompt"]


def test_three_continuations_then_task_fails(sessions, tmp_path):
    queue = TaskQueueService()
    report = tmp_path / "missing.md"
    with sessions() as db:
        task = enqueue(db, hook_metadata(report, max_continues=3))
        manager = WorkerManager()

        for _ in range(4):
            queue.claim_next_task(db, "worker", "default")
            run_agent(manager, db, task.id)

        db.refresh(task)
        assert task.status == TaskStatus.FAILED
        assert task.continuation_count == 3
        assert "does not exist" in task.error_message
        assert queue.claim_next_task(db, "worker", "default") is None

        logs, _ = queue.list_logs(db, task.id, 0, 100)
        assert any(log.event_type == "hook_failed" for log in logs)


def test_report_is_published_before_task_succeeds(sessions, tmp_path):
    queue = TaskQueueService()
    report = tmp_path / "review.md"

    class FakePublisher:
        def __init__(self):
            self.calls = []

        def publish(self, target, **kwargs):
            self.calls.append((target, kwargs))
            return CommentPublication(
                provider="gitlab",
                comment_id="42",
                comment_url="https://gitlab.example/group/project/-/merge_requests/7#note_42",
                body_chars=123,
            )

    publisher = FakePublisher()
    metadata = hook_metadata(
        report,
        publish_to_comment=True,
        comment={
            "provider": "gitlab",
            "project_path": "group/project",
            "pr_number": "7",
            "instance_url": "https://gitlab.example",
        },
    )
    with sessions() as db:
        task = enqueue(db, metadata)
        queue.claim_next_task(db, "worker", "default")
        manager = WorkerManager()
        manager.hooks = TaskStopHookEngine(publisher=publisher)

        run_agent(
            manager,
            db,
            task.id,
            write_report=lambda: report.write_text("# Review\nOK", encoding="utf-8"),
        )
        db.refresh(task)

        assert task.status == TaskStatus.SUCCEEDED
        assert len(publisher.calls) == 1
        target, kwargs = publisher.calls[0]
        assert target.project_path == "group/project"
        assert target.number == "7"
        assert kwargs["report_path"] == report.resolve()
        assert task.result["hook_results"]["published"] is True
        assert task.result["hook_results"]["comment_id"] == "42"


def test_publish_failure_fails_without_consuming_continuations(sessions, tmp_path):
    queue = TaskQueueService()
    report = tmp_path / "review.md"

    class FailingPublisher:
        def publish(self, *_args, **_kwargs):
            raise CommentPublishError("GITLAB_API_TOKEN is required")

    metadata = hook_metadata(
        report,
        publish_to_comment=True,
        comment={"provider": "gitlab", "project_path": "group/project", "number": "7"},
    )
    with sessions() as db:
        task = enqueue(db, metadata)
        queue.claim_next_task(db, "worker", "default")
        manager = WorkerManager()
        manager.hooks = TaskStopHookEngine(publisher=FailingPublisher())

        run_agent(
            manager,
            db,
            task.id,
            write_report=lambda: report.write_text("# Review", encoding="utf-8"),
        )
        db.refresh(task)

        assert task.status == TaskStatus.FAILED
        assert task.continuation_count == 0
        assert "publishing it to the PR/MR comment area failed" in task.error_message


def test_stale_report_is_rejected(sessions, tmp_path):
    queue = TaskQueueService()
    report = tmp_path / "stale.md"
    report.write_text("old report", encoding="utf-8")
    old_time = datetime.now(UTC) - timedelta(hours=1)
    os.utime(report, (old_time.timestamp(), old_time.timestamp()))

    with sessions() as db:
        task = enqueue(db, hook_metadata(report))
        queue.claim_next_task(db, "worker", "default")
        manager = WorkerManager()

        run_agent(manager, db, task.id)
        db.refresh(task)

        assert task.status == TaskStatus.QUEUED
        assert "predates this run" in task.payload["hook_feedback"]


def test_comment_publisher_posts_gitlab_note(tmp_path):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["token"] = request.headers.get("PRIVATE-TOKEN")
        captured["body"] = request.content.decode("utf-8")
        return httpx.Response(201, json={"id": 99})

    settings = SimpleNamespace(
        gitlab_api_token="gl-token",
        gitlab_api_url="",
        github_api_token="",
        github_api_url="",
    )
    publisher = CommentPublisher(settings=settings, transport=httpx.MockTransport(handler))
    report = tmp_path / "report.md"
    report.write_text("# Finding\n- bug", encoding="utf-8")

    publication = publisher.publish(
        CommentTarget(
            provider="gitlab",
            project_path="group/project",
            number="7",
            instance_url="https://gitlab.example",
        ),
        task_id="task-1",
        session_id="session-1",
        report_path=report,
    )

    assert publication.comment_id == "99"
    assert captured["url"] == (
        "https://gitlab.example/api/v4/projects/group%2Fproject/merge_requests/7/notes"
    )
    assert captured["token"] == "gl-token"
    assert "cc-fastapi:review-report:task-1" in captured["body"]
    assert "bug" in captured["body"]


def test_comment_publisher_posts_github_comment(tmp_path):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = request.content.decode("utf-8")
        return httpx.Response(201, json={"id": 5, "html_url": "https://github.com/o/r/pull/3#c5"})

    settings = SimpleNamespace(
        gitlab_api_token="",
        gitlab_api_url="",
        github_api_token="gh-token",
        github_api_url="",
    )
    publisher = CommentPublisher(settings=settings, transport=httpx.MockTransport(handler))
    report = tmp_path / "report.md"
    report.write_text("report", encoding="utf-8")

    publication = publisher.publish(
        CommentTarget(provider="github", project_path="org/repo", number="3"),
        task_id="task-2",
        session_id=None,
        report_path=report,
    )

    assert publication.comment_id == "5"
    assert captured["url"] == "https://api.github.com/repos/org/repo/issues/3/comments"
    assert captured["auth"] == "Bearer gh-token"
    assert "task-2" in captured["body"]


def test_enabled_only_skips_publish_when_no_webhook_context(sessions, tmp_path):
    queue = TaskQueueService()
    report = tmp_path / "review.md"
    metadata = {"hooks": {"review_report": {"enabled": True, "report_path": str(report)}}}
    with sessions() as db:
        task = enqueue(db, metadata)
        queue.claim_next_task(db, "worker", "default")
        manager = WorkerManager()

        run_agent(
            manager,
            db,
            task.id,
            write_report=lambda: report.write_text("# Review", encoding="utf-8"),
        )
        db.refresh(task)

        assert task.status == TaskStatus.SUCCEEDED
        assert task.result["hook_results"]["published"] is False
        assert task.result["hook_results"]["comment_target_missing"] is True


def test_require_comment_target_fails_without_webhook_context(sessions, tmp_path):
    queue = TaskQueueService()
    report = tmp_path / "review.md"
    metadata = {
        "hooks": {
            "review_report": {
                "enabled": True,
                "report_path": str(report),
                "require_comment_target": True,
            }
        }
    }
    with sessions() as db:
        task = enqueue(db, metadata)
        queue.claim_next_task(db, "worker", "default")
        manager = WorkerManager()

        run_agent(
            manager,
            db,
            task.id,
            write_report=lambda: report.write_text("# Review", encoding="utf-8"),
        )
        db.refresh(task)

        assert task.status == TaskStatus.FAILED
        assert task.continuation_count == 0
        assert "requires a comment target" in task.error_message


def test_enabled_only_auto_assembles_comment_target_from_webhook_metadata(sessions, tmp_path):
    queue = TaskQueueService()
    report = tmp_path / "review.md"

    class FakePublisher:
        def __init__(self):
            self.calls = []

        def publish(self, target, **kwargs):
            self.calls.append((target, kwargs))
            return CommentPublication(provider="gitlab", comment_id="77")

    publisher = FakePublisher()
    metadata = {
        "trigger": "gitlab_webhook",
        "gitlab": {
            "project_path": "group/project",
            "change_request_number": "7",
            "instance_url": "https://gitlab.example",
        },
        "hooks": {"review_report": {"enabled": True, "report_path": str(report)}},
    }
    with sessions() as db:
        task = enqueue(db, metadata)
        queue.claim_next_task(db, "worker", "default")
        manager = WorkerManager()
        manager.hooks = TaskStopHookEngine(publisher=publisher)

        run_agent(
            manager,
            db,
            task.id,
            write_report=lambda: report.write_text("# Review", encoding="utf-8"),
        )
        db.refresh(task)

        assert task.status == TaskStatus.SUCCEEDED
        assert len(publisher.calls) == 1
        target, _kwargs = publisher.calls[0]
        assert target.provider == "gitlab"
        assert target.project_path == "group/project"
        assert target.number == "7"
        assert target.instance_url == "https://gitlab.example"


def test_report_path_defaults_to_agent_cwd_override(tmp_path):
    workdir = tmp_path / "work"
    report = workdir / "reports" / "review.md"
    report.parent.mkdir(parents=True)
    report.write_text("# Review", encoding="utf-8")

    evaluation = TaskStopHookEngine().evaluate(
        task_id="task-cwd",
        session_id="session-cwd",
        started_at=None,
        metadata={
            "hooks": {
                "review_report": {
                    "enabled": True,
                    "report_path": "reports/review.md",
                    "publish_to_comment": False,
                }
            }
        },
        payload={"claude_agent_options": {"cwd": str(workdir)}},
        output_text="done",
    )

    assert evaluation.passed is True
    assert evaluation.details["report_path"] == str(report.resolve())


def test_workflow_injects_task_metadata_and_change_request_identity(tmp_path):
    from cc_fastapi.workflows.base import WorkflowEvent
    from cc_fastapi.workflows.gitlab_prompt import GitLabPromptTaskWorkflow

    template = tmp_path / "prompt.j2"
    template.write_text("review {{ object_attributes.iid }}", encoding="utf-8")
    workflow = GitLabPromptTaskWorkflow()
    plan = workflow.before(
        WorkflowEvent(
            provider="gitlab",
            event_type="Merge Request Hook",
            payload={
                "object_kind": "merge_request",
                "project": {"path_with_namespace": "group/project"},
                "object_attributes": {
                    "iid": 7,
                    "action": "open",
                    "source_branch": "feature-7",
                    "target_branch": "main",
                },
                "commits": [],
            },
            config={
                "prompt_template_path": str(template),
                "task_metadata": {"hooks": {"review_report": {"enabled": True}}},
            },
        )
    )

    metadata = plan.tasks[0].metadata
    assert metadata["hooks"]["review_report"]["enabled"] is True
    assert metadata["gitlab"]["project_path"] == "group/project"
    assert metadata["gitlab"]["change_request_number"] == "7"


def test_single_enabled_flag_uses_all_defaults(sessions, tmp_path):
    queue = TaskQueueService()

    class FakePublisher:
        def __init__(self):
            self.calls = []

        def publish(self, target, **kwargs):
            self.calls.append((target, kwargs))
            return CommentPublication(provider="gitlab", comment_id="88")

    publisher = FakePublisher()
    metadata = {
        "trigger": "gitlab_webhook",
        "gitlab": {
            "project_path": "group/project",
            "change_request_number": "9",
            "instance_url": "https://gitlab.example",
        },
        "hooks": {"review_report": True},
    }
    with sessions() as db:
        task = enqueue(db, metadata)
        queue.claim_next_task(db, "worker", "default")
        manager = WorkerManager()
        manager.hooks = TaskStopHookEngine(publisher=publisher)
        default_report = tmp_path / "reports" / f"review-report-{task.id}.md"
        default_report.parent.mkdir(parents=True)

        run_agent(
            manager,
            db,
            task.id,
            write_report=lambda: default_report.write_text("# Review", encoding="utf-8"),
        )
        db.refresh(task)

        assert task.status == TaskStatus.SUCCEEDED
        assert task.result["hook_results"]["report_path"] == str(default_report.resolve())
        assert task.result["hook_results"]["max_continues"] == 3
        assert len(publisher.calls) == 1
