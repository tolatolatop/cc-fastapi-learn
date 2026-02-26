from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI

from cc_fastapi.api.tasks import router as tasks_router
from cc_fastapi.core.config import get_settings
from cc_fastapi.db.models import Base
from cc_fastapi.db.session import engine
from cc_fastapi.logging_setup import setup_logging
from cc_fastapi.services.worker import WorkerManager


settings = get_settings()
worker_manager = WorkerManager()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    setup_logging(
        settings.log_level,
        log_dir=settings.log_dir,
        debug_log_enabled=settings.debug_log_enabled,
        debug_log_backup_days=settings.debug_log_backup_days,
        debug_log_filename=settings.debug_log_filename,
        debug_log_utc=settings.debug_log_utc,
    )
    logger.info("application startup begin", extra={"event_type": "app_startup"})
    Base.metadata.create_all(bind=engine)
    recovered = worker_manager.run_startup_recovery()
    logger.info(
        "startup recovery finished",
        extra={"event_type": "startup_recovery", "reason": f"recovered={recovered}"},
    )
    worker_manager.start()
    try:
        yield
    finally:
        abandoned = worker_manager.abandon_running_on_shutdown()
        logger.info(
            "shutdown running-task abandon finished",
            extra={"event_type": "shutdown_abandon", "reason": f"abandoned={abandoned}"},
        )
        worker_manager.stop()
        logger.info("application shutdown end", extra={"event_type": "app_shutdown"})


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.include_router(tasks_router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}

