from collections.abc import Collection

from cc_fastapi.workflows.base import (
    WorkflowEvent,
    WorkflowPlan,
    WorkflowTaskSpec,
)
from cc_fastapi.workflows.correlations import change_request_correlation
from cc_fastapi.workflows.prompt_task import WebhookPromptTaskWorkflow


class ProviderPromptTaskWorkflow(WebhookPromptTaskWorkflow):
    version = "1"
    priority = -1000

    def __init__(
        self,
        provider: str,
        *,
        supersede_actions: Collection[str] = (),
    ) -> None:
        self.provider = provider.strip().casefold()
        if not self.provider:
            raise ValueError("workflow provider must not be blank")
        self.name = f"{self.provider}_prompt_task"
        self.supersede_actions = frozenset(
            action.strip().casefold() for action in supersede_actions
        )
        super().__init__()

    @staticmethod
    def _gitlab_skip_reason(event: WorkflowEvent) -> str | None:
        parsed = event.webhook_payload
        if parsed is None:
            return "invalid_gitlab_event"
        if parsed.event_kind == "push":
            return None
        if parsed.event_kind == "merge_request":
            action = (
                parsed.change_request.action.casefold()
                if parsed.change_request and parsed.change_request.action
                else ""
            )
            if action in {"open", "reopen", "update", "merge"}:
                return None
            return "unsupported_gitlab_merge_request_action"
        if parsed.event_kind != "note":
            return "unsupported_gitlab_event"
        if parsed.change_request is None or parsed.comment is None:
            return "gitlab_note_not_on_merge_request"
        if (parsed.comment.target_type or "").casefold() not in {
            "mergerequest",
            "merge_request",
        }:
            return "gitlab_note_not_on_merge_request"
        if "<!-- cc-platform-operation:" in (parsed.comment.body or ""):
            return "agent_generated_note"
        return None

    def before(self, event: WorkflowEvent) -> WorkflowPlan:
        if self.provider == "gitlab":
            skip_reason = self._gitlab_skip_reason(event)
            if skip_reason is not None:
                correlation = change_request_correlation(event.webhook_payload)
                return WorkflowPlan.skip(
                    skip_reason,
                    correlations=(correlation,) if correlation is not None else (),
                )
        return super().before(event)

    def _build_plan(
        self,
        event: WorkflowEvent,
        prompt: str,
        queue_name: str | None,
        model: str | None,
    ) -> WorkflowPlan:
        parsed_payload = event.webhook_payload
        correlation = change_request_correlation(parsed_payload)
        action = (
            parsed_payload.change_request.action.casefold()
            if parsed_payload
            and parsed_payload.change_request
            and parsed_payload.change_request.action
            else ""
        )
        correlations = (correlation,) if correlation is not None else ()
        supersede_correlations = (
            correlations if action in self.supersede_actions else ()
        )
        return WorkflowPlan.create_tasks(
            WorkflowTaskSpec(
                prompt=prompt,
                model=model,
                queue_name=queue_name,
                metadata=self._task_metadata(event),
            ),
            context={
                "prompt_template_path": event.config.get("prompt_template_path"),
                "planned_task_count": 1,
            },
            correlations=correlations,
            supersede_correlations=supersede_correlations,
        )
