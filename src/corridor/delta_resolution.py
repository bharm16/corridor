"""Resolve one Proposed Delta: the one trusted semantic primitive (#519).

ADR-0076 gives the Project Record four operations, and this module owns the
fourth.  ``accept``, ``edit``, and ``reject`` are the **semantic dispositions**:
each records one attributable Human Record Decision and, standalone, one atomic
Project Record revision.  ``defer`` is **Work List scheduling** (ADR-0084,
ADR-0085): the delta stays open, a dated attributable receipt is written, and no
Project Record revision exists.  Nothing here ever updates or deletes a Source
Fact, a Proposed Delta, a Support Assessment, or a prior decision; a wrong
decision is corrected by a later attributable decision.

**Why one seam, and not two.**  #526 resolves a whole Review Packet in one
transaction, and the obvious shortcut — a packet path that re-implements the
same checks — is how two sets of decision rules are born.  So the seam is split
in two halves that both contexts use:

``validate_child_decision`` reads the delta, the accepted record, and the
effective Support Assessments and returns either the exact ``ValidatedChildDecision``
the authorized command will be asked to write, or a structured ``Refusal``.  It
writes nothing.  ``commit_child_decision`` takes that validated content and
calls the command.  Passing ``revision_id`` makes the decision a child of the
packet's own revision, so no intermediate revision exists; passing ``None``
makes it a standalone act that opens exactly one revision.  ``resolve_delta``
is simply the two halves in sequence, so the standalone command cannot drift
from the packet child.

**Why an edit is constrained.**  ADR-0084 forbids an edit that turns unsupported
free text into a source-backed value.  An ``EditBasis`` is therefore one of four
typed things — a captured Source Fact the human selected, a named replayable
composition over supported Source Facts, a proven lossless normalization, or a
separately attributable source origin — and each is replayed here before the
value can become effective.  Free text on Utility Owner, attribution, Promised
For, completion, Applies To, or an equivalent external fact returns a bounded
``coordination_needed`` result so #526 can open a Follow-up Plan while the
proposed value stays unaccepted; free text anywhere else is a ``constrained_edit``
refusal.

**What was tried before, and rejected.**  Recording the disposition alone (as
#518 shipped it) left the decision with no revision, no principal-bearing
authority row, and no cited support, so "the record now shows this" was a claim
no reader could prove.  Enforcing the rules only in Python was rejected for the
reason ADR-0083 gives: the guard must be in PostgreSQL, so
``resolve_proposed_delta_decision`` re-checks every rule as the record-decision
role and the runtime capabilities hold no write on the decision tables at all.
The Python here is the readable half of the same rules, not the authority.

**Which half raises what is declared, not discovered.**  Both halves refuse in
the same words because ``delta_refusals`` declares every code once, with its
outcome status, its sole raiser, and the one customer sentence it carries.  A
concurrency refusal — a key already bound to other content, two effective
decisions for one field, a value that became effective between the read and the
write — is the command's alone, so ``_refusal`` will not build one here; the
readable half of a rule PostgreSQL also enforces is declared ``both``, which is
ADR-0083's defense in depth rather than a duplicate to tidy away.

Locator validity is never read in this module.  A passed Source Passage Check
says a quotation is where it was cited, and it is never support (ADR-0082).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any, Sequence

from sqlalchemy import BigInteger, bindparam, cast, func, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from corridor.analytics import (
    AnalyticsBinding,
    child_decision_event,
    default_binding,
    emit_event,
)
from corridor.delta_refusals import (
    CONSTRAINED_EDIT,
    COORDINATION_NEEDED,
    DATABASE_ONLY,
    DEFERRED,
    REFUSAL_CODES,
    REFUSED,
    RESOLVED,
    STALE,
    UNSUPPORTED,
    RefusalCode,
    database_refusal_code,
)
from corridor.fact_values import read_fact_value
from corridor.models import (
    DELTA_EFFECT_KINDS,
    DELTA_ORGANIZATION_CHANGE_KINDS,
    DeltaDeferral,
    DeltaDecisionSupport,
    DeltaDisposition,
    DeltaRecordDecision,
    DeltaReviewPacketChild,
    DeltaReviewPacketReversal,
    DeltaCaptureCorrection,
    DeltaSupersession,
    Fact,
    FactDecision,
    ProposedDelta,
    SupportAssessment,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.proposed_deltas import record_delta_deferral


ACCEPT = "accept"
EDIT = "edit"
REJECT = "reject"
DEFER = "defer"
SEMANTIC_ACTIONS = (ACCEPT, EDIT, REJECT)
ACTIONS = (*SEMANTIC_ACTIONS, DEFER)

# What one resolution did to the accepted record, derived from the delta
# itself so a caller cannot mislabel it.  The one judgment a caller must
# supply is whether an organization change corrects a wrong name or records
# that ownership actually moved: those fail differently and the record must
# say which happened.
NEW_SUBJECT = "new_subject"
CHANGED_FIELD = "changed_field"
TIMING = "timing"
ORGANIZATION = "organization"
APPARENT_REMOVAL = "apparent_removal"
CONTRADICTION = "contradiction"
SCHEDULE_KEY_DATE = "schedule_key_date"
CLOSURE = "closure"

# Promised For and the action date a promise carries: a timing change moves a
# promise and keeps its predecessor readable.
TIMING_FIELDS = frozenset({"committed_date", "action_due_date"})
# Required By is the Key Date a schedule states, so it is its own effect.
SCHEDULE_KEY_DATE_FIELDS = frozenset({"need_date"})
ORGANIZATION_FIELDS = frozenset({"external_org", "external_org_contact"})
CLOSURE_FIELDS = frozenset({"closure_result", "marked_resolution"})

# The material external facts ADR-0076 lists, mapped onto the released Fact
# types that carry them today.  Unsupported free text for one of these is a
# coordination question, never an accepted value (ADR-0084).  Agreement and
# permit status has no released Fact type of its own yet; the recorded status
# field stands in until one exists, and the list moves with the vocabulary.
EXTERNAL_FACT_FIELDS = frozenset(
    {
        "external_org",  # Utility Owner
        "external_org_contact",  # attribution of an external commitment
        "committed_date",  # Promised For
        "action_due_date",
        "need_date",  # Required By
        "closure_result",  # completion
        "marked_resolution",  # completion as the source marked it
        "applies_to",  # Applies To
        "operational_status",  # agreement or permit status as recorded
    }
)

# The outcome statuses, the refusal codes, their declared customer sentences,
# and which half of the write boundary raises each one live in `delta_refusals`
# and are imported above, so no raise site here writes a status of its own.


class ContradictoryDeltaStanding(RuntimeError):
    """One Proposed Delta carries more than one terminal relationship.

    A delta is resolved, superseded, or retired because its capture was
    corrected -- never two of those (ADR-0101).  Every write command takes
    ``lock_proposed_delta_terminal`` and refuses under it, so this cannot
    arise from concurrent acts; what it can arise from is an import, or
    retained history written before the guards existed.

    It is raised rather than resolved by precedence because the honest answer
    is that the record contradicts itself and a person has to look.  Returning
    the first word the order happens to reach would hide the contradiction in
    exactly the place a reader would trust it most.
    """

    def __init__(self, delta_id: int, carried: tuple[str, ...]) -> None:
        self.delta_id = delta_id
        self.carried = carried
        super().__init__(
            f"Proposed Delta {delta_id} carries {len(carried)} terminal "
            f"relationships at once ({', '.join(carried)}); a delta has at "
            "most one, so this record contradicts itself and the precedence "
            "order must not be asked to settle it"
        )


class DeltaResolutionRefused(ValueError):
    """A caller cannot construct an attributable delta resolution at all."""


# --- The constrained edit -------------------------------------------------


@dataclass(frozen=True, slots=True)
class CapturedSupport:
    """The human selected one already-captured Source Fact."""

    fact_id: int


@dataclass(frozen=True, slots=True)
class NamedComposition:
    """Supported Source Facts composed under a named replayable transformation."""

    transformation: str
    input_fact_ids: tuple[int, ...]
    result_fact_id: int


@dataclass(frozen=True, slots=True)
class LosslessNormalization:
    """A normalization a replay proves value-preserving."""

    normalization: str
    input_fact_id: int
    result_fact_id: int


@dataclass(frozen=True, slots=True)
class SeparateSourceOrigin:
    """A separately attributable origin, such as a Recorded Verbal Statement."""

    origin: str
    fact_id: int


@dataclass(frozen=True, slots=True)
class FreeText:
    """Words a person typed. Never a source-backed external fact."""

    text: str


EditBasis = (
    CapturedSupport
    | NamedComposition
    | LosslessNormalization
    | SeparateSourceOrigin
    | FreeText
)


def _concatenate_ordered_v1(values: Sequence[Any]) -> Any:
    return " ".join(str(value) for value in values)


def _earliest_date_v1(values: Sequence[Any]) -> Any:
    return min(str(value) for value in values)


def _latest_date_v1(values: Sequence[Any]) -> Any:
    return max(str(value) for value in values)


# Named, replayable compositions.  Each is a pure function of its inputs' own
# values, so the composed value is reproducible from the record at any later
# time and the transformation identity is recorded on the decision.
NAMED_COMPOSITIONS = {
    "concatenate_ordered_v1": _concatenate_ordered_v1,
    "earliest_date_v1": _earliest_date_v1,
    "latest_date_v1": _latest_date_v1,
}


def _trim_whitespace_v1(value: Any) -> Any:
    return str(value).strip()


def _collapse_internal_whitespace_v1(value: Any) -> Any:
    return " ".join(str(value).split())


def _upper_case_v1(value: Any) -> Any:
    return str(value).upper()


# Normalizations whose canonical form a replay proves value-preserving: the
# proof is that both the original and the edited value normalize to the edited
# value, so nothing but the canonicalized difference was changed.
LOSSLESS_NORMALIZATIONS = {
    "trim_whitespace_v1": _trim_whitespace_v1,
    "collapse_internal_whitespace_v1": _collapse_internal_whitespace_v1,
    "upper_case_v1": _upper_case_v1,
}

# The origins an edit may cite as its own attributable source.
SEPARATE_SOURCE_ORIGINS = frozenset(
    {"recorded_verbal_statement", "separately_captured_document"}
)


# --- Requests, validated content, and outcomes ----------------------------


@dataclass(frozen=True, slots=True)
class RecordEffect:
    """One Source Fact this resolution makes (or stops making) effective."""

    fact_id: int
    disposition: str = "include"

    def as_payload(self) -> dict[str, Any]:
        return {"fact_id": int(self.fact_id), "disposition": self.disposition}


@dataclass(frozen=True)
class ChildDecisionRequest:
    """What one coordinator asks of one delta, standalone or inside a packet."""

    project_id: int
    delta_id: int
    action: str
    principal: HumanPrincipal
    idempotency_key: str
    decided_at: datetime
    observed_accepted_revision_id: int | None = None
    record_effects: tuple[RecordEffect, ...] = ()
    support_assessment_ids: tuple[int, ...] = ()
    edit_basis: EditBasis | None = None
    organization_change_kind: str | None = None
    rationale: str | None = None
    effective_value: Any | None = None
    # A source contradiction is a human reading of the delta, not a column on
    # it, so the coordinator says when one resolution settles one.
    contradiction: bool = False
    # Deferral only.  A deferral writes no Project Record revision, so
    # ``idempotency_key`` above is not a revision's key here: it is the
    # identity the caller gives *this request*, and it is what makes a replay
    # one act with the original rather than a second schedule (#903).
    deferred_until: datetime | None = None
    wake_condition: str | None = None
    deferral_reason: str | None = None
    # The scheduling receipt the caller believed was in force. A reschedule
    # names it; a first Defer of an unscheduled change names nothing.
    supersedes_deferral_id: int | None = None


@dataclass(frozen=True)
class ValidatedChildDecision:
    """Exactly the content the authorized command will be asked to write.

    Two contexts that validated the same request produce equal instances of
    this, which is what "one decision-construction seam" means in a test.
    """

    project_id: int
    delta_id: int
    disposition: str
    effect_kind: str
    organization_change_kind: str | None
    principal_subject: str
    idempotency_key: str
    observed_accepted_revision_id: int | None
    decided_at: datetime
    effective_value: Any | None
    rationale: str | None
    edit_basis: dict[str, Any] | None
    record_effects: tuple[dict[str, Any], ...]
    support_assessment_ids: tuple[int, ...]


@dataclass(frozen=True)
class Refusal:
    """Why nothing was written, and what a reading needs to refresh itself."""

    status: str
    reason: str
    detail: str
    delta_id: int
    subject_identity: str | None = None
    field: str | None = None
    proposed_value: Any | None = None
    accepted_value: Any | None = None
    # What the Work List should re-read. The coordinator's own selections are
    # never part of this, so refreshing never discards them.
    current_accepted_revision_id: int | None = None
    delta_status: str | None = None
    # Present only on a coordination-needed result: the exact question #526
    # turns into a Follow-up Plan.
    open_question: str | None = None


def _refusal(code: str, *, detail: str | None = None, **context: Any) -> Refusal:
    """Build one pre-check refusal from the declared vocabulary.

    Every readable-half refusal is constructed here, so the status a screen
    routes on is the declared one rather than a word retyped at each site, and
    ``detail`` is passed only where the sentence names identifiers the reader
    needs.  A code the pre-check may not raise — every concurrency refusal is
    the command's own, decided by state Python cannot hold still — is refused
    outright rather than guessed at (ADR-0083, `delta_refusals`).
    """

    declared = REFUSAL_CODES[code]
    if declared.raiser == DATABASE_ONLY:
        raise DeltaResolutionRefused(
            f"{code!r} is the record-decision command's refusal to raise, not "
            "this module's"
        )
    if detail is None and declared.sentence is None:
        raise DeltaResolutionRefused(
            f"{code!r} declares no fixed sentence because it is raised for "
            "several reasons, so this refusal must say which"
        )
    return Refusal(
        status=declared.status,
        reason=declared.code,
        detail=detail if detail is not None else declared.sentence,
        **context,
    )


@dataclass(frozen=True)
class ResolutionOutcome:
    """One resolution's result, whatever it was."""

    status: str
    delta_id: int
    action: str
    revision_id: int | None = None
    disposition_id: int | None = None
    decision_id: int | None = None
    deferral_id: int | None = None
    fact_decision_ids: tuple[int, ...] = ()
    effect_kind: str | None = None
    created: bool = False
    packet_owned: bool = False
    refusal: Refusal | None = None

    @property
    def wrote_revision(self) -> bool:
        """Whether this act created a Project Record revision of its own."""

        return self.revision_id is not None and not self.packet_owned


# --- The typed effect -----------------------------------------------------


def delta_effect_kind(delta: ProposedDelta, *, contradiction: bool = False) -> str:
    """The typed effect resolving this delta has on the accepted record."""

    if delta.change_type == "apparent_removal":
        return APPARENT_REMOVAL
    if delta.target_type == "proposed_subject":
        return NEW_SUBJECT
    if contradiction:
        return CONTRADICTION
    field_name = delta.target_field or ""
    if field_name in ORGANIZATION_FIELDS:
        return ORGANIZATION
    if field_name in SCHEDULE_KEY_DATE_FIELDS:
        return SCHEDULE_KEY_DATE
    if field_name in TIMING_FIELDS:
        return TIMING
    if field_name in CLOSURE_FIELDS:
        return CLOSURE
    return CHANGED_FIELD


# --- Reading the delta and its accepted context ---------------------------


def live_delta_status(session: Session, delta_id: int) -> str:
    """open, resolved, superseded, capture_corrected, or deferred, derived and never stored.

    Deliberately clockless: this answers whether an act on the delta is
    lawful, and that answer must not change with the hour.  Whether a
    deferred delta is *visible* on this week's Work List is the reading's
    question, not this one's, and it belongs to #494 with the return date
    (ADR-0084, ADR-0085).

    The order is ADR-0101's, and the retirement sits third deliberately: a
    deferred delta whose capture was corrected is retired *without being
    woken*, because a scheduled return to a comparison that no longer exists is
    a return to nothing.  The order is how a reader picks one word for valid
    history; it is not what stops two terminal relationships being written, and
    ADR-0101 is explicit that it must not be asked to be.  That is enforced at
    every write, by the advisory lock `lock_proposed_delta_terminal` and by the
    guards each command takes under it.
    """

    resolved, superseded, corrected = session.execute(
        select(
            select(DeltaDisposition.id)
            .where(DeltaDisposition.delta_id == delta_id)
            .exists(),
            select(DeltaSupersession.id)
            .where(DeltaSupersession.prior_delta_id == delta_id)
            .exists(),
            select(DeltaCaptureCorrection.id)
            .where(DeltaCaptureCorrection.delta_id == delta_id)
            .exists(),
        )
    ).one()
    # All three are read before any is returned, deliberately.  Reading them
    # in precedence order and returning the first would make the order the
    # thing that decides what a contradictory record means, which is exactly
    # what ADR-0101 forbids: "the shared readers may use the order to present
    # valid history; they should not be the mechanism that makes contradictory
    # writes appear harmless."
    carried = tuple(
        name
        for name, present in (
            ("resolved", resolved),
            ("superseded", superseded),
            ("capture_corrected", corrected),
        )
        if present
    )
    if len(carried) > 1:
        raise ContradictoryDeltaStanding(int(delta_id), carried)
    if carried:
        return carried[0]
    if session.scalar(
        select(DeltaDeferral.id)
        .where(DeltaDeferral.delta_id == delta_id)
        # A packet Undo compensates for its scheduling receipts too, so a
        # reversed deferral no longer holds the delta out of immediate work
        # (#526, ADR-0035).  The receipt is never rewritten; the reversal is
        # simply a later row this derivation reads.
        .where(
            ~DeltaDeferral.id.in_(
                select(DeltaReviewPacketChild.deferral_id)
                .join(
                    DeltaReviewPacketReversal,
                    DeltaReviewPacketReversal.receipt_id
                    == DeltaReviewPacketChild.receipt_id,
                )
                .where(DeltaReviewPacketChild.deferral_id.is_not(None))
            )
        )
    ):
        return "deferred"
    return "open"


def current_accepted_revision_id(
    session: Session, *, project_id: int, subject_identity: str, field_name: str | None
) -> int | None:
    """The revision the accepted value of one exact subject and field stands on."""

    query = select(func.max(FactDecision.revision_id)).where(
        FactDecision.project_id == project_id,
        FactDecision.subject_key == subject_identity,
        FactDecision.superseded_by.is_(None),
    )
    if field_name is not None:
        query = query.where(FactDecision.fact_type == field_name)
    return session.scalar(query)


def incoming_source_facts(
    session: Session, delta: ProposedDelta
) -> tuple[Fact, ...]:
    """The captured Source Facts for this delta's exact subject and field.

    A convenience for a caller building a request: the delta records what the
    source said, and this finds the Facts that say it, in append order.
    """

    query = select(Fact).where(
        Fact.project_id == delta.project_id,
        Fact.subject_key == delta.target_subject_identity,
    )
    if delta.target_type == "existing_subject" and delta.target_field:
        query = query.where(Fact.fact_type == delta.target_field)
    return tuple(session.scalars(query.order_by(Fact.id)).all())


def cited_support_assessments(
    session: Session, decision: DeltaRecordDecision
) -> tuple[SupportAssessment, ...]:
    """The Support Assessments one delta decision named, in the order it named them."""

    return tuple(
        session.scalars(
            select(SupportAssessment)
            .join(
                DeltaDecisionSupport,
                DeltaDecisionSupport.support_assessment_id == SupportAssessment.id,
            )
            .where(DeltaDecisionSupport.decision_id == decision.id)
            .order_by(DeltaDecisionSupport.ordinal)
        ).all()
    )


# --- Validation: the half both contexts share -----------------------------


def validate_child_decision(
    session: Session, request: ChildDecisionRequest
) -> ValidatedChildDecision | Refusal:
    """Construct one child decision, or say exactly why there is none.

    Writes nothing.  #526 calls this for every child of a packet and refuses
    the whole transaction when any child comes back a ``Refusal``.
    """

    if request.action not in SEMANTIC_ACTIONS:
        return _refusal(
            "invalid_action",
            detail=f"{request.action!r} is not a semantic delta disposition",
            delta_id=request.delta_id,
        )
    principal = require_human_principal(request.principal)
    if not request.idempotency_key.strip():
        raise DeltaResolutionRefused("a Resolve Delta needs an idempotency key")
    if request.decided_at.tzinfo is None:
        raise DeltaResolutionRefused(
            "a Resolve Delta records an aware decision time from its caller's clock"
        )

    delta = session.get(ProposedDelta, request.delta_id)
    if delta is None or delta.project_id != request.project_id:
        return _refusal(
            "cross_project_delta",
            detail="the Proposed Delta is not this project's to resolve",
            delta_id=request.delta_id,
        )

    status = live_delta_status(session, delta.id)
    if status in ("resolved", "superseded", "capture_corrected"):
        return _delta_state_refusal(session, delta, status)

    effect_kind = delta_effect_kind(delta, contradiction=request.contradiction)
    if effect_kind not in DELTA_EFFECT_KINDS:  # pragma: no cover - vocabulary guard
        raise DeltaResolutionRefused(f"unrecognized delta effect {effect_kind!r}")

    organization_change_kind = request.organization_change_kind
    edit_payload: dict[str, Any] | None = None
    record_effects = tuple(request.record_effects)
    if request.action == EDIT:
        resolved_basis = _validate_edit_basis(session, delta, request)
        if isinstance(resolved_basis, Refusal):
            return resolved_basis
        edit_payload, basis_effects = resolved_basis
        if not record_effects:
            record_effects = basis_effects
    elif request.edit_basis is not None:
        return _refusal(
            "invalid_action",
            detail="only an edit carries an edit basis",
            delta_id=delta.id,
        )

    organization_change_kind = request.organization_change_kind
    if effect_kind == ORGANIZATION:
        if organization_change_kind not in DELTA_ORGANIZATION_CHANGE_KINDS:
            return _refusal(
                "organization_change_kind_required",
                delta_id=delta.id,
                subject_identity=delta.target_subject_identity,
                field=delta.target_field,
            )
    elif organization_change_kind is not None:
        return _refusal(
            "invalid_action",
            detail="only an organization change carries an organization change kind",
            delta_id=delta.id,
        )

    if request.action == REJECT:
        if record_effects:
            return _refusal(
                "invalid_action",
                detail=(
                    "keeping the current accepted value changes no effective "
                    "decision"
                ),
                delta_id=delta.id,
            )
    elif not record_effects:
        return _refusal(
            "missing_record_effect",
            delta_id=delta.id,
            subject_identity=delta.target_subject_identity,
            field=delta.target_field,
        )

    support_ids = tuple(int(value) for value in request.support_assessment_ids)
    scope_refusal = _validate_scope_support_and_staleness(
        session, delta, request, record_effects, support_ids
    )
    if scope_refusal is not None:
        return scope_refusal

    return ValidatedChildDecision(
        project_id=request.project_id,
        delta_id=delta.id,
        disposition=request.action,
        effect_kind=effect_kind,
        organization_change_kind=organization_change_kind,
        principal_subject=principal.subject,
        idempotency_key=request.idempotency_key,
        observed_accepted_revision_id=request.observed_accepted_revision_id,
        decided_at=request.decided_at.astimezone(timezone.utc),
        effective_value=request.effective_value,
        rationale=request.rationale,
        edit_basis=edit_payload,
        record_effects=tuple(effect.as_payload() for effect in record_effects),
        support_assessment_ids=support_ids,
    )


def refresh_context(session: Session, delta: ProposedDelta) -> dict[str, Any]:
    """What a Work List reading needs to refresh, and nothing the caller typed.

    Public because #526 builds the same structured refusal for a coordination
    or deferral child, and two spellings of "what the reading must re-read"
    is how the Work List starts discarding a coordinator's selections.
    """

    return {
        "subject_identity": delta.target_subject_identity,
        "field": delta.target_field,
        "proposed_value": delta.proposed_value,
        "accepted_value": delta.accepted_value,
        "delta_status": live_delta_status(session, delta.id),
        "current_accepted_revision_id": current_accepted_revision_id(
            session,
            project_id=delta.project_id,
            subject_identity=delta.target_subject_identity,
            field_name=delta.target_field,
        ),
    }


def _delta_state_refusal(
    session: Session, delta: ProposedDelta, status: str, *, detail: str | None = None
) -> Refusal:
    """Why this delta is no longer one to act on, in the declared vocabulary.

    ``detail`` is passed only where the declared sentence is the wrong voice
    for the act: the approved words for a corrected capture say that nothing
    was *applied*, which is exactly right for Apply and wrong for scheduling a
    return date.
    """

    return _refusal(
        "already_resolved"
        if status == "resolved"
        else ("superseded_delta" if status == "superseded" else "capture_corrected_delta"),
        detail=detail,
        delta_id=delta.id,
        **refresh_context(session, delta),
    )


def _validate_scope_support_and_staleness(
    session: Session,
    delta: ProposedDelta,
    request: ChildDecisionRequest,
    record_effects: tuple[RecordEffect, ...],
    support_ids: tuple[int, ...],
) -> Refusal | None:
    """Project scope, exact subject and field, support, then staleness."""

    for assessment_id in support_ids:
        assessment = session.get(SupportAssessment, assessment_id)
        if (
            assessment is None
            or assessment.project_id != request.project_id
            or assessment.superseded_by is not None
        ):
            return _refusal(
                "missing_support",
                detail=(
                    f"Support Assessment {assessment_id} is not an effective "
                    "assessment of this project"
                ),
                delta_id=delta.id,
                **refresh_context(session, delta),
            )

    if request.action in (ACCEPT, EDIT) and not support_ids:
        return _refusal(
            "missing_support",
            detail=(
                "a semantic decision names the effective Support Assessments it "
                "relied on; a passed Source Passage Check is not support"
            ),
            delta_id=delta.id,
            **refresh_context(session, delta),
        )

    for effect in record_effects:
        fact = session.get(Fact, effect.fact_id)
        if fact is None or fact.project_id != request.project_id:
            return _refusal("cross_project_fact", delta_id=delta.id)
        if fact.subject_key != delta.target_subject_identity:
            return _refusal(
                "subject_mismatch",
                delta_id=delta.id,
                subject_identity=delta.target_subject_identity,
            )
        if (
            delta.target_type == "existing_subject"
            and effect.disposition == "include"
            and fact.fact_type != delta.target_field
        ):
            return _refusal(
                "field_mismatch",
                delta_id=delta.id,
                subject_identity=delta.target_subject_identity,
                field=delta.target_field,
            )
        if effect.disposition == "include" and not _has_named_value_support(
            session, request.project_id, fact.id, support_ids
        ):
            return _refusal(
                "missing_support",
                detail=(
                    f"Source Fact {fact.id} has no effective value support this "
                    "decision names"
                ),
                delta_id=delta.id,
                **{**refresh_context(session, delta), "field": fact.fact_type},
            )

        standing = current_accepted_revision_id(
            session,
            project_id=request.project_id,
            subject_identity=fact.subject_key,
            field_name=fact.fact_type,
        )
        observed = request.observed_accepted_revision_id or 0
        if standing is not None and standing > observed:
            return _refusal(
                "stale_accepted_revision",
                detail=(
                    f"the accepted record moved to revision {standing} after "
                    f"revision {observed} was read"
                ),
                delta_id=delta.id,
                **{
                    **refresh_context(session, delta),
                    "subject_identity": fact.subject_key,
                    "field": fact.fact_type,
                    "current_accepted_revision_id": standing,
                },
            )
    return None


def _has_named_value_support(
    session: Session, project_id: int, fact_id: int, support_ids: tuple[int, ...]
) -> bool:
    if not support_ids:
        return False
    return bool(
        session.scalar(
            select(SupportAssessment.id).where(
                SupportAssessment.project_id == project_id,
                SupportAssessment.proposition_kind == "source_fact",
                SupportAssessment.fact_id == fact_id,
                SupportAssessment.superseded_by.is_(None),
                SupportAssessment.evidence_role == "value_support",
                SupportAssessment.assessment.in_(
                    ("supported", "partially_supported")
                ),
                SupportAssessment.id.in_(support_ids),
            )
        )
    )


def _validate_edit_basis(
    session: Session, delta: ProposedDelta, request: ChildDecisionRequest
) -> tuple[dict[str, Any], tuple[RecordEffect, ...]] | Refusal:
    """Replay the basis that keeps an edited value source-backed (ADR-0084)."""

    basis = request.edit_basis
    field_name = delta.target_field
    if basis is None or isinstance(basis, FreeText):
        if field_name in EXTERNAL_FACT_FIELDS or delta.target_type == "proposed_subject":
            return _refusal(
                "external_fact_needs_coordination",
                delta_id=delta.id,
                open_question=_coordination_question(delta, basis),
                **refresh_context(session, delta),
            )
        return _refusal(
            "unsupported_free_text",
            delta_id=delta.id,
            **refresh_context(session, delta),
        )

    if isinstance(basis, CapturedSupport):
        fact = _project_fact(session, delta, basis.fact_id)
        if fact is None:
            return _edit_basis_refusal(
                session,
                delta,
                "the selected Source Fact is not captured for this subject",
            )
        return (
            {"kind": "captured_support", "fact_id": int(fact.id)},
            (RecordEffect(fact_id=int(fact.id)),),
        )

    if isinstance(basis, NamedComposition):
        composition = NAMED_COMPOSITIONS.get(basis.transformation)
        if composition is None:
            return _edit_basis_refusal(
                session,
                delta,
                f"{basis.transformation!r} is not a released replayable transformation",
            )
        inputs = []
        for fact_id in basis.input_fact_ids:
            fact = session.get(Fact, fact_id)
            if fact is None or fact.project_id != delta.project_id:
                return _edit_basis_refusal(
                    session, delta, "a composed edit reads only this project's Source Facts"
                )
            inputs.append(fact)
        if not inputs:
            return _edit_basis_refusal(
                session, delta, "a composed edit names the Source Facts it composes"
            )
        result = _project_fact(session, delta, basis.result_fact_id)
        if result is None:
            return _edit_basis_refusal(
                session, delta, "a composed edit resolves to a captured Source Fact"
            )
        replayed = composition([read_fact_value(session, fact) for fact in inputs])
        if replayed != read_fact_value(session, result):
            return _edit_basis_refusal(
                session,
                delta,
                f"replaying {basis.transformation} did not reproduce the edited value",
            )
        return (
            {
                "kind": "named_composition",
                "transformation": basis.transformation,
                "input_fact_ids": [int(fact.id) for fact in inputs],
                "fact_id": int(result.id),
            },
            (RecordEffect(fact_id=int(result.id)),),
        )

    if isinstance(basis, LosslessNormalization):
        normalization = LOSSLESS_NORMALIZATIONS.get(basis.normalization)
        if normalization is None:
            return _edit_basis_refusal(
                session,
                delta,
                f"{basis.normalization!r} is not a released lossless normalization",
            )
        source = session.get(Fact, basis.input_fact_id)
        result = _project_fact(session, delta, basis.result_fact_id)
        if source is None or source.project_id != delta.project_id or result is None:
            return _edit_basis_refusal(
                session, delta, "a normalized edit reads only this project's Source Facts"
            )
        original = read_fact_value(session, source)
        normalized = read_fact_value(session, result)
        # Lossless means the two differ only in what this normalization
        # canonicalizes, and that the canonical form is already canonical.
        if (
            normalization(original) != normalized
            or normalization(normalized) != normalized
        ):
            return _edit_basis_refusal(
                session,
                delta,
                f"{basis.normalization} is not value-preserving between these facts",
            )
        return (
            {
                "kind": "lossless_normalization",
                "normalization": basis.normalization,
                "input_fact_id": int(source.id),
                "fact_id": int(result.id),
            },
            (RecordEffect(fact_id=int(result.id)),),
        )

    if isinstance(basis, SeparateSourceOrigin):
        if basis.origin not in SEPARATE_SOURCE_ORIGINS:
            return _edit_basis_refusal(
                session,
                delta, f"{basis.origin!r} is not an attributable source origin"
            )
        fact = _project_fact(session, delta, basis.fact_id)
        if fact is None:
            return _edit_basis_refusal(
                session, delta, "a separately sourced edit names a captured Source Fact"
            )
        return (
            {
                "kind": "separate_source_origin",
                "origin": basis.origin,
                "fact_id": int(fact.id),
            },
            (RecordEffect(fact_id=int(fact.id)),),
        )

    raise DeltaResolutionRefused(  # pragma: no cover - vocabulary guard
        f"{type(basis).__name__} is not a typed edit basis"
    )


def _project_fact(
    session: Session, delta: ProposedDelta, fact_id: int
) -> Fact | None:
    fact = session.get(Fact, fact_id)
    if fact is None or fact.project_id != delta.project_id:
        return None
    if fact.subject_key != delta.target_subject_identity:
        return None
    return fact


def _edit_basis_refusal(
    session: Session, delta: ProposedDelta, detail: str
) -> Refusal:
    return _refusal(
        "constrained_edit",
        detail=detail,
        delta_id=delta.id,
        **refresh_context(session, delta),
    )


def _coordination_question(delta: ProposedDelta, basis: EditBasis | None) -> str:
    wanted = basis.text if isinstance(basis, FreeText) else None
    subject = delta.target_subject_identity
    field_name = delta.target_field or "this subject"
    if wanted:
        return (
            f"What is the supported {field_name} for {subject}? "
            f"The coordinator believes it is {wanted!r} but no captured source says so."
        )
    return f"What is the supported {field_name} for {subject}?"


# --- Committing: the authorized write -------------------------------------


def open_resolution_revision(
    session: Session,
    *,
    project_id: int,
    principal: HumanPrincipal,
    idempotency_key: str,
) -> int:
    """Open the one Project Record revision a packet's children share (#526)."""

    actor = require_human_principal(principal).subject
    if not idempotency_key.strip():
        raise DeltaResolutionRefused("a Resolve Delta revision needs an idempotency key")
    return int(
        session.scalar(
            select(
                func.open_delta_resolution_revision(
                    project_id, actor, idempotency_key
                )
            )
        )
    )


def commit_child_decision(
    session: Session,
    decision: ValidatedChildDecision,
    *,
    revision_id: int | None = None,
    binding: AnalyticsBinding | None = None,
) -> ResolutionOutcome:
    """Write one validated decision through the record-decision role's command.

    ``revision_id`` names #526's packet-owned revision, in which case this
    child contributes its own separately identified decision and creates no
    intermediate revision.  ``None`` opens exactly one revision for this act.
    """

    try:
        # A refusal is raised by PostgreSQL, which aborts the transaction it
        # was raised in.  The savepoint keeps the refusal structured for the
        # caller instead of turning it into a lost transaction.
        with session.begin_nested():
            outcome = session.scalar(
                select(
                    func.resolve_proposed_delta_decision(
                        decision.project_id,
                        decision.delta_id,
                        decision.disposition,
                        decision.effect_kind,
                        decision.organization_change_kind,
                        decision.principal_subject,
                        decision.idempotency_key,
                        decision.observed_accepted_revision_id,
                        decision.decided_at,
                        _jsonb(decision.effective_value),
                        decision.rationale,
                        _jsonb(decision.edit_basis),
                        _jsonb(list(decision.record_effects)),
                        cast(
                            bindparam(None, list(decision.support_assessment_ids)),
                            ARRAY(BigInteger),
                        ),
                        revision_id,
                    )
                )
            )
    except DBAPIError as exc:
        refusal = _database_refusal(decision, exc)
        _emit_child_decision(
            binding,
            decision=decision,
            outcome=refusal.status,
            reason=refusal.reason,
            revision_id=None,
            packet_owned=revision_id is not None,
        )
        return ResolutionOutcome(
            status=refusal.status,
            delta_id=decision.delta_id,
            action=decision.disposition,
            effect_kind=decision.effect_kind,
            packet_owned=revision_id is not None,
            refusal=refusal,
        )

    session.expire_all()
    written = tuple(int(value) for value in (outcome.get("fact_decision_ids") or ()))
    result = ResolutionOutcome(
        status=RESOLVED,
        delta_id=decision.delta_id,
        action=decision.disposition,
        revision_id=int(outcome["revision_id"]),
        disposition_id=int(outcome["disposition_id"]),
        decision_id=int(outcome["decision_id"]),
        fact_decision_ids=written,
        effect_kind=decision.effect_kind,
        created=bool(outcome["created"]),
        packet_owned=revision_id is not None,
    )
    _emit_child_decision(
        binding,
        decision=decision,
        outcome=RESOLVED,
        reason=None,
        revision_id=result.revision_id,
        packet_owned=result.packet_owned,
    )
    return result


def resolve_delta(
    session: Session,
    request: ChildDecisionRequest,
    *,
    revision_id: int | None = None,
    binding: AnalyticsBinding | None = None,
) -> ResolutionOutcome:
    """Resolve one delta: validate through the shared seam, then write once."""

    if request.action == DEFER:
        return defer_delta(session, request, binding=binding)
    validated = validate_child_decision(session, request)
    if isinstance(validated, Refusal):
        _emit_refusal(binding, request, validated)
        return ResolutionOutcome(
            status=validated.status,
            delta_id=request.delta_id,
            action=request.action,
            packet_owned=revision_id is not None,
            refusal=validated,
        )
    return commit_child_decision(
        session, validated, revision_id=revision_id, binding=binding
    )


def defer_delta(
    session: Session,
    request: ChildDecisionRequest,
    *,
    binding: AnalyticsBinding | None = None,
) -> ResolutionOutcome:
    """Schedule one delta for later. It stays open and no revision is written."""

    principal = require_human_principal(request.principal)
    if request.decided_at.tzinfo is None:
        raise DeltaResolutionRefused(
            "a deferral records an aware time from its caller's clock"
        )
    if request.deferred_until is None and request.wake_condition is None:
        raise DeltaResolutionRefused(
            "a deferral carries a return date or a wake condition"
        )
    delta = session.get(ProposedDelta, request.delta_id)
    if delta is None or delta.project_id != request.project_id:
        refusal = _refusal(
            "cross_project_delta",
            detail="the Proposed Delta is not this project's to defer",
            delta_id=request.delta_id,
        )
        _emit_refusal(binding, request, refusal)
        return ResolutionOutcome(
            status=REFUSED,
            delta_id=request.delta_id,
            action=DEFER,
            refusal=refusal,
        )
    status = live_delta_status(session, delta.id)
    if status in ("resolved", "superseded", "capture_corrected"):
        refusal = _delta_state_refusal(
            session,
            delta,
            status,
            detail=(
                "this proposed change left Review because its source reading "
                "was corrected, so there is nothing to schedule a return to"
                if status == "capture_corrected"
                else None
            ),
        )
        _emit_refusal(binding, request, refusal)
        return ResolutionOutcome(
            status=REFUSED, delta_id=delta.id, action=DEFER, refusal=refusal
        )

    try:
        with session.begin_nested():
            # ADR-0084 puts the deferral receipt with the delta's own
            # lifecycle (#518); this module owns whether the act is lawful,
            # not the row's shape.
            receipt = record_delta_deferral(
                session,
                project_id=request.project_id,
                delta_id=delta.id,
                deferred_at=request.decided_at.astimezone(timezone.utc),
                scheduled_by_principal=principal.subject,
                request_identity=request.idempotency_key,
                deferred_until=request.deferred_until,
                wake_condition=request.wake_condition,
                reason=request.deferral_reason,
                supersedes_deferral_id=request.supersedes_deferral_id,
            )
            deferral_id = int(receipt.id)
    except DBAPIError as exc:
        refusal = _database_refusal_for(delta.id, exc)
        _emit_refusal(binding, request, refusal)
        return ResolutionOutcome(
            status=refusal.status, delta_id=delta.id, action=DEFER, refusal=refusal
        )
    session.expire_all()
    _emit_child_decision(
        binding,
        decision=None,
        outcome=DEFERRED,
        reason=None,
        revision_id=None,
        packet_owned=False,
        request=request,
    )
    return ResolutionOutcome(
        status=DEFERRED,
        delta_id=delta.id,
        action=DEFER,
        deferral_id=deferral_id,
        created=True,
    )


def _database_refusal(
    decision: ValidatedChildDecision, exc: DBAPIError
) -> Refusal:
    return _database_refusal_for(decision.delta_id, exc)


def _database_refusal_for(delta_id: int, exc: DBAPIError) -> Refusal:
    """Map the command's own stable refusal token through the declared vocabulary.

    The token is the agreement between the two halves, so the status a screen
    routes on comes from the declaration rather than from a second table beside
    the scraper.  The command's own message line is kept as the detail: it names
    the identifiers the coordinator needs, which no declared sentence can.
    """

    message = str(getattr(exc, "orig", exc))
    declared: RefusalCode = database_refusal_code(message)
    return Refusal(
        status=declared.status,
        reason=declared.code,
        detail=message.strip().splitlines()[0],
        delta_id=delta_id,
    )


# --- The versioned event contract (#558) ----------------------------------


def _emit_refusal(
    binding: AnalyticsBinding | None,
    request: ChildDecisionRequest,
    refusal: Refusal,
) -> None:
    _emit_child_decision(
        binding,
        decision=None,
        outcome=refusal.status,
        reason=refusal.reason,
        revision_id=None,
        packet_owned=False,
        request=request,
    )


def _emit_child_decision(
    binding: AnalyticsBinding | None,
    *,
    decision: ValidatedChildDecision | None,
    outcome: str,
    reason: str | None,
    revision_id: int | None,
    packet_owned: bool,
    request: ChildDecisionRequest | None = None,
) -> None:
    """Every accept, edit, reject, defer, and refusal is one versioned event.

    A stale-revision refusal is emitted exactly like a decision, because the
    measurement the event exists for — how often a coordinator's reading was
    already out of date — is invisible if only the successes are recorded.
    """

    if decision is not None:
        project_id = decision.project_id
        delta_id = decision.delta_id
        action = decision.disposition
        effect_kind = decision.effect_kind
        support_count = len(decision.support_assessment_ids)
        occurred_at = decision.decided_at
    elif request is not None:
        project_id = request.project_id
        delta_id = request.delta_id
        action = request.action
        effect_kind = None
        support_count = len(request.support_assessment_ids)
        occurred_at = request.decided_at.astimezone(timezone.utc)
    else:  # pragma: no cover - one of the two is always supplied
        return

    emit_event(
        child_decision_event(
            binding or default_binding(),
            occurred_at=occurred_at,
            project_id=project_id,
            delta_id=delta_id,
            action=action,
            effect_kind=effect_kind,
            outcome=outcome,
            refusal_reason=reason,
            revision_id=revision_id,
            packet_owned=packet_owned,
            support_assessment_count=support_count,
        )
    )


def _jsonb(value: object):
    if value is None:
        return cast(bindparam(None, None), JSONB)
    return cast(bindparam(None, json.dumps(value, default=str)), JSONB)
