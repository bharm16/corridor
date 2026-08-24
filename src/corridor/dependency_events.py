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
    CommitmentLineage,
    Dependency,
    DependencyEvent,
    DependencyEventEvidence,
    DependencyEventScope,
    DependencyEventScopeDecision,
    DependencyEventTiming,
    Document,
    EvidenceLink,
    WorkDecision,
)
from corridor.statement_lifecycle import (
    current_scope_decision_filter as current_lifecycle_scope_decision_filter,
    current_statement_event_filter,
)
from corridor.statement_evidence import validate_cited_statement_evidence
from corridor.statement_values import CitedStatementEvidence, StatementRefusal


COMMITTED_EVENT_TYPES = ("commitment", "committed_date_change")


def current_scope_decision_filter():
    """SQL predicate for the one scope decision not replaced by a later act."""
    superseding = aliased(DependencyEventScopeDecision)
    return (
        ~exists(
            select(superseding.id).where(
                superseding.supersedes_scope_decision_id
                == DependencyEventScopeDecision.id
            )
        )
        & current_lifecycle_scope_decision_filter(DependencyEventScopeDecision.id)
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
            current_statement_event_filter(DependencyEvent.id),
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
class PublishedStatementCoordinationPlan:
    """The current, separately-attributable project response to a statement."""

    internal_owner: str | None
    internal_owner_decision: WorkDecision | None
    next_action: str | None
    next_action_decision: WorkDecision | None
    action_due_date: date | None
    action_due_date_reason: str | None
    milestone_impact: str | None
    milestone_impact_decision: WorkDecision | None
    needs_review: bool


@dataclass(frozen=True)
class PublishedPartyStatement:
    """One open unknown-scope statement a Report may publish without a Dependency."""

    current_event: DependencyEvent
    event: DependencyEvent | None
    timings: tuple[DependencyEventTiming, ...]
    scope_decision: DependencyEventScopeDecision
    cited_provenance: CitedStatementProvenance | None
    is_closed: bool
    unsupported_current: bool
    plan: PublishedStatementCoordinationPlan


@dataclass(frozen=True)
class StatementPublication:
    """One provenance-consistent statement reading for every Report surface."""

    project_id: int
    by_dependency: Mapping[int, PublishedDependencyStatement]
    document_only: bool = False
    party_statements: tuple[PublishedPartyStatement, ...] = ()

    def __post_init__(self) -> None:
        """Keep the paired publication from being changed after it is read."""
        object.__setattr__(self, "by_dependency", MappingProxyType(dict(self.by_dependency)))
        object.__setattr__(self, "party_statements", tuple(self.party_statements))

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
            party_statements=tuple(
                PartyStatementPublicationEntryFingerprint(
                    commitment_lineage_id=statement.current_event.commitment_lineage_id,
                    current_event_id=statement.current_event.id,
                    published_event_id=(
                        statement.event.id if statement.event is not None else None
                    ),
                    scope_decision_id=statement.scope_decision.id,
                    is_closed=statement.is_closed,
                    unsupported_current=statement.unsupported_current,
                    plan_decision_ids=tuple(
                        decision.id
                        for decision in (
                            statement.plan.internal_owner_decision,
                            statement.plan.next_action_decision,
                            statement.plan.milestone_impact_decision,
                        )
                        if decision is not None
                    ),
                    plan_needs_review=statement.plan.needs_review,
                )
                for statement in self.party_statements
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
class PartyStatementPublicationEntryFingerprint:
    """One party-level statement identity inside a frozen Report reading."""

    commitment_lineage_id: int | None
    current_event_id: int
    published_event_id: int | None
    scope_decision_id: int
    is_closed: bool
    unsupported_current: bool
    plan_decision_ids: tuple[int, ...]
    plan_needs_review: bool


@dataclass(frozen=True)
class StatementPublicationFingerprint:
    """The immutable statement identity and audience mode behind one reading."""

    document_only: bool
    statements: tuple[StatementPublicationEntryFingerprint, ...]
    party_statements: tuple[PartyStatementPublicationEntryFingerprint, ...] = ()


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
            DependencyEventScopeDecision.scope_mode.in_(
                ("selected", "all_active", "carried_forward")
            ),
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
    return StatementPublication(
        project_id,
        by_dependency,
        document_only=document_only,
        party_statements=published_party_statements(
            session,
            project_id=project_id,
            document_only=document_only,
        ),
    )


def published_party_statements(
    session: Session,
    *,
    project_id: int,
    document_only: bool,
) -> tuple[PublishedPartyStatement, ...]:
    """Freeze every current unknown-scope party statement for Report readers.

    A statement with unknown Dependency scope cannot enter the legacy
    ``by_dependency`` projection.  It still belongs in the automatic internal
    Report, so it travels beside that projection in the same immutable
    ``StatementPublication`` rather than inviting a second reader to select
    its own current event, Evidence, or provenance mode.
    """
    rows = session.execute(
        select(DependencyEvent, DependencyEventScopeDecision, CommitmentLineage)
        .join(
            DependencyEventScopeDecision,
            DependencyEventScopeDecision.event_id == DependencyEvent.id,
        )
        .join(
            CommitmentLineage,
            CommitmentLineage.id == DependencyEvent.commitment_lineage_id,
        )
        .where(
            DependencyEvent.project_id == project_id,
            DependencyEvent.event_type.in_(COMMITTED_EVENT_TYPES),
            DependencyEvent.attribution_state == "resolved",
            DependencyEvent.stated_external_org_id.is_not(None),
            DependencyEvent.commitment_lineage_id.is_not(None),
            DependencyEventScopeDecision.scope_mode == "unknown",
            current_scope_decision_filter(),
            current_statement_event_filter(DependencyEvent.id),
        )
        .order_by(DependencyEvent.commitment_lineage_id, DependencyEvent.id)
    ).all()
    event_ids = tuple(event.id for event, _, _ in rows)
    timings_by_event: dict[int, list[DependencyEventTiming]] = {}
    if event_ids:
        for timing in session.scalars(
            select(DependencyEventTiming)
            .where(DependencyEventTiming.event_id.in_(event_ids))
            .order_by(DependencyEventTiming.event_id, DependencyEventTiming.id)
        ):
            timings_by_event.setdefault(timing.event_id, []).append(timing)

    cited_provenance = _verified_party_statement_provenance(
        session, event_ids, project_id=project_id
    )
    closed_lineages = _closed_party_commitment_lineages(session, project_id)
    published: list[PublishedPartyStatement] = []
    for event, scope_decision, lineage in rows:
        published_event = (
            event
            if event.source_kind == "verbal" or event.id in cited_provenance
            else None
        )
        if document_only and event.source_kind != "cited":
            published_event = None
        plan = _published_statement_plan(session, lineage)
        published.append(
            PublishedPartyStatement(
                current_event=event,
                event=published_event,
                timings=(
                    tuple(timings_by_event.get(event.id, ()))
                    if published_event is not None
                    else ()
                ),
                scope_decision=scope_decision,
                cited_provenance=(
                    cited_provenance.get(event.id)
                    if published_event is not None
                    else None
                ),
                is_closed=lineage.id in closed_lineages,
                unsupported_current=published_event is None,
                plan=plan,
            )
        )
    return tuple(published)


def _published_statement_plan(
    session: Session, lineage: CommitmentLineage
) -> PublishedStatementCoordinationPlan:
    """Read one statement plan's receipt tails while freezing publication."""
    # ``work_decisions`` imports this module for scope authority, so this local
    # import keeps the shared read seam acyclic while preserving its one tail
    # reader for all three independently-attributable plan fields.
    from corridor.work_decisions import (
        CoordinationSubject,
        current_internal_owner_decision,
        current_milestone_impact_decision,
        current_next_action_decision,
    )

    subject = CoordinationSubject.statement(lineage.id)
    return PublishedStatementCoordinationPlan(
        internal_owner=lineage.internal_owner,
        internal_owner_decision=current_internal_owner_decision(session, subject),
        next_action=lineage.next_action,
        next_action_decision=current_next_action_decision(session, subject),
        action_due_date=lineage.action_due_date,
        action_due_date_reason=lineage.action_due_date_reason,
        milestone_impact=lineage.milestone_impact,
        milestone_impact_decision=current_milestone_impact_decision(session, subject),
        needs_review=lineage.plan_needs_review,
    )


def _verified_party_statement_provenance(
    session: Session,
    event_ids: Iterable[int],
    *,
    project_id: int,
) -> dict[int, CitedStatementProvenance]:
    """Return only page- and quote-verified Evidence owned by these events."""
    ids = tuple(dict.fromkeys(event_ids))
    if not ids:
        return {}
    # The writer validates cited Evidence, but publication has to fail closed
    # when a later raw mutation or bad migration makes that exact page/quote
    # unsupported.  Do not publish an older statement in its place.
    provenance: dict[int, CitedStatementProvenance] = {}
    rows = session.execute(
        select(DependencyEventEvidence.event_id, EvidenceLink, Document)
        .join(
            EvidenceLink,
            EvidenceLink.id == DependencyEventEvidence.evidence_link_id,
        )
        .join(Document, Document.id == EvidenceLink.document_id)
        .where(
            DependencyEventEvidence.event_id.in_(ids),
            EvidenceLink.verified.is_(True),
        )
        .order_by(DependencyEventEvidence.event_id, EvidenceLink.id)
    ).all()
    for event_id, link, document in rows:
        try:
            validate_cited_statement_evidence(
                session,
                CitedStatementEvidence(link.document_id, link.page_no, link.quote),
                project_id,
            )
        except StatementRefusal:
            continue
        provenance.setdefault(
            event_id,
            CitedStatementProvenance(
                document.id,
                document.filename,
                link.page_no,
                link.quote,
            ),
        )
    return provenance


def _closed_party_commitment_lineages(
    session: Session, project_id: int
) -> frozenset[int]:
    """A Closure ends a party-level row only when its provenance still holds."""
    closures = session.scalars(
        select(DependencyEvent).where(
            DependencyEvent.project_id == project_id,
            DependencyEvent.event_type == "closure",
            DependencyEvent.closes_commitment_lineage_id.is_not(None),
            DependencyEvent.attribution_state == "resolved",
            DependencyEvent.stated_external_org_id.is_not(None),
            current_statement_event_filter(DependencyEvent.id),
        )
    ).all()
    cited = _verified_party_statement_provenance(
        session, (closure.id for closure in closures), project_id=project_id
    )
    return frozenset(
        closure.closes_commitment_lineage_id
        for closure in closures
        if (
            closure.source_kind == "verbal" and closure.event_date is not None
        )
        or (closure.source_kind == "cited" and closure.id in cited)
    )


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
