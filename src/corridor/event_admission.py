"""Policy-authorized admission of extracted events onto the Ledger.

An event Candidate enters through a named, versioned policy an
accountable human authorizes — or through Adjudication — and through
nothing else (ADR-0026). Every check here is a replayable computation:
the quote verified on its page, the type inside the policy, a parseable
date, a reference resolving to exactly one Dependency, the party
matching that Dependency's External Party, and the actor not being the
project's own side. Nothing consults a model. A model may order the
residue or flag an event into it, and may never put one on the record.

The shape is the Carry-Forward family's (ADR-0022), because the problem
is the same one: a human authorizes rules rather than rows, the machine
acts only where it can prove eligibility, and everything it cannot prove
abstains — left pending for Adjudication rather than forced onto the
record. What differs is the act: this writes a DependencyEvent, which
carries an External Party's statement, so the actor boundary is a check
rather than an afterthought.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor import audit
from corridor import policy
from corridor.models import (
    Candidate,
    Dependency,
    DependencyEvent,
    EventAdmissionOutcome,
    PolicyApproval,
    PolicyRun,
    Project,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.project_lock import lock_project

EVENT_ADMISSION_POLICY_VERSION = "event-admission-v1"
FAMILY = "event-admission"
ABSTENTION_REASON_VERSION = "event-admission-abstentions-v1"
MACHINE_ACTOR = "corridor:event-admission"

OUTCOME_ADMITTED = "admitted"
OUTCOME_ABSTAINED = "abstained"

# The types the policy will admit. A response or a status change says
# something happened without stating a date anyone is held to, and the
# date lanes read only these three.
ADMISSIBLE_EVENT_TYPES = ("closure", "commitment", "slip")

ABSTENTION_REASONS = frozenset(
    {
        "citations_unverified",
        "event_type_outside_policy",
        "no_date",
        "unparseable_date",
        "no_conflict_reference",
        "reference_resolves_to_no_dependency",
        "reference_resolves_to_many",
        "party_unstated",
        "party_mismatch",
        "project_side_actor",
    }
)


class EventAdmissionNotAuthorized(RuntimeError):
    """No current approval covers this policy version for this project."""


@dataclass(frozen=True)
class EventAdmissionAbstention:
    candidate_id: int
    reason: str
    reason_version: str = ABSTENTION_REASON_VERSION


@dataclass(frozen=True)
class EventAdmissionResult:
    run_id: int
    admitted_count: int
    abstained_count: int
    abstentions: list[EventAdmissionAbstention] = field(default_factory=list)


def authorize_event_admission(
    session: Session, project_id: int, *, principal: HumanPrincipal
) -> PolicyApproval:
    """Append one human authorization of the current policy version."""
    principal = require_human_principal(principal)
    project = session.get(Project, project_id)
    if project is None:
        raise ValueError(f"project {project_id} does not exist")
    lock_project(session, project_id)

    return policy.record_approval(
        session,
        FAMILY,
        project_id=project_id,
        policy_version=EVENT_ADMISSION_POLICY_VERSION,
        policy_json=_canonical_policy(project),
        principal=principal,
        action=audit.AUTHORIZE_EVENT_ADMISSION,
    )


def current_event_admission_approval(
    session: Session, project_id: int
) -> PolicyApproval | None:
    """The newest approval, and only if it covers the current policy.

    The project's project-side parties are part of the digest, so editing
    them is a policy change like any other.
    """
    return policy.current_approval(
        session,
        FAMILY,
        project_id=project_id,
        policy_version=EVENT_ADMISSION_POLICY_VERSION,
        recompute=lambda project, _approval: _canonical_policy(project),
    )


def run_event_admission(
    session: Session, project_id: int
) -> EventAdmissionResult:
    """Evaluate the authorized policy over the project's pending events."""
    approval = current_event_admission_approval(session, project_id)
    if approval is None:
        raise EventAdmissionNotAuthorized(
            "event admission requires a current authorization of "
            f"{EVENT_ADMISSION_POLICY_VERSION} — authorization covers the "
            "rules, and a changed policy needs a new one"
        )
    lock_project(session, project_id)
    project = session.get(Project, project_id)

    candidates = session.scalars(
        select(Candidate)
        .where(
            Candidate.project_id == project_id,
            Candidate.kind == "event",
            Candidate.state == "pending",
        )
        .order_by(Candidate.id)
    ).all()

    # Every verdict first, then one receipt written with its final counts:
    # the receipt table is immutable, so a run row is never updated after
    # it exists — which is the property that makes it a receipt.
    admissible: list[
        tuple[Candidate, Dependency, dict, date | None, date | None]
    ] = []
    abstentions: list[EventAdmissionAbstention] = []
    for candidate in candidates:
        verdict = _evaluate(session, project, candidate)
        if isinstance(verdict, str):
            abstentions.append(
                EventAdmissionAbstention(
                    candidate_id=candidate.id, reason=verdict
                )
            )
        else:
            dependency, fields, event_date, committed_date = verdict
            admissible.append(
                (candidate, dependency, fields, event_date, committed_date)
            )

    run = PolicyRun(
        project_id=project_id,
        family=FAMILY,
        policy_approval_id=approval.id,
        policy_version=approval.policy_version,
        policy_sha256=approval.policy_sha256,
        abstention_reason_version=ABSTENTION_REASON_VERSION,
        applied_count=len(admissible),
        abstained_count=len(abstentions),
    )
    session.add(run)
    session.flush([run])

    for abstention in abstentions:
        session.add(
            EventAdmissionOutcome(
                policy_run_id=run.id,
                candidate_id=abstention.candidate_id,
                outcome=OUTCOME_ABSTAINED,
                reason=abstention.reason,
            )
        )

    touched: set[int] = set()
    for candidate, dependency, fields, event_date, committed_date in admissible:
        event = DependencyEvent(
            dependency_id=dependency.id,
            event_type=fields["event_type"],
            event_date=event_date,
            committed_date=committed_date,
            description=str(fields.get("description") or ""),
            created_by=MACHINE_ACTOR,
        )
        session.add(event)
        session.flush([event])

        candidate.state = "accepted"
        candidate.adjudicated_at = datetime.now(timezone.utc)
        session.add(
            EventAdmissionOutcome(
                policy_run_id=run.id,
                candidate_id=candidate.id,
                outcome=OUTCOME_ADMITTED,
                dependency_event_id=event.id,
            )
        )
        audit.record(
            session,
            actor=MACHINE_ACTOR,
            action=audit.ADMIT_EVENT,
            entity_type=audit.DEPENDENCY,
            entity_id=dependency.id,
            after={
                "policy_run_id": run.id,
                "candidate_id": candidate.id,
                "dependency_event_id": event.id,
                "policy_sha256": approval.policy_sha256,
            },
        )
        touched.add(dependency.id)

    for dependency_id in sorted(touched):
        _project_committed_date(session, dependency_id)

    session.flush()
    return EventAdmissionResult(
        run_id=run.id,
        admitted_count=len(admissible),
        abstained_count=len(abstentions),
        abstentions=abstentions,
    )


def _evaluate(
    session: Session, project: Project, candidate: Candidate
) -> str | tuple[Dependency, dict, date | None, date | None]:
    """Every check, in order. A string is the abstention reason."""
    if not candidate.citations_verified:
        return "citations_unverified"

    fields = (candidate.payload_json or {}).get("fields", {})
    if fields.get("event_type") not in ADMISSIBLE_EVENT_TYPES:
        return "event_type_outside_policy"

    # Two dates, kept two: when the party spoke, and what they promised.
    # A missing meeting date is recorded as missing — never filled in
    # from the promised date, because the promised date is not evidence
    # of when anything was said, and the Committed Date projection
    # orders by exactly that.
    raw_event_date = fields.get("event_date")
    raw_committed_date = fields.get("committed_date")
    if not raw_event_date and not raw_committed_date:
        return "no_date"
    event_date = _parse_date(raw_event_date) if raw_event_date else None
    committed_date = (
        _parse_date(raw_committed_date) if raw_committed_date else None
    )
    if (raw_event_date and event_date is None) or (
        raw_committed_date and committed_date is None
    ):
        return "unparseable_date"

    ref = fields.get("conflict_ref")
    if not ref:
        return "no_conflict_reference"

    matches = session.scalars(
        select(Dependency).where(
            Dependency.project_id == project.id,
            Dependency.source_ref == str(ref),
        )
    ).all()
    if not matches:
        return "reference_resolves_to_no_dependency"
    if len(matches) > 1:
        return "reference_resolves_to_many"
    [dependency] = matches

    org = str(fields.get("external_org") or "").strip()
    if not org:
        return "party_unstated"
    if _is_project_side(project, org):
        # The project's own engineer taking an action item is a Next
        # Action's territory, never an External Party's commitment
        # (ADR-0026). Adjudication may still record it by hand.
        return "project_side_actor"
    if not _party_matches(session, dependency, org):
        # Alias resolution is Adjudication's judgment, not the policy's.
        return "party_mismatch"

    return dependency, fields, event_date, committed_date


def _is_project_side(project: Project, org: str) -> bool:
    stated = project.project_side_parties or []
    return any(org.casefold() == str(p).casefold() for p in stated)


def _party_matches(
    session: Session, dependency: Dependency, org: str
) -> bool:
    if dependency.external_org_id is None:
        return False
    from corridor.models import ExternalOrg

    external = session.get(ExternalOrg, dependency.external_org_id)
    if external is None:
        return False
    names = [external.name, *(external.aliases or [])]
    return any(org.casefold() == str(n).casefold() for n in names if n)


def _project_committed_date(session: Session, dependency_id: int) -> None:
    """The Dependency's Committed Date, projected from its events.

    The most recently *stated* commitment wins — ordered by when the
    party said it, not by which promised date is furthest out, because a
    party pulling a date earlier is as real as a party slipping it and
    both are the same act: a newer statement replacing an older one. The
    projection is the same shape a Work Decision's current values take:
    queryable, recomputable from the appended events beneath it, so a
    divergence is a defect a consistency check catches rather than a
    second source of truth.
    """
    latest = session.scalars(
        select(DependencyEvent)
        .where(
            DependencyEvent.dependency_id == dependency_id,
            DependencyEvent.event_type.in_(("commitment", "slip")),
            DependencyEvent.committed_date.is_not(None),
        )
        .order_by(
            DependencyEvent.event_date.desc().nulls_last(),
            DependencyEvent.id.desc(),
        )
        .limit(1)
    ).first()
    if latest is None:
        return
    dependency = session.get(Dependency, dependency_id)
    if dependency is not None:
        dependency.committed_date = latest.committed_date


def _parse_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _rule_source_bytes() -> tuple[tuple[str, bytes], ...]:
    """The deployed bytes of the code that decides admission.

    ADR-0022's guarantee, inherited here: a digest over configuration
    alone would let someone loosen a check — widen a date format, relax
    the party match — and keep running under an authorization no one
    re-read. The authorization covers the rules, and the rules are code.
    """
    from pathlib import Path

    from corridor import models as models_module
    from corridor import principals as principals_module

    paths = (
        ("corridor.event_admission", Path(__file__)),
        ("corridor.audit", Path(audit.__file__)),
        ("corridor.policy", Path(policy.__file__)),
        ("corridor.models", Path(models_module.__file__)),
        ("corridor.principals", Path(principals_module.__file__)),
        (
            "corridor.migrations.c7d2f5a83b46",
            Path(__file__).parent
            / "migrations/versions/c7d2f5a83b46_add_event_admission.py",
        ),
    )
    return tuple((name, path.read_bytes()) for name, path in paths)




def _rules_digest() -> str:
    return policy.digest_of_sources(_rule_source_bytes)


def _canonical_policy(project: Project) -> dict:
    """What the approval is approving — the rules, exactly.

    Three things move this digest, and each must pause the policy until
    a principal authorizes the replacement: the stated configuration,
    the project's project-side parties (they decide which events may
    carry a commitment), and the deployed bytes of the deciding code.
    """
    return {
        "policy_version": EVENT_ADMISSION_POLICY_VERSION,
        "abstention_reason_version": ABSTENTION_REASON_VERSION,
        "admissible_event_types": list(ADMISSIBLE_EVENT_TYPES),
        "abstention_reasons": sorted(ABSTENTION_REASONS),
        "project_side_parties": sorted(
            str(p) for p in (project.project_side_parties or [])
        ),
        "checks": [
            "citations_verified",
            "event_type_admissible",
            "date_present_and_parseable",
            "reference_resolves_to_exactly_one_dependency",
            "party_stated_and_matching",
            "actor_is_not_project_side",
        ],
        "rules_digest_method": "sha256-rule-source-files-v1",
        "rules_digest": _rules_digest(),
    }


