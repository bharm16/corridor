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

from corridor.candidate_statement_facts import prepare_candidate_statement_facts
from corridor.identity import is_project_side_party, normalize_party
from corridor.models import (
    Candidate,
    CommitmentLineage,
    Dependency,
    ExternalPartyStatement,
    StatementEvidence,
    CommitmentScopeMembership,
    CommitmentScopeDecision,
    StatementTimingRecord,
    EvidenceLink,
    ExternalParty,
    Project,
)
from corridor.principals import HumanPrincipal, InvalidHumanPrincipal
from corridor.project_lock import lock_project
from corridor.statement_lifecycle import (
    current_scope_decision_filter as current_lifecycle_scope_decision_filter,
    current_statement_event_filter,
)
from corridor.statement_evidence import validate_cited_statement_evidence
from corridor.statement_values import (
    CitedStatementEvidence,
    StatementRefusal,
    StatementScope,
    StatementTiming,
)
from corridor.verify import normalize


_STATEMENT_SCOPE_POLICY_ACTORS = frozenset(
    {
        "corridor:event-admission",
        "corridor:statement-migration-v1",
        # ADR-0054's identifying-language exact tier (#370).
        "corridor:statement-scope-matcher",
    }
)


@dataclass(frozen=True)
class EvidenceBoundPartyResolution:
    """One human-guided binding of source wording to a registered party.

    This is statement-local provenance, not a registry alias.  The shared
    writer revalidates every field before it permits otherwise-unregistered
    source wording to name the selected External Party.
    """

    mode: str
    project_id: int
    candidate_id: int
    stated_party: str
    stated_external_org_id: int
    principal: str
    evidence: tuple[CitedStatementEvidence, ...]

    def as_json(self) -> dict:
        return {
            "mode": self.mode,
            "candidate_id": self.candidate_id,
            "stated_party": self.stated_party.strip(),
            "stated_external_org_id": self.stated_external_org_id,
            "principal": self.principal,
            "evidence": [
                {
                    "document_id": item.document_id,
                    "page_no": item.page_no,
                    "quote": item.quote.strip(),
                }
                for item in self.evidence
            ],
        }


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
    supporting_evidence: tuple[CitedStatementEvidence, ...] = (),
    commitment_lineage_id: int | None = None,
    allow_party_correction: bool = False,
    party_resolution: EvidenceBoundPartyResolution | None = None,
    _scope_snapshot_dependency_ids: tuple[int, ...] | None = None,
) -> ExternalPartyStatement:
    """Append one attributable External Party Commitment or Date Change.

    The caller supplies already-resolved attribution and source provenance.
    This boundary validates the durable facts together, snapshots any known
    scope, creates one event-level citation, and refreshes only exact-day,
    known-scope Dependency projections.
    """
    project = validate_external_party_statement_draft(
        session,
        project_id=project_id,
        stated_party=stated_party,
        stated_external_org_id=stated_external_org_id,
        source_kind=source_kind,
        event_date=event_date,
        description=description,
        new_timing=new_timing,
        previous_timing=previous_timing,
        evidence=evidence,
        supporting_evidence=supporting_evidence,
        party_resolution=party_resolution,
        resolution_principal=created_by,
    )
    lock_project(session, project.id)

    party = stated_party.strip()
    stated = session.get(ExternalParty, stated_external_org_id)
    if stated is None:
        # Re-check after locking the project rather than relying on a stale
        # draft read if an administrator removed an organization concurrently.
        raise StatementRefusal("the affected and stated External Parties must exist")
    if not created_by.strip():
        raise StatementRefusal("a statement must identify who recorded it")

    affected = session.get(ExternalParty, affected_external_org_id)
    if affected is None:
        raise StatementRefusal("the affected and stated External Parties must exist")
    scope_actor = _scope_actor_subject(created_by)
    event_type = "committed_date_change" if previous_timing else "commitment"
    dependency_ids = (
        _resolve_scope_snapshot(
            session,
            project_id=project.id,
            affected_external_org_id=affected.id,
            scope=scope,
            dependency_ids=_scope_snapshot_dependency_ids,
        )
        if _scope_snapshot_dependency_ids is not None
        else _resolve_scope(
            session,
            project_id=project.id,
            affected_external_org_id=affected.id,
            scope=scope,
        )
    )
    # This command is the one atomic writer for every statement path. A
    # database refusal after the event row exists must not leave the caller
    # with an unusable transaction or an event missing its timing, scope,
    # Evidence, or projection.
    with session.begin_nested():
        lineage, predecessor = _commitment_lineage_for_append(
            session,
            project_id=project.id,
            commitment_lineage_id=commitment_lineage_id,
            affected_external_org_id=affected.id,
            stated_external_org_id=stated.id,
            allow_party_correction=allow_party_correction,
        )
        event = ExternalPartyStatement(
            project_id=project.id,
            commitment_lineage_id=lineage.id,
            supersedes_event_id=predecessor.id if predecessor is not None else None,
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
        if predecessor is not None and _lineage_has_coordination_plan(
            session, lineage.id
        ):
            # The receipts remain current history, but changed attribution or
            # timing is not permission to silently call their response apt.
            lineage.plan_needs_review = True
        scope_decision = session.scalar(
            select(CommitmentScopeDecision).where(
                CommitmentScopeDecision.event_id == event.id
            )
        )
        if scope_decision is None:
            raise RuntimeError(
                "statement event did not receive its initial scope decision"
            )
        session.add(
            StatementTimingRecord(
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
                StatementTimingRecord(
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
                CommitmentScopeMembership(
                    event_id=event.id,
                    scope_decision_id=scope_decision.id,
                    dependency_id=dependency_id,
                    recorded_by=scope_actor,
                )
            )
        for cited_evidence in _all_cited_evidence(evidence, supporting_evidence):
            _record_event_evidence(session, event, cited_evidence, created_by)
        session.flush()

        # Imported lazily: projections read the event representation but do
        # not participate in its write validation.
        from corridor.dependency_events import project_committed_dates

        project_committed_dates(session, dependency_ids)
        session.flush()
    return event


def record_external_party_closure(
    session: Session,
    *,
    project_id: int,
    commitment_lineage_id: int,
    source_kind: str,
    event_date: date | None,
    description: str,
    created_by: str,
    evidence: CitedStatementEvidence | None = None,
) -> ExternalPartyStatement:
    """Record an attributable closure for exactly one External Party Commitment.

    Closure ends the party-level statement fact only.  It deliberately does
    not complete, cancel, or otherwise alter a Coordination Plan's internal
    Next Action, and it never derives a Dependency scope from the party.
    """
    project = session.get(Project, project_id)
    if project is None:
        raise StatementRefusal(f"project {project_id} does not exist")
    if source_kind not in {"cited", "verbal"}:
        raise StatementRefusal("a closure has an unknown source kind")
    if not description.strip():
        raise StatementRefusal("a closure must preserve what the party said")
    if not created_by.strip():
        raise StatementRefusal("a closure must identify who recorded it")
    if source_kind == "cited":
        if evidence is None:
            raise StatementRefusal("a cited closure requires its verified Evidence")
        validate_cited_statement_evidence(session, evidence, project.id)
    elif evidence is not None:
        raise StatementRefusal("a Verbal closure cannot be presented as cited Evidence")
    if source_kind == "verbal" and event_date is None:
        raise StatementRefusal("a Verbal closure must preserve the conversation date")

    lock_project(session, project.id)
    lineage = session.get(CommitmentLineage, commitment_lineage_id)
    if lineage is None or lineage.project_id != project.id:
        raise StatementRefusal("Commitment Lineage belongs to another project")
    commitment = _current_commitment_event(session, lineage.id)
    if commitment is None:
        raise StatementRefusal("Commitment Lineage has no accepted statement to close")
    if (
        commitment.affected_external_org_id is None
        or commitment.stated_external_org_id is None
        or commitment.attribution_state != "resolved"
        or not commitment.stated_party
    ):
        raise StatementRefusal(
            "only an attributable External Party Commitment can close"
        )

    with session.begin_nested():
        closure = ExternalPartyStatement(
            project_id=project.id,
            closes_commitment_lineage_id=lineage.id,
            affected_external_org_id=commitment.affected_external_org_id,
            stated_external_org_id=commitment.stated_external_org_id,
            attribution_state="resolved",
            scope_mode="unknown",
            event_type="closure",
            source_kind=source_kind,
            stated_party=commitment.stated_party,
            event_date=event_date,
            description=description.strip(),
            created_by=created_by.strip(),
        )
        session.add(closure)
        session.flush([closure])
        if evidence is not None:
            _record_event_evidence(session, closure, evidence, created_by)
        session.flush()
    return closure


def _record_event_evidence(
    session: Session,
    event: ExternalPartyStatement,
    evidence: CitedStatementEvidence,
    recorded_by: str,
) -> None:
    """Attach one already-validated citation to its one External Party event."""
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
        StatementEvidence(
            evidence_link_id=event_evidence.id,
            event_id=event.id,
            recorded_by=recorded_by.strip(),
        )
    )


def _commitment_lineage_for_append(
    session: Session,
    *,
    project_id: int,
    commitment_lineage_id: int | None,
    affected_external_org_id: int,
    stated_external_org_id: int,
    allow_party_correction: bool = False,
) -> tuple[CommitmentLineage, ExternalPartyStatement | None]:
    """Return the durable statement subject and its current factual tail."""
    if commitment_lineage_id is None:
        lineage = CommitmentLineage(project_id=project_id)
        session.add(lineage)
        session.flush([lineage])
        return lineage, None

    lineage = session.get(CommitmentLineage, commitment_lineage_id)
    if lineage is None or lineage.project_id != project_id:
        raise StatementRefusal("Commitment Lineage belongs to another project")
    predecessor = _current_commitment_event(session, lineage.id)
    if predecessor is None:
        raise StatementRefusal(
            "Commitment Lineage has no accepted statement to correct"
        )
    if not allow_party_correction and (
        predecessor.affected_external_org_id != affected_external_org_id
        or predecessor.stated_external_org_id != stated_external_org_id
    ):
        raise StatementRefusal(
            "Commitment Lineage corrections must keep the same External Parties"
        )
    return lineage, predecessor


def _current_commitment_event(
    session: Session, commitment_lineage_id: int
) -> ExternalPartyStatement | None:
    superseding = ExternalPartyStatement.__table__.alias("superseding")
    return session.scalar(
        select(ExternalPartyStatement)
        .where(
            ExternalPartyStatement.commitment_lineage_id == commitment_lineage_id,
            current_statement_event_filter(ExternalPartyStatement.id),
            ~select(superseding.c.id)
            .where(superseding.c.supersedes_event_id == ExternalPartyStatement.id)
            .exists(),
        )
        .order_by(ExternalPartyStatement.id)
    )


def _lineage_has_coordination_plan(
    session: Session, commitment_lineage_id: int
) -> bool:
    from corridor.models import WorkDecision

    return (
        session.scalar(
            select(WorkDecision.id)
            .where(WorkDecision.commitment_lineage_id == commitment_lineage_id)
            .limit(1)
        )
        is not None
    )


def validate_external_party_statement_draft(
    session: Session,
    *,
    project_id: int,
    stated_party: str,
    stated_external_org_id: int,
    source_kind: str,
    event_date: date | None,
    description: str,
    new_timing: StatementTiming,
    previous_timing: StatementTiming | None = None,
    evidence: CitedStatementEvidence | None = None,
    supporting_evidence: tuple[CitedStatementEvidence, ...] = (),
    party_resolution: EvidenceBoundPartyResolution | None = None,
    resolution_principal: str | None = None,
) -> Project:
    """Validate the statement facts shared by policy, preview, and writer.

    Target selection belongs to the final writer because only it knows the
    affected party and chosen Dependency scope.  Everything a Candidate says
    for itself belongs here, so the worklist never enables a placement whose
    own description, timing, attribution, or cited page the writer rejects.
    """
    project = session.get(Project, project_id)
    if project is None:
        raise StatementRefusal(f"project {project_id} does not exist")
    party = stated_party.strip()
    if not party:
        raise StatementRefusal("a statement must name the party who spoke")
    if not description.strip():
        raise StatementRefusal("a statement must preserve what the party said")
    if source_kind not in {"cited", "verbal"}:
        raise StatementRefusal("a statement has an unknown source kind")
    if source_kind == "cited" and evidence is None:
        raise StatementRefusal("a cited statement requires its verified Evidence")
    if source_kind == "verbal" and evidence is not None:
        raise StatementRefusal("a Verbal cannot be presented as cited Evidence")

    stated = session.get(ExternalParty, stated_external_org_id)
    if stated is None:
        raise StatementRefusal("the affected and stated External Parties must exist")
    registered_spellings = {
        normalize_party(name) for name in (stated.name, *(stated.aliases or [])) if name
    }
    if normalize_party(party) not in registered_spellings:
        _require_evidence_bound_party_resolution(
            session,
            party_resolution,
            project_id=project.id,
            stated_party=party,
            stated_external_org_id=stated.id,
            source_kind=source_kind,
            evidence=_all_cited_evidence(evidence, supporting_evidence),
            resolution_principal=resolution_principal,
        )
    if is_project_side_party(project, party):
        raise StatementRefusal(
            f"{party} is the project's own side — an action item, never an External Party commitment"
        )

    _validate_timing(new_timing)
    if previous_timing is not None:
        _validate_timing(previous_timing)
    if source_kind == "verbal" and event_date is None:
        # A verbal keeps its one source-specific requirement: the recorder must
        # know when the conversation happened.  Its timing precision and scope
        # now follow the same rules as any other attributable statement
        # (ADR-0033, ADR-0036).
        raise StatementRefusal("a Verbal must preserve the conversation date")
    for cited_evidence in _all_cited_evidence(evidence, supporting_evidence):
        validate_cited_statement_evidence(session, cited_evidence, project.id)
    return project


def _require_evidence_bound_party_resolution(
    session: Session,
    resolution: EvidenceBoundPartyResolution | None,
    *,
    project_id: int,
    stated_party: str,
    stated_external_org_id: int,
    source_kind: str,
    evidence: tuple[CitedStatementEvidence, ...],
    resolution_principal: str | None,
) -> None:
    if resolution is None:
        raise StatementRefusal(
            "the stated-party wording does not resolve to the stated External Party"
        )
    candidate = session.get(Candidate, resolution.candidate_id)
    prepared = (
        prepare_candidate_statement_facts(session, candidate)
        if candidate is not None and candidate.project_id == project_id
        else None
    )
    source_party = (
        prepared.source_stated_party_wording if prepared is not None else None
    )
    expected = (
        source_kind == "cited"
        and resolution.mode == "guided_evidence_bound"
        and resolution.project_id == project_id
        and resolution.stated_party.strip() == stated_party.strip()
        and resolution.stated_external_org_id == stated_external_org_id
        and resolution.principal == str(resolution_principal or "").strip()
        and resolution.evidence == evidence
        and prepared is not None
        and isinstance(source_party, str)
        and source_party.strip() == stated_party.strip()
    )
    party_words = stated_party.strip()
    if (
        not expected
        or not party_words
        or not any(party_words in item.quote for item in evidence)
    ):
        raise StatementRefusal(
            "the guided party resolution does not match this statement and its Evidence"
        )


def _all_cited_evidence(
    evidence: CitedStatementEvidence | None,
    supporting_evidence: tuple[CitedStatementEvidence, ...],
) -> tuple[CitedStatementEvidence, ...]:
    """One cited statement can own every verified quote its facts require."""
    cited = (() if evidence is None else (evidence,)) + tuple(supporting_evidence)
    identities = [
        (item.document_id, item.page_no, item.quote.strip()) for item in cited
    ]
    if len(identities) != len(set(identities)):
        raise StatementRefusal("a statement cannot own the same Evidence twice")
    return cited


def record_statement_scope_decision(
    session: Session,
    *,
    event_id: int,
    scope: StatementScope,
    actor: HumanPrincipal | str,
) -> CommitmentScopeDecision:
    """Append a correction or expansion of one statement's Dependency scope.

    The former decision and its links remain readable history.  Readers use
    the one decision not superseded by a later decision when deriving current
    Dependency effects.
    """
    event = session.get(ExternalPartyStatement, event_id)
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
            select(CommitmentScopeMembership.dependency_id).where(
                CommitmentScopeMembership.scope_decision_id == predecessor.id
            )
        ).all()
    )
    with session.begin_nested():
        decision = CommitmentScopeDecision(
            event_id=event.id,
            scope_mode=scope.mode,
            supersedes_scope_decision_id=predecessor.id,
            decided_by=actor_subject,
        )
        session.add(decision)
        session.flush([decision])
        for dependency_id in dependency_ids:
            session.add(
                CommitmentScopeMembership(
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


def _timing_direction(previous: StatementTiming, new: StatementTiming) -> str:
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
            raise StatementRefusal(
                "selected scope cannot include a dismissed Dependency"
            )
    return tuple(scope.dependency_ids)


def _resolve_scope_snapshot(
    session: Session,
    *,
    project_id: int,
    affected_external_org_id: int,
    scope: StatementScope,
    dependency_ids: tuple[int, ...],
) -> tuple[int, ...]:
    """Carry exact current scope through a factual successor without re-choosing it.

    The snapshot is internal to the fact-correction command.  A correction to
    attribution or timing must not silently expand or shrink a separate human
    scope decision merely because active Dependencies changed in the meantime.
    """
    ids = tuple(dependency_ids)
    if len(ids) != len(set(ids)):
        raise StatementRefusal(
            "a preserved Commitment Scope contains a duplicate Dependency"
        )
    if scope.mode == "unknown":
        if ids:
            raise StatementRefusal("unknown scope cannot name Dependencies")
        return ()
    if scope.mode not in {"selected", "all_active", "carried_forward"} or not ids:
        raise StatementRefusal("a preserved Commitment Scope is incomplete")
    dependencies = session.scalars(
        select(Dependency).where(Dependency.id.in_(ids))
    ).all()
    if len(dependencies) != len(ids) or any(
        dependency.project_id != project_id
        or dependency.external_org_id != affected_external_org_id
        for dependency in dependencies
    ):
        raise StatementRefusal(
            "a preserved Commitment Scope no longer belongs to this statement"
        )
    return ids


def _scope_actor_subject(actor: HumanPrincipal | str) -> str:
    """Accept a named human or a deployed Corridor policy, never a role."""
    if isinstance(actor, HumanPrincipal):
        return actor.subject
    if not isinstance(actor, str) or not actor.strip() or actor != actor.strip():
        raise StatementRefusal(
            "a scope decision must name its human or deployed-policy actor"
        )
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
) -> CommitmentScopeDecision | None:
    superseding = CommitmentScopeDecision.__table__.alias("superseding")
    return session.scalar(
        select(CommitmentScopeDecision)
        .where(
            CommitmentScopeDecision.event_id == event_id,
            current_lifecycle_scope_decision_filter(CommitmentScopeDecision.id),
            ~select(superseding.c.id)
            .where(
                superseding.c.supersedes_scope_decision_id == CommitmentScopeDecision.id
            )
            .exists(),
        )
        .order_by(CommitmentScopeDecision.id)
    )
