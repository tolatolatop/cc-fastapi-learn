from cc_fastapi.core.webhook_providers import webhook_provider_registry
from cc_fastapi.workflows.engine import WorkflowEngine
from cc_fastapi.workflows.provider_prompt import ProviderPromptTaskWorkflow
from cc_fastapi.workflows.registry import WorkflowRegistry


def build_default_workflow_engine() -> WorkflowEngine:
    return WorkflowEngine(
        WorkflowRegistry(
            ProviderPromptTaskWorkflow(
                definition.id,
                supersede_actions=definition.supersede_actions,
            )
            for definition in webhook_provider_registry.list()
        )
    )


__all__ = ["WorkflowEngine", "WorkflowRegistry", "build_default_workflow_engine"]
