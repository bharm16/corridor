"""Public acceptance-bundle coverage for the bounded SH 99 coordinator rehearsal."""

from __future__ import annotations

import json
from types import SimpleNamespace

import fitz
import pytest

from corridor.sh99_coordinator_rehearsal import (
    BUNDLE_SCHEMA_VERSION,
    CoordinatorRehearsalCapture,
    CorruptSH99CoordinatorRehearsalBundle,
    _require_report_pdf_contents,
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
                "source_migration_head": "c0ffee",
                "environment_details": {"python_version": "3.12.0"},
                "shared_admission_receipt": {
                    "sha256": "c" * 64,
                    "approval_comment_url": "https://example.test/approval",
                },
                "corpus_inputs": [{"document_id": 11, "sha256": "e" * 64}],
                "active_runs": [{"document_id": 11, "run_id": 21}],
                "policy_identities": [{"run_id": 31, "policy_sha256": "f" * 64}],
                "report_publication": {
                    "ruleset_version": "v0.4",
                    "provenance_mode": "all-supported-sources",
                },
                "seeded_coordinator": {"subject": "local:sh99-coordinator"},
                "scenario_candidates": {"7296": 7296, "7129": 7129, "7587": 7587},
            },
            operations={"elapsed_seconds": 17.5, "backfill_elapsed_seconds": 291.0},
            coordinator={
                "elapsed_seconds": 42.0,
                "scenario_timings": {"7296": 20.0, "7129_and_release": 22.0},
                "interactions": ["coordinator_home", "render_report", "release_report"],
                "retries": [],
            },
            outcome={
                "status": "failed",
                "assistance": ["release required an artifact identity not exposed by a coordinator screen"],
                "errors": [],
                "deviations": [],
                "released_pdf": {
                    "sha256": "f0a9624cf25cbb23e2d237e385e3bebc363ae45d1f99e93416f466ebbd451737",
                    "release_id": 81,
                    "artifact_name": "sh99.pdf",
                },
            },
            verification={
                "candidate_7587": {"admission_outcome": "abstained"},
                "release": {
                    "release_id": 81,
                    "artifact_name": "sh99.pdf",
                    "sha256": "f0a9624cf25cbb23e2d237e385e3bebc363ae45d1f99e93416f466ebbd451737",
                    "evaluated_on": "2026-08-13",
                    "ruleset_version": "v0.4",
                    "provenance_mode": "all-supported-sources",
                    "released_by": "local:sh99-coordinator",
                    "released_at": "2026-08-13T00:00:00+00:00",
                    "record_context": {"party_statements": []},
                    "evaluation_context": {"evaluated_on": "2026-08-13"},
                },
            },
            released_pdf_bytes=b"%PDF-1.4\nfixed sh99 report\n",
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
    assert receipt["outcome"]["released_pdf"]["sha256"] == (
        "f0a9624cf25cbb23e2d237e385e3bebc363ae45d1f99e93416f466ebbd451737"
    )
    assert (summary.bundle_dir / "released-report.pdf").read_bytes() == (
        b"%PDF-1.4\nfixed sh99 report\n"
    )


def test_verify_refuses_tampered_fixed_pdf_bytes(tmp_path):
    """A byte change after publication cannot retain a valid release receipt."""

    summary = publish_coordinator_rehearsal_bundle(
        tmp_path / "bundle",
        CoordinatorRehearsalCapture(
            inputs={
                "source_revision": "a" * 40,
                "source_snapshot_sha256": "b" * 64,
                "source_migration_head": "c0ffee",
                "environment_details": {"python_version": "3.12.0"},
                "shared_admission_receipt": {"sha256": "c" * 64},
                "corpus_inputs": [],
                "active_runs": [],
                "policy_identities": [],
                "report_publication": {"ruleset_version": "v0.4"},
                "seeded_coordinator": {"subject": "local:sh99-coordinator"},
                "scenario_candidates": {"7296": 7296, "7129": 7129, "7587": 7587},
            },
            operations={"elapsed_seconds": 1.0, "backfill_elapsed_seconds": 291.0},
            coordinator={
                "elapsed_seconds": 1.0,
                "scenario_timings": {},
                "interactions": [],
                "retries": [],
            },
            outcome={
                "status": "failed",
                "assistance": [],
                "errors": ["coordinator home did not distinguish a statement"],
                "deviations": [],
                "released_pdf": None,
            },
            verification={"valid": False},
        ),
    )

    (summary.bundle_dir / "released-report.pdf").write_bytes(b"not a PDF")

    try:
        verify_coordinator_rehearsal_bundle(
            summary.bundle_dir,
            expected_integrity_manifest_sha256=summary.integrity_manifest_sha256,
        )
    except CorruptSH99CoordinatorRehearsalBundle as exc:
        assert "released-report.pdf does not match its digest" in str(exc)
    else:
        raise AssertionError("tampered PDF passed bundle verification")


def test_retained_pdf_check_requires_each_party_report_field():
    """The byte-pinned PDF must also retain the fields named by its receipt."""

    statement = SimpleNamespace(
        event=SimpleNamespace(
            event_type="committed_date_change",
            timing_direction="later",
            stated_party="Kinder Morgan",
            description="Kinder Morgan moved completion to May 16th.",
        ),
        timings=(
            SimpleNamespace(text="March 2026", precision="month"),
            SimpleNamespace(text="May 16th", precision="day"),
        ),
        plan=SimpleNamespace(
            internal_owner="SH 99 Coordinator",
            next_action="Confirm the revised plan",
            action_due_date=None,
            next_action_decision=object(),
            milestone_impact="not_yet_known",
        ),
    )
    text = "\n".join(
        (
            "External Party commitments",
            "External Party",
            "Supported statement",
            "Timing",
            "Timing precision",
            "Statement type",
            "Commitment Scope",
            "Open / past-due status",
            "Internal Owner",
            "Next Action",
            "Action Due",
            "Milestone Impact",
            "Scope not yet known",
            "Kinder Morgan",
            "Kinder Morgan moved completion to May 16th.",
            "Committed Date Change · later",
            "March 2026",
            "May 16th",
            "month",
            "day",
            "SH 99 Coordinator",
            "Confirm the revised plan",
            "Date not yet known",
            "Not yet known",
        )
    )
    document = fitz.open()
    page = document.new_page()
    page.insert_textbox(fitz.Rect(36, 36, 559, 806), text, fontsize=9)
    pdf_bytes = document.tobytes()
    document.close()

    _require_report_pdf_contents(pdf_bytes, (statement,))
    incomplete = fitz.open()
    incomplete_page = incomplete.new_page()
    incomplete_page.insert_text((36, 36), "External Party commitments", fontsize=9)
    incomplete_bytes = incomplete.tobytes()
    incomplete.close()
    with pytest.raises(ValueError, match="omits required Report fields"):
        _require_report_pdf_contents(incomplete_bytes, (statement,))
