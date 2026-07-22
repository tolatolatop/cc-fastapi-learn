from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cc_fastapi.core.repository_values import (
    normalize_repository_project_path,
    normalize_repository_provider,
    normalize_repository_search,
    normalize_repository_tags,
)
from cc_fastapi.core.webhook_payloads import WebhookPayload
from cc_fastapi.db.models import (
    Repository,
    ReviewIssue,
    ReviewIssueBatch,
    ReviewIssueVerificationStatus,
    WebhookTrigger,
    utc_now,
)


class RepositoryNotFoundError(Exception):
    pass


class RepositoryConflictError(Exception):
    pass


class RepositoryFilterError(Exception):
    pass


class RepositoryService:
    @staticmethod
    def _normalized_provider_filter(value: str) -> str:
        try:
            return normalize_repository_provider(value)
        except ValueError as exc:
            raise RepositoryFilterError(str(exc)) from exc

    @staticmethod
    def _get_for_update(db: Session, repository_id: str) -> Repository:
        repository = db.scalar(
            select(Repository)
            .where(Repository.id == repository_id)
            .with_for_update()
        )
        if repository is None:
            raise RepositoryNotFoundError("repository not found")
        return repository

    def create(self, db: Session, values: dict[str, Any]) -> Repository:
        repository = Repository(**values)
        db.add(repository)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise RepositoryConflictError(
                "repository already exists for this provider"
            ) from exc
        db.refresh(repository)
        return repository

    @staticmethod
    def _discover_webhook_repositories(
        db: Session,
    ) -> dict[tuple[str, str], str | None]:
        discovered: dict[tuple[str, str], str | None] = {}
        webhook_rows = db.execute(
            select(
                WebhookTrigger.provider,
                WebhookTrigger.event_type,
                WebhookTrigger.payload_json,
            ).order_by(WebhookTrigger.created_at.desc(), WebhookTrigger.id.desc())
        ).tuples()
        for provider, event_type, payload in webhook_rows:
            if not isinstance(payload, dict):
                continue
            parsed_payload = WebhookPayload.from_payload(provider, event_type, payload)
            if parsed_payload is None or parsed_payload.repository is None:
                continue
            repository = parsed_payload.repository
            key = (parsed_payload.provider, repository.project_path)
            if key not in discovered or (
                discovered[key] is None and repository.web_url is not None
            ):
                discovered[key] = repository.web_url
        return discovered

    @staticmethod
    def _discover_review_issue_repositories(
        db: Session,
    ) -> set[tuple[str, str]]:
        discovered: set[tuple[str, str]] = set()
        review_rows = db.execute(
            select(ReviewIssueBatch.provider, ReviewIssueBatch.project_path)
            .join(ReviewIssue, ReviewIssue.batch_id == ReviewIssueBatch.id)
            .distinct()
            .order_by(ReviewIssueBatch.provider, ReviewIssueBatch.project_path)
        ).tuples()
        for provider, project_path in review_rows:
            try:
                key = (
                    normalize_repository_provider(provider),
                    normalize_repository_project_path(project_path),
                )
            except ValueError:
                continue
            discovered.add(key)
        return discovered

    def sync_from_sources(self, db: Session) -> list[Repository]:
        discovered = self._discover_webhook_repositories(db)
        for key in self._discover_review_issue_repositories(db):
            discovered.setdefault(key, None)

        for attempt in range(2):
            existing = set(
                db.execute(
                    select(Repository.provider, Repository.project_path)
                ).tuples()
            )
            created = [
                Repository(
                    provider=key[0],
                    project_path=key[1],
                    web_url=web_url,
                    tags=[],
                )
                for key, web_url in discovered.items()
                if key not in existing
            ]
            if not created:
                return []
            db.add_all(created)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                if attempt == 0:
                    continue
                raise
            for repository in created:
                db.refresh(repository)
            return created
        return []

    @staticmethod
    def get(db: Session, repository_id: str) -> Repository | None:
        return db.get(Repository, repository_id)

    def _filtered_repositories(
        self,
        db: Session,
        *,
        provider: str | None,
        search: str | None,
        tags: list[str] | None,
    ) -> list[Repository]:
        filters = []
        if provider:
            normalized_provider = self._normalized_provider_filter(provider)
            filters.append(Repository.provider == normalized_provider)
        if search:
            normalized_search = normalize_repository_search(search)
            if normalized_search:
                filters.append(
                    or_(
                        Repository.provider.contains(normalized_search, autoescape=True),
                        Repository.project_path.contains(
                            normalized_search, autoescape=True
                        ),
                    )
                )

        candidates = list(
            db.scalars(
                select(Repository)
                .where(*filters)
                .order_by(Repository.updated_at.desc(), Repository.id.desc())
            )
        )
        if tags:
            try:
                normalized_tags = normalize_repository_tags(tags)
            except ValueError as exc:
                raise RepositoryFilterError(str(exc)) from exc
            required_tags = set(normalized_tags)
            candidates = [
                repository
                for repository in candidates
                if required_tags.issubset(set(repository.tags))
            ]
        return candidates

    def list_repositories(
        self,
        db: Session,
        *,
        provider: str | None,
        search: str | None,
        tags: list[str] | None,
        offset: int,
        limit: int,
    ) -> tuple[list[Repository], int]:
        candidates = self._filtered_repositories(
            db,
            provider=provider,
            search=search,
            tags=tags,
        )
        return candidates[offset : offset + limit], len(candidates)

    @staticmethod
    def summarize(db: Session) -> tuple[int, list[str], list[str]]:
        repositories = list(db.scalars(select(Repository)))
        providers = sorted({repository.provider for repository in repositories})
        tags = sorted(
            {tag for repository in repositories for tag in repository.tags}
        )
        return len(repositories), providers, tags

    def update(
        self,
        db: Session,
        repository_id: str,
        values: dict[str, Any],
    ) -> Repository:
        repository = self._get_for_update(db, repository_id)
        for field_name, value in values.items():
            setattr(repository, field_name, value)
        repository.updated_at = utc_now()
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise RepositoryConflictError(
                "repository already exists for this provider"
            ) from exc
        db.refresh(repository)
        return repository

    def replace_tags(
        self,
        db: Session,
        repository_id: str,
        tags: list[str],
    ) -> Repository:
        repository = self._get_for_update(db, repository_id)
        repository.tags = tags
        repository.updated_at = utc_now()
        db.commit()
        db.refresh(repository)
        return repository

    def bulk_update_tags(
        self,
        db: Session,
        repository_ids: list[str],
        *,
        add_tags: list[str],
        remove_tags: list[str],
    ) -> list[Repository]:
        repositories = list(
            db.scalars(
                select(Repository)
                .where(Repository.id.in_(repository_ids))
                .with_for_update()
            )
        )
        by_id = {repository.id: repository for repository in repositories}
        if len(by_id) != len(repository_ids):
            db.rollback()
            raise RepositoryNotFoundError("one or more repositories not found")

        remove_set = set(remove_tags)
        now = utc_now()
        try:
            for repository_id in repository_ids:
                repository = by_id[repository_id]
                next_tags = [tag for tag in repository.tags if tag not in remove_set]
                next_tags.extend(tag for tag in add_tags if tag not in next_tags)
                repository.tags = normalize_repository_tags(next_tags)
                repository.updated_at = now
            db.commit()
        except ValueError as exc:
            db.rollback()
            raise RepositoryFilterError(str(exc)) from exc

        ordered = [by_id[repository_id] for repository_id in repository_ids]
        for repository in ordered:
            db.refresh(repository)
        return ordered

    @staticmethod
    def _review_statistics(
        db: Session,
        repositories: list[Repository],
    ) -> dict[str, dict[str, int]]:
        empty = {
            repository.id: {
                "review_total": 0,
                "issue_total": 0,
                "accepted_issues": 0,
                "unhandled_issues": 0,
                "pending_issues": 0,
            }
            for repository in repositories
        }
        for start in range(0, len(repositories), 400):
            repository_ids = [
                repository.id for repository in repositories[start : start + 400]
            ]
            rows = db.execute(
                select(
                    Repository.id,
                    func.count(func.distinct(ReviewIssueBatch.id)).label("review_total"),
                    func.count(ReviewIssue.id).label("issue_total"),
                    func.sum(
                        case(
                            (
                                ReviewIssue.verification_status
                                == ReviewIssueVerificationStatus.ACCEPTED,
                                1,
                            ),
                            else_=0,
                        )
                    ).label("accepted_issues"),
                    func.sum(
                        case(
                            (
                                ReviewIssue.verification_status
                                == ReviewIssueVerificationStatus.NOT_ACCEPTED,
                                1,
                            ),
                            else_=0,
                        )
                    ).label("unhandled_issues"),
                    func.sum(
                        case(
                            (
                                ReviewIssue.verification_status
                                == ReviewIssueVerificationStatus.UNVERIFIED,
                                1,
                            ),
                            else_=0,
                        )
                    ).label("pending_issues"),
                )
                .select_from(Repository)
                .outerjoin(
                    ReviewIssueBatch,
                    and_(
                        ReviewIssueBatch.provider == Repository.provider,
                        ReviewIssueBatch.project_path == Repository.project_path,
                    ),
                )
                .outerjoin(ReviewIssue, ReviewIssue.batch_id == ReviewIssueBatch.id)
                .where(Repository.id.in_(repository_ids))
                .group_by(Repository.id)
            ).all()
            for row in rows:
                empty[row.id] = {
                    "review_total": int(row.review_total or 0),
                    "issue_total": int(row.issue_total or 0),
                    "accepted_issues": int(row.accepted_issues or 0),
                    "unhandled_issues": int(row.unhandled_issues or 0),
                    "pending_issues": int(row.pending_issues or 0),
                }
        return empty

    def list_overview(
        self,
        db: Session,
        *,
        provider: str | None,
        search: str | None,
        tags: list[str] | None,
        offset: int,
        limit: int,
    ) -> tuple[
        list[Repository],
        int,
        dict[str, dict[str, int]],
        dict[str, Any],
    ]:
        candidates = self._filtered_repositories(
            db,
            provider=provider,
            search=search,
            tags=tags,
        )
        all_statistics = self._review_statistics(db, candidates)
        page = candidates[offset : offset + limit]
        _summary_total, providers, available_tags = self.summarize(db)
        summary = {
            "repository_total": len(candidates),
            "review_total": sum(item["review_total"] for item in all_statistics.values()),
            "issue_total": sum(item["issue_total"] for item in all_statistics.values()),
            "accepted_issues": sum(
                item["accepted_issues"] for item in all_statistics.values()
            ),
            "unhandled_issues": sum(
                item["unhandled_issues"] for item in all_statistics.values()
            ),
            "pending_issues": sum(
                item["pending_issues"] for item in all_statistics.values()
            ),
            "providers": providers,
            "tags": available_tags,
        }
        page_statistics = {
            repository.id: all_statistics[repository.id] for repository in page
        }
        return page, len(candidates), page_statistics, summary

    def delete(self, db: Session, repository_id: str) -> None:
        repository = self._get_for_update(db, repository_id)
        db.delete(repository)
        db.commit()
