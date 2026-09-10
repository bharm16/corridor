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

Each capability's engine is built on first use rather than at import.  Three
engines at import time made merely *naming* a session factory resolve three
URLs and construct three pools, and pulled the customer router — which reads
deployment configuration — into every command that only wanted a session
factory.  It also fixed each URL at the moment of import, which is the wrong
moment: the test harness rewrites `DATABASE_URL` in `pytest_configure` and
provisions its worker database on the first DBAPI connection, so resolution
must happen when a caller actually asks.  `engine`, `web_engine` and
`worker_engine` remain importable names — a module-level `__getattr__` builds
the one that is asked for — so `from corridor.db import engine` still reads as
it always did while importing the module alone builds nothing.
"""

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import sessionmaker

from corridor.config import settings
from corridor.db_roles import WEB_CAPABILITY_LOGIN, WORKER_CAPABILITY_LOGIN


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


def web_database_url() -> str:
    return capability_url(
        settings.web_database_url, WEB_CAPABILITY_LOGIN, settings.web_db_password
    )


def worker_database_url() -> str:
    return capability_url(
        settings.worker_database_url, WORKER_CAPABILITY_LOGIN, settings.worker_db_password
    )


# One engine and one session factory per capability, built on the first request
# for it. The capability name is the key, so a caller cannot reach an engine
# without naming which authority it is asking for.
_CAPABILITY_URLS = {
    # The schema owner. Migrations, the test harness, and local tooling only.
    "owner": lambda: settings.database_url,
    "web": web_database_url,
    "worker": worker_database_url,
}
_engines: dict[str, Engine] = {}
_factories: dict[str, sessionmaker] = {}


def capability_engine(capability: str) -> Engine:
    """The engine for one capability, constructed once, on first use."""

    engine = _engines.get(capability)
    if engine is None:
        engine = create_engine(_CAPABILITY_URLS[capability]())
        _engines[capability] = engine
    return engine


def capability_session_factory(capability: str) -> sessionmaker:
    """The session factory bound to one capability's engine."""

    factory = _factories.get(capability)
    if factory is None:
        factory = sessionmaker(bind=capability_engine(capability))
        _factories[capability] = factory
    return factory


def engine_state() -> tuple[str, ...]:
    """Which capability engines have actually been built, for guards and teardown."""

    return tuple(sorted(_engines))


def dispose_engines() -> None:
    """Release every pool that was built, leaving the unbuilt ones unbuilt."""

    for engine in _engines.values():
        engine.dispose()


_ENGINE_ATTRIBUTES = {"engine": "owner", "web_engine": "web", "worker_engine": "worker"}
_FACTORY_ATTRIBUTES = {"Session": "owner", "WebSession": "web"}


def __getattr__(name: str):
    """Resolve `engine`/`Session` and their capability siblings on first access."""

    capability = _ENGINE_ATTRIBUTES.get(name)
    if capability is not None:
        return capability_engine(capability)
    capability = _FACTORY_ATTRIBUTES.get(name)
    if capability is not None:
        return capability_session_factory(capability)
    # #492's derived URLs stayed importable for the authority tests that assert
    # which login each capability resolves to.
    if name == "WEB_DATABASE_URL":
        return web_database_url()
    if name == "WORKER_DATABASE_URL":
        return worker_database_url()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def WorkerSession():
    """Every deployed worker session rechecks its configured customer route."""
    from corridor.customer_routing_runtime import configured_customer_router

    router = configured_customer_router()
    if router is None:
        return capability_session_factory("worker")()
    return router.open_session(router.identity, capability="worker")
