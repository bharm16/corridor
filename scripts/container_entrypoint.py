"""Compose scoped customer and control-plane URLs, then exec the real command.

The task definition cannot build a database URL. A password exists there only
as a secret reference, and the RDS-managed credential is a JSON document rather
than a URL, so the stack injects connection *parts* and this script assembles
them. It runs as the container's entrypoint and replaces itself with the
command it was given.

Which URL it sets is the whole point. `corridor.config.Settings` reads three
separate ones -- `DATABASE_URL` for the schema owner, `WEB_DATABASE_URL` and
`WORKER_DATABASE_URL` for the two runtime capabilities -- and
`corridor.db.capability_url` derives a capability URL from the schema owner's
only when that capability's own URL is empty. Setting exactly one, and never
`DATABASE_URL` for a runtime role, is what keeps #492's boundary from quietly
degrading into "everything connects as the owner".

Passwords are URL-encoded, because a generated secret may legitimately contain
a character that would otherwise terminate the userinfo component and produce
a URL pointing somewhere else entirely.

Every connection requires TLS against the bundled RDS trust store. `verify-full`
rather than `require`, because `require` encrypts without authenticating the
server and so does not stop an interception.
"""

from __future__ import annotations

import os
import re
import sys
from urllib.parse import quote

# Shipped in the image; see the Dockerfile.
RDS_CA_BUNDLE = "/opt/corridor/rds-global-bundle.pem"

# The two modes that actually authenticate the server. `require` encrypts
# without verifying, which does not stop an interception.
VERIFIED_SSL_MODES = frozenset({"verify-full", "verify-ca"})

# Everything else is a deployed environment and may not lower TLS.
LOCAL_ENVIRONMENTS = frozenset({"development", "test"})

# role -> (login source, password variable, URL variable)
ROLES: dict[str, tuple[str | None, str, str]] = {
    # The schema owner. Its login is supplied by the RDS-managed secret rather
    # than fixed here, because the master username is chosen at instance
    # creation.
    "migration": (None, "CORRIDOR_DB_ADMIN_PASSWORD", "DATABASE_URL"),
    "web": ("corridor_web", "CORRIDOR_WEB_DB_PASSWORD", "WEB_DATABASE_URL"),
    "batch": ("corridor_worker", "CORRIDOR_WORKER_DB_PASSWORD", "WORKER_DATABASE_URL"),
}

CONTROL_ROLES = {
    "owner": "CONTROL_PLANE_DATABASE_URL",
    "operations": "CONTROL_PLANE_OPERATIONS_DATABASE_URL",
    "resolver": "CONTROL_PLANE_RESOLVER_DATABASE_URL",
}
IDENTITY_INPUTS = (
    "CORRIDOR_CUSTOMER_ID", "CORRIDOR_CUSTOMER_ENVIRONMENT_ID", "CORRIDOR_DEPLOYMENT_ID",
)

# The migration creates the two runtime logins and reads their passwords to set
# them, so it is the one role that legitimately holds all three credentials.
KEEP_FOR_MIGRATION = frozenset(
    {"CORRIDOR_WEB_DB_PASSWORD", "CORRIDOR_WORKER_DB_PASSWORD"}
)


class EntrypointError(RuntimeError):
    """A configuration fault the container cannot recover from."""


def _require(env: dict[str, str], name: str) -> str:
    value = env.get(name, "")
    if not value:
        raise EntrypointError(f"{name} is required but was empty or unset")
    return value


def compose_url(env: dict[str, str], role: str) -> str:
    """Build the SQLAlchemy URL this role connects with."""

    if role not in ROLES:
        raise EntrypointError(
            f"CORRIDOR_TASK_ROLE must be one of {sorted(ROLES)}; got {role!r}"
        )
    fixed_login, password_var, _ = ROLES[role]
    login = fixed_login or _require(env, "CORRIDOR_DB_ADMIN_USERNAME")
    password = _require(env, password_var)
    host = _require(env, "CORRIDOR_DB_HOST")
    port = _require(env, "CORRIDOR_DB_PORT")
    name = _require(env, "CORRIDOR_DB_NAME")

    return _database_url(env, login, password, host, port, name)


def compose_control_url(env: dict[str, str], capability: str) -> str:
    """Use only that control-plane login, with the same verified RDS TLS."""

    if capability not in CONTROL_ROLES:
        raise EntrypointError("unknown control-plane capability")
    prefix = f"CORRIDOR_CONTROL_{capability.upper()}_DB"
    return _database_url(
        env, _require(env, f"{prefix}_USERNAME"), _require(env, f"{prefix}_PASSWORD"),
        _require(env, "CORRIDOR_CONTROL_DB_HOST"),
        _require(env, "CORRIDOR_CONTROL_DB_PORT"),
        _require(env, "CORRIDOR_CONTROL_DB_NAME"),
    )


def _database_url(
    env: dict[str, str], login: str, password: str, host: str, port: str, name: str
) -> str:

    # quote() with an empty safe set: every reserved character is escaped, so a
    # password containing @ : / ? # cannot redirect the connection.
    userinfo = f"{quote(login, safe='')}:{quote(password, safe='')}"
    url = f"postgresql+psycopg://{userinfo}@{host}:{port}/{quote(name, safe='')}"

    # Defaults to verify-full and is only ever lowered by setting the variable
    # explicitly, which exists so a smoke test can run against a plain
    # PostgreSQL container that serves no certificate. The CDK never sets it,
    # and a test asserts that.
    #
    # Belt and braces: a deployed environment refuses the override outright, so
    # the guarantee does not rest on the stack alone. CORRIDOR_ENVIRONMENT is
    # set to "nonproduction" by the task definition and defaults to
    # "development" in config.py.
    sslmode = env.get("CORRIDOR_DB_SSLMODE") or "verify-full"
    environment = env.get("CORRIDOR_ENVIRONMENT", "development")
    if sslmode not in VERIFIED_SSL_MODES and environment not in LOCAL_ENVIRONMENTS:
        raise EntrypointError(
            f"CORRIDOR_DB_SSLMODE={sslmode!r} does not verify the server "
            f"certificate, and CORRIDOR_ENVIRONMENT={environment!r} is a "
            "deployed environment. Only development and test may lower it."
        )
    if sslmode in VERIFIED_SSL_MODES:
        return f"{url}?sslmode={sslmode}&sslrootcert={RDS_CA_BUNDLE}"
    return f"{url}?sslmode={sslmode}"


def prepare_environment(env: dict[str, str], role: str) -> dict[str, str]:
    """Return the environment the real command should run with.

    Runtime receives exactly one customer capability and the control-plane
    resolver. Migration may bootstrap both databases. Its explicit operations
    mode receives only the operations URL, for the existing control-plane CLI.
    Raw passwords and inherited URLs for other capabilities are removed.
    """

    prepared = dict(env)
    if role not in ROLES:
        raise EntrypointError(f"CORRIDOR_TASK_ROLE must be one of {sorted(ROLES)}")
    mode = env.get("CORRIDOR_MIGRATION_MODE", "configure")
    if mode not in {"configure", "operations"} or (role != "migration" and mode != "configure"):
        raise EntrypointError("only the migration task may select operations mode")

    # Clear all three first. Setting one without removing the others would let
    # an inherited or injected DATABASE_URL survive into a runtime container,
    # and db.capability_url reads it whenever that capability's own URL is
    # empty -- which would reconnect web or batch as the schema owner.
    for _, _, name in ROLES.values():
        prepared.pop(name, None)
    for name in CONTROL_ROLES.values():
        prepared.pop(name, None)

    _, _, url_var = ROLES[role]
    if mode != "operations":
        prepared[url_var] = compose_url(env, role)

    control_configured = any(
        key.startswith("CORRIDOR_CONTROL_") and value for key, value in env.items()
    ) or any(env.get(key) for key in (*IDENTITY_INPUTS, *CONTROL_ROLES.values()))
    deployed = env.get("CORRIDOR_ENVIRONMENT", "development") not in LOCAL_ENVIRONMENTS
    if control_configured or deployed or mode == "operations":
        for name in IDENTITY_INPUTS:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", _require(env, name)):
                raise EntrypointError(f"{name} must be a stable bounded identifier")
        capabilities = (
            ("operations",) if mode == "operations"
            else tuple(CONTROL_ROLES) if role == "migration"
            else ("resolver",)
        )
        for capability in capabilities:
            prepared[CONTROL_ROLES[capability]] = compose_control_url(env, capability)
        if role == "web" and len(_require(env, "CORRIDOR_CUSTOMER_ROUTING_KEY").encode()) < 32:
            raise EntrypointError("CORRIDOR_CUSTOMER_ROUTING_KEY needs at least 32 bytes")

    keep = KEEP_FOR_MIGRATION if role == "migration" and mode == "configure" else frozenset()
    for _, password_var, _ in ROLES.values():
        if password_var in prepared and password_var not in keep:
            del prepared[password_var]
    for capability in CONTROL_ROLES:
        for suffix in ("USERNAME", "PASSWORD"):
            prepared.pop(f"CORRIDOR_CONTROL_{capability.upper()}_DB_{suffix}", None)
    prepared.pop("CORRIDOR_DB_ADMIN_USERNAME", None)
    if role != "web":
        prepared.pop("CORRIDOR_CUSTOMER_ROUTING_KEY", None)
    prepared["CORRIDOR_TASK_ROLE"] = role
    return prepared


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        raise EntrypointError("no command given to the entrypoint")
    role = os.environ.get("CORRIDOR_TASK_ROLE", "")
    if not role:
        raise EntrypointError("CORRIDOR_TASK_ROLE is required")

    prepared = prepare_environment(dict(os.environ), role)
    command = argv[1:]
    # execvpe, not a shell: the command comes from the task definition as an
    # argument vector and is never re-parsed, so nothing in it can be
    # interpolated or word-split.
    os.execvpe(command[0], command, prepared)


if __name__ == "__main__":  # pragma: no cover - exercised via the image
    try:
        sys.exit(main(sys.argv))
    except EntrypointError as error:
        print(f"corridor-entrypoint: {error}", file=sys.stderr)
        sys.exit(78)  # EX_CONFIG
