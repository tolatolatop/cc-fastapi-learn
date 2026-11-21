from sqlalchemy import Engine, inspect
from sqlalchemy.exc import DBAPIError

from review_console.models import ConsoleUser


def _has_column(engine: Engine, table_name: str, column_name: str) -> bool:
    return any(
        column["name"] == column_name
        for column in inspect(engine).get_columns(table_name)
    )


def _has_table(engine: Engine, table_name: str) -> bool:
    return inspect(engine).has_table(table_name)


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


def apply_schema_migrations(engine: Engine) -> None:
    """Apply additive schema changes needed by databases created by older releases."""
    if not _has_table(engine, ConsoleUser.__tablename__):
        return
    _add_column(
        engine,
        table_name=ConsoleUser.__tablename__,
        model_column=ConsoleUser.__table__.c.all_repositories_read,
        definition_suffix=" NOT NULL DEFAULT false",
    )
