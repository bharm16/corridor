"""Read current structured statements into the legacy Dependency view.

The old event table stored one Dependency and one scalar date, so every reader
invented its own answer after a party-level statement or a month-only source.
Structured events are authoritative; this module gives compatibility readers
the one exact-day projection they can represent honestly.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import date

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


def current_dependency_statements(
    session: Session,
    dependency_ids: Iterable[int],
    *,
    source_kind: str | None = None,
    event_ids: Collection[int] | None = None,
) -> dict[int, CurrentDependencyStatement]:
    """Return the current exact-day statement and closure state per Dependency.

    An unfiltered compatibility read preserves an existing scalar only where
    no structured event exists.  A filtered read is event-only: selecting
    cited statements must not surface a Verbal-backed scalar as cited.
    """
    ids = tuple(dict.fromkeys(dependency_ids))
    if not ids:
        return {}

    scalar_dates = dict(
        session.execute(
            select(Dependency.id, Dependency.committed_date).where(
                Dependency.id.in_(ids)
            )
        ).all()
    )
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

    filtered = source_kind is not None or event_ids is not None
    statements: dict[int, CurrentDependencyStatement] = {}
    for dependency_id in scalar_dates:
        current_event = current.get(dependency_id)
        effective_date = (
            current_event[1].start_date
            if current_event is not None and current_event[1].precision == "day"
            else None
        )
        if current_event is None and not filtered:
            effective_date = scalar_dates[dependency_id]
        statements[dependency_id] = CurrentDependencyStatement(
            event=current_event[0] if current_event else None,
            effective_date=effective_date,
            provenance_class=current_event[0].source_kind if current_event else None,
            is_closed=dependency_id in closed_ids,
        )
    return statements


def verified_cited_statement_event_ids(
    session: Session, dependency_ids: Iterable[int]
) -> set[int]:
    """Cited statement ids whose own Evidence is verified for these Dependencies."""
    ids = tuple(dict.fromkeys(dependency_ids))
    if not ids:
        return set()
    return set(
        session.scalars(
            select(DependencyEventEvidence.event_id)
            .join(
                EvidenceLink,
                EvidenceLink.id == DependencyEventEvidence.evidence_link_id,
            )
            .join(
                DependencyEvent,
                DependencyEventEvidence.event_id == DependencyEvent.id,
            )
            .join(
                DependencyEventScope,
                DependencyEventScope.event_id == DependencyEventEvidence.event_id,
            )
            .join(
                DependencyEventScopeDecision,
                DependencyEventScope.scope_decision_id
                == DependencyEventScopeDecision.id,
            )
            .where(
                DependencyEventScope.dependency_id.in_(ids),
                current_scope_decision_filter(),
                DependencyEvent.source_kind == "cited",
                EvidenceLink.verified.is_(True),
            )
            .distinct()
        )
    )


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
