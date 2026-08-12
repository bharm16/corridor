"""Record what an External Party said without dressing it as a document.

The earlier idea of a coordinator-only note left a material commitment outside
the append-only history and unable to drive its existing projection. A verbal
is instead a human-attributed DependencyEvent, not a second history beside the
event chain. It carries the party as stated, the day of the conversation, and
the date the party gave; its explicit source kind stops an absent citation from
ever being mistaken for a declaration. The projected Committed Date still comes
from the newest statement, exactly as it does for cited events.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from corridor import audit
from corridor.external_statements import (
    StatementRefusal,
    StatementScope,
    StatementTiming,
    record_external_party_statement,
)
from corridor.identity import is_project_side_party, party_matches
from corridor.models import Dependency, DependencyEvent, Project
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.project_lock import lock_project


class VerbalRefusal(ValueError):
    """A phone statement cannot become a commitment on this record."""


def record_verbal(
    session: Session,
    dependency: Dependency,
    *,
    stated_party: str,
    description: str,
    conversation_date: date,
    committed_date: date,
    principal: HumanPrincipal,
) -> DependencyEvent:
    """Append what the named External Party told the recorder on a call."""
    recorder = require_human_principal(principal)
    party = stated_party.strip()
    what_was_said = description.strip()
    if not party:
        raise VerbalRefusal("a verbal must name the party who spoke")
    if not what_was_said:
        raise VerbalRefusal("a verbal must say what the party told you")
    if not isinstance(conversation_date, date):
        raise VerbalRefusal("a verbal must record the conversation date")
    if not isinstance(committed_date, date):
        raise VerbalRefusal("a verbal must carry the date the party gave")

    project = session.get(Project, dependency.project_id)
    if project is None:
        raise VerbalRefusal("the record's project no longer exists")
    lock_project(session, project.id)
    session.refresh(dependency)
    if dependency.dismissed_at is not None:
        raise VerbalRefusal(
            f"{dependency.ref_code} was dismissed — record a restoration "
            "decision before a verbal"
        )
    if is_project_side_party(project, party):
        raise VerbalRefusal(
            f"{party} is the project's own side — an action item, never an "
            "External Party commitment"
        )
    if not party_matches(session, dependency, party):
        raise VerbalRefusal(
            f"{party} is not this record's External Party or a registered alias"
        )

    if dependency.external_org_id is None:
        raise VerbalRefusal("this record has no resolved External Party")
    try:
        event = record_external_party_statement(
            session,
            project_id=project.id,
            affected_external_org_id=dependency.external_org_id,
            stated_party=party,
            stated_external_org_id=dependency.external_org_id,
            source_kind="verbal",
            event_date=conversation_date,
            description=what_was_said,
            new_timing=StatementTiming.day(committed_date.isoformat(), committed_date),
            scope=StatementScope.selected((dependency.id,)),
            created_by=recorder.subject,
        )
    except StatementRefusal as exc:
        raise VerbalRefusal(str(exc)) from exc
    audit.record(
        session,
        principal=recorder,
        action=audit.RECORD_VERBAL,
        entity_type=audit.DEPENDENCY,
        entity_id=dependency.id,
        after={
            "dependency_event_id": event.id,
            "source_kind": event.source_kind,
            "stated_party": party,
            "conversation_date": conversation_date.isoformat(),
            "committed_date": event.new_timing.start_date.isoformat(),
            "event_type": event.event_type,
        },
    )
    session.flush()
    return event
