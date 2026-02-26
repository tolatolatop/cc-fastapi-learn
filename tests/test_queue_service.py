from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cc_fastapi.core.config import get_settings
from cc_fastapi.core.queue_config import get_queue_config
from cc_fastapi.db.models import AgentTask, AgentTaskContext, Base, TaskStatus, utc_now
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

