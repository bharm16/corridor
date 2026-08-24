"""Public behavior for exact Product Proving database baselines."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from corridor.config import settings
from corridor.product_proving_database import (
    DISPOSABLE_DATABASE_PREFIX,
    DatabaseFingerprint,
    DatabaseReplacementReceipt,
    ProductProvingDatabaseBaselineConfig,
    ProductProvingDatabaseError,
    SequenceFingerprint,
    SharedDevelopmentRestoreConfig,
    TableFingerprint,
    capture_product_proving_database_baseline,
    fingerprint_public_database,
    restore_shared_development_database,
    verify_product_proving_database_baseline,
)


REVISION = "a" * 40
MIGRATION_HEAD = "a316c5d7e9f1"
SOURCE_URL = "postgresql+psycopg://corridor:corridor@localhost:5433/corridor"
ADMIN_URL = SOURCE_URL


def _fingerprint(seed: str) -> DatabaseFingerprint:
    table = TableFingerprint(
        name="projects",
        row_count=1,
        rows_sha256=seed * 64,
    )
    sequence = SequenceFingerprint(
        name="projects_id_seq",
        data_type="bigint",
        start_value="1",
        minimum_value="1",
        maximum_value="9223372036854775807",
        increment="1",
        cache_size="1",
        cycles=False,
        last_value="1",
        is_called=True,
    )
    canonical = {
        "tables": [table.as_dict()],
        "sequences": [sequence.as_dict()],
    }
    return DatabaseFingerprint(
        tables=(table,),
        sequences=(sequence,),
        state_sha256=sha256(
            json.dumps(
                canonical,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
    )


@dataclass
class _FakeEnvironment:
    head: str
    restore_targets: list[str]

    @property
    def checkout_migration_head(self) -> str:
        return self.head

    @property
    def database_migration_head(self) -> str:
        return self.head

    @property
    def source_database(self) -> dict[str, str]:
        return {"database": "corridor", "username": "corridor"}

    def capture(self, dump_path: Path) -> None:
        dump_path.write_bytes(b"custom-format-data-only-dump")

    def restore(self, _dump_path: Path, database_name: str) -> None:
        self.restore_targets.append(database_name)

    @staticmethod
    def clone_url(_admin_url: str, database_name: str) -> str:
        return f"postgresql+psycopg://corridor:corridor@localhost:5433/{database_name}"


def _opener(environment: _FakeEnvironment):
    def open_environment(**_kwargs):
        return environment

    return open_environment


def _provisioner(name: str = f"{DISPOSABLE_DATABASE_PREFIX}test"):
    @contextmanager
    def provision(_admin_url: str):
        yield SimpleNamespace(
            name=name,
            migration_head=MIGRATION_HEAD,
            postgres_version="16.10",
        )

    return provision


def _capture_config(tmp_path: Path) -> ProductProvingDatabaseBaselineConfig:
    return ProductProvingDatabaseBaselineConfig(
        source_database_url=SOURCE_URL,
        postgres_admin_url=ADMIN_URL,
        expected_checkout_revision=REVISION,
        expected_migration_head=MIGRATION_HEAD,
        output_dir=tmp_path / "baseline",
        repo_root=tmp_path,
    )


def _capture_fake_baseline(tmp_path: Path):
    expected = _fingerprint("1")
    environment = _FakeEnvironment(MIGRATION_HEAD, [])

    def read_fingerprint(_database_url: str) -> DatabaseFingerprint:
        return expected

    summary = capture_product_proving_database_baseline(
        _capture_config(tmp_path),
        provision_database=_provisioner(),
        open_environment=_opener(environment),
        fingerprint_database=read_fingerprint,
    )
    return summary, expected, environment


def test_public_fingerprint_discovers_all_current_tables_and_sequences_read_only():
    """The baseline follows PostgreSQL, not the ORM's necessarily partial metadata."""

    engine = create_engine(settings.database_url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            fingerprint = fingerprint_public_database(connection)
            transaction_read_only = connection.scalar(
                text("show transaction_read_only")
            )
    finally:
        engine.dispose()

    assert transaction_read_only == "on"
    assert fingerprint.table_count == 62
    assert fingerprint.sequence_count == 53
    assert [item.name for item in fingerprint.tables] == sorted(
        item.name for item in fingerprint.tables
    )
    assert [item.name for item in fingerprint.sequences] == sorted(
        item.name for item in fingerprint.sequences
    )
    assert {item.name for item in fingerprint.tables} >= {
        "alembic_version",
        "candidates",
        "dependency_events",
        "statement_coordination_receipts",
    }
    assert len(fingerprint.state_sha256) == 64


def test_capture_restores_only_a_fresh_database_and_publishes_immutable_bundle(
    tmp_path,
):
    summary, expected, environment = _capture_fake_baseline(tmp_path)

    assert environment.restore_targets == [f"{DISPOSABLE_DATABASE_PREFIX}test"]
    assert environment.restore_targets != ["corridor"]
    assert summary.state_sha256 == expected.state_sha256
    assert summary.table_count == 1
    assert summary.sequence_count == 1
    assert {path.name for path in summary.bundle_dir.iterdir()} == {
        "baseline.json",
        "baseline.dump",
        "manifest.json",
    }
    verified = verify_product_proving_database_baseline(
        summary.bundle_dir,
        expected_manifest_sha256=summary.manifest_sha256,
    )
    assert verified.fingerprint == expected
    assert verified.dump_sha256 == summary.dump_sha256

    with pytest.raises(ProductProvingDatabaseError, match="already exists"):
        capture_product_proving_database_baseline(
            _capture_config(tmp_path),
            provision_database=_provisioner(),
            open_environment=_opener(_FakeEnvironment(MIGRATION_HEAD, [])),
            fingerprint_database=lambda _url: expected,
        )


def test_capture_refuses_a_dump_that_does_not_restore_to_the_exact_source(tmp_path):
    source = _fingerprint("1")
    restored = _fingerprint("2")
    environment = _FakeEnvironment(MIGRATION_HEAD, [])
    reads = iter((source, restored))

    with pytest.raises(ProductProvingDatabaseError, match="does not match"):
        capture_product_proving_database_baseline(
            _capture_config(tmp_path),
            provision_database=_provisioner(),
            open_environment=_opener(environment),
            fingerprint_database=lambda _url: next(reads),
        )

    assert not (tmp_path / "baseline").exists()


def test_verifier_refuses_tampered_dump_and_wrong_caller_pin(tmp_path):
    summary, _expected, _environment = _capture_fake_baseline(tmp_path)

    with pytest.raises(ProductProvingDatabaseError, match="caller-provided pin"):
        verify_product_proving_database_baseline(
            summary.bundle_dir,
            expected_manifest_sha256="f" * 64,
        )

    (summary.bundle_dir / "baseline.dump").write_bytes(b"tampered")
    with pytest.raises(ProductProvingDatabaseError, match="manifest digest"):
        verify_product_proving_database_baseline(
            summary.bundle_dir,
            expected_manifest_sha256=summary.manifest_sha256,
        )


def test_shared_restore_requires_opt_in_before_inspecting_a_bundle(tmp_path):
    config = SharedDevelopmentRestoreConfig(
        source_database_url=SOURCE_URL,
        postgres_admin_url=ADMIN_URL,
        expected_source_database_name="corridor",
        expected_checkout_revision=REVISION,
        expected_migration_head=MIGRATION_HEAD,
        bundle_dir=tmp_path / "absent",
        expected_manifest_sha256="f" * 64,
        repo_root=tmp_path,
    )

    with pytest.raises(ProductProvingDatabaseError, match="explicit opt-in"):
        restore_shared_development_database(config)


def test_verification_database_namespace_fits_postgresql_identifier_limit():
    assert len(f"{DISPOSABLE_DATABASE_PREFIX}{'a' * 32}".encode()) <= 63


def test_shared_restore_verifies_clone_then_replaces_only_the_exact_source(tmp_path):
    captured, baseline, _capture_environment = _capture_fake_baseline(tmp_path)
    current = _fingerprint("2")
    restore_environment = _FakeEnvironment(MIGRATION_HEAD, [])
    states = {
        SOURCE_URL: current,
        (
            "postgresql+psycopg://corridor:corridor@localhost:5433/"
            f"{DISPOSABLE_DATABASE_PREFIX}test"
        ): baseline,
    }
    replacement_requests = []

    def replace(request):
        replacement_requests.append(request)
        states[SOURCE_URL] = baseline
        return DatabaseReplacementReceipt(
            source_database_name=request.source_database_name,
            replacement_database_name=request.replacement_database_name,
            backup_database_name="corridor_pre_proving_backup",
            restored_fingerprint=baseline,
        )

    config = SharedDevelopmentRestoreConfig(
        source_database_url=SOURCE_URL,
        postgres_admin_url=ADMIN_URL,
        expected_source_database_name="corridor",
        expected_checkout_revision=REVISION,
        expected_migration_head=MIGRATION_HEAD,
        bundle_dir=captured.bundle_dir,
        expected_manifest_sha256=captured.manifest_sha256,
        repo_root=tmp_path,
        allow_shared_development_restore=True,
    )
    summary = restore_shared_development_database(
        config,
        provision_database=_provisioner(),
        open_environment=_opener(restore_environment),
        fingerprint_database=states.__getitem__,
        replace_database=replace,
        read_database_head=lambda _url, _root: MIGRATION_HEAD,
    )

    assert restore_environment.restore_targets == [f"{DISPOSABLE_DATABASE_PREFIX}test"]
    assert len(replacement_requests) == 1
    request = replacement_requests[0]
    assert request.source_database_name == "corridor"
    assert request.replacement_database_name.startswith(DISPOSABLE_DATABASE_PREFIX)
    assert request.expected_current_fingerprint == current
    assert request.expected_replacement_fingerprint == baseline
    assert summary.previous_state_sha256 == current.state_sha256
    assert summary.restored_state_sha256 == baseline.state_sha256


@pytest.mark.parametrize(
    ("source_url", "source_name", "error"),
    [
        (
            "postgresql+psycopg://corridor:corridor@db.example.com:5433/corridor",
            "corridor",
            "local PostgreSQL",
        ),
        (SOURCE_URL, "some_other_database", "exact caller-provided database name"),
        (
            "postgresql+psycopg://corridor:corridor@localhost:5433/postgres",
            "postgres",
            "not an allowed exact target",
        ),
        (
            "postgresql+psycopg://corridor:corridor@localhost:5433/$DATABASE",
            "$DATABASE",
            "not an allowed exact target",
        ),
    ],
)
def test_shared_restore_refuses_a_nonlocal_or_differently_named_source(
    tmp_path,
    source_url,
    source_name,
    error,
):
    config = SharedDevelopmentRestoreConfig(
        source_database_url=source_url,
        postgres_admin_url=ADMIN_URL,
        expected_source_database_name=source_name,
        expected_checkout_revision=REVISION,
        expected_migration_head=MIGRATION_HEAD,
        bundle_dir=tmp_path / "unused",
        expected_manifest_sha256="f" * 64,
        repo_root=tmp_path,
        allow_shared_development_restore=True,
    )

    with pytest.raises(ProductProvingDatabaseError, match=error):
        restore_shared_development_database(config)
