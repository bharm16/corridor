"""Exact database baselines for repeatable Product Proving runs.

Product Proving needs to return the shared development database to an exact
starting point between passes.  ORM metadata and a handful of project counts
cannot prove that state: Corridor also has tables outside an individual
project and PostgreSQL sequences whose values affect later writes.

This module therefore fingerprints the live ``public`` schema discovered from
PostgreSQL itself, verifies a data-only dump by restoring it into a freshly
migrated disposable database, and publishes the verified dump once.  Restoring
the shared development database is a separate, explicit operation.  It never
feeds the dump directly to the shared database; it swaps in a fully verified
clone while retaining the old database as a rollback target until the new
source has passed its final fingerprint check.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.pool import NullPool

from corridor.m8_acceptance_database import (
    DatabaseProvisioner,
    ProvisionedDatabase,
    provision_disposable_postgres,
    require_local_postgres_host,
    require_postgres_16,
)
from corridor.m8_acceptance_publication import publish_directory_once
from corridor.rehearsal_environment import SealedRehearsalEnvironment


BASELINE_SCHEMA_VERSION = "corridor.product-proving-database-baseline.v1"
MANIFEST_SCHEMA_VERSION = "corridor.product-proving-database-manifest.v1"
BASELINE_FILENAME = "baseline.json"
DUMP_FILENAME = "baseline.dump"
MANIFEST_FILENAME = "manifest.json"
BASELINE_FILES = (BASELINE_FILENAME, DUMP_FILENAME)
DISPOSABLE_DATABASE_PREFIX = "corridor_proving_restore_"

_DATABASE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_SHARED_DEVELOPMENT_DATABASE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_POSTGRES_MAINTENANCE_DATABASES = frozenset({"postgres", "template0", "template1"})


class ProductProvingDatabaseError(ValueError):
    """A database baseline or guarded restore failed closed."""


@dataclass(frozen=True)
class TableFingerprint:
    """Canonical data identity for one discovered public base table."""

    name: str
    row_count: int
    rows_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "row_count": self.row_count,
            "rows_sha256": self.rows_sha256,
        }


@dataclass(frozen=True)
class SequenceFingerprint:
    """Definition and current value for one discovered public sequence."""

    name: str
    data_type: str
    start_value: str
    minimum_value: str
    maximum_value: str
    increment: str
    cache_size: str
    cycles: bool
    last_value: str
    is_called: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "data_type": self.data_type,
            "start_value": self.start_value,
            "minimum_value": self.minimum_value,
            "maximum_value": self.maximum_value,
            "increment": self.increment,
            "cache_size": self.cache_size,
            "cycles": self.cycles,
            "last_value": self.last_value,
            "is_called": self.is_called,
        }


@dataclass(frozen=True)
class DatabaseFingerprint:
    """Canonical identity of all public base-table data and sequence state."""

    tables: tuple[TableFingerprint, ...]
    sequences: tuple[SequenceFingerprint, ...]
    state_sha256: str

    @property
    def table_count(self) -> int:
        return len(self.tables)

    @property
    def sequence_count(self) -> int:
        return len(self.sequences)

    def as_dict(self) -> dict[str, Any]:
        return {
            "table_count": self.table_count,
            "sequence_count": self.sequence_count,
            "tables": [item.as_dict() for item in self.tables],
            "sequences": [item.as_dict() for item in self.sequences],
            "state_sha256": self.state_sha256,
        }


@dataclass(frozen=True)
class ProductProvingDatabaseBaselineConfig:
    """Caller-held source pins for one immutable baseline capture."""

    source_database_url: str
    postgres_admin_url: str
    expected_checkout_revision: str
    expected_migration_head: str
    output_dir: Path
    repo_root: Path
    compose_root: Path | None = None


@dataclass(frozen=True)
class ProductProvingDatabaseBaselineSummary:
    bundle_dir: Path
    manifest_path: Path
    manifest_sha256: str
    dump_sha256: str
    state_sha256: str
    table_count: int
    sequence_count: int
    verification_database_name: str


@dataclass(frozen=True)
class VerifiedProductProvingDatabaseBaseline:
    bundle_dir: Path
    manifest_sha256: str
    dump_sha256: str
    baseline: Mapping[str, Any]
    fingerprint: DatabaseFingerprint


@dataclass(frozen=True)
class SharedDevelopmentRestoreConfig:
    """Exact pins and opt-in for replacing one local development database."""

    source_database_url: str
    postgres_admin_url: str
    expected_source_database_name: str
    expected_checkout_revision: str
    expected_migration_head: str
    bundle_dir: Path
    expected_manifest_sha256: str
    repo_root: Path
    allow_shared_development_restore: bool = False
    compose_root: Path | None = None


@dataclass(frozen=True)
class DatabaseReplacementRequest:
    """A clone already proved equivalent to the immutable baseline."""

    source_database_url: str
    postgres_admin_url: str
    source_database_name: str
    replacement_database_name: str
    expected_current_fingerprint: DatabaseFingerprint
    expected_replacement_fingerprint: DatabaseFingerprint
    expected_migration_head: str


@dataclass(frozen=True)
class StagedDatabaseReplacement:
    """A verified replacement now named as source, with both databases sealed."""

    request: DatabaseReplacementRequest
    backup_database_name: str
    source_database_oid: int
    replacement_database_oid: int


@dataclass(frozen=True)
class StagedDatabaseValidation:
    """Post-swap state read while every untrusted connection is excluded."""

    fingerprint: DatabaseFingerprint
    migration_head: str
    postgres_version: str


@dataclass(frozen=True)
class DatabaseReplacementReceipt:
    source_database_name: str
    replacement_database_name: str
    backup_database_name: str
    restored_fingerprint: DatabaseFingerprint


@dataclass(frozen=True)
class SharedDevelopmentRestoreSummary:
    source_database_name: str
    previous_state_sha256: str
    restored_state_sha256: str
    manifest_sha256: str
    dump_sha256: str
    verification_database_name: str
    backup_database_name: str


class _EnvironmentOpener(Protocol):
    def __call__(
        self,
        *,
        source_database_url: str,
        expected_checkout_revision: str,
        repo_root: Path,
        compose_root: Path | None = None,
        expected_database_migration_head: str | None = None,
    ) -> SealedRehearsalEnvironment: ...


FingerprintReader = Callable[[str], DatabaseFingerprint]
DatabaseReplacementStager = Callable[
    [DatabaseReplacementRequest], StagedDatabaseReplacement
]
StagedDatabaseValidator = Callable[
    [StagedDatabaseReplacement], StagedDatabaseValidation
]
DatabaseReplacementFinalizer = Callable[
    [StagedDatabaseReplacement], DatabaseReplacementReceipt
]
DatabaseReplacementRollback = Callable[[StagedDatabaseReplacement], None]
DatabaseCatalogReader = Callable[[str, set[str]], dict[str, int]]
DatabaseQuiescer = Callable[[str, str, str], StagedDatabaseValidation]
DatabaseRenamer = Callable[[str, str, str], None]
DatabaseConnectionSetter = Callable[[str, str, bool], None]


def fingerprint_public_database(connection: Connection) -> DatabaseFingerprint:
    """Fingerprint every public base table and sequence in one read-only snapshot."""

    connection.exec_driver_sql(
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    )
    return _fingerprint_public_database_snapshot(connection)


def _fingerprint_public_database_snapshot(
    connection: Connection,
) -> DatabaseFingerprint:
    """Read a snapshot after its caller has made the transaction read-only."""

    table_names = tuple(
        connection.scalars(
            text(
                "select table_name from information_schema.tables "
                "where table_schema = 'public' and table_type = 'BASE TABLE' "
                'order by table_name collate "C"'
            )
        ).all()
    )
    sequence_rows = connection.execute(
        text(
            "select c.relname, format_type(s.seqtypid, null), "
            "s.seqstart::text, s.seqmin::text, s.seqmax::text, "
            "s.seqincrement::text, s.seqcache::text, s.seqcycle "
            "from pg_catalog.pg_class c "
            "join pg_catalog.pg_namespace n on n.oid = c.relnamespace "
            "join pg_catalog.pg_sequence s on s.seqrelid = c.oid "
            "where n.nspname = 'public' and c.relkind = 'S' "
            'order by c.relname collate "C"'
        )
    ).all()

    preparer = connection.dialect.identifier_preparer
    schema = preparer.quote("public")
    tables: list[TableFingerprint] = []
    for table_name in table_names:
        name = str(table_name)
        qualified = f"{schema}.{preparer.quote(name)}"
        rows = connection.exec_driver_sql(
            "select row_json from ("
            f"select to_jsonb(_row)::text as row_json from {qualified} as _row"
            ') as canonical_rows order by row_json collate "C"'
        )
        digest = sha256()
        row_count = 0
        for row_json in rows.scalars():
            encoded = str(row_json).encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            row_count += 1
        tables.append(
            TableFingerprint(
                name=name,
                row_count=row_count,
                rows_sha256=digest.hexdigest(),
            )
        )

    sequences: list[SequenceFingerprint] = []
    for row in sequence_rows:
        name = str(row[0])
        qualified = f"{schema}.{preparer.quote(name)}"
        last_value, is_called = connection.exec_driver_sql(
            f"select last_value::text, is_called from {qualified}"
        ).one()
        sequences.append(
            SequenceFingerprint(
                name=name,
                data_type=str(row[1]),
                start_value=str(row[2]),
                minimum_value=str(row[3]),
                maximum_value=str(row[4]),
                increment=str(row[5]),
                cache_size=str(row[6]),
                cycles=bool(row[7]),
                last_value=str(last_value),
                is_called=bool(is_called),
            )
        )

    canonical = {
        "tables": [item.as_dict() for item in tables],
        "sequences": [item.as_dict() for item in sequences],
    }
    return DatabaseFingerprint(
        tables=tuple(tables),
        sequences=tuple(sequences),
        state_sha256=_json_sha256(canonical),
    )


def fingerprint_database_url(database_url: str) -> DatabaseFingerprint:
    """Open a short-lived connection and capture a read-only canonical snapshot."""

    engine = create_engine(database_url, poolclass=NullPool, future=True)
    try:
        with engine.connect() as connection:
            return fingerprint_public_database(connection)
    finally:
        engine.dispose()


def capture_product_proving_database_baseline(
    config: ProductProvingDatabaseBaselineConfig,
    *,
    provision_database: DatabaseProvisioner | None = None,
    open_environment: _EnvironmentOpener = SealedRehearsalEnvironment.open,
    fingerprint_database: FingerprintReader = fingerprint_database_url,
) -> ProductProvingDatabaseBaselineSummary:
    """Capture, independently restore-verify, and publish one immutable baseline."""

    source_identity = _database_identity(config.source_database_url)
    admin_identity = _database_identity(config.postgres_admin_url)
    _require_local_identity(source_identity)
    _require_local_identity(admin_identity)
    if any(
        source_identity[key] != admin_identity[key]
        for key in ("host", "port", "username")
    ):
        raise ProductProvingDatabaseError(
            "PostgreSQL admin URL does not identify the exact baseline source server"
        )
    repo_root = Path(config.repo_root).resolve()
    compose_root = (
        Path(config.compose_root).resolve()
        if config.compose_root is not None
        else repo_root
    )
    environment = open_environment(
        source_database_url=config.source_database_url,
        expected_checkout_revision=config.expected_checkout_revision,
        expected_database_migration_head=config.expected_migration_head,
        repo_root=repo_root,
        compose_root=compose_root,
    )
    _require_environment_pins(environment, config.expected_migration_head)
    if provision_database is None:
        provision_database = _database_provisioner(
            repo_root,
            migration_head=config.expected_migration_head,
        )

    with tempfile.TemporaryDirectory(
        prefix="corridor-product-proving-baseline-"
    ) as parent:
        dump_path = Path(parent) / DUMP_FILENAME
        source_before = fingerprint_database(config.source_database_url)
        environment.capture(dump_path)
        dump_bytes = dump_path.read_bytes()
        dump_sha256 = _sha256(dump_bytes)
        if not dump_bytes:
            raise ProductProvingDatabaseError("captured database dump is empty")

        with provision_database(config.postgres_admin_url) as database:
            _require_verification_database(database, config.expected_migration_head)
            environment.restore(dump_path, database.name)
            clone_url = environment.clone_url(config.postgres_admin_url, database.name)
            restored = fingerprint_database(clone_url)
            verification_database_name = database.name
        if restored != source_before:
            raise ProductProvingDatabaseError(
                "restored verification database does not match the source fingerprint"
            )
        source_after = fingerprint_database(config.source_database_url)
        if source_after != source_before:
            raise ProductProvingDatabaseError(
                "source database changed while its baseline was captured"
            )

        baseline = {
            "schema_version": BASELINE_SCHEMA_VERSION,
            "checkout": {
                "revision": config.expected_checkout_revision,
                "migration_head": config.expected_migration_head,
            },
            "source_database": source_identity,
            "dump": {
                "filename": DUMP_FILENAME,
                "format": "postgresql-custom-data-only",
                "bytes": len(dump_bytes),
                "sha256": dump_sha256,
            },
            "fingerprint": source_before.as_dict(),
            "verification": {
                "restored_into_freshly_migrated_postgresql_16": True,
                "migration_head": config.expected_migration_head,
                "state_sha256": restored.state_sha256,
                "source_unchanged": True,
            },
        }
        published_dir = _publish_baseline(
            Path(config.output_dir), baseline=baseline, dump_path=dump_path
        )

    manifest_path = published_dir / MANIFEST_FILENAME
    manifest_sha256 = _sha256(manifest_path.read_bytes())
    verified = verify_product_proving_database_baseline(
        published_dir,
        expected_manifest_sha256=manifest_sha256,
    )
    if verified.fingerprint != source_before:
        raise ProductProvingDatabaseError(
            "new database baseline failed self-verification"
        )
    return ProductProvingDatabaseBaselineSummary(
        bundle_dir=published_dir,
        manifest_path=manifest_path,
        manifest_sha256=manifest_sha256,
        dump_sha256=dump_sha256,
        state_sha256=source_before.state_sha256,
        table_count=source_before.table_count,
        sequence_count=source_before.sequence_count,
        verification_database_name=verification_database_name,
    )


def verify_product_proving_database_baseline(
    bundle_dir: Path,
    *,
    expected_manifest_sha256: str,
) -> VerifiedProductProvingDatabaseBaseline:
    """Verify exact bundle membership, caller-held manifest pin, and state metadata."""

    bundle_dir = Path(bundle_dir)
    if not _SHA256.fullmatch(expected_manifest_sha256):
        raise ProductProvingDatabaseError(
            "expected baseline manifest digest is invalid"
        )
    if bundle_dir.is_symlink() or not bundle_dir.is_dir():
        raise ProductProvingDatabaseError(
            "database baseline directory is absent or unsafe"
        )
    entries = {path.name: path for path in bundle_dir.iterdir()}
    if set(entries) != {MANIFEST_FILENAME, *BASELINE_FILES}:
        raise ProductProvingDatabaseError(
            "database baseline contains an unmanifested export"
        )
    if any(path.is_symlink() or not path.is_file() for path in entries.values()):
        raise ProductProvingDatabaseError("database baseline contains an unsafe export")

    manifest_bytes = entries[MANIFEST_FILENAME].read_bytes()
    if _sha256(manifest_bytes) != expected_manifest_sha256:
        raise ProductProvingDatabaseError(
            "baseline manifest digest does not match the caller-provided pin"
        )
    manifest = _read_json(manifest_bytes, label=MANIFEST_FILENAME)
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ProductProvingDatabaseError(
            "database baseline manifest has an unsupported schema"
        )
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != set(BASELINE_FILES):
        raise ProductProvingDatabaseError(
            "database baseline manifest does not name the exact export set"
        )
    for filename in BASELINE_FILES:
        value = entries[filename].read_bytes()
        expected = files.get(filename)
        if (
            not isinstance(expected, dict)
            or expected.get("bytes") != len(value)
            or expected.get("sha256") != _sha256(value)
        ):
            raise ProductProvingDatabaseError(
                f"{filename} does not match its baseline manifest digest"
            )

    baseline_bytes = entries[BASELINE_FILENAME].read_bytes()
    baseline = _read_json(baseline_bytes, label=BASELINE_FILENAME)
    if baseline.get("schema_version") != BASELINE_SCHEMA_VERSION:
        raise ProductProvingDatabaseError("database baseline has an unsupported schema")
    if manifest.get("baseline_sha256") != _sha256(baseline_bytes):
        raise ProductProvingDatabaseError("database baseline identity does not match")
    dump = baseline.get("dump")
    dump_bytes = entries[DUMP_FILENAME].read_bytes()
    if (
        not isinstance(dump, dict)
        or dump.get("filename") != DUMP_FILENAME
        or dump.get("format") != "postgresql-custom-data-only"
        or dump.get("bytes") != len(dump_bytes)
        or dump.get("sha256") != _sha256(dump_bytes)
    ):
        raise ProductProvingDatabaseError(
            "database dump identity does not match baseline.json"
        )
    fingerprint = _parse_fingerprint(baseline.get("fingerprint"))
    verification = baseline.get("verification")
    checkout = baseline.get("checkout")
    if (
        not isinstance(verification, dict)
        or not isinstance(checkout, dict)
        or verification.get("restored_into_freshly_migrated_postgresql_16") is not True
        or verification.get("source_unchanged") is not True
        or verification.get("state_sha256") != fingerprint.state_sha256
        or verification.get("migration_head") != checkout.get("migration_head")
    ):
        raise ProductProvingDatabaseError(
            "database baseline verification claim is invalid"
        )
    return VerifiedProductProvingDatabaseBaseline(
        bundle_dir=bundle_dir,
        manifest_sha256=expected_manifest_sha256,
        dump_sha256=str(dump["sha256"]),
        baseline=baseline,
        fingerprint=fingerprint,
    )


def restore_shared_development_database(
    config: SharedDevelopmentRestoreConfig,
    *,
    provision_database: DatabaseProvisioner | None = None,
    open_environment: _EnvironmentOpener = SealedRehearsalEnvironment.open,
    fingerprint_database: FingerprintReader = fingerprint_database_url,
    stage_database: DatabaseReplacementStager | None = None,
    validate_staged_database: StagedDatabaseValidator | None = None,
    finalize_database: DatabaseReplacementFinalizer | None = None,
    rollback_database: DatabaseReplacementRollback | None = None,
) -> SharedDevelopmentRestoreSummary:
    """Replace exactly one opted-in local development database with a proved clone."""

    if config.allow_shared_development_restore is not True:
        raise ProductProvingDatabaseError(
            "shared development restore requires explicit opt-in"
        )
    _require_shared_development_source_name(config.expected_source_database_name)
    source_identity = _database_identity(config.source_database_url)
    _require_local_identity(source_identity)
    if source_identity["database"] != config.expected_source_database_name:
        raise ProductProvingDatabaseError(
            "source database URL does not match the exact caller-provided database name"
        )
    admin_identity = _database_identity(config.postgres_admin_url)
    _require_local_identity(admin_identity)
    if any(
        source_identity[key] != admin_identity[key]
        for key in ("host", "port", "username")
    ):
        raise ProductProvingDatabaseError(
            "PostgreSQL admin URL does not identify the exact local source server"
        )

    verified = verify_product_proving_database_baseline(
        config.bundle_dir,
        expected_manifest_sha256=config.expected_manifest_sha256,
    )
    checkout = verified.baseline.get("checkout")
    if checkout != {
        "revision": config.expected_checkout_revision,
        "migration_head": config.expected_migration_head,
    }:
        raise ProductProvingDatabaseError(
            "database baseline does not match the exact checkout and migration pins"
        )
    if verified.baseline.get("source_database") != source_identity:
        raise ProductProvingDatabaseError(
            "database baseline does not identify the exact shared development source"
        )

    repo_root = Path(config.repo_root).resolve()
    compose_root = (
        Path(config.compose_root).resolve()
        if config.compose_root is not None
        else repo_root
    )
    environment = open_environment(
        source_database_url=config.source_database_url,
        expected_checkout_revision=config.expected_checkout_revision,
        expected_database_migration_head=config.expected_migration_head,
        repo_root=repo_root,
        compose_root=compose_root,
    )
    _require_environment_pins(environment, config.expected_migration_head)
    if (
        environment.source_database.get("database")
        != config.expected_source_database_name
    ):
        raise ProductProvingDatabaseError(
            "opened rehearsal environment does not match the exact source database"
        )
    if provision_database is None:
        provision_database = _database_provisioner(
            repo_root,
            migration_head=config.expected_migration_head,
        )
    if stage_database is None:
        stage_database = stage_local_database_replacement
    if validate_staged_database is None:
        validate_staged_database = validate_staged_local_database_replacement
    if finalize_database is None:
        finalize_database = finalize_staged_local_database_replacement
    if rollback_database is None:
        rollback_database = rollback_staged_local_database_replacement

    source_before = fingerprint_database(config.source_database_url)
    with provision_database(config.postgres_admin_url) as database:
        _require_verification_database(database, config.expected_migration_head)
        environment.restore(verified.bundle_dir / DUMP_FILENAME, database.name)
        clone_url = environment.clone_url(config.postgres_admin_url, database.name)
        clone_fingerprint = fingerprint_database(clone_url)
        if clone_fingerprint != verified.fingerprint:
            raise ProductProvingDatabaseError(
                "restored replacement database does not match the verified baseline"
            )
        source_ready = fingerprint_database(config.source_database_url)
        if source_ready != source_before:
            raise ProductProvingDatabaseError(
                "shared development database changed while its replacement was prepared"
            )
        request = DatabaseReplacementRequest(
            source_database_url=config.source_database_url,
            postgres_admin_url=config.postgres_admin_url,
            source_database_name=config.expected_source_database_name,
            replacement_database_name=database.name,
            expected_current_fingerprint=source_ready,
            expected_replacement_fingerprint=verified.fingerprint,
            expected_migration_head=config.expected_migration_head,
        )
        staged = stage_database(request)
        try:
            staged_validation = validate_staged_database(staged)
            if staged_validation.fingerprint != verified.fingerprint:
                raise ProductProvingDatabaseError(
                    "staged shared development database failed its final fingerprint"
                )
            if staged_validation.migration_head != config.expected_migration_head:
                raise ProductProvingDatabaseError(
                    "staged shared development database has the wrong migration head"
                )
            require_postgres_16(
                staged_validation.postgres_version,
                error_cls=ProductProvingDatabaseError,
            )
            replacement = finalize_database(staged)
        except BaseException as primary_error:
            try:
                rollback_database(staged)
            except Exception as rollback_error:
                primary_error.add_note(
                    f"staged database rollback also failed: {rollback_error}"
                )
            raise
        verification_database_name = database.name

    restored = replacement.restored_fingerprint
    if restored != verified.fingerprint:
        raise ProductProvingDatabaseError(
            "shared development database failed its final baseline fingerprint"
        )
    return SharedDevelopmentRestoreSummary(
        source_database_name=config.expected_source_database_name,
        previous_state_sha256=source_before.state_sha256,
        restored_state_sha256=restored.state_sha256,
        manifest_sha256=verified.manifest_sha256,
        dump_sha256=verified.dump_sha256,
        verification_database_name=verification_database_name,
        backup_database_name=replacement.backup_database_name,
    )


def stage_local_database_replacement(
    request: DatabaseReplacementRequest,
    *,
    read_catalog: DatabaseCatalogReader | None = None,
    quiesce_database: DatabaseQuiescer | None = None,
    rename_database: DatabaseRenamer | None = None,
    set_connections: DatabaseConnectionSetter | None = None,
) -> StagedDatabaseReplacement:
    """Quiesce, verify, and rename both databases while retaining the old source."""

    _require_replacement_request(request)
    read_catalog = read_catalog or _read_database_catalog
    quiesce_database = quiesce_database or _quiesce_and_read_database
    rename_database = rename_database or _rename_database
    set_connections = set_connections or _set_database_connections
    source_name = request.source_database_name
    replacement_name = request.replacement_database_name
    backup_name = _bounded_database_name(f"corridor_pre_proving_{uuid4().hex}")
    guarded_names = {source_name, replacement_name, backup_name}
    initial_catalog = read_catalog(request.postgres_admin_url, guarded_names)
    if set(initial_catalog) != {
        source_name,
        replacement_name,
    }:
        raise ProductProvingDatabaseError(
            "source or verified replacement database identity changed before swap"
        )
    source_oid = initial_catalog[source_name]
    replacement_oid = initial_catalog[replacement_name]
    if source_oid == replacement_oid:
        raise ProductProvingDatabaseError(
            "source and replacement database OIDs must be distinct"
        )

    try:
        source_state = quiesce_database(
            request.source_database_url,
            request.postgres_admin_url,
            source_name,
        )
        if source_state.fingerprint != request.expected_current_fingerprint:
            raise ProductProvingDatabaseError(
                "quiesced source database does not match its pre-swap fingerprint"
            )
        replacement_url = _database_url_for_name(
            request.postgres_admin_url, replacement_name
        )
        replacement_state = quiesce_database(
            replacement_url,
            request.postgres_admin_url,
            replacement_name,
        )
        if replacement_state.fingerprint != request.expected_replacement_fingerprint:
            raise ProductProvingDatabaseError(
                "quiesced replacement database does not match its verified fingerprint"
            )
        if replacement_state.migration_head != request.expected_migration_head:
            raise ProductProvingDatabaseError(
                "quiesced replacement database has the wrong migration head"
            )
        require_postgres_16(
            replacement_state.postgres_version,
            error_cls=ProductProvingDatabaseError,
        )

        rename_database(request.postgres_admin_url, source_name, backup_name)
        rename_database(request.postgres_admin_url, replacement_name, source_name)
        staged_catalog = read_catalog(request.postgres_admin_url, guarded_names)
        if staged_catalog != {
            source_name: replacement_oid,
            backup_name: source_oid,
        }:
            raise ProductProvingDatabaseError(
                "database catalog does not show the exact staged replacement"
            )
    except BaseException as primary_error:
        try:
            _recover_database_replacement(
                request,
                backup_name=backup_name,
                source_oid=source_oid,
                replacement_oid=replacement_oid,
                read_catalog=read_catalog,
                rename_database=rename_database,
                set_connections=set_connections,
            )
        except Exception as rollback_error:
            primary_error.add_note(
                f"database replacement staging rollback also failed: {rollback_error}"
            )
        raise
    return StagedDatabaseReplacement(
        request=request,
        backup_database_name=backup_name,
        source_database_oid=source_oid,
        replacement_database_oid=replacement_oid,
    )


def validate_staged_local_database_replacement(
    staged: StagedDatabaseReplacement,
    *,
    read_catalog: DatabaseCatalogReader | None = None,
    quiesce_database: DatabaseQuiescer | None = None,
) -> StagedDatabaseValidation:
    """Read the renamed source while excluding every other database connection."""

    read_catalog = read_catalog or _read_database_catalog
    quiesce_database = quiesce_database or _quiesce_and_read_database
    request = staged.request
    expected_catalog = {
        request.source_database_name: staged.replacement_database_oid,
        staged.backup_database_name: staged.source_database_oid,
    }
    if (
        read_catalog(request.postgres_admin_url, set(expected_catalog))
        != expected_catalog
    ):
        raise ProductProvingDatabaseError(
            "staged replacement or retained backup database is absent"
        )
    return quiesce_database(
        request.source_database_url,
        request.postgres_admin_url,
        request.source_database_name,
    )


def finalize_staged_local_database_replacement(
    staged: StagedDatabaseReplacement,
    *,
    read_catalog: DatabaseCatalogReader | None = None,
    drop_database: Callable[[str, str], None] | None = None,
    set_connections: DatabaseConnectionSetter | None = None,
) -> DatabaseReplacementReceipt:
    """Discard the retained backup and reopen only after final validation passed."""

    read_catalog = read_catalog or _read_database_catalog
    drop_database = drop_database or _drop_database
    set_connections = set_connections or _set_database_connections
    request = staged.request
    expected_catalog = {
        request.source_database_name: staged.replacement_database_oid,
        staged.backup_database_name: staged.source_database_oid,
    }
    if (
        read_catalog(request.postgres_admin_url, set(expected_catalog))
        != expected_catalog
    ):
        raise ProductProvingDatabaseError(
            "cannot finalize an incomplete staged database replacement"
        )
    try:
        drop_database(request.postgres_admin_url, staged.backup_database_name)
    except Exception:
        remaining = read_catalog(request.postgres_admin_url, set(expected_catalog))
        if remaining != {request.source_database_name: staged.replacement_database_oid}:
            raise
    set_connections(
        request.postgres_admin_url,
        request.source_database_name,
        True,
    )
    return DatabaseReplacementReceipt(
        source_database_name=request.source_database_name,
        replacement_database_name=request.replacement_database_name,
        backup_database_name=staged.backup_database_name,
        restored_fingerprint=request.expected_replacement_fingerprint,
    )


def rollback_staged_local_database_replacement(
    staged: StagedDatabaseReplacement,
    *,
    read_catalog: DatabaseCatalogReader | None = None,
    rename_database: DatabaseRenamer | None = None,
    set_connections: DatabaseConnectionSetter | None = None,
) -> None:
    """Restore the retained old source after any staged-validation failure."""

    request = staged.request
    _recover_database_replacement(
        request,
        backup_name=staged.backup_database_name,
        source_oid=staged.source_database_oid,
        replacement_oid=staged.replacement_database_oid,
        read_catalog=read_catalog or _read_database_catalog,
        rename_database=rename_database or _rename_database,
        set_connections=set_connections or _set_database_connections,
    )


def replace_local_database_with_verified_clone(
    request: DatabaseReplacementRequest,
) -> DatabaseReplacementReceipt:
    """Convenience wrapper for a fully validated, finalized local clone swap."""

    staged = stage_local_database_replacement(request)
    try:
        validation = validate_staged_local_database_replacement(staged)
        if validation.fingerprint != request.expected_replacement_fingerprint:
            raise ProductProvingDatabaseError(
                "replacement database failed its post-swap fingerprint"
            )
        if validation.migration_head != request.expected_migration_head:
            raise ProductProvingDatabaseError(
                "replacement database failed its post-swap migration pin"
            )
        require_postgres_16(
            validation.postgres_version,
            error_cls=ProductProvingDatabaseError,
        )
        return finalize_staged_local_database_replacement(staged)
    except BaseException as primary_error:
        try:
            rollback_staged_local_database_replacement(staged)
        except Exception as rollback_error:
            primary_error.add_note(
                f"database replacement rollback also failed: {rollback_error}"
            )
        raise


def _recover_database_replacement(
    request: DatabaseReplacementRequest,
    *,
    backup_name: str,
    source_oid: int,
    replacement_oid: int,
    read_catalog: DatabaseCatalogReader,
    rename_database: DatabaseRenamer,
    set_connections: DatabaseConnectionSetter,
) -> None:
    """Recover from any observable rename phase, including ambiguous failures."""

    source_name = request.source_database_name
    replacement_name = request.replacement_database_name
    guarded = {source_name, replacement_name, backup_name}
    catalog = read_catalog(request.postgres_admin_url, guarded)
    original_catalog = {
        source_name: source_oid,
        replacement_name: replacement_oid,
    }
    after_first_rename = {
        backup_name: source_oid,
        replacement_name: replacement_oid,
    }
    after_both_renames = {
        source_name: replacement_oid,
        backup_name: source_oid,
    }
    if catalog == original_catalog:
        pass
    elif catalog == after_first_rename:
        rename_database(request.postgres_admin_url, backup_name, source_name)
    elif catalog == after_both_renames:
        rename_database(request.postgres_admin_url, source_name, replacement_name)
        rename_database(request.postgres_admin_url, backup_name, source_name)
    elif catalog == {source_name: replacement_oid}:
        set_connections(request.postgres_admin_url, source_name, True)
        return
    else:
        raise ProductProvingDatabaseError(
            "database rename phase is ambiguous; automatic rollback refused"
        )
    final_catalog = read_catalog(request.postgres_admin_url, guarded)
    if final_catalog != original_catalog:
        raise ProductProvingDatabaseError(
            "database replacement rollback did not restore both exact OIDs"
        )
    set_connections(request.postgres_admin_url, source_name, True)
    set_connections(request.postgres_admin_url, replacement_name, True)


def _quiesce_and_read_database(
    database_url: str,
    admin_url: str,
    database_name: str,
    *,
    after_verifier_pid: Callable[[int], None] | None = None,
) -> StagedDatabaseValidation:
    """Seal all writers before starting the verifier's canonical snapshot."""

    _require_database_name(database_name, "database")
    _set_database_connections(admin_url, database_name, True)
    engine = create_engine(
        database_url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
        future=True,
    )
    connection = None
    snapshot_started = False
    try:
        connection = engine.connect()
        verifier_pid = int(connection.scalar(text("select pg_backend_pid()")))
        connection.commit()
        if after_verifier_pid is not None:
            after_verifier_pid(verifier_pid)
        _set_database_connections(admin_url, database_name, False)
        _terminate_database_connections(
            admin_url,
            database_name,
            except_pid=verifier_pid,
        )
        connection.exec_driver_sql("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        snapshot_started = True
        fingerprint = _fingerprint_public_database_snapshot(connection)
        migration_heads = tuple(
            connection.scalars(text("select version_num from alembic_version")).all()
        )
        if len(migration_heads) != 1:
            raise ProductProvingDatabaseError(
                "quiesced database does not have exactly one migration head"
            )
        postgres_version = str(connection.scalar(text("show server_version")))
        return StagedDatabaseValidation(
            fingerprint=fingerprint,
            migration_head=str(migration_heads[0]),
            postgres_version=postgres_version,
        )
    finally:
        if connection is not None:
            if snapshot_started:
                connection.exec_driver_sql("ROLLBACK")
            connection.close()
        engine.dispose()


def _read_database_catalog(admin_url: str, names: set[str]) -> dict[str, int]:
    if not names:
        return {}
    for name in names:
        _require_database_name(name, "database")
    engine = _maintenance_engine(admin_url)
    try:
        with engine.connect() as connection:
            version = str(connection.scalar(text("show server_version")))
            require_postgres_16(version, error_cls=ProductProvingDatabaseError)
            return {
                str(name): int(oid)
                for name, oid in connection.execute(
                    text(
                        "select datname, oid::bigint from pg_database "
                        "where datname = any(:names)"
                    ),
                    {"names": list(names)},
                ).all()
            }
    finally:
        engine.dispose()


def _rename_database(admin_url: str, old_name: str, new_name: str) -> None:
    _require_database_name(old_name, "database")
    _require_database_name(new_name, "database")
    engine = _maintenance_engine(admin_url)
    try:
        with engine.connect() as connection:
            connection.execute(
                text(f'alter database "{old_name}" rename to "{new_name}"')
            )
    finally:
        engine.dispose()


def _drop_database(admin_url: str, database_name: str) -> None:
    _require_database_name(database_name, "database")
    engine = _maintenance_engine(admin_url)
    try:
        with engine.connect() as connection:
            connection.execute(text(f'drop database "{database_name}"'))
    finally:
        engine.dispose()


def _set_database_connections(
    admin_url: str,
    database_name: str,
    allowed: bool,
) -> None:
    _require_database_name(database_name, "database")
    flag = "true" if allowed else "false"
    engine = _maintenance_engine(admin_url)
    try:
        with engine.connect() as connection:
            connection.execute(
                text(f'alter database "{database_name}" with allow_connections {flag}')
            )
    finally:
        engine.dispose()


def _terminate_database_connections(
    admin_url: str,
    database_name: str,
    *,
    except_pid: int,
) -> None:
    engine = _maintenance_engine(admin_url)
    try:
        with engine.connect() as connection:
            connection.scalars(
                text(
                    "select pg_terminate_backend(pid, 5000) from pg_stat_activity "
                    "where datname = :name and pid <> :except_pid"
                ),
                {"name": database_name, "except_pid": except_pid},
            ).all()
            remaining = int(
                connection.scalar(
                    text(
                        "select count(*) from pg_stat_activity "
                        "where datname = :name and pid <> :except_pid"
                    ),
                    {"name": database_name, "except_pid": except_pid},
                )
            )
            verifier_present = int(
                connection.scalar(
                    text(
                        "select count(*) from pg_stat_activity "
                        "where datname = :name and pid = :except_pid"
                    ),
                    {"name": database_name, "except_pid": except_pid},
                )
            )
            if remaining or verifier_present != 1:
                raise ProductProvingDatabaseError(
                    "database quiescence did not leave exactly one verifier backend"
                )
    finally:
        engine.dispose()


def _maintenance_engine(admin_url: str):
    parsed = make_url(admin_url)
    require_local_postgres_host(
        parsed.host,
        error_cls=ProductProvingDatabaseError,
    )
    return create_engine(
        parsed.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
        future=True,
    )


def _database_url_for_name(admin_url: str, database_name: str) -> str:
    _require_database_name(database_name, "database")
    return (
        make_url(admin_url)
        .set(database=database_name)
        .render_as_string(hide_password=False)
    )


def _publish_baseline(
    output_dir: Path,
    *,
    baseline: Mapping[str, Any],
    dump_path: Path,
) -> Path:
    baseline_bytes = _canonical_json(baseline) + b"\n"

    def build(stage_dir: Path) -> None:
        baseline_target = stage_dir / BASELINE_FILENAME
        dump_target = stage_dir / DUMP_FILENAME
        baseline_target.write_bytes(baseline_bytes)
        shutil.copyfile(dump_path, dump_target)
        files = {
            BASELINE_FILENAME: {
                "bytes": len(baseline_bytes),
                "sha256": _sha256(baseline_bytes),
            },
            DUMP_FILENAME: {
                "bytes": dump_target.stat().st_size,
                "sha256": _sha256(dump_target.read_bytes()),
            },
        }
        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "baseline_sha256": _sha256(baseline_bytes),
            "files": files,
        }
        (stage_dir / MANIFEST_FILENAME).write_bytes(_canonical_json(manifest) + b"\n")

    try:
        return publish_directory_once(
            output_dir,
            temp_prefix="corridor-product-proving-database-baseline",
            build=build,
        )
    except FileExistsError as exc:
        raise ProductProvingDatabaseError(
            "database baseline output directory already exists"
        ) from exc


def _parse_fingerprint(raw: object) -> DatabaseFingerprint:
    if not isinstance(raw, dict):
        raise ProductProvingDatabaseError("database fingerprint is invalid")
    raw_tables = raw.get("tables")
    raw_sequences = raw.get("sequences")
    if not isinstance(raw_tables, list) or not isinstance(raw_sequences, list):
        raise ProductProvingDatabaseError("database fingerprint members are invalid")
    try:
        tables = tuple(
            TableFingerprint(
                name=str(item["name"]),
                row_count=int(item["row_count"]),
                rows_sha256=str(item["rows_sha256"]),
            )
            for item in raw_tables
            if isinstance(item, dict)
        )
        sequences = tuple(
            SequenceFingerprint(
                name=str(item["name"]),
                data_type=str(item["data_type"]),
                start_value=str(item["start_value"]),
                minimum_value=str(item["minimum_value"]),
                maximum_value=str(item["maximum_value"]),
                increment=str(item["increment"]),
                cache_size=str(item["cache_size"]),
                cycles=_strict_bool(item["cycles"]),
                last_value=str(item["last_value"]),
                is_called=_strict_bool(item["is_called"]),
            )
            for item in raw_sequences
            if isinstance(item, dict)
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProductProvingDatabaseError("database fingerprint is invalid") from exc
    if len(tables) != len(raw_tables) or len(sequences) != len(raw_sequences):
        raise ProductProvingDatabaseError("database fingerprint entries are invalid")
    if [item.name for item in tables] != sorted({item.name for item in tables}):
        raise ProductProvingDatabaseError(
            "database table fingerprints are not canonical"
        )
    if [item.name for item in sequences] != sorted({item.name for item in sequences}):
        raise ProductProvingDatabaseError(
            "database sequence fingerprints are not canonical"
        )
    if (
        raw.get("table_count") != len(tables)
        or raw.get("sequence_count") != len(sequences)
        or any(
            item.row_count < 0 or not _SHA256.fullmatch(item.rows_sha256)
            for item in tables
        )
    ):
        raise ProductProvingDatabaseError(
            "database fingerprint counts or digests are invalid"
        )
    canonical = {
        "tables": [item.as_dict() for item in tables],
        "sequences": [item.as_dict() for item in sequences],
    }
    state_sha256 = str(raw.get("state_sha256"))
    if not _SHA256.fullmatch(state_sha256) or state_sha256 != _json_sha256(canonical):
        raise ProductProvingDatabaseError("database state fingerprint is invalid")
    return DatabaseFingerprint(
        tables=tables,
        sequences=sequences,
        state_sha256=state_sha256,
    )


def _database_provisioner(
    repo_root: Path,
    *,
    migration_head: str,
) -> DatabaseProvisioner:
    @contextmanager
    def provision(admin_url: str) -> Iterator[ProvisionedDatabase]:
        maintenance_url = (
            make_url(admin_url)
            .set(database="postgres")
            .render_as_string(hide_password=False)
        )
        with provision_disposable_postgres(
            maintenance_url,
            repo_root=repo_root,
            error_cls=ProductProvingDatabaseError,
            database_prefix=DISPOSABLE_DATABASE_PREFIX,
            migration_revision=migration_head,
        ) as database:
            yield database

    return provision


def _require_environment_pins(
    environment: SealedRehearsalEnvironment,
    expected_migration_head: str,
) -> None:
    if (
        environment.checkout_migration_head != expected_migration_head
        or environment.database_migration_head != expected_migration_head
    ):
        raise ProductProvingDatabaseError(
            "opened rehearsal environment does not match the migration pin"
        )


def _require_verification_database(
    database: ProvisionedDatabase,
    expected_migration_head: str,
) -> None:
    _require_database_name(database.name, "verification database")
    if not database.name.startswith(DISPOSABLE_DATABASE_PREFIX):
        raise ProductProvingDatabaseError(
            "verification database is outside the guarded disposable namespace"
        )
    if database.migration_head != expected_migration_head:
        raise ProductProvingDatabaseError(
            "verification database does not match the exact migration pin"
        )
    require_postgres_16(
        database.postgres_version,
        error_cls=ProductProvingDatabaseError,
    )


def _database_identity(database_url: str) -> dict[str, Any]:
    parsed = make_url(database_url)
    if parsed.get_backend_name() != "postgresql":
        raise ProductProvingDatabaseError("Product Proving database must be PostgreSQL")
    if parsed.query:
        raise ProductProvingDatabaseError(
            "query-based PostgreSQL connection routing is not allowed"
        )
    if not parsed.database or not parsed.username:
        raise ProductProvingDatabaseError(
            "Product Proving database identity is incomplete"
        )
    _require_database_name(parsed.database, "database")
    return {
        "backend": parsed.get_backend_name(),
        "host": parsed.host,
        "port": parsed.port,
        "database": parsed.database,
        "username": parsed.username,
    }


def _require_local_identity(identity: Mapping[str, Any]) -> None:
    require_local_postgres_host(
        identity.get("host"),
        error_cls=ProductProvingDatabaseError,
    )


def _require_replacement_request(request: DatabaseReplacementRequest) -> None:
    source_name = request.source_database_name
    replacement_name = request.replacement_database_name
    _require_shared_development_source_name(source_name)
    _require_database_name(replacement_name, "replacement database")
    if not replacement_name.startswith(DISPOSABLE_DATABASE_PREFIX):
        raise ProductProvingDatabaseError(
            "replacement database is outside the guarded disposable namespace"
        )
    if source_name == replacement_name:
        raise ProductProvingDatabaseError(
            "source and replacement database names must be different"
        )
    if not re.fullmatch(r"[0-9a-f]+", request.expected_migration_head):
        raise ProductProvingDatabaseError("replacement migration head pin is invalid")
    source_identity = _database_identity(request.source_database_url)
    admin_identity = _database_identity(request.postgres_admin_url)
    _require_local_identity(source_identity)
    _require_local_identity(admin_identity)
    if source_identity["database"] != source_name:
        raise ProductProvingDatabaseError(
            "replacement request names the wrong source database"
        )
    if any(
        source_identity[key] != admin_identity[key]
        for key in ("host", "port", "username")
    ):
        raise ProductProvingDatabaseError(
            "replacement request crosses PostgreSQL server identities"
        )


def _require_database_name(value: str, label: str) -> None:
    if len(value.encode("utf-8")) > 63 or not _DATABASE_NAME.fullmatch(value):
        raise ProductProvingDatabaseError(f"{label} name is invalid")


def _require_shared_development_source_name(value: str) -> None:
    if (
        not _SHARED_DEVELOPMENT_DATABASE_NAME.fullmatch(value)
        or value in _POSTGRES_MAINTENANCE_DATABASES
    ):
        raise ProductProvingDatabaseError(
            "shared development source database name is not an allowed exact target"
        )


def _bounded_database_name(value: str) -> str:
    bounded = value[:63]
    _require_database_name(bounded, "backup database")
    return bounded


def _read_json(value: bytes, *, label: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProductProvingDatabaseError(f"{label} is invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise ProductProvingDatabaseError(f"{label} must contain an object")
    return decoded


def _strict_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("expected a boolean")
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _json_sha256(value: object) -> str:
    return _sha256(_canonical_json(value))


def _sha256(value: bytes) -> str:
    return sha256(value).hexdigest()
