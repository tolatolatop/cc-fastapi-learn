from collections.abc import Mapping

from fastapi import FastAPI
from fastapi.testclient import TestClient

from cc_fastapi.api.providers import router
from cc_fastapi.core.config import Settings, get_settings
from cc_fastapi.core.webhook_payloads import WebhookPayload
from cc_fastapi.core.webhook_providers import (
    ReceivedWebhook,
    WebhookProviderDefinition,
    WebhookProviderRegistry,
)
from cc_fastapi.workflows.base import WorkflowEvent
from cc_fastapi.workflows.provider_prompt import ProviderPromptTaskWorkflow


class ExamplePayloadAdapter:
    def parse(self, event_type: str, payload: Mapping[str, object]) -> WebhookPayload:
        return WebhookPayload(
            provider="gitea",
            event_type=event_type,
            event_kind="pull_request",
        )


class ExampleRequestAdapter:
    def parse(
        self,
        headers: Mapping[str, str],
        body: bytes,
        settings: Settings,
    ) -> ReceivedWebhook:
        return ReceivedWebhook(
            payload={},
            event_type="pull_request",
            event_uuid=None,
            webhook_uuid=None,
            instance_url=None,
            provider_metadata={},
        )


def test_provider_capabilities_are_served_from_the_adapter_registry(monkeypatch):
    monkeypatch.setenv("API_TOKEN", "")
    get_settings.cache_clear()
    app = FastAPI()
    app.include_router(router)

    response = TestClient(app).get("/v1/providers")

    assert response.status_code == 200
    assert response.json() == {
        "custom_provider_allowed": True,
        "items": [
            {
                "id": "github",
                "display_name": "GitHub",
                "capabilities": [
                    "review_recording",
                    "webhook",
                    "payload_projection",
                    "repository_sync",
                    "change_request",
                ],
            },
            {
                "id": "gitlab",
                "display_name": "GitLab",
                "capabilities": [
                    "review_recording",
                    "webhook",
                    "payload_projection",
                    "repository_sync",
                    "change_request",
                ],
            },
        ]
    }
    get_settings.cache_clear()


def test_new_provider_can_register_without_a_provider_specific_workflow_class():
    definition = WebhookProviderDefinition(
        id="Gitea",
        display_name="Gitea",
        payload_adapter=ExamplePayloadAdapter(),
        request_adapter=ExampleRequestAdapter(),
        prompt_template_setting="resolved_github_webhook_prompt_template_path",
        queue_setting="github_webhook_queue_name",
        supersede_actions=frozenset({"synchronize"}),
    )
    registry = WebhookProviderRegistry((definition,))
    workflow = ProviderPromptTaskWorkflow(
        definition.id,
        supersede_actions=definition.supersede_actions,
    )

    assert registry.require("gitea") is definition
    assert definition.payload_adapter.parse("pull_request", {}).provider == "gitea"
    assert workflow.name == "gitea_prompt_task"
    assert workflow.matches(
        WorkflowEvent(provider="gitea", event_type="pull_request", payload={})
    )
