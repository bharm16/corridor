"""The shared event history and its one Committed Date projection.

Mechanical minutes admission once carried its own "latest commitment" query,
while reports read the Dependency projection and would quietly disagree after
a later statement. Mechanical admission and a coordinator's verbal are now
distinct writers, but their statements share one order and one date
projection. Keeping that readback here prevents either writer or a report from
inventing a different meaning of "newest commitment."
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.models import Dependency, DependencyEvent


COMMITTED_EVENT_TYPES = ("commitment", "slip")


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
    query = select(DependencyEvent).where(
        DependencyEvent.dependency_id.in_(ids),
        DependencyEvent.event_type.in_(COMMITTED_EVENT_TYPES),
        DependencyEvent.committed_date.is_not(None),
    )
    if source_kind is not None:
        query = query.where(DependencyEvent.source_kind == source_kind)
    if event_ids is not None:
        query = query.where(DependencyEvent.id.in_(event_ids))
    events = session.scalars(
        query.order_by(
            DependencyEvent.dependency_id,
            DependencyEvent.event_date.desc().nulls_last(),
            DependencyEvent.id.desc(),
        )
    ).all()
    latest: dict[int, DependencyEvent] = {}
    for event in events:
        latest.setdefault(event.dependency_id, event)
    return latest


def project_committed_date(session: Session, dependency_id: int) -> None:
    """Refresh the Dependency's stored projection from its appended events."""
    event = latest_committed_events(session, (dependency_id,)).get(dependency_id)
    if event is None:
        return
    dependency = session.get(Dependency, dependency_id)
    if dependency is not None:
        dependency.committed_date = event.committed_date


def event_type_for_verbal(
    session: Session, dependency_id: int, committed_date: date
) -> str:
    """A later stated date is a Slip; any other newly stated date commits."""
    previous = latest_committed_events(session, (dependency_id,)).get(dependency_id)
    if previous is not None and committed_date > previous.committed_date:
        return "slip"
    return "commitment"


def verbal_attribution(event: DependencyEvent | None) -> str | None:
    """The source line that must travel with a verbal-backed date."""
    if event is None or event.source_kind != "verbal":
        return None
    return (
        f"Verbal — {event.stated_party} told {event.created_by} "
        f"on {event.event_date}"
    )
