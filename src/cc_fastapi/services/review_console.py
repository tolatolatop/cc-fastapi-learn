from datetime import datetime

from sqlalchemy import and_, case, false, func, or_, select
from sqlalchemy.orm import Session

from cc_fastapi.db.models import (
    Repository,
    ReviewBatchStatus,
    ReviewIssue,
    ReviewIssueBatch,
    ReviewIssueDecisionReason,
    ReviewIssueDecisionStatus,
    ReviewIssueSeverity,
    ReviewIssueStatusChange,
    utc_now,
)


class ReviewConsoleNotFoundError(Exception):
    pass


class ReviewConsoleConflictError(Exception):
    pass


class ReviewConsoleService:
    @staticmethod
    def list_repositories(db: Session) -> list[dict]:
        rows = db.execute(
            select(
                ReviewIssueBatch.provider,
                ReviewIssueBatch.project_path,
                func.count(ReviewIssue.id),
                func.sum(
                    case(
                        (
                            ReviewIssue.decision_status.in_(
                                (
                                    ReviewIssueDecisionStatus.UNVERIFIED,
                                    ReviewIssueDecisionStatus.NEEDS_INFO,
                                )
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
            )
            .join(ReviewIssue, ReviewIssue.batch_id == ReviewIssueBatch.id)
            .group_by(ReviewIssueBatch.provider, ReviewIssueBatch.project_path)
            .order_by(ReviewIssueBatch.provider, ReviewIssueBatch.project_path)
        ).all()
        totals = {
            (provider, project_path): (issue_total, int(pending_total or 0))
            for provider, project_path, issue_total, pending_total in rows
        }
        keys = set(totals)
        keys.update(
            db.execute(select(Repository.provider, Repository.project_path)).all()
        )
        return [
            {
                "provider": provider,
                "project_path": project_path,
                "issue_total": totals.get((provider, project_path), (0, 0))[0],
                "pending_total": totals.get((provider, project_path), (0, 0))[1],
            }
            for provider, project_path in sorted(keys)
        ]

    @staticmethod
    def list_issues(
        db: Session,
        *,
        provider: str,
        project_path: str,
        pr_number: str | None,
        statuses: list[ReviewIssueDecisionStatus] | None,
        severities: list[ReviewIssueSeverity] | None,
        query: str | None,
        offset: int,
        limit: int,
    ) -> tuple[list[tuple[ReviewIssue, ReviewIssueBatch]], int]:
        filters = [
            ReviewIssueBatch.provider == provider,
            ReviewIssueBatch.project_path == project_path,
        ]
        if pr_number:
            filters.append(ReviewIssueBatch.pr_number == pr_number)
        if statuses:
            filters.append(ReviewIssue.decision_status.in_(statuses))
        if severities:
            filters.append(ReviewIssue.severity.in_(severities))
        if query and query.strip():
            pattern = f"%{query.strip()}%"
            filters.append(
                ReviewIssue.title.ilike(pattern)
                | ReviewIssue.description.ilike(pattern)
                | ReviewIssue.file_path.ilike(pattern)
            )
        total = (
            db.scalar(
                select(func.count())
                .select_from(ReviewIssue)
                .join(ReviewIssueBatch, ReviewIssueBatch.id == ReviewIssue.batch_id)
                .where(*filters)
            )
            or 0
        )
        rows = list(
            db.execute(
                select(ReviewIssue, ReviewIssueBatch)
                .join(ReviewIssueBatch, ReviewIssueBatch.id == ReviewIssue.batch_id)
                .where(*filters)
                .order_by(ReviewIssue.updated_at.desc(), ReviewIssue.id.desc())
                .offset(offset)
                .limit(limit)
            ).all()
        )
        return rows, total

    @staticmethod
    def list_pull_requests(
        db: Session,
        *,
        provider: str,
        project_path: str,
        completion_statuses: list[str] | None,
        offset: int,
        limit: int,
    ) -> tuple[list[dict], int]:
        batches = list(
            db.scalars(
                select(ReviewIssueBatch)
                .where(
                    ReviewIssueBatch.provider == provider,
                    ReviewIssueBatch.project_path == project_path,
                )
                .order_by(
                    ReviewIssueBatch.updated_at.desc(), ReviewIssueBatch.id.desc()
                )
            )
        )
        grouped: dict[str, list[ReviewIssueBatch]] = {}
        for batch in batches:
            grouped.setdefault(batch.pr_number, []).append(batch)
        count_rows = db.execute(
            select(
                ReviewIssueBatch.pr_number,
                func.count(ReviewIssue.id),
                func.sum(
                    case(
                        (
                            ReviewIssue.decision_status.in_(
                                (
                                    ReviewIssueDecisionStatus.UNVERIFIED,
                                    ReviewIssueDecisionStatus.NEEDS_INFO,
                                )
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
            )
            .outerjoin(ReviewIssue, ReviewIssue.batch_id == ReviewIssueBatch.id)
            .where(
                ReviewIssueBatch.provider == provider,
                ReviewIssueBatch.project_path == project_path,
            )
            .group_by(ReviewIssueBatch.pr_number)
        ).all()
        counts = {
            pr_number: (int(issue_total), int(pending_total or 0))
            for pr_number, issue_total, pending_total in count_rows
        }
        records = [
            ReviewConsoleService._pull_request_record(
                db,
                pr_batches,
                issue_total=counts[pr_number][0],
                pending_total=counts[pr_number][1],
            )
            for pr_number, pr_batches in grouped.items()
        ]
        if completion_statuses:
            allowed = set(completion_statuses)
            records = [item for item in records if item["completion_status"] in allowed]
        records.sort(
            key=lambda item: (item["updated_at"], item["pr_number"]), reverse=True
        )
        return records[offset : offset + limit], len(records)

    @staticmethod
    def get_pull_request(
        db: Session, *, provider: str, project_path: str, pr_number: str
    ) -> dict:
        batches = list(
            db.scalars(
                select(ReviewIssueBatch)
                .where(
                    ReviewIssueBatch.provider == provider,
                    ReviewIssueBatch.project_path == project_path,
                    ReviewIssueBatch.pr_number == pr_number,
                )
                .order_by(
                    ReviewIssueBatch.updated_at.desc(), ReviewIssueBatch.id.desc()
                )
            )
        )
        if not batches:
            raise ReviewConsoleNotFoundError("review pull request not found")
        return ReviewConsoleService._pull_request_record(db, batches)

    @staticmethod
    def _pull_request_record(
        db: Session,
        batches: list[ReviewIssueBatch],
        *,
        issue_total: int | None = None,
        pending_total: int | None = None,
    ) -> dict:
        if issue_total is None or pending_total is None:
            batch_ids = [batch.id for batch in batches]
            issue_total = (
                db.scalar(
                    select(func.count())
                    .select_from(ReviewIssue)
                    .where(ReviewIssue.batch_id.in_(batch_ids))
                )
                or 0
            )
            pending_total = (
                db.scalar(
                    select(func.count())
                    .select_from(ReviewIssue)
                    .where(
                        ReviewIssue.batch_id.in_(batch_ids),
                        ReviewIssue.decision_status.in_(
                            (
                                ReviewIssueDecisionStatus.UNVERIFIED,
                                ReviewIssueDecisionStatus.NEEDS_INFO,
                            )
                        ),
                    )
                )
                or 0
            )
        latest = batches[0]
        if latest.status == ReviewBatchStatus.COLLECTING:
            completion_status = "processing"
        elif pending_total:
            completion_status = "pending"
        elif issue_total:
            completion_status = "completed"
        elif latest.status in {
            ReviewBatchStatus.WAITING_MERGE,
            ReviewBatchStatus.VERIFYING,
        }:
            completion_status = "processing"
        elif all(batch.status == ReviewBatchStatus.FAILED for batch in batches):
            completion_status = "failed"
        else:
            completion_status = "no_issues"
        return {
            "provider": latest.provider,
            "project_path": latest.project_path,
            "pr_number": latest.pr_number,
            "pr_url": next((batch.pr_url for batch in batches if batch.pr_url), None),
            "completion_status": completion_status,
            "batch_total": len(batches),
            "issue_total": issue_total,
            "reviewed_total": issue_total - pending_total,
            "pending_total": pending_total,
            "updated_at": max(batch.updated_at for batch in batches),
        }

    @staticmethod
    def get_issue(db: Session, issue_id: str) -> tuple[ReviewIssue, ReviewIssueBatch]:
        row = db.execute(
            select(ReviewIssue, ReviewIssueBatch)
            .join(ReviewIssueBatch, ReviewIssueBatch.id == ReviewIssue.batch_id)
            .where(ReviewIssue.id == issue_id)
        ).one_or_none()
        if row is None:
            raise ReviewConsoleNotFoundError("review issue not found")
        return row

    def update_status(
        self,
        db: Session,
        *,
        issue_id: str,
        new_status: ReviewIssueDecisionStatus,
        reason_code: ReviewIssueDecisionReason | None,
        note: str | None,
        expected_updated_at: datetime,
        actor_id: str,
        actor_name: str,
    ) -> tuple[ReviewIssue, ReviewIssueBatch]:
        if new_status == ReviewIssueDecisionStatus.NOT_ACCEPTED:
            if reason_code is None or not note:
                raise ValueError("rejection category and detail are required")
        elif reason_code is not None:
            raise ValueError("reason code is only valid for rejected decisions")
        if new_status == ReviewIssueDecisionStatus.NEEDS_INFO and not note:
            raise ValueError("note is required when more information is needed")
        row = db.execute(
            select(ReviewIssue, ReviewIssueBatch)
            .join(ReviewIssueBatch, ReviewIssueBatch.id == ReviewIssue.batch_id)
            .where(ReviewIssue.id == issue_id)
            .with_for_update()
        ).one_or_none()
        if row is None:
            raise ReviewConsoleNotFoundError("review issue not found")
        issue, batch = row
        reason_value = reason_code.value if reason_code is not None else None
        if (
            issue.decision_status == new_status
            and issue.decision_reason_code == reason_value
            and issue.decision_note == note
        ):
            return issue, batch
        actual = issue.updated_at
        if actual.tzinfo is None and expected_updated_at.tzinfo is not None:
            expected_updated_at = expected_updated_at.replace(tzinfo=None)
        if actual != expected_updated_at:
            raise ReviewConsoleConflictError(
                "review issue was changed; refresh before saving"
            )
        now = utc_now()
        change = ReviewIssueStatusChange(
            issue_id=issue.id,
            previous_status=issue.decision_status,
            new_status=new_status,
            previous_note=issue.decision_note,
            new_note=note,
            previous_reason_code=issue.decision_reason_code,
            new_reason_code=reason_value,
            actor_id=actor_id,
            actor_name=actor_name,
            dimension="decision",
            source="review_console",
            created_at=now,
        )
        issue.decision_status = new_status
        issue.decision_reason_code = reason_value
        issue.decision_note = note
        issue.decided_by_id = (
            None if new_status == ReviewIssueDecisionStatus.UNVERIFIED else actor_id
        )
        issue.decided_by_name = (
            None if new_status == ReviewIssueDecisionStatus.UNVERIFIED else actor_name
        )
        issue.decided_at = (
            None if new_status == ReviewIssueDecisionStatus.UNVERIFIED else now
        )
        issue.updated_at = now
        db.add(change)
        db.commit()
        db.refresh(issue)
        return issue, batch

    @staticmethod
    def list_history(db: Session, issue_id: str) -> list[ReviewIssueStatusChange]:
        if db.get(ReviewIssue, issue_id) is None:
            raise ReviewConsoleNotFoundError("review issue not found")
        return list(
            db.scalars(
                select(ReviewIssueStatusChange)
                .where(ReviewIssueStatusChange.issue_id == issue_id)
                .order_by(ReviewIssueStatusChange.created_at.desc())
            )
        )

    @staticmethod
    def statistics(
        db: Session,
        *,
        created_from: datetime | None,
        created_to: datetime | None,
        repositories: list[tuple[str, str]] | None,
    ) -> dict:
        filters = []
        if repositories is not None:
            if not repositories:
                filters.append(false())
            else:
                filters.append(
                    or_(
                        *(
                            and_(
                                ReviewIssueBatch.provider == provider,
                                ReviewIssueBatch.project_path == project_path,
                            )
                            for provider, project_path in repositories
                        )
                    )
                )
        if created_from is not None:
            filters.append(ReviewIssue.decided_at >= created_from)
        if created_to is not None:
            filters.append(ReviewIssue.decided_at <= created_to)

        terminal_statuses = (
            ReviewIssueDecisionStatus.ACCEPTED,
            ReviewIssueDecisionStatus.NOT_ACCEPTED,
        )
        summary_row = db.execute(
            select(
                func.sum(
                    case(
                        (ReviewIssue.decision_status == ReviewIssueDecisionStatus.ACCEPTED, 1),
                        else_=0,
                    )
                ),
                func.sum(
                    case(
                        (
                            and_(
                                ReviewIssue.decision_status.in_(terminal_statuses),
                                ReviewIssue.decided_by_id.is_not(None),
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
                func.sum(
                    case(
                        (
                            and_(
                                ReviewIssue.decision_status
                                == ReviewIssueDecisionStatus.NOT_ACCEPTED,
                                ReviewIssue.decision_reason_code
                                == ReviewIssueDecisionReason.FALSE_POSITIVE.value,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ),
            )
            .select_from(ReviewIssue)
            .join(ReviewIssueBatch, ReviewIssueBatch.id == ReviewIssue.batch_id)
            .where(*filters)
        ).one()
        valid_total, confirmed_total, false_positive_total = (
            int(value or 0) for value in summary_row
        )

        contributor_rows = db.execute(
            select(
                ReviewIssue.decided_by_id,
                ReviewIssue.decided_by_name,
                func.count(ReviewIssue.id),
                func.sum(
                    case(
                        (ReviewIssue.decision_status == ReviewIssueDecisionStatus.ACCEPTED, 1),
                        else_=0,
                    )
                ),
                func.sum(
                    case(
                        (
                            ReviewIssue.decision_status
                            == ReviewIssueDecisionStatus.NOT_ACCEPTED,
                            1,
                        ),
                        else_=0,
                    )
                ),
            )
            .join(ReviewIssueBatch, ReviewIssueBatch.id == ReviewIssue.batch_id)
            .where(
                *filters,
                ReviewIssue.decision_status.in_(terminal_statuses),
                ReviewIssue.decided_by_id.is_not(None),
            )
            .group_by(ReviewIssue.decided_by_id, ReviewIssue.decided_by_name)
            .order_by(func.count(ReviewIssue.id).desc(), ReviewIssue.decided_by_name)
        ).all()

        false_positive_case = case(
            (
                and_(
                    ReviewIssue.decision_status
                    == ReviewIssueDecisionStatus.NOT_ACCEPTED,
                    ReviewIssue.decision_reason_code
                    == ReviewIssueDecisionReason.FALSE_POSITIVE.value,
                ),
                1,
            ),
            else_=0,
        )
        rejected_case = case(
            (ReviewIssue.decision_status == ReviewIssueDecisionStatus.NOT_ACCEPTED, 1),
            else_=0,
        )
        decided_case = case(
            (ReviewIssue.decision_status.in_(terminal_statuses), 1), else_=0
        )
        repository_rows = db.execute(
            select(
                ReviewIssueBatch.provider,
                ReviewIssueBatch.project_path,
                func.sum(false_positive_case).label("false_positive_total"),
                func.sum(rejected_case).label("rejected_total"),
                func.sum(decided_case).label("decided_total"),
            )
            .join(ReviewIssue, ReviewIssue.batch_id == ReviewIssueBatch.id)
            .where(*filters)
            .group_by(ReviewIssueBatch.provider, ReviewIssueBatch.project_path)
            .having(func.sum(false_positive_case) > 0)
            .order_by(
                func.sum(false_positive_case).desc(),
                ReviewIssueBatch.provider,
                ReviewIssueBatch.project_path,
            )
            .limit(5)
        ).all()
        return {
            "created_from": created_from,
            "created_to": created_to,
            "summary": {
                "valid_opinion_total": valid_total,
                "confirmed_total": confirmed_total,
                "false_positive_total": false_positive_total,
            },
            "contributors": [
                {
                    "actor_id": actor_id,
                    "actor_name": actor_name or actor_id,
                    "confirmed_total": count,
                    "accepted_total": int(accepted or 0),
                    "not_accepted_total": int(not_accepted or 0),
                }
                for actor_id, actor_name, count, accepted, not_accepted in contributor_rows
            ],
            "top_false_positive_repositories": [
                {
                    "provider": provider,
                    "project_path": project_path,
                    "false_positive_total": int(false_positive_count or 0),
                    "rejected_total": int(rejected_count or 0),
                    "decided_total": int(decided_count or 0),
                    "false_positive_rate": round(
                        int(false_positive_count or 0) / int(decided_count or 1), 4
                    ),
                }
                for (
                    provider,
                    project_path,
                    false_positive_count,
                    rejected_count,
                    decided_count,
                ) in repository_rows
            ],
        }
