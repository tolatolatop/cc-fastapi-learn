from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cc_fastapi.core.repository_values import (
    normalize_repository_provider,
    normalize_repository_search,
    normalize_repository_tags,
)
from cc_fastapi.db.models import Repository, utc_now


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
    def get(db: Session, repository_id: str) -> Repository | None:
        return db.get(Repository, repository_id)

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
        total = len(candidates)
        return candidates[offset : offset + limit], total

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

    def delete(self, db: Session, repository_id: str) -> None:
        repository = self._get_for_update(db, repository_id)
        db.delete(repository)
        db.commit()
