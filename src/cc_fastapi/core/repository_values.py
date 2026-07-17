import unicodedata


MAX_REPOSITORY_TAGS = 50
MAX_REPOSITORY_TAG_LENGTH = 64


def _normalize_required(value: str, *, field_name: str, max_length: int) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    if field_name == "project_path":
        normalized = normalized.strip("/")
    if not normalized:
        raise ValueError(f"{field_name} must not be blank")
    if len(normalized) > max_length:
        raise ValueError(f"{field_name} must not exceed {max_length} characters")
    return normalized


def normalize_repository_provider(value: str) -> str:
    return _normalize_required(value, field_name="provider", max_length=32)


def normalize_repository_project_path(value: str) -> str:
    return _normalize_required(value, field_name="project_path", max_length=255)


def normalize_repository_tags(values: list[str]) -> list[str]:
    if len(values) > MAX_REPOSITORY_TAGS:
        raise ValueError(f"tags must contain at most {MAX_REPOSITORY_TAGS} items")
    normalized_tags: list[str] = []
    seen: set[str] = set()
    for value in values:
        tag = unicodedata.normalize("NFKC", value).strip().casefold()
        if not tag:
            raise ValueError("tag must not be blank")
        if len(tag) > MAX_REPOSITORY_TAG_LENGTH:
            raise ValueError(
                f"tag must not exceed {MAX_REPOSITORY_TAG_LENGTH} characters"
            )
        if tag not in seen:
            seen.add(tag)
            normalized_tags.append(tag)
    return normalized_tags


def normalize_repository_search(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()
