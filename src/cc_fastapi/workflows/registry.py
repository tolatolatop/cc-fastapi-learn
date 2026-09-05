from collections.abc import Iterable

from cc_fastapi.workflows.base import Workflow, WorkflowEvent, WorkflowNotFoundError


class WorkflowRegistry:
    def __init__(self, workflows: Iterable[Workflow] = ()) -> None:
        self._workflows: list[Workflow] = []
        self._by_key: dict[tuple[str, str], Workflow] = {}
        for workflow in workflows:
            self.register(workflow)

    def register(self, workflow: Workflow) -> None:
        key = (workflow.name, workflow.version)
        if key in self._by_key:
            raise ValueError(f"workflow already registered: {workflow.name}@{workflow.version}")
        self._workflows.append(workflow)
        self._workflows.sort(key=lambda item: item.priority, reverse=True)
        self._by_key[key] = workflow

    def resolve(self, event: WorkflowEvent) -> Workflow:
        for workflow in self._workflows:
            if workflow.matches(event):
                return workflow
        raise WorkflowNotFoundError(f"no workflow matched event: {event.provider}/{event.event_type}")

    def get(self, name: str, version: str) -> Workflow:
        workflow = self._by_key.get((name, version))
        if workflow is None:
            raise WorkflowNotFoundError(f"workflow is not registered: {name}@{version}")
        return workflow
