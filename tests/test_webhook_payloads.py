from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest

from cc_fastapi.core.webhook_payloads import (
    WebhookActor,
    WebhookChangeRequest,
    WebhookPayload,
    WebhookRepository,
)


def test_github_push_payload_is_normalized():
    parsed = WebhookPayload.from_payload(
        " GitHub ",
        "push",
        {
            "ref": "refs/heads/Feature/API",
            "repository": {
                "full_name": "/Octo-Org/Console/",
                "html_url": "https://github.com/Octo-Org/Console/",
            },
            "sender": {"login": "octocat"},
        },
    )

    assert parsed == WebhookPayload(
        provider="github",
        event_type="push",
        event_kind="push",
        repository=WebhookRepository(
            project_path="octo-org/console",
            web_url="https://github.com/Octo-Org/Console",
        ),
        actor=WebhookActor(display_name="octocat", username="octocat"),
        ref="Feature/API",
    )


def test_github_pull_request_payload_exposes_change_request():
    parsed = WebhookPayload.from_payload(
        "github",
        "pull_request",
        {
            "action": "synchronize",
            "number": 17,
            "repository": {"full_name": "org/project"},
            "pull_request": {
                "head": {"ref": "feature-17", "sha": "head-sha"},
                "base": {"ref": "main"},
            },
        },
    )

    assert parsed is not None
    assert parsed.ref == "feature-17"
    assert parsed.change_request == WebhookChangeRequest(
        resource_type="pull_request",
        number="17",
        action="synchronize",
        source_branch="feature-17",
        target_branch="main",
        head_sha="head-sha",
    )


def test_gitlab_merge_request_payload_is_normalized():
    parsed = WebhookPayload.from_payload(
        "GITLAB",
        "Merge Request Hook",
        {
            "object_kind": "merge_request",
            "project": {
                "path_with_namespace": "Group/API",
                "web_url": "https://gitlab.example.com/Group/API/",
            },
            "user": {"name": "Reviewer", "username": "reviewer"},
            "object_attributes": {
                "iid": 8,
                "action": "update",
                "source_branch": "feature-8",
                "target_branch": "main",
                "last_commit": {"id": "head-sha"},
            },
        },
    )

    assert parsed == WebhookPayload(
        provider="gitlab",
        event_type="Merge Request Hook",
        event_kind="merge_request",
        repository=WebhookRepository(
            project_path="group/api",
            web_url="https://gitlab.example.com/Group/API",
        ),
        actor=WebhookActor(display_name="Reviewer", username="reviewer"),
        ref="feature-8",
        change_request=WebhookChangeRequest(
            resource_type="merge_request",
            number="8",
            action="update",
            source_branch="feature-8",
            target_branch="main",
            head_sha="head-sha",
        ),
    )


def test_unknown_provider_returns_safe_empty_projection():
    parsed = WebhookPayload.from_payload(
        "bitbucket",
        "repo:push",
        {"repository": {"full_name": "team/project"}},
    )

    assert parsed == WebhookPayload(
        provider="bitbucket",
        event_type="repo:push",
        event_kind="repo:push",
    )


def test_malformed_optional_fields_do_not_break_projection():
    parsed = WebhookPayload.from_payload(
        "github",
        "push",
        {
            "repository": {
                "full_name": "org/project",
                "html_url": "not-an-http-url",
            },
            "sender": "not-an-object",
        },
    )

    assert parsed is not None
    assert parsed.repository == WebhookRepository(project_path="org/project")
    assert parsed.actor is None


def test_projection_is_immutable():
    parsed = WebhookPayload.from_payload("github", "push", {})
    assert parsed is not None

    with pytest.raises(FrozenInstanceError):
        parsed.ref = "changed"  # type: ignore[misc]


def test_parsing_does_not_mutate_raw_payload():
    payload = {
        "ref": "refs/heads/main",
        "repository": {"full_name": "org/project"},
        "sender": {"login": "octocat"},
    }
    original = deepcopy(payload)

    WebhookPayload.from_payload("github", "push", payload)

    assert payload == original
