from typing import Any

from cc_fastapi.workflows.base import (
    WorkflowCorrelationSpec,
    WorkflowEvent,
    WorkflowPlan,
    WorkflowTaskSpec,
)
from cc_fastapi.workflows.prompt_task import WebhookPromptTaskWorkflow


def github_pull_request_correlation(payload: dict[str, Any]) -> WorkflowCorrelationSpec | None:
    repository = payload.get("repository")
    pull_request = payload.get("pull_request")
    if not isinstance(repository, dict) or not isinstance(pull_request, dict):
        return None
    project_path = str(repository.get("full_name", "")).strip()
    pull_request_number = str(payload.get("number") or pull_request.get("number") or "").strip()
    if not project_path or not pull_request_number:
        return None
    return WorkflowCorrelationSpec(
        provider="github",
        resource_type="pull_request",
        project_path=project_path,
        resource_id=pull_request_number,
    )


class GitHubPromptTaskWorkflow(WebhookPromptTaskWorkflow):
    name = "github_prompt_task"
    version = "1"
    provider = "github"
    priority = -1000

    def _build_plan(self, event: WorkflowEvent, prompt: str, queue_name: str | None) -> WorkflowPlan:
        correlation = github_pull_request_correlation(event.payload)
        action = str(event.payload.get("action", "")).strip().lower()
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
