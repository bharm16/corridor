"""M8 acceptance: the exported bundle, and the assertions it must carry.

Split from the replay and controlled-lane suites so the three run on separate
CI runners (#548). One 1,263-line module held 258 of the slow gate's 381
seconds and a file cannot span runners, so it set the gate's floor alone.

These seven tests read one baseline bundle rather than replaying per test, so
they share `baseline_acceptance` and stay together. `m8_acceptance_support`
holds the fixtures the three suites provision for themselves.
"""

from __future__ import annotations

import json
import shutil

import pytest

pytestmark = pytest.mark.slow

from corridor.m8_acceptance import (
    AssertionResult,
    CorruptAcceptanceBundle,
    ProvisionedDatabase,
    _acceptance_assertions,
    run_m8_acceptance,
    verify_m8_acceptance_bundle,
)

from m8_acceptance_support import (
    CLAIM_BOUNDARY,
    _canonical_json,
    _capture_fixture,
    _read_export,
    _run_config,
    _sha256,
)


@pytest.fixture(scope="module")
def replay_capture(tmp_path_factory):
    return _capture_fixture(tmp_path_factory.mktemp("m8-replay-capture"))


@pytest.fixture(scope="module")
def baseline_acceptance(replay_capture, tmp_path_factory):
    output_dir = tmp_path_factory.mktemp("m8-baseline") / "bundle"
    return run_m8_acceptance(_run_config(replay_capture, output_dir))


def test_controlled_phase_carries_only_exact_previously_established_support(
    baseline_acceptance,
):
    summary = baseline_acceptance

    controlled = _read_export(summary, "controlled-lane.json")
    assert controlled["claim_boundary"] == CLAIM_BOUNDARY
    assert [step["review_status"] for step in controlled["lifecycle"]] == [
        "awaiting_extraction",
        "extraction_failed",
        "awaiting_active_run",
        "awaiting_comparison",
        "actionable",
    ]
    assert controlled["test_precondition"] == {
        "principal": "local:m8-acceptance-fixture",
        "simulated_human_setup": True,
        "human_review_performed": False,
    }
    assert controlled["before_policy"] == {
        "eligible_route": "automatic_carry_forward",
        "automatic_writes": 0,
    }
    assert controlled["passes"] == {
        "first": {"carried": 2},
        "second": {"carried": 0},
        "receipt_count": 2,
    }
    assert controlled["policy"]["authorization_count"] == 0
    drift = controlled["policy"]["drift_outcome"]
    assert drift["outcome"] == "released_policy_changed_and_ran_idempotently"
    assert drift["pause"] == {
        "carried": 0,
        "reasons": [],
        "ledger_unchanged": False,
        "active_remained_initial": False,
    }
    assert drift["replacement"]["rules_digest"] == drift["drifted_rules_digest"]
    assert drift["replacement"]["active_pointer_replaced"] is False
    assert drift["replacement"]["authorization_count"] == 0
    assert drift["resume"] == {
        "carried": 1,
        "receipt_bound_to_replacement": True,
        "idempotent_second_carried": 0,
        "comparison_count": 1,
        "comparison_preserved": True,
    }

    cases = {case["case_id"]: case for case in controlled["cases"]}
    assert {
        case_id: (
            case["outcome"],
            case["abstention_reason"],
            case["inherited_scopes"],
            case["ready_after"],
        )
        for case_id, case in cases.items()
    } == {
        "readiness-exact": (
            "carried",
            None,
            ["publication", "readiness"],
            True,
        ),
        "publication-exact": (
            "carried",
            None,
            ["publication"],
            False,
        ),
        "changed": ("abstained", "comparison_changed", [], False),
        "dropped": ("abstained", "comparison_dropped", [], False),
        "ambiguous": ("abstained", "comparison_ambiguous", [], False),
        "fan-in-ambiguous": (
            "abstained",
            "comparison_ambiguous",
            [],
            False,
        ),
        "fan-out-ambiguous": (
            "abstained",
            "comparison_ambiguous",
            [],
            False,
        ),
        "unmatched": ("abstained", "comparison_unmatched", [], False),
        "normalized-only": (
            "abstained",
            "successor_fields_not_exact",
            [],
            False,
        ),
        "unverified-citation": (
            "abstained",
            "successor_provenance_unsafe",
            [],
            False,
        ),
        "multiple-citations": (
            "abstained",
            "successor_provenance_unsafe",
            [],
            False,
        ),
        "human-edited": (
            "abstained",
            "successor_fields_not_exact",
            [],
            False,
        ),
    }
    assert all(case["dependency_fields_changed"] is False for case in cases.values())
    assert all(
        case["ledger_unchanged"] is True
        for case in cases.values()
        if case["outcome"] == "abstained"
    )
    assert cases["readiness-exact"]["ledger_unchanged"] is False
    assert cases["publication-exact"]["ledger_unchanged"] is False
    assert cases["fan-out-ambiguous"]["finding_predecessor_candidate_ids"] == [
        cases["fan-out-ambiguous"]["predecessor_candidate_id"]
    ]
    assert (
        cases["fan-out-ambiguous"]["finding_successor_candidate_ids"]
        == cases["fan-out-ambiguous"]["successor_candidate_ids"]
    )
    assert len(cases["fan-out-ambiguous"]["successor_candidate_ids"]) == 2
    assert {
        case["successor_candidate_state"]
        for case in cases.values()
        if case["successor_candidate_state"] is not None
    } == {"pending"}
    assert summary.carried_count == 2
    assert summary.abstention_counts == {
        "comparison_ambiguous": 3,
        "comparison_changed": 1,
        "comparison_dropped": 1,
        "comparison_unmatched": 1,
        "successor_fields_not_exact": 2,
        "successor_provenance_unsafe": 2,
    }
    assert controlled["failed_attempt"]["partial_candidate_count_after_failure"] == 0
    assert controlled["failed_attempt"]["active_at_failure"] is False
    assert controlled["failed_attempt"]["committed_before_retry"] is True
    automation = controlled["automation_write_boundary"]
    assert automation["boundary"] == (
        "after_released_policy_identity_before_first_carry_through_idempotent_second_carry"
    )
    assert automation["new_dependency_rows"] == 0
    assert automation["new_assertion_rows"] == 0
    assert automation["admission_audit_actions_created"] == {
        "human": 0,
        "machine": 0,
        "other": 0,
        "total": 0,
    }
    assert automation["successor_candidate_states"]
    assert set(automation["successor_candidate_states"].values()) == {"pending"}
    assert automation["observed_mutation_categories"] == [
        "audit",
        "automatic_carry_forward_outcomes",
        "automatic_carry_forward_receipts",
        "automatic_carry_forward_runs",
        "dependency_evidence_sufficiencies",
        "evidence_links",
        "operative_support",
    ]
    assert automation["allowed_mutation_categories"] == [
        "audit",
        "automatic_carry_forward_outcomes",
        "automatic_carry_forward_receipts",
        "automatic_carry_forward_runs",
        "dependency_evidence_sufficiencies",
        "evidence_links",
        "operative_support",
    ]
    assert len(automation["audit_entries_created"]) == 2
    assert {
        (entry["action"], entry["actor"], entry["human_principal"])
        for entry in automation["audit_entries_created"]
    } == {("automatic_carry_forward", "corridor:automatic-carry-forward", None)}


def test_production_verifier_rejects_a_tampered_raw_export(
    baseline_acceptance,
    tmp_path,
):
    bundle_dir = tmp_path / "bundle"
    shutil.copytree(baseline_acceptance.bundle_dir, bundle_dir)
    controlled = bundle_dir / "controlled-lane.json"
    controlled.write_bytes(controlled.read_bytes() + b"\n")

    with pytest.raises(CorruptAcceptanceBundle, match="controlled-lane.json"):
        verify_m8_acceptance_bundle(
            bundle_dir,
            expected_integrity_manifest_sha256=(
                baseline_acceptance.integrity_manifest_sha256
            ),
        )


def test_verifier_rejects_whole_bundle_rewrite_against_pinned_manifest(
    baseline_acceptance,
    tmp_path,
):
    bundle_dir = tmp_path / "bundle"
    shutil.copytree(baseline_acceptance.bundle_dir, bundle_dir)
    controlled_path = bundle_dir / "controlled-lane.json"
    controlled = json.loads(controlled_path.read_text())
    controlled["passes"]["first"]["carried"] = 999
    controlled_bytes = _canonical_json(controlled) + b"\n"
    controlled_path.write_bytes(controlled_bytes)
    manifest_path = bundle_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["controlled-lane.json"] = {
        "bytes": len(controlled_bytes),
        "sha256": _sha256(controlled_bytes),
    }
    manifest_path.write_bytes(_canonical_json(manifest) + b"\n")

    with pytest.raises(CorruptAcceptanceBundle, match="manifest digest"):
        verify_m8_acceptance_bundle(
            bundle_dir,
            expected_integrity_manifest_sha256=(
                baseline_acceptance.integrity_manifest_sha256
            ),
        )


def test_failed_assertion_is_exported_in_a_verifiable_bundle(
    monkeypatch,
    replay_capture,
    tmp_path,
):
    monkeypatch.setattr(
        "corridor.m8_acceptance._acceptance_assertions",
        lambda *_args: [
            AssertionResult(
                name="controlled_contradiction",
                passed=False,
                observed="contradiction",
                expected="design",
            )
        ],
    )

    summary = run_m8_acceptance(_run_config(replay_capture, tmp_path / "failed-bundle"))

    assert [item.passed for item in summary.assertions] == [False]
    assert (
        verify_m8_acceptance_bundle(
            summary.bundle_dir,
            expected_integrity_manifest_sha256=summary.integrity_manifest_sha256,
        ).valid
        is True
    )
    exported = _read_export(summary, "assertions.json")
    assert exported["assertions"][0]["observed"] == "contradiction"


def test_acceptance_assertions_enforce_fan_in_ambiguous_abstention(
    baseline_acceptance,
):
    summary = baseline_acceptance
    real = _read_export(summary, "real-chain.json")
    controlled = _read_export(summary, "controlled-lane.json")
    database = ProvisionedDatabase(
        name="corridor_disposable_m8_acceptance_1234_000000000000",
        session_factory=None,  # type: ignore[arg-type]
        postgres_version="16.10",
        migration_head="head",
    )

    fan_in = next(
        case for case in controlled["cases"] if case["case_id"] == "fan-in-ambiguous"
    )
    fan_in["abstention_reason"] = "comparison_changed"

    assertions = _acceptance_assertions(real, controlled, database)
    by_name = {assertion.name: assertion for assertion in assertions}

    assert by_name["unsafe_cases_abstain_without_ledger_mutation"].passed is False


def test_acceptance_assertions_enforce_real_fan_out_ambiguous_abstention(
    baseline_acceptance,
):
    summary = baseline_acceptance
    real = _read_export(summary, "real-chain.json")
    controlled = _read_export(summary, "controlled-lane.json")
    database = ProvisionedDatabase(
        name="corridor_disposable_m8_acceptance_1234_000000000000",
        session_factory=None,  # type: ignore[arg-type]
        postgres_version="16.10",
        migration_head="head",
    )

    fan_out = next(
        case for case in controlled["cases"] if case["case_id"] == "fan-out-ambiguous"
    )
    fan_out["finding_successor_candidate_ids"] = fan_out[
        "finding_successor_candidate_ids"
    ][:1]

    assertions = _acceptance_assertions(real, controlled, database)
    by_name = {assertion.name: assertion for assertion in assertions}

    assert by_name["fan_out_ambiguity_is_mechanically_real"].passed is False


def test_acceptance_assertions_enforce_no_automated_record_origination(
    baseline_acceptance,
):
    summary = baseline_acceptance
    real = _read_export(summary, "real-chain.json")
    controlled = _read_export(summary, "controlled-lane.json")
    database = ProvisionedDatabase(
        name="corridor_disposable_m8_acceptance_1234_000000000000",
        session_factory=None,  # type: ignore[arg-type]
        postgres_version="16.10",
        migration_head="head",
    )

    controlled["automation_write_boundary"]["new_dependency_rows"] = 1

    assertions = _acceptance_assertions(real, controlled, database)
    by_name = {assertion.name: assertion for assertion in assertions}

    assert (
        by_name["automatic_carry_never_performs_admission_or_record_origination"].passed
        is False
    )
