from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cc_fastapi.core.repository_values import (
    MAX_REPOSITORY_TAGS,
    normalize_repository_project_path,
    normalize_repository_provider,
    normalize_repository_tags,
)


class RepositoryCreateRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=32)
    project_path: str = Field(min_length=1, max_length=255)
    tags: list[str] = Field(default_factory=list, max_length=MAX_REPOSITORY_TAGS)

    @field_validator("provider")
    @classmethod
    def normalize_provider(cls, value: str) -> str:
        return normalize_repository_provider(value)

    @field_validator("project_path")
    @classmethod
    def normalize_project_path(cls, value: str) -> str:
        return normalize_repository_project_path(value)

    @field_validator("tags")
    @classmethod
    def normalize_tag_list(cls, values: list[str]) -> list[str]:
        return normalize_repository_tags(values)


class RepositoryUpdateRequest(BaseModel):
    provider: str | None = Field(default=None, min_length=1, max_length=32)
    project_path: str | None = Field(default=None, min_length=1, max_length=255)
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
            if getattr(self, field_name) is None:
                raise ValueError(f"{field_name} must not be null")
        return self


class RepositoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    provider: str
    project_path: str
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
