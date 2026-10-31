from concurrent.futures import ThreadPoolExecutor
from datetime import UTC
from threading import Barrier

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cc_fastapi.core.config import get_settings
from cc_fastapi.core.queue_config import get_queue_config
from cc_fastapi.db.models import AgentTask, Base, TaskResumeMode, TaskStatus, utc_now
from cc_fastapi.services.queue import TaskControlConflict, TaskQueueService
from cc_fastapi.services.worker import WorkerManager


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    config = tmp_path / "queues.yaml"
    config.write_text("default_queue: default\nqueues:\n  default:\n    workers: 1\n")
    monkeypatch.setenv("QUEUES_CONFIG_PATH", str(config))
    get_settings.cache_clear()
    get_queue_config.cache_clear()
    engine = create_engine(
        f"sqlite:///{tmp_path / 'continue.db'}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    engine.dispose()
    get_settings.cache_clear()
    get_queue_config.cache_clear()


def enqueue(db, *, max_attempts=3):
    return TaskQueueService().create_task(
        db, prompt="review code", model=None, queue_name=None, metadata=None,
        priority=0, agent_mode=True, unattended=True, max_attempts=max_attempts,
    )


def finish_task(queue, db, session_id="saved-session"):
    task = enqueue(db)
    queue.claim_next_task(db, "worker", "default")
    queue.set_task_session_id(db, task.id, session_id)
    queue.upsert_task_context(db, task.id, ["first pass"])
    assert queue.mark_success(
        db, task.id, {"session_id": session_id, "output_text": "partial"}, execution_token=task.execution_token,
    )
    db.refresh(task)
    assert task.status == TaskStatus.SUCCEEDED
    return task


def test_continue_requeues_same_session_and_keeps_history(sessions):
    queue = TaskQueueService()
    with sessions() as db:
        task = finish_task(queue, db)
        continued = queue.continue_task(db, task.id)

        assert continued.id == task.id
        assert continued.status == TaskStatus.QUEUED
        assert continued.resume_pending is True
        assert continued.resume_mode == TaskResumeMode.CONTINUE
        assert continued.session_id == "saved-session"
        assert continued.continuation_count == 1
        assert continued.attempt == 1
        assert continued.payload["prompt"] == "review code"
        assert continued.started_at is None
        assert continued.finished_at is None
        assert continued.running_remaining_seconds is None
        assert continued.queue_expire_at.replace(tzinfo=UTC) > utc_now()
        assert continued.result == {"session_id": "saved-session", "output_text": "partial"}

        logs, _ = queue.list_logs(db, task.id, 0, 100)
        continued_logs = [log for log in logs if log.event_type == "continued"]
        assert len(continued_logs) == 1
        assert continued_logs[0].metadata_json["continuation_count"] == 1
        assert continued_logs[0].metadata_json["previous_status"] == "succeeded"


def test_continue_rejects_incomplete_and_failed_tasks(sessions):
    queue = TaskQueueService()
    with sessions() as db:
        queued = enqueue(db)
        with pytest.raises(TaskControlConflict, match="已完成"):
            queue.continue_task(db, queued.id)
        queue.claim_next_task(db, "worker", "default")
        with pytest.raises(TaskControlConflict, match="已完成"):
            queue.continue_task(db, queued.id)
        db.refresh(queued)
        assert queued.status == TaskStatus.RUNNING
        assert queued.continuation_count == 0

        failed = enqueue(db, max_attempts=1)
        queue.claim_next_task(db, "worker", "default")
        db.refresh(failed)
        assert queue.mark_retry_or_failed(
            db, failed.id, "boom", execution_token=failed.execution_token,
        )
        db.refresh(failed)
        assert failed.status == TaskStatus.FAILED
        with pytest.raises(TaskControlConflict, match="已完成"):
            queue.continue_task(db, failed.id)


def test_continue_requires_a_resumable_session(sessions):
    queue = TaskQueueService()
    with sessions() as db:
        task = enqueue(db)
        queue.claim_next_task(db, "worker", "default")
        assert queue.mark_success(db, task.id, {"output_text": "no session"}, execution_token=task.execution_token)
        db.refresh(task)
        assert task.session_id is None

        with pytest.raises(TaskControlConflict, match="会话"):
            queue.continue_task(db, task.id)
        db.refresh(task)
        assert task.status == TaskStatus.SUCCEEDED
        assert task.continuation_count == 0


def test_continue_not_found_returns_none(sessions):
    with sessions() as db:
        assert TaskQueueService().continue_task(db, "missing-task") is None


def test_continue_under_global_pause_waits_for_resume_all(sessions):
    queue = TaskQueueService()
    with sessions() as db:
        task = finish_task(queue, db)
        assert queue.pause_all(db)["paused"]
        continued = queue.continue_task(db, task.id)
        assert continued.status == TaskStatus.PAUSED
        assert continued.resume_pending is True
        assert continued.paused_at is not None

        assert queue.resume_all(db)["state"] == "active"
        db.refresh(continued)
        assert continued.status == TaskStatus.QUEUED

        claimed = queue.claim_next_task(db, "worker", "default")
        assert claimed.id == task.id
        assert claimed.session_id == "saved-session"
        assert claimed.resume_mode == TaskResumeMode.CONTINUE
        assert claimed.started_at is not None


def test_concurrent_continue_only_one_click_wins(sessions):
    with sessions() as db:
        task_id = finish_task(TaskQueueService(), db).id
    barrier = Barrier(4)

    def act(_index):
        with sessions() as db:
            barrier.wait()
            try:
                TaskQueueService().continue_task(db, task_id)
                return "ok"
            except TaskControlConflict:
                db.rollback()
                return "conflict"

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(act, range(4)))

    assert results.count("ok") == 1
    with sessions() as db:
        task = db.get(AgentTask, task_id)
        assert task.continuation_count == 1
        assert task.status == TaskStatus.QUEUED


def test_worker_continuation_resumes_same_session_with_continue_instruction(sessions):
    queue = TaskQueueService()
    with sessions() as db:
        task = finish_task(queue, db)
        queue.continue_task(db, task.id)
        claimed = queue.claim_next_task(db, "worker", "default")
        assert claimed.id == task.id

        manager = WorkerManager()
        captured = {}

        def run(**kwargs):
            captured.update(kwargs)
            kwargs["on_message_update"](["after continue"])
            return {"session_id": "saved-session", "output_text": "completed"}

        manager.client.run_agent_task = run
        manager._run_task(db, task.id)

        assert captured["resume_session_id"] == "saved-session"
        assert captured["resume_reason"] == "continue"
        assert captured["prompt"] == "review code"

        db.refresh(task)
        assert task.status == TaskStatus.SUCCEEDED
        assert task.resume_pending is False
        assert task.resume_mode is None
        assert task.session_id == "saved-session"
        assert task.continuation_count == 1
        assert queue.get_task_context(db, task.id).messages_json == ["first pass", "after continue"]


def test_continuation_failure_keeps_session_and_marks_failed(sessions):
    queue = TaskQueueService()
    with sessions() as db:
        task = finish_task(queue, db)
        queue.continue_task(db, task.id)
        queue.claim_next_task(db, "worker", "default")

        manager = WorkerManager()

        def run(**kwargs):
            raise RuntimeError("Session not found")

        manager.client.run_agent_task = run
        manager._run_task(db, task.id)

        db.refresh(task)
        assert task.status == TaskStatus.FAILED
        assert task.error_message == "Session not found"
        assert task.session_id == "saved-session"
        assert task.resume_pending is False
        assert queue.claim_next_task(db, "worker", "default") is None
