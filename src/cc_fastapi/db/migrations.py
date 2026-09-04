from sqlalchemy import Engine, inspect
from sqlalchemy.exc import DBAPIError

from cc_fastapi.db.models import (
    AgentTask,
    Repository,
    ReviewIssue,
    ReviewIssueStatusChange,
)


def _has_column(engine: Engine, table_name: str, column_name: str) -> bool:
    return any(
        column["name"] == column_name
        for column in inspect(engine).get_columns(table_name)
    )


def _add_column(
    engine: Engine,
    *,
    table_name: str,
    model_column,
    definition_suffix: str = "",
) -> None:
    column_name = model_column.name
    if _has_column(engine, table_name, column_name):
        return
    preparer = engine.dialect.identifier_preparer
    quoted_table = preparer.quote(table_name)
    quoted_column = preparer.quote(column_name)
    column_type = model_column.type.compile(dialect=engine.dialect)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                f"ALTER TABLE {quoted_table} ADD COLUMN {quoted_column} "
                f"{column_type}{definition_suffix}"
            )
    except DBAPIError:
        if not _has_column(engine, table_name, column_name):
            raise


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

    if "review_issues" in table_names:
        issue_columns = ReviewIssue.__table__.c
        had_decision_status = _has_column(
            engine, "review_issues", "decision_status"
        )
        _add_column(
            engine,
            table_name="review_issues",
            model_column=issue_columns.decision_status,
            definition_suffix=" NOT NULL DEFAULT 'UNVERIFIED'",
        )
        for column_name in (
            "decision_reason_code",
            "decision_note",
            "decided_by_id",
            "decided_by_name",
            "decided_at",
        ):
            _add_column(
                engine,
                table_name="review_issues",
                model_column=issue_columns[column_name],
            )
        if not had_decision_status:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "UPDATE review_issues SET decision_status = verification_status, "
                    "decision_note = verification_note, decided_at = verified_at "
                    "WHERE decision_status = 'UNVERIFIED' "
                    "AND verification_status <> 'UNVERIFIED'"
                )
        decision_index = next(
            index
            for index in ReviewIssue.__table__.indexes
            if index.name == "ix_review_issues_decision_statistics"
        )
        decision_index.create(bind=engine, checkfirst=True)

    if "review_issue_status_changes" in table_names:
        change_columns = ReviewIssueStatusChange.__table__.c
        for column_name in ("previous_reason_code", "new_reason_code"):
            _add_column(
                engine,
                table_name="review_issue_status_changes",
                model_column=change_columns[column_name],
            )
        _add_column(
            engine,
            table_name="review_issue_status_changes",
            model_column=change_columns.dimension,
            definition_suffix=" NOT NULL DEFAULT 'decision'",
        )
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE review_issue_status_changes SET dimension = 'verification' "
                "WHERE source = 'verification_workflow'"
            )
