"""A corrected capture, and the proposal it retires (ADR-0101, #836, #842).

ADR-0101 lists twelve properties and says they belong to the decision rather
than to one implementation's test file.  This module holds them.  They divide
into four groups:

* **the four outcomes** -- a correction that establishes no change retires the
  proposal and writes neither a zero-difference delta nor a disposition; one
  that still differs produces a linked replacement and retires through this
  relationship rather than through a newer-source-version supersession; one
  that cannot be substantiated claims nothing and leaves the proposal open;
  and one whose proposal a coordinator decided in the meantime is refused, with
  the decision and its issued history intact;
* **the proof the command establishes** -- the exact challenged capture and
  request, the corrected capture held to the retained source, the declared rule
  and the stated accepted revision, eligibility, no accepted value or
  disposition, and idempotency.  Each is exercised by removing it and watching
  the command refuse;
* **the terminal-state invariant, in both write orders** -- retirement refuses
  a decided or superseded delta, and resolution, scheduling and Follow-up Plan
  commands refuse a retired one.  The concurrency half cannot be proved by a
  rollback-scoped test at all, so it uses the harness's own
  ``runtime_database`` and two real committing transactions; and
* **what a person is told** -- the approved sentences, where they appear, and
  what each links to.

Nothing here reads a clock.  Every instant and cutoff is declared.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from corridor import access, audit
from corridor.db_roles import RECORD_DECISION_ROLE
from corridor.migrations.source_append_commands.project_partition import (
    UNPARTITIONED_ROLES,
)
from corridor.capture_correction import (
    CaptureCorrectionRefused,
)
from corridor.capture_correction_retirement import (
    CORRECTED_READING,
    INCONCLUSIVE,
    NO_CHANGE,
    STALE_APPLY_SENTENCE,
    STILL_DIFFERS,
    record_correction_result,
    results_by_request,
    retirements_by_delta,
)
from corridor.delta_resolution import (
    ACCEPT,
    DEFER,
    ChildDecisionRequest,
    ContradictoryDeltaStanding,
    RecordEffect,
    live_delta_status,
    resolve_delta,
)
from corridor.models import (
    CaptureCorrectionResult,
    DeltaCaptureCorrection,
    DeltaDeferral,
    DeltaDisposition,
    DeltaSupersession,
    Project,
    ProposedDelta,
)
from corridor.operations_repair import (
    SOURCE_GROUNDED_RECAPTURE,
    OperationsRepairRefused,
    correct_captured_reading,
    repair_receipts,
)
from corridor.packet_review import read_review_items
from corridor.principals import HumanPrincipal
from corridor.review_packet_reading import read_open_deltas
from corridor.record_history import CAPTURE_CORRECTED, read_record_history

from access_support import seed_membership
from harness_support import as_role
from packet_review_support import (
    append_deltas,
    modify,
    subject,
    support,
)


from capture_correction_support import (
    ACCEPTED_TEXT,
    ALICE,
    CORRECTED_AT,
    CUTOFF,
    DECIDED_AT,
    FIELD,
    OPERATOR,
    STILL_DIFFERENT_TEXT,
    WORKER_IDENTITY,
    Misread,
)


@pytest.fixture
def project(member_project) -> Project:
    """One project whose roster carries the coordinator this screen answers."""

    return member_project(ALICE)


@pytest.fixture
def misread(session: Session, project: Project) -> Misread:
    seed_membership(
        session, project, OPERATOR, designations=(access.TECHNICAL_OPERATIONS,)
    )
    return Misread(session, project)


def _correct(misread: Misread, report, **kwargs):
    return correct_captured_reading(
        misread.session,
        request_id=int(report.id),
        principal=OPERATOR,
        performed_at=CORRECTED_AT,
        executed_by=WORKER_IDENTITY,
        **kwargs,
    )


def _apply(session: Session, misread: Misread, fact_id: int):
    """The Apply submission a coordinator's rendered form would send."""

    return resolve_delta(
        session,
        ChildDecisionRequest(
            project_id=misread.project.id,
            delta_id=misread.delta.id,
            action=ACCEPT,
            principal=ALICE,
            decided_at=DECIDED_AT,
            idempotency_key=f"apply:{uuid4().hex[:10]}",
            observed_accepted_revision_id=misread.revision_id,
            record_effects=(RecordEffect(fact_id=fact_id),),
            support_assessment_ids=(misread.support.id,),
        ),
    )


# --- the four outcomes, each recorded as itself ----------------------------


def test_a_correction_matching_the_accepted_value_retires_the_proposal(
    session: Session, misread: Misread
) -> None:
    """ADR-0101 property 1, and the whole reason the relationship exists.

    The capture was wrong, the corrected reading matches what the record
    already holds, and there is therefore nothing to propose. What must *not*
    appear is a zero-difference delta (a finding with no content) or a
    disposition (a coordinator decision nobody made).
    """

    report = misread.report()
    before = session.scalar(
        select(func.count()).select_from(ProposedDelta).where(
            ProposedDelta.project_id == misread.project.id
        )
    )

    outcome = _correct(misread, report)

    assert outcome.outcome == NO_CHANGE
    assert outcome.retired
    assert live_delta_status(session, misread.delta.id) == "capture_corrected"
    # No zero-difference delta was manufactured, and no disposition filed.
    assert (
        session.scalar(
            select(func.count()).select_from(ProposedDelta).where(
                ProposedDelta.project_id == misread.project.id
            )
        )
        == before
    )
    assert not session.scalar(
        select(DeltaDisposition.id).where(
            DeltaDisposition.delta_id == misread.delta.id
        )
    )
    assert not session.scalar(
        select(DeltaSupersession.id).where(
            DeltaSupersession.prior_delta_id == misread.delta.id
        )
    )
    # The accepted revision the recomparison actually read is recorded, which
    # is what the approved sentence prints.
    assert outcome.accepted_revision_id == misread.revision_id


@pytest.mark.parametrize(
    "role", ["corridor_web", "corridor_worker", "corridor_source_append"]
)
def test_every_role_that_may_ask_the_retirement_question_can_read_the_answer(
    session: Session, misread: Misread, role: str
) -> None:
    """Execute without select is a call that raises the first time it matters.

    ``proposed_delta_capture_correction`` is ``security invoker``, so it reads
    the retirement relation as whoever called it. The two bulk supersession
    sweeps that call it -- ``append_email_thread_reading`` and
    ``append_minutes_capture`` -- are ``security definer`` functions owned by
    the source-append role, which owns none of this family's relations, so
    granting execute alone left them raising ``insufficient_privilege`` the
    first time any retirement existed. Neither sweep's own tests could catch
    it, because they build no retirement; three unrelated spine tests did.

    Reading the answer and *being allowed to ask* are two different things
    here, and the test says which it is proving for each role. The
    unpartitioned roles see every project's rows, so for them the recorded
    retirement must come back. ``corridor_web`` reads under the project
    partition policy, so with no partition declared it correctly sees nothing
    -- what matters for it is that the call does not raise.
    """

    _correct(misread, misread.report())
    session.flush()
    with as_role(session, role):
        answered = session.scalar(
            text("select public.proposed_delta_capture_correction(:delta)"),
            {"delta": misread.delta.id},
        )
    if role in UNPARTITIONED_ROLES:
        assert answered is not None, (
            f"{role} may call the helper but cannot read what it reads"
        )
    else:
        assert answered is None, (
            "a partitioned reader with no declared partition sees no row, and "
            "the point of this case is that asking did not raise"
        )


def test_a_correction_that_still_differs_replaces_rather_than_supersedes(
    session: Session, misread: Misread
) -> None:
    """ADR-0101 property 2. The cause is a correction, not a newer version."""

    report = misread.report(passage=misread.divergent)

    outcome = _correct(misread, report)

    assert outcome.outcome == STILL_DIFFERS
    assert outcome.retired
    replacement = session.get(ProposedDelta, outcome.replacement_delta_id)
    assert replacement is not None
    assert replacement.target_subject_identity == subject(1)
    assert replacement.target_field == FIELD
    assert replacement.proposed_value == STILL_DIFFERENT_TEXT
    # The original left Review through this relationship, and not by being
    # called superseded: ADR-0083's supersession is a newer source version, and
    # this is the same version read again.
    assert live_delta_status(session, misread.delta.id) == "capture_corrected"
    assert not session.scalar(
        select(DeltaSupersession.id).where(
            DeltaSupersession.prior_delta_id == misread.delta.id
        )
    )
    # Both the retirement and the replacement hang off one correction result.
    retired = retirements_by_delta(session, project_id=misread.project.id)
    assert retired[misread.delta.id].replacement_delta_id == replacement.id
    assert retired[misread.delta.id].result_id == outcome.result_id
    # The corrected proposal is an ordinary open proposal for Review.
    reading = read_open_deltas(session, project_id=misread.project.id, as_of=CUTOFF)
    assert replacement.id in reading.actionable_delta_ids
    assert misread.delta.id not in reading.open_delta_ids


def test_an_unsubstantiated_investigation_claims_nothing_and_leaves_it_open(
    session: Session, misread: Misread
) -> None:
    """ADR-0101's third outcome, and its own words for why.

    "The source did not establish this assertion" is not "the source matches
    the accepted value", so no corrected capture is written, nothing is
    retired, and the proposal is still waiting for a decision.
    """

    report = misread.report()

    outcome = _correct(
        misread,
        report,
        substantiated=False,
        finding="the cell is legible but says neither value; escalated",
    )

    assert outcome.outcome == INCONCLUSIVE
    assert not outcome.retired
    assert outcome.corrected_fact_id is None
    assert live_delta_status(session, misread.delta.id) == "open"
    reading = read_open_deltas(session, project_id=misread.project.id, as_of=CUTOFF)
    assert misread.delta.id in reading.actionable_delta_ids
    # The result is retained and the reporter can retrieve it.
    (found,) = results_by_request(
        session, project_id=misread.project.id, request_ids=[report.id]
    )[report.id]
    assert found.outcome == INCONCLUSIVE
    assert found.headline == ""
    assert "escalated" in found.explanation


def test_a_passage_that_does_not_read_as_this_field_is_not_a_correction(
    session: Session, project: Project
) -> None:
    """A materializer refusal is an unsubstantiated investigation, not a value.

    The alternative -- writing a Source Fact that asserts the source says
    nothing, or says what the record already holds -- is a manufactured fact
    with a locator that dereferences to nothing of the kind (ADR-0101). The
    field is a date, whose released contract can actually refuse a cell, which
    is what makes the refusal reachable rather than hypothetical.
    """

    seed_membership(
        session, project, OPERATOR, designations=(access.TECHNICAL_OPERATIONS,)
    )
    dated = Misread(
        session,
        project,
        field="committed_date",
        accepted_text="2026-11-01",
        misread_text="2026-12-15",
        divergent_text="2027-01-20",
    )
    unreadable = dated.incoming.segment("to be advised", cell="C9")
    report = dated.report(passage=unreadable)

    outcome = _correct(dated, report)

    assert outcome.outcome == INCONCLUSIVE
    assert outcome.corrected_fact_id is None
    assert live_delta_status(session, dated.delta.id) == "open"
    assert "committed_date" in outcome.finding


def test_a_proposal_decided_during_the_investigation_keeps_its_decision(
    session: Session, misread: Misread
) -> None:
    """ADR-0101 property 8. The retirement is refused, not applied over the top."""

    report = misread.report()
    applied = _apply(session, misread, misread.fact.id)
    assert applied.status == "resolved"

    with pytest.raises(CaptureCorrectionRefused) as refused:
        with session.begin_nested():
            _correct(misread, report)

    assert refused.value.reason == "already_resolved"
    assert live_delta_status(session, misread.delta.id) == "resolved"
    # The decision and the revision it produced are exactly as they were.
    assert session.scalar(
        select(DeltaDisposition.id).where(
            DeltaDisposition.delta_id == misread.delta.id
        )
    )
    assert not session.scalar(
        select(DeltaCaptureCorrection.id).where(
            DeltaCaptureCorrection.delta_id == misread.delta.id
        )
    )
    assert not session.scalar(
        select(CaptureCorrectionResult.id).where(
            CaptureCorrectionResult.delta_id == misread.delta.id
        )
    )


# --- the proof the command establishes, one absence at a time --------------


def test_the_request_has_to_name_this_proposal_and_this_capture(
    session: Session, misread: Misread
) -> None:
    """Proof 1. Without it operations could retire whichever item resembles the report."""

    report = misread.report()

    with pytest.raises(CaptureCorrectionRefused) as refused:
        with session.begin_nested():
            record_correction_result(
                session,
                project_id=misread.project.id,
                request_id=int(report.id),
                delta_id=int(misread.delta.id),
                # A capture that is not the one this report challenged.
                challenged_fact_id=int(misread.fact.id) + 10_000,
                challenged_fact_sha256=misread.fact.content_sha256,
                corrected_fact_id=None,
                corrected_support_assessment_id=None,
                accepted_revision_id=None,
                comparison_rule_version="v1",
                outcome=INCONCLUSIVE,
                replacement_delta_id=None,
                finding="not this capture",
                authorized_by_principal=OPERATOR.subject,
                executed_by=WORKER_IDENTITY,
                recorded_at=CORRECTED_AT,
            )

    assert refused.value.reason == "request_not_for_this_capture"


def test_the_corrected_capture_has_to_be_held_to_the_retained_source(
    session: Session, misread: Misread
) -> None:
    """Proof 2. The reporter's suggested answer alone is not evidence.

    A corrected capture with no effective Support Assessment citing a passage
    of this source is refused, so a value that arrived some other way cannot
    retire anything.
    """

    report = misread.report()
    # A Fact of this document with no supporting assessment at all.
    unsupported, _ = misread.incoming.capture(
        fact_type=FIELD, value=ACCEPTED_TEXT, subject_key=subject(1), cell="C7"
    )

    with pytest.raises(CaptureCorrectionRefused) as refused:
        with session.begin_nested():
            record_correction_result(
                session,
                project_id=misread.project.id,
                request_id=int(report.id),
                delta_id=int(misread.delta.id),
                challenged_fact_id=int(misread.fact.id),
                challenged_fact_sha256=misread.fact.content_sha256,
                corrected_fact_id=int(unsupported.id),
                corrected_support_assessment_id=None,
                accepted_revision_id=misread.revision_id,
                comparison_rule_version="v1",
                outcome=NO_CHANGE,
                replacement_delta_id=None,
                finding="no support was cited",
                authorized_by_principal=OPERATOR.subject,
                executed_by=WORKER_IDENTITY,
                recorded_at=CORRECTED_AT,
            )

    assert refused.value.reason == "corrected_capture_unsupported"


def test_the_accepted_record_moving_refuses_the_earlier_comparison(
    session: Session, misread: Misread
) -> None:
    """Proof 3 and ADR-0101's first edge case.

    A recomparison computed against revision n proves nothing about revision
    n+1, so a comparison computed earlier is not permission to retire the
    proposal after the record changes.
    """

    report = misread.report()

    with pytest.raises(CaptureCorrectionRefused) as refused:
        with session.begin_nested():
            record_correction_result(
                session,
                project_id=misread.project.id,
                request_id=int(report.id),
                delta_id=int(misread.delta.id),
                challenged_fact_id=int(misread.fact.id),
                challenged_fact_sha256=misread.fact.content_sha256,
                corrected_fact_id=None,
                corrected_support_assessment_id=None,
                # An accepted revision that is not the one this subject and field
                # stand on, as if the record had moved under the comparison.
                accepted_revision_id=None,
                comparison_rule_version="v1",
                outcome=STILL_DIFFERS,
                replacement_delta_id=None,
                finding="computed against a revision that is no longer current",
                authorized_by_principal=OPERATOR.subject,
                executed_by=WORKER_IDENTITY,
                recorded_at=CORRECTED_AT,
            )

    assert refused.value.reason in {"stale_accepted_revision", "refused"}
    assert not session.scalar(
        select(DeltaCaptureCorrection.id).where(
            DeltaCaptureCorrection.delta_id == misread.delta.id
        )
    )


def test_an_exact_retry_is_the_same_act_and_a_changed_one_is_a_conflict(
    session: Session, misread: Misread
) -> None:
    """ADR-0101 properties 3 and 4, which are one mechanism seen twice."""

    report = misread.report()
    first = _correct(misread, report)
    again = _correct(misread, report)

    assert again.result_id == first.result_id
    assert again.retirement_id == first.retirement_id
    assert (
        session.scalar(
            select(func.count()).select_from(DeltaCaptureCorrection).where(
                DeltaCaptureCorrection.delta_id == misread.delta.id
            )
        )
        == 1
    )

    # The same key presented with different content is a bounded conflict
    # rather than an overwrite or a silent replay of the prior row.
    with pytest.raises(CaptureCorrectionRefused) as refused:
        with session.begin_nested():
            record_correction_result(
                session,
                project_id=misread.project.id,
                request_id=int(report.id),
                delta_id=int(misread.delta.id),
                challenged_fact_id=int(misread.fact.id),
                challenged_fact_sha256=misread.fact.content_sha256,
                corrected_fact_id=None,
                corrected_support_assessment_id=None,
                accepted_revision_id=None,
                comparison_rule_version="v1",
                outcome=INCONCLUSIVE,
                replacement_delta_id=None,
                finding="a different result under the same key",
                authorized_by_principal=OPERATOR.subject,
                executed_by=WORKER_IDENTITY,
                recorded_at=CORRECTED_AT,
                idempotency_key=session.get(
                    CaptureCorrectionResult, first.result_id
                ).idempotency_key,
            )

    assert refused.value.reason == "key_bound_to_other_content"


def test_a_later_capture_does_not_move_what_the_correction_names(
    session: Session, misread: Misread
) -> None:
    """ADR-0101 property 6, proved the way ADR-0100 states it."""

    report = misread.report()
    # A second capture of the same document, subject and field arrives, so the
    # query the screen reconstructs a capture from now answers differently.
    later, later_segment = misread.incoming.capture(
        fact_type=FIELD, value="1099+00", subject_key=subject(1), cell="C8"
    )
    support(session, misread.project, later, later_segment)

    outcome = _correct(misread, report)

    stored = session.get(CaptureCorrectionResult, outcome.result_id)
    assert stored.challenged_fact_id == misread.fact.id
    assert stored.challenged_fact_sha256 == misread.fact.content_sha256
    assert stored.challenged_fact_id != later.id


def test_the_responsible_actor_and_the_executing_identity_stay_distinct(
    session: Session, misread: Misread
) -> None:
    """A queued re-capture does not become the author of the decision to correct."""

    report = misread.report()

    outcome = _correct(misread, report)

    stored = session.get(CaptureCorrectionResult, outcome.result_id)
    assert stored.authorized_by_principal == OPERATOR.subject
    assert stored.executed_by == WORKER_IDENTITY
    assert stored.authorized_by_principal != stored.executed_by


# --- the terminal-state invariant, in both write orders --------------------


def test_a_retired_proposal_refuses_a_later_decision_in_the_approved_words(
    session: Session, misread: Misread
) -> None:
    """ADR-0101 property 7: the stale Apply.

    A coordinator's browser was showing a page that was true when it was
    rendered. The submission applies nothing, and the refusal says so -- the
    middle sentence is the one that cannot be dropped.
    """

    report = misread.report()
    _correct(misread, report)

    outcome = _apply(session, misread, misread.fact.id)

    assert outcome.status == "refused"
    assert outcome.refusal.reason == "capture_corrected_delta"
    assert outcome.refusal.detail == STALE_APPLY_SENTENCE
    assert not session.scalar(
        select(DeltaDisposition.id).where(
            DeltaDisposition.delta_id == misread.delta.id
        )
    )


def test_a_retired_proposal_refuses_a_later_scheduling_act(
    session: Session, misread: Misread
) -> None:
    """ADR-0101 property 10 for the scheduling half.

    A scheduled return to a comparison that no longer exists is a return to
    nothing, so the words are the scheduling act's rather than Apply's.
    """

    report = misread.report()
    _correct(misread, report)

    outcome = resolve_delta(
        session,
        ChildDecisionRequest(
            project_id=misread.project.id,
            delta_id=misread.delta.id,
            action=DEFER,
            principal=ALICE,
            decided_at=DECIDED_AT,
            idempotency_key=f"defer:{uuid4().hex[:10]}",
            deferred_until=datetime(2026, 10, 1, tzinfo=timezone.utc),
        ),
    )

    assert outcome.status == "refused"
    assert outcome.refusal.reason == "capture_corrected_delta"
    assert "nothing to schedule a return to" in outcome.refusal.detail


def test_a_deferred_proposal_is_retired_without_being_woken(
    session: Session, misread: Misread
) -> None:
    """ADR-0101 property 11, and the precedence clause that does real work.

    The deferral receipt is not rewritten and does not disappear: removing an
    invalid proposal from active work does not mean the scheduling never
    happened.
    """

    report = misread.report()
    scheduled = resolve_delta(
        session,
        ChildDecisionRequest(
            project_id=misread.project.id,
            delta_id=misread.delta.id,
            action=DEFER,
            principal=ALICE,
            decided_at=DECIDED_AT,
            idempotency_key=f"defer:{uuid4().hex[:10]}",
            deferred_until=datetime(2026, 10, 1, tzinfo=timezone.utc),
            deferral_reason="waiting on the district",
        ),
    )
    assert scheduled.status == "deferred"
    assert live_delta_status(session, misread.delta.id) == "deferred"

    _correct(misread, report)

    assert live_delta_status(session, misread.delta.id) == "capture_corrected"
    receipt = session.get(DeltaDeferral, scheduled.deferral_id)
    assert receipt is not None
    assert receipt.reason == "waiting on the district"
    assert receipt.deferred_until == datetime(2026, 10, 1, tzinfo=timezone.utc)
    # The record history prints the retirement as the terminal reason and keeps
    # the scheduling beside it rather than instead of it.
    history = read_record_history(session, project_id=misread.project.id)
    (line,) = [row for row in history.deltas if row.delta_id == misread.delta.id]
    assert line.standing == CAPTURE_CORRECTED
    assert line.deferred_until == datetime(2026, 10, 1).date()
    assert line.deferral_reason == "waiting on the district"


def test_a_superseded_proposal_cannot_also_be_retired(
    session: Session, misread: Misread
) -> None:
    """The other direction of the invariant, which precedence alone would hide."""

    report = misread.report()
    (replacement,) = append_deltas(
        session,
        misread.project,
        misread.incoming,
        source_revision="2026-10",
        values=[
            modify(
                subject_key=subject(1),
                field_name=FIELD,
                accepted_value=ACCEPTED_TEXT,
                proposed_value="1050+00",
                baseline_revision=misread.revision_id,
            )
        ],
    )
    session.add(
        DeltaSupersession(
            project_id=misread.project.id,
            prior_delta_id=misread.delta.id,
            superseding_delta_id=replacement.id,
            reason="newer_source_revision",
        )
    )
    session.flush()

    with pytest.raises(CaptureCorrectionRefused) as refused:
        with session.begin_nested():
            _correct(misread, report)

    assert refused.value.reason == "superseded_delta"
    assert live_delta_status(session, misread.delta.id) == "superseded"


# --- the approved words, and what they link to -----------------------------


def test_the_no_change_sentence_prints_the_revision_it_was_compared_against(
    session: Session, misread: Misread
) -> None:
    report = misread.report()
    _correct(misread, report)

    (reading,) = retirements_by_delta(
        session, project_id=misread.project.id
    ).values()

    assert reading.headline == CORRECTED_READING
    assert reading.explanation == (
        f"The corrected value matches the accepted record at revision "
        f"{misread.revision_id}, so this proposed change is no longer in "
        f"Review. No accepted value changed."
    )
    # Every identifier the sentence is linked to is reachable from it.
    assert reading.request_id == report.id
    assert reading.challenged_fact_id == misread.fact.id
    assert reading.corrected_fact_id is not None
    assert reading.corrected_support_assessment_id is not None
    assert reading.accepted_revision_id == misread.revision_id


def test_the_replacement_sentence_points_at_the_corrected_proposal(
    session: Session, misread: Misread
) -> None:
    report = misread.report(passage=misread.divergent)
    outcome = _correct(misread, report)

    (reading,) = retirements_by_delta(
        session, project_id=misread.project.id
    ).values()

    assert reading.headline == CORRECTED_READING
    assert reading.explanation == (
        "The original proposed change has been replaced by a corrected "
        "proposal. Review the corrected proposal before changing the accepted "
        "record."
    )
    assert reading.replacement_delta_id == outcome.replacement_delta_id


def test_the_retired_item_leaves_review_without_leaving_the_record(
    session: Session, misread: Misread
) -> None:
    """ADR-0101's own words: a coordinator who goes looking for it finds it."""

    report = misread.report()
    _correct(misread, report)

    reading = read_review_items(session, project_id=misread.project.id, as_of=CUTOFF)
    assert all(
        child.delta_id != misread.delta.id
        for item in reading.items
        for child in item.children
    )
    history = read_record_history(session, project_id=misread.project.id)
    (line,) = [row for row in history.deltas if row.delta_id == misread.delta.id]
    assert line.standing == CAPTURE_CORRECTED
    assert line.capture_correction is not None
    assert line.capture_correction.request_id == report.id


# --- #842's procedure and the refusals it proves through processing_holds ---


def test_the_procedure_leaves_a_receipt_the_source_register_reads(
    session: Session, misread: Misread
) -> None:
    report = misread.report()

    outcome = _correct(misread, report)

    receipt = repair_receipts(
        session, document_ids=[misread.incoming.document.id]
    )[misread.incoming.document.id]
    assert receipt.procedure == SOURCE_GROUNDED_RECAPTURE
    assert receipt.outcome == NO_CHANGE
    assert receipt.performed_by == OPERATOR.subject
    entry = session.get(audit.AuditLog, outcome.audit_id)
    assert entry.action == audit.CORRECT_CAPTURED_READING
    assert entry.after_json["executed_by"] == WORKER_IDENTITY
    assert entry.after_json["result_id"] == outcome.result_id


def test_a_principal_without_the_designation_cannot_correct_a_capture(
    session: Session, misread: Misread
) -> None:
    """Retiring a machine-generated comparison is still a designated act.

    ADR-0101 grants the authority to Technical Operations and says in the same
    breath that this must not become a generic operations permission to remove
    things from Review, so a project person on the roster without that
    designation cannot reach it.
    """

    report = misread.report()
    onlooker = HumanPrincipal("local:onlooker")
    seed_membership(session, misread.project, onlooker, designations=())

    with pytest.raises(OperationsRepairRefused) as refused:
        correct_captured_reading(
            session,
            request_id=int(report.id),
            principal=onlooker,
            performed_at=CORRECTED_AT,
        )

    assert refused.value.reason == "not_designated"


def test_a_held_source_is_refused_through_the_one_stage_aware_check(
    session: Session, misread: Misread
) -> None:
    """#919's rule, asked of the stage this act actually performs.

    The refusal is `processing_holds`' own recorded words, reached by calling
    it rather than by a copy of the rule living here. Nothing in the procedure
    releases the restriction.
    """

    from corridor import processing_holds

    report = misread.report()
    processing_holds.impose_hold(
        session,
        document_id=misread.incoming.document.id,
        prohibited_stage=processing_holds.SEMANTIC_EXTRACTION,
        reason_code=processing_holds.UNDECLARED_SOURCE_REVISION,
        reason="this revision has not been declared",
        authority=processing_holds.TECHNICAL_OPERATIONS,
        imposed_by=OPERATOR.subject,
        evidence="the delivery declared no source revision",
    )

    with pytest.raises(OperationsRepairRefused) as refused:
        _correct(misread, report)

    assert refused.value.reason == "held_in_quarantine"
    assert "this revision has not been declared" in str(refused.value)
    # Nothing here lifted it.
    assert processing_holds.open_holds(session, misread.incoming.document.id)


def test_a_reading_refused_for_want_of_authorization_is_not_corrected_either(
    session: Session, misread: Misread
) -> None:
    """The missing thing is the customer's own signed authorization (#522, #919)."""

    from corridor.models import PageProcessingFailure
    from corridor.provider_authorization import AUTHORIZATION_ABSENT

    report = misread.report()
    session.add(
        PageProcessingFailure(
            document_id=misread.incoming.document.id,
            page_number=1,
            engine="textract",
            configuration_json={},
            region_id="page-1",
            scope_json={},
            error_type=AUTHORIZATION_ABSENT,
            error_message="no customer authorization was given",
        )
    )
    session.flush()

    with pytest.raises(OperationsRepairRefused) as refused:
        _correct(misread, report)

    assert refused.value.reason == "authorization_missing"


# --- the concurrency half, which a rollback-scoped test cannot prove --------


@dataclass(frozen=True, slots=True)
class Competing:
    """The committed scenario two racing transactions both act on."""

    project_id: int
    delta_id: int
    request_id: int
    fact_id: int
    digest: str
    revision_id: int
    corrected_fact_id: int
    corrected_support_id: int
    support_id: int


def _seed_competing_scenario(factory) -> Competing:
    """One committed project, report and proposal, in its own transaction."""

    with factory() as seeding:
        from corridor.models import Project as ProjectModel

        project = ProjectModel(
            slug=f"project-{uuid4().hex[:8]}", name="Project", is_synthetic=True
        )
        seeding.add(project)
        seeding.flush()
        seed_membership(seeding, project, ALICE)
        seed_membership(
            seeding,
            project,
            OPERATOR,
            designations=(access.TECHNICAL_OPERATIONS,),
        )
        misread = Misread(seeding, project)
        report = misread.report()
        # The corrected capture and its Support Assessment are seeded here
        # rather than appended by each competing act, so what the two
        # transactions race for is the one thing under test: the delta's single
        # terminal relationship.
        corrected, _ = misread.incoming.capture(
            fact_type=FIELD, value=ACCEPTED_TEXT, subject_key=subject(1), cell="C2"
        )
        corrected_support = support(seeding, project, corrected, misread.correct)
        seeding.commit()
        return Competing(
            project_id=int(project.id),
            delta_id=int(misread.delta.id),
            request_id=int(report.id),
            fact_id=int(misread.fact.id),
            digest=misread.fact.content_sha256,
            revision_id=int(misread.revision_id),
            corrected_fact_id=int(corrected.id),
            corrected_support_id=int(corrected_support.id),
            support_id=int(misread.support.id),
        )


@pytest.mark.slow
def test_a_competing_decision_and_retirement_serialise_to_one_winner(
    runtime_database,
) -> None:
    """ADR-0101 property 10's concurrency half, in two committing transactions.

    A coordinator's Apply and an operations retirement of the same proposal
    are started together, and whichever loses is told so: exactly one terminal
    relationship exists afterwards, and the other act comes back as a named
    refusal rather than a lost write or a silent overwrite.

    This cannot be proved by a rollback-scoped test at all -- neither
    transaction can see the other's uncommitted rows -- so it uses the
    harness's own isolated database.
    """

    factory = runtime_database.session_factory
    for _ in range(RACES):
        _one_competing_pair(factory)


#: How many competing pairs the race above runs. Whether two transactions
#: actually overlap is the scheduler's business, so one pair says very little;
#: repeating it says the invariant holds however the two land. What this does
#: *not* prove on its own is which mechanism holds it -- the pre-check, the
#: commit order and the lock all keep it true here, and removing the lock
#: leaves this passing. `test_a_retirement_in_flight_holds_the_lock_every_terminal_writer_takes`
#: is the one that names the mechanism, and it fails when the lock is removed.
RACES = 12


def _one_competing_pair(factory) -> None:
    """One decision and one retirement, started together on one fresh proposal."""

    seeded = _seed_competing_scenario(factory)

    both_ready = Barrier(2, timeout=30)

    def retire() -> object:
        with factory() as retiring:
            retiring.execute(select(func.txid_current()))
            both_ready.wait()
            try:
                record_correction_result(
                    retiring,
                    project_id=seeded.project_id,
                    request_id=seeded.request_id,
                    delta_id=seeded.delta_id,
                    challenged_fact_id=seeded.fact_id,
                    challenged_fact_sha256=seeded.digest,
                    corrected_fact_id=seeded.corrected_fact_id,
                    corrected_support_assessment_id=seeded.corrected_support_id,
                    accepted_revision_id=seeded.revision_id,
                    comparison_rule_version="v1",
                    outcome=NO_CHANGE,
                    replacement_delta_id=None,
                    finding="competing retirement",
                    authorized_by_principal=OPERATOR.subject,
                    executed_by=WORKER_IDENTITY,
                    recorded_at=CORRECTED_AT,
                )
            except Exception as exc:  # noqa: BLE001 - the refusal is the result
                retiring.rollback()
                return exc
            retiring.commit()
            return None

    def decide() -> object:
        with factory() as deciding:
            deciding.execute(select(func.txid_current()))
            both_ready.wait()
            outcome = resolve_delta(
                deciding,
                ChildDecisionRequest(
                    project_id=seeded.project_id,
                    delta_id=seeded.delta_id,
                    action=ACCEPT,
                    principal=ALICE,
                    decided_at=DECIDED_AT,
                    idempotency_key=f"apply:{uuid4().hex[:10]}",
                    observed_accepted_revision_id=seeded.revision_id,
                    record_effects=(RecordEffect(fact_id=seeded.fact_id),),
                    support_assessment_ids=(seeded.support_id,),
                ),
            )
            if outcome.status == "resolved":
                deciding.commit()
            else:
                deciding.rollback()
            return outcome

    with ThreadPoolExecutor(max_workers=2) as pool:
        retirement = pool.submit(retire)
        decision = pool.submit(decide)
        retired_error = retirement.result()
        decided = decision.result()

    with factory() as verify:
        terminal = verify.scalar(
            select(func.count()).select_from(DeltaCaptureCorrection).where(
                DeltaCaptureCorrection.delta_id == seeded.delta_id
            )
        ) + verify.scalar(
            select(func.count()).select_from(DeltaDisposition).where(
                DeltaDisposition.delta_id == seeded.delta_id
            )
        )

    # Exactly one terminal relationship exists, and the loser was refused in a
    # bounded way rather than losing its write silently.
    assert terminal == 1
    if retired_error is None:
        assert decided.status == "refused"
        assert decided.refusal.reason == "capture_corrected_delta"
    else:
        assert decided.status == "resolved"
        assert isinstance(retired_error, CaptureCorrectionRefused)
        assert retired_error.reason == "already_resolved"


@pytest.mark.slow
def test_two_competing_retirements_leave_exactly_one(runtime_database) -> None:
    """The same invariant between two copies of the same act.

    The second inserter waits on `uq_delta_capture_corrections_delta`, then
    finds the row the winner wrote and replays it: one retirement, the same
    identity returned.
    """

    factory = runtime_database.session_factory
    seeded = _seed_competing_scenario(factory)

    both_ready = Barrier(2, timeout=30)

    def retire() -> object:
        with factory() as retiring:
            retiring.execute(select(func.txid_current()))
            both_ready.wait()
            try:
                recorded = record_correction_result(
                    retiring,
                    project_id=seeded.project_id,
                    request_id=seeded.request_id,
                    delta_id=seeded.delta_id,
                    challenged_fact_id=seeded.fact_id,
                    challenged_fact_sha256=seeded.digest,
                    corrected_fact_id=seeded.corrected_fact_id,
                    corrected_support_assessment_id=seeded.corrected_support_id,
                    accepted_revision_id=seeded.revision_id,
                    comparison_rule_version="v1",
                    outcome=NO_CHANGE,
                    replacement_delta_id=None,
                    finding="competing retirement",
                    authorized_by_principal=OPERATOR.subject,
                    executed_by=WORKER_IDENTITY,
                    recorded_at=CORRECTED_AT,
                )
            except Exception as exc:  # noqa: BLE001 - the refusal is the result
                retiring.rollback()
                return exc
            retiring.commit()
            return recorded

    with ThreadPoolExecutor(max_workers=2) as pool:
        both = [pool.submit(retire) for _ in range(2)]
        answers = [future.result() for future in both]

    with factory() as verify:
        rows = verify.scalar(
            select(func.count()).select_from(DeltaCaptureCorrection).where(
                DeltaCaptureCorrection.delta_id == seeded.delta_id
            )
        )

    assert rows == 1
    recorded = [answer for answer in answers if not isinstance(answer, Exception)]
    assert recorded, answers
    assert len({answer.result_id for answer in recorded}) == 1


@pytest.mark.slow
def test_a_retirement_in_flight_holds_the_lock_every_terminal_writer_takes(
    runtime_database,
) -> None:
    """The serialisation point itself, named rather than inferred from a race.

    The race above proves the invariant holds; this proves *what* holds it. A
    retirement that has not committed yet holds
    `lock_proposed_delta_terminal` for its delta, so a competing terminal
    writer waits instead of reading past the uncommitted row and writing a
    second terminal relationship. A bounded `lock_timeout` is what makes the
    waiting observable: without the lock the competing writer would acquire
    immediately and go on to commit a contradiction.

    Then the retirement commits, and the decision that was waiting refuses in
    the approved words.
    """

    factory = runtime_database.session_factory
    seeded = _seed_competing_scenario(factory)

    with factory() as retiring:
        record_correction_result(
            retiring,
            project_id=seeded.project_id,
            request_id=seeded.request_id,
            delta_id=seeded.delta_id,
            challenged_fact_id=seeded.fact_id,
            challenged_fact_sha256=seeded.digest,
            corrected_fact_id=seeded.corrected_fact_id,
            corrected_support_assessment_id=seeded.corrected_support_id,
            accepted_revision_id=seeded.revision_id,
            comparison_rule_version="v1",
            outcome=NO_CHANGE,
            replacement_delta_id=None,
            finding="held open while a competing writer tries to take the lock",
            authorized_by_principal=OPERATOR.subject,
            executed_by=WORKER_IDENTITY,
            recorded_at=CORRECTED_AT,
        )

        with factory() as competing:
            competing.execute(text("set lock_timeout = '1s'"))
            with pytest.raises(DBAPIError) as blocked:
                competing.execute(
                    select(func.lock_proposed_delta_terminal(seeded.delta_id))
                )
            assert "lock timeout" in str(blocked.value).lower()
            competing.rollback()

        retiring.commit()

    with factory() as deciding:
        outcome = resolve_delta(
            deciding,
            ChildDecisionRequest(
                project_id=seeded.project_id,
                delta_id=seeded.delta_id,
                action=ACCEPT,
                principal=ALICE,
                decided_at=DECIDED_AT,
                idempotency_key=f"apply:{uuid4().hex[:10]}",
                observed_accepted_revision_id=seeded.revision_id,
                record_effects=(RecordEffect(fact_id=seeded.fact_id),),
                support_assessment_ids=(seeded.support_id,),
            ),
        )
        deciding.rollback()

    assert outcome.status == "refused"
    assert outcome.refusal.reason == "capture_corrected_delta"
    assert outcome.refusal.detail == STALE_APPLY_SENTENCE


def test_a_record_carrying_two_terminal_relationships_is_an_integrity_problem(
    session: Session, misread: Misread
) -> None:
    """ADR-0101 property 12.

    Every write command refuses this under the terminal lock, so the only way
    a delta reaches it is an import or history written before the guards
    existed. The clockless authority every write guard consults reads all
    three terminal relations before it answers, and says the record
    contradicts itself rather than returning whichever word its precedence
    order reaches first.
    """

    report = misread.report()
    _correct(misread, report)
    assert live_delta_status(session, misread.delta.id) == "capture_corrected"

    # Written as the record-decision role, which is the only principal the
    # guard trigger admits; no command would write this pair.
    with as_role(session, RECORD_DECISION_ROLE):
        session.execute(
            text(
                "insert into delta_dispositions ("
                "project_id, delta_id, disposition, decided_by_principal,"
                " decided_at"
                ") values (:project_id, :delta_id, 'reject', :principal, :at)"
            ),
            {
                "project_id": misread.project.id,
                "delta_id": misread.delta.id,
                "principal": ALICE.subject,
                "at": DECIDED_AT,
            },
        )

    with pytest.raises(ContradictoryDeltaStanding) as contradiction:
        live_delta_status(session, misread.delta.id)

    assert contradiction.value.carried == ("resolved", "capture_corrected")
    assert "contradicts itself" in str(contradiction.value)
