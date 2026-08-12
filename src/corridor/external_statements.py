"""Record an External Party statement without collapsing attribution or scope.

The first event implementation put a statement directly on one Dependency and
turned every timing into a day.  That made an affected party look like its
speaker, made ``01/2025`` look like January 1, and forced a party-level fact
onto an invented conflict.  This module is the shared write boundary for the
mechanical admission policy, human statement placement, and Verbal adapter.
Those doors may resolve facts differently, but none may build a second event
shape or a second Committed Date projection.
"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.identity import is_project_side_party, normalize_party
from corridor.models import (
    Dependency,
    DependencyEvent,
    DependencyEventEvidence,
    DependencyEventScope,
    DependencyEventScopeDecision,
    DependencyEventTiming,
    Document,
    EvidenceLink,
    ExternalOrg,
    Project,
)
from corridor.principals import HumanPrincipal, InvalidHumanPrincipal
from corridor.project_lock import lock_project


class StatementRefusal(ValueError):
    """The proposed statement would manufacture a fact the record lacks."""


_STATEMENT_SCOPE_POLICY_ACTORS = frozenset(
    {"corridor:event-admission", "corridor:statement-migration-v1"}
)


@dataclass(frozen=True)
class StatementTiming:
    """One timing exactly as the External Party stated it."""

    text: str
    precision: str
    start_date: date | None
    end_date: date | None

    @classmethod
    def day(cls, text: str, value: date) -> "StatementTiming":
        return cls(text=text, precision="day", start_date=value, end_date=value)

    @classmethod
    def month(cls, text: str, year: int, month: int) -> "StatementTiming":
        return cls(
            text=text,
            precision="month",
            start_date=date(year, month, 1),
            end_date=date(year, month, monthrange(year, month)[1]),
        )

    @classmethod
    def approximate(cls, text: str) -> "StatementTiming":
        return cls(text=text, precision="approximate", start_date=None, end_date=None)


@dataclass(frozen=True)
class StatementScope:
    """The attributable scope decision, never inferred from party context."""

    mode: str
    dependency_ids: tuple[int, ...] = ()

    @classmethod
    def unknown(cls) -> "StatementScope":
        return cls("unknown")

    @classmethod
    def selected(cls, dependency_ids: tuple[int, ...] | list[int]) -> "StatementScope":
        return cls("selected", tuple(dependency_ids))

    @classmethod
    def all_active(cls) -> "StatementScope":
        return cls("all_active")


@dataclass(frozen=True)
class CitedStatementEvidence:
    """The one page citation owned by a cited event rather than a Dependency."""

    document_id: int
    page_no: int
    quote: str


def record_external_party_statement(
    session: Session,
    *,
    project_id: int,
    affected_external_org_id: int,
    stated_party: str,
    stated_external_org_id: int,
    source_kind: str,
    event_date: date | None,
    description: str,
    new_timing: StatementTiming,
    scope: StatementScope,
    created_by: str,
    previous_timing: StatementTiming | None = None,
    evidence: CitedStatementEvidence | None = None,
) -> DependencyEvent:
    """Append one attributable External Party Commitment or Date Change.

    The caller supplies already-resolved attribution and source provenance.
    This boundary validates the durable facts together, snapshots any known
    scope, creates one event-level citation, and refreshes only exact-day,
    known-scope Dependency projections.
    """
    project = session.get(Project, project_id)
    if project is None:
        raise StatementRefusal(f"project {project_id} does not exist")
    lock_project(session, project.id)

    party = stated_party.strip()
    if not party:
        raise StatementRefusal("a statement must name the party who spoke")
    if not description.strip():
        raise StatementRefusal("a statement must preserve what the party said")
    if not created_by.strip():
        raise StatementRefusal("a statement must identify who recorded it")
    if source_kind not in {"cited", "verbal"}:
        raise StatementRefusal("a statement has an unknown source kind")
    if source_kind == "cited" and evidence is None:
        raise StatementRefusal("a cited statement requires its verified Evidence")
    if source_kind == "verbal" and evidence is not None:
        raise StatementRefusal("a Verbal cannot be presented as cited Evidence")

    affected = session.get(ExternalOrg, affected_external_org_id)
    stated = session.get(ExternalOrg, stated_external_org_id)
    if affected is None or stated is None:
        raise StatementRefusal("the affected and stated External Parties must exist")
    if normalize_party(party) not in {
        normalize_party(name) for name in (stated.name, *(stated.aliases or [])) if name
    }:
        raise StatementRefusal(
            "the stated-party wording does not resolve to the stated External Party"
        )
    if is_project_side_party(project, party):
        raise StatementRefusal(
            f"{party} is the project's own side — an action item, never an External Party commitment"
        )

    _validate_timing(new_timing)
    if previous_timing is not None:
        _validate_timing(previous_timing)
    if source_kind == "verbal":
        if event_date is None:
            raise StatementRefusal("a Verbal must preserve the conversation date")
        if previous_timing is not None:
            raise StatementRefusal("a Verbal cannot be a Committed Date Change")
        if new_timing.precision != "day":
            raise StatementRefusal("a Verbal must preserve one exact-day commitment")
        if scope.mode != "selected" or len(scope.dependency_ids) != 1:
            raise StatementRefusal("a Verbal must scope to exactly one Dependency")
    scope_actor = _scope_actor_subject(created_by)
    event_type = "committed_date_change" if previous_timing else "commitment"
    dependency_ids = _resolve_scope(
        session,
        project_id=project.id,
        affected_external_org_id=affected.id,
        scope=scope,
    )
    if evidence is not None:
        _validate_evidence(session, evidence, project.id)

    # This command is the one atomic writer for every statement path. A
    # database refusal after the event row exists must not leave the caller
    # with an unusable transaction or an event missing its timing, scope,
    # Evidence, or projection.
    with session.begin_nested():
        event = DependencyEvent(
            project_id=project.id,
            affected_external_org_id=affected.id,
            stated_external_org_id=stated.id,
            attribution_state="resolved",
            stated_party=party,
            event_type=event_type,
            source_kind=source_kind,
            scope_mode=scope.mode,
            timing_direction=(
                _timing_direction(previous_timing, new_timing)
                if previous_timing is not None
                else None
            ),
            event_date=event_date,
            description=description.strip(),
            created_by=created_by.strip(),
        )
        session.add(event)
        session.flush([event])
        scope_decision = session.scalar(
            select(DependencyEventScopeDecision).where(
                DependencyEventScopeDecision.event_id == event.id
            )
        )
        if scope_decision is None:
            raise RuntimeError("statement event did not receive its initial scope decision")
        session.add(
            DependencyEventTiming(
                event_id=event.id,
                kind="new",
                text=new_timing.text.strip(),
                precision=new_timing.precision,
                start_date=new_timing.start_date,
                end_date=new_timing.end_date,
            )
        )
        if previous_timing is not None:
            session.add(
                DependencyEventTiming(
                    event_id=event.id,
                    kind="previous",
                    text=previous_timing.text.strip(),
                    precision=previous_timing.precision,
                    start_date=previous_timing.start_date,
                    end_date=previous_timing.end_date,
                )
            )
        for dependency_id in dependency_ids:
            session.add(
                DependencyEventScope(
                    event_id=event.id,
                    scope_decision_id=scope_decision.id,
                    dependency_id=dependency_id,
                    recorded_by=scope_actor,
                )
            )
        if evidence is not None:
            event_evidence = EvidenceLink(
                dependency_id=None,
                document_id=evidence.document_id,
                page_no=evidence.page_no,
                quote=evidence.quote.strip(),
                verified=True,
            )
            session.add(event_evidence)
            session.flush([event_evidence])
            session.add(
                DependencyEventEvidence(
                    evidence_link_id=event_evidence.id,
                    event_id=event.id,
                    recorded_by=created_by.strip(),
                )
            )
        session.flush()

        # Imported lazily: projections read the event representation but do
        # not participate in its write validation.
        from corridor.dependency_events import project_committed_dates

        project_committed_dates(session, dependency_ids)
        session.flush()
    return event


def record_statement_scope_decision(
    session: Session,
    *,
    event_id: int,
    scope: StatementScope,
    actor: HumanPrincipal | str,
) -> DependencyEventScopeDecision:
    """Append a correction or expansion of one statement's Dependency scope.

    The former decision and its links remain readable history.  Readers use
    the one decision not superseded by a later decision when deriving current
    Dependency effects.
    """
    event = session.get(DependencyEvent, event_id)
    if event is None:
        raise StatementRefusal(f"statement event {event_id} does not exist")
    actor_subject = _scope_actor_subject(actor)
    lock_project(session, event.project_id)
    predecessor = _current_scope_decision(session, event.id)
    if predecessor is None:
        raise StatementRefusal("statement has no scope decision to correct")
    dependency_ids = _resolve_scope(
        session,
        project_id=event.project_id,
        affected_external_org_id=event.affected_external_org_id,
        scope=scope,
    )
    previous_ids = tuple(
        session.scalars(
            select(DependencyEventScope.dependency_id).where(
                DependencyEventScope.scope_decision_id == predecessor.id
            )
        ).all()
    )
    with session.begin_nested():
        decision = DependencyEventScopeDecision(
            event_id=event.id,
            scope_mode=scope.mode,
            supersedes_scope_decision_id=predecessor.id,
            decided_by=actor_subject,
        )
        session.add(decision)
        session.flush([decision])
        for dependency_id in dependency_ids:
            session.add(
                DependencyEventScope(
                    event_id=event.id,
                    scope_decision_id=decision.id,
                    dependency_id=dependency_id,
                    recorded_by=actor_subject,
                )
            )
        session.flush()
        from corridor.dependency_events import project_committed_dates

        project_committed_dates(session, (*previous_ids, *dependency_ids))
        session.flush()
    return decision


def _validate_timing(timing: StatementTiming) -> None:
    if not timing.text.strip():
        raise StatementRefusal("a stated timing must preserve its source wording")
    if timing.precision == "day":
        if timing.start_date is None or timing.end_date != timing.start_date:
            raise StatementRefusal("a day timing must name exactly one day")
    elif timing.precision == "month":
        if timing.start_date is None or timing.end_date is None:
            raise StatementRefusal("a month timing must retain its calendar bounds")
        expected_end = date(
            timing.start_date.year,
            timing.start_date.month,
            monthrange(timing.start_date.year, timing.start_date.month)[1],
        )
        if timing.start_date.day != 1 or timing.end_date != expected_end:
            raise StatementRefusal("a month timing has invalid calendar bounds")
    elif timing.precision == "approximate":
        if timing.start_date is not None or timing.end_date is not None:
            raise StatementRefusal("an approximate timing cannot claim calendar bounds")
    else:
        raise StatementRefusal(f"unknown timing precision {timing.precision!r}")


def _timing_direction(
    previous: StatementTiming, new: StatementTiming
) -> str:
    """Name movement only when the two source-supported periods prove it."""
    if previous.end_date is not None and new.start_date is not None:
        if new.start_date > previous.end_date:
            return "later"
    if new.end_date is not None and previous.start_date is not None:
        if new.end_date < previous.start_date:
            return "earlier"
    return "unknown"


def _resolve_scope(
    session: Session,
    *,
    project_id: int,
    affected_external_org_id: int,
    scope: StatementScope,
) -> tuple[int, ...]:
    if scope.mode == "unknown":
        if scope.dependency_ids:
            raise StatementRefusal("unknown scope cannot name Dependencies")
        return ()
    if scope.mode == "all_active":
        if scope.dependency_ids:
            raise StatementRefusal("all-active scope derives its own snapshot")
        ids = tuple(
            session.scalars(
                select(Dependency.id)
                .where(
                    Dependency.project_id == project_id,
                    Dependency.external_org_id == affected_external_org_id,
                    Dependency.dismissed_at.is_(None),
                    Dependency.status != "closed",
                )
                .order_by(Dependency.id)
            ).all()
        )
        if not ids:
            raise StatementRefusal("all-active scope found no active Dependencies")
        return ids
    if scope.mode != "selected":
        raise StatementRefusal(f"unknown statement scope {scope.mode!r}")
    if not scope.dependency_ids:
        raise StatementRefusal("selected scope must name at least one Dependency")
    if len(scope.dependency_ids) != len(set(scope.dependency_ids)):
        raise StatementRefusal("selected scope contains a duplicate Dependency")
    dependencies = session.scalars(
        select(Dependency).where(Dependency.id.in_(scope.dependency_ids))
    ).all()
    if len(dependencies) != len(scope.dependency_ids):
        raise StatementRefusal("selected scope names a missing Dependency")
    for dependency in dependencies:
        if dependency.project_id != project_id:
            raise StatementRefusal("selected scope cannot cross projects")
        if dependency.external_org_id != affected_external_org_id:
            raise StatementRefusal("selected scope names another External Party")
        if dependency.dismissed_at is not None:
            raise StatementRefusal("selected scope cannot include a dismissed Dependency")
        if dependency.status == "closed":
            raise StatementRefusal("selected scope cannot include a closed Dependency")
    return tuple(scope.dependency_ids)


def _scope_actor_subject(actor: HumanPrincipal | str) -> str:
    """Accept a named human or a deployed Corridor policy, never a role."""
    if isinstance(actor, HumanPrincipal):
        return actor.subject
    if not isinstance(actor, str) or not actor.strip() or actor != actor.strip():
        raise StatementRefusal("a scope decision must name its human or deployed-policy actor")
    if actor in _STATEMENT_SCOPE_POLICY_ACTORS:
        return actor
    try:
        return HumanPrincipal(actor).subject
    except InvalidHumanPrincipal as exc:
        raise StatementRefusal(
            "a scope decision actor must be a named human or deployed Corridor policy"
        ) from exc


def _current_scope_decision(
    session: Session, event_id: int
) -> DependencyEventScopeDecision | None:
    superseding = DependencyEventScopeDecision.__table__.alias("superseding")
    return session.scalar(
        select(DependencyEventScopeDecision)
        .where(
            DependencyEventScopeDecision.event_id == event_id,
            ~select(superseding.c.id)
            .where(
                superseding.c.supersedes_scope_decision_id
                == DependencyEventScopeDecision.id
            )
            .exists(),
        )
        .order_by(DependencyEventScopeDecision.id)
    )


def _validate_evidence(
    session: Session, evidence: CitedStatementEvidence, project_id: int
) -> None:
    if evidence.page_no < 1 or not evidence.quote.strip():
        raise StatementRefusal("cited Evidence needs a page and quote")
    document = session.get(Document, evidence.document_id)
    if document is None or document.project_id != project_id:
        raise StatementRefusal("cited Evidence belongs to another project")
