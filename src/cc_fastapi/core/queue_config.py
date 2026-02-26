from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from cc_fastapi.core.config import get_settings


class QueueDefinition(BaseModel):
    workers: int = Field(default=1, ge=1)


class QueueConfig(BaseModel):
    default_queue: str = "default"
    queues: dict[str, QueueDefinition] = Field(default_factory=lambda: {"default": QueueDefinition(workers=1)})


def _load_from_file(path: Path) -> QueueConfig:
    if not path.exists():
        return QueueConfig()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return QueueConfig.model_validate(data)


@lru_cache(maxsize=1)
def get_queue_config() -> QueueConfig:
    settings = get_settings()
    cfg_path = Path(settings.queues_config_path)
    if not cfg_path.is_absolute():
        cfg_path = Path.cwd() / cfg_path
    return _load_from_file(cfg_path)

