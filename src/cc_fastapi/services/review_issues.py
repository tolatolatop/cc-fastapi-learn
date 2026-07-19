from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cc_fastapi.db.models import (
    AgentTask,
    ReviewBatchStatus,
    ReviewIssue,
    ReviewIssueBatch,
    ReviewIssueSeverity,
    ReviewIssueVerificationStatus,
    TaskStatus,
    WorkflowCorrelation,
    WorkflowRun,
    WorkflowTaskLink,
    utc_now,
)


class ReviewIssueNotFoundError(Exception):
    pass


class ReviewIssueConflictError(Exception):
    pass


class ReviewIssueReferenceError(Exception):
    pass


class ReviewIssueFilterError(Exception):
    pass


@dataclass
class PullRequestIssueRecords:
    latest_batch: ReviewIssueBatch
    pr_url: str | None
    items: list[tuple[ReviewIssue, ReviewIssueBatch]]
    total: int
    tasks: dict[str, AgentTask]
    summary: dict[str, Any]


class ReviewIssueService:
    @staticmethod
    def _get_batch_for_update(db: Session, batch_id: str) -> ReviewIssueBatch:
        batch = db.scalar(
            select(ReviewIssueBatch).where(ReviewIssueBatch.id == batch_id).with_for_update()
        )
        if batch is None:
            raise ReviewIssueNotFoundError("review issue batch not found")
        return batch

    @staticmethod
    def _validate_task_reference(db: Session, task_id: str | None, label: str) -> None:
        if task_id is not None and db.get(AgentTask, task_id) is None:
            raise ReviewIssueReferenceError(f"{label} not found")

    def create_batch(self, db: Session, values: dict[str, Any]) -> ReviewIssueBatch:
        self._validate_task_reference(db, values["review_task_id"], "review task")
        self._validate_task_reference(db, values.get("extract_task_id"), "extract task")
        self._validate_task_reference(db, values.get("verify_task_id"), "verify task")
        workflow_run_id = values.get("review_workflow_run_id")
        if workflow_run_id is not None and db.get(WorkflowRun, workflow_run_id) is None:
            raise ReviewIssueReferenceError("review workflow run not found")
        if workflow_run_id is not None:
            review_task = db.get(AgentTask, values["review_task_id"])
            link = db.scalar(
                select(WorkflowTaskLink).where(
                    WorkflowTaskLink.workflow_run_id == workflow_run_id,
                    WorkflowTaskLink.task_id == values["review_task_id"],
                )
            )
            if link is None:
                raise ReviewIssueConflictError(
                    "review task does not belong to the review workflow run"
                )
            if not link.is_active or review_task is None or review_task.status != TaskStatus.SUCCEEDED:
                raise ReviewIssueConflictError(
                    "review task must be active and succeeded before issue collection"
                )
            correlation = db.scalar(
                select(WorkflowCorrelation).where(
                    WorkflowCorrelation.workflow_run_id == workflow_run_id,
                    WorkflowCorrelation.provider == values["provider"],
                    WorkflowCorrelation.resource_type.in_(
                        ("merge_request", "pull_request")
                    ),
                    WorkflowCorrelation.project_path == values["project_path"],
                    WorkflowCorrelation.resource_id == values["pr_number"],
                )
            )
            if correlation is None:
                raise ReviewIssueConflictError(
                    "review workflow run does not belong to the pull request"
                )

        batch = ReviewIssueBatch(**values)
        db.add(batch)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise ReviewIssueConflictError(
                "review task already has a collection batch or a task reference is already in use"
            ) from exc
        db.refresh(batch)
        return batch

    @staticmethod
    def get_batch(db: Session, batch_id: str) -> ReviewIssueBatch | None:
        return db.get(ReviewIssueBatch, batch_id)

    @staticmethod
    def _batch_filters(
        *,
        provider: str | None,
        project_path: str | None,
        pr_number: str | None,
        review_task_id: str | None,
        statuses: list[ReviewBatchStatus] | None,
        created_from: datetime | None,
        created_to: datetime | None,
    ) -> list[Any]:
        filters: list[Any] = []
        if provider:
            filters.append(ReviewIssueBatch.provider == provider.strip())
        if project_path:
            filters.append(ReviewIssueBatch.project_path == project_path.strip())
        if pr_number:
            filters.append(ReviewIssueBatch.pr_number == pr_number.strip())
        if review_task_id:
            filters.append(ReviewIssueBatch.review_task_id == review_task_id.strip())
        if statuses:
            filters.append(ReviewIssueBatch.status.in_(statuses))
        if created_from is not None:
            filters.append(ReviewIssueBatch.created_at >= created_from)
        if created_to is not None:
            filters.append(ReviewIssueBatch.created_at <= created_to)
        return filters

    def list_batches(
        self,
        db: Session,
        *,
        provider: str | None,
        project_path: str | None,
        pr_number: str | None,
        review_task_id: str | None,
        statuses: list[ReviewBatchStatus] | None,
        created_from: datetime | None,
        created_to: datetime | None,
        offset: int,
        limit: int,
    ) -> tuple[list[ReviewIssueBatch], int]:
        filters = self._batch_filters(
            provider=provider,
            project_path=project_path,
            pr_number=pr_number,
            review_task_id=review_task_id,
            statuses=statuses,
            created_from=created_from,
            created_to=created_to,
        )
        items = list(
            db.scalars(
                select(ReviewIssueBatch)
                .where(*filters)
                .order_by(ReviewIssueBatch.created_at.desc(), ReviewIssueBatch.id.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        total = db.scalar(
            select(func.count()).select_from(ReviewIssueBatch).where(*filters)
        ) or 0
        return items, total

    def update_batch(
        self,
        db: Session,
        batch_id: str,
        values: dict[str, Any],
    ) -> ReviewIssueBatch:
        batch = self._get_batch_for_update(db, batch_id)
        self._validate_task_reference(db, values.get("extract_task_id"), "extract task")
        self._validate_task_reference(db, values.get("verify_task_id"), "verify task")

        resulting_merged_sha = values.get("merged_sha", batch.merged_sha)
        new_status = values.get("status")
        is_transition = new_status is not None and new_status != batch.status
        terminal_statuses = {
            ReviewBatchStatus.COMPLETED,
            ReviewBatchStatus.FAILED,
            ReviewBatchStatus.CANCELLED,
        }
        changed_values = {
            field: value
            for field, value in values.items()
            if getattr(batch, field) != value
        }
        if batch.status in terminal_statuses and changed_values:
            raise ReviewIssueConflictError("terminal review issue batches are immutable")
        allowed_transitions = {
            ReviewBatchStatus.COLLECTING: {
                ReviewBatchStatus.FAILED,
                ReviewBatchStatus.CANCELLED,
            },
            ReviewBatchStatus.WAITING_MERGE: {
                ReviewBatchStatus.VERIFYING,
                ReviewBatchStatus.COMPLETED,
                ReviewBatchStatus.FAILED,
                ReviewBatchStatus.CANCELLED,
            },
            ReviewBatchStatus.VERIFYING: {
                ReviewBatchStatus.COMPLETED,
                ReviewBatchStatus.FAILED,
                ReviewBatchStatus.CANCELLED,
            },
            ReviewBatchStatus.COMPLETED: set(),
            ReviewBatchStatus.FAILED: set(),
            ReviewBatchStatus.CANCELLED: set(),
        }
        if is_transition and new_status not in allowed_transitions[batch.status]:
            raise ReviewIssueConflictError(
                f"cannot transition review issue batch from {batch.status.value} to {new_status.value}"
            )
        requested_merged_sha = values.get("merged_sha")
        if (
            requested_merged_sha is not None
            and batch.merged_sha is not None
            and requested_merged_sha != batch.merged_sha
        ):
            raise ReviewIssueConflictError("merged_sha cannot be changed once recorded")
        if new_status in {ReviewBatchStatus.VERIFYING, ReviewBatchStatus.COMPLETED}:
            if not resulting_merged_sha:
                raise ReviewIssueConflictError("merged_sha is required before verification")
        if new_status == ReviewBatchStatus.COMPLETED:
            if batch.status == ReviewBatchStatus.WAITING_MERGE and (
                batch.issue_count != 0 or batch.extracted_at is None
            ):
                raise ReviewIssueConflictError(
                    "only a collected zero-issue batch can complete without verification"
                )
            unverified = db.scalar(
                select(func.count())
                .select_from(ReviewIssue)
                .where(
                    ReviewIssue.batch_id == batch.id,
                    ReviewIssue.verification_status
                    == ReviewIssueVerificationStatus.UNVERIFIED,
                )
            ) or 0
            if unverified:
                raise ReviewIssueConflictError(
                    "cannot complete a batch while issues remain unverified"
                )

        for field, value in values.items():
            setattr(batch, field, value)
        now = utc_now()
        if is_transition and new_status == ReviewBatchStatus.WAITING_MERGE and batch.extracted_at is None:
            batch.extracted_at = now
        if is_transition and new_status == ReviewBatchStatus.COMPLETED:
            batch.verified_at = now
        batch.updated_at = now
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise ReviewIssueConflictError("task reference is already in use") from exc
        db.refresh(batch)
        return batch

    def create_issues(
        self,
        db: Session,
        batch_id: str,
        items: list[dict[str, Any]],
    ) -> list[ReviewIssue]:
        batch = self._get_batch_for_update(db, batch_id)
        if batch.status != ReviewBatchStatus.COLLECTING:
            raise ReviewIssueConflictError(
                "issues can only be collected while the batch is collecting"
            )
        existing = db.scalar(
            select(func.count()).select_from(ReviewIssue).where(ReviewIssue.batch_id == batch.id)
        ) or 0
        if existing:
            raise ReviewIssueConflictError("review issues have already been collected")

        issues = [
            ReviewIssue(batch_id=batch.id, issue_no=index, **values)
            for index, values in enumerate(items, start=1)
        ]
        db.add_all(issues)
        now = utc_now()
        batch.issue_count = len(issues)
        batch.status = ReviewBatchStatus.WAITING_MERGE
        batch.extracted_at = now
        batch.updated_at = now
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise ReviewIssueConflictError("review issues have already been collected") from exc
        for issue in issues:
            db.refresh(issue)
        db.refresh(batch)
        return issues

    @staticmethod
    def get_issue(db: Session, issue_id: str) -> ReviewIssue | None:
        return db.get(ReviewIssue, issue_id)

    def list_issues(
        self,
        db: Session,
        *,
        batch_id: str | None,
        provider: str | None,
        project_path: str | None,
        pr_number: str | None,
        severities: list[ReviewIssueSeverity] | None,
        verification_statuses: list[ReviewIssueVerificationStatus] | None,
        category: str | None,
        created_from: datetime | None,
        created_to: datetime | None,
        batch_created_from: datetime | None,
        batch_created_to: datetime | None,
        offset: int,
        limit: int,
    ) -> tuple[list[ReviewIssue], int]:
        filters: list[Any] = []
        if batch_id:
            filters.append(ReviewIssue.batch_id == batch_id)
        if provider:
            filters.append(ReviewIssueBatch.provider == provider.strip())
        if project_path:
            filters.append(ReviewIssueBatch.project_path == project_path.strip())
        if pr_number:
            filters.append(ReviewIssueBatch.pr_number == pr_number.strip())
        if severities:
            filters.append(ReviewIssue.severity.in_(severities))
        if verification_statuses:
            filters.append(ReviewIssue.verification_status.in_(verification_statuses))
        if category:
            filters.append(ReviewIssue.category == category.strip())
        if created_from is not None:
            filters.append(ReviewIssue.created_at >= created_from)
        if created_to is not None:
            filters.append(ReviewIssue.created_at <= created_to)
        if batch_created_from is not None:
            filters.append(ReviewIssueBatch.created_at >= batch_created_from)
        if batch_created_to is not None:
            filters.append(ReviewIssueBatch.created_at <= batch_created_to)

        base_query = select(ReviewIssue).join(
            ReviewIssueBatch, ReviewIssueBatch.id == ReviewIssue.batch_id
        )
        items = list(
            db.scalars(
                base_query.where(*filters)
                .order_by(ReviewIssue.created_at.desc(), ReviewIssue.id.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        total = db.scalar(
            select(func.count())
            .select_from(ReviewIssue)
            .join(ReviewIssueBatch, ReviewIssueBatch.id == ReviewIssue.batch_id)
            .where(*filters)
        ) or 0
        return items, total

    def list_pull_request_issue_records(
        self,
        db: Session,
        *,
        provider: str,
        project_path: str,
        pr_number: str,
        batch_id: str | None,
        severities: list[ReviewIssueSeverity] | None,
        verification_statuses: list[ReviewIssueVerificationStatus] | None,
        batch_statuses: list[ReviewBatchStatus] | None,
        commit_sha: str | None,
        category: str | None,
        created_from: datetime | None,
        created_to: datetime | None,
        offset: int,
        limit: int,
    ) -> PullRequestIssueRecords | None:
        identity = {
            "provider": provider.strip(),
            "project_path": project_path.strip(),
            "pr_number": pr_number.strip(),
        }
        for field_name, value in identity.items():
            if not value:
                raise ReviewIssueFilterError(f"{field_name} must not be blank")

        identity_filters = [
            ReviewIssueBatch.provider == identity["provider"],
            ReviewIssueBatch.project_path == identity["project_path"],
            ReviewIssueBatch.pr_number == identity["pr_number"],
        ]
        latest_batch = db.scalar(
            select(ReviewIssueBatch)
            .where(*identity_filters)
            .order_by(ReviewIssueBatch.created_at.desc(), ReviewIssueBatch.id.desc())
            .limit(1)
        )
        if latest_batch is None:
            return None
        pr_url = db.scalar(
            select(ReviewIssueBatch.pr_url)
            .where(*identity_filters, ReviewIssueBatch.pr_url.is_not(None))
            .order_by(ReviewIssueBatch.created_at.desc(), ReviewIssueBatch.id.desc())
            .limit(1)
        )

        batch_filters = list(identity_filters)
        if batch_id is not None:
            normalized_batch_id = batch_id.strip()
            if not normalized_batch_id:
                raise ReviewIssueFilterError("batch_id must not be blank")
            batch_filters.append(ReviewIssueBatch.id == normalized_batch_id)
        if batch_statuses:
            batch_filters.append(ReviewIssueBatch.status.in_(batch_statuses))
        if commit_sha is not None:
            normalized_sha = commit_sha.strip()
            if not normalized_sha:
                raise ReviewIssueFilterError("commit_sha must not be blank")
            batch_filters.append(
                or_(
                    ReviewIssueBatch.review_head_sha == normalized_sha,
                    ReviewIssueBatch.merged_sha == normalized_sha,
                )
            )

        issue_filters = list(batch_filters)
        if severities:
            issue_filters.append(ReviewIssue.severity.in_(severities))
        if verification_statuses:
            issue_filters.append(
                ReviewIssue.verification_status.in_(verification_statuses)
            )
        if category:
            issue_filters.append(ReviewIssue.category == category.strip())
        if created_from is not None:
            issue_filters.append(ReviewIssue.created_at >= created_from)
        if created_to is not None:
            issue_filters.append(ReviewIssue.created_at <= created_to)

        rows = list(
            db.execute(
                select(ReviewIssue, ReviewIssueBatch)
                .join(ReviewIssueBatch, ReviewIssueBatch.id == ReviewIssue.batch_id)
                .where(*issue_filters)
                .order_by(ReviewIssue.created_at.desc(), ReviewIssue.id.desc())
                .offset(offset)
                .limit(limit)
            ).all()
        )
        total = db.scalar(
            select(func.count())
            .select_from(ReviewIssue)
            .join(ReviewIssueBatch, ReviewIssueBatch.id == ReviewIssue.batch_id)
            .where(*issue_filters)
        ) or 0

        task_ids = {
            task_id
            for _issue, batch in rows
            for task_id in (
                batch.review_task_id,
                batch.extract_task_id,
                batch.verify_task_id,
            )
            if task_id is not None
        }
        tasks = (
            {
                task.id: task
                for task in db.scalars(
                    select(AgentTask).where(AgentTask.id.in_(task_ids))
                )
            }
            if task_ids
            else {}
        )

        batch_total = db.scalar(
            select(func.count()).select_from(ReviewIssueBatch).where(*batch_filters)
        ) or 0
        batch_status_rows = db.execute(
            select(ReviewIssueBatch.status, func.count())
            .where(*batch_filters)
            .group_by(ReviewIssueBatch.status)
        ).all()
        verification_rows = db.execute(
            select(ReviewIssue.verification_status, func.count())
            .join(ReviewIssueBatch, ReviewIssueBatch.id == ReviewIssue.batch_id)
            .where(*issue_filters)
            .group_by(ReviewIssue.verification_status)
        ).all()
        severity_rows = db.execute(
            select(ReviewIssue.severity, func.count())
            .join(ReviewIssueBatch, ReviewIssueBatch.id == ReviewIssue.batch_id)
            .where(*issue_filters)
            .group_by(ReviewIssue.severity)
        ).all()
        batch_status_counts = {status: 0 for status in ReviewBatchStatus}
        batch_status_counts.update(dict(batch_status_rows))
        verification_status_counts = {
            status: 0 for status in ReviewIssueVerificationStatus
        }
        verification_status_counts.update(dict(verification_rows))
        severity_counts = {severity: 0 for severity in ReviewIssueSeverity}
        severity_counts.update(dict(severity_rows))

        return PullRequestIssueRecords(
            latest_batch=latest_batch,
            pr_url=pr_url,
            items=rows,
            total=total,
            tasks=tasks,
            summary={
                "batch_total": batch_total,
                "issue_total": total,
                "batch_status_counts": batch_status_counts,
                "verification_status_counts": verification_status_counts,
                "severity_counts": severity_counts,
            },
        )

    def verify_issues(
        self,
        db: Session,
        batch_id: str,
        results: list[dict[str, Any]],
    ) -> list[ReviewIssue]:
        batch = self._get_batch_for_update(db, batch_id)
        if batch.status != ReviewBatchStatus.VERIFYING:
            raise ReviewIssueConflictError(
                "issues can only be verified while the batch is verifying"
            )

        issue_ids = [result["id"] for result in results]
        issues = list(
            db.scalars(
                select(ReviewIssue)
                .where(ReviewIssue.batch_id == batch.id, ReviewIssue.id.in_(issue_ids))
                .with_for_update()
            )
        )
        if len(issues) != len(issue_ids):
            raise ReviewIssueNotFoundError(
                "one or more review issues do not belong to the batch"
            )
        by_id = {issue.id: issue for issue in issues}
        now = utc_now()
        for result in results:
            issue = by_id[result["id"]]
            issue.verification_status = result["status"]
            issue.verification_note = result.get("note")
            issue.verified_at = now
            issue.updated_at = now

        db.flush()
        unverified = db.scalar(
            select(func.count())
            .select_from(ReviewIssue)
            .where(
                ReviewIssue.batch_id == batch.id,
                ReviewIssue.verification_status == ReviewIssueVerificationStatus.UNVERIFIED,
            )
        ) or 0
        if unverified == 0:
            batch.status = ReviewBatchStatus.COMPLETED
            batch.verified_at = now
        batch.updated_at = now
        db.commit()
        ordered = [by_id[result["id"]] for result in results]
        for issue in ordered:
            db.refresh(issue)
        return ordered

    def summarize(
        self,
        db: Session,
        *,
        provider: str | None,
        project_path: str | None,
        pr_number: str | None,
        created_from: datetime | None,
        created_to: datetime | None,
    ) -> dict[str, Any]:
        batch_filters = self._batch_filters(
            provider=provider,
            project_path=project_path,
            pr_number=pr_number,
            review_task_id=None,
            statuses=None,
            created_from=created_from,
            created_to=created_to,
        )
        batch_total = db.scalar(
            select(func.count()).select_from(ReviewIssueBatch).where(*batch_filters)
        ) or 0
        zero_issue_batches = db.scalar(
            select(func.count())
            .select_from(ReviewIssueBatch)
            .where(
                *batch_filters,
                ReviewIssueBatch.extracted_at.is_not(None),
                ReviewIssueBatch.issue_count == 0,
            )
        ) or 0
        batch_status_rows = db.execute(
            select(ReviewIssueBatch.status, func.count())
            .where(*batch_filters)
            .group_by(ReviewIssueBatch.status)
        ).all()

        issue_base = (
            select(ReviewIssue)
            .join(ReviewIssueBatch, ReviewIssueBatch.id == ReviewIssue.batch_id)
            .where(*batch_filters)
        )
        issue_total = db.scalar(
            select(func.count()).select_from(issue_base.subquery())
        ) or 0
        verification_rows = db.execute(
            select(ReviewIssue.verification_status, func.count())
            .join(ReviewIssueBatch, ReviewIssueBatch.id == ReviewIssue.batch_id)
            .where(*batch_filters)
            .group_by(ReviewIssue.verification_status)
        ).all()
        severity_rows = db.execute(
            select(ReviewIssue.severity, func.count())
            .join(ReviewIssueBatch, ReviewIssueBatch.id == ReviewIssue.batch_id)
            .where(*batch_filters)
            .group_by(ReviewIssue.severity)
        ).all()

        batch_status_counts = {status: 0 for status in ReviewBatchStatus}
        batch_status_counts.update(dict(batch_status_rows))
        verification_status_counts = {
            status: 0 for status in ReviewIssueVerificationStatus
        }
        verification_status_counts.update(dict(verification_rows))
        severity_counts = {severity: 0 for severity in ReviewIssueSeverity}
        severity_counts.update(dict(severity_rows))
        accepted = verification_status_counts[ReviewIssueVerificationStatus.ACCEPTED]
        verified = accepted + verification_status_counts[
            ReviewIssueVerificationStatus.NOT_ACCEPTED
        ]
        return {
            "batch_total": batch_total,
            "zero_issue_batches": zero_issue_batches,
            "batch_status_counts": batch_status_counts,
            "issue_total": issue_total,
            "verified_issues": verified,
            "accepted_issues": accepted,
            "acceptance_rate": accepted / verified if verified else None,
            "verification_status_counts": verification_status_counts,
            "severity_counts": severity_counts,
        }
