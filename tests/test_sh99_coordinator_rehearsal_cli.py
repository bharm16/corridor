"""Operator-facing routing for the bounded SH 99 coordinator rehearsal."""

from __future__ import annotations

import json

from corridor.m8_acceptance_bundle import VerificationResult
from corridor.sh99_coordinator_rehearsal import (
    CoordinatorRehearsalBundleSummary,
    SH99CoordinatorRehearsalSummary,
)
from corridor.sh99_coordinator_rehearsal_cli import main


def test_replay_pins_the_prior_admission_receipt_and_shared_operations_time(
    monkeypatch, tmp_path, capsys
):
    bundle = tmp_path / "bundle"
    admission_bundle = tmp_path / "admission"
    seen = []

    def replay(config):
        seen.append(config)
        return SH99CoordinatorRehearsalSummary(
            bundle=CoordinatorRehearsalBundleSummary(
                bundle_dir=bundle,
                manifest_path=bundle / "manifest.json",
                integrity_manifest_sha256="11" * 32,
                canonical_content_sha256="22" * 32,
            ),
            database_name="corridor_sh99_coordinator_rehearsal_test",
            status="failed",
        )

    monkeypatch.setattr(
        "corridor.sh99_coordinator_rehearsal_cli.run_sh99_coordinator_rehearsal",
        replay,
    )

    assert main(
        [
            "replay",
            "--project-slug",
            "sh99-grand-parkway",
            "--source-database-url",
            "postgresql+psycopg://corridor:corridor@localhost:5433/corridor",
            "--postgres-admin-url",
            "postgresql+psycopg://corridor:corridor@localhost:5433/corridor",
            "--expected-clean-git-revision",
            "33" * 13 + "3",
            "--expected-source-migration-head",
            "e255a7c4d9e2",
            "--expected-target-migration-head",
            "f255b7c4d9e3",
            "--shared-admission-receipt-path",
            str(admission_bundle / "validation-passed.json"),
            "--expected-shared-admission-receipt-sha256",
            "44" * 32,
            "--approved-shared-state-receipt",
            "https://example.test/receipt",
            "--shared-backfill-elapsed-seconds",
            "291",
            "--output-dir",
            str(bundle),
        ]
    ) == 0

    [config] = seen
    assert config.shared_admission_receipt_path == admission_bundle / "validation-passed.json"
    assert config.expected_source_migration_head == "e255a7c4d9e2"
    assert config.expected_target_migration_head == "f255b7c4d9e3"
    assert config.expected_shared_admission_receipt_sha256 == "44" * 32
    assert config.shared_backfill_elapsed_seconds == 291.0
    assert config.approved_shared_state_receipt == "https://example.test/receipt"
    assert json.loads(capsys.readouterr().out) == {
        "bundle_dir": str(bundle),
        "canonical_content_sha256": "22" * 32,
        "command": "replay",
        "database_name": "corridor_sh99_coordinator_rehearsal_test",
        "integrity_manifest_sha256": "11" * 32,
        "status": "failed",
    }


def test_verify_is_database_free(monkeypatch, tmp_path, capsys):
    bundle = tmp_path / "bundle"
    seen = []

    def verify(path, *, expected_integrity_manifest_sha256):
        seen.append((path, expected_integrity_manifest_sha256))
        return VerificationResult(
            valid=True,
            integrity_manifest_sha256="55" * 32,
            canonical_content_sha256="66" * 32,
        )

    monkeypatch.setattr(
        "corridor.sh99_coordinator_rehearsal_cli.verify_coordinator_rehearsal_bundle",
        verify,
    )

    assert main(
        ["verify", str(bundle), "--expected-manifest-sha256", "77" * 32]
    ) == 0

    assert seen == [(bundle, "77" * 32)]
    assert json.loads(capsys.readouterr().out) == {
        "bundle_dir": str(bundle),
        "canonical_content_sha256": "66" * 32,
        "command": "verify",
        "integrity_manifest_sha256": "55" * 32,
        "valid": True,
    }
