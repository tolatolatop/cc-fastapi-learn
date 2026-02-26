from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cc_fastapi.core.config import get_settings
from cc_fastapi.db.models import AgentTask, Base, TaskStatus, utc_now
from cc_fastapi.services.queue import TaskQueueService


def make_db():
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, future=True)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)()


def test_create_task_default_flags():
    db = make_db()
    queue = TaskQueueService()
    task = queue.create_task(
        db,
        prompt="hello",
        model=None,
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
        metadata=None,
        priority=1,
        agent_mode=True,
        unattended=True,
        max_attempts=None,
        claude_agent_options=options,
    )
    assert task.payload.get("claude_agent_options") == options


def test_abandon_expired_queued():
    db = make_db()
    queue = TaskQueueService()
    task = queue.create_task(
        db,
        prompt="to-expire",
        model=None,
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
        metadata=None,
        priority=0,
        agent_mode=True,
        unattended=True,
        max_attempts=1,
    )
    claimed = queue.claim_next_task(db, "worker-1")
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
        metadata=None,
        priority=0,
        agent_mode=True,
        unattended=True,
        max_attempts=1,
    )
    claimed = queue.claim_next_task(db, "worker-1")
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

