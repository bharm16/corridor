"""Read exact-day, known-scope statements into the legacy Dependency view.

The old event table stored one Dependency and one scalar date, so every reader
invented its own answer after a party-level statement or a month-only source.
The structured event record is now authoritative; this module intentionally
exports only the honest subset legacy Dependency readers can represent.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.models import Dependency, DependencyEvent, DependencyEventScope, DependencyEventTiming


COMMITTED_EVENT_TYPES = ("commitment", "committed_date_change")


def latest_committed_events(
    session: Session,
    dependency_ids: Iterable[int],
    *,
    source_kind: str | None = None,
    event_ids: Collection[int] | None = None,
) -> dict[int, DependencyEvent]:
    """The newest date-bearing commitment per Dependency, by stated date."""
    ids = tuple(dependency_ids)
    if not ids:
        return {}
    if event_ids is not None and not event_ids:
        return {}
    query = (
        select(DependencyEventScope.dependency_id, DependencyEvent)
        .join(DependencyEvent, DependencyEventScope.event_id == DependencyEvent.id)
        .join(DependencyEventTiming, DependencyEventTiming.event_id == DependencyEvent.id)
        .where(
            DependencyEventScope.dependency_id.in_(ids),
            DependencyEvent.event_type.in_(COMMITTED_EVENT_TYPES),
            DependencyEvent.scope_mode.in_(("selected", "all_active")),
            DependencyEventTiming.kind == "new",
            DependencyEventTiming.precision == "day",
        )
    )
    if source_kind is not None:
        query = query.where(DependencyEvent.source_kind == source_kind)
    if event_ids is not None:
        query = query.where(DependencyEvent.id.in_(event_ids))
    rows = session.execute(
        query.order_by(
            DependencyEventScope.dependency_id,
            DependencyEvent.event_date.desc().nulls_last(),
            DependencyEvent.id.desc(),
        )
    ).all()
    latest: dict[int, DependencyEvent] = {}
    for dependency_id, event in rows:
        latest.setdefault(dependency_id, event)
    return latest


def project_committed_date(session: Session, dependency_id: int) -> None:
    """Refresh the Dependency's stored projection from its appended events."""
    event = latest_committed_events(session, (dependency_id,)).get(dependency_id)
    dependency = session.get(Dependency, dependency_id)
    if dependency is not None:
        dependency.committed_date = (
            event.new_timing.start_date if event is not None and event.new_timing else None
        )


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
