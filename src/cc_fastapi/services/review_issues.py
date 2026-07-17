from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cc_fastapi.db.models import (
    AgentTask,
    ReviewBatchStatus,
    ReviewIssue,
    ReviewIssueBatch,
    ReviewIssueSeverity,
    ReviewIssueVerificationStatus,
    WorkflowRun,
    utc_now,
)


class ReviewIssueNotFoundError(Exception):
    pass


class ReviewIssueConflictError(Exception):
    pass


class ReviewIssueReferenceError(Exception):
    pass


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
        if new_status in {ReviewBatchStatus.VERIFYING, ReviewBatchStatus.COMPLETED}:
            if not resulting_merged_sha:
                raise ReviewIssueConflictError("merged_sha is required before verification")
        if new_status == ReviewBatchStatus.COMPLETED:
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
        if new_status == ReviewBatchStatus.WAITING_MERGE and batch.extracted_at is None:
            batch.extracted_at = now
        if new_status == ReviewBatchStatus.COMPLETED:
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
