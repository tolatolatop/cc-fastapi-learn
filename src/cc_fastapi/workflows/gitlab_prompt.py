from typing import Any

from cc_fastapi.workflows.base import (
    WorkflowCorrelationSpec,
    WorkflowEvent,
    WorkflowPlan,
    WorkflowTaskSpec,
)
from cc_fastapi.workflows.prompt_task import WebhookPromptTaskWorkflow


def gitlab_merge_request_correlation(payload: dict[str, Any]) -> WorkflowCorrelationSpec | None:
    if str(payload.get("object_kind", "")).strip().lower() != "merge_request":
        return None
    project = payload.get("project")
    attributes = payload.get("object_attributes")
    if not isinstance(project, dict) or not isinstance(attributes, dict):
        return None
    project_path = str(project.get("path_with_namespace", "")).strip()
    merge_request_iid = str(attributes.get("iid", "")).strip()
    if not project_path or not merge_request_iid:
        return None
    return WorkflowCorrelationSpec(
        provider="gitlab",
        resource_type="merge_request",
        project_path=project_path,
        resource_id=merge_request_iid,
    )


class GitLabPromptTaskWorkflow(WebhookPromptTaskWorkflow):
    name = "gitlab_prompt_task"
    version = "1"
    provider = "gitlab"
    # This is the catch-all GitLab workflow. More specific workflows register
    # with the default priority (or higher) and are evaluated first.
    priority = -1000

    def _build_plan(self, event: WorkflowEvent, prompt: str, queue_name: str | None) -> WorkflowPlan:
        correlation = gitlab_merge_request_correlation(event.payload)
        attributes = event.payload.get("object_attributes")
        action = str(attributes.get("action", "")).strip().lower() if isinstance(attributes, dict) else ""
        correlations = (correlation,) if correlation is not None else ()
        supersede_correlations = correlations if action == "update" else ()
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
