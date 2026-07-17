from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from cc_fastapi.api.dependencies import require_token
from cc_fastapi.db.session import get_db
from cc_fastapi.schemas.repositories import (
    RepositoryCreateRequest,
    RepositoryListResponse,
    RepositoryListSummaryResponse,
    RepositoryResponse,
    RepositoryUpdateRequest,
)
from cc_fastapi.services.repositories import (
    RepositoryConflictError,
    RepositoryFilterError,
    RepositoryNotFoundError,
    RepositoryService,
)


router = APIRouter(
    prefix="/v1/repositories",
    tags=["repositories"],
    dependencies=[Depends(require_token)],
)
repositories = RepositoryService()


def _raise_service_error(exc: Exception) -> None:
    if isinstance(exc, RepositoryNotFoundError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if isinstance(exc, RepositoryConflictError):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if isinstance(exc, RepositoryFilterError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    raise exc


@router.post("", response_model=RepositoryResponse, status_code=status.HTTP_201_CREATED)
def create_repository(
    payload: RepositoryCreateRequest,
    db: Session = Depends(get_db),
) -> RepositoryResponse:
    try:
        repository = repositories.create(db, payload.model_dump())
    except RepositoryConflictError as exc:
        _raise_service_error(exc)
        raise AssertionError("unreachable")
    return RepositoryResponse.model_validate(repository)


@router.get("", response_model=RepositoryListResponse)
def list_repositories(
    provider: str | None = Query(default=None, max_length=32),
    search: str | None = Query(default=None, alias="q", max_length=200),
    tags: list[str] | None = Query(default=None, alias="tag"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
) -> RepositoryListResponse:
    try:
        items, total = repositories.list_repositories(
            db,
            provider=provider,
            search=search,
            tags=tags,
            offset=offset,
            limit=limit,
        )
    except RepositoryFilterError as exc:
        _raise_service_error(exc)
        raise AssertionError("unreachable")
    summary_total, providers, available_tags = repositories.summarize(db)
    return RepositoryListResponse(
        items=[RepositoryResponse.model_validate(item) for item in items],
        total=total,
        summary=RepositoryListSummaryResponse(
            total=summary_total,
            providers=providers,
            tags=available_tags,
        ),
    )


@router.get("/{repository_id}", response_model=RepositoryResponse)
def get_repository(
    repository_id: str,
    db: Session = Depends(get_db),
) -> RepositoryResponse:
    repository = repositories.get(db, repository_id)
    if repository is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="repository not found",
        )
    return RepositoryResponse.model_validate(repository)


@router.patch("/{repository_id}", response_model=RepositoryResponse)
def update_repository(
    repository_id: str,
    payload: RepositoryUpdateRequest,
    db: Session = Depends(get_db),
) -> RepositoryResponse:
    try:
        repository = repositories.update(
            db,
            repository_id,
            payload.model_dump(exclude_unset=True),
        )
    except (RepositoryNotFoundError, RepositoryConflictError) as exc:
        _raise_service_error(exc)
        raise AssertionError("unreachable")
    return RepositoryResponse.model_validate(repository)


@router.delete("/{repository_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_repository(
    repository_id: str,
    db: Session = Depends(get_db),
) -> Response:
    try:
        repositories.delete(db, repository_id)
    except RepositoryNotFoundError as exc:
        _raise_service_error(exc)
        raise AssertionError("unreachable")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
