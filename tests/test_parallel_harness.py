"""Public test-harness contract for xdist worker database isolation."""

import re

from sqlalchemy import text

from corridor.db import engine


def test_xdist_worker_uses_a_guarded_isolated_database(worker_id):
    with engine.connect() as connection:
        database_name = str(connection.scalar(text("select current_database()")))

    if worker_id == "master":
        assert database_name
        return
    assert re.fullmatch(
        rf"corridor_pytest_[0-9a-f]{{12}}_{re.escape(worker_id)}",
        database_name,
    )
