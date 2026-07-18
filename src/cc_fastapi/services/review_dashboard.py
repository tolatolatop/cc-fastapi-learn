from collections import defaultdict
from datetime import date, datetime
from typing import Any, Literal

from sqlalchemy import and_, case, false, func, or_, select, tuple_
from sqlalchemy.orm import Session

from cc_fastapi.core.repository_values import normalize_repository_tags
from cc_fastapi.db.models import (
    AgentTask,
    Repository,
    ReviewIssue,
    ReviewIssueBatch,
    ReviewIssueVerificationStatus,
    TaskStatus,
)


ReviewDashboardOutcome = Literal["all", "accepted", "unhandled", "pending"]


class ReviewDashboardFilterError(Exception):
    pass


class ReviewDashboardService:
    @staticmethod
    def _batch_filters(
        *,
        provider: str | None,
        project_path: str | None,
        created_from: datetime | None,
        created_to: datetime | None,
    ) -> list[Any]:
        filters: list[Any] = []
        if provider:
            filters.append(ReviewIssueBatch.provider == provider.strip())
        if project_path:
            filters.append(ReviewIssueBatch.project_path == project_path.strip())
        if created_from is not None:
            filters.append(ReviewIssueBatch.created_at >= created_from)
        if created_to is not None:
            filters.append(ReviewIssueBatch.created_at <= created_to)
        return filters

    @staticmethod
    def _tag_filter(db: Session, tag: str | None):
        if tag is None:
            return None
        try:
            normalized_tag = normalize_repository_tags([tag])[0]
        except ValueError as exc:
            raise ReviewDashboardFilterError(str(exc)) from exc
        repository_keys = [
            (repository.provider, repository.project_path)
            for repository in db.scalars(select(Repository))
            if normalized_tag in repository.tags
        ]
        if not repository_keys:
            return false()
        return tuple_(
            ReviewIssueBatch.provider,
            ReviewIssueBatch.project_path,
        ).in_(repository_keys)

    @staticmethod
    def _tags(db: Session) -> list[str]:
        return sorted(
            {
                tag
                for repository in db.scalars(select(Repository))
                for tag in repository.tags
            }
        )

    @staticmethod
    def _pull_request_statistics(filters: list[Any]):
        accepted = case(
            (
                ReviewIssue.verification_status
                == ReviewIssueVerificationStatus.ACCEPTED,
                1,
            ),
            else_=0,
        )
        merged_unhandled = case(
            (
                and_(
                    ReviewIssueBatch.merged_sha.is_not(None),
                    ReviewIssue.verification_status
                    == ReviewIssueVerificationStatus.NOT_ACCEPTED,
                ),
                1,
            ),
            else_=0,
        )
        pending = case(
            (
                ReviewIssue.verification_status
                == ReviewIssueVerificationStatus.UNVERIFIED,
                1,
            ),
            else_=0,
        )
        return (
            select(
                ReviewIssueBatch.provider.label("provider"),
                ReviewIssueBatch.project_path.label("project_path"),
                ReviewIssueBatch.pr_number.label("pr_number"),
                func.count(func.distinct(ReviewIssueBatch.id)).label("batch_total"),
                func.count(ReviewIssue.id).label("issue_total"),
                func.sum(accepted).label("accepted_issues"),
                func.sum(merged_unhandled).label("merged_unhandled_issues"),
                func.sum(pending).label("pending_issues"),
                func.max(ReviewIssueBatch.created_at).label("latest_activity_at"),
            )
            .select_from(ReviewIssueBatch)
            .outerjoin(ReviewIssue, ReviewIssue.batch_id == ReviewIssueBatch.id)
            .where(*filters)
            .group_by(
                ReviewIssueBatch.provider,
                ReviewIssueBatch.project_path,
                ReviewIssueBatch.pr_number,
            )
        )

    @staticmethod
    def _outcome_filter(statistics, outcome: ReviewDashboardOutcome):
        if outcome == "accepted":
            return statistics.c.accepted_issues > 0
        if outcome == "unhandled":
            return statistics.c.merged_unhandled_issues > 0
        if outcome == "pending":
            return statistics.c.pending_issues > 0
        return None

    @staticmethod
    def _task_records(
        db: Session,
        batches: list[ReviewIssueBatch],
    ) -> list[dict[str, Any]]:
        references: list[tuple[ReviewIssueBatch, str, str]] = []
        for batch in batches:
            for role, task_id in (
                ("review", batch.review_task_id),
                ("extract", batch.extract_task_id),
                ("verify", batch.verify_task_id),
            ):
                if task_id:
                    references.append((batch, role, task_id))
        if not references:
            return []
        task_ids = {task_id for _, _, task_id in references}
        tasks = {
            task.id: task
            for task in db.scalars(select(AgentTask).where(AgentTask.id.in_(task_ids)))
        }
        role_order = {"review": 0, "extract": 1, "verify": 2}
        records = [
            {
                "id": task.id,
                "batch_id": batch.id,
                "role": role,
                "status": task.status,
                "session_id": task.session_id,
                "created_at": task.created_at,
                "started_at": task.started_at,
                "finished_at": task.finished_at,
                "error_message": task.error_message,
                "_batch_created_at": batch.created_at,
            }
            for batch, role, task_id in references
            if (task := tasks.get(task_id)) is not None
        ]
        records.sort(
            key=lambda item: (
                item["_batch_created_at"],
                -role_order[item["role"]],
            ),
            reverse=True,
        )
        for record in records:
            record.pop("_batch_created_at")
        return records

    @staticmethod
    def _status_counts(tasks: list[dict[str, Any]]) -> dict[TaskStatus, int]:
        counts = {status: 0 for status in TaskStatus}
        for task in tasks:
            counts[task["status"]] += 1
        return counts

    def _repositories(self, db: Session) -> list[dict[str, Any]]:
        per_pull_request = self._pull_request_statistics([]).subquery()
        rows = db.execute(
            select(
                per_pull_request.c.provider,
                per_pull_request.c.project_path,
                func.count().label("pull_request_total"),
                func.sum(per_pull_request.c.issue_total).label("issue_total"),
            )
            .group_by(per_pull_request.c.provider, per_pull_request.c.project_path)
            .order_by(per_pull_request.c.project_path.asc())
        ).all()
        return [
            {
                "provider": row.provider,
                "project_path": row.project_path,
                "pull_request_total": int(row.pull_request_total or 0),
                "issue_total": int(row.issue_total or 0),
            }
            for row in rows
        ]

    @staticmethod
    def _summary(db: Session, statistics) -> dict[str, Any]:
        row = db.execute(
            select(
                func.count().label("pull_request_total"),
                func.sum(statistics.c.batch_total).label("batch_total"),
                func.sum(statistics.c.issue_total).label("issue_total"),
                func.sum(statistics.c.accepted_issues).label("accepted_issues"),
                func.sum(statistics.c.merged_unhandled_issues).label(
                    "merged_unhandled_issues"
                ),
                func.sum(statistics.c.pending_issues).label("pending_issues"),
            ).select_from(statistics)
        ).one()
        accepted = int(row.accepted_issues or 0)
        merged_unhandled = int(row.merged_unhandled_issues or 0)
        verified = accepted + merged_unhandled
        return {
            "pull_request_total": int(row.pull_request_total or 0),
            "batch_total": int(row.batch_total or 0),
            "issue_total": int(row.issue_total or 0),
            "accepted_issues": accepted,
            "merged_unhandled_issues": merged_unhandled,
            "pending_issues": int(row.pending_issues or 0),
            "acceptance_rate": accepted / verified if verified else None,
        }

    @staticmethod
    def _timeline(db: Session, filters: list[Any]) -> list[dict[str, Any]]:
        day = func.date(ReviewIssueBatch.created_at).label("date")
        accepted = func.sum(
            case(
                (
                    ReviewIssue.verification_status
                    == ReviewIssueVerificationStatus.ACCEPTED,
                    1,
                ),
                else_=0,
            )
        ).label("accepted_issues")
        merged_unhandled = func.sum(
            case(
                (
                    and_(
                        ReviewIssueBatch.merged_sha.is_not(None),
                        ReviewIssue.verification_status
                        == ReviewIssueVerificationStatus.NOT_ACCEPTED,
                    ),
                    1,
                ),
                else_=0,
            )
        ).label("merged_unhandled_issues")
        pending = func.sum(
            case(
                (
                    ReviewIssue.verification_status
                    == ReviewIssueVerificationStatus.UNVERIFIED,
                    1,
                ),
                else_=0,
            )
        ).label("pending_issues")
        rows = db.execute(
            select(
                day,
                func.count(ReviewIssue.id).label("issue_total"),
                accepted,
                merged_unhandled,
                pending,
            )
            .select_from(ReviewIssueBatch)
            .join(ReviewIssue, ReviewIssue.batch_id == ReviewIssueBatch.id)
            .where(*filters)
            .group_by(day)
            .order_by(day.asc())
        ).all()
        return [
            {
                "date": value if isinstance(value, date) else date.fromisoformat(str(value)),
                "issue_total": int(issue_total or 0),
                "accepted_issues": int(accepted_issues or 0),
                "merged_unhandled_issues": int(merged_unhandled_issues or 0),
                "pending_issues": int(pending_issues or 0),
            }
            for value, issue_total, accepted_issues, merged_unhandled_issues, pending_issues in rows
        ]

    def dashboard(
        self,
        db: Session,
        *,
        provider: str | None,
        project_path: str | None,
        tag: str | None,
        created_from: datetime | None,
        created_to: datetime | None,
        outcome: ReviewDashboardOutcome,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        filters = self._batch_filters(
            provider=provider,
            project_path=project_path,
            created_from=created_from,
            created_to=created_to,
        )
        tag_filter = self._tag_filter(db, tag)
        if tag_filter is not None:
            filters.append(tag_filter)
        statistics = self._pull_request_statistics(filters).subquery()
        outcome_filter = self._outcome_filter(statistics, outcome)
        item_query = select(statistics)
        if outcome_filter is not None:
            item_query = item_query.where(outcome_filter)
        total = db.scalar(
            select(func.count()).select_from(item_query.subquery())
        ) or 0
        rows = db.execute(
            item_query.order_by(
                statistics.c.merged_unhandled_issues.desc(),
                statistics.c.pending_issues.desc(),
                statistics.c.latest_activity_at.desc(),
            )
            .offset(offset)
            .limit(limit)
        ).all()

        row_keys = [
            (row.provider, row.project_path, row.pr_number)
            for row in rows
        ]
        batch_groups: dict[tuple[str, str, str], list[ReviewIssueBatch]] = defaultdict(list)
        if row_keys:
            key_filter = or_(
                *[
                    and_(
                        ReviewIssueBatch.provider == provider_value,
                        ReviewIssueBatch.project_path == project_value,
                        ReviewIssueBatch.pr_number == pr_value,
                    )
                    for provider_value, project_value, pr_value in row_keys
                ]
            )
            page_batches = list(
                db.scalars(
                    select(ReviewIssueBatch)
                    .where(key_filter, *filters)
                    .order_by(ReviewIssueBatch.created_at.desc(), ReviewIssueBatch.id.desc())
                )
            )
            all_batches = list(
                db.scalars(
                    select(ReviewIssueBatch)
                    .where(key_filter)
                    .order_by(ReviewIssueBatch.created_at.desc(), ReviewIssueBatch.id.desc())
                )
            )
            for batch in page_batches:
                batch_groups[(batch.provider, batch.project_path, batch.pr_number)].append(batch)
        else:
            all_batches = []

        batch_key_by_id = {
            batch.id: (batch.provider, batch.project_path, batch.pr_number)
            for batch in all_batches
        }
        task_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for task in self._task_records(db, all_batches):
            task_groups[batch_key_by_id[task["batch_id"]]].append(task)

        items = []
        for row in rows:
            key = (row.provider, row.project_path, row.pr_number)
            latest = batch_groups[key][0]
            tasks = task_groups[key]
            items.append(
                {
                    "provider": row.provider,
                    "project_path": row.project_path,
                    "pr_number": row.pr_number,
                    "pr_url": latest.pr_url,
                    "latest_batch_id": latest.id,
                    "latest_batch_status": latest.status,
                    "batch_total": int(row.batch_total or 0),
                    "issue_total": int(row.issue_total or 0),
                    "accepted_issues": int(row.accepted_issues or 0),
                    "merged_unhandled_issues": int(row.merged_unhandled_issues or 0),
                    "pending_issues": int(row.pending_issues or 0),
                    "latest_activity_at": row.latest_activity_at,
                    "task_total": len(tasks),
                    "task_status_counts": self._status_counts(tasks),
                }
            )

        return {
            "summary": self._summary(db, statistics),
            "timeline": self._timeline(db, filters),
            "repositories": self._repositories(db),
            "tags": self._tags(db),
            "items": items,
            "total": int(total),
        }

    def pull_request_detail(
        self,
        db: Session,
        *,
        provider: str,
        project_path: str,
        pr_number: str,
    ) -> dict[str, Any] | None:
        filters = self._batch_filters(
            provider=provider,
            project_path=project_path,
            created_from=None,
            created_to=None,
        )
        filters.append(ReviewIssueBatch.pr_number == pr_number.strip())
        batches = list(
            db.scalars(
                select(ReviewIssueBatch)
                .where(*filters)
                .order_by(ReviewIssueBatch.created_at.desc(), ReviewIssueBatch.id.desc())
            )
        )
        if not batches:
            return None
        statistics = self._pull_request_statistics(filters).subquery()
        row = db.execute(select(statistics)).one()
        tasks = self._task_records(db, batches)
        latest = batches[0]
        return {
            "pull_request": {
                "provider": row.provider,
                "project_path": row.project_path,
                "pr_number": row.pr_number,
                "pr_url": latest.pr_url,
                "latest_batch_id": latest.id,
                "latest_batch_status": latest.status,
                "batch_total": int(row.batch_total or 0),
                "issue_total": int(row.issue_total or 0),
                "accepted_issues": int(row.accepted_issues or 0),
                "merged_unhandled_issues": int(row.merged_unhandled_issues or 0),
                "pending_issues": int(row.pending_issues or 0),
                "latest_activity_at": row.latest_activity_at,
                "task_total": len(tasks),
                "task_status_counts": self._status_counts(tasks),
            },
            "batches": batches,
            "tasks": tasks,
        }
