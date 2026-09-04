"""Compose one database URL for one task role, then exec the real command.

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
import sys
from urllib.parse import quote

# Shipped in the image; see the Dockerfile.
RDS_CA_BUNDLE = "/opt/corridor/rds-global-bundle.pem"

# role -> (login source, password variable, URL variable)
ROLES: dict[str, tuple[str | None, str, str]] = {
    # The schema owner. Its login is supplied by the RDS-managed secret rather
    # than fixed here, because the master username is chosen at instance
    # creation.
    "migration": (None, "CORRIDOR_DB_ADMIN_PASSWORD", "DATABASE_URL"),
    "web": ("corridor_web", "CORRIDOR_WEB_DB_PASSWORD", "WEB_DATABASE_URL"),
    "batch": ("corridor_worker", "CORRIDOR_WORKER_DB_PASSWORD", "WORKER_DATABASE_URL"),
}

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

    # quote() with an empty safe set: every reserved character is escaped, so a
    # password containing @ : / ? # cannot redirect the connection.
    userinfo = f"{quote(login, safe='')}:{quote(password, safe='')}"
    url = f"postgresql+psycopg://{userinfo}@{host}:{port}/{quote(name, safe='')}"

    # Defaults to verify-full and is only ever lowered by setting the variable
    # explicitly, which exists so a smoke test can run against a plain
    # PostgreSQL container that serves no certificate. The CDK never sets it,
    # and a test asserts that, so a deployed task always verifies.
    sslmode = env.get("CORRIDOR_DB_SSLMODE") or "verify-full"
    if sslmode in {"verify-full", "verify-ca"}:
        return f"{url}?sslmode={sslmode}&sslrootcert={RDS_CA_BUNDLE}"
    return f"{url}?sslmode={sslmode}"


def prepare_environment(env: dict[str, str], role: str) -> dict[str, str]:
    """Return the environment the real command should run with.

    Exactly one URL variable is set, and the raw passwords this role no longer
    needs are dropped, so a crash dump or a subprocess that inherits the
    environment carries fewer secrets than it otherwise would. The render
    subprocess in particular inherits whatever is left.
    """

    prepared = dict(env)
    _, _, url_var = ROLES[role]
    prepared[url_var] = compose_url(env, role)

    keep = KEEP_FOR_MIGRATION if role == "migration" else frozenset()
    for _, password_var, _ in ROLES.values():
        if password_var in prepared and password_var not in keep:
            del prepared[password_var]
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
