"""Pinned local PostgreSQL source and disposable-clone rehearsals.

Admission acceptance and the coordinator rehearsal both verify a clean source
checkout, pin source and database migration identities, capture the shared
database read-only, and restore it only into a disposable clone.  Those safety
rules used to live as private helpers in one scenario and leak into the other.

The seam then stopped one layer too low.  Four harnesses each wrote the loop
*above* it — capture, provision, refuse a migration-head mismatch, restore,
check the clone against the source, run, read the state again — with four
refusal messages for the one mismatch, and "observe the checkout" was written
five times.  One engine migration therefore had to edit three harnesses (#748).
``rehearse_on_disposable_clone`` owns that loop now: a harness supplies the
operation, the state reader and its own claim boundary, and receives the
before/after pair.

This module still never runs a domain operation and never authorizes
shared-state mutation.  Each harness keeps its own assertions, receipt and
claim boundary: a Product Test Run, a Rehearsal Input Manifest and an
Extraction Measurement are different things with different claim boundaries,
and nothing here merges them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
import os
from pathlib import Path
import re
import subprocess
from typing import Any

from sqlalchemy.engine import make_url

from corridor import digests
from corridor.m8_acceptance_database import (
    DatabaseProvisioner,
    ProvisionedDatabase,
    provision_disposable_postgres,
    read_migration_head,
    require_local_postgres_host,
)


_DATABASE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

StateReader = Callable[[str], Any]
"""Read one comparable state from a database URL: the harness decides what."""


@dataclass(frozen=True)
class CheckoutObservation:
    """One read of the source checkout HEAD and its worktree cleanliness."""

    revision: str
    clean: bool

    @property
    def status(self) -> str:
        """The word an acceptance receipt records for this observation."""

        return "clean" if self.clean else "dirty"


def read_git_output(repo_root: Path, *args: str) -> str:
    """Run one read-only Git command in the checkout under rehearsal."""

    return _git(Path(repo_root), *args)


def observe_checkout(repo_root: Path) -> CheckoutObservation:
    """Observe the checkout once, for every harness that pins it.

    A harness that also pins ``origin/main`` reads that reference itself with
    ``read_git_output``; fetching is a network act and stays with the caller
    that wants it.
    """

    root = Path(repo_root)
    return CheckoutObservation(
        revision=_git(root, "rev-parse", "HEAD"),
        clean=not _git(root, "status", "--porcelain"),
    )


def disposable_provisioner(
    *,
    repo_root: Path,
    label: str,
    migration_revision: str = "head",
    reuse_migrated_template: bool = True,
) -> DatabaseProvisioner:
    """One rehearsal's own labeled disposable-database namespace.

    Every harness built this partial application itself; the label, the name and
    the refusal belong to ``m8_acceptance_database``, and choosing the namespace
    is the only decision left. Only a keyword that differs from that module's
    own default is passed on, so this wrapper cannot pin a stale default in
    front of it.
    """

    keywords: dict[str, Any] = {"repo_root": repo_root, "label": label}
    if migration_revision != "head":
        keywords["migration_revision"] = migration_revision
    if not reuse_migrated_template:
        keywords["reuse_migrated_template"] = False
    return partial(provision_disposable_postgres, **keywords)


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
        observed = observe_checkout(repo_root)
        revision = observed.revision
        if not observed.clean:
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


@dataclass(frozen=True)
class RehearsalClone:
    """The disposable clone one rehearsal operation is allowed to change."""

    database_url: str
    database_name: str
    read_state: StateReader

    def state(self) -> Any:
        """Read the clone's state with the harness's own reader."""

        return self.read_state(self.database_url)


@dataclass(frozen=True)
class RehearsedClone:
    """One disposable clone of the pinned source, and what one operation did.

    ``claim_boundary`` travels with the pair so a harness publishes the boundary
    its own receipt claims and never another harness's.
    """

    claim_boundary: Mapping[str, bool]
    database_name: str
    database_url: str
    source_dump_sha256: str
    source_state: Any
    before: Any
    after: Any
    result: Any


def rehearse_on_disposable_clone(
    environment: "SealedRehearsalEnvironment",
    *,
    postgres_admin_url: str,
    provision_database: DatabaseProvisioner,
    dump_path: Path,
    read_state: StateReader,
    operation: Callable[[RehearsalClone], Any],
    claim_boundary: Mapping[str, bool],
    require_source: Callable[[Any], None] | None = None,
    require_database: Callable[[ProvisionedDatabase], None] | None = None,
    error_cls: type[Exception] = ValueError,
) -> RehearsedClone:
    """Run one operation on a verified disposable clone of the pinned source.

    The whole loop lives here: read the pinned source state, capture it
    read-only, provision a disposable database, refuse a migration head that is
    not the checkout's, restore, refuse a clone that does not match the source,
    run the operation, and read the clone again.  The migration-head refusal is
    one message; a harness that owes its own exception type passes ``error_cls``.

    ``dump_path`` is captured only when it does not already hold a capture, so a
    rehearsal that needs a second clone restores the *identical* bytes — two
    clones are comparable only when they came from one capture.
    ``require_source`` refuses a source that fails the harness's own pins before
    anything is captured; ``require_database`` proves something further about the
    database this rehearsal was handed before anything is restored into it.
    """

    if not claim_boundary or any(
        not isinstance(value, bool) for value in claim_boundary.values()
    ):
        raise error_cls("a rehearsal must state the claim boundary it runs under")
    dump_path = Path(dump_path)
    source_state = read_state(environment.source_database_url)
    if require_source is not None:
        require_source(source_state)
    if not dump_path.exists():
        environment.capture(dump_path)
    dump_bytes = dump_path.read_bytes()
    if not dump_bytes:
        raise error_cls("the captured source dump is empty")
    with provision_database(postgres_admin_url) as database:
        if require_database is not None:
            require_database(database)
        if database.migration_head != environment.checkout_migration_head:
            raise error_cls(
                "disposable clone was not provisioned at the pinned migration head"
            )
        environment.restore(dump_path, database.name)
        clone = RehearsalClone(
            database_url=environment.clone_url(postgres_admin_url, database.name),
            database_name=database.name,
            read_state=read_state,
        )
        before = clone.state()
        if before != source_state:
            raise error_cls(
                "restored disposable clone does not match the pinned source state"
            )
        result = operation(clone)
        return RehearsedClone(
            claim_boundary=claim_boundary,
            database_name=database.name,
            database_url=clone.database_url,
            source_dump_sha256=digests.sha256_bytes(dump_bytes),
            source_state=source_state,
            before=before,
            after=clone.state(),
            result=result,
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
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise ValueError(f"cannot observe Git checkout: {detail}")
    return completed.stdout.strip()


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
