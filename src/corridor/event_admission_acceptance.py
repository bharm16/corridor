"""Prove and activate ADR-0042's exact unknown-scope Admission class.

The proof runs on two disposable clones of one pinned real project state.  The
predecessor and opt-in policies therefore read the same declared population,
while the shared source database receives only the immutable acceptance receipt
and, when every gate passes, one append-only activation act.  A failed gate is
durable evidence and never changes normal processing.

A reduced synthetic fixture was rejected because it could not prove the real
Active Run population, cross-project guards, or exact shared-operation path.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from corridor import audit, identity, policy
from corridor.event_admission import (
    EVENT_ADMISSION_POLICY_VERSION,
    UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
    UNKNOWN_SCOPE_POLICY_VERSION,
    _acceptance_receipt_is_current,
    _canonical_policy,
    run_event_admission,
)
from corridor.external_statements import (
    CitedStatementEvidence,
    StatementRefusal,
    validate_cited_statement_evidence,
)
from corridor.models import (
    ActiveRunDeclaration,
    AuditLog,
    Candidate,
    CandidateDisposition,
    CommitmentLineage,
    Dependency,
    DependencyEvent,
    DependencyEventEvidence,
    DependencyEventScope,
    DependencyEventScopeDecision,
    Document,
    EventAdmissionAcceptanceReceipt,
    EventAdmissionActivation,
    EventAdmissionOutcome,
    EvidenceLink,
    PolicyRun,
    Project,
    ReportRun,
    WorkDecision,
)
from corridor.m8_acceptance_database import (
    DatabaseProvisioner,
    provision_disposable_postgres,
    read_migration_head,
)
from corridor.rehearsal_environment import SealedRehearsalEnvironment
from corridor.sh99_admission_acceptance import (
    REPO_ROOT,
    _provision_database,
)
from corridor.supersession import actionable_candidate_query
from corridor.work_list import build_work_list


SELECTION_RULE = "current-active-run-pending-event-candidates-v1"
RECEIPT_VERSION = "corridor.event-admission-unknown-scope-acceptance.v1"
ACTIVATION_ACTOR = "corridor:event-admission-activation"
PROMOTION_GATE_NAMES = frozenset(
    {
        "eligible_case_observed",
        "zero_false_party_attribution",
        "zero_false_dependency_scope",
        "zero_project_side_masquerade",
        "zero_cross_project_references",
        "zero_unauthorized_work_decisions",
        "zero_duplicates",
        "zero_protected_state_changes",
        "all_evidence_and_receipts_valid",
        "all_admissions_enter_residual_work",
        "fresh_and_predecessor_migrations_passed",
    }
)


@dataclass(frozen=True)
class EventAdmissionAcceptanceConfig:
    project_slug: str
    source_database_url: str
    postgres_admin_url: str
    expected_clean_git_revision: str


@dataclass(frozen=True)
class EventAdmissionAcceptanceResult:
    receipt_id: int
    status: str
    activated: bool
    receipt_sha256: str
    source_revision: str
    migration_head: str
    clone_database_names: tuple[str, str]


def run_event_admission_acceptance(
    config: EventAdmissionAcceptanceConfig,
    *,
    provision_database: DatabaseProvisioner | None = None,
) -> EventAdmissionAcceptanceResult:
    """Replay both policy versions and append only the resulting gate receipt."""
    provision = provision_database or _provision_database
    rehearsal = SealedRehearsalEnvironment.open(
        source_database_url=config.source_database_url,
        expected_checkout_revision=config.expected_clean_git_revision,
        repo_root=REPO_ROOT,
    )
    source = rehearsal.checkout
    migration_head = rehearsal.checkout_migration_head
    migration_rehearsal = _rehearse_predecessor_upgrade(
        config.postgres_admin_url, expected_head=migration_head
    )
    with tempfile.TemporaryDirectory(prefix="corridor-event-admission-acceptance-") as parent:
        dump_path = Path(parent) / "source.dump"
        rehearsal.capture(dump_path)
        source_dump_sha256 = hashlib.sha256(dump_path.read_bytes()).hexdigest()
        clone_names: list[str] = []

        with provision(config.postgres_admin_url) as predecessor_database:
            clone_names.append(predecessor_database.name)
            if predecessor_database.migration_head != migration_head:
                raise ValueError("predecessor clone migration head does not match source")
            rehearsal.restore(dump_path, predecessor_database.name)
            predecessor = _run_policy_clone(
                rehearsal.clone_url(
                    config.postgres_admin_url, predecessor_database.name
                ),
                config.project_slug,
                EVENT_ADMISSION_POLICY_VERSION,
                repeat=False,
            )

        with provision(config.postgres_admin_url) as opt_in_database:
            clone_names.append(opt_in_database.name)
            if opt_in_database.migration_head != migration_head:
                raise ValueError("opt-in clone migration head does not match source")
            rehearsal.restore(dump_path, opt_in_database.name)
            opt_in = _run_policy_clone(
                rehearsal.clone_url(
                    config.postgres_admin_url, opt_in_database.name
                ),
                config.project_slug,
                UNKNOWN_SCOPE_POLICY_VERSION,
                repeat=True,
            )
    migration_rehearsal = {
        **migration_rehearsal,
        "fresh_head": migration_head,
        "fresh_status": "passed",
    }

    if predecessor["population"] != opt_in["population"]:
        raise ValueError("predecessor and opt-in policies did not read one population")
    receipt_json = _acceptance_receipt_json(
        source_revision=source["revision"],
        migration_head=migration_head,
        source_dump_sha256=source_dump_sha256,
        predecessor=predecessor,
        opt_in=opt_in,
        migration_rehearsal=migration_rehearsal,
    )
    engine = create_engine(config.source_database_url, poolclass=NullPool, future=True)
    try:
        with Session(engine) as session:
            project = session.scalar(
                select(Project).where(Project.slug == config.project_slug)
            )
            if project is None:
                raise ValueError(f"no project with slug {config.project_slug!r}")
            stored = record_acceptance_receipt(
                session,
                project_id=project.id,
                source_revision=source["revision"],
                migration_head=migration_head,
                receipt_json=receipt_json,
            )
            activation = activate_passing_acceptance(session, stored.id)
            session.commit()
            return EventAdmissionAcceptanceResult(
                receipt_id=stored.id,
                status=stored.status,
                activated=activation is not None,
                receipt_sha256=stored.receipt_sha256,
                source_revision=source["revision"],
                migration_head=migration_head,
                clone_database_names=(clone_names[0], clone_names[1]),
            )
    finally:
        engine.dispose()


def record_acceptance_receipt(
    session: Session,
    *,
    project_id: int,
    source_revision: str,
    migration_head: str,
    receipt_json: dict,
) -> EventAdmissionAcceptanceReceipt:
    """Persist one immutable pass or failure without relabelling its gates."""
    project = session.get(Project, project_id)
    if project is None:
        raise ValueError(f"project {project_id} does not exist")
    gates = receipt_json.get("gates")
    if not isinstance(gates, dict) or set(gates) != PROMOTION_GATE_NAMES:
        raise ValueError("acceptance receipt must state every promotion gate")
    if any(not isinstance(value, bool) for value in gates.values()):
        raise ValueError("acceptance promotion gates must be boolean")
    opt_in = receipt_json.get("opt_in")
    if not isinstance(opt_in, dict) or gates != _receipt_promotion_gates(receipt_json):
        raise ValueError("acceptance promotion gates do not match recorded metrics")
    if (
        receipt_json.get("schema_version") != RECEIPT_VERSION
        or receipt_json.get("selection_rule") != SELECTION_RULE
        or receipt_json.get("policy_version") != UNKNOWN_SCOPE_POLICY_VERSION
        or receipt_json.get("reason_version")
        != UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION
    ):
        raise ValueError("acceptance receipt identity does not match this gate")
    if (
        receipt_json.get("source_revision") != source_revision
        or receipt_json.get("migration_head") != migration_head
        or (receipt_json.get("migration_rehearsal") or {}).get("head")
        != migration_head
    ):
        raise ValueError("acceptance receipt source or migration pin does not match")
    status = "passed" if all(value is True for value in gates.values()) else "failed"
    policy_json = _canonical_policy(project, UNKNOWN_SCOPE_POLICY_VERSION)
    policy_sha256 = policy.canonical_sha256(policy_json)
    if receipt_json.get("policy_sha256") != policy_sha256:
        raise ValueError("acceptance receipt policy digest does not match deployed rules")
    canonical = {**receipt_json, "status": status}
    stored = EventAdmissionAcceptanceReceipt(
        project_id=project.id,
        status=status,
        source_revision=source_revision,
        migration_head=migration_head,
        predecessor_policy_version=EVENT_ADMISSION_POLICY_VERSION,
        policy_version=UNKNOWN_SCOPE_POLICY_VERSION,
        policy_sha256=policy_sha256,
        reason_version=UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
        selection_rule=SELECTION_RULE,
        receipt_json=canonical,
        receipt_sha256=policy.canonical_sha256(canonical),
    )
    session.add(stored)
    session.flush([stored])
    return stored


def activate_passing_acceptance(
    session: Session, receipt_id: int
) -> EventAdmissionActivation | None:
    """Activate only a current passing receipt; failed receipts remain evidence."""
    receipt = session.get(EventAdmissionAcceptanceReceipt, receipt_id)
    if receipt is None:
        raise ValueError("Event Admission acceptance receipt does not exist")
    if receipt.status != "passed":
        return None
    newest_receipt_id = session.scalar(
        select(EventAdmissionAcceptanceReceipt.id)
        .where(EventAdmissionAcceptanceReceipt.project_id == receipt.project_id)
        .order_by(EventAdmissionAcceptanceReceipt.id.desc())
        .limit(1)
    )
    if newest_receipt_id != receipt.id:
        raise ValueError("only the latest Event Admission acceptance may activate")
    project = session.get(Project, receipt.project_id)
    if project is None:
        raise ValueError("acceptance receipt project no longer exists")
    current_sha256 = policy.canonical_sha256(
        _canonical_policy(project, UNKNOWN_SCOPE_POLICY_VERSION)
    )
    if receipt.policy_sha256 != current_sha256:
        raise ValueError("proved Event Admission rules no longer match deployed rules")
    if not _acceptance_receipt_is_current(session, receipt):
        raise ValueError(
            "proved Event Admission source revision or migration head is stale"
        )
    latest = session.scalar(
        select(EventAdmissionActivation)
        .where(EventAdmissionActivation.project_id == receipt.project_id)
        .order_by(EventAdmissionActivation.id.desc())
        .limit(1)
    )
    if (
        latest is not None
        and latest.action == "activate"
        and latest.acceptance_receipt_id == receipt.id
    ):
        return latest
    activation = EventAdmissionActivation(
        project_id=receipt.project_id,
        acceptance_receipt_id=receipt.id,
        action="activate",
        policy_version=receipt.policy_version,
        reason="all declared real-state promotion gates passed",
        recorded_by=ACTIVATION_ACTOR,
    )
    session.add(activation)
    session.flush([activation])
    return activation


def suspend_unknown_scope_admission(
    session: Session,
    *,
    project_id: int,
    reason: str,
    recorded_by: str,
) -> EventAdmissionActivation:
    """Append a suspension; the predecessor becomes normal immediately."""
    if not reason.strip() or not recorded_by.strip():
        raise ValueError("suspension requires a reason and attributable recorder")
    latest = session.scalar(
        select(EventAdmissionActivation)
        .where(EventAdmissionActivation.project_id == project_id)
        .order_by(EventAdmissionActivation.id.desc())
        .limit(1)
    )
    if latest is None or latest.action != "activate":
        raise ValueError("unknown-scope Event Admission is not active")
    suspension = EventAdmissionActivation(
        project_id=project_id,
        acceptance_receipt_id=latest.acceptance_receipt_id,
        action="suspend",
        policy_version=latest.policy_version,
        reason=reason.strip(),
        recorded_by=recorded_by.strip(),
    )
    session.add(suspension)
    session.flush([suspension])
    return suspension


def _run_policy_clone(
    database_url: str,
    project_slug: str,
    policy_version: str,
    *,
    repeat: bool,
) -> dict[str, Any]:
    engine = create_engine(database_url, poolclass=NullPool, future=True)
    try:
        with Session(engine) as session:
            project = session.scalar(select(Project).where(Project.slug == project_slug))
            if project is None:
                raise ValueError(f"no project with slug {project_slug!r}")
            population = _population_receipt(session, project.id)
            before = _protected_state(session, project.id)
            first = run_event_admission(
                session, project.id, policy_version=policy_version
            )
            session.commit()
            after_first = _protected_state(session, project.id)
            second = None
            if repeat:
                second = run_event_admission(
                    session, project.id, policy_version=policy_version
                )
                session.commit()
            after_second = _protected_state(session, project.id)
            run = session.get(PolicyRun, first.run_id)
            if run is None:
                raise RuntimeError("Event Admission run receipt disappeared")
            outcomes = tuple(
                session.scalars(
                    select(EventAdmissionOutcome)
                    .where(EventAdmissionOutcome.policy_run_id == run.id)
                    .order_by(EventAdmissionOutcome.id)
                ).all()
            )
            metrics = _guardrail_metrics(
                session,
                project,
                outcomes,
                before=before,
                after_first=after_first,
                after_second=after_second,
                second_applied_count=second.admitted_count if second else None,
            )
            return {
                "policy_version": policy_version,
                "policy_sha256": run.policy_sha256,
                "population": population,
                "first_run": {
                    "run_id": first.run_id,
                    "admitted_count": first.admitted_count,
                    "abstained_count": first.abstained_count,
                    "abstention_reasons": dict(
                        sorted(Counter(item.reason for item in first.abstentions).items())
                    ),
                },
                "second_run": (
                    {
                        "run_id": second.run_id,
                        "admitted_count": second.admitted_count,
                        "abstained_count": second.abstained_count,
                    }
                    if second is not None
                    else None
                ),
                "metrics": metrics,
                "admissions": [
                    {
                        "candidate_id": outcome.candidate_id,
                        "commitment_lineage_id": outcome.commitment_lineage_id,
                        "statement_event_id": outcome.dependency_event_id,
                    }
                    for outcome in outcomes
                    if outcome.outcome == "admitted"
                ],
                "work_items": _admitted_work_items(session, project.id, outcomes),
            }
    finally:
        engine.dispose()


def _population_receipt(session: Session, project_id: int) -> dict[str, Any]:
    candidates = tuple(
        session.scalars(
            actionable_candidate_query(project_id)
            .where(Candidate.kind == "event", Candidate.state == "pending")
            .order_by(Candidate.id)
        ).all()
    )
    active_runs = tuple(
        session.scalars(
            select(ActiveRunDeclaration.extraction_run_id)
            .join(Document, Document.id == ActiveRunDeclaration.document_id)
            .where(Document.project_id == project_id)
            .order_by(ActiveRunDeclaration.extraction_run_id)
        ).all()
    )
    evidence = []
    for candidate in candidates:
        for citation in (candidate.payload_json or {}).get("citations", []):
            if isinstance(citation, dict):
                evidence.append(
                    {
                        "candidate_id": candidate.id,
                        "document_id": citation.get("document_id"),
                        "page": citation.get("page"),
                        "quote_sha256": policy.canonical_sha256(
                            str(citation.get("quote") or "").strip()
                        ),
                        "verified": citation.get("verified") is True,
                    }
                )
    return {
        "selection_rule": SELECTION_RULE,
        "active_run_ids": list(active_runs),
        "candidate_ids": [candidate.id for candidate in candidates],
        "evidence_fingerprints": evidence,
    }


def _protected_state(session: Session, project_id: int) -> dict[str, int]:
    lineage_ids = select(DependencyEvent.commitment_lineage_id).where(
        DependencyEvent.project_id == project_id,
        DependencyEvent.commitment_lineage_id.is_not(None),
    )
    return {
        "commitment_lineages": session.scalar(
            select(func.count()).select_from(CommitmentLineage).where(
                CommitmentLineage.project_id == project_id
            )
        ) or 0,
        "statement_events": session.scalar(
            select(func.count()).select_from(DependencyEvent).where(
                DependencyEvent.project_id == project_id
            )
        ) or 0,
        "scope_decisions": session.scalar(
            select(func.count())
            .select_from(DependencyEventScopeDecision)
            .join(
                DependencyEvent,
                DependencyEvent.id == DependencyEventScopeDecision.event_id,
            )
            .where(DependencyEvent.project_id == project_id)
        ) or 0,
        "dependencies": session.scalar(
            select(func.count()).select_from(Dependency).where(
                Dependency.project_id == project_id
            )
        ) or 0,
        "scope_links": session.scalar(
            select(func.count())
            .select_from(DependencyEventScope)
            .join(DependencyEvent, DependencyEvent.id == DependencyEventScope.event_id)
            .where(DependencyEvent.project_id == project_id)
        ) or 0,
        "work_decisions": session.scalar(
            select(func.count()).select_from(WorkDecision).where(
                WorkDecision.commitment_lineage_id.in_(lineage_ids)
            )
        ) or 0,
        "reports": session.scalar(
            select(func.count()).select_from(ReportRun).where(
                ReportRun.project_id == project_id
            )
        ) or 0,
        "candidate_dispositions": session.scalar(
            select(func.count())
            .select_from(CandidateDisposition)
            .join(Candidate, Candidate.id == CandidateDisposition.candidate_id)
            .where(Candidate.project_id == project_id)
        ) or 0,
        "policy_outcomes": session.scalar(
            select(func.count())
            .select_from(EventAdmissionOutcome)
            .join(PolicyRun, PolicyRun.id == EventAdmissionOutcome.policy_run_id)
            .where(PolicyRun.project_id == project_id)
        ) or 0,
        "audits": session.scalar(
            select(func.count()).select_from(AuditLog).where(
                AuditLog.entity_type == "commitment_lineage",
                AuditLog.entity_id.in_(lineage_ids),
            )
        ) or 0,
    }


def _guardrail_metrics(
    session: Session,
    project: Project,
    outcomes: tuple[EventAdmissionOutcome, ...],
    *,
    before: dict[str, int],
    after_first: dict[str, int],
    after_second: dict[str, int],
    second_applied_count: int | None,
) -> dict[str, Any]:
    admitted = tuple(item for item in outcomes if item.outcome == "admitted")
    events = tuple(
        session.get(DependencyEvent, item.dependency_event_id)
        for item in admitted
        if item.dependency_event_id is not None
    )
    false_party = sum(
        event is None
        or event.affected_external_org_id != event.stated_external_org_id
        for event in events
    )
    project_side = sum(
        event is not None
        and bool(event.stated_party)
        and identity.is_project_side_party(project, event.stated_party)
        for event in events
    )
    false_scope = 0
    unauthorized_decisions = 0
    invalid_evidence = 0
    cross_project = 0
    for outcome, event in zip(admitted, events, strict=True):
        scope = session.get(DependencyEventScopeDecision, outcome.scope_decision_id)
        links = session.scalars(
            select(DependencyEventScope).where(
                DependencyEventScope.event_id == outcome.dependency_event_id
            )
        ).all()
        if event is None or scope is None or scope.scope_mode != "unknown" or links:
            false_scope += 1
        if event is not None and event.project_id != project.id:
            cross_project += 1
        if event is not None:
            unauthorized_decisions += session.scalar(
                select(func.count()).select_from(WorkDecision).where(
                    WorkDecision.commitment_lineage_id == event.commitment_lineage_id
                )
            ) or 0
            memberships = session.scalars(
                select(DependencyEventEvidence).where(
                    DependencyEventEvidence.event_id == event.id
                )
            ).all()
            for membership in memberships:
                link = session.get(EvidenceLink, membership.evidence_link_id)
                if link is None or not link.verified:
                    invalid_evidence += 1
                    continue
                try:
                    validate_cited_statement_evidence(
                        session,
                        CitedStatementEvidence(
                            document_id=link.document_id,
                            page_no=link.page_no,
                            quote=link.quote,
                        ),
                        project.id,
                    )
                except StatementRefusal:
                    invalid_evidence += 1
                document = session.get(Document, link.document_id)
                if document is None or document.project_id != project.id:
                    cross_project += 1
        if (
            outcome.eligibility_json is None
            or outcome.eligibility_sha256
            != policy.canonical_sha256(outcome.eligibility_json)
        ):
            invalid_evidence += 1
    duplicates = sum(
        after_second[key] != after_first[key]
        for key in (
            "commitment_lineages",
            "statement_events",
            "scope_decisions",
            "scope_links",
            "work_decisions",
            "candidate_dispositions",
            "policy_outcomes",
            "audits",
        )
    ) + (second_applied_count or 0)
    admitted_lineage_ids = tuple(
        outcome.commitment_lineage_id
        for outcome in admitted
        if outcome.commitment_lineage_id is not None
    )
    admitted_candidate_ids = tuple(outcome.candidate_id for outcome in admitted)
    corrections = (
        session.scalar(
            select(func.count()).select_from(AuditLog).where(
                AuditLog.entity_type == audit.COMMITMENT_LINEAGE,
                AuditLog.entity_id.in_(admitted_lineage_ids),
                AuditLog.action.in_(
                    (audit.CORRECT_STATEMENT_SCOPE, audit.CORRECT_STATEMENT_FACTS)
                ),
            )
        )
        or 0
        if admitted_lineage_ids
        else 0
    )
    reversals = (
        session.scalar(
            select(func.count()).select_from(AuditLog).where(
                AuditLog.entity_type == audit.CANDIDATE,
                AuditLog.entity_id.in_(admitted_candidate_ids),
                AuditLog.action == audit.UNDO_COORDINATED_STATEMENT,
            )
        )
        or 0
        if admitted_candidate_ids
        else 0
    )
    return {
        "admission_count": len(admitted),
        "abstention_reasons": dict(
            sorted(Counter(item.reason for item in outcomes if item.reason).items())
        ),
        "false_party_attribution": false_party,
        "false_dependency_scope": false_scope,
        "project_side_masquerade": project_side,
        "cross_project_references": cross_project,
        "unauthorized_work_decisions": unauthorized_decisions,
        "invalid_evidence_or_receipts": invalid_evidence,
        "duplicates": duplicates,
        "corrections": corrections,
        "reversals": reversals,
        "protected_dependency_delta": after_first["dependencies"] - before["dependencies"],
        "protected_report_delta": after_first["reports"] - before["reports"],
    }


def _admitted_work_items(
    session: Session,
    project_id: int,
    outcomes: tuple[EventAdmissionOutcome, ...],
) -> list[dict[str, Any]]:
    admitted_lineages = {
        outcome.commitment_lineage_id
        for outcome in outcomes
        if outcome.outcome == "admitted" and outcome.commitment_lineage_id is not None
    }
    work = build_work_list(session, project_id, today=datetime.now(timezone.utc).date())
    return [
        {
            "commitment_lineage_id": item.commitment_lineage_id,
            "statement_event_id": item.statement_event_id,
            "source_candidate_id": item.source_candidate_id,
            "dependency_id": item.dependency_id,
            "attention_reasons": list(item.attention_reason_codes),
        }
        for item in (*work.immediate, *work.backlog)
        if item.commitment_lineage_id in admitted_lineages
    ]


def _acceptance_receipt_json(
    *,
    source_revision: str,
    migration_head: str,
    source_dump_sha256: str,
    predecessor: dict[str, Any],
    opt_in: dict[str, Any],
    migration_rehearsal: dict[str, str],
) -> dict[str, Any]:
    receipt = {
        "schema_version": RECEIPT_VERSION,
        "source_revision": source_revision,
        "migration_head": migration_head,
        "source_dump_sha256": source_dump_sha256,
        "selection_rule": SELECTION_RULE,
        "policy_version": UNKNOWN_SCOPE_POLICY_VERSION,
        "policy_sha256": opt_in["policy_sha256"],
        "reason_version": UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
        "population": opt_in["population"],
        "predecessor": predecessor,
        "opt_in": opt_in,
        "migration_rehearsal": migration_rehearsal,
        "authority_statement": (
            "Activation authorizes only the enumerated deterministic party-level "
            "Commitment class at Commitment Scope not yet known; it grants no model "
            "or Evidence Investigator write authority."
        ),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    receipt["gates"] = _receipt_promotion_gates(receipt)
    return receipt


def _promotion_gates(opt_in: dict[str, Any]) -> dict[str, bool]:
    metrics = opt_in.get("metrics")
    admissions = opt_in.get("admissions")
    work_items = opt_in.get("work_items")
    if (
        not isinstance(metrics, dict)
        or not isinstance(admissions, list)
        or not isinstance(work_items, list)
    ):
        raise ValueError(
            "opt-in acceptance result is missing metrics, admissions, or Work Items"
        )
    expected_work = {
        (
            admission.get("candidate_id"),
            admission.get("commitment_lineage_id"),
            admission.get("statement_event_id"),
        )
        for admission in admissions
        if isinstance(admission, dict)
    }
    observed_work = {
        (
            item.get("source_candidate_id"),
            item.get("commitment_lineage_id"),
            item.get("statement_event_id"),
        )
        for item in work_items
        if isinstance(item, dict)
    }
    allowed_reasons = {
        "past_due",
        "unknown_scope",
        "missing_internal_owner",
        "missing_next_action",
    }
    required_reasons = {
        "unknown_scope",
        "missing_internal_owner",
        "missing_next_action",
    }
    residual_work_is_exact = (
        len(admissions) == metrics["admission_count"]
        and len(work_items) == metrics["admission_count"]
        and len(expected_work) == metrics["admission_count"]
        and observed_work == expected_work
        and all(
            isinstance(item, dict)
            and item.get("dependency_id") is None
            and isinstance(item.get("attention_reasons"), list)
            and required_reasons.issubset(item["attention_reasons"])
            and set(item["attention_reasons"]).issubset(allowed_reasons)
            for item in work_items
        )
    )
    gates = {
        "eligible_case_observed": metrics["admission_count"] > 0,
        "zero_false_party_attribution": metrics["false_party_attribution"] == 0,
        "zero_false_dependency_scope": metrics["false_dependency_scope"] == 0,
        "zero_project_side_masquerade": metrics["project_side_masquerade"] == 0,
        "zero_cross_project_references": metrics["cross_project_references"] == 0,
        "zero_unauthorized_work_decisions": metrics["unauthorized_work_decisions"] == 0,
        "zero_duplicates": metrics["duplicates"] == 0,
        "zero_protected_state_changes": (
            metrics["protected_dependency_delta"] == 0
            and metrics["protected_report_delta"] == 0
        ),
        "all_evidence_and_receipts_valid": metrics["invalid_evidence_or_receipts"] == 0,
        "all_admissions_enter_residual_work": residual_work_is_exact,
    }
    return gates


def _receipt_promotion_gates(receipt: dict[str, Any]) -> dict[str, bool]:
    opt_in = receipt.get("opt_in")
    migration = receipt.get("migration_rehearsal")
    if not isinstance(opt_in, dict):
        raise ValueError("acceptance receipt is missing the opt-in result")
    gates = {
        **_promotion_gates(opt_in),
        "fresh_and_predecessor_migrations_passed": (
            isinstance(migration, dict)
            and migration.get("status") == "passed"
            and migration.get("predecessor") == "a257c9e6f204"
            and isinstance(migration.get("head"), str)
            and migration.get("fresh_status") == "passed"
            and migration.get("fresh_head") == migration.get("head")
        ),
    }
    assert set(gates) == PROMOTION_GATE_NAMES
    return gates


def _rehearse_predecessor_upgrade(
    postgres_admin_url: str, *, expected_head: str
) -> dict[str, str]:
    predecessor = "a257c9e6f204"
    with provision_disposable_postgres(
        postgres_admin_url,
        repo_root=REPO_ROOT,
        error_cls=ValueError,
        database_prefix="corridor_unknown_scope_upgrade_",
        migration_revision=predecessor,
    ) as database:
        database_url = SealedRehearsalEnvironment.clone_url(
            postgres_admin_url, database.name
        )
        completed = subprocess.run(
            ["uv", "run", "alembic", "upgrade", "head"],
            cwd=REPO_ROOT,
            env={**os.environ, "DATABASE_URL": database_url},
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode:
            detail = (completed.stderr or completed.stdout).strip().splitlines()
            raise ValueError(
                "Event Admission predecessor migration rehearsal failed"
                + (f": {detail[-1]}" if detail else "")
            )
        actual_head = read_migration_head(
            database_url, repo_root=REPO_ROOT, error_cls=ValueError
        )
        if actual_head != expected_head:
            raise ValueError("Event Admission predecessor rehearsal reached the wrong head")
        engine = create_engine(database_url, poolclass=NullPool, future=True)
        try:
            with engine.connect() as connection:
                receipt_count = connection.scalar(
                    select(func.count()).select_from(
                        EventAdmissionAcceptanceReceipt
                    )
                )
                if receipt_count != 0:
                    raise ValueError(
                        "fresh Event Admission acceptance tables were not empty"
                    )
        finally:
            engine.dispose()
    return {"predecessor": predecessor, "head": expected_head, "status": "passed"}
