"""Public behavior of the post-activation ordinary Admission seal.

The earlier policy-only proof could pass without exercising the composed
Dependency and Event Admission command. These tests keep the production seam
whole and reject a second-run receipt that repeats an Abstention outcome.
"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from pathlib import Path

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.slow

from corridor.admission import load_project
from corridor.db import Session, engine
from corridor.event_admission import (
    UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
    UNKNOWN_SCOPE_POLICY_VERSION,
    canonical_event_admission_policy,
    _current_migration_head,
    _current_source_revision,
)
from corridor.event_admission_acceptance import (
    RECEIPT_VERSION,
    SELECTION_RULE,
    _receipt_promotion_gates,
    activate_passing_acceptance,
    record_acceptance_receipt,
)
from corridor.extraction_runs import (
    declare_single_run_documents_by_policy,
    record_extraction_run,
)
from corridor.models import (
    Candidate,
    DocPage,
    Document,
    ExternalOrg,
    Project,
)
from corridor import policy
from corridor.sh99_admission_acceptance import (
    CorruptSH99AdmissionBundle,
    HISTORICAL_ACCEPTANCE_RECEIPT,
    SH99SharedAdmissionSealConfig,
    _read_shared_seal_state,
    _require_origin_main_revision,
    _require_shared_seal_pins,
    _require_shared_seal_outcomes,
    _shared_seal_run_receipt,
    _shared_seal_state,
    _write_shared_seal_bundle,
    verify_sh99_shared_admission_seal_bundle,
)
from corridor.config import settings


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    value = Session(bind=connection)
    yield value
    value.close()
    transaction.rollback()
    connection.close()


def test_exact_ordinary_load_is_one_statement_then_zero_new_outcomes(session):
    project = Project(
        slug="shared-admission-seal-test",
        name="Shared Admission Seal Test",
        is_synthetic=True,
    )
    equistar = session.scalar(select(ExternalOrg).where(ExternalOrg.name == "Equistar"))
    if equistar is None:
        equistar = ExternalOrg(name="Equistar", aliases=[])
    session.add_all((project, equistar))
    session.flush()
    quote = "Equistar to provide title by 01/2025."
    document = Document(
        project_id=project.id,
        sha256=hashlib.sha256(quote.encode()).hexdigest(),
        filename="equistar-minutes.pdf",
        doc_type="minutes",
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush()
    other_quote = "Equistar provided a status response without a Commitment."
    session.add(
        DocPage(
            document_id=document.id,
            page_no=1,
            text=f"{quote}\n{other_quote}",
        )
    )
    candidate = Candidate(
        project_id=project.id,
        kind="event",
        payload_json={
            "kind": "event",
            "fields": {
                "event_type": "commitment",
                "description": quote,
                "external_org": equistar.name,
                "stated_party": equistar.name,
                "event_date": "2025-01-16",
                "committed_date": {
                    "text": "01/2025",
                    "precision": "month",
                    "start_date": "2025-01-01",
                    "end_date": "2025-01-31",
                },
            },
            "citations": [
                {
                    "document_id": document.id,
                    "page": 1,
                    "quote": quote,
                    "verified": True,
                    "whole_row": False,
                }
            ],
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=0.99,
        prompt_version="minutes_v3",
        model="gpt-test",
        citations_verified=True,
    )
    other_candidate = Candidate(
        project_id=project.id,
        kind="event",
        payload_json={
            "kind": "event",
            "fields": {
                "event_type": "response",
                "description": other_quote,
                "external_org": equistar.name,
            },
            "citations": [
                {
                    "document_id": document.id,
                    "page": 1,
                    "quote": other_quote,
                    "verified": True,
                    "whole_row": False,
                }
            ],
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=0.99,
        prompt_version="minutes_v3",
        model="gpt-test",
        citations_verified=True,
    )
    run = record_extraction_run(
        session,
        document,
        prompt_version="minutes_v3",
        candidate_count=2,
        page_errors=0,
        candidates=(candidate, other_candidate),
        model="gpt-test",
        schema_version="minutes_v3",
        allow_unsealed_legacy=True,
    )
    declare_single_run_documents_by_policy(session, project.id)
    session.flush()
    assert run.id == candidate.extraction_run_id

    policy_sha256 = policy.canonical_sha256(
        canonical_event_admission_policy(project, UNKNOWN_SCOPE_POLICY_VERSION)
    )
    source_revision = _current_source_revision()
    migration_head = _current_migration_head(session)
    receipt_json = {
        "schema_version": RECEIPT_VERSION,
        "source_revision": source_revision,
        "migration_head": migration_head,
        "selection_rule": SELECTION_RULE,
        "policy_version": UNKNOWN_SCOPE_POLICY_VERSION,
        "policy_sha256": policy_sha256,
        "reason_version": UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
        "opt_in": {
            "metrics": {
                "admission_count": 1,
                "false_party_attribution": 0,
                "false_dependency_scope": 0,
                "project_side_masquerade": 0,
                "cross_project_references": 0,
                "unauthorized_work_decisions": 0,
                "duplicates": 0,
                "protected_dependency_delta": 0,
                "protected_report_delta": 0,
                "invalid_evidence_or_receipts": 0,
            },
            "admissions": [
                {
                    "candidate_id": candidate.id,
                    "commitment_lineage_id": 1,
                    "statement_event_id": 1,
                }
            ],
            "work_items": [
                {
                    "commitment_lineage_id": 1,
                    "statement_event_id": 1,
                    "source_candidate_id": candidate.id,
                    "dependency_id": None,
                    "attention_reasons": [
                        "unknown_scope",
                        "missing_internal_owner",
                        "missing_next_action",
                    ],
                }
            ],
        },
        "migration_rehearsal": {
            "predecessor": "a257c9e6f204",
            "head": migration_head,
            "status": "passed",
            "fresh_head": migration_head,
            "fresh_status": "passed",
        },
        "authority_statement": (
            "Only the deterministic unknown-scope Commitment class is authorized; "
            "models receive no write authority."
        ),
    }
    receipt_json["gates"] = _receipt_promotion_gates(receipt_json)
    receipt = record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision=source_revision,
        migration_head=migration_head,
        receipt_json=receipt_json,
    )
    assert activate_passing_acceptance(session, receipt.id) is not None

    before = _shared_seal_state(session, project.slug)
    first = load_project(session, project.id)
    session.flush()
    after_first = _shared_seal_state(session, project.slug)
    second = load_project(session, project.id)
    session.flush()
    after_second = _shared_seal_state(session, project.slug)

    first_receipt = _shared_seal_run_receipt(
        before,
        after_first,
        {
            "argv": ["make", "admission", f"ARGS=load {project.slug}"],
            "returncode": 0,
            "stdout": "0 conflicts and 1 statements on the record",
        },
    )
    second_receipt = _shared_seal_run_receipt(
        after_first,
        after_second,
        {
            "argv": ["make", "admission", f"ARGS=load {project.slug}"],
            "returncode": 0,
            "stdout": "0 conflicts and 0 statements on the record",
        },
    )

    exact = _require_shared_seal_outcomes(
        before,
        after_first,
        after_second,
        first_receipt,
        second_receipt,
        project_slug=project.slug,
        expected_candidate_id=candidate.id,
    )

    assert (first.dependencies.admitted_count, first.events.admitted_count) == (0, 1)
    assert second.admitted_count == 0
    assert exact["candidate_id"] == candidate.id
    assert exact["second_run_zero_new_outcomes"] is True

    missing_outcome = deepcopy(first_receipt)
    missing_outcome["event_outcomes"] = [
        outcome
        for outcome in missing_outcome["event_outcomes"]
        if outcome["candidate_id"] != other_candidate.id
    ]
    with pytest.raises(ValueError, match="outcome for every Candidate"):
        _require_shared_seal_outcomes(
            before,
            after_first,
            after_second,
            missing_outcome,
            second_receipt,
            project_slug=project.slug,
            expected_candidate_id=candidate.id,
        )


def test_current_shared_seal_pins_are_exact_and_historical_receipt_is_guarded(
    shared_source_database_url,
):
    try:
        state = _read_shared_seal_state(
            shared_source_database_url,
            "sh99-grand-parkway",
        )
    except ValueError as error:
        if str(error) == "no project with slug 'sh99-grand-parkway'":
            pytest.skip("the separately managed SH99 source is not present")
        raise
    candidate = next(
        item
        for item in state["project_state"]["candidates"]
        if item["id"] == 405519
    )
    if candidate["state"] != "pending":
        pytest.skip("the one-time shared Admission seal gate was already consumed")
    activation = state["event_admission_activations"][-1]
    receipt = next(
        item
        for item in state["event_admission_acceptance_receipts"]
        if item["id"] == activation["acceptance_receipt_id"]
    )
    config = SH99SharedAdmissionSealConfig(
        project_slug="sh99-grand-parkway",
        source_database_url=shared_source_database_url,
        expected_clean_git_revision=receipt["source_revision"],
        output_dir=Path("unused"),
        postgres_admin_url=settings.database_url,
        expected_acceptance_receipt_id=receipt["id"],
        expected_acceptance_receipt_sha256=receipt["receipt_sha256"],
        expected_activation_id=activation["id"],
        expected_active_runs=((1435, 193811), (1438, 193812)),
        expected_candidate_id=405519,
    )

    if state["current_event_admission_policy"] == receipt["policy_version"]:
        pins = _require_shared_seal_pins(
            state,
            config,
            source_revision=receipt["source_revision"],
            migration_head=receipt["migration_head"],
        )
        assert pins["candidate_id"] == 405519
        assert pins["historical_acceptance_receipt_id"] == 156
        assert pins["historical_activation_id"] == 140
    else:
        with pytest.raises(ValueError, match="current Event Admission policy"):
            _require_shared_seal_pins(
                state,
                config,
                source_revision=receipt["source_revision"],
                migration_head=receipt["migration_head"],
            )

    corrupted = deepcopy(state)
    historical_id, _ = HISTORICAL_ACCEPTANCE_RECEIPT
    historical = next(
        item
        for item in corrupted["event_admission_acceptance_receipts"]
        if item["id"] == historical_id
    )
    historical["receipt_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="historical acceptance receipt 156"):
        _require_shared_seal_pins(
            corrupted,
            config,
            source_revision=receipt["source_revision"],
            migration_head=receipt["migration_head"],
        )

    corrupted_activation = deepcopy(state)
    historical_activation = next(
        item
        for item in corrupted_activation["event_admission_activations"]
        if item["id"] == 140
    )
    historical_activation["reason"] = "changed historical reason"
    with pytest.raises(ValueError, match="historical activation 140"):
        _require_shared_seal_pins(
            corrupted_activation,
            config,
            source_revision=receipt["source_revision"],
            migration_head=receipt["migration_head"],
        )


def test_shared_seal_requires_the_exact_origin_main_revision(monkeypatch):
    monkeypatch.setattr(
        "corridor.sh99_admission_acceptance._git",
        lambda *args: "a" * 40,
    )
    assert _require_origin_main_revision("a" * 40) == "a" * 40
    with pytest.raises(ValueError, match="HEAD to equal origin/main"):
        _require_origin_main_revision("b" * 40)


def test_shared_seal_bundle_is_database_free_and_tamper_evident(tmp_path):
    canonical = {
        "schema_version": "corridor.sh99-shared-admission-seal.v1",
        "source_snapshot": {"source": {"revision": "a" * 40}, "pins": {}},
        "first_run": {},
        "second_run": {},
        "after_second_state_sha256": "b" * 64,
        "exact_outcome": {},
        "human_approval_gate": "approval required",
    }
    bundle = tmp_path / "seal"
    _, manifest_sha256, canonical_sha256 = _write_shared_seal_bundle(
        bundle,
        environment={"schema_version": canonical["schema_version"]},
        receipt={**canonical, "after_first": {}, "after_second": {}},
        receipt_markdown=b"# Human-readable seal\n",
        canonical_content=canonical,
    )

    verified = verify_sh99_shared_admission_seal_bundle(
        bundle,
        expected_integrity_manifest_sha256=manifest_sha256,
    )

    assert verified.valid is True
    assert verified.canonical_content_sha256 == canonical_sha256
    (bundle / "receipt.md").write_text("tampered\n")
    with pytest.raises(CorruptSH99AdmissionBundle, match="receipt.md"):
        verify_sh99_shared_admission_seal_bundle(
            bundle,
            expected_integrity_manifest_sha256=manifest_sha256,
        )
