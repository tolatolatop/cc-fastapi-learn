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


def test_apply_schema_migrations_adds_web_url_to_legacy_repositories_table():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE repositories (id VARCHAR(36) PRIMARY KEY, provider VARCHAR(32), "
            "project_path VARCHAR(255), tags JSON, created_at DATETIME, updated_at DATETIME)"
        )
        connection.exec_driver_sql(
            "INSERT INTO repositories "
            "(id, provider, project_path, tags, created_at, updated_at) "
            "VALUES ('repo-1', 'gitlab', 'group/project', '[]', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )

    apply_schema_migrations(engine)
    apply_schema_migrations(engine)

    columns = {column["name"] for column in inspect(engine).get_columns("repositories")}
    assert "web_url" in columns
    with engine.connect() as connection:
        row = connection.exec_driver_sql(
            "SELECT id, web_url FROM repositories WHERE id = 'repo-1'"
        ).one()
    assert row == ("repo-1", None)


def test_apply_schema_migrations_adds_human_decision_fields_and_audit_dimension():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE review_issues ("
            "id VARCHAR(36) PRIMARY KEY, verification_status VARCHAR(32) NOT NULL, "
            "verification_note TEXT, verified_at DATETIME)"
        )
        connection.exec_driver_sql(
            "INSERT INTO review_issues "
            "(id, verification_status, verification_note, verified_at) "
            "VALUES ('issue-1', 'ACCEPTED', 'legacy result', CURRENT_TIMESTAMP)"
        )
        connection.exec_driver_sql(
            "CREATE TABLE review_issue_status_changes ("
            "id VARCHAR(36) PRIMARY KEY, source VARCHAR(64) NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO review_issue_status_changes (id, source) "
            "VALUES ('change-1', 'verification_workflow')"
        )

    apply_schema_migrations(engine)
    apply_schema_migrations(engine)

    issue_columns = {
        column["name"] for column in inspect(engine).get_columns("review_issues")
    }
    assert {
        "decision_status",
        "decision_reason_code",
        "decision_note",
        "decided_by_id",
        "decided_by_name",
        "decided_at",
    }.issubset(issue_columns)
    with engine.connect() as connection:
        issue = connection.exec_driver_sql(
            "SELECT decision_status, decision_note, decided_at "
            "FROM review_issues WHERE id = 'issue-1'"
        ).one()
        change = connection.exec_driver_sql(
            "SELECT dimension FROM review_issue_status_changes "
            "WHERE id = 'change-1'"
        ).one()
    assert issue[0:2] == ("ACCEPTED", "legacy result")
    assert issue[2] is not None
    assert change == ("verification",)

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE review_issues SET decision_status = 'UNVERIFIED', "
            "decision_note = NULL WHERE id = 'issue-1'"
        )
    apply_schema_migrations(engine)
    with engine.connect() as connection:
        status_after_restart = connection.exec_driver_sql(
            "SELECT decision_status FROM review_issues WHERE id = 'issue-1'"
        ).scalar_one()
    assert status_after_restart == "UNVERIFIED"
