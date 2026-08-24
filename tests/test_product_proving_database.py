"""Public behavior for exact Product Proving database baselines."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from corridor.config import settings
from corridor.m8_acceptance_database import provision_disposable_postgres
from corridor.product_proving_database import (
    DISPOSABLE_DATABASE_PREFIX,
    DatabaseFingerprint,
    DatabaseReplacementReceipt,
    DatabaseReplacementRequest,
    ProductProvingDatabaseBaselineConfig,
    ProductProvingDatabaseError,
    SequenceFingerprint,
    SharedDevelopmentRestoreConfig,
    StagedDatabaseReplacement,
    StagedDatabaseValidation,
    TableFingerprint,
    capture_product_proving_database_baseline,
    fingerprint_database_url,
    fingerprint_public_database,
    restore_shared_development_database,
    stage_local_database_replacement,
    verify_product_proving_database_baseline,
    _quiesce_and_read_database,
)


REVISION = "a" * 40
MIGRATION_HEAD = "b317c5d7e9f2"
SOURCE_URL = "postgresql+psycopg://corridor:corridor@localhost:5433/corridor"
ADMIN_URL = SOURCE_URL
REPO_ROOT = Path(__file__).resolve().parents[1]


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


@pytest.mark.slow
def test_quiescence_starts_its_snapshot_after_a_writer_commits_in_the_pid_window():
    configured = make_url(settings.database_url)
    admin_url = configured.set(database="postgres").render_as_string(
        hide_password=False
    )
    with provision_disposable_postgres(
        admin_url,
        repo_root=REPO_ROOT,
        error_cls=ProductProvingDatabaseError,
        database_prefix=DISPOSABLE_DATABASE_PREFIX,
        migration_revision=MIGRATION_HEAD,
    ) as database:
        database_url = configured.set(database=database.name).render_as_string(
            hide_password=False
        )
        before = fingerprint_database_url(database_url)
        writer_engine = create_engine(database_url, poolclass=NullPool)
        writer = writer_engine.connect()
        transaction = writer.begin()
        writer.execute(
            text(
                "insert into projects (slug, name) "
                "values ('pid-window-project', 'PID Window Project')"
            )
        )
        callback_pids = []

        def commit_after_pid(verifier_pid):
            callback_pids.append(verifier_pid)
            transaction.commit()

        try:
            validation = _quiesce_and_read_database(
                database_url,
                admin_url,
                database.name,
                after_verifier_pid=commit_after_pid,
            )
        finally:
            writer.close()
            writer_engine.dispose()

        before_projects = next(
            item for item in before.tables if item.name == "projects"
        )
        after_projects = next(
            item for item in validation.fingerprint.tables if item.name == "projects"
        )
        assert callback_pids
        assert after_projects.row_count == before_projects.row_count + 1
        assert validation.fingerprint != before


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


@pytest.mark.parametrize(
    "url_field",
    ["source_database_url", "postgres_admin_url"],
)
def test_capture_refuses_query_based_connection_routing(tmp_path, url_field):
    config = _capture_config(tmp_path)
    routed = f"{SOURCE_URL}?host=/var/run/postgresql"

    with pytest.raises(
        ProductProvingDatabaseError,
        match="query-based PostgreSQL connection routing",
    ):
        capture_product_proving_database_baseline(
            replace(config, **{url_field: routed}),
            open_environment=lambda **_kwargs: pytest.fail(
                "unsafe routed URL reached environment open"
            ),
        )


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

    def stage(request):
        replacement_requests.append(request)
        states[SOURCE_URL] = baseline
        return StagedDatabaseReplacement(
            request=request,
            backup_database_name="corridor_pre_proving_backup",
            source_database_oid=101,
            replacement_database_oid=202,
        )

    def validate(_staged):
        return StagedDatabaseValidation(
            fingerprint=baseline,
            migration_head=MIGRATION_HEAD,
            postgres_version="16.10",
        )

    def finalize(request):
        return DatabaseReplacementReceipt(
            source_database_name=request.request.source_database_name,
            replacement_database_name=request.request.replacement_database_name,
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
        stage_database=stage,
        validate_staged_database=validate,
        finalize_database=finalize,
        rollback_database=lambda _staged: pytest.fail("unexpected rollback"),
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
    ("failing_rename", "fail_after_mutation"),
    [(1, True), (2, True)],
)
def test_staging_recovers_the_exact_source_from_each_ambiguous_rename_failure(
    failing_rename,
    fail_after_mutation,
):
    current = _fingerprint("1")
    replacement = _fingerprint("2")
    replacement_name = f"{DISPOSABLE_DATABASE_PREFIX}rename_failure"
    catalog = {"corridor": 101, replacement_name: 202}
    connections = {"corridor": True, replacement_name: True}
    rename_calls = 0

    def read_catalog(_admin_url, guarded):
        return {name: oid for name, oid in catalog.items() if name in guarded}

    def quiesce(_database_url, _admin_url, database_name):
        connections[database_name] = False
        return StagedDatabaseValidation(
            fingerprint=(current if database_name == "corridor" else replacement),
            migration_head=MIGRATION_HEAD,
            postgres_version="16.10",
        )

    def rename(_admin_url, old_name, new_name):
        nonlocal rename_calls
        rename_calls += 1
        should_fail = rename_calls == failing_rename
        if should_fail and not fail_after_mutation:
            raise RuntimeError("injected rename failure")
        catalog[new_name] = catalog.pop(old_name)
        connections[new_name] = connections.pop(old_name)
        if should_fail:
            raise RuntimeError("injected rename failure")

    def set_connections(_admin_url, database_name, allowed):
        connections[database_name] = allowed

    request = DatabaseReplacementRequest(
        source_database_url=SOURCE_URL,
        postgres_admin_url=ADMIN_URL,
        source_database_name="corridor",
        replacement_database_name=replacement_name,
        expected_current_fingerprint=current,
        expected_replacement_fingerprint=replacement,
        expected_migration_head=MIGRATION_HEAD,
    )

    with pytest.raises(RuntimeError, match="injected rename failure"):
        stage_local_database_replacement(
            request,
            read_catalog=read_catalog,
            quiesce_database=quiesce,
            rename_database=rename,
            set_connections=set_connections,
        )

    assert catalog == {"corridor": 101, replacement_name: 202}
    assert connections["corridor"] is True
    assert connections[replacement_name] is True


def test_shared_restore_rolls_back_before_finalization_on_post_swap_validation_failure(
    tmp_path,
):
    captured, baseline, _capture_environment = _capture_fake_baseline(tmp_path)
    current = _fingerprint("2")
    restore_environment = _FakeEnvironment(MIGRATION_HEAD, [])
    clone_url = (
        "postgresql+psycopg://corridor:corridor@localhost:5433/"
        f"{DISPOSABLE_DATABASE_PREFIX}test"
    )
    staged_items = []
    rollbacks = []

    def stage(request):
        staged = StagedDatabaseReplacement(
            request=request,
            backup_database_name="corridor_pre_proving_backup",
            source_database_oid=101,
            replacement_database_oid=202,
        )
        staged_items.append(staged)
        return staged

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

    with pytest.raises(ProductProvingDatabaseError, match="final fingerprint"):
        restore_shared_development_database(
            config,
            provision_database=_provisioner(),
            open_environment=_opener(restore_environment),
            fingerprint_database={SOURCE_URL: current, clone_url: baseline}.__getitem__,
            stage_database=stage,
            validate_staged_database=lambda _staged: StagedDatabaseValidation(
                fingerprint=_fingerprint("3"),
                migration_head=MIGRATION_HEAD,
                postgres_version="16.10",
            ),
            finalize_database=lambda _staged: pytest.fail(
                "finalization must follow validation"
            ),
            rollback_database=rollbacks.append,
        )

    assert rollbacks == staged_items


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
        (
            (
                "postgresql+psycopg://corridor:corridor@localhost:5433/"
                "corridor?host=/var/run/postgresql"
            ),
            "corridor",
            "query-based PostgreSQL connection routing",
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
