"""The operator-facing, digest-pinned SH 99 Admission acceptance boundary."""

from __future__ import annotations

import json
from pathlib import Path

from corridor.m8_acceptance_bundle import VerificationResult
from corridor.sh99_admission_acceptance import (
    SH99AdmissionAcceptanceSummary,
    SH99SharedAdmissionSealSummary,
)
from corridor.sh99_admission_acceptance_cli import main


def _output(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_replay_receives_the_real_project_and_source_database(
    monkeypatch, tmp_path, capsys
):
    bundle = tmp_path / "bundle"
    seen = []

    def replay(config):
        seen.append(config)
        return SH99AdmissionAcceptanceSummary(
            bundle_dir=bundle,
            manifest_path=bundle / "manifest.json",
            integrity_manifest_sha256="11" * 32,
            canonical_content_sha256="22" * 32,
            database_name="corridor_sh99_admission_acceptance_test",
        )

    monkeypatch.setattr(
        "corridor.sh99_admission_acceptance_cli.run_sh99_admission_acceptance",
        replay,
    )

    assert main(
        [
            "replay",
            "--project-slug",
            "sh99-grand-parkway",
            "--source-database-url",
            "postgresql+psycopg://corridor:corridor@localhost:5433/corridor",
            "--expected-clean-git-revision",
            "44" * 10,
            "--output-dir",
            str(bundle),
            "--postgres-admin-url",
            "postgresql+psycopg://corridor:corridor@localhost:5433/corridor",
        ]
    ) == 0

    [config] = seen
    assert config.project_slug == "sh99-grand-parkway"
    assert config.source_database_url.endswith("/corridor")
    assert config.expected_clean_git_revision == "44" * 10
    assert config.output_dir == bundle


def test_verify_uses_only_the_public_database_free_verifier(monkeypatch, tmp_path, capsys):
    bundle = tmp_path / "bundle"
    seen = []

    def verify(path, *, expected_integrity_manifest_sha256):
        seen.append((path, expected_integrity_manifest_sha256))
        return VerificationResult(
            valid=True,
            integrity_manifest_sha256="44" * 32,
            canonical_content_sha256="55" * 32,
        )

    monkeypatch.setattr("corridor.sh99_admission_acceptance_cli.verify_sh99_admission_bundle", verify)

    assert main(
        [
            "verify",
            str(bundle),
            "--expected-manifest-sha256",
            "66" * 32,
        ]
    ) == 0
    assert seen == [(bundle, "66" * 32)]
    assert _output(capsys) == {
        "bundle_dir": str(bundle),
        "canonical_content_sha256": "55" * 32,
        "command": "verify",
        "integrity_manifest_sha256": "44" * 32,
        "valid": True,
    }


def test_seal_receives_the_approved_shared_operation_pins(
    monkeypatch, tmp_path, capsys
):
    bundle = tmp_path / "shared-seal"
    seen = []

    def seal(config):
        seen.append(config)
        return SH99SharedAdmissionSealSummary(
            bundle_dir=bundle,
            manifest_path=bundle / "manifest.json",
            integrity_manifest_sha256="77" * 32,
            canonical_content_sha256="88" * 32,
            database_name="corridor_sh99_shared_admission_seal_test",
        )

    monkeypatch.setattr(
        "corridor.sh99_admission_acceptance_cli.run_sh99_shared_admission_seal",
        seal,
    )

    assert main(
        [
            "seal",
            "--project-slug",
            "sh99-grand-parkway",
            "--source-database-url",
            "postgresql+psycopg://corridor:corridor@localhost:5433/corridor",
            "--expected-clean-git-revision",
            "44" * 10,
            "--output-dir",
            str(bundle),
            "--postgres-admin-url",
            "postgresql+psycopg://corridor:corridor@localhost:5433/corridor",
            "--expected-acceptance-receipt-id",
            "156",
            "--expected-acceptance-receipt-sha256",
            "99" * 32,
            "--expected-activation-id",
            "140",
            "--expected-active-run",
            "1435:193811",
            "--expected-active-run",
            "1438:193812",
            "--expected-candidate-id",
            "405519",
        ]
    ) == 0

    [config] = seen
    assert config.expected_acceptance_receipt_id == 156
    assert config.expected_acceptance_receipt_sha256 == "99" * 32
    assert config.expected_activation_id == 140
    assert config.expected_active_runs == ((1435, 193811), (1438, 193812))
    assert config.expected_candidate_id == 405519
    assert _output(capsys) == {
        "bundle_dir": str(bundle),
        "canonical_content_sha256": "88" * 32,
        "command": "seal",
        "database_name": "corridor_sh99_shared_admission_seal_test",
        "integrity_manifest_sha256": "77" * 32,
        "manifest_path": str(bundle / "manifest.json"),
        "shared_database_mutated": False,
    }


def test_verify_seal_uses_the_database_free_seal_verifier(
    monkeypatch, tmp_path, capsys
):
    bundle = tmp_path / "shared-seal"
    seen = []

    def verify(path, *, expected_integrity_manifest_sha256):
        seen.append((path, expected_integrity_manifest_sha256))
        return VerificationResult(
            valid=True,
            integrity_manifest_sha256="aa" * 32,
            canonical_content_sha256="bb" * 32,
        )

    monkeypatch.setattr(
        "corridor.sh99_admission_acceptance_cli."
        "verify_sh99_shared_admission_seal_bundle",
        verify,
    )

    assert main(
        [
            "verify-seal",
            str(bundle),
            "--expected-manifest-sha256",
            "cc" * 32,
        ]
    ) == 0
    assert seen == [(bundle, "cc" * 32)]
    assert _output(capsys) == {
        "bundle_dir": str(bundle),
        "canonical_content_sha256": "bb" * 32,
        "command": "verify-seal",
        "integrity_manifest_sha256": "aa" * 32,
        "valid": True,
    }
