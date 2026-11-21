from datetime import datetime, timezone

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


PENDING_DECISION_STATUSES = (
    ReviewIssueDecisionStatus.UNVERIFIED,
    ReviewIssueDecisionStatus.NEEDS_INFO,
)


def _normalized_moment(value: datetime) -> datetime:
    """Normalize timestamps so mixed naive/aware values stay comparable."""
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _batch_order_key(
    batch: ReviewIssueBatch,
) -> tuple[datetime, datetime, str]:
    """Order batches by reviewed code version, then by collection time and id.

    ``head_committed_at`` is the commit time of the reviewed code version, so a
    replayed or backfilled older report cannot outrank a newer version. Batches
    without that value fall back to their collection time.
    """
    version_moment = _normalized_moment(batch.head_committed_at or batch.created_at)
    return (version_moment, _normalized_moment(batch.created_at), batch.id)


def _pending_total_expression():
    return func.sum(
        case(
            (ReviewIssue.decision_status.in_(PENDING_DECISION_STATUSES), 1),
            else_=0,
        )
    )


class ReviewConsoleService:
    @staticmethod
    def _latest_batch_ids(
        db: Session,
        *,
        provider: str | None = None,
        project_path: str | None = None,
    ) -> list[str]:
        """Return the newest batch id of every PR/MR in scope."""
        statement = select(
            ReviewIssueBatch.id,
            ReviewIssueBatch.provider,
            ReviewIssueBatch.project_path,
            ReviewIssueBatch.pr_number,
            ReviewIssueBatch.created_at,
            ReviewIssueBatch.head_committed_at,
        )
        if provider is not None:
            statement = statement.where(ReviewIssueBatch.provider == provider)
        if project_path is not None:
            statement = statement.where(ReviewIssueBatch.project_path == project_path)
        latest: dict[tuple[str, str, str], tuple[tuple[datetime, datetime, str], str]] = {}
        for (
            batch_id,
            row_provider,
            row_path,
            pr_number,
            created_at,
            head_committed_at,
        ) in db.execute(statement):
            key = (row_provider, row_path, pr_number)
            order_key = (
                _normalized_moment(head_committed_at or created_at),
                _normalized_moment(created_at),
                batch_id,
            )
            current = latest.get(key)
            if current is None or order_key > current[0]:
                latest[key] = (order_key, batch_id)
        return [batch_id for _, batch_id in latest.values()]

    @staticmethod
    def _latest_batch(
        db: Session, *, provider: str, project_path: str, pr_number: str
    ) -> ReviewIssueBatch | None:
        return db.scalars(
            select(ReviewIssueBatch)
            .where(
                ReviewIssueBatch.provider == provider,
                ReviewIssueBatch.project_path == project_path,
                ReviewIssueBatch.pr_number == pr_number,
            )
            .order_by(
                func.coalesce(
                    ReviewIssueBatch.head_committed_at, ReviewIssueBatch.created_at
                ).desc(),
                ReviewIssueBatch.created_at.desc(),
                ReviewIssueBatch.id.desc(),
            )
            .limit(1)
        ).one_or_none()

    @staticmethod
    def list_repositories(
        db: Session, *, latest_only: bool = True
    ) -> list[dict]:
        statement = (
            select(
                ReviewIssueBatch.provider,
                ReviewIssueBatch.project_path,
                func.count(ReviewIssue.id),
                _pending_total_expression(),
            )
            .join(ReviewIssue, ReviewIssue.batch_id == ReviewIssueBatch.id)
            .group_by(ReviewIssueBatch.provider, ReviewIssueBatch.project_path)
            .order_by(ReviewIssueBatch.provider, ReviewIssueBatch.project_path)
        )
        if latest_only:
            statement = statement.where(
                ReviewIssueBatch.id.in_(ReviewConsoleService._latest_batch_ids(db))
            )
        rows = db.execute(statement).all()
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
        latest_only: bool = True,
        offset: int,
        limit: int,
    ) -> tuple[list[tuple[ReviewIssue, ReviewIssueBatch]], int]:
        filters = [
            ReviewIssueBatch.provider == provider,
            ReviewIssueBatch.project_path == project_path,
        ]
        if pr_number:
            filters.append(ReviewIssueBatch.pr_number == pr_number)
        if latest_only:
            if pr_number:
                latest_batch = ReviewConsoleService._latest_batch(
                    db,
                    provider=provider,
                    project_path=project_path,
                    pr_number=pr_number,
                )
                if latest_batch is None:
                    return [], 0
                filters.append(ReviewIssue.batch_id == latest_batch.id)
            else:
                filters.append(
                    ReviewIssue.batch_id.in_(
                        ReviewConsoleService._latest_batch_ids(
                            db, provider=provider, project_path=project_path
                        )
                    )
                )
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
        latest_only: bool = True,
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
        latest_ids = [
            max(pr_batches, key=_batch_order_key).id
            for pr_batches in grouped.values()
        ]
        count_statement = (
            select(
                ReviewIssueBatch.pr_number,
                func.count(ReviewIssue.id),
                _pending_total_expression(),
            )
            .outerjoin(ReviewIssue, ReviewIssue.batch_id == ReviewIssueBatch.id)
            .where(
                ReviewIssueBatch.provider == provider,
                ReviewIssueBatch.project_path == project_path,
            )
            .group_by(ReviewIssueBatch.pr_number)
        )
        history_counts: dict[str, int] = {}
        if latest_only:
            count_statement = count_statement.where(
                ReviewIssueBatch.id.in_(latest_ids)
            )
            history_counts = ReviewConsoleService._history_issue_counts(
                db, provider=provider, project_path=project_path, latest_ids=latest_ids
            )
        count_rows = db.execute(count_statement).all()
        counts = {
            pr_number: (int(issue_total), int(pending_total or 0))
            for pr_number, issue_total, pending_total in count_rows
        }
        records = [
            ReviewConsoleService._pull_request_record(
                db,
                pr_batches,
                latest_only=latest_only,
                issue_total=counts.get(pr_number, (0, 0))[0],
                pending_total=counts.get(pr_number, (0, 0))[1],
                history_issue_total=history_counts.get(pr_number, 0),
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
    def _history_issue_counts(
        db: Session,
        *,
        provider: str,
        project_path: str,
        latest_ids: list[str],
    ) -> dict[str, int]:
        statement = (
            select(ReviewIssueBatch.pr_number, func.count(ReviewIssue.id))
            .join(ReviewIssue, ReviewIssue.batch_id == ReviewIssueBatch.id)
            .where(
                ReviewIssueBatch.provider == provider,
                ReviewIssueBatch.project_path == project_path,
            )
            .group_by(ReviewIssueBatch.pr_number)
        )
        if latest_ids:
            statement = statement.where(ReviewIssueBatch.id.not_in(latest_ids))
        return {
            pr_number: int(issue_total)
            for pr_number, issue_total in db.execute(statement).all()
        }

    @staticmethod
    def get_pull_request(
        db: Session,
        *,
        provider: str,
        project_path: str,
        pr_number: str,
        latest_only: bool = True,
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
        return ReviewConsoleService._pull_request_record(
            db, batches, latest_only=latest_only
        )

    @staticmethod
    def _pull_request_record(
        db: Session,
        batches: list[ReviewIssueBatch],
        *,
        latest_only: bool = True,
        issue_total: int | None = None,
        pending_total: int | None = None,
        history_issue_total: int | None = None,
    ) -> dict:
        ordered = sorted(batches, key=_batch_order_key, reverse=True)
        latest = ordered[0]
        scope = [latest] if latest_only else ordered
        scope_ids = [batch.id for batch in scope]
        if issue_total is None or pending_total is None:
            issue_total = (
                db.scalar(
                    select(func.count())
                    .select_from(ReviewIssue)
                    .where(ReviewIssue.batch_id.in_(scope_ids))
                )
                or 0
            )
            pending_total = (
                db.scalar(
                    select(func.count())
                    .select_from(ReviewIssue)
                    .where(
                        ReviewIssue.batch_id.in_(scope_ids),
                        ReviewIssue.decision_status.in_(PENDING_DECISION_STATUSES),
                    )
                )
                or 0
            )
        if history_issue_total is None:
            if latest_only:
                history_ids = [batch.id for batch in ordered if batch.id != latest.id]
                history_issue_total = (
                    db.scalar(
                        select(func.count())
                        .select_from(ReviewIssue)
                        .where(ReviewIssue.batch_id.in_(history_ids))
                    )
                    or 0
                )
            else:
                history_issue_total = 0
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
        elif all(batch.status == ReviewBatchStatus.FAILED for batch in scope):
            completion_status = "failed"
        else:
            completion_status = "no_issues"
        return {
            "provider": latest.provider,
            "project_path": latest.project_path,
            "pr_number": latest.pr_number,
            "pr_url": next((batch.pr_url for batch in ordered if batch.pr_url), None),
            "completion_status": completion_status,
            "batch_total": len(ordered),
            "latest_batch_id": latest.id,
            "latest_batch_created_at": latest.created_at,
            "latest_review_head_sha": latest.review_head_sha,
            "latest_head_committed_at": latest.head_committed_at,
            "history_issue_total": history_issue_total,
            "issue_total": issue_total,
            "reviewed_total": issue_total - pending_total,
            "pending_total": pending_total,
            "updated_at": max(batch.updated_at for batch in ordered),
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
    def _decision_filters(
        *,
        created_from: datetime | None,
        created_to: datetime | None,
        repositories: list[tuple[str, str]] | None,
    ) -> list:
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
        return filters

    @staticmethod
    def review_records(
        db: Session,
        *,
        created_from: datetime | None,
        created_to: datetime | None,
        repositories: list[tuple[str, str]] | None,
        limit: int = 5000,
    ) -> list[tuple[ReviewIssue, ReviewIssueBatch]]:
        rows = db.execute(
            select(ReviewIssue, ReviewIssueBatch)
            .join(ReviewIssueBatch, ReviewIssueBatch.id == ReviewIssue.batch_id)
            .where(
                *ReviewConsoleService._decision_filters(
                    created_from=created_from,
                    created_to=created_to,
                    repositories=repositories,
                ),
                ReviewIssue.decided_at.is_not(None),
                ReviewIssue.decided_by_id.is_not(None),
                ReviewIssue.decision_status.in_(
                    (
                        ReviewIssueDecisionStatus.ACCEPTED,
                        ReviewIssueDecisionStatus.NOT_ACCEPTED,
                    )
                ),
            )
            .order_by(ReviewIssue.decided_at.desc(), ReviewIssue.id)
            .limit(limit)
        ).all()
        return list(rows)

    @staticmethod
    def statistics(
        db: Session,
        *,
        created_from: datetime | None,
        created_to: datetime | None,
        repositories: list[tuple[str, str]] | None,
    ) -> dict:
        filters = ReviewConsoleService._decision_filters(
            created_from=created_from,
            created_to=created_to,
            repositories=repositories,
        )

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
