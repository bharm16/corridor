"""What a corrected capture established, and the proposal it retires (ADR-0101).

#836 recorded an attributable report that one capture is wrong about its
source, and then stopped: the no-change outcome ADR-0100 calls "a truthful
outcome of this path, not a failure of it" had nowhere to go.  Both exits
ADR-0083's lifecycle offers are false entries for it.  A ``reject`` disposition
files a coordinator decision nobody made, into the decision lineage ADR-0082
requires every accepted value to carry.  A supersession is *a newer source
version for the same subject and field coalescing*, and a corrected re-read of
the **same** version is a different cause -- PostgreSQL refuses the row anyway,
because ``ck_delta_supersessions_successor`` wants a successor artifact and a
correction that produced no replacement names none.  So ``withdraw_for_no_change``
refused with ``NO_CHANGE_EXIT_UNAVAILABLE`` rather than inventing a disposition
value, and asked the maintainer for the relationship.

**This module is that relationship, built.**  ADR-0101 names it
``DeltaCaptureCorrection`` and gives it one assertion: *this particular
Proposed Delta no longer represents an actionable comparison because the
particular capture on which it depended was corrected.*  It is not a decision
about what the record should show, not a claim that a coordinator concluded
anything, and not a newer source version arriving.

**A corrected Source Fact alone does not prove "no difference."**  ADR-0101 is
explicit, and it is why the relation is not ``old_delta_id ->
corrected_fact_id``: whether a corrected capture equals the accepted value
depends on which accepted revision was read and which comparison rule was
applied, and a pair of foreign keys records neither.  A later reader asking
"why did this stop being a question?" would find the corrected fact and have to
recompute the conclusion from whatever the record holds *now* -- which is not
the record the conclusion was drawn from.  So ``capture_correction_results``
holds the whole proof and ``delta_capture_corrections`` names it: the request,
the exact challenged capture by immutable identity and digest, the corrected
capture and the Support Assessment holding it to the retained source, the
accepted revision, the comparison rule and its version, the conclusion, the
replacement where there is one, the responsible operations actor beside the
service identity that executed the work, and an idempotency identity.

**The comparison is the ordinary comparison.**  ``recompare_corrected_capture``
calls ``proposed_delta_comparison.compare_stated_subjects`` under a declared
``comparison_rule_version`` -- the same contract every producer of Proposed
Deltas already compares under.  A correction handler inventing a second
"looks equal" check of its own is exactly what ADR-0101 forbids: two comparison
rules mean a value that agrees on one path and proposes a change on the other,
and this is the path where disagreement is least visible, because its output is
a proposal quietly disappearing.

**Three outcomes, each recorded as itself.**  A corrected result matching the
accepted value retires the proposal and creates *no zero-difference delta*.  A
corrected result that still differs produces the replacement proposal and links
the original's retirement to the same result -- through this relationship and
not through a newer-source-version supersession, because the cause is a
correction and ADR-0083's sentence stays true by not being stretched.  An
investigation that cannot be substantiated records ``INCONCLUSIVE``, retires
nothing, and leaves the proposal open: "the source did not establish this
assertion" is not "the source matches the accepted value", and a capture
asserting absence or equality would be a manufactured fact with a locator that
dereferences to nothing of the kind.  A proposal a coordinator decided during
the investigation is not retired at all; the command refuses, the decision
stands, and any repair returns through a new authorized Review path.

**Standing stays derived.**  There is no ``status`` column on
``ProposedDelta`` and this module adds none.  A retirement is one more
append-only relationship, read the way the other three are read -- a row
exists, or it does not -- and ``delta_resolution.live_delta_status`` places it
third, after a disposition and a supersession and *before* a live deferral,
because a deferred delta is retired without being woken.

**The precedence order is not the enforcement.**  ADR-0101 says so in the
maintainer's own words: the shared readers may use the order to present valid
history, but they must not be the mechanism that makes contradictory writes
appear harmless.  So the invariant is enforced at every write, in both
directions and by the database: ``lock_proposed_delta_terminal`` serialises
every terminal writer on one delta, and under that lock this command refuses a
delta already disposed of or superseded while
``resolve_proposed_delta_decision``, ``defer_proposed_delta`` and
``record_delta_follow_up_plan`` each refuse one already retired here.  The two
bulk supersession sweeps skip a retired delta exactly as they already skip a
decided one.

**Nothing that already happened to the proposal is removed.**  A prior
deferral receipt and any Follow-up Plan and its correspondence are retained and
readable; removing an invalid proposal from active work does not mean the
scheduling or the correspondence never happened, and a history reading that hid
them would be making the same false claim as a fabricated disposition.

**No clock.**  Every instant is the caller's, as everywhere else on this seam.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Any, Mapping, Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from corridor.capture_correction import CaptureCorrectionRefused, command_refusal
from corridor.correction_applicability import PassageApplicability
from corridor.models import (
    CaptureCorrectionResult,
    DeltaCaptureCorrection,
    ProposedDelta,
)
from corridor.proposed_delta_comparison import (
    StatedSubject,
    accepted_values,
    compare_stated_subjects,
)


__all__ = [
    "CORRECTED_READING",
    "CORRECTION_OUTCOMES",
    "INCONCLUSIVE",
    "NO_CHANGE",
    "STALE_APPLY_SENTENCE",
    "STANDING_WORDS",
    "STILL_DIFFERS",
    "CorrectionRecomparison",
    "InvestigationResult",
    "RecordedCorrectionResult",
    "RetirementReading",
    "correction_result_idempotency_key",
    "record_correction_result",
    "recompare_corrected_capture",
    "result_sentences",
    "results_by_request",
    "retired_delta_ids",
    "retirements_by_delta",
]


# The three conclusions an investigation reaches, spelled where the command's
# check constraint spells them so the two cannot name different sets.
NO_CHANGE = "no_change"
STILL_DIFFERS = "still_differs"
INCONCLUSIVE = "inconclusive"
CORRECTION_OUTCOMES = (NO_CHANGE, STILL_DIFFERS, INCONCLUSIVE)

# The words the maintainer approved, kept in one place because three surfaces
# print them and a second copy is a second wording. Each is a description of
# what happened rather than a defined customer-facing type, which is the same
# treatment ADR-0100 gave the "Report an extraction error" control; a defined
# type for this outcome would need the terminology research first.
CORRECTED_READING = "Corridor corrected its reading of this source."
STALE_APPLY_SENTENCE = (
    "This proposal can no longer be applied because its source reading was "
    "corrected. Nothing was applied. View the correction result."
)

#: How the record history names this standing, in the same voice as the other
#: standings it prints. It is the approved sentence without its full stop, so
#: the column says what happened rather than naming a type nobody has defined.
STANDING_WORDS = "Corridor corrected its reading of this source"


@dataclass(frozen=True, slots=True)
class CorrectionRecomparison:
    """What the ordinary comparison said about one corrected capture.

    ``outcome`` is ``NO_CHANGE`` or ``STILL_DIFFERS``; an investigation that
    could not be substantiated never reaches this seam, because there is no
    corrected capture to compare. ``replacement`` is the difference the
    comparison stated, ready for ``create_proposed_delta_group``, and is empty
    exactly when the corrected value matches the accepted one.
    """

    outcome: str
    accepted_revision_id: int | None
    comparison_rule_version: str
    accepted_value: Any | None
    corrected_value: Any | None
    replacement: tuple[Any, ...]

    @property
    def establishes_no_difference(self) -> bool:
        return self.outcome == NO_CHANGE


@dataclass(frozen=True, slots=True)
class RecordedCorrectionResult:
    """One retained investigation result, and the retirement it established."""

    result_id: int
    retirement_id: int | None
    created: bool

    @property
    def retired(self) -> bool:
        return self.retirement_id is not None


@dataclass(frozen=True, slots=True)
class RetirementReading:
    """Why one proposal left Review, as a surface shows it.

    Carries the identifiers every approved sentence is linked to -- the
    retained request, the original capture, the corrected evidence and the
    comparison result -- so a coordinator who wants the proof can reach it from
    the sentence rather than being told only that the item is gone.
    """

    delta_id: int
    retirement_id: int
    result_id: int
    request_id: int
    outcome: str
    challenged_fact_id: int
    corrected_fact_id: int | None
    corrected_support_assessment_id: int | None
    accepted_revision_id: int | None
    comparison_rule_version: str
    replacement_delta_id: int | None
    finding: str
    authorized_by_principal: str
    executed_by: str
    retired_at: datetime

    @property
    def headline(self) -> str:
        return result_sentences(self)[0]

    @property
    def explanation(self) -> str:
        return result_sentences(self)[1]


def result_sentences(result: RetirementReading | CaptureCorrectionResult) -> tuple[str, str]:
    """The approved words for one outcome: a headline and its explanation.

    ADR-0101 approved these as written, so they are returned rather than
    composed. The revision number in the no-change explanation is proof item 5
    -- the accepted revision the recomparison actually read -- printed rather
    than described, and in the same ``revision N`` form three other modules
    already print to customers.
    """

    outcome = result.outcome
    if outcome == NO_CHANGE:
        return (
            CORRECTED_READING,
            f"The corrected value matches the accepted record at revision "
            f"{result.accepted_revision_id}, so this proposed change is no "
            f"longer in Review. No accepted value changed.",
        )
    if outcome == STILL_DIFFERS:
        return (
            CORRECTED_READING,
            "The original proposed change has been replaced by a corrected "
            "proposal. Review the corrected proposal before changing the "
            "accepted record.",
        )
    # An investigation that could not be substantiated claims no correction, so
    # there is no approved sentence to print and none is minted here: what a
    # reader is shown is what operations recorded finding.
    return ("", result.finding)


# --- The recomparison, under the ordinary contract --------------------------


def recompare_corrected_capture(
    session: Session,
    delta: ProposedDelta,
    *,
    corrected_value: Any,
    comparison_rule_version: str,
    accepted_revision_id: int | None,
) -> CorrectionRecomparison:
    """Compare one corrected capture against the accepted record, the normal way.

    ``proposed_delta_comparison.compare_stated_subjects`` is the contract every
    producer of Proposed Deltas already compares under, and it is the one used
    here. A second "looks equal" check written for this path would be a rule
    that can disagree with the one every proposal was raised under, on the one
    path where disagreement shows up as a proposal quietly disappearing
    (ADR-0101).

    An empty result is the no-change conclusion and a stated difference is the
    replacement proposal; the caller records whichever it is, and neither is
    decided here by comparing strings.
    """

    accepted = accepted_values(session, int(delta.project_id))
    field = delta.target_field or ""
    comparison = compare_stated_subjects(
        accepted=accepted,
        stated=(
            StatedSubject(
                subject_identity=delta.target_subject_identity,
                values=((field, corrected_value),),
                paired=delta.target_type == "existing_subject",
            ),
        ),
        comparison_rule_version=comparison_rule_version,
        accepted_baseline_revision=(
            None
            if accepted_revision_id is None
            else f"revision:{accepted_revision_id}"
        ),
    )
    return CorrectionRecomparison(
        outcome=NO_CHANGE if not comparison.deltas else STILL_DIFFERS,
        accepted_revision_id=accepted_revision_id,
        comparison_rule_version=comparison_rule_version,
        accepted_value=accepted.get((delta.target_subject_identity, field)),
        corrected_value=corrected_value,
        replacement=comparison.deltas,
    )


# --- Recording one, and reading back what stands ---------------------------


def correction_result_idempotency_key(
    *,
    request_id: int,
    delta_id: int,
    corrected_fact_id: int | None,
    outcome: str,
    accepted_revision_id: int | None,
) -> str:
    """One key per distinct investigation result, so a retry replays it (#457).

    The conclusion and its inputs are in the key because a second
    investigation of the same request that reached a different corrected
    capture, or ran against a different accepted revision, is a second result;
    the same investigation submitted twice is one. Presenting this key with
    different content is a bounded conflict, not an overwrite: the command
    refuses it.
    """

    material = "\x00".join(
        [
            str(request_id),
            str(delta_id),
            "" if corrected_fact_id is None else str(corrected_fact_id),
            outcome,
            "" if accepted_revision_id is None else str(accepted_revision_id),
        ]
    )
    return f"capture-correction-result:{sha256(material.encode('utf-8')).hexdigest()[:32]}"


def record_correction_result(
    session: Session,
    *,
    project_id: int,
    request_id: int,
    delta_id: int,
    challenged_fact_id: int,
    challenged_fact_sha256: str,
    corrected_fact_id: int | None,
    corrected_support_assessment_id: int | None,
    accepted_revision_id: int | None,
    comparison_rule_version: str,
    applicability: PassageApplicability,
    outcome: str,
    replacement_delta_id: int | None,
    finding: str,
    authorized_by_principal: str,
    executed_by: str,
    recorded_at: datetime,
    idempotency_key: str | None = None,
) -> RecordedCorrectionResult:
    """Append the result, and the retirement it establishes, through the command.

    The runtime capabilities hold no write on either relation and a guard
    trigger refuses one that does not arrive through
    ``record_capture_correction_result``, so this is the only way in. It runs
    in the caller's transaction: a rolled-back caller leaves no result and no
    claim that a correction happened.

    Every one of ADR-0101's six required conditions is the command's, not this
    function's -- the challenged capture and request identified, the corrected
    capture supported by the retained source, the declared rule and stated
    accepted revision, the delta still eligible, no accepted value or
    disposition written, and the operation retained and idempotent. A Python
    copy of any of them would be a second opinion that can drift, and the
    concurrency ones would be a guess at state Python cannot hold still.

    ``applicability`` is #945's seventh, and it is passed rather than trusted.
    The command re-derives the whole verdict from the same retained rows -- the
    selected passage's locator, the conflict number its own row states in the
    document matched against the accepted record, and the document's own header
    row read through the released heading vocabulary -- and what travels here is
    the evidence it is checked against: the subject and field the passage was
    found to carry, the passage row's own conflict-number cell and that column's
    header, and the exact header cell the field claim rests on. A caller that
    states a convenient verdict, or assembles its own Support Assessment over an
    unrelated cell, is refused by the command and by the relation's own CHECK.
    """

    if outcome not in CORRECTION_OUTCOMES:
        raise CaptureCorrectionRefused(
            "invalid_outcome",
            f"{outcome!r} is not a correction outcome; the three are "
            f"{NO_CHANGE!r}, {STILL_DIFFERS!r} and {INCONCLUSIVE!r}.",
            delta_id=delta_id,
        )
    key = idempotency_key or correction_result_idempotency_key(
        request_id=request_id,
        delta_id=delta_id,
        corrected_fact_id=corrected_fact_id,
        outcome=outcome,
        accepted_revision_id=accepted_revision_id,
    )
    try:
        answer = session.execute(
            select(
                func.record_capture_correction_result(
                    project_id,
                    request_id,
                    delta_id,
                    challenged_fact_id,
                    challenged_fact_sha256,
                    corrected_fact_id,
                    corrected_support_assessment_id,
                    accepted_revision_id,
                    comparison_rule_version,
                    applicability.subject_identity,
                    applicability.subject_row_identity_segment_id,
                    applicability.subject_row_identity_heading_segment_id,
                    applicability.field,
                    applicability.field_heading_segment_id,
                    outcome,
                    replacement_delta_id,
                    finding,
                    authorized_by_principal,
                    executed_by,
                    recorded_at,
                    key,
                )
            )
        ).scalar_one()
    except DBAPIError as exc:
        raise command_refusal(delta_id, exc) from exc
    session.expire_all()
    retirement = answer.get("retirement_id")
    return RecordedCorrectionResult(
        result_id=int(answer["result_id"]),
        retirement_id=None if retirement is None else int(retirement),
        created=bool(answer["created"]),
    )


def retirements_by_delta(
    session: Session, *, project_id: int, delta_ids: Sequence[int] | None = None
) -> Mapping[int, RetirementReading]:
    """Why each of these proposals left Review, by proposal.

    One statement rather than one per delta: every surface that shows this asks
    it for a page of findings at once. ``delta_ids`` of ``None`` reads the
    whole project, which is what a record history wants.
    """

    query = (
        select(DeltaCaptureCorrection, CaptureCorrectionResult)
        .join(
            CaptureCorrectionResult,
            CaptureCorrectionResult.id == DeltaCaptureCorrection.result_id,
        )
        .where(DeltaCaptureCorrection.project_id == project_id)
        .order_by(DeltaCaptureCorrection.delta_id)
    )
    if delta_ids is not None:
        wanted = [int(value) for value in dict.fromkeys(delta_ids)]
        if not wanted:
            return {}
        query = query.where(DeltaCaptureCorrection.delta_id.in_(wanted))
    found: dict[int, RetirementReading] = {}
    for retirement, result in session.execute(query).all():
        found[int(retirement.delta_id)] = RetirementReading(
            delta_id=int(retirement.delta_id),
            retirement_id=int(retirement.id),
            result_id=int(result.id),
            request_id=int(result.request_id),
            outcome=result.outcome,
            challenged_fact_id=int(result.challenged_fact_id),
            corrected_fact_id=(
                None
                if result.corrected_fact_id is None
                else int(result.corrected_fact_id)
            ),
            corrected_support_assessment_id=(
                None
                if result.corrected_support_assessment_id is None
                else int(result.corrected_support_assessment_id)
            ),
            accepted_revision_id=(
                None
                if result.accepted_revision_id is None
                else int(result.accepted_revision_id)
            ),
            comparison_rule_version=result.comparison_rule_version,
            replacement_delta_id=(
                None
                if result.replacement_delta_id is None
                else int(result.replacement_delta_id)
            ),
            finding=result.finding,
            authorized_by_principal=result.authorized_by_principal,
            executed_by=result.executed_by,
            retired_at=retirement.retired_at,
        )
    return found


@dataclass(frozen=True, slots=True)
class InvestigationResult:
    """One retained investigation result against one report, as a screen shows it.

    Read per *report* rather than per proposal, because this is the half a
    reporter goes looking for: they made a report, and what they want back is
    what operations found. A result that retired the proposal takes the item
    off the Review screen, so the one a coordinator meets here is usually the
    unsubstantiated one -- which is exactly the case ADR-0101 says must leave a
    clarification path open rather than be recorded as a success.
    """

    result_id: int
    request_id: int
    delta_id: int
    outcome: str
    finding: str
    accepted_revision_id: int | None
    replacement_delta_id: int | None
    executed_by: str
    recorded_at: datetime

    @property
    def substantiated(self) -> bool:
        return self.outcome != INCONCLUSIVE

    @property
    def headline(self) -> str:
        return result_sentences(self)[0]

    @property
    def explanation(self) -> str:
        return result_sentences(self)[1]


def results_by_request(
    session: Session, *, project_id: int, request_ids: Sequence[int]
) -> Mapping[int, tuple[InvestigationResult, ...]]:
    """Every investigation result against these reports, by report.

    One grouped statement rather than one per report: the Review screen asks
    this for every report standing against every change it is about to render.
    """

    wanted = [int(value) for value in dict.fromkeys(request_ids)]
    if not wanted:
        return {}
    found: dict[int, list[InvestigationResult]] = {}
    for row in session.scalars(
        select(CaptureCorrectionResult)
        .where(
            CaptureCorrectionResult.project_id == project_id,
            CaptureCorrectionResult.request_id.in_(wanted),
        )
        .order_by(CaptureCorrectionResult.id)
    ).all():
        found.setdefault(int(row.request_id), []).append(
            InvestigationResult(
                result_id=int(row.id),
                request_id=int(row.request_id),
                delta_id=int(row.delta_id),
                outcome=row.outcome,
                finding=row.finding,
                accepted_revision_id=(
                    None
                    if row.accepted_revision_id is None
                    else int(row.accepted_revision_id)
                ),
                replacement_delta_id=(
                    None
                    if row.replacement_delta_id is None
                    else int(row.replacement_delta_id)
                ),
                executed_by=row.executed_by,
                recorded_at=row.recorded_at,
            )
        )
    return {key: tuple(value) for key, value in found.items()}


def retired_delta_ids(session: Session, *, project_id: int) -> frozenset[int]:
    """Every proposal this project retired because its capture was corrected."""

    return frozenset(
        int(value)
        for value in session.scalars(
            select(DeltaCaptureCorrection.delta_id).where(
                DeltaCaptureCorrection.project_id == project_id
            )
        ).all()
    )
