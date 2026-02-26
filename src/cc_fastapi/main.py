from contextlib import asynccontextmanager

from fastapi import FastAPI

from cc_fastapi.api.tasks import router as tasks_router
from cc_fastapi.core.config import get_settings
from cc_fastapi.db.models import Base
from cc_fastapi.db.session import engine
from cc_fastapi.logging_setup import setup_logging
from cc_fastapi.services.worker import WorkerManager


settings = get_settings()
worker_manager = WorkerManager()


@asynccontextmanager
async def lifespan(_: FastAPI):
    setup_logging(settings.log_level)
    Base.metadata.create_all(bind=engine)
    worker_manager.run_startup_recovery()
    worker_manager.start()
    try:
        yield
    finally:
        worker_manager.abandon_running_on_shutdown()
        worker_manager.stop()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.include_router(tasks_router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}

