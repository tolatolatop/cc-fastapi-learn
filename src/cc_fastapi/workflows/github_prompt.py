from cc_fastapi.workflows.base import (
    WorkflowEvent,
    WorkflowPlan,
    WorkflowTaskSpec,
)
from cc_fastapi.workflows.correlations import change_request_correlation
from cc_fastapi.workflows.prompt_task import WebhookPromptTaskWorkflow


class GitHubPromptTaskWorkflow(WebhookPromptTaskWorkflow):
    name = "github_prompt_task"
    version = "1"
    provider = "github"
    priority = -1000

    def _build_plan(self, event: WorkflowEvent, prompt: str, queue_name: str | None) -> WorkflowPlan:
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
        supersede_correlations = correlations if action == "synchronize" else ()
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
