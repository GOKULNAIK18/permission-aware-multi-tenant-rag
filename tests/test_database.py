from sqlalchemy import inspect

from app.db.database import engine


def test_database_engine_is_configured() -> None:
    assert engine is not None
    assert str(engine.url).startswith("postgresql")


def test_expected_tables_exist() -> None:
    expected_tables = {
        "tenants",
        "users",
        "roles",
        "user_roles",
        "documents",
        "document_roles",
        "document_users",
        "audit_logs",
    }

    with engine.connect() as connection:
        inspector = inspect(connection)
        actual_tables = set(inspector.get_table_names())

    assert expected_tables.issubset(actual_tables)
