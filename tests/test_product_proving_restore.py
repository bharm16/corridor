"""Product Proving restore receipts close each pass-to-baseline state edge."""

from types import SimpleNamespace

import pytest

from corridor.product_proving_database import (
    BACKUP_DATABASE_PREFIX,
    DatabaseFingerprint,
    SharedDevelopmentRestoreSummary,
)
from corridor.product_proving_restore import (
    CorruptProductProvingRestore,
    publish_product_proving_restore_bundle,
    verify_product_proving_restore_bundle,
)


def _inputs(pass_number=1):
    terminal = "7" * 64
    baseline_state = "f" * 64
    frontend = SimpleNamespace(
        valid=True,
        product_proving_pass=SimpleNamespace(pass_number=pass_number),
        integrity_manifest_sha256="3" * 64,
        canonical_content_sha256="4" * 64,
        terminal_database_state_sha256=terminal,
        execution_id="22222222-2222-4222-8222-222222222222",
        database_baseline_manifest_sha256="1" * 64,
        database_baseline_dump_sha256="2" * 64,
        database_baseline_state_sha256=baseline_state,
        database_baseline_schema_sha256=DatabaseFingerprint(
            (), (), baseline_state
        ).schema_sha256,
        database_source_identity={
            "backend": "postgresql",
            "host": "localhost",
            "port": 5433,
            "database": "corridor",
            "username": "corridor",
        },
        database_source_connection_identity=None,
    )
    source_connection = {
        "database": "corridor",
        "username": "corridor",
        "server_address": "127.0.0.1",
        "server_port": "5432",
        "system_identifier": "123456789",
        "postgres_version": "16.10",
    }
    frontend.database_source_connection_identity = source_connection
    baseline = SimpleNamespace(
        manifest_sha256="1" * 64,
        dump_sha256="2" * 64,
        fingerprint=DatabaseFingerprint((), (), baseline_state),
        baseline={
            "source_database": frontend.database_source_identity,
            "source_connection": source_connection,
        },
    )
    restore = SharedDevelopmentRestoreSummary(
        source_database_name="corridor",
        previous_state_sha256=terminal,
        restored_state_sha256=baseline_state,
        manifest_sha256=baseline.manifest_sha256,
        dump_sha256=baseline.dump_sha256,
        verification_database_name="corridor_pp_restore_test",
        backup_database_name=f"{BACKUP_DATABASE_PREFIX}test",
        operation_id="11111111-1111-4111-8111-111111111111",
        started_at="2026-08-26T12:00:00+00:00",
        completed_at="2026-08-26T12:00:01+00:00",
        previous_database_oid=1001,
        restored_database_oid=1002,
    )
    return frontend, baseline, restore


def test_restore_bundle_seals_terminal_pass_to_exact_baseline(tmp_path):
    frontend, baseline, restore = _inputs()
    summary = publish_product_proving_restore_bundle(
        tmp_path / "restore",
        frontend_pass=frontend,
        baseline=baseline,
        restore=restore,
    )

    verified = verify_product_proving_restore_bundle(
        summary.bundle_dir,
        expected_integrity_manifest_sha256=summary.integrity_manifest_sha256,
    )

    assert verified.valid
    assert verified.receipt.pass_number == 1
    assert verified.receipt.previous_state_sha256 == "7" * 64
    assert verified.receipt.restored_state_sha256 == "f" * 64
    assert (
        verified.receipt.restore_operation_id
        == "11111111-1111-4111-8111-111111111111"
    )
    assert verified.receipt.source_connection_identity["system_identifier"] == (
        "123456789"
    )


def test_republishing_one_restore_cannot_create_a_second_operation(tmp_path):
    frontend, baseline, restore = _inputs()
    first = publish_product_proving_restore_bundle(
        tmp_path / "first",
        frontend_pass=frontend,
        baseline=baseline,
        restore=restore,
    )
    second = publish_product_proving_restore_bundle(
        tmp_path / "second",
        frontend_pass=frontend,
        baseline=baseline,
        restore=restore,
    )

    assert first.canonical_content_sha256 == second.canonical_content_sha256


def test_restore_bundle_refuses_wrong_start_and_detects_tampering(tmp_path):
    frontend, baseline, restore = _inputs()
    wrong = SimpleNamespace(**{**restore.__dict__, "previous_state_sha256": "8" * 64})
    with pytest.raises(ValueError, match="sealed frontend pass state"):
        publish_product_proving_restore_bundle(
            tmp_path / "wrong",
            frontend_pass=frontend,
            baseline=baseline,
            restore=wrong,
        )

    summary = publish_product_proving_restore_bundle(
        tmp_path / "restore",
        frontend_pass=frontend,
        baseline=baseline,
        restore=restore,
    )
    (summary.bundle_dir / "receipt.json").write_text("{}")
    with pytest.raises(CorruptProductProvingRestore):
        verify_product_proving_restore_bundle(
            summary.bundle_dir,
            expected_integrity_manifest_sha256=summary.integrity_manifest_sha256,
        )
