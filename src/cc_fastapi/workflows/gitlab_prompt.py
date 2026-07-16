from pathlib import Path
from typing import Any

from jinja2 import StrictUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment

from cc_fastapi.workflows.base import (
    Workflow,
    WorkflowCorrelationSpec,
    WorkflowEvent,
    WorkflowPlan,
    WorkflowPostResult,
    WorkflowTaskOutcome,
    WorkflowTaskSpec,
    WorkflowTemplateError,
)


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


class GitLabPromptTaskWorkflow(Workflow):
    name = "gitlab_prompt_task"
    version = "1"
    # This is the catch-all GitLab workflow. More specific workflows register
    # with the default priority (or higher) and are evaluated first.
    priority = -1000

    def __init__(self) -> None:
        self.template_environment = SandboxedEnvironment(
            autoescape=False,
            undefined=StrictUndefined,
        )

    def matches(self, event: WorkflowEvent) -> bool:
        return event.provider == "gitlab"

    def _render_prompt(self, event: WorkflowEvent) -> str:
        template_path = str(event.config.get("prompt_template_path", "")).strip()
        if not template_path:
            raise WorkflowTemplateError("failed to load webhook prompt template: path is empty")
        try:
            template_source = Path(template_path).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise WorkflowTemplateError(f"failed to load webhook prompt template: {template_path}") from exc

        webhook = {
            "provider": event.provider,
            "event_type": event.event_type,
            "event_uuid": event.event_uuid,
            "webhook_uuid": event.webhook_uuid,
            "instance_url": event.instance_url,
        }
        render_context: dict[str, Any] = {
            **event.payload,
            "payload": event.payload,
            "event_type": event.event_type,
            "webhook": webhook,
        }
        try:
            prompt = self.template_environment.from_string(template_source).render(render_context).strip()
        except TemplateError as exc:
            raise WorkflowTemplateError(f"failed to render webhook prompt: {exc}") from exc
        if not prompt:
            raise WorkflowTemplateError("failed to render webhook prompt: rendered prompt is empty")
        return prompt

    def before(self, event: WorkflowEvent) -> WorkflowPlan:
        prompt = self._render_prompt(event)
        queue_name_value = event.config.get("queue_name")
        queue_name = str(queue_name_value).strip() if queue_name_value else None
        metadata = {
            "trigger": "gitlab_webhook",
            "gitlab": {
                "event_type": event.event_type,
                "event_uuid": event.event_uuid,
                "webhook_uuid": event.webhook_uuid,
                "instance_url": event.instance_url,
            },
        }
        correlation = gitlab_merge_request_correlation(event.payload)
        attributes = event.payload.get("object_attributes")
        action = str(attributes.get("action", "")).strip().lower() if isinstance(attributes, dict) else ""
        correlations = (correlation,) if correlation is not None else ()
        supersede_correlations = correlations if action == "update" else ()
        return WorkflowPlan.create_tasks(
            WorkflowTaskSpec(
                prompt=prompt,
                queue_name=queue_name,
                metadata=metadata,
            ),
            context={
                "prompt_template_path": event.config.get("prompt_template_path"),
                "planned_task_count": 1,
            },
            correlations=correlations,
            supersede_correlations=supersede_correlations,
        )

    def after_task(
        self,
        event: WorkflowEvent,
        outcome: WorkflowTaskOutcome,
        context: dict[str, Any],
    ) -> WorkflowPostResult:
        return WorkflowPostResult(
            context_updates={
                "last_task_id": outcome.task_id,
                "last_task_status": outcome.status.value,
            }
        )
