import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cc_fastapi.api.tasks import router
from cc_fastapi.core.config import get_settings
from cc_fastapi.core.queue_config import get_queue_config
from cc_fastapi.db.models import AgentTask, Base
from cc_fastapi.db.session import get_db
from cc_fastapi.services.queue import TaskQueueService


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
    client = TestClient(app)
    client.session_factory = TestingSessionLocal
    return client


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
    assert detail.json()["prompt"] == "hello"
    assert detail.json()["model"]
    assert detail.json()["metadata"] == {"source": "test"}
    assert detail.json()["session_id"] is None
    assert detail.json()["agent_mode"] is True
    assert detail.json()["unattended"] is True

    logs = client.get(f"/v1/agent-tasks/{task_id}/logs")
    assert logs.status_code == 200
    assert logs.json()["total"] >= 1


def test_list_tasks_supports_pagination_filters_and_global_summary():
    client = build_client()
    task_ids = []
    for prompt, queue_name in [("alpha task", "default"), ("beta task", "slow"), ("gamma task", "default")]:
        response = client.post("/v1/agent-tasks", json={"prompt": prompt, "queue_name": queue_name})
        assert response.status_code == 200
        task_ids.append(response.json()["task_id"])
    assert client.post(f"/v1/agent-tasks/{task_ids[-1]}/cancel").status_code == 200

    page = client.get("/v1/agent-tasks", params={"offset": 1, "limit": 1})

    assert page.status_code == 200
    assert page.json()["total"] == 3
    assert len(page.json()["items"]) == 1
    assert page.json()["summary"] == {
        "total": 3,
        "status_counts": {
            "queued": 2,
            "running": 0,
            "pausing": 0,
            "paused": 0,
            "succeeded": 0,
            "failed": 0,
            "cancelled": 1,
            "abandoned": 0,
        },
        "queues": [
            {"name": "default", "total": 2, "queued": 1, "running": 0},
            {"name": "slow", "total": 1, "queued": 1, "running": 0},
        ],
    }

    searched = client.get("/v1/agent-tasks", params={"q": "beta", "limit": 20})
    assert searched.status_code == 200
    assert searched.json()["total"] == 1
    assert searched.json()["items"][0]["prompt"] == "beta task"

    filtered = client.get(
        "/v1/agent-tasks",
        params=[("status", "queued"), ("status", "cancelled"), ("queue", "default")],
    )
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 2


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


def test_retry_task_creates_new_queued_task():
    client = build_client()
    create = client.post(
        "/v1/agent-tasks",
        json={"prompt": "retry-me", "queue_name": "slow", "priority": 3},
    )
    original_task_id = create.json()["task_id"]
    cancelled = client.post(f"/v1/agent-tasks/{original_task_id}/cancel")
    assert cancelled.status_code == 200

    response = client.post(f"/v1/agent-tasks/{original_task_id}/retry")

    assert response.status_code == 200
    retried_task_id = response.json()["task_id"]
    assert retried_task_id != original_task_id
    assert response.json()["status"] == "queued"
    assert response.json()["queue_name"] == "slow"
    assert client.get(f"/v1/agent-tasks/{original_task_id}").json()["status"] == "cancelled"

    duplicate = client.post(f"/v1/agent-tasks/{original_task_id}/retry")
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "task retry was already created"


def test_retry_task_not_found_returns_404():
    client = build_client()

    response = client.post("/v1/agent-tasks/not-exists/retry")

    assert response.status_code == 404
    assert response.json()["detail"] == "task not found"


def finish_succeeded_task(client, session_id="saved-session"):
    task_id = client.post("/v1/agent-tasks", json={"prompt": "continue-me"}).json()["task_id"]
    with client.session_factory() as db:
        queue = TaskQueueService()
        queue.claim_next_task(db, "worker", "default")
        queue.set_task_session_id(db, task_id, session_id)
        task = db.get(AgentTask, task_id)
        assert queue.mark_success(
            db, task_id, {"session_id": session_id}, execution_token=task.execution_token,
        )
    return task_id


def test_continue_task_requeues_same_session_and_task_id():
    client = build_client()
    task_id = finish_succeeded_task(client)

    response = client.post(f"/v1/agent-tasks/{task_id}/continue")

    assert response.status_code == 200
    payload = response.json()
    assert payload["task_id"] == task_id
    assert payload["status"] == "queued"
    assert payload["session_id"] == "saved-session"
    assert payload["continuation_count"] == 1

    task = client.get(f"/v1/agent-tasks/{task_id}").json()
    assert task["status"] == "queued"
    assert task["session_id"] == "saved-session"
    assert task["continuation_count"] == 1

    duplicate = client.post(f"/v1/agent-tasks/{task_id}/continue")
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "只有已完成的任务可以继续执行"


def test_continue_task_without_session_returns_conflict():
    client = build_client()
    task_id = client.post("/v1/agent-tasks", json={"prompt": "no-session"}).json()["task_id"]
    with client.session_factory() as db:
        queue = TaskQueueService()
        queue.claim_next_task(db, "worker", "default")
        task = db.get(AgentTask, task_id)
        assert queue.mark_success(
            db, task_id, {"output_text": "partial"}, execution_token=task.execution_token,
        )

    response = client.post(f"/v1/agent-tasks/{task_id}/continue")

    assert response.status_code == 409
    assert "会话" in response.json()["detail"]


def test_continue_task_not_found_returns_404():
    client = build_client()

    response = client.post("/v1/agent-tasks/not-exists/continue")

    assert response.status_code == 404
    assert response.json()["detail"] == "task not found"


def test_create_task_with_explicit_queue():
    client = build_client()
    response = client.post("/v1/agent-tasks", json={"prompt": "queue", "queue_name": "slow"})
    assert response.status_code == 200
    assert response.json()["queue_name"] == "slow"


def test_list_available_queues():
    client = build_client()
    response = client.get("/v1/agent-tasks/queues/available")
    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {"name": "default", "workers": 1, "is_default": True},
            {"name": "slow", "workers": 1, "is_default": False},
        ]
    }


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


def test_pause_resume_endpoints_keep_task_ids_and_new_tasks_paused():
    client = build_client()
    task_id = client.post('/v1/agent-tasks', json={'prompt': 'first'}).json()['task_id']
    assert client.get('/v1/agent-tasks/control/status').json()['state'] == 'active'
    paused = client.post('/v1/agent-tasks/control/pause')
    assert paused.status_code == 200
    assert paused.json()['state'] == 'paused'
    assert paused.json()['paused_count'] == 1
    second = client.post('/v1/agent-tasks', json={'prompt': 'second'}).json()
    assert second['status'] == 'paused'
    filtered = client.get('/v1/agent-tasks', params={'status': 'paused'}).json()
    assert filtered['total'] == 2
    assert filtered['summary']['status_counts']['paused'] == 2
    assert client.get(f'/v1/agent-tasks/{task_id}').json()['paused_at']
    assert client.post('/v1/agent-tasks/control/resume').json()['state'] == 'active'
    assert client.get(f'/v1/agent-tasks/{task_id}').json()['status'] == 'queued'
    assert client.post('/v1/agent-tasks/control/resume').status_code == 200


def test_pause_controls_require_api_token(monkeypatch):
    monkeypatch.setenv('API_TOKEN', 'synthetic-test-token')
    get_settings.cache_clear()
    client = build_client()
    for method, path in [('get', 'status'), ('post', 'pause'), ('post', 'resume')]:
        assert getattr(client, method)(f'/v1/agent-tasks/control/{path}').status_code == 401
    assert client.post('/v1/agent-tasks/not-exists/continue').status_code == 401
