from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from cc_fastapi.db.models import (
    AgentTask,
    AgentTaskRetryLink,
    Base,
    TaskStatus,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowCorrelation,
    WorkflowStepRun,
    WorkflowStepStatus,
    WorkflowTaskLink,
)
from cc_fastapi.workflows.base import (
    Workflow,
    WorkflowEvent,
    WorkflowPlan,
    WorkflowPostResult,
    WorkflowRetryConflictError,
    WorkflowTaskOutcome,
    WorkflowTaskSpec,
)
from cc_fastapi.workflows.engine import WorkflowEngine
from cc_fastapi.workflows.registry import WorkflowRegistry


def make_db():
    engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, future=True)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)()


def make_session_factory(tmp_path):
    engine = create_engine(
        f"sqlite+pysqlite:///{tmp_path / 'workflow-concurrency.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)


class SkipWorkflow(Workflow):
    name = "skip_large_change"

    def matches(self, event: WorkflowEvent) -> bool:
        return event.event_type == "large_change"

    def before(self, event: WorkflowEvent) -> WorkflowPlan:
        return WorkflowPlan.skip("changed_files_exceeded", context={"changed_files": 500})


class MultiTaskWorkflow(Workflow):
    name = "multi_task"

    def matches(self, event: WorkflowEvent) -> bool:
        return event.event_type == "merge_completed"

    def before(self, event: WorkflowEvent) -> WorkflowPlan:
        return WorkflowPlan.create_tasks(
            WorkflowTaskSpec(prompt="update code graph", role="code_graph"),
            WorkflowTaskSpec(prompt="update knowledge base", role="knowledge_base"),
            context={"source": "merge"},
        )

    def after_task(
        self,
        event: WorkflowEvent,
        outcome: WorkflowTaskOutcome,
        context: dict,
    ) -> WorkflowPostResult:
        completed = list(context.get("completed_tasks", []))
        completed.append(outcome.task_id)
        return WorkflowPostResult(context_updates={"completed_tasks": completed})


class FallbackWorkflow(Workflow):
    name = "fallback"
    priority = -1000

    def matches(self, event: WorkflowEvent) -> bool:
        return True

    def before(self, event: WorkflowEvent) -> WorkflowPlan:
        return WorkflowPlan.skip("fallback")


def test_workflow_can_record_skip_without_creating_task():
    db = make_db()
    engine = WorkflowEngine(WorkflowRegistry([SkipWorkflow()]))

    execution = engine.start(
        db,
        WorkflowEvent(
            provider="gitlab",
            event_type="large_change",
            payload={
                "changed_files": 500,
                "object_kind": "merge_request",
                "project": {"path_with_namespace": "Group/Project"},
                "object_attributes": {"iid": 42},
            },
        ),
    )
    db.commit()

    assert execution.tasks == ()
    assert execution.run.status == WorkflowRunStatus.SKIPPED
    assert execution.run.skip_reason == "changed_files_exceeded"
    assert db.scalar(select(func.count()).select_from(AgentTask)) == 0
    step = db.scalar(select(WorkflowStepRun).where(WorkflowStepRun.workflow_run_id == execution.run.id))
    assert step is not None
    assert step.status == WorkflowStepStatus.SKIPPED
    assert step.output_json["decision"] == "skip"
    assert step.output_json["reason"] == "changed_files_exceeded"
    correlation = db.scalar(
        select(WorkflowCorrelation).where(
            WorkflowCorrelation.workflow_run_id == execution.run.id
        )
    )
    assert correlation is not None
    assert correlation.project_path == "group/project"
    assert correlation.resource_id == "42"


def test_registry_prefers_specific_workflow_over_fallback():
    registry = WorkflowRegistry([FallbackWorkflow(), SkipWorkflow()])

    selected = registry.resolve(WorkflowEvent(provider="gitlab", event_type="large_change", payload={}))

    assert selected.name == "skip_large_change"


def test_workflow_waits_for_all_tasks_and_runs_post_steps_once():
    db = make_db()
    engine = WorkflowEngine(WorkflowRegistry([MultiTaskWorkflow()]))
    execution = engine.start(
        db,
        WorkflowEvent(provider="gitlab", event_type="merge_completed", payload={"ref": "main"}),
    )
    db.commit()

    assert len(execution.tasks) == 2
    assert execution.run.status == WorkflowRunStatus.RUNNING
    links = list(
        db.scalars(
            select(WorkflowTaskLink)
            .where(WorkflowTaskLink.workflow_run_id == execution.run.id)
            .order_by(WorkflowTaskLink.ordinal)
        )
    )
    assert [link.role for link in links] == ["code_graph", "knowledge_base"]

    first, second = execution.tasks
    first.status = TaskStatus.SUCCEEDED
    first.result = {"ok": True}
    db.commit()
    engine.handle_task_terminal(db, first.id)
    db.refresh(execution.run)
    assert execution.run.status == WorkflowRunStatus.RUNNING

    second.status = TaskStatus.SUCCEEDED
    second.result = {"ok": True}
    db.commit()
    engine.handle_task_terminal(db, second.id)
    db.refresh(execution.run)
    assert execution.run.status == WorkflowRunStatus.SUCCEEDED
    assert set(execution.run.context_json["completed_tasks"]) == {first.id, second.id}

    step_count = db.scalar(
        select(func.count()).select_from(WorkflowStepRun).where(WorkflowStepRun.workflow_run_id == execution.run.id)
    )
    assert step_count == 3
    assert engine.handle_task_terminal(db, second.id) == []
    assert db.scalar(
        select(func.count()).select_from(WorkflowStepRun).where(WorkflowStepRun.workflow_run_id == execution.run.id)
    ) == 3


def test_manual_task_retry_reopens_workflow_and_supersedes_failed_task():
    db = make_db()
    engine = WorkflowEngine(WorkflowRegistry([MultiTaskWorkflow()]))
    execution = engine.start(
        db,
        WorkflowEvent(provider="gitlab", event_type="merge_completed", payload={"ref": "main"}),
    )
    db.commit()
    first, second = execution.tasks
    first.status = TaskStatus.FAILED
    first.error_message = "first attempt failed"
    second.status = TaskStatus.SUCCEEDED
    db.commit()
    engine.handle_task_terminal(db, first.id)
    engine.handle_task_terminal(db, second.id)
    db.refresh(execution.run)
    assert execution.run.status == WorkflowRunStatus.FAILED

    retried = engine.retry_task(db, first.id)
    assert retried is not None
    db.refresh(execution.run)
    assert execution.run.status == WorkflowRunStatus.RUNNING
    original_link = db.scalar(select(WorkflowTaskLink).where(WorkflowTaskLink.task_id == first.id))
    retry_link = db.scalar(select(WorkflowTaskLink).where(WorkflowTaskLink.task_id == retried.id))
    assert original_link is not None and original_link.is_active is False
    assert retry_link is not None and retry_link.is_active is True
    assert retry_link.role == original_link.role

    retried.status = TaskStatus.SUCCEEDED
    retried.result = {"ok": True}
    db.commit()
    engine.handle_task_terminal(db, retried.id)
    db.refresh(execution.run)
    assert execution.run.status == WorkflowRunStatus.SUCCEEDED


def test_concurrent_terminal_callbacks_are_serialized_per_workflow(tmp_path):
    session_factory = make_session_factory(tmp_path)
    registry = WorkflowRegistry([MultiTaskWorkflow()])
    with session_factory() as db:
        execution = WorkflowEngine(registry).start(
            db,
            WorkflowEvent(provider="gitlab", event_type="merge_completed", payload={"ref": "main"}),
        )
        db.commit()
        run_id = execution.run.id
        first_id, second_id = (task.id for task in execution.tasks)
        for task_id in (first_id, second_id):
            task = db.get(AgentTask, task_id)
            assert task is not None
            task.status = TaskStatus.SUCCEEDED
            task.result = {"ok": True}
        db.commit()

    barrier = Barrier(3)

    def process_terminal(task_id: str) -> int:
        with session_factory() as db:
            barrier.wait()
            return len(WorkflowEngine(registry).handle_task_terminal(db, task_id))

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(process_terminal, [first_id, first_id, second_id]))

    assert sum(results) == 2
    with session_factory() as db:
        run = db.get(WorkflowRun, run_id)
        assert run is not None
        assert run.status == WorkflowRunStatus.SUCCEEDED
        assert sorted(run.context_json["completed_tasks"]) == sorted([first_id, second_id])
        assert run.context_json["completed_tasks"].count(first_id) == 1
        steps = list(
            db.scalars(
                select(WorkflowStepRun).where(
                    WorkflowStepRun.workflow_run_id == run_id,
                    WorkflowStepRun.step_name.like("after_task:%"),
                )
            )
        )
        assert sorted(step.step_name for step in steps) == sorted(
            [f"after_task:{first_id}", f"after_task:{second_id}"]
        )


def test_concurrent_manual_retry_creates_only_one_replacement(tmp_path):
    session_factory = make_session_factory(tmp_path)
    registry = WorkflowRegistry([MultiTaskWorkflow()])
    with session_factory() as db:
        execution = WorkflowEngine(registry).start(
            db,
            WorkflowEvent(provider="gitlab", event_type="merge_completed", payload={"ref": "main"}),
        )
        db.commit()
        original_task_id = execution.tasks[0].id
        run_id = execution.run.id

    barrier = Barrier(2)

    def retry_once() -> str:
        with session_factory() as db:
            barrier.wait()
            try:
                task = WorkflowEngine(registry).retry_task(db, original_task_id)
            except WorkflowRetryConflictError:
                return "conflict"
            assert task is not None
            return task.id

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: retry_once(), range(2)))

    assert results.count("conflict") == 1
    replacement_ids = [result for result in results if result != "conflict"]
    assert len(replacement_ids) == 1
    with session_factory() as db:
        links = list(
            db.scalars(
                select(WorkflowTaskLink)
                .where(WorkflowTaskLink.workflow_run_id == run_id)
                .order_by(WorkflowTaskLink.created_at)
            )
        )
        assert sum(link.is_active for link in links if link.ordinal == 0) == 1
        assert db.scalar(select(func.count()).select_from(AgentTaskRetryLink)) == 1
        assert db.get(AgentTask, replacement_ids[0]) is not None
        assert db.scalar(select(func.count()).select_from(AgentTask)) == 3
