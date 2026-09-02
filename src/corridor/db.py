"""The production PostgreSQL adapters and their session factories.

Domain modules accept a Session supplied by their caller.  Only command and
HTTP adapters reach this module to choose a configured production engine;
tests bind the same interface to rollback-scoped real PostgreSQL connections.

Three bindings, because write authority is a database boundary rather than a
convention (#492).  `Session` carries the migration credential that owns the
schema: migrations, the test harness, and local tooling use it, and no
application process may.  `WebSession` and `WorkerSession` carry the web and
worker capabilities, whose logins cannot write accepted authority, cannot
write the frozen legacy accepted tables, and hold execute on only their own
command family.
"""

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from corridor.config import settings


def capability_url(configured: str, login: str, password: str) -> str:
    """One capability's URL: configured outright, or derived from the schema URL.

    Deriving keeps a local clone bootable without configuring three URLs,
    while a deployment sets each one explicitly.
    """

    if configured:
        return configured
    return (
        make_url(settings.database_url)
        .set(username=login, password=password)
        .render_as_string(hide_password=False)
    )


WEB_DATABASE_URL = capability_url(
    settings.web_database_url, "corridor_web", settings.web_db_password
)
WORKER_DATABASE_URL = capability_url(
    settings.worker_database_url, "corridor_worker", settings.worker_db_password
)

# The schema owner. Migrations, the test harness, and local tooling only.
engine = create_engine(settings.database_url)
Session = sessionmaker(bind=engine)

web_engine = create_engine(WEB_DATABASE_URL)
WebSession = sessionmaker(bind=web_engine)

worker_engine = create_engine(WORKER_DATABASE_URL)
WorkerSession = sessionmaker(bind=worker_engine)
