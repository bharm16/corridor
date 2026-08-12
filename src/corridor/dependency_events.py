"""Read current structured statements into the legacy Dependency view.

The old event table stored one Dependency and one scalar date, so every reader
invented its own answer after a party-level statement or a month-only source.
Structured events are authoritative; this module gives compatibility readers
the one exact-day projection they can represent honestly.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType

from sqlalchemy import exists, false as sa_false, select
from sqlalchemy.orm import aliased
from sqlalchemy.orm import Session

from corridor.models import (
    Dependency,
    DependencyEvent,
    DependencyEventEvidence,
    DependencyEventScope,
    DependencyEventScopeDecision,
    DependencyEventTiming,
    Document,
    EvidenceLink,
)


COMMITTED_EVENT_TYPES = ("commitment", "committed_date_change")


def current_scope_decision_filter():
    """SQL predicate for the one scope decision not replaced by a later act."""
    superseding = aliased(DependencyEventScopeDecision)
    return ~exists(
        select(superseding.id).where(
            superseding.supersedes_scope_decision_id
            == DependencyEventScopeDecision.id
        )
    )


@dataclass(frozen=True)
class StatementEvidenceMembership:
    """One Evidence identity visible through one current statement scope link."""

    dependency_id: int
    event: DependencyEvent
    scope_link_id: int
    evidence_link: EvidenceLink
    document: Document

    @property
    def event_id(self) -> int:
        return self.event.id


@dataclass(frozen=True)
class CurrentStatementEvidenceMemberships:
    """Current statement Evidence membership without exposing its join graph."""

    members: tuple[StatementEvidenceMembership, ...]

    def for_dependency(
        self, dependency_id: int
    ) -> tuple[StatementEvidenceMembership, ...]:
        return tuple(
            member
            for member in self.members
            if member.dependency_id == dependency_id
        )

    def contains(self, dependency_id: int, evidence_link_id: int) -> bool:
        return self.find(dependency_id, evidence_link_id) is not None

    def find(
        self, dependency_id: int, evidence_link_id: int
    ) -> StatementEvidenceMembership | None:
        return next(
            (
                member
                for member in self.members
                if member.dependency_id == dependency_id
                and member.evidence_link.id == evidence_link_id
            ),
            None,
        )

    @property
    def counts(self) -> dict[int, int]:
        counts: dict[int, int] = {}
        for member in self.members:
            counts[member.dependency_id] = counts.get(member.dependency_id, 0) + 1
        return counts


def current_statement_evidence_memberships(
    session: Session, dependency_ids: Iterable[int]
) -> CurrentStatementEvidenceMemberships:
    """Return Event Evidence visible through each Dependency's current scope."""
    ids = tuple(dict.fromkeys(dependency_ids))
    if not ids:
        return CurrentStatementEvidenceMemberships(())
    rows = session.execute(
        select(
            DependencyEventScope.dependency_id,
            DependencyEvent,
            DependencyEventScope.id,
            EvidenceLink,
            Document,
        )
        .join(
            DependencyEventScopeDecision,
            DependencyEventScope.scope_decision_id
            == DependencyEventScopeDecision.id,
        )
        .join(
            DependencyEventEvidence,
            DependencyEventEvidence.event_id == DependencyEventScope.event_id,
        )
        .join(
            DependencyEvent,
            DependencyEvent.id == DependencyEventEvidence.event_id,
        )
        .join(
            EvidenceLink,
            EvidenceLink.id == DependencyEventEvidence.evidence_link_id,
        )
        .join(Document, Document.id == EvidenceLink.document_id)
        .where(
            DependencyEventScope.dependency_id.in_(ids),
            current_scope_decision_filter(),
        )
        .order_by(DependencyEventScope.dependency_id, EvidenceLink.id)
    ).all()
    return CurrentStatementEvidenceMemberships(
        tuple(
            StatementEvidenceMembership(
                dependency_id,
                event,
                scope_link_id,
                evidence_link,
                document,
            )
            for dependency_id, event, scope_link_id, evidence_link, document in rows
        )
    )


@dataclass(frozen=True)
class CurrentDependencyStatement:
    """The one statement compatibility readers can safely expose.

    The event and exact effective date preserve the authoritative structured
    record.  The source class and closure state travel beside them so legacy
    readers do not reimplement either scope traversal or event selection.
    """

    event: DependencyEvent | None
    effective_date: date | None
    provenance_class: str | None
    is_closed: bool


@dataclass(frozen=True)
class CitedStatementProvenance:
    """The verified page citation that makes one cited statement publishable."""

    document_id: int
    filename: str
    page_no: int
    quote: str


@dataclass(frozen=True)
class PublishedDependencyStatement:
    """The statement one publisher may expose for one Dependency."""

    current_event: DependencyEvent | None
    event: DependencyEvent | None
    committed_date: date | None
    source_attribution: str | None
    cited_provenance: CitedStatementProvenance | None
    is_closed: bool
    unsupported_current: bool


@dataclass(frozen=True)
class StatementPublication:
    """One provenance-consistent statement reading for a set of Dependencies."""

    project_id: int
    by_dependency: Mapping[int, PublishedDependencyStatement]
    document_only: bool = False

    def __post_init__(self) -> None:
        """Keep the paired publication from being changed after it is read."""
        object.__setattr__(self, "by_dependency", MappingProxyType(dict(self.by_dependency)))

    @property
    def committed_dates(self) -> dict[int, date | None]:
        return {
            dependency_id: statement.committed_date
            for dependency_id, statement in self.by_dependency.items()
        }

    @property
    def committed_events(self) -> dict[int, DependencyEvent]:
        return {
            dependency_id: statement.event
            for dependency_id, statement in self.by_dependency.items()
            if statement.event is not None and statement.committed_date is not None
        }

    @property
    def unsupported_dependency_ids(self) -> frozenset[int]:
        return frozenset(
            dependency_id
            for dependency_id, statement in self.by_dependency.items()
            if statement.unsupported_current
        )

    @property
    def fingerprint(self) -> "StatementPublicationFingerprint":
        """The event-and-audience identity an Evaluation must agree with."""
        return StatementPublicationFingerprint(
            document_only=self.document_only,
            statements=tuple(
                StatementPublicationEntryFingerprint(
                    dependency_id=dependency_id,
                    current_event_id=(
                        statement.current_event.id if statement.current_event else None
                    ),
                    published_event_id=(
                        statement.event.id if statement.event else None
                    ),
                    published_source_kind=(
                        statement.event.source_kind if statement.event else None
                    ),
                    committed_date=statement.committed_date,
                    unsupported_current=statement.unsupported_current,
                )
                for dependency_id, statement in sorted(self.by_dependency.items())
            ),
        )


@dataclass(frozen=True)
class StatementPublicationEntryFingerprint:
    """One Dependency's immutable identity inside a statement publication."""

    dependency_id: int
    current_event_id: int | None
    published_event_id: int | None
    published_source_kind: str | None
    committed_date: date | None
    unsupported_current: bool


@dataclass(frozen=True)
class StatementPublicationFingerprint:
    """The immutable statement identity and audience mode behind one reading."""

    document_only: bool
    statements: tuple[StatementPublicationEntryFingerprint, ...]


def current_dependency_statements(
    session: Session,
    dependency_ids: Iterable[int],
    *,
    source_kind: str | None = None,
    event_ids: Collection[int] | None = None,
) -> dict[int, CurrentDependencyStatement]:
    """Return the current exact-day statement and closure state per Dependency.

    The Dependency scalar is a materialized compatibility projection, never a
    fallback authority. A filtered read is event-only: selecting cited
    statements must not surface a Verbal-backed projection as cited.
    """
    ids = tuple(dict.fromkeys(dependency_ids))
    if not ids:
        return {}

    closed_ids = set(
        session.scalars(
            select(DependencyEventScope.dependency_id)
            .join(
                DependencyEventScopeDecision,
                DependencyEventScope.scope_decision_id
                == DependencyEventScopeDecision.id,
            )
            .join(
                DependencyEvent,
                DependencyEventScope.event_id == DependencyEvent.id,
            )
            .where(
                DependencyEventScope.dependency_id.in_(ids),
                current_scope_decision_filter(),
                DependencyEvent.event_type == "closure",
            )
        ).all()
    )

    query = (
        select(
            DependencyEventScope.dependency_id,
            DependencyEvent,
            DependencyEventTiming,
        )
        .join(
            DependencyEventScopeDecision,
            DependencyEventScope.scope_decision_id
            == DependencyEventScopeDecision.id,
        )
        .join(
            DependencyEvent,
            DependencyEventScope.event_id == DependencyEvent.id,
        )
        .join(
            DependencyEventTiming,
            DependencyEventTiming.event_id == DependencyEvent.id,
        )
        .where(
            DependencyEventScope.dependency_id.in_(ids),
            DependencyEvent.event_type.in_(COMMITTED_EVENT_TYPES),
            DependencyEventScopeDecision.scope_mode.in_(("selected", "all_active")),
            current_scope_decision_filter(),
            DependencyEventTiming.kind == "new",
        )
    )
    if source_kind is not None:
        query = query.where(DependencyEvent.source_kind == source_kind)
    if event_ids is not None:
        query = (
            query.where(DependencyEvent.id.in_(event_ids))
            if event_ids
            else query.where(sa_false())
        )

    current: dict[int, tuple[DependencyEvent, DependencyEventTiming]] = {}
    for dependency_id, event, timing in session.execute(
        query.order_by(
            DependencyEventScope.dependency_id,
            DependencyEvent.event_date.desc().nulls_last(),
            DependencyEvent.id.desc(),
        )
    ):
        current.setdefault(dependency_id, (event, timing))

    statements: dict[int, CurrentDependencyStatement] = {}
    for dependency_id in ids:
        current_event = current.get(dependency_id)
        effective_date = (
            current_event[1].start_date
            if current_event is not None and current_event[1].precision == "day"
            else None
        )
        statements[dependency_id] = CurrentDependencyStatement(
            event=current_event[0] if current_event else None,
            effective_date=effective_date,
            provenance_class=current_event[0].source_kind if current_event else None,
            is_closed=dependency_id in closed_ids,
        )
    return statements


def published_dependency_statements(
    session: Session,
    dependency_ids: Iterable[int],
    *,
    project_id: int,
    document_only: bool = False,
) -> StatementPublication:
    """Read statements a publisher may expose with their provenance attached."""
    ids = tuple(dict.fromkeys(dependency_ids))
    owned_ids = set(
        session.scalars(
            select(Dependency.id).where(
                Dependency.id.in_(ids), Dependency.project_id == project_id
            )
        ).all()
    )
    if owned_ids != set(ids):
        raise ValueError("statement publication contains another project's record")
    current = current_dependency_statements(session, ids)
    verified_cited_provenance = verified_cited_statement_provenance(session, ids)
    verified_cited_event_ids = set(verified_cited_provenance)
    cited_history = (
        current_dependency_statements(
            session,
            ids,
            source_kind="cited",
            event_ids=verified_cited_event_ids,
        )
        if document_only
        else {}
    )
    if document_only:
        published_by_dependency = {}
        for dependency_id, statement in current.items():
            event = statement.event
            if event is None:
                continue
            if event.source_kind == "verbal":
                # A document-only view intentionally excludes Verbal history,
                # so it may select the newest supported cited predecessor.
                cited = cited_history.get(dependency_id)
                if cited is not None and cited.effective_date is not None:
                    published_by_dependency[dependency_id] = cited
            elif (
                event.id in verified_cited_event_ids
                and statement.effective_date is not None
            ):
                # A current cited statement is authoritative. If its own
                # date or Evidence is unsupported, withhold rather than fall
                # back to stale cited history.
                published_by_dependency[dependency_id] = statement
    else:
        published_by_dependency = {
            dependency_id: statement
            for dependency_id, statement in current.items()
            if statement.event is None
            or statement.event.source_kind == "verbal"
            or statement.event.id in verified_cited_event_ids
        }
    by_dependency: dict[int, PublishedDependencyStatement] = {}
    for dependency_id in ids:
        current_statement = current[dependency_id]
        current_event = current_statement.event
        published = published_by_dependency.get(dependency_id)
        event = published.event if published is not None else None
        committed_date = published.effective_date if published is not None else None
        by_dependency[dependency_id] = PublishedDependencyStatement(
            current_event=current_event,
            event=event,
            committed_date=committed_date,
            source_attribution=(
                verbal_attribution(event) or "Cited statement"
                if event is not None and committed_date is not None
                else None
            ),
            cited_provenance=(
                verified_cited_provenance.get(event.id)
                if event is not None and committed_date is not None
                else None
            ),
            is_closed=current_statement.is_closed,
            unsupported_current=(
                current_event is not None
                and (published is None or event is None or committed_date is None)
            ),
        )
    return StatementPublication(project_id, by_dependency, document_only=document_only)


def verified_cited_statement_provenance(
    session: Session, dependency_ids: Iterable[int]
) -> dict[int, CitedStatementProvenance]:
    """The canonical verified page citation for each scoped cited statement."""
    ids = tuple(dict.fromkeys(dependency_ids))
    if not ids:
        return {}
    provenance: dict[int, CitedStatementProvenance] = {}
    memberships = current_statement_evidence_memberships(session, ids)
    for member in memberships.members:
        if member.event.source_kind != "cited" or not member.evidence_link.verified:
            continue
        link = member.evidence_link
        document = member.document
        provenance.setdefault(
            member.event_id,
            CitedStatementProvenance(
                document.id,
                document.filename,
                link.page_no,
                link.quote,
            ),
        )
    return provenance


def latest_committed_events(
    session: Session,
    dependency_ids: Iterable[int],
    *,
    source_kind: str | None = None,
    event_ids: Collection[int] | None = None,
) -> dict[int, DependencyEvent]:
    """Compatibility access to current statement identities only."""
    return {
        dependency_id: statement.event
        for dependency_id, statement in current_dependency_statements(
            session,
            dependency_ids,
            source_kind=source_kind,
            event_ids=event_ids,
        ).items()
        if statement.event is not None and statement.effective_date is not None
    }


def project_committed_date(session: Session, dependency_id: int) -> None:
    """Refresh the scalar compatibility projection from its statement view."""
    dependency = session.get(Dependency, dependency_id)
    if dependency is not None:
        statement = current_dependency_statements(session, (dependency_id,)).get(
            dependency_id
        )
        dependency.committed_date = statement.effective_date if statement else None


def project_committed_dates(session: Session, dependency_ids: Iterable[int]) -> None:
    """Refresh every compatibility projection affected by one statement."""
    for dependency_id in set(dependency_ids):
        project_committed_date(session, dependency_id)


def verbal_attribution(event: DependencyEvent | None) -> str | None:
    """The source line that must travel with a verbal-backed date."""
    if event is None or event.source_kind != "verbal":
        return None
    return (
        f"Verbal — {event.stated_party} told {event.created_by} "
        f"on {event.event_date}"
    )
