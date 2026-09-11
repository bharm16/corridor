"""The statement coordination screens' reading: which screen, and its facts.

The two screens were rendered from the end of a two-hundred-and-ten line route
body in `web/app.py`: eight inline ORM queries, a suggestion-rank sort, a
`ScopeMatchCandidate` projection and a second projection of the Constraint rows
written inside a thirty-one key dict literal. Card G-01 made that body a named
reading — `StatementCoordinationView`, carrying its own template name — but
left it in the route module, because a `web/statement_view.py` naming
`Candidate`, `CommitmentLineage` and `Dependency` is a new consumer of three
frozen tables as far as the ADR-0081 stage 4 census can tell, and the census
refuses one.

It is not a new consumer. These are the same reads, in a different file, and
`tests/ratchet_support.py` is the mechanism that can tell the difference:
`RELOCATED_LEGACY_READINGS` in `tests/test_architecture.py` declares this move
and the merge base proves it. The declaration is spent by the merge that uses
it; from then on this module is an ordinary consumer, counted like every other,
and it retires with the legacy readers it reads through.

Nothing here decides anything about the record, and nothing here renders. The
route keeps `TEMPLATES.TemplateResponse` and the refusal sentence a write path
just raised, which is the route's own and not a fact about the record. This is
the shape `corridor.web.queue_view`, `corridor.web.dependency_view`,
`corridor.web.operations_view`, `corridor.web.follow_up_view` and
`corridor.web.issue_section` already use for their screens.

Terminology: nothing here coins a customer word. Constraint, Extracted
Proposal, Commitment and Follow-up Plan come from the adopted glossary through
the readers below.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor import audit
from corridor.candidate_statement_facts import (
    CandidateStatementFacts,
    prepare_candidate_statement_facts,
)
from corridor.models import (
    AuditLog,
    Candidate,
    CandidateDisposition,
    CommitmentLineage,
    Dependency,
    EventAdmissionOutcome,
    ExternalOrg,
    Milestone,
    Project,
    ProjectRosterEntry,
    StatementCoordinationReceipt,
    StatementCoordinationReversal,
)
from corridor.presentation import authority_gap_label, read_guided_save_offer
from corridor.statement_coordination import (
    STATEMENT_NEXT_ACTION_CHOICES,
    AdmittedStatementCoordination,
    pending_statement_authority_gap,
    read_admitted_statement_coordination,
)
from corridor.statement_lifecycle import (
    current_candidate_disposition,
    current_lineage_statement,
)
from corridor.statement_scope_match import (
    ScopeMatchCandidate,
    read_statement_scope_match_card,
    scope_match_card_data,
)
from corridor.statement_suggestions import read_statement_suggestions
from corridor.web.statement_forms import (
    CANDIDATE_EVIDENCE_UNAVAILABLE,
    candidate_statement_evidence_view,
)
from corridor.work_decisions import (
    CANCELLATION_REASONS,
    DEFERRAL_REASONS,
    NO_FOLLOW_UP_REASONS,
    UNKNOWN_DUE_DATE_REASONS,
)


def active_statement_coordination_receipt(
    session: Session, candidate_id: int
) -> StatementCoordinationReceipt | None:
    return session.scalar(
        select(StatementCoordinationReceipt)
        .outerjoin(
            StatementCoordinationReversal,
            StatementCoordinationReversal.receipt_id
            == StatementCoordinationReceipt.id,
        )
        .where(
            StatementCoordinationReceipt.candidate_id == candidate_id,
            StatementCoordinationReversal.id.is_(None),
        )
        .order_by(StatementCoordinationReceipt.id.desc())
        .limit(1)
    )


# The two statement coordination screens. Which one a request lands on is a
# fact about the record, so the reading below names it (`QueueView` does the
# same for `queue.html` and `empty.html`).
ADMITTED_TEMPLATE = "statement_admitted_coordinate.html"
PROPOSED_TEMPLATE = "statement_coordinate.html"


@dataclass(frozen=True, slots=True)
class StatementCoordinationView:
    """One statement coordination request's reading, and the screen it is for.

    `statement_coordinate.html` required thirty names that nothing declared,
    and they were assembled as a thirty-one key dict literal at the end of a
    two-hundred-and-ten line handler body: eight inline ORM queries, a
    suggestion-rank sort, a `ScopeMatchCandidate` projection, and a second
    projection of the Constraint rows written inside the literal itself. What
    a caller had to know in order to render the screen was discoverable only
    by reading that body, and the environment's default `Undefined` renders a
    missing scalar or a missing sequence as nothing at all, so a dropped key
    produced a blank section rather than a failure.

    The fields below are those two templates' interface, in the shape
    `corridor.web.queue_view`, `corridor.web.dependency_view`,
    `corridor.web.operations_view`, `corridor.web.follow_up_view` and
    `corridor.web.issue_section` already use for their screens: one frozen
    reading, carrying the template it is for, with no decision of its own. A
    field only one screen prints defaults to the empty reading of its kind, so
    the other screen's `{% set %}` prelude binds a real value rather than the
    environment's `Undefined`.

    `AdmittedStatementCoordination` already declares everything the residual
    screen prints about the accepted Commitment, so it is carried whole rather
    than flattened into loose names beside it.
    """

    project: Project
    candidate: Candidate
    template: str

    # --- the residual coordination screen ---------------------------------
    admitted: AdmittedStatementCoordination | None = None
    plan_unknown_date_reasons: Sequence[str] = ()
    plan_no_follow_up_reasons: Sequence[str] = ()
    plan_cancellation_reasons: Sequence[str] = ()
    plan_deferral_reasons: Sequence[str] = ()

    # --- the proposed statement screen ------------------------------------
    candidate_fields: Mapping[str, Any] | None = None
    candidate_party: str = ""
    candidate_affected_party_id: int | None = None
    candidate_stated_party: str = ""
    candidate_stated_party_id: int | None = None
    candidate_description: str = ""
    candidate_event_date: str = ""
    candidate_timing: Mapping[str, str | bool] | None = None
    guided_save_available: bool = False
    guided_save_refusal: str | None = None
    next_action_choices: Sequence[str] = ()
    scope_match_card: Any = None
    candidate_evidence: Sequence[Any] = ()
    candidate_evidence_available: bool = False
    candidate_evidence_unavailable_message: str = CANDIDATE_EVIDENCE_UNAVAILABLE
    statement_suggestions: Sequence[Any] = ()
    dependencies: Sequence[Mapping[str, Any]] = ()
    roster: Sequence[ProjectRosterEntry] = ()
    parties: Sequence[ExternalOrg] = ()
    milestones: Sequence[Milestone] = ()
    receipt: StatementCoordinationReceipt | None = None
    event: Any = None
    line: CommitmentLineage | None = None
    not_relevant: CandidateDisposition | None = None
    history: Sequence[Mapping[str, Any]] = ()
    pending_authority_gap: Any = None
    pending_authority_gap_label: str | None = None
    unresolved_acknowledgment: AuditLog | None = None


def read_statement_coordination(
    session: Session, *, project: Project, candidate: Candidate
) -> StatementCoordinationView:
    """Read one statement coordination request: which screen, and its facts.

    Nothing here decides anything about the record.
    `read_admitted_statement_coordination` owns whether a statement is already
    in the record and what one residual decision it still needs;
    `prepare_candidate_statement_facts` owns what the source supports;
    `statement_scope_match` owns the narrowed-set card;
    `read_guided_save_offer` owns whether the guided Save is offered, as one
    value with its own refusal sentence (ADR-0039); `statement_suggestions`
    owns which similarities may order the Constraint list and never preselects
    one. This reads each once and arranges the answers.
    """

    admitted = read_admitted_statement_coordination(
        session, project.id, candidate.id
    )
    if admitted is not None:
        return StatementCoordinationView(
            project=project,
            candidate=candidate,
            template=ADMITTED_TEMPLATE,
            admitted=admitted,
            plan_unknown_date_reasons=sorted(UNKNOWN_DUE_DATE_REASONS),
            plan_no_follow_up_reasons=sorted(NO_FOLLOW_UP_REASONS),
            plan_cancellation_reasons=sorted(CANCELLATION_REASONS),
            plan_deferral_reasons=sorted(DEFERRAL_REASONS),
        )
    receipt = (
        active_statement_coordination_receipt(session, candidate.id)
        if candidate.state == "accepted"
        else None
    )
    event = (
        current_lineage_statement(session, receipt.commitment_lineage_id)
        if receipt
        else None
    )
    line = (
        session.get(CommitmentLineage, receipt.commitment_lineage_id)
        if receipt
        else None
    )
    candidate_facts = prepare_candidate_statement_facts(session, candidate)
    statement_suggestions = (
        read_statement_suggestions(session, project.id, candidate.id)
        if candidate.state == "pending" and candidate.citations_verified
        else ()
    )
    candidate_evidence = candidate_statement_evidence_view(candidate_facts)
    candidate_evidence_available = candidate_facts.evidence_is_reviewable
    dependencies = _ordered_scope_dependencies(
        session, project, statement_suggestions
    )
    roster = session.scalars(
        select(ProjectRosterEntry)
        .where(
            ProjectRosterEntry.project_id == project.id,
            ProjectRosterEntry.active.is_(True),
        )
        .order_by(ProjectRosterEntry.display_name)
    ).all()
    parties = list(
        session.scalars(select(ExternalOrg).order_by(ExternalOrg.name)).all()
    )
    milestones = session.scalars(
        select(Milestone)
        .where(Milestone.project_id == project.id)
        .order_by(Milestone.code)
    ).all()
    fields = candidate_facts.fields
    candidate_affected_party_id = (
        candidate_facts.affected_party.visible_external_org_id
    )
    candidate_timing = _candidate_statement_timing_view(candidate_facts)
    guided_save_offer = read_guided_save_offer(
        evidence_available=candidate_evidence_available,
        timing_available=bool(candidate_timing["available"]),
        roster_available=bool(roster),
    )
    disposition = current_candidate_disposition(session, candidate.id)
    not_relevant = (
        disposition
        if candidate.state == "rejected"
        and disposition is not None
        and disposition.disposition == "not_relevant"
        else None
    )
    pending_authority_gap = pending_statement_authority_gap(
        session,
        project.id,
        candidate.id,
    )
    unresolved_acknowledgment = session.scalar(
        select(AuditLog)
        .where(
            AuditLog.entity_type == audit.CANDIDATE,
            AuditLog.entity_id == candidate.id,
            AuditLog.action == audit.KEEP_STATEMENT_UNRESOLVED,
        )
        .order_by(AuditLog.id.desc())
        .limit(1)
    )
    return StatementCoordinationView(
        project=project,
        candidate=candidate,
        template=PROPOSED_TEMPLATE,
        candidate_fields=fields,
        candidate_party=candidate_facts.affected_party.wording,
        candidate_affected_party_id=candidate_affected_party_id,
        candidate_stated_party=candidate_facts.stated_party.wording,
        candidate_stated_party_id=(
            candidate_facts.stated_party.visible_external_org_id
        ),
        candidate_description=str(fields.get("description") or ""),
        candidate_event_date=str(fields.get("event_date") or ""),
        candidate_timing=candidate_timing,
        # One value for the offer (ADR-0039): the template renders it and
        # adds no condition of its own.
        guided_save_available=guided_save_offer.available,
        guided_save_refusal=guided_save_offer.refusal,
        next_action_choices=STATEMENT_NEXT_ACTION_CHOICES,
        scope_match_card=_statement_scope_match_card(
            session,
            candidate,
            dependencies=dependencies,
            candidate_affected_party_id=candidate_affected_party_id,
        ),
        candidate_evidence=candidate_evidence,
        candidate_evidence_available=candidate_evidence_available,
        statement_suggestions=statement_suggestions,
        dependencies=[
            {
                "id": dependency.id,
                "ref_code": dependency.ref_code,
                "source_ref": dependency.source_ref,
                "title": dependency.title,
                "location_desc": dependency.location_desc,
                "station_from": dependency.station_from,
                "station_to": dependency.station_to,
                "external_org_id": dependency.external_org_id,
                "external_org_name": (
                    external_org_name or "Organization not identified"
                ),
            }
            for dependency, external_org_name in dependencies
        ],
        roster=roster,
        parties=parties,
        milestones=milestones,
        receipt=receipt,
        event=event,
        line=line,
        not_relevant=not_relevant,
        history=_statement_coordination_history(session, candidate.id),
        pending_authority_gap=pending_authority_gap,
        pending_authority_gap_label=(
            authority_gap_label(pending_authority_gap.code)
            if pending_authority_gap is not None
            else None
        ),
        unresolved_acknowledgment=unresolved_acknowledgment,
    )


def _ordered_scope_dependencies(
    session: Session, project: Project, suggestions: Sequence[Any]
) -> list[tuple[Dependency, str | None]]:
    """This project's open Constraints, in the order the choices are offered.

    Suggestions may reorder the choice list; they never preselect an entry,
    and a protected or ineligible statement keeps the plain ref-code order.
    """
    rows = session.execute(
        select(Dependency, ExternalOrg.name)
        .outerjoin(ExternalOrg, ExternalOrg.id == Dependency.external_org_id)
        .where(
            Dependency.project_id == project.id,
            Dependency.dismissed_at.is_(None),
        )
        .order_by(Dependency.ref_code)
    ).all()
    suggestion_rank = {
        suggestion.dependency_id: rank
        for rank, suggestion in enumerate(suggestions)
    }
    return sorted(
        rows,
        key=lambda row: (
            suggestion_rank.get(row[0].id, len(suggestion_rank)),
            row[0].ref_code,
        ),
    )


def _statement_scope_match_card(
    session: Session,
    candidate: Candidate,
    *,
    dependencies: Sequence[tuple[Dependency, str | None]],
    candidate_affected_party_id: int | None,
):
    """The narrowed-set card, or nothing.

    It renders only what the matcher's own abstention receipt recorded (#370,
    ADR-0054). This screen re-derives nothing: no recorded ambiguous
    abstention, no card, and the ordinary explicit scope choices remain the
    only way to record scope.
    """
    scope_abstention = session.scalar(
        select(EventAdmissionOutcome)
        .where(
            EventAdmissionOutcome.candidate_id == candidate.id,
            EventAdmissionOutcome.outcome == "abstained",
        )
        .order_by(EventAdmissionOutcome.id.desc())
        .limit(1)
    )
    scope_card_data = (
        scope_match_card_data(scope_abstention.eligibility_json)
        if scope_abstention is not None
        else None
    )
    if scope_card_data is None:
        return None
    return read_statement_scope_match_card(
        scope_card_data,
        tuple(
            ScopeMatchCandidate(
                dependency_id=dependency.id,
                ref_code=dependency.ref_code,
                source_ref=dependency.source_ref,
                title=dependency.title,
                location=dependency.location_desc,
                station_from=dependency.station_from,
                station_to=dependency.station_to,
            )
            for dependency, _external_org_name in dependencies
            if candidate_affected_party_id is not None
            and dependency.external_org_id == candidate_affected_party_id
        ),
    )


def _candidate_statement_timing_view(
    facts: CandidateStatementFacts,
) -> dict[str, str | bool]:
    """Expose supported Candidate timing read-only; never ask for transcription."""
    timing = facts.new_timing.visible_timing
    return {
        "available": facts.new_timing.is_visible,
        "text": timing.text if timing is not None else "",
        "precision": timing.precision if timing is not None else "",
        "start_date": (
            timing.start_date.isoformat()
            if timing is not None and timing.start_date is not None
            else ""
        ),
        "end_date": (
            timing.end_date.isoformat()
            if timing is not None and timing.end_date is not None
            else ""
        ),
    }


def _statement_coordination_history(session: Session, candidate_id: int) -> tuple[dict, ...]:
    """Render durable lifecycle acts in plain time order without hiding reversals."""
    receipts = session.scalars(
        select(StatementCoordinationReceipt)
        .where(StatementCoordinationReceipt.candidate_id == candidate_id)
        .order_by(StatementCoordinationReceipt.id)
    ).all()
    dispositions = session.scalars(
        select(CandidateDisposition)
        .where(CandidateDisposition.candidate_id == candidate_id)
        .order_by(CandidateDisposition.id)
    ).all()
    reversals = session.scalars(
        select(StatementCoordinationReversal)
        .where(StatementCoordinationReversal.candidate_id == candidate_id)
        .order_by(StatementCoordinationReversal.id)
    ).all()
    unresolved = session.scalars(
        select(AuditLog)
        .where(
            AuditLog.entity_type == audit.CANDIDATE,
            AuditLog.entity_id == candidate_id,
            AuditLog.action == audit.KEEP_STATEMENT_UNRESOLVED,
        )
        .order_by(AuditLog.id)
    ).all()
    rows = [
        {
            "created_at": receipt.created_at,
            "label": "Saved statement and Follow-up plan",
            "detail": f"grouping receipt {receipt.id}",
        }
        for receipt in receipts
    ]
    rows.extend(
        {
            "created_at": disposition.created_at,
            "label": (
                "Not added to project record"
                if disposition.disposition == "not_relevant"
                else "Recorded proposed statement"
            ),
            "detail": disposition.reason.replace("_", " ") if disposition.reason else "",
        }
        for disposition in dispositions
    )
    rows.extend(
        {
            "created_at": reversal.created_at,
            "label": (
                "Undid guided Save"
                if reversal.receipt_id is not None
                else "Restored extracted statement"
            ),
            "detail": (
                f"grouping receipt {reversal.receipt_id}"
                if reversal.receipt_id is not None
                else f"disposition {reversal.candidate_disposition_id}"
            ),
        }
        for reversal in reversals
    )
    rows.extend(
        {
            "created_at": entry.ts,
            "label": "Recorded unresolved authority gap",
            "detail": authority_gap_label(
                str((entry.after_json or {}).get("authority_gap") or "")
            ),
        }
        for entry in unresolved
    )
    return tuple(sorted(rows, key=lambda row: (row["created_at"], row["label"])))
