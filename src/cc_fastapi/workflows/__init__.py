from cc_fastapi.workflows.engine import WorkflowEngine
from cc_fastapi.workflows.gitlab_prompt import GitLabPromptTaskWorkflow
from cc_fastapi.workflows.registry import WorkflowRegistry


def build_default_workflow_engine() -> WorkflowEngine:
    return WorkflowEngine(WorkflowRegistry([GitLabPromptTaskWorkflow()]))


__all__ = ["WorkflowEngine", "WorkflowRegistry", "build_default_workflow_engine"]
