"""One reading of a frozen case's independent human outcome as of an instant.

The one-time shadow capture reads the outcome at the moment it runs; the
scheduled cutoff capture reads it as of a declared cutoff. Both are the same
rule with a time parameter, so these tests read one fixture through
``read_human_outcome`` at now and at a cutoff after every human act and prove
the two readings identical, then move the instant to before an act and prove
the act disappears. The digest each writer records as ``human_outcome_identity``
is pinned here by spelling it out, so a rewording of the reading cannot change
what existing rows say.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from corridor.evidence_investigator_capture import (
    CaptureObservationContract,
    declare_capture_contract,
    reconstruct_cutoff_outcome,
)
from corridor.evidence_investigator_evaluation import (
    EVALUATION_STRATA,
    REQUIRED_STRATA,
)
from corridor.evidence_investigator_human_outcome import (
    HUMAN_OUTCOME_STRATA,
    HumanOutcomeUnreadable,
    read_human_outcome,
)
from corridor.evidence_investigator_shadow import capture_shadow_outcome
from corridor.external_statements import (
    CitedStatementEvidence,
    StatementScope,
    StatementTiming,
)
from corridor.models import (
    CandidateDisposition,
    DependencyEvent,
    DependencyEventScopeDecision,
    Document,
    EvidenceInvestigationReviewObservation,
    EvidenceInvestigationShadowCase,
    Project,
)
from corridor.statement_coordination import (
    StatementFactCorrectionDraft,
    correct_statement_facts,
)
from access_support import seed_membership
from evidence_outcome_support import (
    HOUR,
    RECORDER,
    accepted_case_with_scope,
    freeze_case,
    record_disposition,
    reverse,
)


FAR_CUTOFF = datetime.now(timezone.utc) + timedelta(days=1)
INSTANTS = ("now", "cutoff after every act")


@pytest.fixture
def project(member_project):
    return member_project(RECORDER)


def _as_of(instant):
    return None if instant == "now" else FAR_CUTOFF


def _observe(session, case, boundary, at):
    session.add(
        EvidenceInvestigationReviewObservation(
            shadow_case_id=case.id,
            boundary=boundary,
            principal="local:reviewer",
            observed_at=at,
        )
    )
    session.flush()


def _spelled_identity(candidate_id, disposition_id, receipt_id, reversal_ids, unresolved):
    return hashlib.sha256(
        json.dumps(
            {
                "candidate_id": candidate_id,
                "candidate_disposition_id": disposition_id,
                "statement_coordination_receipt_id": receipt_id,
                "reversal_ids": list(reversal_ids),
                "unresolved": unresolved,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def _correct_facts(session, case, result, document):
    corrected = "Kinder Morgan will provide the relocation schedule by July 1, 2026."
    return correct_statement_facts(
        session,
        StatementFactCorrectionDraft(
            candidate_id=case.candidate_id,
            expected_statement_event_id=result.event.id,
            affected_external_org_id=result.event.affected_external_org_id,
            stated_party="Kinder Morgan",
            stated_external_org_id=result.event.affected_external_org_id,
            event_date=date(2025, 1, 17),
            description=corrected,
            new_timing=StatementTiming.day("July 1, 2026", date(2026, 7, 1)),
            previous_timing=None,
            evidence=(CitedStatementEvidence(document.id, 1, corrected),),
        ),
        principal=RECORDER,
    )


# --------------------------------------------------------------------------- #
# The same rule at now and at a cutoff after every act
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("instant", INSTANTS)
def test_an_undecided_case_is_unresolved(session, project, instant):
    case = freeze_case(session, project)
    _observe(session, case, "start", case.frozen_at + HOUR)

    outcome = read_human_outcome(session, case, as_of=_as_of(instant))

    assert outcome.candidate_id == case.candidate_id
    assert outcome.run_id is not None
    assert outcome.candidate_disposition is None
    assert outcome.unresolved is True
    assert outcome.undo is False
    assert outcome.correction is False
    assert outcome.review_seconds is None
    assert outcome.strata == ("unresolved",)
    assert outcome.human_outcome_identity == _spelled_identity(
        case.candidate_id, None, None, (), True
    )


@pytest.mark.parametrize("instant", INSTANTS)
def test_a_not_relevant_decision_with_a_bounded_review(session, project, instant):
    case = freeze_case(session, project)
    _observe(session, case, "start", case.frozen_at + HOUR)
    decision = record_disposition(
        session, case.candidate_id, "not_relevant",
        created_at=case.frozen_at + (2 * HOUR), reason="outside_project_scope",
    )
    _observe(session, case, "end", case.frozen_at + (2 * HOUR))

    outcome = read_human_outcome(session, case, as_of=_as_of(instant))

    assert outcome.candidate_disposition == "not_relevant"
    assert outcome.candidate_disposition_id == decision.id
    assert outcome.last_candidate_disposition_id == decision.id
    assert outcome.scope_mode is None
    assert outcome.unresolved is False
    assert outcome.review_seconds == 3600
    assert outcome.strata == ("not_relevant",)
    assert outcome.human_outcome_identity == _spelled_identity(
        case.candidate_id, decision.id, None, (), False
    )


@pytest.mark.parametrize("instant", INSTANTS)
@pytest.mark.parametrize(
    ("scope", "scope_mode"),
    [(None, "selected"), (StatementScope.all_active(), "all_active")],
    ids=["selected", "all_active"],
)
def test_an_accepted_save_is_read_from_its_grouped_receipt(
    session, project, instant, scope, scope_mode
):
    case, dependency, result, _document = accepted_case_with_scope(
        session, project, scope=scope
    )

    outcome = read_human_outcome(session, case, as_of=_as_of(instant))

    assert outcome.candidate_disposition == "accepted"
    assert outcome.statement_coordination_receipt_id == result.receipt.id
    assert outcome.scope_decision_id == result.receipt.scope_decision_id
    assert outcome.scope_mode == scope_mode
    assert outcome.selected_dependency_ids == (dependency.id,)
    assert outcome.correction is False
    assert outcome.undo is False
    assert outcome.unresolved is False
    # An all-active scope is counted by the Constraints it snapshotted, like a
    # selected one; no scope mode produces a stratum of its own.
    assert outcome.strata == ("single_dependency",)
    assert outcome.human_outcome_identity == _spelled_identity(
        case.candidate_id, result.receipt.candidate_disposition_id, result.receipt.id,
        (), False,
    )


@pytest.mark.parametrize("instant", INSTANTS)
def test_an_undone_save_leaves_the_case_unresolved_with_its_reversal(
    session, project, instant
):
    case, _dependency, result, _document = accepted_case_with_scope(session, project)
    accepted_at = session.get(
        CandidateDisposition, result.receipt.candidate_disposition_id
    ).created_at
    reversal = reverse(
        session, case.candidate_id, receipt_id=result.receipt.id,
        created_at=accepted_at + timedelta(minutes=1),
    )

    outcome = read_human_outcome(session, case, as_of=_as_of(instant))

    assert outcome.candidate_disposition is None
    assert outcome.last_candidate_disposition_id == result.receipt.candidate_disposition_id
    assert outcome.reversal_ids == (reversal.id,)
    assert outcome.undo is True
    assert outcome.unresolved is True
    assert outcome.strata == ("unresolved",)
    assert outcome.human_outcome_identity == _spelled_identity(
        case.candidate_id, None, None, (reversal.id,), True
    )


@pytest.mark.parametrize("instant", INSTANTS)
def test_a_factual_correction_marks_the_save_later_corrected(session, project, instant):
    case, dependency, result, document = accepted_case_with_scope(session, project)
    _correct_facts(session, case, result, document)

    outcome = read_human_outcome(session, case, as_of=_as_of(instant))

    # A factual correction appends a successor statement in the lineage and
    # writes no second grouped Save receipt, so the lineage reader is the one
    # definition that sees it.
    assert outcome.candidate_disposition == "accepted"
    assert outcome.correction is True
    assert outcome.selected_dependency_ids == (dependency.id,)
    assert outcome.strata == ("later_corrected", "single_dependency")


# --------------------------------------------------------------------------- #
# The instant excludes every later human act
# --------------------------------------------------------------------------- #


def test_a_decision_counts_from_its_own_instant_and_not_before(session, project):
    case = freeze_case(session, project)
    decided_at = case.frozen_at + (3 * HOUR)
    record_disposition(
        session, case.candidate_id, "not_relevant",
        created_at=decided_at, reason="outside_project_scope",
    )

    before = read_human_outcome(session, case, as_of=decided_at - HOUR)
    at = read_human_outcome(session, case, as_of=decided_at)

    assert before.unresolved is True and before.candidate_disposition is None
    assert at.unresolved is False and at.candidate_disposition == "not_relevant"


def test_review_seconds_use_only_boundaries_observed_by_the_instant(session, project):
    case = freeze_case(session, project)
    _observe(session, case, "start", case.frozen_at + HOUR)
    _observe(session, case, "end", case.frozen_at + (4 * HOUR))

    early = read_human_outcome(session, case, as_of=case.frozen_at + (3 * HOUR))
    late = read_human_outcome(session, case, as_of=case.frozen_at + (4 * HOUR))

    assert early.review_seconds is None
    assert late.review_seconds == 3 * 3600


def test_a_reversed_decision_stands_until_its_reversal(session, project):
    case = freeze_case(session, project)
    frozen = case.frozen_at
    first = record_disposition(
        session, case.candidate_id, "not_relevant",
        created_at=frozen + HOUR, reason="outside_project_scope",
    )
    reversal = reverse(
        session, case.candidate_id, disposition_id=first.id, created_at=frozen + (2 * HOUR)
    )
    second = record_disposition(
        session, case.candidate_id, "not_relevant",
        created_at=frozen + (3 * HOUR), reason="belongs_to_other_party",
    )

    early = read_human_outcome(session, case, as_of=frozen + HOUR)
    late = read_human_outcome(session, case, as_of=frozen + (3 * HOUR))

    assert early.candidate_disposition_id == first.id
    assert early.undo is False and early.reversal_ids == ()
    assert late.candidate_disposition_id == second.id
    assert late.last_candidate_disposition_id == second.id
    assert late.undo is True and late.reversal_ids == (reversal.id,)


def test_an_undo_is_visible_only_from_an_instant_after_it(session, project):
    case, _dependency, result, _document = accepted_case_with_scope(session, project)
    accepted_at = session.get(
        CandidateDisposition, result.receipt.candidate_disposition_id
    ).created_at
    reversed_at = accepted_at + timedelta(minutes=1)
    reverse(session, case.candidate_id, receipt_id=result.receipt.id, created_at=reversed_at)

    early = read_human_outcome(session, case, as_of=accepted_at)
    late = read_human_outcome(session, case, as_of=reversed_at)

    assert early.candidate_disposition == "accepted" and early.undo is False
    assert late.undo is True and late.unresolved is True


def test_later_corrected_is_read_as_of_the_instant(runtime_database):
    # Statements are append-only, so a successor's instant cannot be pinned by
    # hand; the Save and the correction are committed in separate transactions
    # and so carry the server's two distinct transaction times.
    factory = runtime_database.session_factory
    with factory() as saving:
        with saving.begin():
            project = Project(
                slug=f"human-outcome-{uuid4().hex}", name="Corrected", is_synthetic=True
            )
            saving.add(project)
            saving.flush()
            seed_membership(saving, project, RECORDER)
            case, _dependency, result, document = accepted_case_with_scope(
                saving, project
            )
            case_id, event_id, document_id = case.id, result.event.id, document.id
            accepted_at = saving.get(
                CandidateDisposition, result.receipt.candidate_disposition_id
            ).created_at
    with factory() as correcting:
        with correcting.begin():
            case = correcting.get(EvidenceInvestigationShadowCase, case_id)
            result = SimpleNamespace(event=correcting.get(DependencyEvent, event_id))
            _correct_facts(correcting, case, result, correcting.get(Document, document_id))
    with factory() as reading:
        case = reading.get(EvidenceInvestigationShadowCase, case_id)
        before = read_human_outcome(reading, case, as_of=accepted_at)
        after = read_human_outcome(reading, case)

    assert before.correction is False and "later_corrected" not in before.strata
    assert after.correction is True and "later_corrected" in after.strata


# --------------------------------------------------------------------------- #
# The digest both writers record, and what the reading refuses
# --------------------------------------------------------------------------- #


def test_both_writers_record_the_pinned_human_outcome_identity(session, project):
    case = freeze_case(session, project)
    cutoff = case.frozen_at + (2 * HOUR)
    decision = record_disposition(
        session, case.candidate_id, "not_relevant",
        created_at=case.frozen_at + HOUR, reason="outside_project_scope",
    )
    expected = _spelled_identity(case.candidate_id, decision.id, None, (), False)

    assert read_human_outcome(session, case).human_outcome_identity == expected
    one_time = capture_shadow_outcome(session, case.public_id)
    assert one_time.human_outcome_identity == expected
    contract = declare_capture_contract(
        session,
        _capture_contract(case, cutoff=cutoff),
        now=case.frozen_at,
    )
    cutoff_outcome = reconstruct_cutoff_outcome(
        session, case, contract, executed_at=cutoff
    )
    assert cutoff_outcome.completeness == "complete"
    assert cutoff_outcome.human_outcome_identity == expected


def _capture_contract(case, *, cutoff):
    return CaptureObservationContract(
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
        window_start=case.frozen_at - HOUR,
        cutoff_at=cutoff,
        protection_end=cutoff + HOUR,
        history_retained_from=case.frozen_at - HOUR,
        eligibility="unplaced-statement-v1",
        missing_label_policy="remain_missing",
        member_case_public_ids=(case.public_id,),
        declared_by="local:capture-approver",
    )


def test_an_accepted_decision_without_its_grouped_save_cannot_be_read(session, project):
    case = freeze_case(session, project)
    record_disposition(
        session, case.candidate_id, "accepted", created_at=case.frozen_at + HOUR
    )

    with pytest.raises(HumanOutcomeUnreadable) as refused:
        read_human_outcome(session, case)

    assert refused.value.reason == "accepted_outcome_missing_receipt"
    # The one-time writer keeps refusing as a ValueError; the cutoff writer's
    # incomplete row is proved in its own file.
    with pytest.raises(ValueError):
        capture_shadow_outcome(session, case.public_id)


def test_the_strata_vocabulary_is_declared_once():
    assert EVALUATION_STRATA is HUMAN_OUTCOME_STRATA
    assert set(REQUIRED_STRATA) == set(HUMAN_OUTCOME_STRATA) - {"multiple_dependencies"}
    # No scope mode the schema admits produces an `all_active_snapshot` stratum;
    # the cutoff reading used to emit one the evaluator never counted.
    modes = next(
        constraint
        for constraint in DependencyEventScopeDecision.__table__.constraints
        if constraint.name == "ck_dependency_event_scope_decisions_mode"
    )
    assert "all_active_snapshot" not in str(modes.sqltext)
    assert "all_active_snapshot" not in HUMAN_OUTCOME_STRATA
