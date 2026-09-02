"""M8 acceptance: replay, controlled lane, and bundle verification.

Split from the capture suite so the two run on separate CI runners (#548):
one 1,263-line module held 258 of the slow gate's 381 seconds, and a single
file cannot span runners.

These twelve tests share the module-scoped provisioning in
`m8_acceptance_support`, so they stay together. Separating them further would
provision twice rather than save time.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
import hashlib
import json
from pathlib import Path
import shutil

import pymupdf
import pytest
from sqlalchemy import select, text

pytestmark = pytest.mark.slow

import corridor.m8_acceptance as m8_acceptance_module
import corridor.m8_acceptance_controlled as m8_acceptance_controlled_module
from corridor.config import settings
from corridor.db import engine
from corridor.page_inventory import inventory_page, route_page
from corridor.m8_acceptance import (
    AcceptanceCaptureConfig,
    AcceptanceError,
    AcceptanceRunConfig,
    AssertionResult,
    CorruptAcceptanceBundle,
    CorruptAcceptanceFixture,
    ProvisionedDatabase,
    _require_local_postgres_host,
    _acceptance_assertions,
    _load_transformations,
    _require_postgres_16,
    capture_m8_fixture,
    run_m8_acceptance,
    verify_m8_acceptance_bundle,
)
from corridor.models import Candidate, DocPage
from corridor.revision_comparison import DEFAULT_MATCHER_VERSION


PROMPT_VERSION = "m8-controlled-capture-v1"
SCHEMA_VERSION = "m8-controlled-candidate-shape-v1"
MODEL = "deterministic-capture-fixture-v1"
SEED_QUOTE = "SEED-1 Controlled Utility Telecom 100+00 IH-45 captured source row"
REVISION_IDS = (
    "nhhip-ucm-2025-06-20",
    "nhhip-ucm-2025-07-22",
    "nhhip-ucm-2025-10-24",
    "nhhip-ucm-2025-12-15",
    "nhhip-ucm-2026-02-13",
)
TRANSFORMATIONS = (
    Path(__file__).parent
    / "fixtures"
    / "m8_acceptance"
    / "controlled_transformations.json"
)
CLAIM_BOUNDARY = {
    "mechanical_correctness_only": True,
    "semantic_correctness": False,
    "recall": False,
    "human_review": False,
    "production_readiness": False,
    "independent_customer_validation": False,
}

from m8_acceptance_support import (  # noqa: F401
    _Capture,
    _canonical_json,
    _capture_extractor,
    _capture_fixture,
    _database_exists,
    _lock_record,
    _read_export,
    _run_config,
    _sha256,
    _stub_capture_harness,
    _write_pdf,
    _write_source_lock,
)


@pytest.fixture(scope="module")
def replay_capture(tmp_path_factory):
    return _capture_fixture(tmp_path_factory.mktemp("m8-replay-capture"))


@pytest.fixture(scope="module")
def baseline_acceptance(replay_capture, tmp_path_factory):
    output_dir = tmp_path_factory.mktemp("m8-baseline") / "bundle"
    return run_m8_acceptance(_run_config(replay_capture, output_dir))



def test_equivalent_model_free_replays_have_one_normalized_identity(
    replay_capture,
    tmp_path,
):
    first = run_m8_acceptance(_run_config(replay_capture, tmp_path / "bundle-a"))
    second = run_m8_acceptance(_run_config(replay_capture, tmp_path / "bundle-b"))

    assert (
        first.fixture_sha256 == second.fixture_sha256 == replay_capture.fixture_sha256
    )
    assert first.canonical_content_sha256 == second.canonical_content_sha256
    assert len(first.canonical_content_sha256) == 64
    assert (
        verify_m8_acceptance_bundle(
            first.bundle_dir,
            expected_integrity_manifest_sha256=first.integrity_manifest_sha256,
        ).valid
        is True
    )
    assert (
        verify_m8_acceptance_bundle(
            second.bundle_dir,
            expected_integrity_manifest_sha256=second.integrity_manifest_sha256,
        ).valid
        is True
    )
    assert all(assertion.passed for assertion in first.assertions)
    real = _read_export(first, "real-chain.json")
    assert [item["registry_id"] for item in real["documents"]] == [
        "nhhip-rid-index-2026-05-01",
        *REVISION_IDS,
    ]
    assert len(real["runs"]) == 5
    assert len(real["active_run_declarations"]) == 5
    assert len(real["comparisons"]) == 4
    assert real["ledger_counts"] == {
        "dependencies": 0,
        "assertions": 0,
        "evidence_links": 0,
        "operative_support": 0,
        "carry_forward_receipts": 0,
    }
    assert real["claims"] == CLAIM_BOUNDARY
    exported_text = "".join(
        path.read_text() for path in first.bundle_dir.iterdir() if path.is_file()
    )
    assert "postgresql://" not in exported_text
    assert "postgresql+psycopg://" not in exported_text
    assert first.database_name != second.database_name
    assert not _database_exists(first.database_name)
    assert not _database_exists(second.database_name)


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


def test_controlled_lane_reuses_worklist_snapshots_within_unchanged_phases(
    monkeypatch,
    replay_capture,
    tmp_path,
):
    original = m8_acceptance_controlled_module.build_reviewer_worklist
    calls = 0

    def counted_worklist(session, project_id):
        nonlocal calls
        calls += 1
        return original(session, project_id)

    monkeypatch.setattr(
        m8_acceptance_controlled_module,
        "build_reviewer_worklist",
        counted_worklist,
    )

    run_m8_acceptance(_run_config(replay_capture, tmp_path / "bundle"))

    # Five lifecycle transitions genuinely change state. Before-policy and
    # after-policy case projections each share one stable project snapshot.
    assert calls <= 7


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


def test_real_replay_contradiction_is_exported_in_a_verifiable_failed_bundle(
    monkeypatch,
    replay_capture,
    tmp_path,
):
    original = m8_acceptance_module._captured_extractor

    def drifted_extractor(run_record):
        extract = original(run_record)

        def wrapped(session, document):
            candidates = extract(session, document)
            if run_record["registry_id"] == REVISION_IDS[0]:
                fields = candidates[0].payload_json["fields"]
                fields["external_org"] = "Replay Drift Utility"
            return candidates

        return wrapped

    monkeypatch.setattr("corridor.m8_acceptance._captured_extractor", drifted_extractor)

    summary = run_m8_acceptance(_run_config(replay_capture, tmp_path / "failed-bundle"))

    assert summary.carried_count == 0
    assert [assertion.name for assertion in summary.assertions] == [
        "real_chain_replay_exact_inputs_match_capture"
    ]
    assert [assertion.passed for assertion in summary.assertions] == [False]
    assert (
        verify_m8_acceptance_bundle(
            summary.bundle_dir,
            expected_integrity_manifest_sha256=summary.integrity_manifest_sha256,
        ).valid
        is True
    )
    exported_real = _read_export(summary, "real-chain.json")
    assert exported_real["replay_failure"]["name"] == (
        "real_chain_replay_exact_inputs_match_capture"
    )
    assert exported_real["replay_failure"]["observed"]["registry_id"] == REVISION_IDS[0]
    assert exported_real["runs"] == []
    exported_assertions = _read_export(summary, "assertions.json")
    assert exported_assertions["assertions"][0]["detail"] == (
        f"replayed exact inputs drifted for {REVISION_IDS[0]}"
    )


def test_controlled_lane_contradiction_is_exported_in_a_verifiable_failed_bundle(
    monkeypatch,
    replay_capture,
    tmp_path,
):
    original = m8_acceptance_controlled_module._compare_controlled_case
    raised = {"done": False}

    def explode_once(session, controlled_case):
        original(session, controlled_case)
        if not raised["done"]:
            raised["done"] = True
            raise AcceptanceError("forced controlled contradiction")

    monkeypatch.setattr(
        "corridor.m8_acceptance_controlled._compare_controlled_case",
        explode_once,
    )

    summary = run_m8_acceptance(_run_config(replay_capture, tmp_path / "failed-bundle"))

    assert summary.carried_count == 0
    assert [assertion.name for assertion in summary.assertions] == [
        "controlled_lane_claims_remain_self_consistent"
    ]
    assert [assertion.passed for assertion in summary.assertions] == [False]
    assert (
        verify_m8_acceptance_bundle(
            summary.bundle_dir,
            expected_integrity_manifest_sha256=summary.integrity_manifest_sha256,
        ).valid
        is True
    )
    controlled = _read_export(summary, "controlled-lane.json")
    assert controlled["status"] == "failed"
    assert (
        controlled["failure"]["name"] == "controlled_lane_claims_remain_self_consistent"
    )
    assert len(controlled["documents"]) > 0
    assert len(controlled["runs"]) > 0


def test_acceptance_assertions_enforce_fan_in_ambiguous_abstention(
    baseline_acceptance,
):
    summary = baseline_acceptance
    real = _read_export(summary, "real-chain.json")
    controlled = _read_export(summary, "controlled-lane.json")
    database = ProvisionedDatabase(
        name="corridor_m8_acceptance_" + "0" * 32,
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
        name="corridor_m8_acceptance_" + "0" * 32,
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
        name="corridor_m8_acceptance_" + "0" * 32,
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


def test_controlled_lane_freezes_one_runtime_for_normal_protocol(
    monkeypatch,
    replay_capture,
    tmp_path,
):
    created = []
    seen = []

    class RuntimeToken:
        def canonical_policy_json(self):
            return {
                "policy_version": "test",
                "eligible_comparison_state": "unchanged",
                "field_equality": "exact-admitted-fields-v1",
                "provenance": "exactly-one-verified-citation-v1",
                "support_scope": "all-operative-scopes-v1",
                "rules_digest_method": "sha256-safety-source-files-v1",
                "rules_digest": "r" * 64,
                "matcher_version": DEFAULT_MATCHER_VERSION,
                "matcher_config": {},
                "matcher_config_sha256": "m" * 64,
            }

        def rules_digest(self):
            return "r" * 64

    def make_runtime():
        runtime = RuntimeToken()
        created.append(runtime)
        return runtime

    original_status = m8_acceptance_controlled_module.automatic_carry_forward_status
    original_run = m8_acceptance_controlled_module.run_automatic_carry_forward

    def wrapped_status(session, project_id, *, _runtime=None, **kwargs):
        seen.append(("status", id(_runtime)))
        return original_status(session, project_id, _runtime=_runtime, **kwargs)

    def wrapped_run(session, project_id, *, _runtime=None):
        seen.append(("run", id(_runtime)))
        return original_run(session, project_id, _runtime=_runtime)

    monkeypatch.setattr(
        "corridor.m8_acceptance_controlled._acceptance_carry_runtime",
        make_runtime,
    )
    monkeypatch.setattr(
        "corridor.m8_acceptance_controlled.automatic_carry_forward_status",
        wrapped_status,
    )
    monkeypatch.setattr(
        "corridor.m8_acceptance_controlled.run_automatic_carry_forward",
        wrapped_run,
    )
    monkeypatch.setattr(
        "corridor.m8_acceptance_controlled._exercise_policy_drift",
        lambda _session: {"outcome": "skipped-for-runtime-test"},
    )
    monkeypatch.setattr(
        "corridor.m8_acceptance._acceptance_assertions",
        lambda *_args: [],
    )

    summary = run_m8_acceptance(_run_config(replay_capture, tmp_path / "bundle"))

    assert summary.assertions == ()
    assert len(created) == 1
    expected_runtime_id = id(created[0])
    assert seen[:3] == [
        ("status", expected_runtime_id),
        ("run", expected_runtime_id),
        ("run", expected_runtime_id),
    ]

