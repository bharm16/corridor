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
    DatabaseConnectionIdentity,
    DatabaseFingerprint,
    DatabaseReplacementReceipt,
    DatabaseReplacementRequest,
    ProductProvingDatabaseBaselineConfig,
    ProductProvingDatabaseError,
    SchemaObjectFingerprint,
    SequenceFingerprint,
    SharedDevelopmentRestoreConfig,
    StagedDatabaseReplacement,
    StagedDatabaseValidation,
    TableFingerprint,
    capture_product_proving_database_baseline,
    fingerprint_database_url,
    fingerprint_public_database,
    parse_database_fingerprint,
    restore_shared_development_database,
    stage_local_database_replacement,
    verify_product_proving_database_baseline,
    _quiesce_and_read_database,
)


REVISION = "a" * 40
MIGRATION_HEAD = "c0a1d0b5e11e"
SOURCE_URL = "postgresql+psycopg://corridor:corridor@localhost:5433/corridor"
ADMIN_URL = SOURCE_URL
REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _configured_shared_database(monkeypatch):
    """The proving source is explicit even inside an xdist worker database."""

    monkeypatch.setattr(settings, "database_url", SOURCE_URL)


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
        "schema_objects": [],
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


def test_public_fingerprint_discovers_all_current_tables_and_sequences_read_only(
    runtime_database,
):
    """The baseline follows PostgreSQL, not the ORM's necessarily partial metadata."""

    database_url = make_url(settings.database_url).set(database=runtime_database.name)
    engine = create_engine(database_url, poolclass=NullPool)
    try:
        with engine.connect() as connection:
            fingerprint = fingerprint_public_database(connection)
            transaction_read_only = connection.scalar(
                text("show transaction_read_only")
            )
    finally:
        engine.dispose()

    assert transaction_read_only == "on"
    # The full delivery wave's tables: organization-identity evidence (#345),
    # Follow-up Plans (#333), statement suggestions (#340), documentation
    # checklists (#347), dispute history (#346), key-date drafts (#363),
    # inbound intake (#372), Coordination Summary (#355), the due-work
    # runtime and its occurrences (#332), assignment/due-action/document
    # notifications (#351-#353), scheduled publication (#354), outcome
    # capture (#356), scheduled reproof (#358), conditions (#373), and the
    # operations assists (#359-#362), and the spreadsheet Source Segment
    # evidence spine (#431-#435), the structured Fact satellites (#449), PDF
    # page Processing Failures (#440), and the exact subject registry,
    # attempts, candidates, decisions, and rankings (#453).
    assert fingerprint.table_count == 143
    assert fingerprint.sequence_count == 132
    assert fingerprint.schema_object_count > 0
    assert len(fingerprint.schema_sha256) == 64
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
        "due_work_occurrences",
        "due_work_receipts",
        "due_work_schedules",
        "facts",
        "fact_sources",
        "source_segments",
        "source_fact_append_receipts",
        "extracted_proposals",
        "extracted_proposal_facts",
        "extraction_run_candidates",
        "fact_dispositions",
        "project_record_revisions",
        "fact_decisions",
        "fact_applies_to",
        "fact_closure_results",
        "fact_closure_sources",
        "stated_by_people",
        "subject_resolution_attempts",
        "subject_resolution_candidates",
        "subject_resolution_decisions",
        "subject_candidate_suggestions",
        "page_processing_failures",
        "statement_coordination_receipts",
    }
    assert "source_segments_id_seq" in {
        item.name for item in fingerprint.sequences
    }
    assert {"facts_id_seq", "fact_sources_id_seq"} <= {
        item.name for item in fingerprint.sequences
    }
    assert "source_fact_append_receipts_id_seq" in {
        item.name for item in fingerprint.sequences
    }
    assert {
        "extracted_proposals_id_seq",
        "extracted_proposal_facts_id_seq",
        "fact_dispositions_id_seq",
        "extraction_run_candidates_id_seq",
        "project_record_revisions_id_seq",
        "fact_decisions_id_seq",
        "fact_applies_to_id_seq",
        "fact_closure_results_id_seq",
        "fact_closure_sources_id_seq",
        "stated_by_people_id_seq",
        "subject_resolution_attempts_id_seq",
        "subject_resolution_candidates_id_seq",
        "subject_resolution_decisions_id_seq",
        "subject_candidate_suggestions_id_seq",
    } <= {item.name for item in fingerprint.sequences}
    assert {item.kind for item in fingerprint.schema_objects} == {
        "column",
        "constraint",
        "function",
        "trigger",
        "index",
    }
    assert any(
        item.kind == "function"
        and item.identity.startswith("extraction_token_usage_membership_is_valid(")
        for item in fingerprint.schema_objects
    )
    assert len(fingerprint.state_sha256) == 64

    assert parse_database_fingerprint(fingerprint.as_dict()) == fingerprint
    incomplete = fingerprint.as_dict()
    incomplete.pop("schema_objects")
    with pytest.raises(ProductProvingDatabaseError, match="members are invalid"):
        parse_database_fingerprint(incomplete)


@pytest.mark.slow
def test_same_migration_head_schema_drift_changes_the_canonical_fingerprint():
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
        engine = create_engine(database_url, poolclass=NullPool)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "create function product_proving_same_head_drift() "
                        "returns integer language sql immutable as 'select 1'"
                    )
                )
            with engine.connect() as connection:
                assert connection.scalar(
                    text("select version_num from alembic_version")
                ) == MIGRATION_HEAD
        finally:
            engine.dispose()
        after = fingerprint_database_url(database_url)

        assert after.tables == before.tables
        assert after.sequences == before.sequences
        assert after.state_sha256 != before.state_sha256
        assert after.schema_sha256 != before.schema_sha256
        assert after != before


@pytest.mark.slow
def test_fresh_databases_at_one_head_have_the_same_schema_fingerprint():
    configured = make_url(settings.database_url)
    admin_url = configured.set(database="postgres").render_as_string(
        hide_password=False
    )
    fingerprints = []
    for suffix in ("a_", "b_"):
        with provision_disposable_postgres(
            admin_url,
            repo_root=REPO_ROOT,
            error_cls=ProductProvingDatabaseError,
            database_prefix=f"{DISPOSABLE_DATABASE_PREFIX}{suffix}",
            migration_revision=MIGRATION_HEAD,
        ) as database:
            database_url = configured.set(database=database.name).render_as_string(
                hide_password=False
            )
            fingerprints.append(fingerprint_database_url(database_url))

    assert fingerprints[0].schema_objects == fingerprints[1].schema_objects
    assert fingerprints[0].schema_sha256 == fingerprints[1].schema_sha256


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
    assert summary.schema_sha256 == expected.schema_sha256
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
    assert verified.baseline["source_connection"]["database"] == "corridor"
    assert verified.baseline["source_connection"]["username"] == "corridor"
    assert verified.baseline["source_connection"]["system_identifier"].isdigit()

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


def test_capture_refuses_same_head_source_schema_drift(tmp_path):
    def with_schema(definition_sha256: str) -> DatabaseFingerprint:
        base = _fingerprint("1")
        objects = (
            SchemaObjectFingerprint(
                "function", "sealed()", definition_sha256
            ),
        )
        canonical = {
            "tables": [item.as_dict() for item in base.tables],
            "sequences": [item.as_dict() for item in base.sequences],
            "schema_objects": [item.as_dict() for item in objects],
        }
        return replace(
            base,
            state_sha256=sha256(
                json.dumps(
                    canonical,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
            schema_objects=objects,
        )

    source = with_schema("1" * 64)
    freshly_migrated = with_schema("2" * 64)
    reads = iter((source, freshly_migrated))

    with pytest.raises(ProductProvingDatabaseError, match="does not match"):
        capture_product_proving_database_baseline(
            _capture_config(tmp_path),
            provision_database=_provisioner(),
            open_environment=_opener(_FakeEnvironment(MIGRATION_HEAD, [])),
            fingerprint_database=lambda _url: next(reads),
        )

    assert not (tmp_path / "baseline").exists()


def test_capture_refuses_an_arbitrary_local_clone_as_the_claimed_source(tmp_path):
    clone_url = SOURCE_URL.rsplit("/", 1)[0] + "/corridor_clone"
    config = replace(
        _capture_config(tmp_path),
        source_database_url=clone_url,
    )

    with pytest.raises(
        ProductProvingDatabaseError,
        match="configured shared development database",
    ):
        capture_product_proving_database_baseline(
            config,
            open_environment=lambda **_kwargs: pytest.fail(
                "clone identity reached environment open"
            ),
        )


def test_capture_refuses_when_the_url_reaches_a_different_database_role(tmp_path):
    observed = DatabaseConnectionIdentity(
        database="corridor",
        username="different_role",
        server_address="127.0.0.1",
        server_port="5432",
        system_identifier="123456789",
        postgres_version="16.10",
    )

    with pytest.raises(
        ProductProvingDatabaseError,
        match="named database and role",
    ):
        capture_product_proving_database_baseline(
            _capture_config(tmp_path),
            open_environment=lambda **_kwargs: pytest.fail(
                "mismatched live role reached environment open"
            ),
            observe_connection_identity=lambda _url: observed,
        )


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


def test_shared_restore_refuses_a_different_postgresql_cluster_identity(tmp_path):
    captured, _baseline, _environment = _capture_fake_baseline(tmp_path)
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
    different_cluster = DatabaseConnectionIdentity(
        database="corridor",
        username="corridor",
        server_address="127.0.0.1",
        server_port="5432",
        system_identifier="9999999999999999999",
        postgres_version="16.10",
    )

    with pytest.raises(ProductProvingDatabaseError, match="server identity changed"):
        restore_shared_development_database(
            config,
            observe_connection_identity=lambda _url: different_cluster,
        )


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
            previous_database_oid=request.source_database_oid,
            restored_database_oid=request.replacement_database_oid,
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
    assert summary.operation_id
    assert summary.started_at <= summary.completed_at
    assert summary.previous_database_oid == 101
    assert summary.restored_database_oid == 202


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
            "postgresql+psycopg://corridor:corridor@localhost:5433/"
            "corridor_proving_restore_fake",
            "corridor_proving_restore_fake",
            "not an allowed exact target",
        ),
        (
            "postgresql+psycopg://corridor:corridor@localhost:5433/"
            "corridor_pre_proving_fake",
            "corridor_pre_proving_fake",
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


def test_shared_restore_refuses_a_safe_but_unconfigured_clone_name(tmp_path):
    clone_url = SOURCE_URL.rsplit("/", 1)[0] + "/corridor_clone"
    config = SharedDevelopmentRestoreConfig(
        source_database_url=clone_url,
        postgres_admin_url=ADMIN_URL,
        expected_source_database_name="corridor_clone",
        expected_checkout_revision=REVISION,
        expected_migration_head=MIGRATION_HEAD,
        bundle_dir=tmp_path / "unused",
        expected_manifest_sha256="f" * 64,
        repo_root=tmp_path,
        allow_shared_development_restore=True,
    )

    with pytest.raises(
        ProductProvingDatabaseError,
        match="configured shared development database",
    ):
        restore_shared_development_database(config)
