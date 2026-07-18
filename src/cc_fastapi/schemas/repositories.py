from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cc_fastapi.core.repository_values import (
    MAX_REPOSITORY_TAGS,
    normalize_repository_project_path,
    normalize_repository_provider,
    normalize_repository_tags,
    normalize_repository_web_url,
)


class RepositoryCreateRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=32)
    project_path: str = Field(min_length=1, max_length=255)
    web_url: str | None = Field(default=None, max_length=2048)
    tags: list[str] = Field(default_factory=list, max_length=MAX_REPOSITORY_TAGS)

    @field_validator("provider")
    @classmethod
    def normalize_provider(cls, value: str) -> str:
        return normalize_repository_provider(value)

    @field_validator("project_path")
    @classmethod
    def normalize_project_path(cls, value: str) -> str:
        return normalize_repository_project_path(value)

    @field_validator("web_url")
    @classmethod
    def normalize_web_url(cls, value: str | None) -> str | None:
        return normalize_repository_web_url(value)

    @field_validator("tags")
    @classmethod
    def normalize_tag_list(cls, values: list[str]) -> list[str]:
        return normalize_repository_tags(values)


class RepositoryUpdateRequest(BaseModel):
    provider: str | None = Field(default=None, min_length=1, max_length=32)
    project_path: str | None = Field(default=None, min_length=1, max_length=255)
    web_url: str | None = Field(default=None, max_length=2048)
    tags: list[str] | None = Field(default=None, max_length=MAX_REPOSITORY_TAGS)

    @field_validator("provider")
    @classmethod
    def normalize_provider(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_repository_provider(value)

    @field_validator("project_path")
    @classmethod
    def normalize_project_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_repository_project_path(value)

    @field_validator("web_url")
    @classmethod
    def normalize_web_url(cls, value: str | None) -> str | None:
        return normalize_repository_web_url(value)

    @field_validator("tags")
    @classmethod
    def normalize_tag_list(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        return normalize_repository_tags(values)

    @model_validator(mode="after")
    def require_update_field(self) -> "RepositoryUpdateRequest":
        if not self.model_fields_set:
            raise ValueError("at least one field must be provided")
        for field_name in self.model_fields_set:
            if field_name != "web_url" and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} must not be null")
        return self


class RepositoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    provider: str
    project_path: str
    web_url: str | None
    tags: list[str]
    created_at: datetime
    updated_at: datetime


class RepositoryListSummaryResponse(BaseModel):
    total: int
    providers: list[str]
    tags: list[str]


class RepositoryListResponse(BaseModel):
    items: list[RepositoryResponse]
    total: int
    summary: RepositoryListSummaryResponse


class RepositoryTagsReplaceRequest(BaseModel):
    tags: list[str] = Field(max_length=MAX_REPOSITORY_TAGS)

    @field_validator("tags")
    @classmethod
    def normalize_tag_list(cls, values: list[str]) -> list[str]:
        return normalize_repository_tags(values)


class RepositoryBulkTagsUpdateRequest(BaseModel):
    repository_ids: list[str] = Field(min_length=1, max_length=200)
    add_tags: list[str] = Field(default_factory=list, max_length=MAX_REPOSITORY_TAGS)
    remove_tags: list[str] = Field(default_factory=list, max_length=MAX_REPOSITORY_TAGS)

    @field_validator("repository_ids")
    @classmethod
    def normalize_repository_ids(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            repository_id = value.strip()
            if not repository_id:
                raise ValueError("repository id must not be blank")
            if repository_id not in seen:
                seen.add(repository_id)
                normalized.append(repository_id)
        return normalized

    @field_validator("add_tags", "remove_tags")
    @classmethod
    def normalize_tag_list(cls, values: list[str]) -> list[str]:
        return normalize_repository_tags(values)

    @model_validator(mode="after")
    def validate_operations(self) -> "RepositoryBulkTagsUpdateRequest":
        if not self.add_tags and not self.remove_tags:
            raise ValueError("at least one tag operation must be provided")
        overlap = set(self.add_tags).intersection(self.remove_tags)
        if overlap:
            raise ValueError("the same tag cannot be added and removed")
        return self


class RepositoryBulkTagsUpdateResponse(BaseModel):
    items: list[RepositoryResponse]
    total: int


class RepositoryReviewStatisticsResponse(BaseModel):
    review_total: int
    issue_total: int
    accepted_issues: int
    unhandled_issues: int
    pending_issues: int


class RepositoryOverviewItemResponse(RepositoryResponse):
    review_statistics: RepositoryReviewStatisticsResponse


class RepositoryOverviewSummaryResponse(RepositoryReviewStatisticsResponse):
    repository_total: int
    providers: list[str]
    tags: list[str]


class RepositoryOverviewListResponse(BaseModel):
    items: list[RepositoryOverviewItemResponse]
    total: int
    summary: RepositoryOverviewSummaryResponse
