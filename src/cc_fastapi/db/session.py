from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cc_fastapi.core.config import get_settings


settings = get_settings()
resolved_database_url = settings.resolved_database_url

connect_args = {}
if resolved_database_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(resolved_database_url, future=True, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

