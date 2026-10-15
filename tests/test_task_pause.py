from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, timedelta
from threading import Barrier

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from cc_fastapi.core.config import get_settings
from cc_fastapi.core.queue_config import get_queue_config
from cc_fastapi.db.models import AgentTask, Base, TaskStatus, utc_now
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
        f"sqlite:///{tmp_path / 'pause.db'}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    engine.dispose()
    get_settings.cache_clear()
    get_queue_config.cache_clear()


def enqueue(db):
    return TaskQueueService().create_task(
        db, prompt="review code", model=None, queue_name=None, metadata=None,
        priority=0, agent_mode=True, unattended=True, max_attempts=1,
    )


def test_pause_queued_and_new_tasks_freezes_deadlines(sessions, monkeypatch):
    queue = TaskQueueService()
    with sessions() as db:
        task = enqueue(db)
        deadline = task.queue_expire_at.replace(tzinfo=UTC)
        assert queue.pause_all(db)["state"] == "paused"
        db.refresh(task)
        paused_at = task.paused_at
        assert queue.pause_all(db)["paused_count"] == 1
        db.refresh(task)
        assert task.paused_at == paused_at
        new_task = enqueue(db)
        assert new_task.status == TaskStatus.PAUSED
        assert queue.claim_next_task(db, "worker", "default") is None
        later = utc_now() + timedelta(days=3)
        monkeypatch.setattr("cc_fastapi.services.queue.utc_now", lambda: later)
        assert queue.abandon_expired_queued(db) == 0
        assert queue.abandon_expired_running(db) == 0
        assert queue.resume_all(db)["state"] == "active"
        db.refresh(task)
        assert task.queue_expire_at.replace(tzinfo=UTC) > deadline + timedelta(days=2)
        assert queue.claim_next_task(db, "worker", "default").id == task.id
        assert task.attempt == 1


def test_running_pause_ack_resume_keeps_task_session_attempt_and_budget(sessions):
    queue = TaskQueueService()
    with sessions() as db:
        task = enqueue(db)
        queue.claim_next_task(db, "worker", "default")
        old_token = task.execution_token
        task.running_expire_at = utc_now() + timedelta(seconds=91)
        db.commit()
        queue.set_task_session_id(db, task.id, "saved-session")
        started = task.started_at
        assert queue.pause_all(db)["state"] == "pausing"
        with pytest.raises(TaskControlConflict, match="暂停中"):
            queue.resume_all(db)
        assert queue.mark_paused(db, task.id, old_token)
        assert queue.control_status(db)["state"] == "paused"
        queue.resume_all(db)
        resumed = queue.claim_next_task(db, "new-worker", "default")
        assert resumed.id == task.id
        assert resumed.session_id == "saved-session"
        assert resumed.attempt == 1
        assert resumed.started_at == started
        remaining = (resumed.running_expire_at.replace(tzinfo=UTC) - utc_now()).total_seconds()
        assert 85 < remaining <= 92
        assert resumed.execution_token != old_token
        assert not queue.mark_success(db, task.id, {"stale": True}, execution_token=old_token)
        queue.upsert_task_context(db, task.id, ["stale output"], execution_token=old_token)
        assert queue.get_task_context(db, task.id) is None
        assert queue.mark_success(db, task.id, {"ok": True}, execution_token=resumed.execution_token)


def test_missing_session_blocks_resume_without_restarting(sessions):
    queue = TaskQueueService()
    with sessions() as db:
        task = enqueue(db)
        queue.claim_next_task(db, "worker", "default")
        token = task.execution_token
        queue.pause_all(db)
        queue.mark_paused(db, task.id, token)
        with pytest.raises(TaskControlConflict, match="缺少可恢复的会话"):
            queue.resume_all(db)
        db.refresh(task)
        assert task.status == TaskStatus.PAUSED
        assert task.attempt == 1
        assert queue.control_status(db)["paused"]


def test_pause_before_agent_starts_can_resume_as_new_execution(sessions):
    queue = TaskQueueService()
    with sessions() as db:
        task = enqueue(db)
        queue.claim_next_task(db, "worker", "default")
        queue.pause_all(db)
        WorkerManager()._run_task(db, task.id)
        queue.resume_all(db)
        task = queue.claim_next_task(db, "worker", "default")
        assert not task.resume_pending
        assert task.attempt == 1


def test_restart_keeps_pause_and_cancelled_tasks_are_not_resumed(sessions):
    queue = TaskQueueService()
    with sessions() as db:
        running = enqueue(db)
        queue.claim_next_task(db, "worker", "default")
        queue.set_task_session_id(db, running.id, "saved-session")
        cancelled = enqueue(db)
        queue.pause_all(db)
        queue.cancel_task(db, cancelled.id)
    with sessions() as db:
        restarted = TaskQueueService()
        assert restarted.recover_orphan_running_on_startup(db) == 1
        assert restarted.control_status(db)["state"] == "paused"
        assert restarted.claim_next_task(db, "worker", "default") is None
        assert restarted.abandon_running_on_shutdown(db) == 0
        restarted.resume_all(db)
        assert db.get(AgentTask, cancelled.id).status == TaskStatus.CANCELLED
        assert restarted.claim_next_task(db, "worker", "default").id == running.id


def test_completion_racing_pause_is_not_resurrected(sessions):
    queue = TaskQueueService()
    with sessions() as db:
        task = enqueue(db)
        queue.claim_next_task(db, "worker", "default")
        token = task.execution_token
        queue.pause_all(db)
        assert queue.mark_success(db, task.id, {"done": True}, execution_token=token)
        assert not queue.mark_paused(db, task.id, token)
        queue.resume_all(db)
        db.refresh(task)
        assert task.status == TaskStatus.SUCCEEDED


def test_concurrent_pause_claim_and_enqueue_cannot_escape_pause(sessions):
    with sessions() as db:
        enqueue(db)
    barrier = Barrier(3)

    def act(action):
        with sessions() as db:
            barrier.wait()
            queue = TaskQueueService()
            if action == "pause":
                queue.pause_all(db)
            elif action == "claim":
                queue.claim_next_task(db, "worker", "default")
            else:
                enqueue(db)

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(act, ["pause", "claim", "enqueue"]))
    with sessions() as db:
        assert set(db.scalars(select(AgentTask.status))) <= {TaskStatus.PAUSED, TaskStatus.PAUSING}
        assert TaskQueueService().claim_next_task(db, "late-worker", "default") is None


def test_worker_resume_uses_same_session_and_appends_context(sessions):
    queue = TaskQueueService()
    with sessions() as db:
        task = enqueue(db)
        queue.claim_next_task(db, "worker", "default")
        queue.set_task_session_id(db, task.id, "saved-session")
        queue.upsert_task_context(db, task.id, ["before pause"])
        queue.pause_all(db)
        queue.mark_paused(db, task.id, task.execution_token)
        queue.resume_all(db)
        queue.claim_next_task(db, "worker", "default")
        manager = WorkerManager()

        def run(**kwargs):
            assert kwargs["resume_session_id"] == "saved-session"
            kwargs["on_message_update"](["after resume"])
            return {"session_id": "saved-session", "output_text": "done"}

        manager.client.run_agent_task = run
        manager._run_task(db, task.id)
        db.refresh(task)
        assert task.status == TaskStatus.SUCCEEDED
        assert task.attempt == 1
        assert queue.get_task_context(db, task.id).messages_json == ["before pause", "after resume"]


def test_failed_session_resume_stays_paused_without_retry(sessions):
    queue = TaskQueueService()
    with sessions() as db:
        task = enqueue(db)
        queue.claim_next_task(db, "worker", "default")
        queue.set_task_session_id(db, task.id, "missing-session")
        queue.pause_all(db)
        queue.mark_paused(db, task.id, task.execution_token)
        queue.resume_all(db)
        queue.claim_next_task(db, "worker", "default")
        manager = WorkerManager()

        def run(**kwargs):
            raise RuntimeError("Session not found")

        manager.client.run_agent_task = run
        manager._run_task(db, task.id)
        db.refresh(task)
        assert task.status == TaskStatus.PAUSED
        assert task.error_message == "Session not found"
        assert task.attempt == 1
        assert queue.claim_next_task(db, "worker", "default") is None


def test_unconfirmed_sdk_shutdown_stays_pausing(sessions):
    from cc_fastapi.services.claude_client import AgentTaskStopError

    queue = TaskQueueService()
    with sessions() as db:
        task = enqueue(db)
        queue.claim_next_task(db, 'worker', 'default')
        manager = WorkerManager()

        def run(**kwargs):
            kwargs['on_session_id']('saved-session')
            queue.pause_all(db)
            raise AgentTaskStopError('shutdown did not complete')

        manager.client.run_agent_task = run
        manager._run_task(db, task.id)
        db.refresh(task)
        assert task.status == TaskStatus.PAUSING
        assert task.error_message == 'shutdown did not complete'
        with pytest.raises(TaskControlConflict, match='暂停中'):
            queue.resume_all(db)


def test_cancelling_a_pausing_task_waits_for_execution_to_stop(sessions):
    queue = TaskQueueService()
    with sessions() as db:
        task = enqueue(db)
        queue.claim_next_task(db, 'worker', 'default')
        token = task.execution_token
        queue.pause_all(db)
        queue.cancel_task(db, task.id)
        assert queue.control_status(db)['state'] == 'pausing'
        with pytest.raises(TaskControlConflict, match='暂停中'):
            queue.resume_all(db)
        queue.release_execution(db, task.id, token)
        assert queue.control_status(db)['state'] == 'paused'
        queue.resume_all(db)
        db.refresh(task)
        assert task.status == TaskStatus.CANCELLED


def test_cancel_before_worker_execution_releases_pending_stop(sessions):
    queue = TaskQueueService()
    with sessions() as db:
        task = enqueue(db)
        queue.claim_next_task(db, 'worker', 'default')
        queue.cancel_task(db, task.id)
        queue.pause_all(db)
        assert queue.control_status(db)['state'] == 'pausing'
        WorkerManager()._run_task(db, task.id)
        assert queue.control_status(db)['state'] == 'paused'
        assert queue.resume_all(db)['state'] == 'active'
