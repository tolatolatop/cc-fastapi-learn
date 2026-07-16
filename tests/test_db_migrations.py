from sqlalchemy import create_engine, inspect

from cc_fastapi.db.migrations import apply_schema_migrations


def test_apply_schema_migrations_adds_session_id_to_legacy_agent_tasks_table():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE agent_tasks (id VARCHAR(36) PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO agent_tasks (id) VALUES ('legacy-task')")

    apply_schema_migrations(engine)
    apply_schema_migrations(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("agent_tasks")}
    indexes = {index["name"] for index in inspect(engine).get_indexes("agent_tasks")}
    assert "session_id" in columns
    assert "ix_agent_tasks_session_id" in indexes
    with engine.connect() as connection:
        row = connection.exec_driver_sql(
            "SELECT id, session_id FROM agent_tasks WHERE id = 'legacy-task'"
        ).one()
    assert row == ("legacy-task", None)
