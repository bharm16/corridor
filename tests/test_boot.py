from sqlalchemy import create_engine, text

from corridor.config import settings


def test_database_is_reachable():
    engine = create_engine(settings.database_url)
    with engine.connect() as conn:
        assert conn.execute(text("select 1")).scalar() == 1


def test_server_is_postgres_16():
    """Guards against connecting to the Homebrew Postgres 14 on this machine.

    Both accept the same credentials on different ports, so a wrong port
    connects successfully and fails much later on version-specific SQL.
    """
    engine = create_engine(settings.database_url)
    with engine.connect() as conn:
        version = conn.execute(text("show server_version")).scalar()
    assert version.startswith("16."), f"expected Postgres 16, got {version}"
