"""Public contract for the bounded raw-Document Product Proving Run."""

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json

import pytest

from corridor.product_proving_run import (
    BUNDLE_SCHEMA_VERSION,
    CandidateSetComparison,
    CorruptProductProvingBundle,
    DatabaseConnectionIdentityEvidence,
    DatabaseSourceIdentity,
    ExpectedPreflight,
    ExtractionConfiguration,
    FRONTEND_EVIDENCE_KIND,
    ObservedPreflight,
    ProductProvingCapture,
    ProductProvingFailureCapture,
    ProductProvingDatabaseBaselineEvidence,
    ProductProvingFinalSessionEvidence,
    ProductProvingFinalStateEvidence,
    ProductProvingPass,
    ProductProvingPassExecutionEvidence,
    ProductProvingRestoreEvidence,
    compare_candidate_sets,
    publish_product_proving_bundle,
    publish_product_proving_failure_bundle,
    verify_preflight,
    verify_product_proving_pass,
    verify_product_proving_bundle,
    verify_product_proving_failure_bundle,
    verify_final_session_evidence,
    verify_two_pass_capture,
)


def _configuration(
    prompt_version: str = "minutes_v3",
    *,
    schema_version: str | None = None,
    prompt_sha256: str = "9" * 64,
) -> ExtractionConfiguration:
    config_json = {
        "receipt_version": 1,
        "extractor": "test-extractor",
        "prompt_version": prompt_version,
        "model": "gpt-5.6-luna",
        "schema_version": schema_version or prompt_version,
        "prompt_sha256": prompt_sha256,
        "schema_sha256": "8" * 64,
        "postprocessor_sha256": "7" * 64,
        "request_controls": {"strict": True},
        "runtime": {
            "python_implementation": "CPython",
            "python_version": "3.12.0",
            "dependency_lock_sha256": "5" * 64,
            "packages": {"sqlalchemy": "2.0-test"},
        },
    }
    return ExtractionConfiguration(
        prompt_version=prompt_version,
        model="gpt-5.6-luna",
        schema_version=schema_version or prompt_version,
        prompt_sha256=prompt_sha256,
        schema_sha256="8" * 64,
        postprocessor_sha256="7" * 64,
        config_sha256=sha256(
            json.dumps(config_json, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    )


def _config_json(configuration: ExtractionConfiguration) -> dict:
    return {
        "receipt_version": 1,
        "extractor": "test-extractor",
        "prompt_version": configuration.prompt_version,
        "model": configuration.model,
        "schema_version": configuration.schema_version,
        "prompt_sha256": configuration.prompt_sha256,
        "schema_sha256": configuration.schema_sha256,
        "postprocessor_sha256": configuration.postprocessor_sha256,
        "request_controls": {"strict": True},
        "runtime": {
            "python_implementation": "CPython",
            "python_version": "3.12.0",
            "dependency_lock_sha256": "5" * 64,
            "packages": {"sqlalchemy": "2.0-test"},
        },
    }


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
        baseline_configuration=_configuration(),
        fresh_configuration=_configuration(),
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
        baseline_configuration=_configuration(),
        fresh_configuration=_configuration(),
    )

    assert not comparison.equal
    assert len(comparison.added) == 1
    assert len(comparison.missing) == 1
    assert expected in json.dumps({"added": comparison.added, "missing": comparison.missing})


def _expected_preflight() -> ExpectedPreflight:
    return ExpectedPreflight(
        source_revision="a" * 40,
        origin_main_revision="b" * 40,
        migration_head="c318d6e8f0a3",
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
    configuration = _configuration()
    return CandidateSetComparison(
        document_id=document_id,
        baseline_run_id=baseline,
        fresh_run_id=fresh,
        baseline_configuration=configuration,
        fresh_configuration=configuration,
        added=(),
        missing=(),
        matched_sha256=(),
    )


_WRITE_FAMILIES = (
    "active_extraction_runs",
    "active_run_declarations",
    "assertions",
    "audit_log",
    "candidate_dispositions",
    "candidates",
    "dependencies",
    "document_rendition_derivations",
    "dependency_admission_outcomes",
    "event_admission_outcomes",
    "evidence_investigation_candidate_review_starts",
    "evidence_links",
    "external_report_artifacts",
    "external_report_releases",
    "extraction_runs",
    "extraction_measurement_case_states",
    "policy_runs",
    "report_runs",
    "work_decisions",
)


def _measured_write_set(number: int) -> dict:
    return {
        table: [
            {
                "operation": "created",
                "identity": {"id": number},
                "content_sha256": str(number) * 64,
                "stable_content_sha256": sha256(table.encode()).hexdigest(),
            }
        ]
        for table in _WRITE_FAMILIES
    }


def _pass(number: int) -> ProductProvingPass:
    pdf_bytes = b"%PDF-1.7\nfixed report\n"
    pdf_sha256 = sha256(pdf_bytes).hexdigest()
    comparisons = (
        _comparison(1311, 117, 200000 + number),
        _comparison(1435, 193811, 200010 + number),
        _comparison(1438, 193812, 200020 + number),
    )
    extraction_run_receipts = tuple(
        {
            "run_id": comparison.fresh_run_id,
            "document_id": comparison.document_id,
            "outcome": "completed",
            "page_errors": 0,
            "candidate_count": len(comparison.matched_sha256),
            "prompt_version": comparison.fresh_configuration.prompt_version,
            "model": comparison.fresh_configuration.model,
            "schema_version": comparison.fresh_configuration.schema_version,
            "prompt_sha256": comparison.fresh_configuration.prompt_sha256,
            "schema_sha256": comparison.fresh_configuration.schema_sha256,
            "postprocessor_sha256": (
                comparison.fresh_configuration.postprocessor_sha256
            ),
            "extractor_config": _config_json(comparison.fresh_configuration),
            "extractor_config_sha256": comparison.fresh_configuration.config_sha256,
            "token_usage": {
                "scope": "run",
                "document_ids": [comparison.document_id],
                "measurement": "exact",
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
                "cached_tokens": 0,
            },
        }
        for comparison in comparisons
    )
    return ProductProvingPass(
        pass_number=number,
        restored_baseline_fingerprint="4" * 64,
        extraction_comparisons=comparisons,
        extraction_run_receipts=extraction_run_receipts,
        extraction_failures=(),
        admission_completed=True,
        residual_candidate_ids=(10, 11, 12),
        residual_outcomes={10: "supported", 11: "not_relevant", 12: "unresolved"},
        frontend_kind=FRONTEND_EVIDENCE_KIND,
        frontend_actions=(
            "review_candidate:10",
            "review_candidate:11",
            "review_candidate:12",
            "accept_candidate:10",
            "mark_statement_not_relevant:11",
            "keep_candidate_unresolved:12",
            "refuse_invalid_action",
            "change_work_decision:101",
            "review_report:201",
            "release_approved_export:301",
        ),
        invalid_action_refused=True,
        invalid_action_write_set={},
        factual_correction_outcome="no_structured_correction_observed",
        work_decision_change_preserved_predecessor=True,
        report_pdf_sha256=pdf_sha256,
        approved_export_sha256=pdf_sha256,
        approved_export_bytes=pdf_bytes,
        report_provenance_classes=("Assertion", "Derivation", "Work Decision"),
        write_set=_measured_write_set(number),
        operations_elapsed_seconds=10.0,
        practitioner_elapsed_seconds=20.0,
        non_blocking_friction=("long label",),
        workarounds=(),
    )


def _final_session_evidence() -> ProductProvingFinalSessionEvidence:
    source = DatabaseSourceIdentity(
        backend="postgresql",
        host=None,
        port=None,
        database="corridor",
        username="local",
    )
    connection = DatabaseConnectionIdentityEvidence(
        database="corridor",
        username="local",
        server_address="",
        server_port="",
        system_identifier="1234567890123456789",
        postgres_version="16.10",
    )
    baseline = ProductProvingDatabaseBaselineEvidence(
        manifest_sha256="1" * 64,
        dump_sha256="2" * 64,
        state_sha256="4" * 64,
        schema_sha256="3" * 64,
        source_identity=source,
        source_connection_identity=connection,
    )
    pass_one = ProductProvingPassExecutionEvidence(
        execution_id="00000000-0000-4000-8000-000000000001",
        pass_number=1,
        prior_restore_operation_id=None,
        prior_restore_bundle_manifest_sha256=None,
        prior_restore_bundle_canonical_sha256=None,
        started_at="2026-08-26T10:00:00+00:00",
        starting_database_state_sha256=baseline.state_sha256,
        starting_database_schema_sha256=baseline.schema_sha256,
        frontend_bundle_manifest_sha256="5" * 64,
        frontend_bundle_canonical_sha256="6" * 64,
        terminal_database_state_sha256="a" * 64,
    )
    restore_one = ProductProvingRestoreEvidence(
        restore_operation_id="00000000-0000-4000-8000-000000000002",
        pass_number=1,
        pass_execution_id=pass_one.execution_id,
        pass_bundle_manifest_sha256=pass_one.frontend_bundle_manifest_sha256,
        pass_bundle_canonical_sha256=pass_one.frontend_bundle_canonical_sha256,
        restore_bundle_manifest_sha256="7" * 64,
        restore_bundle_canonical_sha256="8" * 64,
        previous_database_state_sha256=pass_one.terminal_database_state_sha256,
        restored_database_state_sha256=baseline.state_sha256,
        database_baseline_manifest_sha256=baseline.manifest_sha256,
        database_baseline_dump_sha256=baseline.dump_sha256,
        database_baseline_state_sha256=baseline.state_sha256,
        database_baseline_schema_sha256=baseline.schema_sha256,
        database_source_identity=source,
        database_source_connection_identity=connection,
        started_at="2026-08-26T10:10:00+00:00",
        completed_at="2026-08-26T10:11:00+00:00",
    )
    pass_two = ProductProvingPassExecutionEvidence(
        execution_id="00000000-0000-4000-8000-000000000003",
        pass_number=2,
        prior_restore_operation_id=restore_one.restore_operation_id,
        prior_restore_bundle_manifest_sha256=(
            restore_one.restore_bundle_manifest_sha256
        ),
        prior_restore_bundle_canonical_sha256=(
            restore_one.restore_bundle_canonical_sha256
        ),
        started_at="2026-08-26T10:12:00+00:00",
        starting_database_state_sha256=restore_one.restored_database_state_sha256,
        starting_database_schema_sha256=baseline.schema_sha256,
        frontend_bundle_manifest_sha256="9" * 64,
        frontend_bundle_canonical_sha256="b" * 64,
        terminal_database_state_sha256="c" * 64,
    )
    restore_two = ProductProvingRestoreEvidence(
        restore_operation_id="00000000-0000-4000-8000-000000000004",
        pass_number=2,
        pass_execution_id=pass_two.execution_id,
        pass_bundle_manifest_sha256=pass_two.frontend_bundle_manifest_sha256,
        pass_bundle_canonical_sha256=pass_two.frontend_bundle_canonical_sha256,
        restore_bundle_manifest_sha256="d" * 64,
        restore_bundle_canonical_sha256="e" * 64,
        previous_database_state_sha256=pass_two.terminal_database_state_sha256,
        restored_database_state_sha256=baseline.state_sha256,
        database_baseline_manifest_sha256=baseline.manifest_sha256,
        database_baseline_dump_sha256=baseline.dump_sha256,
        database_baseline_state_sha256=baseline.state_sha256,
        database_baseline_schema_sha256=baseline.schema_sha256,
        database_source_identity=source,
        database_source_connection_identity=connection,
        started_at="2026-08-26T10:22:00+00:00",
        completed_at="2026-08-26T10:23:00+00:00",
    )
    return ProductProvingFinalSessionEvidence(
        baseline=baseline,
        pass_one=pass_one,
        restore_one=restore_one,
        pass_two=pass_two,
        restore_two=restore_two,
        final_state=ProductProvingFinalStateEvidence(
            database_state_sha256=baseline.state_sha256,
            database_schema_sha256=baseline.schema_sha256,
            source_identity=source,
            source_connection_identity=connection,
        ),
    )


def _capture() -> ProductProvingCapture:
    observed = _observed_preflight()
    return ProductProvingCapture(
        expected=_expected_preflight(),
        observed=observed,
        pass_one=_pass(1),
        pass_two=_pass(2),
        final_session_evidence=_final_session_evidence(),
        final_baseline_fingerprint=observed.baseline_fingerprint,
        simulated_practitioner=True,
        same_project_manual_report_compared=False,
        revision_processing_included=False,
    )


@pytest.mark.parametrize(
    ("evidence", "message"),
    [
        (
            replace(
                _final_session_evidence(),
                pass_one=replace(
                    _final_session_evidence().pass_one,
                    prior_restore_operation_id=(
                        _final_session_evidence().restore_one.restore_operation_id
                    ),
                ),
            ),
            "pass 1 must not name a prior restore",
        ),
        (
            replace(
                _final_session_evidence(),
                pass_two=replace(
                    _final_session_evidence().pass_two,
                    prior_restore_operation_id=(
                        _final_session_evidence().restore_two.restore_operation_id
                    ),
                ),
            ),
            "pass 2 prior restore",
        ),
        (
            replace(
                _final_session_evidence(),
                pass_two=replace(
                    _final_session_evidence().pass_two,
                    prior_restore_bundle_canonical_sha256="f" * 64,
                ),
            ),
            "pass 2 prior restore bundle",
        ),
        (
            replace(
                _final_session_evidence(),
                pass_two=replace(
                    _final_session_evidence().pass_two,
                    starting_database_schema_sha256="f" * 64,
                ),
            ),
            "pass 2 did not start from restore 1",
        ),
        (
            replace(
                _final_session_evidence(),
                restore_one=replace(
                    _final_session_evidence().restore_one,
                    previous_database_state_sha256="f" * 64,
                ),
            ),
            "restore 1 does not follow pass 1",
        ),
        (
            replace(
                _final_session_evidence(),
                restore_one=replace(
                    _final_session_evidence().restore_one,
                    pass_bundle_manifest_sha256="f" * 64,
                ),
            ),
            "restore 1 does not follow pass 1",
        ),
        (
            replace(
                _final_session_evidence(),
                pass_two=replace(
                    _final_session_evidence().pass_two,
                    execution_id=_final_session_evidence().pass_one.execution_id,
                ),
            ),
            "operation ids are not distinct",
        ),
        (
            replace(
                _final_session_evidence(),
                pass_two=replace(
                    _final_session_evidence().pass_two,
                    terminal_database_state_sha256=(
                        _final_session_evidence().pass_one.terminal_database_state_sha256
                    ),
                ),
                restore_two=replace(
                    _final_session_evidence().restore_two,
                    previous_database_state_sha256=(
                        _final_session_evidence().pass_one.terminal_database_state_sha256
                    ),
                ),
            ),
            "terminal database states are not distinct",
        ),
        (
            replace(
                _final_session_evidence(),
                final_state=replace(
                    _final_session_evidence().final_state,
                    database_state_sha256="f" * 64,
                ),
            ),
            "final database state does not close",
        ),
        (
            replace(
                _final_session_evidence(),
                pass_one=replace(
                    _final_session_evidence().pass_one,
                    execution_id="not-a-uuid",
                ),
            ),
            "operation id is not a UUID",
        ),
        (
            replace(
                _final_session_evidence(),
                baseline=replace(
                    _final_session_evidence().baseline,
                    manifest_sha256="not-a-digest",
                ),
            ),
            "invalid digest",
        ),
        (
            replace(
                _final_session_evidence(),
                final_state=replace(
                    _final_session_evidence().final_state,
                    source_identity=replace(
                        _final_session_evidence().final_state.source_identity,
                        database="",
                    ),
                ),
            ),
            "source identity",
        ),
        (
            replace(
                _final_session_evidence(),
                pass_two=replace(
                    _final_session_evidence().pass_two,
                    frontend_bundle_canonical_sha256=(
                        _final_session_evidence().pass_one.frontend_bundle_canonical_sha256
                    ),
                ),
                restore_two=replace(
                    _final_session_evidence().restore_two,
                    pass_bundle_canonical_sha256=(
                        _final_session_evidence().pass_one.frontend_bundle_canonical_sha256
                    ),
                ),
            ),
            "frontend pass bundle identities",
        ),
        (
            replace(
                _final_session_evidence(),
                restore_two=replace(
                    _final_session_evidence().restore_two,
                    database_baseline_manifest_sha256="f" * 64,
                ),
            ),
            "restore 2 uses a different database baseline",
        ),
        (
            replace(
                _final_session_evidence(),
                final_state=replace(
                    _final_session_evidence().final_state,
                    database_schema_sha256="f" * 64,
                ),
            ),
            "final database state does not close",
        ),
        (
            replace(
                _final_session_evidence(),
                final_state=replace(
                    _final_session_evidence().final_state,
                    source_connection_identity=replace(
                        _final_session_evidence().final_state.source_connection_identity,
                        system_identifier="not-numeric",
                    ),
                ),
            ),
            "connection identity",
        ),
        (
            replace(
                _final_session_evidence(),
                pass_two=replace(
                    _final_session_evidence().pass_two,
                    started_at="2026-08-26T10:10:30+00:00",
                ),
            ),
            "operation times are not ordered",
        ),
        (
            replace(
                _final_session_evidence(),
                restore_two=replace(
                    _final_session_evidence().restore_two,
                    completed_at="2026-08-26T10:23:00",
                ),
            ),
            "timezone-aware",
        ),
    ],
)
def test_final_session_evidence_requires_the_exact_ordered_chain(evidence, message):
    with pytest.raises(ValueError, match=message):
        verify_final_session_evidence(
            evidence,
            expected_baseline_fingerprint=_expected_preflight().baseline_fingerprint,
            pass_one=_pass(1),
            pass_two=_pass(2),
            final_baseline_fingerprint="4" * 64,
        )


def test_two_pass_capture_requires_frontend_truth_restore_and_equivalent_outputs():
    verify_two_pass_capture(_capture())

    with pytest.raises(ValueError, match="frontend actions"):
        verify_two_pass_capture(
            replace(_capture(), pass_two=replace(_pass(2), frontend_actions=()))
        )
    with pytest.raises(ValueError, match="frontend evidence kind"):
        verify_two_pass_capture(
            replace(
                _capture(),
                pass_two=replace(_pass(2), frontend_kind="real_frontend"),
            )
        )
    with pytest.raises(ValueError, match="frontend actions"):
        verify_two_pass_capture(
            replace(
                _capture(),
                pass_two=replace(_pass(2), frontend_actions=("yes",)),
            )
        )
    changed_actions = tuple(
        "merge_candidate:10" if action == "accept_candidate:10" else action
        for action in _pass(2).frontend_actions
    )
    with pytest.raises(ValueError, match="frontend action behavior"):
        verify_two_pass_capture(
            replace(
                _capture(),
                pass_two=replace(_pass(2), frontend_actions=changed_actions),
            )
        )
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
    with pytest.raises(ValueError, match="factual correction outcome"):
        verify_two_pass_capture(
            replace(
                _capture(),
                pass_two=replace(_pass(2), factual_correction_outcome="invented"),
            )
        )
    separately_verified_write_set = _measured_write_set(2)
    for table in (
        "active_extraction_runs",
        "active_run_declarations",
        "candidates",
        "external_report_artifacts",
        "external_report_releases",
        "extraction_runs",
    ):
        separately_verified_write_set[table][0]["stable_content_sha256"] = sha256(
            f"pass-two-{table}".encode()
        ).hexdigest()
    verify_two_pass_capture(
        replace(
            _capture(),
            pass_two=replace(
                _pass(2), write_set=separately_verified_write_set
            ),
        )
    )

    changed_write_set = _measured_write_set(2)
    changed_write_set["dependencies"][0]["stable_content_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="write-set behavior"):
        verify_two_pass_capture(
            replace(
                _capture(),
                pass_two=replace(_pass(2), write_set=changed_write_set),
            )
        )
    fabricated_usage = _pass(2)
    fabricated_usage.extraction_run_receipts[0]["token_usage"][
        "measurement"
    ] = "fabricated"
    with pytest.raises(ValueError, match="Extraction Run receipt"):
        verify_two_pass_capture(replace(_capture(), pass_two=fabricated_usage))


def test_one_pass_verifier_does_not_require_fabricated_final_session_evidence():
    verify_product_proving_pass(_expected_preflight(), _pass(1))
    verify_product_proving_pass(_expected_preflight(), _pass(2))
    with pytest.raises(ValueError, match="must be 1 or 2"):
        verify_product_proving_pass(
            _expected_preflight(), replace(_pass(1), pass_number=3)
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
    assert "does not cryptographically distinguish a browser" in (
        bundle.bundle_dir / "receipt.md"
    ).read_text()

    (bundle.bundle_dir / "pass-2-approved-export.pdf").write_bytes(b"not the reviewed PDF")
    with pytest.raises(CorruptProductProvingBundle, match="digest"):
        verify_product_proving_bundle(
            bundle.bundle_dir,
            expected_integrity_manifest_sha256=bundle.integrity_manifest_sha256,
        )


def test_bundle_verifier_rejects_a_resealed_broken_session_chain(tmp_path):
    bundle = publish_product_proving_bundle(tmp_path / "receipt", _capture())
    canonical_path = bundle.bundle_dir / "canonical-content.json"
    canonical = json.loads(canonical_path.read_bytes())
    canonical["final_session_evidence"]["pass_two"][
        "prior_restore_operation_id"
    ] = canonical["final_session_evidence"]["restore_two"]["restore_operation_id"]
    canonical_bytes = (
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )
    receipt_bytes = (
        json.dumps(
            {"schema_version": BUNDLE_SCHEMA_VERSION, **canonical},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    canonical_path.write_bytes(canonical_bytes)
    (bundle.bundle_dir / "receipt.json").write_bytes(receipt_bytes)
    manifest_path = bundle.bundle_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["canonical_content_sha256"] = sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    for name, value in (
        ("canonical-content.json", canonical_bytes),
        ("receipt.json", receipt_bytes),
    ):
        manifest["files"][name] = {
            "bytes": len(value),
            "sha256": sha256(value).hexdigest(),
        }
    manifest_bytes = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )
    manifest_path.write_bytes(manifest_bytes)

    with pytest.raises(CorruptProductProvingBundle, match="session evidence"):
        verify_product_proving_bundle(
            bundle.bundle_dir,
            expected_integrity_manifest_sha256=sha256(manifest_bytes).hexdigest(),
        )


def test_failed_repeatability_receipt_is_publishable_but_can_never_claim_pass(tmp_path):
    comparison = compare_candidate_sets(
        document_id=1438,
        baseline_run_id=193812,
        fresh_run_id=206508,
        baseline=[_candidate(1)],
        fresh=[_candidate(2, event_type="response")],
        baseline_configuration=_configuration(),
        fresh_configuration=_configuration(),
    )
    failure = ProductProvingFailureCapture(
        expected=_expected_preflight(),
        observed=_observed_preflight(),
        pass_number=1,
        phase="extraction_repeatability",
        errors=("Document 1438 changed Candidate meaning",),
        extraction_comparisons=(comparison,),
        extraction_run_receipts=(
            {
                "run_id": 206508,
                "document_id": 1438,
                "outcome": "completed",
                "candidate_count": 8,
            },
        ),
        admission_started=False,
        source_database_mutated=False,
        operations_elapsed_seconds=19.0,
        baseline_dump_sha256="5" * 64,
        baseline_state_manifest_sha256="6" * 64,
        restored_baseline_fingerprint="4" * 64,
    )

    bundle = publish_product_proving_failure_bundle(tmp_path / "failed", failure)
    verified = verify_product_proving_failure_bundle(
        bundle.bundle_dir,
        expected_integrity_manifest_sha256=bundle.integrity_manifest_sha256,
    )
    assert verified.valid
    receipt = json.loads((bundle.bundle_dir / "receipt.json").read_bytes())
    assert receipt["status"] == "failed"
    assert receipt["admission_started"] is False
    assert "passed" not in (bundle.bundle_dir / "receipt.md").read_text().lower()


def test_failed_receipt_requires_exact_post_failure_restore(tmp_path):
    failure = ProductProvingFailureCapture(
        expected=_expected_preflight(),
        observed=_observed_preflight(),
        pass_number=1,
        phase="frontend",
        errors=("frontend could not preserve an unresolved gap",),
        extraction_comparisons=(),
        extraction_run_receipts=(),
        admission_started=True,
        source_database_mutated=True,
        operations_elapsed_seconds=2.0,
        baseline_dump_sha256="5" * 64,
        baseline_state_manifest_sha256="6" * 64,
        restored_baseline_fingerprint="7" * 64,
    )

    with pytest.raises(ValueError, match="restore its baseline"):
        publish_product_proving_failure_bundle(tmp_path / "failed-restore", failure)
