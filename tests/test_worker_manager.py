import pytest

import cc_fastapi.services.worker as worker_module
from cc_fastapi.core.queue_config import QueueConfig, QueueDefinition


class FakeThread:
    def __init__(self, target, args, daemon):
        self.target = target
        self.args = args
        self.daemon = daemon
        self.started = False

    def start(self):
        self.started = True

    def join(self, timeout=None):
        return None


def test_worker_manager_starts_workers_per_queue(monkeypatch):
    monkeypatch.setattr(
        worker_module,
        "get_queue_config",
        lambda: QueueConfig(
            default_queue="default",
            queues={
                "default": QueueDefinition(workers=2),
                "slow": QueueDefinition(workers=1),
            },
        ),
    )
    monkeypatch.setattr(worker_module.threading, "Thread", FakeThread)

    manager = worker_module.WorkerManager()
    manager.start()

    assert len(manager.threads) == 3
    thread_args = [(thread.args[0], thread.args[1].name) for thread in manager.threads]
    assert ("default-worker-1", "default") in thread_args
    assert ("default-worker-2", "default") in thread_args
    assert ("slow-worker-1", "slow") in thread_args

    manager.stop()


def test_worker_manager_resolves_per_queue_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "global-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY_SLOW", "slow-key")
    monkeypatch.setattr(
        worker_module,
        "get_queue_config",
        lambda: QueueConfig(
            default_queue="default",
            queues={
                "default": QueueDefinition(workers=1),
                "slow": QueueDefinition(workers=1, api_key_env="ANTHROPIC_API_KEY_SLOW"),
                "high_priority": QueueDefinition(
                    workers=1,
                    api_key_env="ANTHROPIC_API_KEY",
                    base_url="https://gateway.example.com/",
                ),
            },
        ),
    )
    monkeypatch.setattr(worker_module.threading, "Thread", FakeThread)

    manager = worker_module.WorkerManager()
    manager.start()

    runtimes = {thread.args[1].name: thread.args[1] for thread in manager.threads}
    assert runtimes["default"].api_key is None
    assert runtimes["slow"].api_key == "slow-key"
    assert runtimes["high_priority"].api_key == "global-key"
    assert runtimes["high_priority"].base_url == "https://gateway.example.com/"


def test_worker_manager_fails_fast_when_queue_key_env_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY_MISSING", raising=False)
    monkeypatch.setattr(
        worker_module,
        "get_queue_config",
        lambda: QueueConfig(
            default_queue="default",
            queues={
                "slow": QueueDefinition(workers=1, api_key_env="ANTHROPIC_API_KEY_MISSING"),
            },
        ),
    )
    monkeypatch.setattr(worker_module.threading, "Thread", FakeThread)

    manager = worker_module.WorkerManager()
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY_MISSING"):
        manager.start()
    assert manager.threads == []


def test_build_queue_runtime_keeps_key_out_of_queue_definition(monkeypatch):
    monkeypatch.setenv("QUEUE_KEY_UNDER_TEST", "abc123")
    definition = QueueDefinition(workers=1, api_key_env="QUEUE_KEY_UNDER_TEST")

    runtime = worker_module.build_queue_runtime("q", definition)

    assert runtime.api_key == "abc123"
    assert definition.api_key_env == "QUEUE_KEY_UNDER_TEST"
    assert "abc123" not in definition.model_dump_json()


def test_build_queue_runtime_reads_dotenv_when_process_env_is_empty(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY_DOTENV", raising=False)
    (tmp_path / ".env").write_text('ANTHROPIC_API_KEY_DOTENV="dotenv-key"\n', encoding="utf-8")

    runtime = worker_module.build_queue_runtime(
        "slow",
        QueueDefinition(workers=1, api_key_env="ANTHROPIC_API_KEY_DOTENV"),
    )

    assert runtime.api_key == "dotenv-key"


def test_run_task_forwards_queue_credentials_to_client(monkeypatch, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from cc_fastapi.core.config import get_settings
    from cc_fastapi.core.queue_config import get_queue_config
    from cc_fastapi.db.models import Base
    from cc_fastapi.services.queue import TaskQueueService

    config = tmp_path / "queues.yaml"
    config.write_text(
        "default_queue: default\n"
        "queues:\n"
        "  default:\n"
        "    workers: 1\n"
        "  slow:\n"
        "    workers: 1\n"
        "    api_key_env: ANTHROPIC_API_KEY_SLOW\n"
        "    base_url: https://slow.example.com\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("QUEUES_CONFIG_PATH", str(config))
    monkeypatch.setenv("ANTHROPIC_API_KEY_SLOW", "slow-key")
    get_settings.cache_clear()
    get_queue_config.cache_clear()

    engine = create_engine(f"sqlite:///{tmp_path / 'queue.db'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    try:
        with sessions() as db:
            task = TaskQueueService().create_task(
                db,
                prompt="queue work",
                model=None,
                queue_name="slow",
                metadata=None,
                priority=0,
                agent_mode=True,
                unattended=True,
                max_attempts=1,
            )
            assert TaskQueueService().claim_next_task(db, "worker", "slow").id == task.id

            captured: dict[str, object] = {}

            def fake_run_agent_task(**kwargs):
                captured.update(kwargs)
                return {"output_text": "ok", "session_id": "s-1"}

            manager = worker_module.WorkerManager()
            monkeypatch.setattr(manager.client, "run_agent_task", fake_run_agent_task)
            runtime = worker_module.build_queue_runtime(
                "slow", get_queue_config().queues["slow"]
            )
            manager._run_task(db, task.id, runtime)

        assert captured["api_key"] == "slow-key"
        assert captured["base_url"] == "https://slow.example.com"
    finally:
        engine.dispose()
        get_settings.cache_clear()
        get_queue_config.cache_clear()

