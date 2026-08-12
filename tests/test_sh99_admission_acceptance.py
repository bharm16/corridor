"""SH 99 Admission rehearsal through its public, isolated acceptance boundary."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
from contextlib import contextmanager

import pytest

from corridor.config import settings
from corridor.m8_acceptance_database import ProvisionedDatabase
from corridor.sh99_admission_acceptance import (
    CorruptSH99AdmissionBundle,
    SH99AdmissionAcceptanceConfig,
    run_sh99_admission_acceptance,
    verify_sh99_admission_bundle,
)


FIXTURE = (
    Path(__file__).parent / "fixtures" / "sh99_admission_acceptance" / "v1" / "snapshot.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _config(output_dir: Path) -> SH99AdmissionAcceptanceConfig:
    return SH99AdmissionAcceptanceConfig(
        snapshot_path=FIXTURE,
        expected_snapshot_sha256=_sha256(FIXTURE),
        expected_clean_git_revision=_git_revision(),
        output_dir=output_dir,
        postgres_admin_url=settings.database_url,
    )


def _receipt(bundle_dir: Path) -> dict:
    return json.loads((bundle_dir / "receipt.json").read_bytes())


def _git_revision() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        cwd=Path(__file__).parents[1],
    ).stdout.strip()


def test_isolated_rehearsal_writes_a_digest_pinned_exact_receipt(tmp_path):
    summary = run_sh99_admission_acceptance(_config(tmp_path / "bundle"))

    verified = verify_sh99_admission_bundle(
        summary.bundle_dir,
        expected_integrity_manifest_sha256=summary.integrity_manifest_sha256,
    )
    assert verified.valid is True
    assert verified.canonical_content_sha256 == summary.canonical_content_sha256

    receipt = _receipt(summary.bundle_dir)
    assert receipt["input_pins"] == {
        "snapshot_sha256": _sha256(FIXTURE),
        "source_revision": "4864d6cebf22b7a16f87c59faefab4a508f21d26",
        "database_snapshot": {
            "identity": "sh99-admission-bounded-rehearsal-v1",
            "captured_at": "2026-08-12T00:00:00+00:00",
            "migration_head": "b230e4f5a6b7",
        },
        "corpus_inputs": [
            {
                "registry_id": "sh99-utility-owner-minutes-2025-01",
                "sha256": "9e8c89f3a6ea0e86e5ce88b00a788c3c35cfda5453ea88f6956c7a43924652d3",
            }
        ],
        "declared_active_runs": [
            {
                "registry_id": "sh99-utility-owner-minutes-2025-01",
                "prompt_version": "sh99-admission-fixture-v1",
                "model": "captured-sh99-input",
                "schema_version": "event-candidate-shape-v1",
            }
        ],
        "policy": {
            "family": "event-admission",
            "version": "event-admission-v2",
            "abstention_reason_version": "event-admission-abstentions-v2",
        },
    }
    assert receipt["source"]["migration_head"] == receipt["database"][
        "migration_head"
    ]
    assert receipt["before"]["candidates"] == [
        {"id": 7129, "state": "pending"},
        {"id": 7296, "state": "pending"},
        {"id": 7587, "state": "pending"},
        {"id": 7600, "state": "pending"},
    ]
    assert receipt["first_run"]["outcomes"] == [
        {
            "candidate_id": 7129,
            "dependency_event_id": None,
            "outcome": "abstained",
            "reason": "no_conflict_reference",
            "created_record_identities": {
                "dependency_events": [],
                "dependency_event_scopes": [],
                "dependency_event_timings": [],
                "event_admission_audits": [],
            },
        },
        {
            "candidate_id": 7296,
            "dependency_event_id": None,
            "outcome": "abstained",
            "reason": "no_conflict_reference",
            "created_record_identities": {
                "dependency_events": [],
                "dependency_event_scopes": [],
                "dependency_event_timings": [],
                "event_admission_audits": [],
            },
        },
        {
            "candidate_id": 7587,
            "dependency_event_id": None,
            "outcome": "abstained",
            "reason": "party_unstated",
            "created_record_identities": {
                "dependency_events": [],
                "dependency_event_scopes": [],
                "dependency_event_timings": [],
                "event_admission_audits": [],
            },
        },
        {
            "candidate_id": 7600,
            "dependency_event_id": 2,
            "outcome": "admitted",
            "reason": None,
            "created_record_identities": {
                "dependency_events": [2],
                "dependency_event_scopes": [1],
                "dependency_event_timings": [1],
                "event_admission_audits": [1],
            },
        },
    ]
    assert receipt["first_run"]["created_record_identities"] == {
            "dependency_events": [2],
            "dependency_event_scopes": [1],
            "dependency_event_timings": [1],
        "event_admission_audits": [1],
    }
    assert receipt["second_run"]["created_record_identities"] == {
        "dependency_events": [],
        "dependency_event_scopes": [],
        "dependency_event_timings": [],
        "event_admission_audits": [],
    }
    assert receipt["after_second_run"]["candidates"] == [
        {"id": 7129, "state": "pending"},
        {"id": 7296, "state": "pending"},
        {"id": 7587, "state": "pending"},
        {"id": 7600, "state": "accepted"},
    ]
    assert receipt["negative_cases"] == {
        "7587": {
            "admission_abstention_reason": "party_unstated",
            "created_statement_ids": [],
            "created_scope_ids": [],
            "projected_committed_dates": [],
        },
        "7129": {
            "state": "pending",
            "created_statement_ids": [],
            "created_scope_ids": [],
        },
        "7296": {
            "state": "pending",
            "created_statement_ids": [],
            "created_scope_ids": [],
        },
    }
    assert receipt["late_refusal"] == {
        "raised": True,
        "ledger_and_candidate_state_unchanged": True,
        "policy_run_ids_created": [],
    }
    assert receipt["human_approval_gate"] == (
        "No shared SH 99 database was changed. A designated human must separately "
        "approve any shared-database Admission operation."
    )


def test_verifier_rejects_receipt_tampering_without_a_database(tmp_path):
    summary = run_sh99_admission_acceptance(_config(tmp_path / "bundle"))
    receipt_path = summary.bundle_dir / "receipt.json"
    receipt = json.loads(receipt_path.read_bytes())
    receipt["human_approval_gate"] = "mutated"
    receipt_path.write_text(json.dumps(receipt))

    with pytest.raises(CorruptSH99AdmissionBundle, match="does not match its digest"):
        verify_sh99_admission_bundle(
            summary.bundle_dir,
            expected_integrity_manifest_sha256=summary.integrity_manifest_sha256,
        )


def test_runner_rejects_an_unpinned_snapshot_before_provisioning(tmp_path):
    config = SH99AdmissionAcceptanceConfig(
        snapshot_path=FIXTURE,
        expected_snapshot_sha256="0" * 64,
        expected_clean_git_revision=_git_revision(),
        output_dir=tmp_path / "bundle",
        postgres_admin_url=settings.database_url,
    )

    with pytest.raises(ValueError, match="snapshot digest"):
        run_sh99_admission_acceptance(config)
    assert not config.output_dir.exists()


def test_runner_refuses_a_snapshot_without_declared_active_runs(tmp_path):
    snapshot = json.loads(FIXTURE.read_bytes())
    snapshot.pop("declared_active_runs")
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snapshot))
    config = SH99AdmissionAcceptanceConfig(
        snapshot_path=path,
        expected_snapshot_sha256=_sha256(path),
        expected_clean_git_revision=_git_revision(),
        output_dir=tmp_path / "bundle",
        postgres_admin_url=settings.database_url,
    )

    with pytest.raises(ValueError, match="declared Active Runs"):
        run_sh99_admission_acceptance(config)
    assert not config.output_dir.exists()


def test_runner_refuses_an_unavailable_pinned_source_revision(tmp_path):
    snapshot = json.loads(FIXTURE.read_bytes())
    snapshot["source_revision"] = "0" * 40
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snapshot))
    config = SH99AdmissionAcceptanceConfig(
        snapshot_path=path,
        expected_snapshot_sha256=_sha256(path),
        expected_clean_git_revision=_git_revision(),
        output_dir=tmp_path / "bundle",
        postgres_admin_url=settings.database_url,
    )

    with pytest.raises(ValueError, match="source revision is unavailable"):
        run_sh99_admission_acceptance(config)
    assert not config.output_dir.exists()


def test_runner_refuses_a_disposable_database_that_is_not_at_source_head(tmp_path):
    config = SH99AdmissionAcceptanceConfig(
        snapshot_path=FIXTURE,
        expected_snapshot_sha256=_sha256(FIXTURE),
        expected_clean_git_revision=_git_revision(),
        output_dir=tmp_path / "bundle",
        postgres_admin_url=settings.database_url,
    )

    @contextmanager
    def provision_wrong_head(_admin_url):
        yield ProvisionedDatabase(
            name="corridor_sh99_admission_acceptance_wrong_head",
            session_factory=None,  # type: ignore[arg-type]
            postgres_version="16.14",
            migration_head="released-but-not-current",
        )

    with pytest.raises(ValueError, match="migration head does not match"):
        run_sh99_admission_acceptance(config, provision_database=provision_wrong_head)
    assert not config.output_dir.exists()
