"""Public cutoff-correct outcome capture over real PostgreSQL.

These tests drive the delayed association through its real seams: the frozen
shadow-case interface, ordinary human workflows (Not Relevant, guided Save,
correction, Undo), the shared Due Work runtime under a controlled clock, and the
declared gate-7 observation contract. They assert the behaviors the ticket turns
on: a run before its cutoff refuses to associate; an exact or late run associates
facts as of the cutoff from retained history; history that cannot support an
exact answer stays incomplete with a reason rather than being backdated; the
frozen case, its run, and the immutable one-time capture are never rewritten; and
repeated ticks, competing workers, and restart converge on one association.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from corridor.db import Session, engine
from corridor.due_work import (
    EvidenceOutcomeCaptureDeclaration,
    HANDLER_EVIDENCE_OUTCOME_CAPTURE,
    configure_evidence_outcome_capture,
    enqueue_due_work,
    run_due_work_once,
)
from corridor.evidence_investigator import (
    InvestigationBudget,
    InvestigationPacket,
    InvestigationRunOutput,
)
from corridor.evidence_investigator_capture import (
    CaptureContractRefusal,
    CaptureObservationContract,
    capture_status,
    declare_capture_contract,
    reconstruct_cutoff_outcome,
    run_outcome_capture,
)
from corridor.evidence_investigator_runtime import RuntimeIdentity
from corridor.evidence_investigator_shadow import (
    capture_shadow_outcome,
    run_shadow_batch,
)
from corridor.extraction_runs import declare_active_run, record_extraction_run
from corridor.models import (
    AuditLog,
    Candidate,
    CandidateDisposition,
    DependencyEventScope,
    DocPage,
    Document,
    EventAdmissionOutcome,
    EvidenceInvestigationCaptureContract,
    EvidenceInvestigationCaptureResult,
    EvidenceInvestigationReviewObservation,
    EvidenceInvestigationShadowCase,
    EvidenceInvestigationShadowOutcome,
    ExternalOrg,
    PolicyRun,
    Project,
    ProjectRosterEntry,
    StatementCoordinationReversal,
)
from corridor.principals import HumanPrincipal
from corridor.statement_coordination import (
    StatementCoordinationDraft,
    coordinate_statement,
)
from corridor.external_statements import (
    CitedStatementEvidence,
    StatementScope,
    StatementTiming,
)
from access_support import seed_membership


RECORDER = HumanPrincipal("local:capture-test")
_H = timedelta(hours=1)


# --------------------------------------------------------------------------- #
# Fixtures and builders
# --------------------------------------------------------------------------- #


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    yield session
    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def project(session):
    project = Project(
        slug=f"capture-{uuid4().hex}",
        name="Capture test",
        is_synthetic=True,
    )
    session.add(project)
    session.flush()
    seed_membership(session, project, RECORDER)
    return project


def _identity(**overrides):
    values = {
        "adapter": "capture-contract-v1",
        "adapter_contract_version": "capture-contract-v1",
        "model": "capture-investigator",
        "prompt_version": "capture-prompt-v1",
        "prompt_sha256": "1" * 64,
        "transport_gate_sha256": "a" * 64,
    }
    values.update(overrides)
    return RuntimeIdentity(**values)


class _StubRuntime:
    async def run(self, case, tools, budget):
        return InvestigationRunOutput(
            packet=InvestigationPacket((), (), (), ("Is this relevant?",)),
            turns=1,
            input_tokens=20,
            output_tokens=10,
        )


def _unplaced_statement(session, project, *, quote=None):
    quote = quote or (
        "Kinder Morgan expects the relocation to finish near Station 6609+00 "
        "during June 2026."
    )
    document = Document(
        project_id=project.id,
        sha256=hashlib.sha256(f"{project.id}:{quote}:{uuid4()}".encode()).hexdigest(),
        filename="minutes/capture.pdf",
        doc_type="minutes",
        doc_date=date(2026, 5, 4),
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    session.add(DocPage(document_id=document.id, page_no=1, text=quote))
    candidate = Candidate(
        project_id=project.id,
        kind="event",
        payload_json={
            "kind": "event",
            "fields": {
                "event_type": "commitment",
                "event_date": "2026-05-04",
                "description": quote,
                "external_org": "Kinder Morgan",
                "committed_date": "June 2026",
            },
            "citations": [
                {"document_id": document.id, "page": 1, "quote": quote, "verified": True}
            ],
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=0.82,
        prompt_version="minutes-v-test",
        model="test-extractor",
        citations_verified=True,
    )
    run = record_extraction_run(
        session,
        document,
        prompt_version="minutes-v-test",
        candidate_count=1,
        page_errors=0,
        candidates=(candidate,),
        model="test-extractor",
        allow_unsealed_legacy=True,
    )
    declare_active_run(session, document.id, run.id, principal=RECORDER)
    admission = PolicyRun(
        project_id=project.id,
        family="event-admission",
        policy_approval_id=None,
        policy_version="capture-test",
        policy_sha256="a" * 64,
        abstention_reason_version="capture-test",
        applied_count=0,
        abstained_count=1,
    )
    session.add(admission)
    session.flush()
    session.add(
        EventAdmissionOutcome(
            policy_run_id=admission.id,
            candidate_id=candidate.id,
            outcome="abstained",
            reason="no_conflict_reference",
        )
    )
    session.flush()
    return candidate


def _freeze(session, project, *, identity=None):
    identity = identity or _identity(model=f"shadow-{uuid4().hex[:8]}")
    candidate = _unplaced_statement(session, project)
    [shadow] = asyncio.run(
        run_shadow_batch(
            session,
            project.id,
            runtime_factory=_StubRuntime,
            identity=identity,
            budget=InvestigationBudget(),
        )
    )
    return shadow.case


def _contract(case, *, cutoff, window_start=None, history_retained_from=None, **overrides):
    frozen = case.frozen_at
    values = dict(
        project_id=case.project_id,
        cohort_id=f"cohort-{case.public_id[:8]}",
        model=case.model,
        prompt_version=case.prompt_version,
        prompt_sha256=case.prompt_sha256,
        adapter_contract_version=case.adapter_contract_version,
        tool_contract_version=case.tool_contract_version,
        validator_version="capture-validator-v1",
        baseline_identity="capture-baseline-v1",
        baseline_collection_contract="baseline-collection-v1",
        window_start=window_start or (frozen - _H),
        cutoff_at=cutoff,
        protection_end=cutoff + _H,
        history_retained_from=(
            history_retained_from
            if history_retained_from is not None
            else frozen - _H
        ),
        eligibility="unplaced-statement-v1",
        missing_label_policy="remain_missing",
        member_case_public_ids=(case.public_id,),
        declared_by="local:capture-approver",
    )
    values.update(overrides)
    return CaptureObservationContract(**values)


def _disposition(session, candidate_id, disposition, *, created_at, reason=None):
    row = CandidateDisposition(
        candidate_id=candidate_id,
        disposition=disposition,
        reason=reason if disposition == "not_relevant" else None,
        recorded_by="local:capture-test",
        created_at=created_at,
    )
    session.add(row)
    session.flush([row])
    return row


# --------------------------------------------------------------------------- #
# Gate-7 declaration
# --------------------------------------------------------------------------- #


def test_declaration_requires_a_complete_verifiable_contract(session, project):
    case = _freeze(session, project)
    cutoff = case.frozen_at + _H
    now = case.frozen_at

    # A malformed missing-label policy is refused (missing labels must stay missing).
    with pytest.raises(CaptureContractRefusal):
        declare_capture_contract(
            session, _contract(case, cutoff=cutoff, missing_label_policy="fill_default"),
            now=now,
        )
    # A window that starts after its cutoff is refused.
    with pytest.raises(CaptureContractRefusal):
        declare_capture_contract(
            session, _contract(case, cutoff=cutoff, window_start=cutoff + _H), now=now
        )
    # A member frozen case whose configuration identity does not match is refused,
    # so a fresh run can never satisfy the frozen cohort.
    with pytest.raises(CaptureContractRefusal):
        declare_capture_contract(
            session, _contract(case, cutoff=cutoff, model="a-different-model"), now=now
        )
    assert session.scalar(
        select(func.count()).select_from(EvidenceInvestigationCaptureContract)
    ) == 0

    contract = declare_capture_contract(session, _contract(case, cutoff=cutoff), now=now)
    assert contract.contract_sha256
    # Re-declaring the identical contract returns the same content identity.
    again = declare_capture_contract(session, _contract(case, cutoff=cutoff), now=now)
    assert again.id == contract.id


def test_declaration_refuses_a_cross_project_member(session, project):
    other = Project(slug=f"other-{uuid4().hex}", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    seed_membership(session, other, RECORDER)
    case = _freeze(session, project)
    contract = _contract(case, cutoff=case.frozen_at + _H)
    cross = CaptureObservationContract(**{**contract.__dict__, "project_id": other.id})
    with pytest.raises(CaptureContractRefusal):
        declare_capture_contract(session, cross, now=case.frozen_at)


# --------------------------------------------------------------------------- #
# Cutoff-correct reconstruction from retained history
# --------------------------------------------------------------------------- #


def test_unresolved_before_a_decision_and_counted_only_after_the_cutoff(session, project):
    case = _freeze(session, project)
    cutoff = case.frozen_at + (2 * _H)
    contract = declare_capture_contract(
        session, _contract(case, cutoff=cutoff), now=case.frozen_at
    )
    # A decision recorded AFTER the cutoff must not be counted as cutoff-time state.
    _disposition(
        session, case.candidate_id, "not_relevant",
        created_at=cutoff + _H, reason="outside_project_scope",
    )
    late = reconstruct_cutoff_outcome(
        session, case, contract, executed_at=cutoff + (5 * _H)
    )
    assert late.completeness == "complete"
    assert late.unresolved is True
    assert late.candidate_disposition is None
    assert "unresolved" in late.strata


def test_exact_cutoff_includes_a_decision_recorded_at_the_cutoff(session, project):
    case = _freeze(session, project)
    cutoff = case.frozen_at + (2 * _H)
    contract = declare_capture_contract(
        session, _contract(case, cutoff=cutoff), now=case.frozen_at
    )
    _disposition(
        session, case.candidate_id, "not_relevant",
        created_at=cutoff, reason="outside_project_scope",
    )
    outcome = reconstruct_cutoff_outcome(session, case, contract, executed_at=cutoff)
    assert outcome.completeness == "complete"
    assert outcome.candidate_disposition == "not_relevant"
    assert outcome.unresolved is False
    assert "not_relevant" in outcome.strata


def test_incomplete_when_retained_history_does_not_cover_the_case(session, project):
    case = _freeze(session, project)
    cutoff = case.frozen_at + (2 * _H)
    # Coverage begins AFTER the case was frozen, so an earlier decision could have
    # existed and not been retained; the cutoff state cannot be proven.
    contract = declare_capture_contract(
        session,
        _contract(case, cutoff=cutoff, history_retained_from=case.frozen_at + _H),
        now=case.frozen_at,
    )
    _disposition(
        session, case.candidate_id, "not_relevant",
        created_at=case.frozen_at + _H, reason="outside_project_scope",
    )
    outcome = reconstruct_cutoff_outcome(session, case, contract, executed_at=cutoff + _H)
    assert outcome.completeness == "incomplete"
    assert outcome.incomplete_reason == "case_predates_retained_history"
    assert outcome.candidate_disposition is None
    assert outcome.unresolved is False


def test_review_duration_uses_only_boundaries_observed_by_the_cutoff(session, project):
    case = _freeze(session, project)
    cutoff = case.frozen_at + (3 * _H)
    contract = declare_capture_contract(
        session, _contract(case, cutoff=cutoff), now=case.frozen_at
    )
    session.add(
        EvidenceInvestigationReviewObservation(
            shadow_case_id=case.id, boundary="start",
            principal="local:reviewer", observed_at=case.frozen_at + _H,
        )
    )
    # The end boundary lands AFTER the cutoff, so no duration can be attributed.
    session.add(
        EvidenceInvestigationReviewObservation(
            shadow_case_id=case.id, boundary="end",
            principal="local:reviewer", observed_at=cutoff + _H,
        )
    )
    session.flush()
    early = reconstruct_cutoff_outcome(session, case, contract, executed_at=cutoff)
    assert early.review_seconds is None

    later = _contract(case, cutoff=cutoff + (2 * _H))
    later_contract = declare_capture_contract(session, later, now=case.frozen_at)
    observed = reconstruct_cutoff_outcome(
        session, case, later_contract, executed_at=cutoff + (2 * _H)
    )
    # start at frozen+1h, end at cutoff+1h == frozen+4h: a three-hour review.
    assert observed.review_seconds == 3 * 3600


def test_accepted_disposition_without_a_grouped_save_is_incomplete(session, project):
    case = _freeze(session, project)
    cutoff = case.frozen_at + (2 * _H)
    contract = declare_capture_contract(
        session, _contract(case, cutoff=cutoff), now=case.frozen_at
    )
    _disposition(session, case.candidate_id, "accepted", created_at=case.frozen_at + _H)
    outcome = reconstruct_cutoff_outcome(session, case, contract, executed_at=cutoff)
    assert outcome.completeness == "incomplete"
    assert outcome.incomplete_reason == "accepted_outcome_missing_receipt"


def _reverse_disposition(session, disposition_id, candidate_id, *, created_at):
    audit = AuditLog(
        actor="local:capture-test",
        action="undo_disposition",
        entity_type="candidate",
        entity_id=candidate_id,
    )
    session.add(audit)
    session.flush([audit])
    session.add(
        StatementCoordinationReversal(
            candidate_disposition_id=disposition_id,
            candidate_id=candidate_id,
            audit_log_id=audit.id,
            recorded_by="local:capture-test",
            created_at=created_at,
        )
    )
    session.flush()


def test_a_corrected_decision_reflects_the_state_current_at_each_cutoff(session, project):
    case = _freeze(session, project)
    frozen = case.frozen_at
    # A first decision, then a compensating reversal, then a corrected decision.
    first = _disposition(
        session, case.candidate_id, "not_relevant",
        created_at=frozen + _H, reason="outside_project_scope",
    )
    _reverse_disposition(
        session, first.id, case.candidate_id, created_at=frozen + (2 * _H)
    )
    second = _disposition(
        session, case.candidate_id, "not_relevant",
        created_at=frozen + (3 * _H), reason="belongs_to_other_party",
    )

    # As of a cutoff before the correction, the first decision stands unreversed.
    before = declare_capture_contract(
        session, _contract(case, cutoff=frozen + _H), now=frozen
    )
    early = reconstruct_cutoff_outcome(
        session, case, before, executed_at=frozen + _H
    )
    assert early.outcome_identities["candidate_disposition_id"] == first.id
    assert early.undo is False

    # As of a cutoff after the correction, the corrected decision stands and the
    # earlier reversal is retained in the association's history.
    after = declare_capture_contract(
        session, _contract(case, cutoff=frozen + (3 * _H)), now=frozen
    )
    late = reconstruct_cutoff_outcome(
        session, case, after, executed_at=frozen + (3 * _H)
    )
    assert late.outcome_identities["candidate_disposition_id"] == second.id
    assert late.undo is True


def test_overlapping_protection_windows_stay_separately_identified(session, project):
    case = _freeze(session, project)
    frozen = case.frozen_at
    _disposition(
        session, case.candidate_id, "not_relevant",
        created_at=frozen + _H, reason="outside_project_scope",
    )
    # Two contracts over the same case with overlapping protection windows.
    window_a = declare_capture_contract(
        session,
        _contract(case, cutoff=frozen + (2 * _H), protection_end=frozen + (5 * _H)),
        now=frozen,
    )
    window_b = declare_capture_contract(
        session,
        _contract(case, cutoff=frozen + (3 * _H), protection_end=frozen + (6 * _H)),
        now=frozen,
    )
    assert window_a.contract_sha256 != window_b.contract_sha256
    assert window_a.protection_end < window_b.protection_end  # windows overlap
    a = reconstruct_cutoff_outcome(session, case, window_a, executed_at=frozen + (2 * _H))
    b = reconstruct_cutoff_outcome(session, case, window_b, executed_at=frozen + (3 * _H))
    # Each window reconstructs its own complete association; neither collapses the
    # other, and the same human outcome is a legitimate label in both windows.
    assert a.completeness == "complete" and b.completeness == "complete"
    assert a.human_outcome_identity == b.human_outcome_identity


# --------------------------------------------------------------------------- #
# Supported distinctions through a real guided Save, correction, and Undo
# --------------------------------------------------------------------------- #


def _accepted_case_with_scope(session, project):
    """A frozen case whose candidate is accepted through the real guided Save."""

    party = ExternalOrg(name="Kinder Morgan")
    session.add(party)
    roster = ProjectRosterEntry(
        project_id=project.id,
        principal_subject="local:dana",
        display_name="Dana Fields",
    )
    session.add(roster)
    session.flush()
    quote = "Kinder Morgan will provide the relocation schedule by June 1, 2026."
    document = Document(
        project_id=project.id,
        sha256=hashlib.sha256(f"{project.id}:acc:{uuid4()}".encode()).hexdigest(),
        filename="accepted.pdf",
        doc_type="minutes",
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    session.add(DocPage(document_id=document.id, page_no=1, text=quote))
    candidate = Candidate(
        project_id=project.id,
        kind="event",
        payload_json={
            "kind": "event",
            "fields": {"event_type": "commitment", "description": quote},
            "citations": [
                {"document_id": document.id, "page": 1, "quote": quote, "verified": True}
            ],
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=0.9,
        prompt_version="capture-accepted",
        model="capture-accepted",
        citations_verified=True,
    )
    run = record_extraction_run(
        session, document, prompt_version="capture-accepted", candidate_count=1,
        page_errors=0, candidates=[candidate], model="capture-accepted",
        allow_unsealed_legacy=True,
    )
    declare_active_run(session, document.id, run.id, principal=RECORDER)
    from corridor.models import Dependency

    dependency = Dependency(
        project_id=project.id, ref_code=f"DEP-{uuid4().hex[:8]}",
        dep_type="utility_relocation", title="KM line",
        station_from="6608+70", station_to="6616+50", external_org_id=party.id,
    )
    session.add(dependency)
    session.flush()
    frozen_at = datetime.now(timezone.utc) - (6 * _H)
    case = EvidenceInvestigationShadowCase(
        public_id=str(uuid4()),
        project_id=project.id,
        candidate_id=candidate.id,
        extraction_run_id=candidate.extraction_run_id,
        candidate_payload_sha256="b" * 64,
        read_fingerprint="c" * 64,
        model="capture-accepted",
        prompt_version="capture-accepted",
        prompt_sha256="1" * 64,
        adapter_contract_version="capture-contract-v1",
        tool_contract_version="tool-contract-v1",
        transport_gate_sha256="a" * 64,
        budget_json={},
        case_json={},
        registered_evidence_json=[],
        option_population_json={},
        option_population_sha256="d" * 64,
        frozen_at=frozen_at,
    )
    session.add(case)
    session.flush([case])
    draft = StatementCoordinationDraft(
        candidate_id=candidate.id,
        affected_external_org_id=party.id,
        stated_party=party.name,
        stated_external_org_id=party.id,
        event_date=date(2025, 1, 16),
        description=quote,
        new_timing=StatementTiming.day("June 1, 2026", date(2026, 6, 1)),
        previous_timing=None,
        evidence=(CitedStatementEvidence(document.id, 1, quote),),
        scope=StatementScope.selected((dependency.id,)),
        internal_owner_roster_entry_id=roster.id,
        next_action="Confirm the revised completion plan with Kinder Morgan",
        action_due_date=date(2026, 2, 1),
        action_due_date_unknown_reason=None,
        milestone_impact=None,
    )
    result = coordinate_statement(session, draft, principal=RECORDER)
    return case, dependency, result


def test_accepted_single_scope_is_preserved_as_of_the_cutoff(session, project):
    case, dependency, _result = _accepted_case_with_scope(session, project)
    cutoff = datetime.now(timezone.utc) + timedelta(days=1)
    contract = declare_capture_contract(
        session, _contract(case, cutoff=cutoff), now=case.frozen_at
    )
    outcome = reconstruct_cutoff_outcome(session, case, contract, executed_at=cutoff)
    assert outcome.completeness == "complete"
    assert outcome.candidate_disposition == "accepted"
    assert outcome.scope_mode is not None
    assert outcome.selected_dependency_ids == (dependency.id,)
    assert "single_dependency" in outcome.strata

    # The cutoff reconstruction agrees with the immutable one-time capture, and
    # neither rewrites the other: they are separate records.
    one_time = capture_shadow_outcome(session, case.public_id)
    assert one_time.candidate_disposition == "accepted"
    assert list(outcome.selected_dependency_ids) == one_time.selected_dependency_ids_json


def test_undo_is_visible_only_from_a_cutoff_after_it(session, project):
    case, _dependency, result = _accepted_case_with_scope(session, project)
    accepted_at = session.get(
        CandidateDisposition, result.receipt.candidate_disposition_id
    ).created_at
    # The compensating Undo is appended after the Save. (A real Undo shares this
    # transaction's clock, so its exact time is pinned explicitly for the test.)
    reversed_at = accepted_at + timedelta(minutes=1)
    audit = AuditLog(
        actor="local:capture-test",
        action="undo_coordinated_statement",
        entity_type="candidate",
        entity_id=case.candidate_id,
    )
    session.add(audit)
    session.flush([audit])
    session.add(
        StatementCoordinationReversal(
            receipt_id=result.receipt.id,
            candidate_id=case.candidate_id,
            audit_log_id=audit.id,
            recorded_by="local:capture-test",
            created_at=reversed_at,
        )
    )
    session.flush()

    # As of a cutoff at the accepted Save (before the Undo), the outcome stands.
    early_contract = declare_capture_contract(
        session, _contract(case, cutoff=accepted_at), now=case.frozen_at
    )
    early = reconstruct_cutoff_outcome(
        session, case, early_contract, executed_at=accepted_at
    )
    assert early.candidate_disposition == "accepted"
    assert early.undo is False

    # As of a cutoff at the Undo, the disposition is reversed and unresolved.
    late_contract = declare_capture_contract(
        session, _contract(case, cutoff=reversed_at), now=case.frozen_at
    )
    late = reconstruct_cutoff_outcome(
        session, case, late_contract, executed_at=reversed_at
    )
    assert late.undo is True
    assert late.unresolved is True


# --------------------------------------------------------------------------- #
# Identity binding and duplicate refusal
# --------------------------------------------------------------------------- #


def test_association_binds_exact_case_run_and_outcome_identities(session, project):
    case = _freeze(session, project)
    cutoff = case.frozen_at + (2 * _H)
    contract = declare_capture_contract(
        session, _contract(case, cutoff=cutoff), now=case.frozen_at
    )
    _disposition(
        session, case.candidate_id, "not_relevant",
        created_at=case.frozen_at + _H, reason="outside_project_scope",
    )
    outcome = reconstruct_cutoff_outcome(session, case, contract, executed_at=cutoff)
    # The association names the exact project, frozen case, run, and human-outcome
    # source identities, so it can never be mistaken for another case's outcome.
    assert outcome.outcome_identities["shadow_case_id"] == case.id
    assert outcome.outcome_identities["candidate_id"] == case.candidate_id
    assert outcome.outcome_identities["candidate_disposition_id"] is not None
    assert outcome.human_outcome_identity is not None


def test_reconstruction_refuses_a_case_from_another_project(session, project):
    other = Project(slug=f"other-{uuid4().hex}", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    seed_membership(session, other, RECORDER)
    # A contract sealed for `project`, and a frozen case that belongs to `other`.
    home_case = _freeze(session, project)
    contract = declare_capture_contract(
        session, _contract(home_case, cutoff=home_case.frozen_at + _H),
        now=home_case.frozen_at,
    )
    foreign_case = _freeze(session, other)
    outcome = reconstruct_cutoff_outcome(
        session, foreign_case, contract, executed_at=contract.cutoff_at
    )
    assert outcome.completeness == "incomplete"
    assert outcome.incomplete_reason == "cross_project_or_missing_candidate"


# --------------------------------------------------------------------------- #
# The shared Due Work runtime: timing, idempotency, recovery, protection
# --------------------------------------------------------------------------- #


class ControlledClock:
    def __init__(self, value: datetime):
        self.value = value

    def now(self) -> datetime:
        return self.value


def _committed_case(factory):
    """Freeze a case, record a Not Relevant decision, and enable capture.

    Returns identities plus the intended cutoff, all anchored to the real freeze
    time so the human decision, the cutoff, and the controlled runtime clock stay
    consistent.
    """

    with factory() as setup:
        project = Project(
            slug=f"capture-rt-{uuid4().hex}", name="Capture runtime", is_synthetic=True
        )
        setup.add(project)
        setup.flush()
        seed_membership(setup, project, RECORDER)
        candidate = _unplaced_statement(setup, project)
        [shadow] = asyncio.run(
            run_shadow_batch(
                setup, project.id, runtime_factory=_StubRuntime,
                identity=_identity(model=f"rt-{uuid4().hex[:8]}"),
                budget=InvestigationBudget(),
            )
        )
        case = shadow.case
        frozen = case.frozen_at
        cutoff = frozen + _H
        _disposition(
            setup, candidate.id, "not_relevant",
            created_at=frozen + timedelta(minutes=1),
            reason="outside_project_scope",
        )
        contract = declare_capture_contract(
            setup,
            _contract(case, cutoff=cutoff, protection_end=cutoff + (4 * _H)),
            now=frozen,
        )
        schedule = configure_evidence_outcome_capture(
            setup,
            EvidenceOutcomeCaptureDeclaration.released_hourly(
                project_id=project.id,
                configuration_version="capture-config-v1",
                cohort_id=contract.cohort_id,
                observation_contract_sha256=contract.contract_sha256,
                starts_at=(frozen - (2 * _H)).replace(
                    minute=0, second=0, microsecond=0
                ),
            ),
            now=frozen,
        )
        ids = (
            project.id, contract.id, contract.contract_sha256, schedule.id, cutoff
        )
        setup.commit()
    return ids


def test_runtime_refuses_before_cutoff_then_associates_late_and_reads_back(
    runtime_database,
):
    factory = runtime_database.session_factory
    project_id, contract_id, _sha, _schedule_id, cutoff = _committed_case(factory)

    # A tick BEFORE the declared cutoff refuses to associate; the case is pending.
    before = cutoff - timedelta(minutes=30)
    with factory() as ticking:
        with ticking.begin():
            enqueue_due_work(ticking, now=before)
    early = run_due_work_once(
        factory, clock=ControlledClock(before), owner="runtime:capture-worker"
    )
    assert early is not None
    assert early.handler_result["before_cutoff_pending"] == 1
    assert early.handler_result["captured_complete"] == 0
    with factory() as reading:
        assert reading.scalar(
            select(func.count()).select_from(EvidenceInvestigationCaptureResult).where(
                EvidenceInvestigationCaptureResult.capture_contract_id == contract_id
            )
        ) == 0

    # A later tick associates facts as of the cutoff, honestly late.
    executed = cutoff + (2 * _H)  # still inside protection
    with factory() as ticking:
        with ticking.begin():
            enqueue_due_work(ticking, now=executed)
    result = run_due_work_once(
        factory, clock=ControlledClock(executed), owner="runtime:capture-worker"
    )
    assert result is not None
    assert result.handler_key == HANDLER_EVIDENCE_OUTCOME_CAPTURE
    assert result.execution_outcome == "completed"
    assert result.handler_result["captured_complete"] == 1

    with factory() as reading:
        rows = reading.scalars(
            select(EvidenceInvestigationCaptureResult).where(
                EvidenceInvestigationCaptureResult.capture_contract_id == contract_id
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].completeness == "complete"
        assert rows[0].cutoff_at == cutoff
        assert rows[0].executed_at == executed
        assert rows[0].executed_at > rows[0].cutoff_at  # honestly late
        assert rows[0].candidate_disposition == "not_relevant"
        status = capture_status(reading, project_id=project_id)
    result_view = status["results"][0]
    assert result_view["cutoff_at"] != result_view["executed_at"]
    assert result_view["on_time"] is False
    assert result_view["protection_active"] is True  # protection not released


def test_runtime_repeated_ticks_and_competing_workers_converge(runtime_database):
    factory = runtime_database.session_factory
    project_id, contract_id, contract_sha, _schedule_id, cutoff = _committed_case(
        factory
    )
    executed = cutoff + _H

    # Repeated ticks: a second full run finds the association already present.
    for _ in range(2):
        with factory() as ticking:
            with ticking.begin():
                enqueue_due_work(ticking, now=executed)
        run_due_work_once(
            factory, clock=ControlledClock(executed), owner="runtime:capture-worker"
        )

    # Competing direct passes race the same durable pass.
    barrier = Barrier(2)

    def _pass():
        barrier.wait()
        return run_outcome_capture(
            factory, project_id=project_id, contract_sha256=contract_sha,
            configuration_version="capture-config-v1", clock=ControlledClock(executed),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        [future.result() for future in [pool.submit(_pass), pool.submit(_pass)]]

    with factory() as reading:
        assert reading.scalar(
            select(func.count()).select_from(EvidenceInvestigationCaptureResult).where(
                EvidenceInvestigationCaptureResult.capture_contract_id == contract_id
            )
        ) == 1


def test_capture_never_rewrites_the_one_time_capture_or_the_frozen_case(session, project):
    case = _freeze(session, project)
    cutoff = case.frozen_at + (2 * _H)
    _disposition(
        session, case.candidate_id, "not_relevant",
        created_at=case.frozen_at + _H, reason="outside_project_scope",
    )
    contract = declare_capture_contract(
        session, _contract(case, cutoff=cutoff), now=case.frozen_at
    )
    one_time = capture_shadow_outcome(session, case.public_id)
    one_time_sha = one_time.outcome_sha256
    frozen_at = case.frozen_at

    reconstruct_cutoff_outcome(session, case, contract, executed_at=cutoff)

    session.refresh(one_time)
    session.refresh(case)
    assert one_time.outcome_sha256 == one_time_sha
    assert case.frozen_at == frozen_at
    # There is exactly one immutable one-time outcome and it is a distinct record.
    assert session.scalar(
        select(func.count()).select_from(EvidenceInvestigationShadowOutcome).where(
            EvidenceInvestigationShadowOutcome.shadow_case_id == case.id
        )
    ) == 1
