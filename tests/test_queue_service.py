from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from cc_fastapi.core.config import get_settings
from cc_fastapi.core.queue_config import get_queue_config
from cc_fastapi.db.models import AgentTask, AgentTaskContext, AgentTaskLog, Base, TaskStatus, utc_now
from cc_fastapi.services.queue import QueueNotFoundError, TaskQueueService


def make_db():
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, future=True)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)()


@pytest.fixture(autouse=True)
def queue_config_file(monkeypatch, tmp_path):
    cfg = tmp_path / "queues.yaml"
    cfg.write_text(
        "default_queue: default\nqueues:\n  default:\n    workers: 1\n  slow:\n    workers: 2\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("QUEUES_CONFIG_PATH", str(cfg))
    get_settings.cache_clear()
    get_queue_config.cache_clear()
    yield
    get_settings.cache_clear()
    get_queue_config.cache_clear()


def test_create_task_default_flags():
    db = make_db()
    queue = TaskQueueService()
    task = queue.create_task(
        db,
        prompt="hello",
        model=None,
        queue_name=None,
        metadata=None,
        priority=1,
        agent_mode=True,
        unattended=True,
        max_attempts=None,
    )
    assert task.status == TaskStatus.QUEUED
    assert task.agent_mode is True
    assert task.unattended is True
    assert task.queue_expire_at > task.created_at


def test_create_task_saves_claude_agent_options_dict():
    db = make_db()
    queue = TaskQueueService()
    options = {"max_turns": 2, "permission_mode": "plan"}
    task = queue.create_task(
        db,
        prompt="hello",
        model=None,
        queue_name=None,
        metadata=None,
        priority=1,
        agent_mode=True,
        unattended=True,
        max_attempts=None,
        claude_agent_options=options,
    )
    assert task.payload.get("claude_agent_options") == options


def test_create_task_with_explicit_queue_name():
    db = make_db()
    queue = TaskQueueService()
    task = queue.create_task(
        db,
        prompt="hello",
        model=None,
        queue_name="slow",
        metadata=None,
        priority=1,
        agent_mode=True,
        unattended=True,
        max_attempts=None,
    )
    assert task.queue_name == "slow"


def test_retry_task_creates_new_task_with_same_configuration():
    db = make_db()
    queue = TaskQueueService()
    original = queue.create_task(
        db,
        prompt="try this again",
        model="test-model",
        queue_name="slow",
        metadata={"source": {"name": "test"}},
        priority=3,
        agent_mode=False,
        unattended=False,
        max_attempts=5,
        claude_agent_options={"max_turns": 2},
    )

    retried = queue.retry_task(db, original.id)

    assert retried is not None
    assert retried.id != original.id
    assert retried.status == TaskStatus.QUEUED
    assert retried.attempt == 0
    assert retried.payload == original.payload
    assert retried.queue_name == original.queue_name
    assert retried.metadata_json == original.metadata_json
    assert retried.priority == original.priority
    assert retried.agent_mode == original.agent_mode
    assert retried.unattended == original.unattended
    assert retried.max_attempts == original.max_attempts


def test_retry_task_returns_none_when_original_does_not_exist():
    db = make_db()
    queue = TaskQueueService()

    assert queue.retry_task(db, "not-exists") is None


def test_cancelled_running_task_is_not_overwritten_by_late_success():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    queue = TaskQueueService()
    with session_factory() as worker_db, session_factory() as webhook_db:
        task = queue.create_task(
            worker_db,
            prompt="cancel while running",
            model=None,
            queue_name=None,
            metadata=None,
            priority=0,
            agent_mode=True,
            unattended=True,
            max_attempts=1,
        )
        claimed = queue.claim_next_task(worker_db, "worker-1", "default")
        assert claimed is not None

        cancelled = queue.cancel_task(webhook_db, task.id, reason="superseded_by_workflow:test")
        assert cancelled is not None and cancelled.status == TaskStatus.CANCELLED

        queue.mark_success(worker_db, task.id, {"late": "result"})
        worker_db.refresh(task)
        assert task.status == TaskStatus.CANCELLED
        assert task.result is None


def test_concurrent_cancel_and_success_have_only_one_winning_transition(tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'task-transition-concurrency.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    with session_factory() as db:
        queue = TaskQueueService()
        task = queue.create_task(
            db,
            prompt="race terminal transitions",
            model=None,
            queue_name=None,
            metadata=None,
            priority=0,
            agent_mode=True,
            unattended=True,
            max_attempts=1,
        )
        task_id = task.id
        assert queue.claim_next_task(db, "worker-1", "default") is not None

    barrier = Barrier(2)

    def cancel_once() -> TaskStatus:
        with session_factory() as db:
            barrier.wait()
            task = TaskQueueService().cancel_task(db, task_id, reason="concurrent_test")
            assert task is not None
            return task.status

    def succeed_once() -> bool:
        with session_factory() as db:
            barrier.wait()
            return TaskQueueService().mark_success(db, task_id, {"ok": True})

    with ThreadPoolExecutor(max_workers=2) as pool:
        cancel_future = pool.submit(cancel_once)
        success_future = pool.submit(succeed_once)
        cancel_status = cancel_future.result()
        success_won = success_future.result()

    with session_factory() as db:
        task = db.get(AgentTask, task_id)
        assert task is not None
        if success_won:
            assert cancel_status == TaskStatus.SUCCEEDED
            assert task.status == TaskStatus.SUCCEEDED
            assert task.result == {"ok": True}
        else:
            assert cancel_status == TaskStatus.CANCELLED
            assert task.status == TaskStatus.CANCELLED
            assert task.result is None
        terminal_log_count = db.scalar(
            select(func.count())
            .select_from(AgentTaskLog)
            .where(
                AgentTaskLog.task_id == task_id,
                AgentTaskLog.event_type.in_(["cancelled", "succeeded"]),
            )
        )
        assert terminal_log_count == 1


def test_upsert_task_context_updates_latest():
    db = make_db()
    queue = TaskQueueService()
    task = queue.create_task(
        db,
        prompt="hello",
        model=None,
        queue_name=None,
        metadata=None,
        priority=1,
        agent_mode=True,
        unattended=True,
        max_attempts=None,
    )
    queue.upsert_task_context(db, task.id, ["first"])
    queue.upsert_task_context(db, task.id, ["first", "second"])
    context = db.get(AgentTaskContext, task.id)
    assert context is not None
    assert context.messages_json == ["first", "second"]


def test_create_task_unknown_queue_raises():
    db = make_db()
    queue = TaskQueueService()
    with pytest.raises(QueueNotFoundError):
        queue.create_task(
            db,
            prompt="hello",
            model=None,
            queue_name="missing-queue",
            metadata=None,
            priority=1,
            agent_mode=True,
            unattended=True,
            max_attempts=None,
        )


def test_abandon_expired_queued():
    db = make_db()
    queue = TaskQueueService()
    task = queue.create_task(
        db,
        prompt="to-expire",
        model=None,
        queue_name=None,
        metadata=None,
        priority=0,
        agent_mode=True,
        unattended=True,
        max_attempts=1,
    )
    task.queue_expire_at = utc_now() - timedelta(seconds=1)
    db.commit()

    changed = queue.abandon_expired_queued(db)
    assert changed == 1
    db.refresh(task)
    assert task.status == TaskStatus.ABANDONED
    assert task.abandoned_reason == "queue_timeout_24h"


def test_abandon_expired_running():
    db = make_db()
    queue = TaskQueueService()
    task = queue.create_task(
        db,
        prompt="run-expire",
        model=None,
        queue_name=None,
        metadata=None,
        priority=0,
        agent_mode=True,
        unattended=True,
        max_attempts=1,
    )
    claimed = queue.claim_next_task(db, "worker-1", "default")
    assert claimed is not None

    running = db.get(AgentTask, task.id)
    assert running is not None
    running.running_expire_at = utc_now() - timedelta(seconds=1)
    db.commit()

    changed = queue.abandon_expired_running(db)
    assert changed == 1
    db.refresh(running)
    assert running.status == TaskStatus.ABANDONED
    assert running.abandoned_reason == "running_timeout_4h"
    assert running.error_message == "task_execution_timeout"


def test_shutdown_abandons_running():
    db = make_db()
    queue = TaskQueueService()
    task = queue.create_task(
        db,
        prompt="running",
        model=None,
        queue_name=None,
        metadata=None,
        priority=0,
        agent_mode=True,
        unattended=True,
        max_attempts=1,
    )
    claimed = queue.claim_next_task(db, "worker-1", "default")
    assert claimed is not None

    changed = queue.abandon_running_on_shutdown(db)
    assert changed == 1
    db.refresh(task)
    assert task.status == TaskStatus.ABANDONED
    assert task.abandoned_reason == "service_shutdown"


def test_default_worker_concurrency_is_one(monkeypatch):
    monkeypatch.delenv("WORKER_CONCURRENCY", raising=False)
    get_settings.cache_clear()
    assert get_settings().worker_concurrency == 1
