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
    thread_args = [thread.args for thread in manager.threads]
    assert ("default-worker-1", "default") in thread_args
    assert ("default-worker-2", "default") in thread_args
    assert ("slow-worker-1", "slow") in thread_args

    manager.stop()
