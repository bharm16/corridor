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
        """Capture PostgreSQL data from the local Compose source, read-only."""
        dump_path = Path(dump_path)
        with dump_path.open("wb") as output:
            completed = subprocess.run(
                [
                    "docker",
                    "compose",
                    "exec",
                    "-T",
                    "postgres",
                    "pg_dump",
                    "--format=custom",
                    "--data-only",
                    "--no-owner",
                    "--no-privileges",
                    "--exclude-table-data=alembic_version",
                    "--username",
                    self.source_database["username"],
                    "--dbname",
                    self.source_database["database"],
                ],
                cwd=self.compose_root,
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
        with Path(dump_path).open("rb") as source:
            completed = subprocess.run(
                [
                    "docker",
                    "compose",
                    "exec",
                    "-T",
                    "postgres",
                    "pg_restore",
                    "--data-only",
                    "--disable-triggers",
                    "--exit-on-error",
                    "--no-owner",
                    "--no-privileges",
                    "--username",
                    self.source_database["username"],
                    "--dbname",
                    database_name,
                ],
                cwd=self.compose_root,
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

    @staticmethod
    def clone_url(admin_url: str, database_name: str) -> str:
        if not _DATABASE_NAME.fullmatch(database_name):
            raise ValueError("disposable database name is invalid")
        return make_url(admin_url).set(database=database_name).render_as_string(
            hide_password=False
        )


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
