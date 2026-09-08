"""One adopted project's accepted record, and everything standing behind it (#642).

A coordinator asking "what does this Utility Conflict say today, what did it
say in July, and who changed it" had nowhere to go.  #536's ordered week is a
*work* surface — it answers what needs deciding — and the source register lists
deliveries, not values.  Between them the accepted record itself, the Source
Facts it was captured from, the Support Assessments weighed for it, the
Proposed Deltas raised against it, the packet acts that settled them, the
releases that carried it out of Corridor, and the audit trail were reachable
only by reading five tables by hand.

**It reads and never writes.**  Every reading here is a projection of records
that already exist, and the module holds no command: there is no way to accept,
resolve, correct, refuse, or authorize anything from this reading, and there is
no receipt for having looked.  Each of those acts already has exactly one home
— the review screen (#527, #528), Resolve Delta (#519), the packet transaction
(#526), release (#533) — and a second door is how two paths drift apart.  The
consuming screen links back to the one home for each act and offers none of
them itself.

**Nothing is derived twice.**  The current and as-of values come from
``record_projection.read_current_project_record`` and
``read_project_record_as_of_revision``, which are the spine's own ending
(ADR-0075); the Support Assessments come from ``support_assessments``; the
release history comes from ``report_release.external_report_release_history``.
This module joins those readings to their sources and standings; it re-derives
none of them, and it caches nothing.

**No clock.**  A revision, not a wall-clock instant, selects the as-of reading.
The caller may pass a revision the project does not hold; the reading says so
rather than silently answering about the current record.

**What the audit section can honestly show.**  ``audit_log`` is entity-scoped —
a Constraint, a Candidate, a Key Date, a project, a document — and carries no
project column, so a project's own entries are those recorded against the
project itself and against the source documents registered to it.  For an
adopted project that is deliberately a small trail: the record's own authority
history is the Project Record revision timeline, which is read here in full and
is the thing that answers "who changed it".  Inventing a wider project scope by
guessing which Constraint ids belong to the project would be a second, weaker
copy of a join the schema does not support.

Terminology: nothing here coins a word.  Utility Conflict, Proposed Delta,
Source Fact, Support Assessment, Project Record, Follow-up Plan, Apply, Keep
current, Needs coordination and Defer are already adopted, and the outcome
words are exactly the ones the review screen prints.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.models import (
    AuditLog,
    BaselineSourceRow,
    DeltaDeferral,
    DeltaDisposition,
    DeltaRecordDecision,
    DeltaReviewPacketChild,
    DeltaReviewPacketReceipt,
    DeltaReviewPacketReversal,
    DeltaSupersession,
    Document,
    Fact,
    FactSource,
    ProjectRecordRevision,
    ProposedDelta,
    SourceSegment,
    SupportAssessment,
    SupportAssessmentSource,
)
from corridor.presentation import field_label
from corridor.record_projection import (
    CurrentRecordValue,
    read_current_project_record,
    read_project_record_as_of_revision,
)
from corridor.report_release import (
    ExternalReportReleaseHistory,
    external_report_release_history,
)
from corridor.prose_spans import is_prose_segment


# How many accepted values are shown with their full evidence at once. The
# search narrows the set; this only stops an unfiltered project rendering every
# fact, segment and assessment it holds into one page.
VALUE_LIMIT = 60

# How far back the project-scoped audit trail is read in one page.
AUDIT_LIMIT = 100

# The words each standing prints. They are the review screen's own outcome
# words (`review.html`), so one decision reads the same on both screens.
OPEN = "Waiting for a decision"
APPLIED = "Applied"
EDITED_AND_APPLIED = "Edited and applied"
KEPT_CURRENT = "Keep current"
SUPERSEDED = "Superseded by a later source revision"
DEFERRED = "Deferred"

# `delta_dispositions.disposition` is the stored vocabulary; these are the
# words for the same three states.
_DISPOSITION_WORDS: Mapping[str, str] = {
    "accept": APPLIED,
    "edit": EDITED_AND_APPLIED,
    "reject": KEPT_CURRENT,
}


class UnknownRevision(LookupError):
    """The revision named for the as-of reading is not this project's."""


@dataclass(frozen=True, slots=True)
class SearchTerms:
    """What the coordinator asked to be shown, as they typed it.

    Four facets, each matched case-insensitively as a substring of what the
    record already holds. An empty facet matches everything, so the unfiltered
    reading is the same code path as a filtered one.
    """

    conflict: str = ""
    source: str = ""
    field: str = ""
    revision: int | None = None

    @property
    def any_term(self) -> bool:
        return bool(
            self.conflict.strip()
            or self.source.strip()
            or self.field.strip()
            or self.revision is not None
        )


@dataclass(frozen=True, slots=True)
class SourceReference:
    """One exact passage a value was captured from, named in words."""

    document_id: int | None
    filename: str
    locator: str
    exact_text: str
    role: str


@dataclass(frozen=True, slots=True)
class AssessmentReading:
    """One attributable judgment that segments support the value (ADR-0082)."""

    assessment_id: int
    evidence_role: str
    assessment: str
    authority: str
    assessed_at: datetime
    effective: bool
    segments: tuple[SourceReference, ...]


@dataclass(frozen=True, slots=True)
class FactReading:
    """The Source Fact one accepted value was made effective from."""

    fact_id: int
    fact_type: str
    recorded_by: str
    recorded_at: datetime
    sources: tuple[SourceReference, ...]
    assessments: tuple[AssessmentReading, ...]


@dataclass(frozen=True, slots=True)
class ValueReading:
    """One accepted value, current or as of a revision, with what backs it."""

    subject_key: str
    subject_name: str
    field_key: str
    field_name: str
    value_text: str | None
    revision_id: int
    decision_id: int
    fact: FactReading | None

    @property
    def sources(self) -> tuple[SourceReference, ...]:
        return self.fact.sources if self.fact else ()

    @property
    def assessments(self) -> tuple[AssessmentReading, ...]:
        return self.fact.assessments if self.fact else ()


@dataclass(frozen=True, slots=True)
class ValueComparison:
    """One field of one Utility Conflict, now and as of the chosen revision."""

    subject_key: str
    subject_name: str
    field_key: str
    field_name: str
    current: ValueReading | None
    as_of: ValueReading | None

    @property
    def changed(self) -> bool:
        """Whether the two readings differ at all, including appearing at all."""

        if self.current is None or self.as_of is None:
            return self.current is not self.as_of
        return self.current.value_text != self.as_of.value_text


@dataclass(frozen=True, slots=True)
class PacketReading:
    """One guided packet act that named this Proposed Delta (#526)."""

    receipt_id: int
    outcome: str
    decided_by_principal: str
    decided_at: datetime
    revision_id: int | None
    reversed_at: datetime | None

    @property
    def undone(self) -> bool:
        return self.reversed_at is not None


@dataclass(frozen=True, slots=True)
class DeltaReading:
    """One Proposed Delta, its standing, and the act that settled it."""

    delta_id: int
    subject_key: str
    subject_name: str
    field_key: str
    field_name: str
    change_type: str
    accepted_value_text: str | None
    proposed_value_text: str | None
    source_family: str
    source_revision: str
    raised_at: datetime
    standing: str
    decided_by: str | None
    decided_at: datetime | None
    revision_id: int | None
    rationale: str | None
    superseded_by_delta_id: int | None
    deferred_until: date | None
    deferral_reason: str | None
    packets: tuple[PacketReading, ...]

    @property
    def settled(self) -> bool:
        return self.standing != OPEN


@dataclass(frozen=True, slots=True)
class RevisionReading:
    """One atomic Project Record revision and the authority behind it."""

    revision_id: int
    command_type: str
    authority: str
    recorded_at: datetime
    predecessor_revision_id: int | None


@dataclass(frozen=True, slots=True)
class AuditReading:
    """One project-scoped audit entry, named rather than replayed."""

    entry_id: int
    action: str
    entity_type: str
    entity_id: int
    actor: str
    human_principal: str | None
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class RecordHistory:
    """One read-only investigation of one adopted project's record."""

    project_id: int
    terms: SearchTerms
    as_of_revision_id: int | None
    current_revision_id: int | None
    revisions: tuple[RevisionReading, ...]
    values: tuple[ValueComparison, ...]
    values_total: int
    deltas: tuple[DeltaReading, ...]
    deltas_total: int
    releases: tuple[ExternalReportReleaseHistory, ...]
    audit: tuple[AuditReading, ...]

    @property
    def truncated(self) -> bool:
        return self.values_total > len(self.values)

    @property
    def revision_changes(self) -> tuple[DeltaReading, ...]:
        """What the selected revision itself settled, in the order it settled."""

        if self.as_of_revision_id is None:
            return ()
        return tuple(
            delta
            for delta in self.deltas
            if delta.revision_id == self.as_of_revision_id
        )


def read_record_history(
    session: Session,
    *,
    project_id: int,
    terms: SearchTerms | None = None,
    value_limit: int = VALUE_LIMIT,
) -> RecordHistory:
    """Assemble one project's record reading from the records that exist.

    ``terms.revision`` selects the as-of reading and is refused when it is not
    this project's, so a mistyped revision is answered rather than silently
    treated as "current".
    """

    terms = terms or SearchTerms()
    revisions = read_revisions(session, project_id=project_id)
    known = {revision.revision_id for revision in revisions}
    as_of = terms.revision
    if as_of is not None and as_of not in known:
        raise UnknownRevision(f"revision {as_of} does not belong to this project")

    names = subject_names(session, project_id=project_id)
    current = read_current_project_record(session, project_id)
    historic = (
        read_project_record_as_of_revision(session, project_id, as_of)
        if as_of is not None
        else ()
    )
    comparisons = _compare(session, project_id, current, historic, names, terms)
    deltas = _delta_history(session, project_id, names, terms)
    return RecordHistory(
        project_id=project_id,
        terms=terms,
        as_of_revision_id=as_of,
        current_revision_id=revisions[-1].revision_id if revisions else None,
        revisions=revisions,
        values=comparisons[:value_limit],
        values_total=len(comparisons),
        deltas=deltas,
        deltas_total=len(deltas),
        releases=external_report_release_history(session, project_id),
        audit=read_project_audit(session, project_id=project_id),
        )


# --- the revision timeline -------------------------------------------------


def read_revisions(
    session: Session, *, project_id: int
) -> tuple[RevisionReading, ...]:
    """Every Project Record revision this project holds, oldest first."""

    return tuple(
        RevisionReading(
            revision_id=row.id,
            command_type=row.command_type,
            authority=row.human_principal or row.released_policy or "",
            recorded_at=row.recorded_at,
            predecessor_revision_id=row.predecessor_revision_id,
        )
        for row in session.scalars(
            select(ProjectRecordRevision)
            .where(ProjectRecordRevision.project_id == project_id)
            .order_by(ProjectRecordRevision.id)
        )
    )


# --- the customer's own names ----------------------------------------------


def subject_names(session: Session, *, project_id: int) -> dict[str, str]:
    """Each record subject in the customer's own words, where they exist."""

    found: dict[str, str] = {}
    for row in session.scalars(
        select(BaselineSourceRow).where(BaselineSourceRow.project_id == project_id)
    ):
        if not row.record_subject_key:
            continue
        where = f"{row.sheet_name} row {row.row_number}"
        found[row.record_subject_key] = (
            f"{row.business_identity} ({where})" if row.business_identity else where
        )
    return found


# --- accepted values, now and then -----------------------------------------


def _compare(
    session: Session,
    project_id: int,
    current: Sequence[CurrentRecordValue],
    historic: Sequence[CurrentRecordValue],
    names: Mapping[str, str],
    terms: SearchTerms,
) -> tuple[ValueComparison, ...]:
    keys = sorted(
        {(value.subject_key, value.fact_type) for value in (*current, *historic)}
    )
    evidence = _fact_readings(
        session,
        project_id,
        tuple({value.fact_id for value in (*current, *historic)}),
    )
    by_current = {(v.subject_key, v.fact_type): v for v in current}
    by_historic = {(v.subject_key, v.fact_type): v for v in historic}
    comparisons: list[ValueComparison] = []
    for subject_key, fact_type in keys:
        name = names.get(subject_key, subject_key)
        if not _matches_value(subject_key, name, fact_type, terms, evidence, by_current, by_historic):
            continue
        comparisons.append(
            ValueComparison(
                subject_key=subject_key,
                subject_name=name,
                field_key=fact_type,
                field_name=field_label(fact_type),
                current=_value_reading(
                    by_current.get((subject_key, fact_type)), name, evidence
                ),
                as_of=_value_reading(
                    by_historic.get((subject_key, fact_type)), name, evidence
                ),
            )
        )
    return tuple(comparisons)


def _value_reading(
    value: CurrentRecordValue | None,
    subject_name: str,
    evidence: Mapping[int, FactReading],
) -> ValueReading | None:
    if value is None:
        return None
    return ValueReading(
        subject_key=value.subject_key,
        subject_name=subject_name,
        field_key=value.fact_type,
        field_name=field_label(value.fact_type),
        value_text=_value_text(value),
        revision_id=value.revision_id,
        decision_id=value.decision_id,
        fact=evidence.get(value.fact_id),
    )


def _value_text(value: CurrentRecordValue) -> str | None:
    """The effective value as one readable string, never a reconstruction."""

    if value.text_value is not None:
        return value.text_value
    if value.date_value is not None:
        return value.date_value.isoformat()
    if value.date_range_start is not None or value.date_range_end is not None:
        start = value.date_range_start.isoformat() if value.date_range_start else "?"
        end = value.date_range_end.isoformat() if value.date_range_end else "?"
        return f"{start} to {end}"
    if value.external_org_value_id is not None:
        return f"organization {value.external_org_value_id}"
    if value.document_value_id is not None:
        return f"document {value.document_value_id}"
    return None


# --- the Source Facts behind a value, and their sources ---------------------


def _fact_readings(
    session: Session, project_id: int, fact_ids: tuple[int, ...]
) -> dict[int, FactReading]:
    if not fact_ids:
        return {}
    facts = {
        fact.id: fact
        for fact in session.scalars(select(Fact).where(Fact.id.in_(fact_ids)))
    }
    sources = _fact_sources(session, fact_ids)
    assessments = _assessments(session, project_id, fact_ids)
    return {
        fact_id: FactReading(
            fact_id=fact_id,
            fact_type=fact.fact_type,
            recorded_by=fact.recorded_by,
            recorded_at=fact.recorded_at,
            sources=tuple(sources.get(fact_id, ())),
            assessments=tuple(assessments.get(fact_id, ())),
        )
        for fact_id, fact in facts.items()
    }


def _fact_sources(
    session: Session, fact_ids: tuple[int, ...]
) -> dict[int, list[SourceReference]]:
    rows = session.execute(
        select(FactSource.fact_id, FactSource.role, SourceSegment, Document.filename)
        .join(SourceSegment, SourceSegment.id == FactSource.source_segment_id)
        .outerjoin(Document, Document.id == SourceSegment.document_id)
        .where(FactSource.fact_id.in_(fact_ids))
        .order_by(FactSource.fact_id, FactSource.role, FactSource.ordinal)
    ).all()
    found: dict[int, list[SourceReference]] = {}
    for fact_id, role, segment, filename in rows:
        found.setdefault(fact_id, []).append(
            _reference(segment, filename, role)
        )
    return found


def _assessments(
    session: Session, project_id: int, fact_ids: tuple[int, ...]
) -> dict[int, list[AssessmentReading]]:
    rows = session.scalars(
        select(SupportAssessment)
        .where(
            SupportAssessment.project_id == project_id,
            SupportAssessment.proposition_kind == "source_fact",
            SupportAssessment.fact_id.in_(fact_ids),
        )
        .order_by(
            SupportAssessment.fact_id,
            SupportAssessment.evidence_role,
            SupportAssessment.assessed_at,
            SupportAssessment.id,
        )
    ).all()
    if not rows:
        return {}
    segments = _assessment_segments(session, tuple(row.id for row in rows))
    found: dict[int, list[AssessmentReading]] = {}
    for row in rows:
        found.setdefault(int(row.fact_id), []).append(
            AssessmentReading(
                assessment_id=row.id,
                evidence_role=row.evidence_role,
                assessment=row.assessment,
                authority=row.human_principal or row.released_policy or "",
                assessed_at=row.assessed_at,
                effective=row.superseded_by is None,
                segments=tuple(segments.get(row.id, ())),
            )
        )
    return found


def _assessment_segments(
    session: Session, assessment_ids: tuple[int, ...]
) -> dict[int, list[SourceReference]]:
    rows = session.execute(
        select(
            SupportAssessmentSource.support_assessment_id,
            SourceSegment,
            Document.filename,
        )
        .join(
            SourceSegment,
            SourceSegment.id == SupportAssessmentSource.source_segment_id,
        )
        .outerjoin(Document, Document.id == SourceSegment.document_id)
        .where(SupportAssessmentSource.support_assessment_id.in_(assessment_ids))
        .order_by(
            SupportAssessmentSource.support_assessment_id,
            SupportAssessmentSource.ordinal,
        )
    ).all()
    found: dict[int, list[SourceReference]] = {}
    for assessment_id, segment, filename in rows:
        found.setdefault(assessment_id, []).append(
            _reference(segment, filename, "assessed")
        )
    return found


def _reference(
    segment: SourceSegment, filename: str | None, role: str
) -> SourceReference:
    return SourceReference(
        document_id=segment.document_id,
        filename=filename or "recorded verbal statement",
        locator=_locator(segment),
        exact_text=segment.exact_text,
        role=role,
    )


def _locator(segment: SourceSegment) -> str:
    """Where the passage is, spelled out rather than left as a typed shape."""

    if segment.kind == "spreadsheet_cell":
        return f"sheet {segment.sheet_name}, cell {segment.cell_range}"
    if is_prose_segment(segment):
        return (
            f"page {segment.page_no}, "
            f"characters {segment.start_offset}–{segment.end_offset}"
        )
    return "recorded verbal statement"


# --- Proposed Delta and resolution history ---------------------------------


def _delta_history(
    session: Session,
    project_id: int,
    names: Mapping[str, str],
    terms: SearchTerms,
) -> tuple[DeltaReading, ...]:
    deltas = tuple(
        session.scalars(
            select(ProposedDelta)
            .where(ProposedDelta.project_id == project_id)
            .order_by(ProposedDelta.id)
        )
    )
    if not deltas:
        return ()
    delta_ids = tuple(delta.id for delta in deltas)
    dispositions = {
        row.delta_id: row
        for row in session.scalars(
            select(DeltaDisposition).where(
                DeltaDisposition.project_id == project_id,
                DeltaDisposition.delta_id.in_(delta_ids),
            )
        )
    }
    decisions = {
        row.delta_id: row
        for row in session.scalars(
            select(DeltaRecordDecision).where(
                DeltaRecordDecision.project_id == project_id,
                DeltaRecordDecision.delta_id.in_(delta_ids),
            )
        )
    }
    superseded = {
        row.prior_delta_id: row.superseding_delta_id
        for row in session.scalars(
            select(DeltaSupersession).where(
                DeltaSupersession.project_id == project_id,
                DeltaSupersession.prior_delta_id.in_(delta_ids),
            )
        )
    }
    deferrals: dict[int, DeltaDeferral] = {}
    for row in session.scalars(
        select(DeltaDeferral)
        .where(
            DeltaDeferral.project_id == project_id,
            DeltaDeferral.delta_id.in_(delta_ids),
        )
        .order_by(DeltaDeferral.id)
    ):
        deferrals[int(row.delta_id)] = row
    packets = _packets(session, project_id, delta_ids)

    readings: list[DeltaReading] = []
    for delta in deltas:
        name = names.get(delta.target_subject_identity, delta.target_subject_identity)
        if not _matches_delta(delta, name, terms):
            continue
        disposition = dispositions.get(delta.id)
        decision = decisions.get(delta.id)
        deferral = deferrals.get(delta.id)
        readings.append(
            DeltaReading(
                delta_id=delta.id,
                subject_key=delta.target_subject_identity,
                subject_name=name,
                field_key=delta.target_field or "",
                field_name=(
                    field_label(delta.target_field)
                    if delta.target_field
                    else "the whole record row"
                ),
                change_type=delta.change_type,
                accepted_value_text=_json_text(delta.accepted_value),
                proposed_value_text=_json_text(delta.proposed_value),
                source_family=delta.source_family,
                source_revision=delta.source_revision,
                raised_at=delta.created_at,
                standing=_standing(disposition, delta.id in superseded, deferral),
                decided_by=(
                    (disposition.decided_by_principal or disposition.decided_by_policy)
                    if disposition
                    else None
                ),
                decided_at=disposition.decided_at if disposition else None,
                revision_id=decision.revision_id if decision else None,
                rationale=disposition.rationale if disposition else None,
                superseded_by_delta_id=superseded.get(delta.id),
                deferred_until=(
                    deferral.deferred_until.date()
                    if deferral is not None and deferral.deferred_until is not None
                    else None
                ),
                deferral_reason=deferral.reason if deferral is not None else None,
                packets=tuple(packets.get(delta.id, ())),
            )
        )
    return tuple(readings)


def _standing(
    disposition: DeltaDisposition | None,
    superseded: bool,
    deferral: DeltaDeferral | None,
) -> str:
    """One word for where this Proposed Delta stands, resolution first.

    A resolved delta keeps the words of its resolution even when a later source
    revision arrived afterwards, because the decision is what happened to the
    record; supersession only describes a delta nobody decided.
    """

    if disposition is not None:
        return _DISPOSITION_WORDS.get(disposition.disposition, disposition.disposition)
    if superseded:
        return SUPERSEDED
    if deferral is not None:
        return DEFERRED
    return OPEN


def _packets(
    session: Session, project_id: int, delta_ids: tuple[int, ...]
) -> dict[int, list[PacketReading]]:
    rows = session.execute(
        select(DeltaReviewPacketChild, DeltaReviewPacketReceipt, DeltaReviewPacketReversal)
        .join(
            DeltaReviewPacketReceipt,
            DeltaReviewPacketReceipt.id == DeltaReviewPacketChild.receipt_id,
        )
        .outerjoin(
            DeltaReviewPacketReversal,
            DeltaReviewPacketReversal.receipt_id == DeltaReviewPacketChild.receipt_id,
        )
        .where(
            DeltaReviewPacketChild.project_id == project_id,
            DeltaReviewPacketChild.delta_id.in_(delta_ids),
        )
        .order_by(DeltaReviewPacketChild.receipt_id, DeltaReviewPacketChild.ordinal)
    ).all()
    found: dict[int, list[PacketReading]] = {}
    for child, receipt, reversal in rows:
        found.setdefault(int(child.delta_id), []).append(
            PacketReading(
                receipt_id=receipt.id,
                outcome=child.outcome,
                decided_by_principal=receipt.decided_by_principal,
                decided_at=receipt.decided_at,
                revision_id=receipt.revision_id,
                reversed_at=reversal.reversed_at if reversal is not None else None,
            )
        )
    return found


def _json_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return ", ".join(f"{key}: {item}" for key, item in sorted(value.items()))
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return str(value)


# --- the project-scoped audit trail ----------------------------------------


def read_project_audit(
    session: Session, *, project_id: int, limit: int = AUDIT_LIMIT
) -> tuple[AuditReading, ...]:
    """The audit entries this project owns, most recent first.

    ``audit_log`` is entity-scoped and has no project column, so the entries a
    project owns are the ones recorded against the project itself and against
    the source documents registered to it. Nothing else is claimed for it.
    """

    document_ids = tuple(
        session.scalars(
            select(Document.id).where(Document.project_id == project_id)
        ).all()
    )
    scope = (AuditLog.entity_type == "project") & (AuditLog.entity_id == project_id)
    if document_ids:
        scope = scope | (
            (AuditLog.entity_type == "document")
            & (AuditLog.entity_id.in_(document_ids))
        )
    return tuple(
        AuditReading(
            entry_id=row.id,
            action=row.action,
            entity_type=row.entity_type,
            entity_id=row.entity_id,
            actor=row.actor,
            human_principal=row.human_principal,
            recorded_at=row.ts,
        )
        for row in session.scalars(
            select(AuditLog).where(scope).order_by(AuditLog.id.desc()).limit(limit)
        )
    )


# --- search ----------------------------------------------------------------


def _contains(term: str, *haystack: str | None) -> bool:
    needle = term.strip().casefold()
    if not needle:
        return True
    return any(needle in (value or "").casefold() for value in haystack)


def _matches_value(
    subject_key: str,
    subject_name: str,
    fact_type: str,
    terms: SearchTerms,
    evidence: Mapping[int, FactReading],
    by_current: Mapping[tuple[str, str], CurrentRecordValue],
    by_historic: Mapping[tuple[str, str], CurrentRecordValue],
) -> bool:
    if not _contains(terms.conflict, subject_key, subject_name):
        return False
    if not _contains(terms.field, fact_type, field_label(fact_type)):
        return False
    if terms.source.strip():
        filenames: list[str | None] = []
        for table in (by_current, by_historic):
            value = table.get((subject_key, fact_type))
            if value is None:
                continue
            reading = evidence.get(value.fact_id)
            if reading is None:
                continue
            filenames.extend(source.filename for source in reading.sources)
        if not _contains(terms.source, *filenames):
            return False
    return True


def _matches_delta(
    delta: ProposedDelta, subject_name: str, terms: SearchTerms
) -> bool:
    if not _contains(terms.conflict, delta.target_subject_identity, subject_name):
        return False
    if not _contains(
        terms.field,
        delta.target_field,
        field_label(delta.target_field) if delta.target_field else None,
    ):
        return False
    if not _contains(terms.source, delta.source_family, delta.source_revision):
        return False
    return True


def readable_terms(
    *,
    conflict: str = "",
    source: str = "",
    field: str = "",
    revision: str = "",
) -> SearchTerms:
    """Build search terms from what a query string actually carried.

    A revision that is not a whole number is treated as no revision at all: the
    caller typed something the record cannot hold, and answering about the
    current record is what the unfiltered reading already does.
    """

    try:
        chosen: int | None = int(revision.strip()) if revision.strip() else None
    except ValueError:
        chosen = None
    return SearchTerms(
        conflict=conflict.strip(),
        source=source.strip(),
        field=field.strip(),
        revision=chosen,
    )
