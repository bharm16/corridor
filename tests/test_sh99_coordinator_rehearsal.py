"""Public acceptance-bundle coverage for the bounded SH 99 coordinator rehearsal."""

from __future__ import annotations

import json

from corridor.sh99_coordinator_rehearsal import (
    BUNDLE_SCHEMA_VERSION,
    CoordinatorRehearsalCapture,
    publish_coordinator_rehearsal_bundle,
    verify_coordinator_rehearsal_bundle,
)


def test_assisted_rehearsal_is_sealed_but_never_called_an_unqualified_pass(tmp_path):
    """A release control needing a technical identifier is an honest failed rehearsal."""

    summary = publish_coordinator_rehearsal_bundle(
        tmp_path / "bundle",
        CoordinatorRehearsalCapture(
            inputs={
                "source_revision": "a" * 40,
                "source_snapshot_sha256": "b" * 64,
                "shared_admission_receipt": {
                    "sha256": "c" * 64,
                    "approval_comment_url": "https://example.test/approval",
                },
                "corpus_inputs": [{"document_id": 11, "sha256": "e" * 64}],
                "active_runs": [{"document_id": 11, "run_id": 21}],
                "policy_runs": [{"id": 31, "policy_sha256": "f" * 64}],
                "seeded_coordinator": {"subject": "local:sh99-coordinator"},
                "scenario_candidates": {"7296": 7296, "7129": 7129, "7587": 7587},
            },
            operations={"elapsed_seconds": 17.5, "backfill_elapsed_seconds": 291.0},
            coordinator={
                "elapsed_seconds": 42.0,
                "scenario_timings": {"7296": 20.0, "7129_and_release": 22.0},
                "interactions": ["coordinator_home", "render_report", "release_report"],
            },
            outcome={
                "status": "failed",
                "assistance": ["release required an artifact identity not exposed by a coordinator screen"],
                "errors": [],
                "deviations": [],
                "released_pdf": {"sha256": "1" * 64, "release_id": 81},
            },
            verification={"candidate_7587": {"admission_outcome": "abstained"}},
        ),
    )

    verified = verify_coordinator_rehearsal_bundle(
        summary.bundle_dir,
        expected_integrity_manifest_sha256=summary.integrity_manifest_sha256,
    )
    receipt = json.loads((summary.bundle_dir / "receipt.json").read_text())

    assert verified.valid is True
    assert receipt["schema_version"] == BUNDLE_SCHEMA_VERSION
    assert receipt["outcome"]["status"] == "failed"
    assert receipt["outcome"]["unqualified_pass"] is False
    assert receipt["claim_boundary"] == {
        "internal_workflow_rehearsal": True,
        "customer_usability_validation": False,
        "provisional_targets": True,
    }
    assert receipt["operations"]["elapsed_seconds"] == 17.5
    assert receipt["coordinator"]["elapsed_seconds"] == 42.0
    assert receipt["outcome"]["released_pdf"]["sha256"] == "1" * 64
