"""Pinned local PostgreSQL source and disposable-clone mechanics for rehearsals.

Admission acceptance and the coordinator rehearsal both verify a clean source
checkout, pin source and database migration identities, capture the shared
database read-only, and restore it only into a disposable clone.  Those safety
rules used to live as private helpers in one scenario and leak into the other.

This module owns that local seam.  It never runs a domain operation and never
authorizes shared-state mutation; each scenario retains its own assertions and
receipt.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess

from sqlalchemy.engine import make_url

from corridor.m8_acceptance_database import (
    read_migration_head,
    require_local_postgres_host,
)


_DATABASE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


@dataclass(frozen=True)
class SealedRehearsalEnvironment:
    """One verified checkout/database pair and its safe clone operations."""

    source_database_url: str
    checkout: dict[str, str]
    checkout_migration_head: str
    database_migration_head: str
    source_database: dict[str, str]
    repo_root: Path
    compose_root: Path

    @classmethod
    def open(
        cls,
        *,
        source_database_url: str,
        expected_checkout_revision: str,
        repo_root: Path,
        compose_root: Path | None = None,
        expected_database_migration_head: str | None = None,
    ) -> "SealedRehearsalEnvironment":
        """Read-verify source code and database identities before capture."""
        repo_root = Path(repo_root).resolve()
        compose_root = (
            Path(compose_root).resolve() if compose_root is not None else repo_root
        )
        revision = _git(repo_root, "rev-parse", "HEAD")
        if _git(repo_root, "status", "--porcelain"):
            raise ValueError("SH 99 rehearsal requires a clean source checkout")
        if revision != expected_checkout_revision:
            raise ValueError("checkout revision does not match the caller-provided pin")
        checkout_head = _source_migration_head(repo_root)
        database = _source_database(source_database_url)
        database_head = read_migration_head(
            source_database_url,
            repo_root=repo_root,
            error_cls=ValueError,
        )
        expected_head = expected_database_migration_head or checkout_head
        if database_head != expected_head:
            raise ValueError(
                "shared database migration head does not match the expected source head"
            )
        return cls(
            source_database_url=source_database_url,
            checkout={
                "expected_checkout_revision": expected_checkout_revision,
                "revision": revision,
            },
            checkout_migration_head=checkout_head,
            database_migration_head=database_head,
            source_database=database,
            repo_root=repo_root,
            compose_root=compose_root,
        )

    def capture(self, dump_path: Path) -> None:
        """Capture PostgreSQL data from the pinned local source, read-only."""
        dump_path = Path(dump_path)
        argv, environment = self._client(
            "pg_dump",
            (
                "--format=custom",
                "--data-only",
                "--no-owner",
                "--no-privileges",
                # Two rows every migrated database writes for itself, so a
                # clone already holds its own and a copy collides with it.
                # The partition seal (#531) is a per-database key the schema
                # generates and only ever derives a session value from — no
                # stored row is sealed with it — so the clone keeping its own
                # is the correct restore, not a lost one.
                "--exclude-table-data=alembic_version",
                "--exclude-table-data=project_partition_secrets",
            ),
            self.source_database["database"],
        )
        with dump_path.open("wb") as output:
            completed = subprocess.run(
                argv,
                cwd=self.compose_root,
                env=environment,
                stdout=output,
                stderr=subprocess.PIPE,
                text=False,
                check=False,
            )
        if completed.returncode or not dump_path.stat().st_size:
            detail = completed.stderr.decode(errors="replace").strip().splitlines()
            raise ValueError(
                "could not capture the shared database"
                + (f": {detail[-1]}" if detail else "")
            )

    def restore(self, dump_path: Path, database_name: str) -> None:
        """Restore captured data only into an already-migrated disposable clone."""
        if not _DATABASE_NAME.fullmatch(database_name):
            raise ValueError("disposable database name is invalid")
        argv, environment = self._client(
            "pg_restore",
            (
                "--data-only",
                "--disable-triggers",
                "--exit-on-error",
                "--no-owner",
                "--no-privileges",
            ),
            database_name,
        )
        with Path(dump_path).open("rb") as source:
            completed = subprocess.run(
                argv,
                cwd=self.compose_root,
                env=environment,
                stdin=source,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        if completed.returncode:
            detail = completed.stderr.decode(errors="replace").strip().splitlines()
            raise ValueError(
                "could not restore the pinned SH 99 source snapshot"
                + (f": {detail[-1]}" if detail else "")
            )

    def _client(
        self, program: str, arguments: tuple[str, ...], database: str
    ) -> tuple[list[str], dict[str, str] | None]:
        """Reach the configured PostgreSQL, through Compose only when it runs it.

        Both routes address the same pinned server; only the client binary
        differs.  A rehearsal used to be able to reach it one way, so the one
        test that executed this path skipped wherever Compose was absent —
        which includes CI, where PostgreSQL comes from the runner image
        instead (#639, #595).  Neither route is an opt-out: when the server
        cannot be reached the rehearsal fails, as it always did.
        """

        if _compose_service_is_running(self.compose_root):
            return (
                [
                    "docker",
                    "compose",
                    "exec",
                    "-T",
                    "postgres",
                    program,
                    *arguments,
                    "--username",
                    self.source_database["username"],
                    "--dbname",
                    database,
                ],
                None,
            )
        url = make_url(self.source_database_url)
        argv = [
            program,
            *arguments,
            "--host",
            str(url.host or "localhost"),
            "--port",
            str(url.port or 5432),
            "--username",
            self.source_database["username"],
            "--dbname",
            database,
        ]
        environment = dict(os.environ)
        if url.password:
            environment["PGPASSWORD"] = str(url.password)
        return argv, environment

    @staticmethod
    def clone_url(admin_url: str, database_name: str) -> str:
        if not _DATABASE_NAME.fullmatch(database_name):
            raise ValueError("disposable database name is invalid")
        return make_url(admin_url).set(database=database_name).render_as_string(
            hide_password=False
        )


def _compose_service_is_running(compose_root: Path) -> bool:
    """Whether this checkout's own Compose stack is serving PostgreSQL here."""

    try:
        completed = subprocess.run(
            ["docker", "compose", "ps", "--status", "running", "--quiet", "postgres"],
            cwd=compose_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    return completed.returncode == 0 and bool(completed.stdout.strip())


def _git(repo_root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _source_migration_head(repo_root: Path) -> str:
    completed = subprocess.run(
        ["uv", "run", "alembic", "heads"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    heads = [
        match.group(1)
        for line in completed.stdout.splitlines()
        if (match := re.match(r"^([0-9a-f]+) \(head\)$", line.strip()))
    ]
    if completed.returncode or len(heads) != 1:
        raise ValueError("checked-out source must declare exactly one Alembic head")
    return heads[0]


def _source_database(url: str) -> dict[str, str]:
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql":
        raise ValueError("SH 99 rehearsal requires PostgreSQL")
    require_local_postgres_host(parsed.host, error_cls=ValueError)
    if not parsed.database or not _DATABASE_NAME.fullmatch(parsed.database):
        raise ValueError("source database name is invalid")
    if not parsed.username or not _DATABASE_NAME.fullmatch(parsed.username):
        raise ValueError("source database username is invalid")
    return {"database": parsed.database, "username": parsed.username}
