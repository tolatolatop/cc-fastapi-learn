from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cc_fastapi.api.tasks import router
from cc_fastapi.core.config import get_settings
from cc_fastapi.core.queue_config import get_queue_config
from cc_fastapi.db.models import Base
from cc_fastapi.db.session import get_db


@pytest.fixture(autouse=True)
def queue_config_file(monkeypatch, tmp_path):
    cfg = tmp_path / "queues.yaml"
    cfg.write_text(
        "default_queue: default\nqueues:\n  default:\n    workers: 1\n  slow:\n    workers: 1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("QUEUES_CONFIG_PATH", str(cfg))
    get_settings.cache_clear()
    get_queue_config.cache_clear()
    yield
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

    def override_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


def test_create_get_and_logs():
    client = build_client()

    response = client.post(
        "/v1/agent-tasks",
        json={"prompt": "hello", "metadata": {"source": "test"}, "agent_mode": True, "unattended": True},
    )
    assert response.status_code == 200
    task_id = response.json()["task_id"]
    assert response.json()["queue_name"] == "default"

    detail = client.get(f"/v1/agent-tasks/{task_id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "queued"
    assert detail.json()["queue_name"] == "default"
    assert detail.json()["agent_mode"] is True
    assert detail.json()["unattended"] is True

    logs = client.get(f"/v1/agent-tasks/{task_id}/logs")
    assert logs.status_code == 200
    assert logs.json()["total"] >= 1


def test_get_task_context_returns_empty_when_not_written():
    client = build_client()
    create = client.post("/v1/agent-tasks", json={"prompt": "context-empty"})
    task_id = create.json()["task_id"]

    response = client.get(f"/v1/agent-tasks/{task_id}/context")
    assert response.status_code == 200
    assert response.json()["task_id"] == task_id
    assert response.json()["messages"] == []
    assert response.json()["updated_at"] is None


def test_get_task_context_not_found_returns_404():
    client = build_client()
    response = client.get("/v1/agent-tasks/not-exists/context")
    assert response.status_code == 404
    assert response.json()["detail"] == "task not found"


def test_cancel_task():
    client = build_client()
    create = client.post("/v1/agent-tasks", json={"prompt": "cancel-me"})
    task_id = create.json()["task_id"]

    cancelled = client.post(f"/v1/agent-tasks/{task_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


def test_create_task_with_explicit_queue():
    client = build_client()
    response = client.post("/v1/agent-tasks", json={"prompt": "queue", "queue_name": "slow"})
    assert response.status_code == 200
    assert response.json()["queue_name"] == "slow"


def test_create_task_with_missing_queue_returns_400():
    client = build_client()
    response = client.post("/v1/agent-tasks", json={"prompt": "queue", "queue_name": "missing"})
    assert response.status_code == 400
    assert "queue not found" in response.json()["detail"]


def test_create_task_with_absolute_cwd_returns_400():
    client = build_client()
    response = client.post(
        "/v1/agent-tasks",
        json={
            "prompt": "queue",
            "claude_agent_options": {"cwd": "/tmp/absolute-path-not-allowed"},
        },
    )
    assert response.status_code == 400
    assert "cwd must be a relative path" in response.json()["detail"]

