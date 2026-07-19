from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cc_fastapi.core.repository_values import (
    normalize_repository_project_path,
    normalize_repository_provider,
    normalize_repository_search,
)
from cc_fastapi.core.webhook_payloads import WebhookPayload
from cc_fastapi.db.models import (
    AgentTask,
    AgentTaskContext,
    TaskStatus,
    WebhookTrigger,
    WorkflowCorrelation,
    WorkflowRun,
    WorkflowRunStatus,
    WorkflowTaskLink,
)


CHANGE_REQUEST_RESOURCE_TYPES = ("merge_request", "pull_request")


@dataclass(frozen=True)
class MergeRequestTaskRecord:
    task: AgentTask
    link: WorkflowTaskLink
    workflow_run: WorkflowRun
    webhook_trigger: WebhookTrigger | None
    task_context: AgentTaskContext | None


@dataclass(frozen=True)
class ChangeRequestRecord:
    provider: str
    resource_type: str
    project_path: str
    pr_number: str
    workflow_run: WorkflowRun
    parsed_payload: WebhookPayload | None
    latest_task: AgentTask | None


@dataclass(frozen=True)
class ChangeRequestDetailRecord:
    change_request: ChangeRequestRecord
    workflow_runs: list[WorkflowRun]


class MergeRequestTaskService:
    @staticmethod
    def _identity(
        provider: str,
        project_path: str,
        pr_number: str,
    ) -> tuple[str, str, str]:
        normalized_number = pr_number.strip()
        if not normalized_number:
            raise ValueError("pr_number must not be blank")
        return (
            normalize_repository_provider(provider),
            normalize_repository_project_path(project_path),
            normalized_number,
        )

    @staticmethod
    def _parse_run(run: WorkflowRun) -> WebhookPayload | None:
        payload = run.payload_json if isinstance(run.payload_json, dict) else {}
        return WebhookPayload.from_payload(run.provider, run.event_type, payload)

    @staticmethod
    def _latest_tasks(
        db: Session,
        workflow_run_ids: list[str],
    ) -> dict[str, AgentTask]:
        if not workflow_run_ids:
            return {}
        rows = db.execute(
            select(WorkflowTaskLink.workflow_run_id, AgentTask)
            .join(AgentTask, AgentTask.id == WorkflowTaskLink.task_id)
            .where(
                WorkflowTaskLink.workflow_run_id.in_(workflow_run_ids),
                WorkflowTaskLink.is_active.is_(True),
            )
            .order_by(
                WorkflowTaskLink.workflow_run_id.asc(),
                AgentTask.created_at.desc(),
                WorkflowTaskLink.ordinal.asc(),
            )
        ).all()
        latest: dict[str, AgentTask] = {}
        for workflow_run_id, task in rows:
            latest.setdefault(workflow_run_id, task)
        return latest

    def list_change_requests(
        self,
        db: Session,
        *,
        provider: str | None,
        project_path: str | None,
        pr_number: str | None,
        states: list[str] | None,
        search: str | None,
        offset: int,
        limit: int,
    ) -> tuple[list[ChangeRequestRecord], int]:
        provider_filter = (
            normalize_repository_provider(provider) if provider is not None else None
        )
        project_filter = (
            normalize_repository_project_path(project_path)
            if project_path is not None
            else None
        )
        number_filter = pr_number.strip() if pr_number is not None else None
        correlation_filters = [
            WorkflowCorrelation.resource_type.in_(CHANGE_REQUEST_RESOURCE_TYPES)
        ]
        if provider_filter is not None:
            correlation_filters.append(WorkflowCorrelation.provider == provider_filter)
        if project_filter is not None:
            correlation_filters.append(
                WorkflowCorrelation.project_path == project_filter
            )
        if number_filter is not None:
            correlation_filters.append(WorkflowCorrelation.resource_id == number_filter)
        ranked = (
            select(
                WorkflowCorrelation.provider.label("provider"),
                WorkflowCorrelation.resource_type.label("resource_type"),
                WorkflowCorrelation.project_path.label("project_path"),
                WorkflowCorrelation.resource_id.label("pr_number"),
                WorkflowRun.id.label("workflow_run_id"),
                func.row_number()
                .over(
                    partition_by=(
                        WorkflowCorrelation.provider,
                        WorkflowCorrelation.project_path,
                        WorkflowCorrelation.resource_id,
                    ),
                    order_by=(WorkflowRun.created_at.desc(), WorkflowRun.id.desc()),
                )
                .label("row_number"),
            )
            .join(WorkflowRun, WorkflowRun.id == WorkflowCorrelation.workflow_run_id)
            .where(*correlation_filters)
            .subquery()
        )
        rows = db.execute(select(ranked).where(ranked.c.row_number == 1)).all()
        run_ids = [row.workflow_run_id for row in rows]
        runs = (
            {
                run.id: run
                for run in db.scalars(
                    select(WorkflowRun).where(WorkflowRun.id.in_(run_ids))
                )
            }
            if run_ids
            else {}
        )
        latest_tasks = self._latest_tasks(db, run_ids)

        state_filters = {
            value.strip().casefold() for value in states or [] if value.strip()
        }
        search_filter = normalize_repository_search(search) if search else ""

        records: list[ChangeRequestRecord] = []
        for row in rows:
            run = runs.get(row.workflow_run_id)
            if run is None:
                continue
            parsed = self._parse_run(run)
            change_request = parsed.change_request if parsed is not None else None
            title = change_request.title if change_request is not None else None
            state = change_request.state if change_request is not None else None
            if provider_filter is not None and row.provider != provider_filter:
                continue
            if project_filter is not None and row.project_path != project_filter:
                continue
            if number_filter is not None and row.pr_number != number_filter:
                continue
            if state_filters and (state or "unknown").casefold() not in state_filters:
                continue
            if search_filter and search_filter not in " ".join(
                value.casefold()
                for value in (row.project_path, row.pr_number, title or "")
            ):
                continue
            records.append(
                ChangeRequestRecord(
                    provider=row.provider,
                    resource_type=row.resource_type,
                    project_path=row.project_path,
                    pr_number=row.pr_number,
                    workflow_run=run,
                    parsed_payload=parsed,
                    latest_task=latest_tasks.get(run.id),
                )
            )
        records.sort(
            key=lambda record: (record.workflow_run.created_at, record.workflow_run.id),
            reverse=True,
        )
        return records[offset : offset + limit], len(records)

    def get_change_request_detail(
        self,
        db: Session,
        *,
        provider: str,
        project_path: str,
        pr_number: str,
    ) -> ChangeRequestDetailRecord | None:
        provider, project_path, pr_number = self._identity(
            provider, project_path, pr_number
        )
        runs = list(
            db.scalars(
                select(WorkflowRun)
                .join(
                    WorkflowCorrelation,
                    WorkflowCorrelation.workflow_run_id == WorkflowRun.id,
                )
                .where(
                    WorkflowCorrelation.provider == provider,
                    WorkflowCorrelation.resource_type.in_(
                        CHANGE_REQUEST_RESOURCE_TYPES
                    ),
                    WorkflowCorrelation.project_path == project_path,
                    WorkflowCorrelation.resource_id == pr_number,
                )
                .distinct()
                .order_by(WorkflowRun.created_at.desc(), WorkflowRun.id.desc())
            )
        )
        if not runs:
            return None
        latest = runs[0]
        latest_tasks = self._latest_tasks(db, [latest.id])
        parsed = self._parse_run(latest)
        resource_type = (
            parsed.change_request.resource_type
            if parsed is not None and parsed.change_request is not None
            else "change_request"
        )
        return ChangeRequestDetailRecord(
            change_request=ChangeRequestRecord(
                provider=provider,
                resource_type=resource_type,
                project_path=project_path,
                pr_number=pr_number,
                workflow_run=latest,
                parsed_payload=parsed,
                latest_task=latest_tasks.get(latest.id),
            ),
            workflow_runs=runs,
        )

    def list_tasks(
        self,
        db: Session,
        *,
        provider: str,
        project_path: str,
        pr_number: str,
        task_id: str | None,
        task_statuses: list[TaskStatus] | None,
        workflow_statuses: list[WorkflowRunStatus] | None,
        role: str | None,
        is_active: bool | None,
        has_result: bool | None,
        created_from: datetime | None,
        created_to: datetime | None,
        offset: int,
        limit: int,
    ) -> tuple[list[MergeRequestTaskRecord], int]:
        provider, project_path, pr_number = self._identity(
            provider, project_path, pr_number
        )
        filters = [
            WorkflowCorrelation.provider == provider,
            WorkflowCorrelation.resource_type.in_(CHANGE_REQUEST_RESOURCE_TYPES),
            WorkflowCorrelation.project_path == project_path,
            WorkflowCorrelation.resource_id == pr_number,
        ]
        if task_id:
            filters.append(AgentTask.id == task_id.strip())
        if task_statuses:
            filters.append(AgentTask.status.in_(task_statuses))
        if workflow_statuses:
            filters.append(WorkflowRun.status.in_(workflow_statuses))
        if role:
            filters.append(WorkflowTaskLink.role == role.strip())
        if is_active is not None:
            filters.append(WorkflowTaskLink.is_active.is_(is_active))
        if has_result is True:
            filters.append(AgentTask.result.is_not(None))
        elif has_result is False:
            filters.append(AgentTask.result.is_(None))
        if created_from is not None:
            filters.append(AgentTask.created_at >= created_from)
        if created_to is not None:
            filters.append(AgentTask.created_at <= created_to)

        rows = db.execute(
            select(
                AgentTask,
                WorkflowTaskLink,
                WorkflowRun,
                WebhookTrigger,
                AgentTaskContext,
            )
            .join(WorkflowTaskLink, WorkflowTaskLink.task_id == AgentTask.id)
            .join(WorkflowRun, WorkflowRun.id == WorkflowTaskLink.workflow_run_id)
            .join(
                WorkflowCorrelation,
                WorkflowCorrelation.workflow_run_id == WorkflowRun.id,
            )
            .outerjoin(
                WebhookTrigger, WebhookTrigger.id == WorkflowRun.webhook_trigger_id
            )
            .outerjoin(AgentTaskContext, AgentTaskContext.task_id == AgentTask.id)
            .where(*filters)
            .order_by(AgentTask.created_at.desc(), WorkflowTaskLink.ordinal.asc())
            .offset(offset)
            .limit(limit)
        ).all()
        total = (
            db.scalar(
                select(func.count())
                .select_from(WorkflowTaskLink)
                .join(AgentTask, AgentTask.id == WorkflowTaskLink.task_id)
                .join(WorkflowRun, WorkflowRun.id == WorkflowTaskLink.workflow_run_id)
                .join(
                    WorkflowCorrelation,
                    WorkflowCorrelation.workflow_run_id == WorkflowRun.id,
                )
                .where(*filters)
            )
            or 0
        )
        return (
            [
                MergeRequestTaskRecord(
                    task=row[0],
                    link=row[1],
                    workflow_run=row[2],
                    webhook_trigger=row[3],
                    task_context=row[4],
                )
                for row in rows
            ],
            total,
        )

    def list_gitlab_tasks(
        self,
        db: Session,
        *,
        project_path: str,
        merge_request_iid: int,
        offset: int,
        limit: int,
    ) -> tuple[list[MergeRequestTaskRecord], int]:
        return self.list_tasks(
            db,
            provider="gitlab",
            project_path=project_path,
            pr_number=str(merge_request_iid),
            task_id=None,
            task_statuses=None,
            workflow_statuses=None,
            role=None,
            is_active=None,
            has_result=None,
            created_from=None,
            created_to=None,
            offset=offset,
            limit=limit,
        )
