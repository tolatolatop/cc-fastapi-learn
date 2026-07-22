import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from secrets import compare_digest
from types import MappingProxyType
from typing import Protocol

from cc_fastapi.core.config import Settings
from cc_fastapi.core.repository_values import normalize_repository_provider
from cc_fastapi.core.webhook_payloads import (
    GitHubWebhookPayloadAdapter,
    GitLabWebhookPayloadAdapter,
    WebhookPayloadAdapter,
)


class WebhookRequestError(ValueError):
    def __init__(self, detail: str, *, status_code: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class ReceivedWebhook:
    payload: dict
    event_type: str
    event_uuid: str | None
    webhook_uuid: str | None
    instance_url: str | None
    provider_metadata: dict


class WebhookRequestAdapter(Protocol):
    def parse(
        self,
        headers: Mapping[str, str],
        body: bytes,
        settings: Settings,
    ) -> ReceivedWebhook: ...


def _header(headers: Mapping[str, str], name: str) -> str | None:
    direct = headers.get(name)
    if direct is not None:
        return direct
    expected = name.casefold()
    return next(
        (value for key, value in headers.items() if key.casefold() == expected),
        None,
    )


def _optional_header(headers: Mapping[str, str], name: str) -> str | None:
    value = _header(headers, name)
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _required_header(headers: Mapping[str, str], name: str) -> str:
    value = _optional_header(headers, name)
    if value is None:
        raise WebhookRequestError(f"missing {name} header", status_code=422)
    return value


def _json_object(body: bytes, provider: str) -> dict:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebhookRequestError(f"invalid {provider} webhook payload") from exc
    if not isinstance(payload, dict):
        raise WebhookRequestError(f"invalid {provider} webhook payload")
    return payload


def _github_instance_url(enterprise_host: str | None) -> str:
    if enterprise_host is None:
        return "https://github.com"
    normalized_host = enterprise_host.strip().rstrip("/")
    if not normalized_host:
        return "https://github.com"
    if normalized_host.startswith(("http://", "https://")):
        return normalized_host
    return f"https://{normalized_host}"


class GitLabWebhookRequestAdapter:
    def parse(
        self,
        headers: Mapping[str, str],
        body: bytes,
        settings: Settings,
    ) -> ReceivedWebhook:
        expected_token = settings.gitlab_webhook_secret
        received_token = _optional_header(headers, "X-Gitlab-Token")
        if expected_token and (
            received_token is None
            or not compare_digest(received_token, expected_token)
        ):
            raise WebhookRequestError(
                "invalid gitlab webhook token",
                status_code=401,
            )
        return ReceivedWebhook(
            payload=_json_object(body, "gitlab"),
            event_type=_required_header(headers, "X-Gitlab-Event"),
            event_uuid=_optional_header(headers, "X-Gitlab-Event-UUID"),
            webhook_uuid=_optional_header(headers, "X-Gitlab-Webhook-UUID"),
            instance_url=_optional_header(headers, "X-Gitlab-Instance"),
            provider_metadata={},
        )


class GitHubWebhookRequestAdapter:
    def parse(
        self,
        headers: Mapping[str, str],
        body: bytes,
        settings: Settings,
    ) -> ReceivedWebhook:
        expected_secret = settings.github_webhook_secret
        signature = _optional_header(headers, "X-Hub-Signature-256")
        if expected_secret:
            expected_signature = "sha256=" + hmac.new(
                expected_secret.encode("utf-8"),
                body,
                hashlib.sha256,
            ).hexdigest()
            if signature is None or not compare_digest(signature, expected_signature):
                raise WebhookRequestError(
                    "invalid github webhook signature",
                    status_code=401,
                )
        delivery_id = _optional_header(headers, "X-GitHub-Delivery")
        hook_id = _optional_header(headers, "X-GitHub-Hook-ID")
        return ReceivedWebhook(
            payload=_json_object(body, "github"),
            event_type=_required_header(headers, "X-GitHub-Event"),
            event_uuid=delivery_id,
            webhook_uuid=delivery_id,
            instance_url=_github_instance_url(
                _optional_header(headers, "X-GitHub-Enterprise-Host")
            ),
            provider_metadata={
                "delivery_id": delivery_id,
                "hook_id": hook_id,
            },
        )


@dataclass(frozen=True, slots=True)
class WebhookProviderDefinition:
    id: str
    display_name: str
    payload_adapter: WebhookPayloadAdapter
    request_adapter: WebhookRequestAdapter
    prompt_template_setting: str
    queue_setting: str
    supersede_actions: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", normalize_repository_provider(self.id))
        display_name = self.display_name.strip()
        if not display_name:
            raise ValueError("provider display_name must not be blank")
        object.__setattr__(self, "display_name", display_name)
        object.__setattr__(
            self,
            "supersede_actions",
            frozenset(action.strip().casefold() for action in self.supersede_actions),
        )

    @property
    def capabilities(self) -> tuple[str, ...]:
        return (
            "review_recording",
            "webhook",
            "payload_projection",
            "repository_sync",
            "change_request",
        )

    def prompt_template_path(self, settings: Settings) -> str:
        return str(getattr(settings, self.prompt_template_setting)).strip()

    def queue_name(self, settings: Settings) -> str | None:
        value = str(getattr(settings, self.queue_setting)).strip()
        return value or None


class WebhookProviderRegistry:
    def __init__(
        self,
        definitions: tuple[WebhookProviderDefinition, ...],
    ) -> None:
        by_id: dict[str, WebhookProviderDefinition] = {}
        for definition in definitions:
            if definition.id in by_id:
                raise ValueError(f"provider already registered: {definition.id}")
            by_id[definition.id] = definition
        self._by_id = MappingProxyType(by_id)

    def get(self, provider: str) -> WebhookProviderDefinition | None:
        try:
            normalized = normalize_repository_provider(provider)
        except ValueError:
            return None
        return self._by_id.get(normalized)

    def require(self, provider: str) -> WebhookProviderDefinition:
        definition = self.get(provider)
        if definition is None:
            raise KeyError(provider)
        return definition

    def list(self) -> tuple[WebhookProviderDefinition, ...]:
        return tuple(sorted(self._by_id.values(), key=lambda item: item.id))


webhook_provider_registry = WebhookProviderRegistry(
    (
        WebhookProviderDefinition(
            id="github",
            display_name="GitHub",
            payload_adapter=GitHubWebhookPayloadAdapter(),
            request_adapter=GitHubWebhookRequestAdapter(),
            prompt_template_setting="resolved_github_webhook_prompt_template_path",
            queue_setting="github_webhook_queue_name",
            supersede_actions=frozenset({"synchronize"}),
        ),
        WebhookProviderDefinition(
            id="gitlab",
            display_name="GitLab",
            payload_adapter=GitLabWebhookPayloadAdapter(),
            request_adapter=GitLabWebhookRequestAdapter(),
            prompt_template_setting="resolved_gitlab_webhook_prompt_template_path",
            queue_setting="gitlab_webhook_queue_name",
            supersede_actions=frozenset({"update"}),
        ),
    )
)
