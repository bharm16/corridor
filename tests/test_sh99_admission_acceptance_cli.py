"""The operator-facing, digest-pinned SH 99 Admission acceptance boundary."""

from __future__ import annotations

import json
from pathlib import Path

from corridor.m8_acceptance_bundle import VerificationResult
from corridor.sh99_admission_acceptance import SH99AdmissionAcceptanceSummary
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
