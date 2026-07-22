from sqlalchemy import Engine, inspect
from sqlalchemy.exc import DBAPIError

from cc_fastapi.db.models import AgentTask, Repository


def _agent_tasks_has_session_id(engine: Engine) -> bool:
    return any(column["name"] == "session_id" for column in inspect(engine).get_columns("agent_tasks"))


def _agent_tasks_has_session_id_index(engine: Engine) -> bool:
    return any(index["name"] == "ix_agent_tasks_session_id" for index in inspect(engine).get_indexes("agent_tasks"))


def _repositories_has_web_url(engine: Engine) -> bool:
    return any(column["name"] == "web_url" for column in inspect(engine).get_columns("repositories"))


def apply_schema_migrations(engine: Engine) -> None:
    """Apply additive schema changes needed by databases created by older releases."""
    inspector = inspect(engine)
    table_names = inspector.get_table_names()

    if "agent_tasks" in table_names and not _agent_tasks_has_session_id(engine):
        preparer = engine.dialect.identifier_preparer
        table_name = preparer.quote("agent_tasks")
        column_name = preparer.quote("session_id")
        column_type = AgentTask.__table__.c.session_id.type.compile(dialect=engine.dialect)
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
                )
        except DBAPIError:
            # Another application instance may have completed the same additive migration.
            if not _agent_tasks_has_session_id(engine):
                raise

    if "agent_tasks" in table_names and not _agent_tasks_has_session_id_index(engine):
        session_index = next(
            index for index in AgentTask.__table__.indexes if index.name == "ix_agent_tasks_session_id"
        )
        try:
            session_index.create(bind=engine, checkfirst=True)
        except DBAPIError:
            if not _agent_tasks_has_session_id_index(engine):
                raise

    if "repositories" in table_names and not _repositories_has_web_url(engine):
        preparer = engine.dialect.identifier_preparer
        table_name = preparer.quote("repositories")
        column_name = preparer.quote("web_url")
        column_type = Repository.__table__.c.web_url.type.compile(dialect=engine.dialect)
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
                )
        except DBAPIError:
            if not _repositories_has_web_url(engine):
                raise
