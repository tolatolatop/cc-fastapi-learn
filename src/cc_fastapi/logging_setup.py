import json
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from datetime import datetime, timezone
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in (
            "task_id",
            "event_type",
            "worker_id",
            "trace_id",
            "queue_name",
            "duration_ms",
            "status_from",
            "status_to",
            "reason",
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=True)


def setup_logging(
    level: str,
    *,
    log_dir: str = "logs",
    debug_log_enabled: bool = True,
    debug_log_backup_days: int = 14,
    debug_log_filename: str = "debug.log",
    debug_log_utc: bool = True,
) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    if root.handlers:
        for handler in root.handlers:
            handler.setFormatter(JsonFormatter())
        return

    console_handler = logging.StreamHandler()
    console_handler.setLevel(getattr(logging, level.upper(), logging.INFO))
    console_handler.setFormatter(JsonFormatter())
    root.addHandler(console_handler)

    if debug_log_enabled:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        file_handler = TimedRotatingFileHandler(
            filename=str(log_path / debug_log_filename),
            when="midnight",
            backupCount=max(1, debug_log_backup_days),
            utc=debug_log_utc,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)

