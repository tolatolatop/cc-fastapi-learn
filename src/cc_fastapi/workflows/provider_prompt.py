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

    def _build_plan(
        self,
        event: WorkflowEvent,
        prompt: str,
        queue_name: str | None,
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
