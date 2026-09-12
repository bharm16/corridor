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
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from corridor import sheets

from corridor import access, audit
from corridor.db_roles import RECORD_DECISION_ROLE
from corridor.migrations.source_append_commands.project_partition import (
    UNPARTITIONED_ROLES,
)
from corridor.capture_correction import (
    CaptureCorrectionRefused,
)
from corridor.correction_applicability import PassageApplicability
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
    ContradictoryDeltaResolution,
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
    Fact,
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
from corridor.review_packets import (
    APPLY,
    REVERSED,
    SAVED,
    DeferralRequest,
    PacketChildRequest,
    ReviewPacketRequest,
    resolve_review_packet,
    reverse_review_packet,
)
from corridor.review_packets import DEFER as DEFER_OUTCOME
from corridor.record_history import CAPTURE_CORRECTED, read_record_history

from access_support import seed_membership
from harness_support import as_role
from packet_review_support import (
    append_deltas,
    modify,
    register_baseline,
    subject,
    support,
)


from capture_correction_support import (
    ACCEPTED_TEXT,
    ALICE,
    CONFLICT_ROW,
    CORRECTED_AT,
    CUTOFF,
    DECIDED_AT,
    FIELD,
    FIELD_COLUMN,
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


@pytest.fixture
def differing(session: Session, project: Project) -> Misread:
    """The same misread, over a cell whose corrected reading still differs.

    One subject and one field have exactly one passage that can support them
    (#945), so "the corrected value still differs" is a different *text* in
    that cell rather than a second cell to point at.
    """

    seed_membership(
        session, project, OPERATOR, designations=(access.TECHNICAL_OPERATIONS,)
    )
    return Misread(session, project, correct_text=STILL_DIFFERENT_TEXT)


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
    session: Session, differing: Misread
) -> None:
    """ADR-0101 property 2. The cause is a correction, not a newer version."""

    report = differing.report()

    outcome = _correct(differing, report)

    assert outcome.outcome == STILL_DIFFERS
    assert outcome.retired
    replacement = session.get(ProposedDelta, outcome.replacement_delta_id)
    assert replacement is not None
    assert replacement.target_subject_identity == differing.subject_key
    assert replacement.target_field == FIELD
    assert replacement.proposed_value == STILL_DIFFERENT_TEXT
    # The original left Review through this relationship, and not by being
    # called superseded: ADR-0083's supersession is a newer source version, and
    # this is the same version read again.
    assert live_delta_status(session, differing.delta.id) == "capture_corrected"
    assert not session.scalar(
        select(DeltaSupersession.id).where(
            DeltaSupersession.prior_delta_id == differing.delta.id
        )
    )
    # Both the retirement and the replacement hang off one correction result.
    retired = retirements_by_delta(session, project_id=differing.project.id)
    assert retired[differing.delta.id].replacement_delta_id == replacement.id
    assert retired[differing.delta.id].result_id == outcome.result_id
    # The corrected proposal is an ordinary open proposal for Review.
    reading = read_open_deltas(session, project_id=differing.project.id, as_of=CUTOFF)
    assert replacement.id in reading.actionable_delta_ids
    assert differing.delta.id not in reading.open_delta_ids


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
    # The passage this conflict's Promised For really is, carrying words no
    # date materializer can read. It is the right row and the right column, so
    # the applicability half is satisfied and what fails is the reading itself.
    dated = Misread(
        session,
        project,
        field="committed_date",
        accepted_text="2026-11-01",
        misread_text="2026-12-15",
        correct_text="to be advised",
    )
    report = dated.report()

    outcome = _correct(dated, report)

    assert outcome.outcome == INCONCLUSIVE
    assert outcome.corrected_fact_id is None
    assert live_delta_status(session, dated.delta.id) == "open"
    assert "committed_date" in outcome.finding


# --- #945: the passage has to carry a value for this subject and field -----


def _wrote_nothing(session: Session, misread: Misread, outcome) -> None:
    """The whole of "no correction was applied", asserted once for every case.

    No corrected Source Fact, no Support Assessment, no retirement and no
    replacement proposal -- and the proposal the coordinator was always going
    to have to decide is still theirs to decide.
    """

    assert outcome.outcome == INCONCLUSIVE
    assert not outcome.retired
    assert outcome.corrected_fact_id is None
    assert outcome.replacement_delta_id is None
    stored = session.get(CaptureCorrectionResult, outcome.result_id)
    assert stored.corrected_fact_id is None
    assert stored.corrected_support_assessment_id is None
    assert stored.applicability_verdict != "applicable"
    assert live_delta_status(session, misread.delta.id) == "open"
    assert session.scalar(
        select(func.count())
        .select_from(Fact)
        .where(
            Fact.project_id == misread.project.id,
            Fact.recorded_by.like("operations:%"),
        )
    ) == 0
    assert not session.scalars(
        select(DeltaCaptureCorrection.id).where(
            DeltaCaptureCorrection.delta_id == misread.delta.id
        )
    ).all()


def test_a_sheet_that_names_no_columns_settles_nothing_and_corrects_nothing(
    session: Session, project: Project
) -> None:
    """#945's unsettled case: the report stands, the correction does not happen.

    A rendition whose sheet retained no header row says nothing about what any
    of its columns carry, so nothing establishes that the reported passage
    carries this field for this conflict. That is not proof the coordinator is
    wrong, which is why the report is retained and the investigation records
    that it could not be substantiated rather than refusing the report itself.
    """

    seed_membership(
        session, project, OPERATOR, designations=(access.TECHNICAL_OPERATIONS,)
    )
    built = Misread(session, project, header=False)
    report = built.report()

    outcome = _correct(built, report)

    _wrote_nothing(session, built, outcome)
    assert outcome.applicability_verdict == "unclear"
    assert outcome.finding == (
        "Corridor could not establish that this passage supports the value "
        "for this Utility Conflict. Your report remains available for "
        "investigation; no correction was applied."
    )
    # The report really is retained, which is what makes that sentence true.
    (found,) = results_by_request(
        session, project_id=project.id, request_ids=[report.id]
    )[report.id]
    assert found.request_id == report.id


def test_re_running_an_unestablished_correction_replays_its_own_receipt(
    session: Session, project: Project
) -> None:
    """A retry is the same act, and it still corrects nothing.

    Operations re-running the procedure over a report the evidence does not
    bear must not accumulate receipts, and must not reach a different answer
    the second time: the verdict is derived from retained rows, so it is the
    same answer however often it is asked.
    """

    seed_membership(
        session, project, OPERATOR, designations=(access.TECHNICAL_OPERATIONS,)
    )
    built = Misread(session, project, header=False)
    report = built.report()

    first = _correct(built, report)
    again = _correct(built, report)

    assert again.result_id == first.result_id
    assert (
        session.scalar(
            select(func.count())
            .select_from(CaptureCorrectionResult)
            .where(CaptureCorrectionResult.request_id == report.id)
        )
        == 1
    )
    _wrote_nothing(session, built, again)


def test_a_report_that_went_round_the_picker_is_refused_by_the_investigation(
    session: Session, misread: Misread
) -> None:
    """The authoritative recheck, at the door the picker cannot stand in for.

    The screen refuses a contradicted selection, so a report naming one exists
    only where somebody went round the screen. Operations re-asks before it
    writes anything, and both shapes #945 names come back as an investigation
    that concluded nothing: another Utility Conflict's own cell, and this
    conflict's own row under the neighbouring field.
    """

    for passage, verdict, sentence in (
        (
            misread.other_conflicts_cell,
            "other_subject",
            "This passage describes a different Utility Conflict. Choose "
            "evidence for this conflict, or ask Corridor operations to review "
            "the source mapping. No correction was applied.",
        ),
        (
            misread.wrong_field_cell,
            "other_field",
            "This passage describes a different field for this Utility "
            "Conflict. Choose evidence for the field being corrected. No "
            "correction was applied.",
        ),
    ):
        report = misread.report_bypassing_the_picker(passage)

        outcome = _correct(misread, report)

        _wrote_nothing(session, misread, outcome)
        assert outcome.applicability_verdict == verdict
        assert outcome.finding == sentence


def test_the_row_mapping_a_correction_rests_on_cannot_move_under_it(
    session: Session, misread: Misread
) -> None:
    """Why there is no "the registration changed mid-investigation" case.

    Which subject a source row resolves to is the customer's own adopted
    registration, and a project has exactly one adopted baseline source. So the
    subject half of #945's verdict cannot be re-registered under a correction
    that is in flight; the half that *can* move is the accepted record, which
    ``test_the_accepted_record_moving_refuses_the_earlier_comparison`` holds.
    """

    with pytest.raises(IntegrityError, match="uq_project_baseline_sources_project"):
        with session.begin_nested():
            register_baseline(
                session,
                misread.project,
                misread.adopted.document,
                misread.revision_id,
            )


def test_a_caller_cannot_substitute_its_own_support_assessment(
    session: Session, misread: Misread
) -> None:
    """The bypass the procedure alone could not close (#945).

    ``correct_captured_reading`` re-asks before it writes, so a caller that
    wants the old behaviour has to go round it and call the command itself with
    a Support Assessment it assembled. The command's own proofs answer that:
    the corrected capture has to be about the challenged subject and field, and
    its assessment has to cite **the passage the report named** rather than
    merely some passage of the same document.
    """

    report = misread.report()
    # A corrected capture held to a cell of this document that is not the one
    # the report named -- the neighbouring column's, which is the shape #945
    # found.
    convenient, _ = misread.incoming.capture(
        fact_type=misread.field,
        value=misread.accepted_text,
        subject_key=misread.subject_key,
        cell="C7",
    )
    assembled = support(session, misread.project, convenient, misread.wrong_field_cell)

    with pytest.raises(CaptureCorrectionRefused) as refused:
        with session.begin_nested():
            record_correction_result(
                session,
                project_id=misread.project.id,
                request_id=int(report.id),
                delta_id=int(misread.delta.id),
                challenged_fact_id=int(misread.fact.id),
                challenged_fact_sha256=misread.fact.content_sha256,
                corrected_fact_id=int(convenient.id),
                corrected_support_assessment_id=int(assembled.id),
                accepted_revision_id=misread.revision_id,
                comparison_rule_version="v1",
                applicability=misread.applicability(),
                outcome=NO_CHANGE,
                replacement_delta_id=None,
                finding="assembled its own evidence",
                authorized_by_principal=OPERATOR.subject,
                executed_by=WORKER_IDENTITY,
                recorded_at=CORRECTED_AT,
            )

    assert refused.value.reason == "corrected_capture_unsupported"
    assert not session.scalars(
        select(DeltaCaptureCorrection.id).where(
            DeltaCaptureCorrection.delta_id == misread.delta.id
        )
    ).all()


def test_a_corrected_capture_about_another_subject_is_refused_by_the_command(
    session: Session, misread: Misread
) -> None:
    """The other half of the same bypass: a Fact filed under somebody else.

    A correction asserts a value about the challenged subject and field. A
    capture recorded under a different subject is not that assertion, whatever
    passage it cites.
    """

    report = misread.report()
    elsewhere, _ = misread.incoming.capture(
        fact_type=misread.field,
        value=misread.accepted_text,
        subject_key=subject(CONFLICT_ROW + 5),
        cell="C7",
    )
    assembled = support(session, misread.project, elsewhere, misread.correct)

    with pytest.raises(CaptureCorrectionRefused) as refused:
        with session.begin_nested():
            record_correction_result(
                session,
                project_id=misread.project.id,
                request_id=int(report.id),
                delta_id=int(misread.delta.id),
                challenged_fact_id=int(misread.fact.id),
                challenged_fact_sha256=misread.fact.content_sha256,
                corrected_fact_id=int(elsewhere.id),
                corrected_support_assessment_id=int(assembled.id),
                accepted_revision_id=misread.revision_id,
                comparison_rule_version="v1",
                applicability=misread.applicability(),
                outcome=NO_CHANGE,
                replacement_delta_id=None,
                finding="a capture about another conflict",
                authorized_by_principal=OPERATOR.subject,
                executed_by=WORKER_IDENTITY,
                recorded_at=CORRECTED_AT,
            )

    assert refused.value.reason == "corrected_capture_other_subject"


def test_the_command_derives_the_verdict_rather_than_taking_the_callers_word(
    session: Session, misread: Misread
) -> None:
    """A stated applicability the retained rows do not bear is contradicted.

    The subject half is entirely the command's, so a caller that states a
    convenient one is refused rather than believed; the field half has to be
    anchored to a retained heading cell of the passage's own column, so a
    caller that points at some other cell is refused too.
    """

    report = misread.report()
    honest = misread.applicability()

    for stated, reason in (
        (
            replace(honest, subject_identity=subject(CONFLICT_ROW + 7)),
            "passage_subject_disagrees",
        ),
        (
            replace(
                honest, field_heading_segment_id=int(misread.wrong_field_cell.id)
            ),
            "field_heading_not_this_column",
        ),
    ):
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
                    applicability=stated,
                    outcome=INCONCLUSIVE,
                    replacement_delta_id=None,
                    finding="a verdict the rows do not bear",
                    authorized_by_principal=OPERATOR.subject,
                    executed_by=WORKER_IDENTITY,
                    recorded_at=CORRECTED_AT,
                )
        assert refused.value.reason == reason


def test_the_heading_field_vocabulary_in_the_schema_matches_the_released_one(
    session: Session,
) -> None:
    """#945 A: the command's trusted copy is derived from `sheets`, not drifting.

    The field a structured column carries is derived in the writing transaction
    from the released heading vocabulary, so the copy the schema carries has to
    be the same mapping `sheets.column_mapping` reads -- every entry, and no
    more -- and its version has to digest that mapping. A released migration
    freezes its bytes, so this is where a later change to the vocabulary that
    forgot to refold this copy is caught rather than letting the reader and the
    writing transaction state two vocabularies.
    """

    in_schema = {
        heading: field
        for heading, field in session.execute(
            text(
                "select normalized_heading, canonical_field "
                "from public.structured_heading_field_vocabulary()"
            )
        ).all()
    }
    assert in_schema == sheets.HEADING_FIELD_VOCABULARY

    version = session.scalar(
        text("select public.structured_heading_vocabulary_version()")
    )
    assert version == sha256(
        json.dumps(
            sorted(sheets.HEADING_FIELD_VOCABULARY.items()), separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()

    # The lookup normalises an arbitrary retained heading the way the mapping is
    # keyed -- collapsed whitespace, casefolded -- and answers nothing for a
    # heading the vocabulary has not ruled on.
    assert (
        session.scalar(
            text("select public.structured_heading_field(:h)"),
            {"h": "  Start   STATION "},
        )
        == "station_from"
    )
    assert (
        session.scalar(
            text("select public.structured_heading_field(:h)"),
            {"h": "a column no form prints"},
        )
        is None
    )


def test_the_command_derives_the_field_from_the_heading_not_the_callers_word(
    session: Session, misread: Misread
) -> None:
    """#945 A: a caller cannot make a wrong-column passage carry the field.

    The passage is a real cell in the neighbouring column, with the real
    header of that column retained above it, and the caller supplies the
    *challenged* field as the passage's own -- the exact shape ADR-0100's
    same-document rule let through.  The command derives what that column means
    from the retained heading through the released heading vocabulary rather
    than trusting the caller, so the claim is contradicted: a Required By
    column is not a Start Station the command may correct against.  A heading
    cell is a mapping record the caller may name; what the column *means* is
    the command's own answer.
    """

    report = misread.report_bypassing_the_picker(misread.wrong_field_cell)
    honest = misread.applicability(passage=misread.wrong_field_cell)
    # The caller claims the neighbouring column carries the challenged field,
    # keeping the real heading cell of that column so only the meaning is a lie.
    stated = replace(honest, field=misread.field)

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
                applicability=stated,
                outcome=INCONCLUSIVE,
                replacement_delta_id=None,
                finding="a field the retained column does not carry",
                authorized_by_principal=OPERATOR.subject,
                executed_by=WORKER_IDENTITY,
                recorded_at=CORRECTED_AT,
            )

    assert refused.value.reason == "passage_field_disagrees"
    assert not session.scalars(
        select(DeltaCaptureCorrection.id).where(
            DeltaCaptureCorrection.delta_id == misread.delta.id
        )
    ).all()


def test_the_successful_correction_retains_the_evidence_it_was_admitted_on(
    session: Session, misread: Misread
) -> None:
    """The proof is on the result row, not recomputed by whoever reads it later.

    Which subject the passage's row resolved to, which field its column
    carries, and the exact retained heading cell that said so -- with the
    heading's own words copied off that cell by the command rather than
    supplied by its caller.
    """

    report = misread.report()

    outcome = _correct(misread, report)

    stored = session.get(CaptureCorrectionResult, outcome.result_id)
    assert stored.applicability_verdict == "applicable"
    assert stored.passage_subject_identity == misread.subject_key
    assert stored.passage_field == misread.field
    assert stored.passage_field_heading_segment_id == misread.headings[
        FIELD_COLUMN
    ].id
    assert stored.passage_field_heading_text == "Start Station"


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
                applicability=misread.applicability(),
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
        fact_type=FIELD, value=ACCEPTED_TEXT, subject_key=misread.subject_key, cell="C7"
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
                applicability=misread.applicability(),
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
                applicability=misread.applicability(),
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
                applicability=misread.applicability(),
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
        fact_type=FIELD, value="1099+00", subject_key=misread.subject_key, cell="C8"
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
                subject_key=misread.subject_key,
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
    session: Session, differing: Misread
) -> None:
    report = differing.report()
    outcome = _correct(differing, report)

    (reading,) = retirements_by_delta(
        session, project_id=differing.project.id
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
    #: The verdict the retained structure gives for the reported passage,
    #: computed in the seeding transaction because the racing ones only write.
    applicability: PassageApplicability


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
        applicability = misread.applicability()
        # The corrected capture and its Support Assessment are seeded here
        # rather than appended by each competing act, so what the two
        # transactions race for is the one thing under test: the delta's single
        # terminal relationship.
        corrected, _ = misread.incoming.capture(
            fact_type=FIELD,
            value=ACCEPTED_TEXT,
            subject_key=misread.subject_key,
            cell=misread.correct.cell_range,
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
            applicability=applicability,
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
                    applicability=seeded.applicability,
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
                    applicability=seeded.applicability,
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
            applicability=seeded.applicability,
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


# --- Undo meets the retirement (#948, ADR-0035) ----------------------------


def _packet_defer(session: Session, misread: Misread):
    """One guided Save that puts this proposal away until a date."""

    return resolve_review_packet(
        session,
        ReviewPacketRequest(
            project_id=misread.project.id,
            grouping_rule_version="packetizer-v1",
            grouping_key_kind="source_revision",
            grouping_key=misread.delta.source_revision,
            principal=ALICE,
            idempotency_key=f"packet:{uuid4().hex[:10]}",
            decided_at=DECIDED_AT,
            observed_accepted_revision_id=misread.revision_id,
            children=(
                PacketChildRequest(
                    delta_id=misread.delta.id,
                    outcome=DEFER_OUTCOME,
                    observed_source_revision=misread.delta.source_revision,
                    deferral=DeferralRequest(
                        deferred_until=datetime(2026, 10, 1, tzinfo=timezone.utc),
                        reason="waiting on the district",
                    ),
                ),
            ),
        ),
    )


def _undo(session: Session, misread: Misread, receipt_id: int):
    return reverse_review_packet(
        session,
        project_id=misread.project.id,
        receipt_id=receipt_id,
        principal=ALICE,
        reversed_at=CORRECTED_AT,
        idempotency_key=f"undo:{uuid4().hex[:10]}",
    )


def test_undo_does_not_revive_a_proposal_whose_capture_was_corrected(
    session: Session, misread: Misread
) -> None:
    """#948's sixth row, on ADR-0101's half of it.

    Undo returns a question that is still applicable, and a comparison whose
    basis Corridor has withdrawn is not one. So releasing the schedule this act
    wrote does not put the proposal back in front of anyone: the retirement is
    the applicable terminal reason and it survives the compensation, which is
    the same precedence the reading already applies to a deferral.
    """

    report = misread.report()
    saved = _packet_defer(session, misread)
    assert saved.status == SAVED
    _correct(misread, report)
    assert live_delta_status(session, misread.delta.id) == "capture_corrected"

    undone = _undo(session, misread, saved.receipt_id)

    assert undone.status == REVERSED
    assert live_delta_status(session, misread.delta.id) == "capture_corrected"
    reading = read_open_deltas(session, project_id=misread.project.id, as_of=CUTOFF)
    assert misread.delta.id not in reading.actionable_delta_ids
    assert misread.delta.id not in reading.open_delta_ids


def test_a_returned_question_can_still_be_retired_by_a_corrected_capture(
    session: Session, misread: Misread
) -> None:
    """The other order, and why #948 needs no precedence clause of its own.

    A decision made during the investigation is preserved rather than
    overridden, and the retirement is refused (property 8). Undoing that
    decision withdraws it, so the proposal is eligible again and the correction
    lands -- and the delta then carries a retained disposition *and* a
    retirement without the reading calling that a contradiction, because the
    retained one is not in force.
    """

    report = misread.report()
    saved = resolve_review_packet(
        session,
        ReviewPacketRequest(
            project_id=misread.project.id,
            grouping_rule_version="packetizer-v1",
            grouping_key_kind="source_revision",
            grouping_key=misread.delta.source_revision,
            principal=ALICE,
            idempotency_key=f"packet:{uuid4().hex[:10]}",
            decided_at=DECIDED_AT,
            observed_accepted_revision_id=misread.revision_id,
            children=(
                PacketChildRequest(
                    delta_id=misread.delta.id,
                    outcome=APPLY,
                    observed_source_revision=misread.delta.source_revision,
                    record_effects=(RecordEffect(fact_id=misread.fact.id),),
                    support_assessment_ids=(misread.support.id,),
                ),
            ),
        ),
    )
    assert saved.status == SAVED
    with pytest.raises(CaptureCorrectionRefused) as refused:
        with session.begin_nested():
            _correct(misread, report)
    assert refused.value.reason == "already_resolved"

    assert _undo(session, misread, saved.receipt_id).status == REVERSED
    assert live_delta_status(session, misread.delta.id) == "open"

    outcome = _correct(misread, report)

    assert outcome.retired
    # Two terminal rows exist and the reading is not confused by them: the
    # disposition is retained history, not a standing resolution.
    assert session.scalar(
        select(DeltaDisposition.id).where(
            DeltaDisposition.delta_id == misread.delta.id
        )
    )
    assert live_delta_status(session, misread.delta.id) == "capture_corrected"


def test_two_decisions_in_force_are_an_integrity_problem_not_a_later_generation(
    session: Session, misread: Misread
) -> None:
    """ADR-0101 property 12's shape, on the resolution history #948 opened.

    An Undo leaves its decision in history and lets a successor take the next
    generation, so a delta may carry several dispositions. At most one is ever
    in force: the generation is assigned by the command under the terminal
    lock, from the decisions already reversed, and a standing decision is
    refused before any of that. ``unique (delta_id, generation)`` alone would
    admit an unreversed decision in generation 0 and another in generation 1,
    so both halves say the record contradicts itself rather than answering with
    whichever generation the order reaches -- the same rule, for the same
    reason, as the terminal relationships above.
    """

    saved = resolve_review_packet(
        session,
        ReviewPacketRequest(
            project_id=misread.project.id,
            grouping_rule_version="packetizer-v1",
            grouping_key_kind="source_revision",
            grouping_key=misread.delta.source_revision,
            principal=ALICE,
            idempotency_key=f"packet:{uuid4().hex[:10]}",
            decided_at=DECIDED_AT,
            observed_accepted_revision_id=misread.revision_id,
            children=(
                PacketChildRequest(
                    delta_id=misread.delta.id,
                    outcome=APPLY,
                    observed_source_revision=misread.delta.source_revision,
                    record_effects=(RecordEffect(fact_id=misread.fact.id),),
                    support_assessment_ids=(misread.support.id,),
                ),
            ),
        ),
    )
    assert saved.status == SAVED, saved.refusals

    # Written as the record-decision role, which is the only principal the
    # guard trigger admits; no command would write this pair.
    with as_role(session, RECORD_DECISION_ROLE):
        session.execute(
            text(
                "insert into delta_dispositions ("
                "project_id, delta_id, generation, disposition,"
                " decided_by_principal, decided_at"
                ") values (:project_id, :delta_id, 1, 'reject', :principal, :at)"
            ),
            {
                "project_id": misread.project.id,
                "delta_id": misread.delta.id,
                "principal": ALICE.subject,
                "at": DECIDED_AT,
            },
        )

    with pytest.raises(ContradictoryDeltaResolution) as contradiction:
        live_delta_status(session, misread.delta.id)
    assert contradiction.value.generations == (0, 1)
    assert "contradicts itself" in str(contradiction.value)

    # And the database half, which every write guard and the shadow seal ask.
    with pytest.raises(DBAPIError) as refused:
        with session.begin_nested():
            session.execute(
                select(func.proposed_delta_effective_disposition(misread.delta.id))
            )
    assert "more than one effective decision" in str(refused.value.orig)
