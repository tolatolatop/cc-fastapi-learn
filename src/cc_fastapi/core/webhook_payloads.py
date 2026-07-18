from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

from cc_fastapi.core.repository_values import (
    normalize_repository_project_path,
    normalize_repository_provider,
    normalize_repository_web_url,
)


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _identifier(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, str | int):
        return None
    normalized = str(value).strip()
    return normalized or None


def _first_text(*values: Any) -> str | None:
    for value in values:
        if normalized := _text(value):
            return normalized
    return None


def _ref_name(value: Any) -> str | None:
    ref = _text(value)
    if ref is None:
        return None
    for prefix in ("refs/heads/", "refs/tags/"):
        if ref.startswith(prefix):
            return ref.removeprefix(prefix)
    return ref


def _web_url(value: Any) -> str | None:
    try:
        return normalize_repository_web_url(value if isinstance(value, str) else None)
    except ValueError:
        return None


def _repository(project_path_value: Any, web_url_value: Any) -> "WebhookRepository | None":
    if not isinstance(project_path_value, str):
        return None
    try:
        project_path = normalize_repository_project_path(project_path_value)
    except ValueError:
        return None
    return WebhookRepository(
        project_path=project_path,
        web_url=_web_url(web_url_value),
    )


@dataclass(frozen=True, slots=True)
class WebhookRepository:
    project_path: str
    web_url: str | None = None


@dataclass(frozen=True, slots=True)
class WebhookActor:
    display_name: str
    username: str | None = None


@dataclass(frozen=True, slots=True)
class WebhookChangeRequest:
    resource_type: str
    number: str
    action: str | None = None
    source_branch: str | None = None
    target_branch: str | None = None
    head_sha: str | None = None


@dataclass(frozen=True, slots=True)
class WebhookPayload:
    """Immutable, normalized read projection of a provider webhook payload."""

    provider: str
    event_type: str
    event_kind: str
    repository: WebhookRepository | None = None
    actor: WebhookActor | None = None
    ref: str | None = None
    change_request: WebhookChangeRequest | None = None

    @classmethod
    def from_payload(
        cls,
        provider_value: str,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> "WebhookPayload | None":
        try:
            provider = normalize_repository_provider(provider_value)
        except ValueError:
            return None
        normalized_event_type = _text(event_type) or "event"
        adapter = WEBHOOK_PAYLOAD_ADAPTERS.get(provider)
        if adapter is None:
            return cls(
                provider=provider,
                event_type=normalized_event_type,
                event_kind=normalized_event_type,
            )
        return adapter.parse(normalized_event_type, payload)


class WebhookPayloadAdapter(Protocol):
    def parse(
        self,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> WebhookPayload: ...


class GitHubWebhookPayloadAdapter:
    def parse(
        self,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> WebhookPayload:
        repository_payload = _mapping(payload.get("repository"))
        repository = (
            _repository(
                repository_payload.get("full_name"),
                repository_payload.get("html_url"),
            )
            if repository_payload is not None
            else None
        )

        sender = _mapping(payload.get("sender"))
        username = _text(sender.get("login")) if sender is not None else None
        actor = WebhookActor(display_name=username, username=username) if username else None

        pull_request = _mapping(payload.get("pull_request"))
        head = _mapping(pull_request.get("head")) if pull_request is not None else None
        base = _mapping(pull_request.get("base")) if pull_request is not None else None
        number = (
            _identifier(payload.get("number"))
            or _identifier(pull_request.get("number"))
            if pull_request is not None
            else None
        )
        change_request = None
        if pull_request is not None and number is not None:
            change_request = WebhookChangeRequest(
                resource_type="pull_request",
                number=number,
                action=_text(payload.get("action")),
                source_branch=_ref_name(head.get("ref")) if head is not None else None,
                target_branch=_ref_name(base.get("ref")) if base is not None else None,
                head_sha=_text(head.get("sha")) if head is not None else None,
            )

        ref = _ref_name(payload.get("ref"))
        if ref is None and change_request is not None:
            ref = change_request.source_branch
        return WebhookPayload(
            provider="github",
            event_type=event_type,
            event_kind=_first_text(payload.get("event_name"), event_type) or "event",
            repository=repository,
            actor=actor,
            ref=ref,
            change_request=change_request,
        )


class GitLabWebhookPayloadAdapter:
    def parse(
        self,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> WebhookPayload:
        project = _mapping(payload.get("project"))
        repository = (
            _repository(
                project.get("path_with_namespace"),
                project.get("web_url"),
            )
            if project is not None
            else None
        )

        user = _mapping(payload.get("user"))
        display_name = _first_text(
            payload.get("user_name"),
            payload.get("user_username"),
            user.get("name") if user is not None else None,
            user.get("username") if user is not None else None,
        )
        username = _first_text(
            payload.get("user_username"),
            user.get("username") if user is not None else None,
        )
        actor = (
            WebhookActor(display_name=display_name, username=username)
            if display_name is not None
            else None
        )

        attributes = _mapping(payload.get("object_attributes"))
        object_kind = _text(payload.get("object_kind"))
        number = _identifier(attributes.get("iid")) if attributes is not None else None
        change_request = None
        if object_kind and object_kind.casefold() == "merge_request" and number is not None:
            last_commit = _mapping(attributes.get("last_commit"))
            change_request = WebhookChangeRequest(
                resource_type="merge_request",
                number=number,
                action=_text(attributes.get("action")),
                source_branch=_ref_name(attributes.get("source_branch")),
                target_branch=_ref_name(attributes.get("target_branch")),
                head_sha=_text(last_commit.get("id")) if last_commit is not None else None,
            )

        ref = _ref_name(payload.get("ref"))
        if ref is None and attributes is not None:
            ref = _ref_name(attributes.get("source_branch")) or _ref_name(
                attributes.get("ref")
            )
        return WebhookPayload(
            provider="gitlab",
            event_type=event_type,
            event_kind=_first_text(object_kind, payload.get("event_name"), event_type)
            or "event",
            repository=repository,
            actor=actor,
            ref=ref,
            change_request=change_request,
        )


WEBHOOK_PAYLOAD_ADAPTERS: Mapping[str, WebhookPayloadAdapter] = MappingProxyType(
    {
        "github": GitHubWebhookPayloadAdapter(),
        "gitlab": GitLabWebhookPayloadAdapter(),
    }
)
