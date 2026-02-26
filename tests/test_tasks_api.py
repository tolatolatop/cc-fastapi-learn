from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cc_fastapi.api.tasks import router
from cc_fastapi.db.models import Base
from cc_fastapi.db.session import get_db


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

    detail = client.get(f"/v1/agent-tasks/{task_id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "queued"
    assert detail.json()["agent_mode"] is True
    assert detail.json()["unattended"] is True

    logs = client.get(f"/v1/agent-tasks/{task_id}/logs")
    assert logs.status_code == 200
    assert logs.json()["total"] >= 1


def test_cancel_task():
    client = build_client()
    create = client.post("/v1/agent-tasks", json={"prompt": "cancel-me"})
    task_id = create.json()["task_id"]

    cancelled = client.post(f"/v1/agent-tasks/{task_id}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

