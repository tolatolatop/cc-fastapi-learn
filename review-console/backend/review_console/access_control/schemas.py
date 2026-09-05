from pydantic import BaseModel, Field, field_validator, model_validator

from review_console.models import RepositoryPermission


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=10, max_length=256)
    is_admin: bool = False


class UserUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=128)
    password: str | None = Field(default=None, min_length=10, max_length=256)
    is_admin: bool | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def require_value(self) -> "UserUpdateRequest":
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        return self


class GrantRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=32)
    project_path: str = Field(min_length=1, max_length=255)
    permission: RepositoryPermission

    @field_validator("provider", "project_path")
    @classmethod
    def normalize_scope(cls, value: str) -> str:
        return value.strip().lower().strip("/")
