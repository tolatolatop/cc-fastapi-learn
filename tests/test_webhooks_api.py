from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cc_fastapi.api.webhooks import router
from cc_fastapi.core.config import get_settings
from cc_fastapi.core.queue_config import get_queue_config
from cc_fastapi.db.models import AgentTask, Base, TaskStatus, WebhookTrigger
from cc_fastapi.db.session import get_db


@pytest.fixture(autouse=True)
def webhook_settings(monkeypatch, tmp_path):
    cfg = tmp_path / "queues.yaml"
    cfg.write_text(
        "default_queue: default\nqueues:\n  default:\n    workers: 1\n  hooks:\n    workers: 1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("QUEUES_CONFIG_PATH", str(cfg))
    monkeypatch.setenv("GITLAB_WEBHOOK_SECRET", "gitlab-secret")
    monkeypatch.setenv("GITLAB_WEBHOOK_QUEUE_NAME", "hooks")
    template = tmp_path / "gitlab_webhook_prompt.j2"
    template.write_text(
        "Review {{ event_type }} for {{ project.path_with_namespace }} on {{ ref }} ({{ commits | length }} commits)",
        encoding="utf-8",
    )
    monkeypatch.setenv("GITLAB_WEBHOOK_PROMPT_TEMPLATE_PATH", str(template))
    monkeypatch.setenv("API_TOKEN", "")
    get_settings.cache_clear()
    get_queue_config.cache_clear()
    yield template
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
    return TestClient(app), TestingSessionLocal


def gitlab_headers(**overrides):
    headers = {
        "X-Gitlab-Token": "gitlab-secret",
        "X-Gitlab-Event": "Push Hook",
        "X-Gitlab-Event-UUID": "event-uuid-1",
        "X-Gitlab-Webhook-UUID": "webhook-uuid-1",
        "X-Gitlab-Instance": "https://gitlab.example.com",
    }
    headers.update(overrides)
    return headers


def gitlab_payload(index: int = 1):
    return {
        "object_kind": "push",
        "ref": f"refs/heads/feature-{index}",
        "project": {"path_with_namespace": "group/project"},
        "commits": [{"id": f"commit-{index}"}],
    }


def test_gitlab_webhook_renders_prompt_creates_task_and_records_metadata():
    client, session_factory = build_client()
    payload = gitlab_payload()

    response = client.post("/v1/webhooks/gitlab", headers=gitlab_headers(), json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    assert body["queue_name"] == "hooks"

    with session_factory() as db:
        task = db.get(AgentTask, body["task_id"])
        trigger = db.get(WebhookTrigger, body["webhook_id"])
        assert task is not None
        assert trigger is not None
        assert task.status == TaskStatus.QUEUED
        assert task.payload["prompt"] == "Review Push Hook for group/project on refs/heads/feature-1 (1 commits)"
        assert task.metadata_json == {
            "trigger": "gitlab_webhook",
            "gitlab": {
                "event_type": "Push Hook",
                "event_uuid": "event-uuid-1",
                "webhook_uuid": "webhook-uuid-1",
                "instance_url": "https://gitlab.example.com",
            },
        }
        assert trigger.task_id == task.id
        assert trigger.payload_json == payload

    listed = client.get("/v1/webhooks")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["task_id"] == body["task_id"]
    assert listed.json()["items"][0]["payload"] == payload


def test_gitlab_webhook_rejects_invalid_token_without_creating_records():
    client, session_factory = build_client()

    response = client.post(
        "/v1/webhooks/gitlab",
        headers=gitlab_headers(**{"X-Gitlab-Token": "wrong"}),
        json=gitlab_payload(),
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid gitlab webhook token"
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AgentTask)) == 0
        assert db.scalar(select(func.count()).select_from(WebhookTrigger)) == 0


def test_gitlab_webhook_template_error_does_not_create_task(webhook_settings):
    webhook_settings.write_text("{{ missing.value }}", encoding="utf-8")
    client, session_factory = build_client()

    response = client.post("/v1/webhooks/gitlab", headers=gitlab_headers(), json=gitlab_payload())

    assert response.status_code == 400
    assert "failed to render webhook prompt" in response.json()["detail"]
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AgentTask)) == 0
        assert db.scalar(select(func.count()).select_from(WebhookTrigger)) == 0


def test_gitlab_webhook_missing_template_file_does_not_create_task(monkeypatch, tmp_path):
    missing_template = tmp_path / "missing.j2"
    monkeypatch.setenv("GITLAB_WEBHOOK_PROMPT_TEMPLATE_PATH", str(missing_template))
    get_settings.cache_clear()
    client, session_factory = build_client()

    response = client.post("/v1/webhooks/gitlab", headers=gitlab_headers(), json=gitlab_payload())

    assert response.status_code == 400
    assert "failed to load webhook prompt template" in response.json()["detail"]
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AgentTask)) == 0
        assert db.scalar(select(func.count()).select_from(WebhookTrigger)) == 0


def test_list_webhook_triggers_supports_pagination():
    client, _ = build_client()
    for index in range(3):
        response = client.post(
            "/v1/webhooks/gitlab",
            headers=gitlab_headers(
                **{
                    "X-Gitlab-Event-UUID": f"event-uuid-{index}",
                    "X-Gitlab-Webhook-UUID": f"webhook-uuid-{index}",
                }
            ),
            json=gitlab_payload(index),
        )
        assert response.status_code == 200

    response = client.get("/v1/webhooks", params={"offset": 1, "limit": 2})

    assert response.status_code == 200
    assert response.json()["total"] == 3
    assert len(response.json()["items"]) == 2


def test_list_webhook_triggers_uses_api_token(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "list-secret")
    get_settings.cache_clear()
    client, _ = build_client()

    unauthorized = client.get("/v1/webhooks")
    authorized = client.get("/v1/webhooks", headers={"X-Api-Token": "list-secret"})

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
