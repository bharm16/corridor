"""Disputes: what the revisions disagree about, and settling it.

A Dispute is not a state a row is put into. It is what you see when two
revisions of a matrix both state a field and state it differently — a
query over Assertions, exactly as an Exception is a query over the
Ledger. That is why a disputed row is an ordinary workable row: nothing
was withheld to create the Dispute, so nothing has to be resolved to
undo it.

Settling one is Adjudication in the narrow sense (ADR-0031): a reviewer
says what the record concludes for that field. The losing claim is never
erased — Assertions preserve every claim — so the settlement records the
conclusion beside them, and records how far its judgment reaches. A
revision that arrives afterwards and disagrees again reopens the Dispute
on its own, because its Assertion postdates what the reviewer saw.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor import audit, notifications, refusals
from corridor.fact_decisions import record_human_fact_decision
from corridor.fact_types import FACT_TYPE_CONTRACTS
from corridor.models import (
    Assertion,
    Candidate,
    Dependency,
    DisputeHistoryResolution,
    DisputeSettlement,
    Document,
    EvidenceLink,
    ExtractedProposal,
    Fact,
    FactDecision,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.project_lock import lock_project
from corridor.measurement_cases import record_dispute_settlement_case
from corridor.work_decisions import (
    active_roster_member,
    assign_internal_owner,
    set_next_action,
)


class DisputeRefusal(refusals.Refusal, ValueError):
    """A Dispute act refused; nothing was written.

    The family declares its own kind (#794 card 22) so an adapter maps it once
    instead of standing a generic sentence in front of a bare ``ValueError``.
    ``CONFLICT`` is the base kind because the acts this refuses contradict what
    the record already holds; the two named subclasses say something narrower.
    Still a ``ValueError`` so callers that already catch one keep working.
    """

    refusal_kind = refusals.CONFLICT


class NoSuchDispute(DisputeRefusal):
    """The field is not in dispute, so there is nothing to settle."""

    refusal_kind = refusals.NOT_OFFERED


class DisputeMovedOn(DisputeRefusal):
    """A claim arrived after the page was read; the judgment would cover
    evidence the reviewer never saw."""

    refusal_kind = refusals.STALE


@dataclass(frozen=True)
class DisputeHistoryAssessment:
    """The deterministic first layer of ADR-0061 for one raw Dispute.

    ``contested`` is deliberately an outcome without a writer.  It is an
    explanation for the human card, never authority to create a Settlement.
    """

    dependency_id: int
    field_name: str
    outcome: str
    older_assertion_id: int | None
    newer_assertion_id: int | None
    covers_assertion_id: int
    why: str


@dataclass(frozen=True)
class DisputeClarification:
    """The two existing Work Decision receipts written by one request."""

    owner_decision_id: int
    next_action_decision_id: int


_PHYSICAL_FIELDS = frozenset(
    {
        "station_from",
        "station_to",
        "location_desc",
        "utility_type",
        "utility_size",
        "utility_material",
    }
)
_HISTORY_RULE_VERSION = "adr-0061-staleness-v1"


# Exactly the characters Python's str.strip() removes. PostgreSQL's trim and
# [[:space:]] definitions are narrower, so the SQL predicate spells out the
# same whitespace vocabulary used by the Python claim reader.
_PY_WHITESPACE = (
    "\t\n\x0b\x0c\r\x1c\x1d\x1e\x1f \x85\xa0\u1680"
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008"
    "\u2009\u200a\u2028\u2029\u202f\u205f\u3000"
)


def claim_predicates():
    """The SQL half of whether an Assertion says anything disputable."""
    return (
        Assertion.asserted_value.is_not(None),
        func.regexp_replace(
            Assertion.asserted_value,
            f"^[{_PY_WHITESPACE}]+|[{_PY_WHITESPACE}]+$",
            "",
            "g",
        )
        != "",
    )


@dataclass(frozen=True)
class DisputedClaim:
    """One revision's claim about a disputed field, with its page."""

    assertion_id: int
    value: str | None
    document_id: int
    document_filename: str
    doc_date: date | None
    document_type: str
    page_no: int | None
    quote: str | None
    # When the document entered the corpus. A printed date is claimable;
    # registration is observed, so a backdated document contradicting its
    # own registration order is a provenance conflict, never quiet history.
    registered_at: datetime | None = None


@dataclass(frozen=True)
class Dispute:
    """One field several revisions state differently."""

    dependency_id: int
    field_name: str
    claims: tuple[DisputedClaim, ...]

    @property
    def newest_claim_id(self) -> int:
        """The claim a reviewer looking at this page has seen up to.

        Submitted with a settlement so the judgment covers exactly what
        was compared. A claim that lands between render and submit makes
        the two disagree, and the settlement is refused rather than
        silently reaching over evidence nobody read.
        """
        return max(claim.assertion_id for claim in self.claims)

    @property
    def values(self) -> tuple[str | None, ...]:
        seen: list[str | None] = []
        for claim in self.claims:
            if claim.value not in seen:
                seen.append(claim.value)
        return tuple(seen)


def settled_field_names(
    session: Session, dependency_ids: list[int]
) -> dict[int, set[str]]:
    """Fields whose newest settlement still covers every claim made.

    A settlement covers the claims that existed when it was made. The
    comparison is against the newest Assertion id for that field now, so
    a claim that arrived later leaves the field unsettled again without
    anything having to notice the arrival.
    """
    if not dependency_ids:
        return {}

    # Only the claims that could contradict anything. `contradicted_fields`
    # counts verified, non-blank assertions; measuring a settlement's reach
    # against every row let an unverified or blank assertion — a bad
    # citation, not a source disagreeing — arrive with a higher id and
    # un-settle a Dispute a reviewer had already decided.
    newest_claim = (
        select(
            Assertion.dependency_id.label("dependency_id"),
            Assertion.field_name.label("field_name"),
            func.max(Assertion.id).label("newest_assertion_id"),
        )
        .join(EvidenceLink, Assertion.evidence_link_id == EvidenceLink.id)
        .where(
            Assertion.dependency_id.in_(dependency_ids),
            EvidenceLink.verified.is_(True),
            *claim_predicates(),
        )
        .group_by(Assertion.dependency_id, Assertion.field_name)
        .subquery()
    )
    newest_settlement = (
        select(
            DisputeSettlement.dependency_id.label("dependency_id"),
            DisputeSettlement.field_name.label("field_name"),
            func.max(DisputeSettlement.covers_assertion_id).label("covers"),
        )
        .where(DisputeSettlement.dependency_id.in_(dependency_ids))
        .group_by(DisputeSettlement.dependency_id, DisputeSettlement.field_name)
        .subquery()
    )
    rows = session.execute(
        select(newest_settlement.c.dependency_id, newest_settlement.c.field_name)
        .join(
            newest_claim,
            (newest_claim.c.dependency_id == newest_settlement.c.dependency_id)
            & (newest_claim.c.field_name == newest_settlement.c.field_name),
        )
        .where(newest_settlement.c.covers >= newest_claim.c.newest_assertion_id)
    ).all()

    settled: dict[int, set[str]] = {}
    for dependency_id, field_name in rows:
        settled.setdefault(dependency_id, set()).add(field_name)
    return settled


def history_resolved_field_names(
    session: Session, dependency_ids: list[int]
) -> dict[int, set[str]]:
    """Physical fields mechanically concluded from exact chronology.

    This is intentionally separate from ``settled_field_names``.  A history
    resolution does not say a person settled a dispute, and only the physical
    branch removes the card.  A contractual outcome stays contested so the
    required amendment work remains visible.
    """
    return _current_history_outcome_fields(
        session, dependency_ids, outcome=PHYSICAL_SUPERSEDED
    )


CONTRACTUAL_AMENDMENT = "contractual_amendment"
PHYSICAL_SUPERSEDED = "physical_superseded"


def assessed_amendment_field_names(
    assessments: Iterable[DisputeHistoryAssessment],
) -> set[str]:
    """The fields whose *live* history assessment is a stale executed agreement.

    This is the sibling of ``contractual_amendment_field_names`` and answers a
    different question, which is why both are here rather than one of them being
    spelled out in a screen. That one reads the *retained*
    ``DisputeHistoryResolution`` rows and is therefore silent until
    ``apply_staleness_resolutions`` has run; this one reads the assessments
    ``history_assessments_for`` derives from the preserved claims, so it speaks
    as soon as the chronology says a contractual outcome applies.

    The Constraint screen needs the second: it renders the amendment work — the
    why-line and both quotations, with no settle control — for a field the
    chronology has already made stale. It used to derive this set itself, from
    the outcome string spelled as a literal in the web adapter.
    """
    return {
        assessment.field_name
        for assessment in assessments
        if assessment.outcome == CONTRACTUAL_AMENDMENT
    }


def contractual_amendment_field_names(
    session: Session, dependency_ids: list[int]
) -> dict[int, set[str]]:
    """Current stale-agreement fields that must stay visible as follow-up.

    The history receipt is immutable, while this view is not: a later
    Assertion exceeds its recorded coverage and reopens the ordinary human
    discrepancy instead of leaving an obsolete amendment task standing alone.
    """
    return _current_history_outcome_fields(
        session, dependency_ids, outcome=CONTRACTUAL_AMENDMENT
    )


def _current_history_outcome_fields(
    session: Session, dependency_ids: list[int], *, outcome: str
) -> dict[int, set[str]]:
    """Read one still-current immutable history outcome by its coverage."""
    if not dependency_ids:
        return {}
    newest_claim = (
        select(
            Assertion.dependency_id.label("dependency_id"),
            Assertion.field_name.label("field_name"),
            func.max(Assertion.id).label("newest_assertion_id"),
        )
        .join(EvidenceLink, Assertion.evidence_link_id == EvidenceLink.id)
        .where(
            Assertion.dependency_id.in_(dependency_ids),
            EvidenceLink.verified.is_(True),
            *claim_predicates(),
        )
        .group_by(Assertion.dependency_id, Assertion.field_name)
        .subquery()
    )
    rows = session.execute(
        select(
            DisputeHistoryResolution.dependency_id,
            DisputeHistoryResolution.field_name,
        )
        .join(
            newest_claim,
            (newest_claim.c.dependency_id == DisputeHistoryResolution.dependency_id)
            & (newest_claim.c.field_name == DisputeHistoryResolution.field_name),
        )
        .where(
            DisputeHistoryResolution.dependency_id.in_(dependency_ids),
            DisputeHistoryResolution.outcome == outcome,
            DisputeHistoryResolution.covers_assertion_id
            >= newest_claim.c.newest_assertion_id,
        )
    ).all()
    result: dict[int, set[str]] = {}
    for dependency_id, field_name in rows:
        result.setdefault(dependency_id, set()).add(field_name)
    return result


def contradicted_fields(
    session: Session, dependency_ids: list[int]
) -> dict[int, list[str]]:
    """Standing Disputes by Dependency after current Settlements are applied."""
    if not dependency_ids:
        return {}

    settled = settled_field_names(session, dependency_ids)
    history_resolved = history_resolved_field_names(session, dependency_ids)
    found: dict[int, list[str]] = {}
    for dependency_id, name in session.execute(
        select(Assertion.dependency_id, Assertion.field_name)
        .join(EvidenceLink, Assertion.evidence_link_id == EvidenceLink.id)
        .where(
            Assertion.dependency_id.in_(dependency_ids),
            EvidenceLink.verified.is_(True),
            *claim_predicates(),
        )
        .group_by(Assertion.dependency_id, Assertion.field_name)
        .having(func.count(func.distinct(Assertion.asserted_value)) > 1)
    ).all():
        if name in settled.get(dependency_id, ()) or name in history_resolved.get(
            dependency_id, ()
        ):
            continue
        found.setdefault(dependency_id, []).append(name)
    return found


def _disagreeing_field_names(session: Session, dependency_id: int) -> list[str]:
    """Fields with two or more distinct verified claims, settled or not."""
    return list(
        session.scalars(
            select(Assertion.field_name)
            .join(EvidenceLink, Assertion.evidence_link_id == EvidenceLink.id)
            .where(
                Assertion.dependency_id == dependency_id,
                EvidenceLink.verified.is_(True),
                *claim_predicates(),
            )
            .group_by(Assertion.field_name)
            .having(func.count(func.distinct(Assertion.asserted_value)) > 1)
        ).all()
    )


def disputes_for(
    session: Session, dependency_id: int, *, include_settled: bool = False
) -> list[Dispute]:
    """Every standing Dispute on one record, with both pages to read.

    `include_settled` also returns the disagreements a settlement has
    answered — for the page that offers to change a conclusion, because a
    mistaken settlement is corrected by settling again (ADR-0031), and
    correcting one needs the claims in front of the reviewer.
    """
    if include_settled:
        names = _disagreeing_field_names(session, dependency_id)
        # A human conclusion stays available for correction; a mechanical
        # physical-history outcome does not.  The latter is not a choice a
        # reviewer can amend, and a later assertion makes it visible again by
        # exceeding its exact coverage watermark.
        names = [
            name
            for name in names
            if name
            not in history_resolved_field_names(session, [dependency_id]).get(
                dependency_id, set()
            )
        ]
    else:
        names = contradicted_fields(session, [dependency_id]).get(
            dependency_id, []
        )
    if not names:
        return []

    found = []
    for name in sorted(names):
        rows = session.execute(
            select(Assertion, EvidenceLink, Document)
            .join(EvidenceLink, Assertion.evidence_link_id == EvidenceLink.id)
            .join(Document, EvidenceLink.document_id == Document.id)
            .where(
                Assertion.dependency_id == dependency_id,
                Assertion.field_name == name,
                EvidenceLink.verified.is_(True),
                # The same claim rule everywhere: a blank assertion is an
                # absent column, not a competing value, and rendering one
                # as a claim would let the page disagree with the engine.
                *claim_predicates(),
            )
            .order_by(Document.doc_date.asc().nulls_first(), Assertion.id.asc())
        ).all()
        found.append(
            Dispute(
                dependency_id=dependency_id,
                field_name=name,
                claims=tuple(
                    DisputedClaim(
                        assertion_id=assertion.id,
                        value=assertion.asserted_value,
                        document_id=document.id,
                        document_filename=document.filename,
                        doc_date=document.doc_date,
                        document_type=document.doc_type,
                        page_no=link.page_no,
                        quote=link.quote,
                        registered_at=document.created_at,
                    )
                    for assertion, link, document in rows
                ),
            )
        )
    return found


def history_assessments_for(
    session: Session, dependency_id: int
) -> list[DisputeHistoryAssessment]:
    """Explain the history layer for every raw disagreement on one row.

    This reads the preserved claims directly, ignoring both human settlements
    and prior history outcomes.  The result therefore remains a useful
    explanation when a later source reopens a field.  It writes nothing; only
    ``apply_staleness_resolutions`` may retain the two strictly mechanical
    outcomes.
    """
    dependency = session.get(Dependency, dependency_id)
    if dependency is None:
        raise ValueError(f"dependency {dependency_id} does not exist")
    assessments = []
    for dispute in _raw_disputes_for(session, dependency_id):
        # Only a like-for-like text projection can witness the row's own
        # recorded position; anything else stays out of the predicate.
        current_value = getattr(dependency, dispute.field_name, None)
        if not isinstance(current_value, str):
            current_value = None
        assessments.append(_history_assessment(dispute, current_value))
    return assessments


def apply_staleness_resolutions(
    session: Session, dependency_id: int
) -> list[DisputeHistoryResolution]:
    """Retain eligible ADR-0061 chronology outcomes and nothing else.

    In particular, this function has no path that writes a human Settlement:
    contested cases receive an explanation and remain for a person.  Calling
    it twice against unchanged evidence is idempotent; a later Assertion has
    a new coverage watermark and must be assessed afresh.
    """
    dependency = session.get(Dependency, dependency_id)
    if dependency is None:
        raise ValueError(f"dependency {dependency_id} does not exist")
    lock_project(session, dependency.project_id)
    session.refresh(dependency)
    if dependency.dismissed_at is not None:
        return []

    recorded: list[DisputeHistoryResolution] = []
    for assessment in history_assessments_for(session, dependency_id):
        if assessment.outcome not in {"physical_superseded", "contractual_amendment"}:
            continue
        assert assessment.older_assertion_id is not None
        assert assessment.newer_assertion_id is not None
        existing = session.scalar(
            select(DisputeHistoryResolution).where(
                DisputeHistoryResolution.dependency_id == dependency_id,
                DisputeHistoryResolution.field_name == assessment.field_name,
                DisputeHistoryResolution.covers_assertion_id
                == assessment.covers_assertion_id,
            )
        )
        if existing is not None:
            continue
        resolution = DisputeHistoryResolution(
            dependency_id=dependency_id,
            field_name=assessment.field_name,
            older_assertion_id=assessment.older_assertion_id,
            newer_assertion_id=assessment.newer_assertion_id,
            covers_assertion_id=assessment.covers_assertion_id,
            outcome=assessment.outcome,
            rule_version=_HISTORY_RULE_VERSION,
            why=assessment.why,
        )
        session.add(resolution)
        session.flush([resolution])
        if assessment.outcome == "physical_superseded":
            newer = session.get(Assertion, assessment.newer_assertion_id)
            if (
                newer is not None
                and assessment.field_name in _PROJECTED_FIELDS
                and not (
                    newer.asserted_value is None
                    and assessment.field_name in _NOT_NULL_COLUMNS
                )
                and _value_fits_column(assessment.field_name, newer.asserted_value)
            ):
                setattr(dependency, assessment.field_name, newer.asserted_value)
        recorded.append(resolution)
    return recorded


def record_dispute_clarification(
    session: Session,
    dependency_id: int,
    field_name: str,
    *,
    roster_entry_id: int,
    next_action: str,
    due_date: date | None,
    due_date_unknown_reason: str | None,
    principal: HumanPrincipal,
) -> DisputeClarification:
    """Keep a contested discrepancy open while atomically recording follow-up.

    This deliberately delegates the actual writes to the existing Work
    Decision writers.  The savepoint means an invalid roster member, action,
    or date leaves neither an owner projection nor a partial Next Action.
    """
    recorder = require_human_principal(principal)
    dependency = session.get(Dependency, dependency_id)
    if dependency is None:
        raise ValueError(f"dependency {dependency_id} does not exist")
    if field_name not in {
        dispute.field_name for dispute in disputes_for(session, dependency_id)
    }:
        raise NoSuchDispute(
            f"{field_name!r} is not a current contested field — clarification is unnecessary"
        )
    roster = active_roster_member(
        session, roster_entry_id, project_id=dependency.project_id
    )

    with session.begin_nested():
        owner = assign_internal_owner(
            session, dependency_id, roster.display_name, principal=recorder
        )
        # This clarification names an accountable person for the Constraint, so
        # the committed assignment registers one new-assignment notification,
        # atomically with the clarification (#351).
        notifications.register_new_assignment_notification(
            session,
            assignment_decision=owner,
            roster_entry=roster,
            principal=recorder,
        )
        action = set_next_action(
            session,
            dependency_id,
            next_action,
            due_date=due_date,
            due_date_unknown_reason=due_date_unknown_reason,
            principal=recorder,
        )
    return DisputeClarification(
        owner_decision_id=owner.id,
        next_action_decision_id=action.id,
    )


def _raw_disputes_for(session: Session, dependency_id: int) -> list[Dispute]:
    """Raw verified disagreements before any conclusion is applied."""
    names = _disagreeing_field_names(session, dependency_id)
    if not names:
        return []
    found = []
    for name in sorted(names):
        rows = session.execute(
            select(Assertion, EvidenceLink, Document)
            .join(EvidenceLink, Assertion.evidence_link_id == EvidenceLink.id)
            .join(Document, EvidenceLink.document_id == Document.id)
            .where(
                Assertion.dependency_id == dependency_id,
                Assertion.field_name == name,
                EvidenceLink.verified.is_(True),
                *claim_predicates(),
            )
            .order_by(Document.doc_date.asc().nulls_first(), Assertion.id.asc())
        ).all()
        found.append(
            Dispute(
                dependency_id=dependency_id,
                field_name=name,
                claims=tuple(
                    DisputedClaim(
                        assertion_id=assertion.id,
                        value=assertion.asserted_value,
                        document_id=document.id,
                        document_filename=document.filename,
                        doc_date=document.doc_date,
                        document_type=document.doc_type,
                        page_no=link.page_no,
                        quote=link.quote,
                        registered_at=document.created_at,
                    )
                    for assertion, link, document in rows
                ),
            )
        )
    return found


def _history_assessment(
    dispute: Dispute, current_value: str | None
) -> DisputeHistoryAssessment:
    """Apply only the narrow, date-provable staleness predicate."""
    claims = dispute.claims
    coverage = dispute.newest_claim_id
    dates = [claim.doc_date for claim in claims]
    if any(not isinstance(value, date) for value in dates):
        return _contested(
            dispute,
            "the sources cannot be ordered because one or more document dates are unavailable",
        )
    assert all(isinstance(value, date) for value in dates)
    if len(set(dates)) != len(dates):
        shared = sorted(
            {value.isoformat() for value in dates if dates.count(value) > 1}
        )
        return _contested(
            dispute,
            "the sources cannot be ordered because competing claims carry the "
            f"same document date ({', '.join(shared)})",
        )
    ordered = tuple(sorted(claims, key=lambda claim: (claim.doc_date, claim.assertion_id)))
    values = [claim.value for claim in ordered]
    compressed: list[DisputedClaim] = []
    for claim in ordered:
        if not compressed or compressed[-1].value != claim.value:
            compressed.append(claim)
    if len(compressed) > 2:
        sequence = " → ".join(
            f"{claim.value!r} ({claim.doc_date.isoformat()})" for claim in compressed
        )
        return _contested(dispute, f"the value oscillated: {sequence}")
    older, newer = compressed
    if older.document_id == newer.document_id:
        return _contested(
            dispute,
            "the competing values are from one document, so cross-document history cannot order them",
        )
    if older.doc_date >= newer.doc_date:
        return _contested(
            dispute,
            "the competing source dates "
            f"({older.doc_date.isoformat()}, {newer.doc_date.isoformat()}) do "
            "not establish an older value followed by a recorded change",
        )
    if (
        older.registered_at is not None
        and newer.registered_at is not None
        and older.registered_at > newer.registered_at
    ):
        return _contested(
            dispute,
            f"provenance conflict: {older.document_filename} carries the "
            f"earlier document date {older.doc_date.isoformat()} but was "
            f"registered {older.registered_at.date().isoformat()}, after "
            f"{newer.document_filename} ({newer.doc_date.isoformat()}, "
            f"registered {newer.registered_at.date().isoformat()}) — a "
            "backdated chronology is surfaced, never trusted mechanically",
        )
    if current_value is not None and current_value != newer.value:
        origin = (
            f" from {older.document_filename} ({older.doc_date.isoformat()})"
            if current_value == older.value
            else ""
        )
        return _contested(
            dispute,
            f"the disagreeing document {newer.document_filename} "
            f"({newer.doc_date.isoformat()}) is newer than the row's recorded "
            f"value {current_value!r}{origin} — a newer source challenging "
            "the record is a human question",
        )
    change = (
        f"the recorded field change from {older.value!r} in "
        f"{older.document_filename} ({older.doc_date.isoformat()}) to "
        f"{newer.value!r} in {newer.document_filename} ({newer.doc_date.isoformat()})"
    )
    if _is_executed_agreement(older):
        return DisputeHistoryAssessment(
            dependency_id=dispute.dependency_id,
            field_name=dispute.field_name,
            outcome="contractual_amendment",
            older_assertion_id=older.assertion_id,
            newer_assertion_id=newer.assertion_id,
            covers_assertion_id=coverage,
            why=(
                f"{change}; field data changed under the executed agreement — "
                "flag for amendment. "
                f"Agreement quote: {older.quote or '(no quote recorded)'} "
                f"Newer source quote: {newer.quote or '(no quote recorded)'}"
            ),
        )
    if dispute.field_name not in _PHYSICAL_FIELDS:
        return _contested(
            dispute,
            f"{change}, but this field is not in the mechanical physical-fact class",
        )
    return DisputeHistoryAssessment(
        dependency_id=dispute.dependency_id,
        field_name=dispute.field_name,
        outcome="physical_superseded",
        older_assertion_id=older.assertion_id,
        newer_assertion_id=newer.assertion_id,
        covers_assertion_id=coverage,
        why=f"{change}; earlier value superseded by field update",
    )


def _value_fits_column(field_name: str, value: str | None) -> bool:
    """A mechanical layer never forces a value the schema would refuse.

    A hostile or malformed claim can carry text longer than the projected
    column.  The chronology receipt is still recorded; only the write-through
    to the Constraint Record is withheld, leaving the field for a person.
    """
    if value is None:
        return True
    column = Dependency.__table__.columns.get(field_name)
    if column is None:
        return False
    limit = getattr(column.type, "length", None)
    return limit is None or len(value) <= limit


def _contested(dispute: Dispute, why: str) -> DisputeHistoryAssessment:
    return DisputeHistoryAssessment(
        dependency_id=dispute.dependency_id,
        field_name=dispute.field_name,
        outcome="contested",
        older_assertion_id=None,
        newer_assertion_id=None,
        covers_assertion_id=dispute.newest_claim_id,
        why=why,
    )


def _is_executed_agreement(claim: DisputedClaim) -> bool:
    """Do not call a draft contract an executed agreement from its type alone."""
    if claim.document_type != "agreement":
        return False
    source = " ".join((claim.document_filename, claim.quote or "")).casefold()
    return "executed" in source or "signed by" in source


def settle_dispute(
    session: Session,
    dependency_id: int,
    field_name: str,
    *,
    value: str | None,
    principal: HumanPrincipal,
    saw_claim_id: int | None = None,
) -> DisputeSettlement:
    """Record what the record concludes for one disputed field.

    The value need not be one of the claims: a reviewer reading both
    pages may conclude a third thing, which is the same latitude
    edit-then-accept has always given. What it may not be is unattributed
    — settling is a human act on the Ledger.

    `saw_claim_id` is the newest claim the page showed. When it is given
    and a newer claim has since arrived, the settlement is refused: the
    reviewer compared two pages and a third has appeared, and recording
    their judgment as covering it would settle a disagreement they were
    never shown.
    """
    settler = require_human_principal(principal)
    dependency = session.get(Dependency, dependency_id)
    if dependency is None:
        raise ValueError(f"dependency {dependency_id} does not exist")
    lock_project(session, dependency.project_id)
    session.refresh(dependency)
    if dependency.dismissed_at is not None:
        raise DisputeRefusal(
            f"{dependency.ref_code} was dismissed — a Dispute on a record "
            "nobody is working needs no verdict"
        )

    # The raw disagreement, ignoring settlements: a mistaken settlement
    # is corrected by settling again (ADR-0031), so "already settled"
    # must not read as "nothing to settle". What still refuses is a field
    # the sources never disagreed about.
    if field_name not in _disagreeing_field_names(session, dependency_id):
        raise NoSuchDispute(
            f"{field_name!r} is not in dispute on this record — nothing to "
            "settle"
        )
    assessment = next(
        (
            item
            for item in history_assessments_for(session, dependency_id)
            if item.field_name == field_name
        ),
        None,
    )
    if assessment is not None and assessment.outcome == "contractual_amendment":
        raise NoSuchDispute(
            f"{field_name!r} is a stale executed-agreement mismatch — "
            "the amendment follow-up stays open instead of becoming a pick-one verdict"
        )
    source_dispute = next(
        (
            dispute
            for dispute in disputes_for(session, dependency_id, include_settled=True)
            if dispute.field_name == field_name
        ),
        None,
    )
    if source_dispute is None:
        raise NoSuchDispute(
            f"{field_name!r} has a recorded physical-history outcome — it is not "
            "a human settlement"
        )

    newest = session.scalar(
        select(func.max(Assertion.id))
        .join(EvidenceLink, Assertion.evidence_link_id == EvidenceLink.id)
        .where(
            Assertion.dependency_id == dependency_id,
            Assertion.field_name == field_name,
            EvidenceLink.verified.is_(True),
            *claim_predicates(),
        )
    )
    if saw_claim_id is not None and newest is not None and newest > saw_claim_id:
        raise DisputeMovedOn(
            "another revision stated this field while you were reading — "
            "look again before settling it"
        )
    settlement = DisputeSettlement(
        dependency_id=dependency_id,
        field_name=field_name,
        settled_value=value,
        settled_by=settler.subject,
        covers_assertion_id=newest,
    )
    session.add(settlement)
    session.flush([settlement])
    record_dispute_settlement_case(
        session,
        dependency,
        settlement,
        claims=source_dispute.claims,
    )

    # Where the record carries a column for the field, the conclusion is
    # projected onto it — the same shape the Committed Date projection
    # takes. Fields with no column live on in the settlement alone.
    #
    # `title` is NOT NULL, and settling a field as saying nothing is a
    # legitimate conclusion, so the two meet at a 500 unless the
    # projection declines. The settlement still records the conclusion;
    # only the denormalized column keeps its last non-null value, which
    # is what a column that cannot be empty means.
    if field_name in _PROJECTED_FIELDS and not (
        value is None and field_name in _NOT_NULL_COLUMNS
    ):
        setattr(dependency, field_name, value)

    # The settlement row already is the conclusion, with its field, its value
    # and the claim it covers. Restating them here was a second copy free to
    # drift from it, so the entry names the settlement and a reader derives
    # the readable before/after from it (#604).
    audit.record(
        session,
        principal=settler,
        action=audit.SETTLE_DISPUTE,
        entity_type=audit.DEPENDENCY,
        entity_id=dependency_id,
        decided_by=audit.DecisionIdentity(
            kind=audit.DISPUTE_SETTLEMENT, identity=settlement.id
        ),
    )
    session.flush()
    _record_discrepancy_resolution_on_spine(
        session,
        dependency=dependency,
        field_name=field_name,
        value=value,
        settlement=settlement,
        recorder=settler,
    )
    return settlement


def _record_discrepancy_resolution_on_spine(
    session: Session,
    *,
    dependency: Dependency,
    field_name: str,
    value: str | None,
    settlement: DisputeSettlement,
    recorder: HumanPrincipal,
) -> None:
    """Dual-write the settlement as a resolve_discrepancy spine decision.

    The resolved field belongs to the record's source-row lineage: a settled
    value that selects an existing fact of the matching type re-decides that
    fact, superseding the current decision (ADR-0074 stage 3). Forward-only,
    never guessed: a field with no matching released fact type, a record whose
    current spine state is absent or ambiguous, a settled-as-nothing value, or
    a synthesized value no observed fact carries all stay legacy-only until
    the schema admits document-less human facts of these types (#458).
    """

    contract = FACT_TYPE_CONTRACTS.get(field_name)
    if contract is None or value is None:
        return
    current = session.execute(
        select(FactDecision, Fact)
        .join(Fact, Fact.id == FactDecision.fact_id)
        .join(
            ExtractedProposal,
            (ExtractedProposal.project_id == Fact.project_id)
            & (ExtractedProposal.document_id == Fact.document_id)
            & (ExtractedProposal.extraction_run_id == Fact.extraction_run_id)
            & (ExtractedProposal.subject_key == Fact.subject_key),
        )
        .join(Candidate, Candidate.id == ExtractedProposal.candidate_id)
        .where(
            Candidate.merged_into == dependency.id,
            FactDecision.project_id == dependency.project_id,
            FactDecision.fact_type == field_name,
            FactDecision.superseded_by.is_(None),
            FactDecision.disposition == "include",
        )
    ).all()
    if len(current) != 1:
        return
    decision, fact = current[0]
    target = _matching_field_fact(session, fact, field_name, value)
    if target is None:
        return
    record_human_fact_decision(
        session,
        target,
        principal=recorder,
        command_type="resolve_discrepancy",
        disposition="include",
        idempotency_key=f"resolve-discrepancy:{settlement.id}",
        expected_predecessor=decision.id,
    )


def _matching_field_fact(
    session: Session, current: Fact, field_name: str, value: str
) -> Fact | None:
    """The observed fact on the row's lineage that carries the settled value."""

    if field_name in _DATE_SPINE_FIELDS:
        try:
            settled: object = date.fromisoformat(value)
        except ValueError:
            return None
        column = Fact.date_value
    else:
        settled = value
        column = Fact.text_value
    return session.scalar(
        select(Fact)
        .where(
            Fact.project_id == current.project_id,
            Fact.subject_key == current.subject_key,
            Fact.fact_type == field_name,
            column == settled,
        )
        .order_by(Fact.id.desc())
        .limit(1)
    )


_DATE_SPINE_FIELDS = frozenset({"committed_date", "action_due_date", "need_date"})


# The Dependency columns a settled field writes through to. Named rather
# than derived from `hasattr`, so settling `notes` can never silently
# overwrite an unrelated attribute that happens to share a name.
_PROJECTED_FIELDS = frozenset(
    {"station_from", "station_to", "title", "location_desc"}
)

# Of those, the ones the schema refuses to leave empty.
_NOT_NULL_COLUMNS = frozenset({"title"})
