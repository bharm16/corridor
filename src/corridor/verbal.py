"""Record what an External Party said without dressing it as a document.

The earlier idea of a coordinator-only note left a material commitment outside
the append-only history and unable to drive its existing projection. A verbal
is instead a human-attributed DependencyEvent, not a second history beside the
event chain. It carries the party as stated, the day of the conversation, and
the timing the party gave; its explicit source kind stops an absent citation
from ever being mistaken for a declaration. The projected Committed Date still
comes from the newest statement, exactly as it does for cited events.

The first version forced every call into one exact-day commitment scoped to one
Constraint.  ADR-0036 accepts a richer shape: a verbal may preserve month or
approximate timing, apply to one or several Constraints, stay party-level while
its scope is unknown, or record a stated change of promised timing.  The stated
organization is a separate fact from the recorder and from scope — a prefilled
organization is confirmed, never inferred from which Constraint the coordinator
was looking at.  The single-Constraint exact-day :func:`record_verbal` is kept
as a convenience over the same atomic writer.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor import audit
from corridor.external_statements import (
    StatementRefusal,
    StatementScope,
    StatementTiming,
    record_external_party_statement,
    record_statement_scope_decision,
)
from corridor.facts import (
    append_recorded_applies_to_fact,
    append_recorded_statement_timing_fact,
    append_recorded_statement_wording_fact,
)
from corridor.fact_decisions import record_human_fact_decision
from corridor.identity import is_project_side_party, party_matches
from corridor.models import (
    CommitmentLineage,
    CommitmentScopeDecision,
    CommitmentScopeMembership,
    Dependency,
    DependencyEvent,
    ExternalParty,
    Fact,
    FactAppliesTo,
    FactDecision,
    Project,
    SourceSegment,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.project_lock import lock_project
from corridor.source_segments import recorded_verbal_statement_segment
from corridor.statement_lifecycle import observe_current_statement


class VerbalRefusal(ValueError):
    """A phone statement cannot become a commitment on this record."""


class StaleVerbalCorrection(VerbalRefusal):
    """The recorded verbal changed underneath a correction; reload and retry."""


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
    """Append one exact-day phone commitment scoped to this one Dependency.

    This is the single-Constraint convenience over the shared writer.  Richer
    timing or scope goes through :func:`record_verbal_statement`.
    """
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

    return _append_verbal(
        session,
        recorder=recorder,
        project=project,
        external_org=session.get(ExternalParty, dependency.external_org_id),
        stated_party=party,
        description=what_was_said,
        conversation_date=conversation_date,
        new_timing=StatementTiming.day(committed_date.isoformat(), committed_date),
        scope=StatementScope.selected((dependency.id,)),
        audit_entity_type=audit.DEPENDENCY,
        audit_entity_id=dependency.id,
    )


def record_verbal_statement(
    session: Session,
    *,
    project_id: int,
    external_org_id: int,
    stated_party: str,
    description: str,
    conversation_date: date,
    new_timing: StatementTiming,
    scope: StatementScope,
    principal: HumanPrincipal,
) -> DependencyEvent:
    """Append one attributable verbal Commitment at its stated precision/scope.

    The stated organization is supplied explicitly and confirmed; it is never
    inferred from the chosen scope.  Timing keeps its wording and precision, and
    the coordinator's scope decision — one, several, all currently active, or
    not yet known — is recorded exactly as chosen.  A new Commitment Lineage
    holds the statement; this is not a change to an earlier promise.
    """
    recorder = require_human_principal(principal)
    project, org, party, what_was_said = _resolve_verbal_subject(
        session,
        project_id=project_id,
        external_org_id=external_org_id,
        stated_party=stated_party,
        description=description,
        conversation_date=conversation_date,
        new_timing=new_timing,
    )
    return _append_verbal(
        session,
        recorder=recorder,
        project=project,
        external_org=org,
        stated_party=party,
        description=what_was_said,
        conversation_date=conversation_date,
        new_timing=new_timing,
        scope=scope,
        audit_entity_type=audit.COMMITMENT_LINEAGE,
        audit_entity_id=None,
    )


def record_verbal_change(
    session: Session,
    *,
    commitment_lineage_id: int,
    stated_party: str,
    description: str,
    conversation_date: date,
    new_timing: StatementTiming,
    principal: HumanPrincipal,
) -> DependencyEvent:
    """Append a stated Change to Promised Timing on one existing commitment.

    The previous timing is the lineage's current attributable timing — both
    timings are the same party's, so Corridor derives the change and its
    direction rather than inventing an earlier promise from a cached date.  The
    exact prior scope is carried forward as a snapshot; a change never
    re-snapshots an all-active decision or copies the plan onto Constraints.
    """
    recorder = require_human_principal(principal)
    if not isinstance(new_timing, StatementTiming):
        raise VerbalRefusal("a verbal must carry the timing the party gave")
    if not isinstance(conversation_date, date):
        raise VerbalRefusal("a verbal must record the conversation date")
    party = stated_party.strip()
    what_was_said = description.strip()
    if not party:
        raise VerbalRefusal("a verbal must name the party who spoke")
    if not what_was_said:
        raise VerbalRefusal("a verbal must say what the party told you")

    lineage = session.get(CommitmentLineage, commitment_lineage_id)
    if lineage is None:
        raise VerbalRefusal("the commitment being changed no longer exists")
    project = session.get(Project, lineage.project_id)
    if project is None:
        raise VerbalRefusal("the record's project no longer exists")
    lock_project(session, project.id)
    observation = observe_current_statement(session, lineage.id)
    if observation is None:
        raise VerbalRefusal(
            "only an attributable commitment can record a stated change"
        )
    current = observation.event
    if (
        current.stated_external_org_id is None
        or current.new_timing is None
        or observation.scope_decision is None
    ):
        raise VerbalRefusal(
            "only an attributable commitment can record a stated change"
        )
    org = session.get(ExternalParty, current.stated_external_org_id)
    if org is None:
        raise VerbalRefusal("this record has no resolved External Party")
    if is_project_side_party(project, party):
        raise VerbalRefusal(
            f"{party} is the project's own side — an action item, never an "
            "External Party commitment"
        )

    previous = current.new_timing
    previous_timing = StatementTiming(
        text=previous.text,
        precision=previous.precision,
        start_date=previous.start_date,
        end_date=previous.end_date,
    )
    scope_ids = tuple(
        session.scalars(
            select(CommitmentScopeMembership.dependency_id)
            .where(
                CommitmentScopeMembership.scope_decision_id
                == observation.scope_decision.id
            )
            .order_by(CommitmentScopeMembership.dependency_id)
        ).all()
    )
    scope = StatementScope(
        "unknown" if observation.scope_decision.scope_mode == "unknown"
        else "carried_forward",
        scope_ids,
    )
    return _append_verbal(
        session,
        recorder=recorder,
        project=project,
        external_org=org,
        stated_party=party,
        description=what_was_said,
        conversation_date=conversation_date,
        new_timing=new_timing,
        scope=scope,
        previous_timing=previous_timing,
        commitment_lineage_id=lineage.id,
        scope_snapshot_dependency_ids=scope_ids,
        audit_entity_type=audit.COMMITMENT_LINEAGE,
        audit_entity_id=None,
    )


def correct_verbal_scope(
    session: Session,
    *,
    event_id: int,
    scope: StatementScope,
    expected_scope_decision_id: int,
    principal: HumanPrincipal,
) -> CommitmentScopeDecision:
    """Append one scope correction to a recorded verbal, preserving the original.

    The statement fact is untouched; only which Constraints it applies to is
    corrected.  A stale expected scope decision refuses the whole correction and
    a no-op change records nothing attributable.
    """
    recorder = require_human_principal(principal)
    event = session.get(DependencyEvent, event_id)
    if event is None or event.source_kind != "verbal":
        raise VerbalRefusal("there is no recorded verbal statement to correct")
    if event.commitment_lineage_id is None:
        raise VerbalRefusal("only an attributable verbal commitment has scope")
    try:
        with session.begin_nested():
            lock_project(session, event.project_id)
            observation = observe_current_statement(
                session, event.commitment_lineage_id
            )
            if observation is None or observation.event.id != event.id:
                raise StaleVerbalCorrection(
                    "the verbal statement changed; reload the newer state"
                )
            predecessor = observation.scope_decision
            if predecessor is None or predecessor.id != expected_scope_decision_id:
                raise StaleVerbalCorrection(
                    "the verbal statement scope changed; reload the newer state"
                )
            if _scope_is_noop(session, event, predecessor, scope):
                raise VerbalRefusal(
                    "Commitment Scope already has this exact value; "
                    "no correction was recorded"
                )
            decision = record_statement_scope_decision(
                session,
                event_id=event.id,
                scope=scope,
                actor=recorder,
            )
            audit.record(
                session,
                principal=recorder,
                action=audit.CORRECT_STATEMENT_SCOPE,
                entity_type=audit.COMMITMENT_LINEAGE,
                entity_id=event.commitment_lineage_id,
                before={"scope_decision_id": predecessor.id},
                after={
                    "statement_event_id": event.id,
                    "scope_decision_id": decision.id,
                    "scope_mode": decision.scope_mode,
                },
            )
            _correct_verbal_scope_on_spine(
                session, event=event, scope_decision=decision, recorder=recorder
            )
            session.flush()
    except StaleVerbalCorrection:
        raise
    except StatementRefusal as exc:
        raise VerbalRefusal(str(exc)) from exc
    return decision


def _resolve_verbal_subject(
    session: Session,
    *,
    project_id: int,
    external_org_id: int,
    stated_party: str,
    description: str,
    conversation_date: date,
    new_timing: StatementTiming,
) -> tuple[Project, ExternalParty, str, str]:
    """Validate and return the shared verbal subject facts before an append."""
    party = stated_party.strip()
    what_was_said = description.strip()
    if not party:
        raise VerbalRefusal("a verbal must name the party who spoke")
    if not what_was_said:
        raise VerbalRefusal("a verbal must say what the party told you")
    if not isinstance(conversation_date, date):
        raise VerbalRefusal("a verbal must record the conversation date")
    if not isinstance(new_timing, StatementTiming):
        raise VerbalRefusal("a verbal must carry the timing the party gave")
    project = session.get(Project, project_id)
    if project is None:
        raise VerbalRefusal("the record's project no longer exists")
    lock_project(session, project.id)
    org = session.get(ExternalParty, external_org_id)
    if org is None:
        raise VerbalRefusal("this record has no resolved External Party")
    if is_project_side_party(project, party):
        raise VerbalRefusal(
            f"{party} is the project's own side — an action item, never an "
            "External Party commitment"
        )
    return project, org, party, what_was_said


def _append_verbal(
    session: Session,
    *,
    recorder: HumanPrincipal,
    project: Project,
    external_org: ExternalParty | None,
    stated_party: str,
    description: str,
    conversation_date: date,
    new_timing: StatementTiming,
    scope: StatementScope,
    audit_entity_type: str,
    audit_entity_id: int | None,
    previous_timing: StatementTiming | None = None,
    commitment_lineage_id: int | None = None,
    scope_snapshot_dependency_ids: tuple[int, ...] | None = None,
) -> DependencyEvent:
    """Write one verbal statement and its audit receipt as one atomic act."""
    if external_org is None:
        raise VerbalRefusal("this record has no resolved External Party")
    try:
        # The call record and its audit receipt are inseparable: a refusal must
        # not preserve a Verbal whose attributable human act was lost.
        with session.begin_nested():
            event = record_external_party_statement(
                session,
                project_id=project.id,
                affected_external_org_id=external_org.id,
                stated_party=stated_party,
                stated_external_org_id=external_org.id,
                source_kind="verbal",
                event_date=conversation_date,
                description=description,
                new_timing=new_timing,
                previous_timing=previous_timing,
                scope=scope,
                commitment_lineage_id=commitment_lineage_id,
                created_by=recorder.subject,
                _scope_snapshot_dependency_ids=scope_snapshot_dependency_ids,
            )
            recorded = event.new_timing
            after = {
                "dependency_event_id": event.id,
                "commitment_lineage_id": event.commitment_lineage_id,
                "source_kind": event.source_kind,
                "stated_party": stated_party,
                "conversation_date": conversation_date.isoformat(),
                "event_type": event.event_type,
                "timing_text": recorded.text,
                "timing_precision": recorded.precision,
                "scope_mode": event.scope_mode,
            }
            if recorded.precision == "day" and recorded.start_date is not None:
                # The exact-day scalar stays in the receipt for the compatibility
                # projection; a month or approximate promise records no invented
                # calendar day.
                after["committed_date"] = recorded.start_date.isoformat()
            if previous_timing is not None:
                after["timing_direction"] = event.timing_direction
            audit.record(
                session,
                principal=recorder,
                action=audit.RECORD_VERBAL,
                entity_type=audit_entity_type,
                entity_id=(
                    audit_entity_id
                    if audit_entity_id is not None
                    else event.commitment_lineage_id
                ),
                after=after,
            )
            session.flush()
            # Dual-write the same recording onto the spine inside this one
            # atomic act (#451, ADR-0074): the legacy event stays the source of
            # truth until cutover, and the spine gains the attributable,
            # reversible human decision beside it.
            _record_verbal_on_spine(
                session,
                event=event,
                project=project,
                recorder=recorder,
                description=description,
                new_timing=new_timing,
                previous_timing=previous_timing,
            )
    except StatementRefusal as exc:
        raise VerbalRefusal(str(exc)) from exc
    return event


def _record_verbal_on_spine(
    session: Session,
    *,
    event: DependencyEvent,
    project: Project,
    recorder: HumanPrincipal,
    description: str,
    new_timing: StatementTiming,
    previous_timing: StatementTiming | None,
) -> None:
    """Append the verbal's recorder segment, typed facts, and spine decisions.

    The statement's spine subject is its Commitment Lineage, so a stated change
    supersedes the lineage's prior decisions rather than standing beside them:
    the current Project Record reflects the newest statement, exactly as the
    legacy projection does.  Scope that did not change records no attributable
    Applies To no-op (CLAUDE.md; ADR-0039).
    """

    subject_key = f"lineage:{event.commitment_lineage_id}"
    segment = recorded_verbal_statement_segment(
        project_id=project.id,
        statement_id=event.id,
        exact_text=description,
    )
    session.add(segment)
    session.flush([segment])
    timings = (("new", new_timing),) + (
        (("previous", previous_timing),) if previous_timing is not None else ()
    )
    wording = append_recorded_statement_wording_fact(
        session,
        segment=segment,
        subject_key=subject_key,
        description=description,
        recorded_by=recorder.subject,
    )
    timing = append_recorded_statement_timing_fact(
        session,
        segment=segment,
        subject_key=subject_key,
        timings=timings,
        recorded_by=recorder.subject,
    )
    _record_verbal_spine_decision(
        session, project.id, subject_key, wording, event.id, recorder
    )
    _record_verbal_spine_decision(
        session, project.id, subject_key, timing, event.id, recorder
    )
    dependency_ids = tuple(sorted(_verbal_scope_dependency_ids(session, event)))
    current_scope = _current_applies_to_members(session, project.id, subject_key)
    if current_scope is None or current_scope != dependency_ids:
        applies = append_recorded_applies_to_fact(
            session,
            segment=segment,
            subject_key=subject_key,
            dependency_ids=dependency_ids,
            recorded_by=recorder.subject,
        )
        _record_verbal_spine_decision(
            session, project.id, subject_key, applies, event.id, recorder
        )


def _record_verbal_spine_decision(
    session: Session,
    project_id: int,
    subject_key: str,
    fact: Fact,
    event_id: int,
    recorder: HumanPrincipal,
) -> None:
    predecessor = _current_decision_id(
        session, project_id, subject_key, fact.fact_type
    )
    record_human_fact_decision(
        session,
        fact,
        principal=recorder,
        command_type="record_verbal_statement",
        idempotency_key=f"verbal:{event_id}:{fact.fact_type}",
        expected_predecessor=predecessor,
    )


def _correct_verbal_scope_on_spine(
    session: Session,
    *,
    event: DependencyEvent,
    scope_decision: CommitmentScopeDecision,
    recorder: HumanPrincipal,
) -> None:
    """Supersede the lineage's spine Applies To with the corrected scope.

    Forward-only: a verbal recorded before the spine dual-write has no Applies
    To decision to correct, so nothing is written until it is first recorded on
    the spine (cutover is #458). The statement wording and timing are untouched;
    only which Constraints it applies to is superseded, mirroring the legacy
    scope-correction that leaves the statement fact intact.
    """

    subject_key = f"lineage:{event.commitment_lineage_id}"
    predecessor = _current_decision_id(
        session, event.project_id, subject_key, "applies_to"
    )
    segment = _verbal_segment_for_event(session, event.id)
    if predecessor is None or segment is None:
        return
    dependency_ids = tuple(
        sorted(_scope_decision_dependency_ids(session, scope_decision.id))
    )
    applies = append_recorded_applies_to_fact(
        session,
        segment=segment,
        subject_key=subject_key,
        dependency_ids=dependency_ids,
        recorded_by=recorder.subject,
    )
    record_human_fact_decision(
        session,
        applies,
        principal=recorder,
        command_type="correct_statement_scope",
        idempotency_key=f"correct-verbal-scope:{scope_decision.id}",
        expected_predecessor=predecessor,
    )


def _current_decision_id(
    session: Session, project_id: int, subject_key: str, fact_type: str
) -> int | None:
    return session.scalar(
        select(FactDecision.id).where(
            FactDecision.project_id == project_id,
            FactDecision.subject_key == subject_key,
            FactDecision.fact_type == fact_type,
            FactDecision.superseded_by.is_(None),
        )
    )


def _verbal_segment_for_event(
    session: Session, event_id: int
) -> SourceSegment | None:
    return session.scalar(
        select(SourceSegment).where(
            SourceSegment.statement_id == event_id,
            SourceSegment.kind == "recorded_verbal_statement",
        )
    )


def _scope_decision_dependency_ids(
    session: Session, scope_decision_id: int
) -> tuple[int, ...]:
    return tuple(
        session.scalars(
            select(CommitmentScopeMembership.dependency_id).where(
                CommitmentScopeMembership.scope_decision_id == scope_decision_id
            )
        ).all()
    )


def _verbal_scope_dependency_ids(
    session: Session, event: DependencyEvent
) -> tuple[int, ...]:
    scope_decision = session.scalar(
        select(CommitmentScopeDecision).where(
            CommitmentScopeDecision.event_id == event.id
        )
    )
    if scope_decision is None:
        return ()
    return tuple(
        session.scalars(
            select(CommitmentScopeMembership.dependency_id).where(
                CommitmentScopeMembership.scope_decision_id == scope_decision.id
            )
        ).all()
    )


def _current_applies_to_members(
    session: Session, project_id: int, subject_key: str
) -> tuple[int, ...] | None:
    current = session.scalar(
        select(FactDecision).where(
            FactDecision.project_id == project_id,
            FactDecision.subject_key == subject_key,
            FactDecision.fact_type == "applies_to",
            FactDecision.superseded_by.is_(None),
        )
    )
    if current is None:
        return None
    return tuple(
        sorted(
            session.scalars(
                select(FactAppliesTo.dependency_id).where(
                    FactAppliesTo.fact_id == current.fact_id
                )
            ).all()
        )
    )


def _scope_is_noop(
    session: Session,
    event: DependencyEvent,
    predecessor: CommitmentScopeDecision,
    requested: StatementScope,
) -> bool:
    """Whether a requested verbal scope would change nothing at all."""
    if predecessor.scope_mode != requested.mode:
        return False
    current_ids = tuple(
        session.scalars(
            select(CommitmentScopeMembership.dependency_id)
            .where(CommitmentScopeMembership.scope_decision_id == predecessor.id)
            .order_by(CommitmentScopeMembership.dependency_id)
        ).all()
    )
    if requested.mode == "unknown":
        requested_ids: tuple[int, ...] = ()
    elif requested.mode == "selected":
        requested_ids = tuple(sorted(requested.dependency_ids))
    elif requested.mode == "all_active":
        requested_ids = tuple(
            session.scalars(
                select(Dependency.id)
                .where(
                    Dependency.project_id == event.project_id,
                    Dependency.external_org_id == event.affected_external_org_id,
                    Dependency.dismissed_at.is_(None),
                )
                .order_by(Dependency.id)
            ).all()
        )
    else:
        return False
    return current_ids == requested_ids
