"""Public contract for the bounded raw-Document Product Proving Run."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json

import pytest

from corridor.product_proving_run import (
    CandidateSetComparison,
    CorruptProductProvingBundle,
    ExpectedPreflight,
    ObservedPreflight,
    ProductProvingCapture,
    ProductProvingPass,
    compare_candidate_sets,
    publish_product_proving_bundle,
    verify_preflight,
    verify_product_proving_bundle,
    verify_two_pass_capture,
)


def _candidate(
    candidate_id: int,
    *,
    event_type: str = "commitment",
    party: str = "Equistar",
    page: int = 2,
    quote: str = "Equistar will provide the easement by January 2025.",
) -> dict:
    return {
        "candidate_id": candidate_id,
        "project_id": 1266,
        "kind": "event",
        "source_document_id": 1438,
        "payload_json": {
            "kind": "event",
            "fields": {
                "event_type": event_type,
                "external_org": party,
                "description": quote,
                "committed_date": {
                    "precision": "month",
                    "start_date": "2025-01-01",
                    "end_date": "2025-01-31",
                    "text": "01/2025",
                },
            },
            "citations": [
                {
                    "document_id": 1438,
                    "page": page,
                    "quote": quote,
                    "verified": True,
                    "whole_row": False,
                }
            ],
            "confidence": 0.97,
            "dedupe_hint": "Equistar||-",
        },
        "source_pages": [page],
        "confidence": 0.97,
        "prompt_version": "minutes_v3",
        "model": "gpt-5.6-luna",
        "citations_verified": True,
        "state": "pending",
    }


def test_candidate_comparison_ignores_only_identity_order_and_nonsemantic_scores():
    first = _candidate(405522)
    second = _candidate(405523, quote="Equistar will provide the title package.")
    reordered = [
        {**second, "candidate_id": 999002, "confidence": 0.52, "state": "accepted"},
        {**first, "candidate_id": 999001, "confidence": 0.61, "state": "rejected"},
    ]

    comparison = compare_candidate_sets(
        document_id=1438,
        baseline_run_id=193812,
        fresh_run_id=200001,
        baseline=[first, second],
        fresh=reordered,
    )

    assert comparison.equal
    assert comparison.added == ()
    assert comparison.missing == ()


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"event_type": "response"}, "event_type"),
        ({"party": "Kinder Morgan"}, "Kinder Morgan"),
        ({"page": 1}, '"page": 1'),
        ({"quote": "A different exact quote."}, "different exact quote"),
    ],
)
def test_candidate_comparison_stops_on_semantic_addition_and_loss(change, expected):
    comparison = compare_candidate_sets(
        document_id=1438,
        baseline_run_id=193812,
        fresh_run_id=200001,
        baseline=[_candidate(1)],
        fresh=[_candidate(2, **change)],
    )

    assert not comparison.equal
    assert len(comparison.added) == 1
    assert len(comparison.missing) == 1
    assert expected in json.dumps({"added": comparison.added, "missing": comparison.missing})


def _expected_preflight() -> ExpectedPreflight:
    return ExpectedPreflight(
        source_revision="a" * 40,
        origin_main_revision="b" * 40,
        migration_head="a316c5d7e9f1",
        policy_digests={"dependency-admission": "c" * 64},
        documents={1311: "d" * 64, 1435: "e" * 64, 1438: "f" * 64},
        baseline_runs={1311: 117, 1435: 193811, 1438: 193812},
        milestone_sources={
            "DESIGN": "1" * 64,
            "ROW-UTIL-EXEC": "2" * 64,
            "RELO-CONSTR": "3" * 64,
        },
        baseline_fingerprint="4" * 64,
    )


def _observed_preflight() -> ObservedPreflight:
    expected = _expected_preflight()
    return ObservedPreflight(
        source_revision=expected.source_revision,
        origin_main_revision=expected.origin_main_revision,
        clean_worktree=True,
        migration_head=expected.migration_head,
        policy_digests=expected.policy_digests,
        documents=expected.documents,
        baseline_runs=expected.baseline_runs,
        milestone_sources=expected.milestone_sources,
        baseline_fingerprint=expected.baseline_fingerprint,
    )


@pytest.mark.parametrize(
    ("observed", "message"),
    [
        (replace(_observed_preflight(), clean_worktree=False), "clean source checkout"),
        (replace(_observed_preflight(), origin_main_revision="9" * 40), "origin/main"),
        (replace(_observed_preflight(), migration_head="0" * 12), "migration head"),
        (
            replace(_observed_preflight(), policy_digests={"dependency-admission": "0" * 64}),
            "policy digest",
        ),
        (replace(_observed_preflight(), documents={1311: "0" * 64}), "Document identity"),
        (replace(_observed_preflight(), baseline_runs={1311: 118}), "baseline Extraction Run"),
        (replace(_observed_preflight(), milestone_sources={"DESIGN": "0" * 64}), "Milestone source"),
        (replace(_observed_preflight(), baseline_fingerprint="0" * 64), "baseline fingerprint"),
    ],
)
def test_preflight_refuses_every_changed_pin(observed, message):
    with pytest.raises(ValueError, match=message):
        verify_preflight(_expected_preflight(), observed)


def _comparison(document_id: int, baseline: int, fresh: int) -> CandidateSetComparison:
    return CandidateSetComparison(document_id, baseline, fresh, (), (), ())


def _pass(number: int) -> ProductProvingPass:
    pdf_bytes = b"%PDF-1.7\nfixed report\n"
    pdf_sha256 = sha256(pdf_bytes).hexdigest()
    return ProductProvingPass(
        pass_number=number,
        restored_baseline_fingerprint="4" * 64,
        extraction_comparisons=(
            _comparison(1311, 117, 200000 + number),
            _comparison(1435, 193811, 200010 + number),
            _comparison(1438, 193812, 200020 + number),
        ),
        extraction_failures=(),
        admission_completed=True,
        residual_candidate_ids=(10, 11, 12),
        residual_outcomes={10: "supported", 11: "not_relevant", 12: "unresolved"},
        frontend_kind="real_frontend",
        frontend_actions=("open_work_list", "release_approved_export"),
        invalid_action_refused=True,
        invalid_action_write_set={},
        correction_preserved_predecessor=True,
        work_decision_change_preserved_predecessor=True,
        report_pdf_sha256=pdf_sha256,
        approved_export_sha256=pdf_sha256,
        approved_export_bytes=pdf_bytes,
        report_provenance_classes=("Assertion", "Derivation", "Work Decision", "Verbal"),
        write_set={"documents": [], "extraction_runs": [200000 + number]},
        operations_elapsed_seconds=10.0,
        practitioner_elapsed_seconds=20.0,
        non_blocking_friction=("long label",),
        workarounds=(),
    )


def _capture() -> ProductProvingCapture:
    observed = _observed_preflight()
    return ProductProvingCapture(
        expected=_expected_preflight(),
        observed=observed,
        pass_one=_pass(1),
        pass_two=_pass(2),
        final_baseline_fingerprint=observed.baseline_fingerprint,
        simulated_practitioner=True,
        same_project_manual_report_compared=False,
        revision_processing_included=False,
    )


def test_two_pass_capture_requires_frontend_truth_restore_and_equivalent_outputs():
    verify_two_pass_capture(_capture())

    with pytest.raises(ValueError, match="terminal or database workaround"):
        verify_two_pass_capture(
            replace(_capture(), pass_two=replace(_pass(2), workarounds=("psql update",)))
        )
    with pytest.raises(ValueError, match="residual Candidate"):
        verify_two_pass_capture(
            replace(_capture(), pass_two=replace(_pass(2), residual_outcomes={10: "supported"}))
        )
    with pytest.raises(ValueError, match="Approved Export"):
        verify_two_pass_capture(
            replace(_capture(), pass_two=replace(_pass(2), approved_export_sha256="6" * 64))
        )


def test_receipt_survives_database_restoration_and_detects_tampering(tmp_path):
    bundle = publish_product_proving_bundle(tmp_path / "receipt", _capture())
    verified = verify_product_proving_bundle(
        bundle.bundle_dir,
        expected_integrity_manifest_sha256=bundle.integrity_manifest_sha256,
    )
    assert verified.valid
    assert "simulated practitioner" in (bundle.bundle_dir / "receipt.md").read_text()
    assert "does not prove replacement" in (bundle.bundle_dir / "receipt.md").read_text()

    (bundle.bundle_dir / "pass-2-approved-export.pdf").write_bytes(b"not the reviewed PDF")
    with pytest.raises(CorruptProductProvingBundle, match="digest"):
        verify_product_proving_bundle(
            bundle.bundle_dir,
            expected_integrity_manifest_sha256=bundle.integrity_manifest_sha256,
        )
