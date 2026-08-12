"""Retire a noncompliant development Ledger without rewriting its history.

The active Ledger is the relational Dependency graph.  Development artifacts
leave that graph only after one complete, canonical copy has been hashed and
sealed.  Candidates, source material, report history, and the original audit
rows remain where they are; the archive is an independently readable receipt,
not a second interpretation of those records.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.inspection import inspect as sqlalchemy_inspect
from sqlalchemy.orm import Session

from corridor import audit
from corridor.models import (
    Assertion,
    AuditLog,
    AutomaticCarryForwardReceipt,
    Candidate,
    Dependency,
    DependencyEvidenceSufficiency,
    DependencyEvent,
    DependencyEventEvidence,
    DependencyEventScope,
    DependencyEventScopeDecision,
    DependencyEventTiming,
    Document,
    EvidenceLink,
    ExternalOrg,
    LegacyLedgerArchive,
    Milestone,
    OperativeSupport,
    Project,
    ReconfirmationReceipt,
)
from corridor.project_lock import lock_project

ARCHIVE_FORMAT_VERSION = "legacy-ledger-v3"
_READABLE_ARCHIVE_FORMATS = frozenset(
    {"legacy-ledger-v1", "legacy-ledger-v2", ARCHIVE_FORMAT_VERSION}
)
RETIREMENT_ACTOR = "system:legacy-ledger-retirement/v1"
STATEMENT_RETIREMENT_ROLE = "corridor_statement_retirement"
LEGACY_ADMISSION_ACTORS = frozenset({"agent", "demo"})
_REF_CODE = re.compile(r"DEP-(\d+)$")


@dataclass(frozen=True)
class RetirementPlan:
    """The exact content a later retirement must compare-and-swap."""

    project_id: int
    content: dict[str, Any]
    content_sha256: str
    counts: dict[str, int]
    ref_code_high_watermark: int


@dataclass(frozen=True)
class ArchiveReadback:
    """One persisted archive after structural and digest verification."""

    archive: LegacyLedgerArchive
    content: dict[str, Any]


class LegacyLedgerArchiveError(RuntimeError):
    """The exact development graph cannot be archived safely."""


class RetirementTargetDrift(LegacyLedgerArchiveError):
    """The active graph no longer matches the operator's sealed plan."""


class CorruptLegacyLedgerArchive(LegacyLedgerArchiveError):
    """A stored archive no longer agrees with its immutable receipt."""


def _assume_statement_retirement_role(session: Session) -> None:
    """Assume the dedicated DB role for the one sealed statement purge."""

    try:
        session.execute(text(f"set local role {STATEMENT_RETIREMENT_ROLE}"))
    except DBAPIError as exc:
        raise LegacyLedgerArchiveError(
            "legacy Ledger retirement requires the statement-retirement database role"
        ) from exc


def _purge_statement_rows(session: Session, project_id: int) -> None:
    """Delete sealed statement rows without leaking the maintenance role."""

    _assume_statement_retirement_role(session)
    try:
        session.execute(
            text(
                "select public.purge_external_party_statement_rows("
                ":project_id, 'retirement')"
            ),
            {"project_id": project_id},
        )
    except Exception:
        # A failing statement aborts this transaction; its eventual rollback
        # also rolls the local role back to the caller.
        raise
    else:
        session.execute(text("set local role none"))


def plan_retirement(session: Session, project_id: int) -> RetirementPlan:
    """Build a canonical, read-only manifest for one project's active Ledger."""

    project = session.get(Project, project_id)
    if project is None:
        raise ValueError(f"project {project_id} does not exist")

    dependencies = list(
        session.scalars(
            select(Dependency)
            .where(Dependency.project_id == project_id)
            .order_by(Dependency.id)
        ).all()
    )
    dependency_ids = [dependency.id for dependency in dependencies]
    assertions = _for_dependencies(session, Assertion, dependency_ids)
    evidence_links = _for_dependencies(session, EvidenceLink, dependency_ids)
    events = list(
        session.scalars(
            select(DependencyEvent)
            .where(DependencyEvent.project_id == project_id)
            .order_by(DependencyEvent.id)
        ).all()
    )
    event_ids = [event.id for event in events]
    event_scope_decisions = list(
        session.scalars(
            select(DependencyEventScopeDecision)
            .where(DependencyEventScopeDecision.event_id.in_(event_ids or [0]))
            .order_by(DependencyEventScopeDecision.id)
        ).all()
    )
    event_scopes = list(
        session.scalars(
            select(DependencyEventScope)
            .where(DependencyEventScope.event_id.in_(event_ids or [0]))
            .order_by(DependencyEventScope.id)
        ).all()
    )
    event_timings = list(
        session.scalars(
            select(DependencyEventTiming)
            .where(DependencyEventTiming.event_id.in_(event_ids or [0]))
            .order_by(DependencyEventTiming.id)
        ).all()
    )
    operative_support = _for_dependencies(
        session, OperativeSupport, dependency_ids
    )
    direct_audit = list(
        session.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == audit.DEPENDENCY,
                AuditLog.entity_id.in_(dependency_ids or [0]),
            )
            .order_by(AuditLog.ts, AuditLog.id)
        ).all()
    )
    originating_candidate_ids = sorted(
        {
            candidate_id
            for entry in direct_audit
            if (
                candidate_id := _positive_int(
                    (entry.after_json or {}).get("candidate_id")
                )
            )
            is not None
        }
    )
    candidate_audit = list(
        session.scalars(
            select(AuditLog)
            .where(
                AuditLog.entity_type == audit.CANDIDATE,
                AuditLog.entity_id.in_(originating_candidate_ids or [0]),
            )
            .order_by(AuditLog.ts, AuditLog.id)
        ).all()
    )
    audit_entries = sorted(
        [*direct_audit, *candidate_audit], key=lambda entry: (entry.ts, entry.id)
    )
    originating_candidates = list(
        session.scalars(
            select(Candidate)
            .where(Candidate.id.in_(originating_candidate_ids or [0]))
            .order_by(Candidate.id)
        ).all()
    )

    event_evidence = list(
        session.scalars(
            select(EvidenceLink)
            .join(
                DependencyEventEvidence,
                DependencyEventEvidence.evidence_link_id == EvidenceLink.id,
            )
            .where(DependencyEventEvidence.event_id.in_(event_ids or [0]))
            .order_by(EvidenceLink.id)
        ).all()
    )
    event_evidence_mappings = list(
        session.scalars(
            select(DependencyEventEvidence)
            .where(DependencyEventEvidence.event_id.in_(event_ids or [0]))
            .order_by(DependencyEventEvidence.evidence_link_id)
        ).all()
    )
    evidence_links = list({link.id: link for link in [*evidence_links, *event_evidence]}.values())
    sufficiencies = list(
        session.scalars(
            select(DependencyEvidenceSufficiency)
            .where(DependencyEvidenceSufficiency.dependency_id.in_(dependency_ids or [0]))
            .order_by(DependencyEvidenceSufficiency.id)
        ).all()
    )
    document_ids = sorted(
        {
            *(link.document_id for link in evidence_links),
            *(candidate.source_document_id for candidate in originating_candidates),
        }
    )
    documents = list(
        session.scalars(
            select(Document)
            .where(Document.id.in_(document_ids or [0]))
            .order_by(Document.id)
        ).all()
    )
    external_org_ids = sorted(
        {
            dependency.external_org_id
            for dependency in dependencies
            if dependency.external_org_id is not None
        }
    )
    external_orgs = list(
        session.scalars(
            select(ExternalOrg)
            .where(ExternalOrg.id.in_(external_org_ids or [0]))
            .order_by(ExternalOrg.id)
        ).all()
    )
    milestone_ids = sorted(
        {
            dependency.milestone_id
            for dependency in dependencies
            if dependency.milestone_id is not None
        }
    )
    milestones = list(
        session.scalars(
            select(Milestone)
            .where(Milestone.id.in_(milestone_ids or [0]))
            .order_by(Milestone.id)
        ).all()
    )
    high_watermark = max(
        (
            int(match.group(1))
            for dependency in dependencies
            if (match := _REF_CODE.fullmatch(dependency.ref_code or ""))
        ),
        default=0,
    )
    content = _canonical_jsonb(
        {
            "format_version": ARCHIVE_FORMAT_VERSION,
            "project": _row_content(project),
            "dependencies": [_row_content(row) for row in dependencies],
            "assertions": [_row_content(row) for row in assertions],
            "evidence_links": [_row_content(row) for row in evidence_links],
            "dependency_evidence_sufficiencies": [
                _row_content(row) for row in sufficiencies
            ],
            "dependency_events": [_row_content(row) for row in events],
            "dependency_event_scope_decisions": [
                _row_content(row) for row in event_scope_decisions
            ],
            "dependency_event_scopes": [_row_content(row) for row in event_scopes],
            "dependency_event_timings": [_row_content(row) for row in event_timings],
            "dependency_event_evidence": [
                _row_content(row) for row in event_evidence_mappings
            ],
            "operative_support": [_row_content(row) for row in operative_support],
            "audit_log": [_row_content(row) for row in audit_entries],
            "originating_candidates": [
                _row_content(row) for row in originating_candidates
            ],
            "documents": [_row_content(row) for row in documents],
            "external_orgs": [_row_content(row) for row in external_orgs],
            "milestones": [_row_content(row) for row in milestones],
            "ref_code_high_watermark": high_watermark,
        }
    )
    counts = {
        "dependencies": len(dependencies),
        "assertions": len(assertions),
        "evidence_links": len(evidence_links),
        "audit_log": len(audit_entries),
    }
    plan = RetirementPlan(
        project_id=project_id,
        content=content,
        content_sha256=_content_sha256(content),
        counts=counts,
        ref_code_high_watermark=high_watermark,
    )
    _validate_retirement_target(session, plan)
    return plan


def retire_legacy_ledger(
    session: Session,
    project_id: int,
    *,
    expected_sha256: str,
    expected_dependency_count: int | None = None,
) -> LegacyLedgerArchive:
    """Seal and remove one exact development Ledger graph atomically.

    A retry after commit returns the already-verified receipt.  Every other
    mismatch fails before the active graph changes.
    """

    with session.begin_nested():
        lock_project(session, project_id)
        # ``expire_all`` discards unflushed attribute changes. Flush every
        # caller-owned pending mutation before taking the fresh post-lock
        # snapshot so retirement can never erase unrelated Session work.
        session.flush()
        # The caller commonly plans in this same Session before executing.
        # Re-read every target row after acquiring the lock so a concurrent
        # cooperating writer cannot be archived from the identity map's
        # pre-lock snapshot and then have its newer database row deleted.
        session.expire_all()
        existing = session.scalar(
            select(LegacyLedgerArchive).where(
                LegacyLedgerArchive.project_id == project_id
            )
        )
        active_count = session.scalar(
            select(func.count(Dependency.id)).where(
                Dependency.project_id == project_id
            )
        )
        if existing is not None:
            verify_archive(session, existing.id)
            if active_count:
                raise LegacyLedgerArchiveError(
                    "an archive exists but the project has active Dependencies"
                )
            if existing.content_sha256 != expected_sha256:
                raise RetirementTargetDrift(
                    "existing archive does not match the expected content digest"
                )
            if expected_dependency_count is not None and (
                existing.dependency_count != expected_dependency_count
            ):
                raise RetirementTargetDrift(
                    f"expected {expected_dependency_count} Dependencies, found "
                    f"{existing.dependency_count} in the existing archive"
                )
            _verify_retirement_audit(session, existing)
            return existing
        if not active_count:
            raise LegacyLedgerArchiveError(
                "the project has no active Ledger and no retirement archive"
            )

        plan = plan_retirement(session, project_id)
        if expected_dependency_count is not None and (
            plan.counts["dependencies"] != expected_dependency_count
        ):
            raise RetirementTargetDrift(
                f"expected {expected_dependency_count} Dependencies, found "
                f"{plan.counts['dependencies']}"
            )
        if plan.content_sha256 != expected_sha256:
            raise RetirementTargetDrift(
                "active Ledger content does not match the expected digest"
            )
        _validate_retirement_target(session, plan)

        archive = LegacyLedgerArchive(
            project_id=project_id,
            format_version=ARCHIVE_FORMAT_VERSION,
            content_json=deepcopy(plan.content),
            content_sha256=plan.content_sha256,
            dependency_count=plan.counts["dependencies"],
            assertion_count=plan.counts["assertions"],
            evidence_link_count=plan.counts["evidence_links"],
            audit_log_count=plan.counts["audit_log"],
            ref_code_high_watermark=plan.ref_code_high_watermark,
            retired_by=RETIREMENT_ACTOR,
        )
        session.add(archive)
        session.flush([archive])
        # Verification must cross the JSONB round trip rather than reading
        # the object that was just added from the identity map.
        session.expire(archive)
        verify_archive(session, archive.id)
        dependency_ids = [row["id"] for row in plan.content["dependencies"]]
        session.execute(
            delete(Assertion).where(Assertion.dependency_id.in_(dependency_ids))
        )
        session.execute(
            delete(OperativeSupport).where(
                OperativeSupport.dependency_id.in_(dependency_ids)
            )
        )
        session.execute(
            delete(DependencyEvidenceSufficiency).where(
                DependencyEvidenceSufficiency.dependency_id.in_(dependency_ids)
            )
        )
        session.execute(
            delete(EvidenceLink).where(
                EvidenceLink.dependency_id.in_(dependency_ids)
            )
        )
        event_ids = set(
            session.scalars(
                select(DependencyEvent.id).where(
                    DependencyEvent.project_id == project_id
                )
            ).all()
        )
        _purge_statement_rows(session, project_id)
        # The privileged procedure deleted rows outside SQLAlchemy's normal
        # synchronize-session path.  Remove only those stale statement
        # identities: archive callers may still need their live Dependency
        # ids for immediate readback.
        for row in list(session.identity_map.values()):
            if isinstance(row, DependencyEvent):
                is_statement_row = row.id in event_ids
            elif isinstance(row, (DependencyEventScope, DependencyEventTiming)):
                is_statement_row = row.event_id in event_ids
            elif isinstance(row, EvidenceLink):
                is_statement_row = row.event_id in event_ids
            else:
                is_statement_row = False
            if is_statement_row:
                session.expunge(row)
        session.execute(delete(Dependency).where(Dependency.id.in_(dependency_ids)))
        session.flush()
        remaining = session.scalar(
            select(func.count(Dependency.id)).where(
                Dependency.project_id == project_id
            )
        )
        if remaining:
            raise LegacyLedgerArchiveError(
                f"retirement left {remaining} active Dependencies"
            )
        audit.record(
            session,
            actor=RETIREMENT_ACTOR,
            action=audit.RETIRE_LEGACY_LEDGER,
            entity_type=audit.PROJECT,
            entity_id=project_id,
            before={
                "dependency_count": plan.counts["dependencies"],
                "content_sha256": plan.content_sha256,
            },
            after={
                "archive_id": archive.id,
                "dependency_count": 0,
                "content_sha256": plan.content_sha256,
            },
        )
        return archive


def verify_archive(session: Session, archive_id: int) -> ArchiveReadback:
    """Read one archive without active Ledger joins and verify its digest."""

    archive = session.get(
        LegacyLedgerArchive, archive_id, populate_existing=True
    )
    if archive is None:
        raise LegacyLedgerArchiveError(
            f"legacy Ledger archive {archive_id} does not exist"
        )
    content = deepcopy(archive.content_json)
    if archive.format_version not in _READABLE_ARCHIVE_FORMATS:
        raise CorruptLegacyLedgerArchive(
            f"unsupported archive format {archive.format_version!r}"
        )
    if _content_sha256(content) != archive.content_sha256:
        raise CorruptLegacyLedgerArchive(
            f"legacy Ledger archive {archive.id} content digest does not match"
        )
    expected_counts = {
        "dependencies": archive.dependency_count,
        "assertions": archive.assertion_count,
        "evidence_links": archive.evidence_link_count,
        "audit_log": archive.audit_log_count,
    }
    actual_counts = {
        name: len(content.get(name) or ()) for name in expected_counts
    }
    if actual_counts != expected_counts:
        raise CorruptLegacyLedgerArchive(
            f"legacy Ledger archive {archive.id} row counts do not match"
        )
    if content.get("ref_code_high_watermark") != archive.ref_code_high_watermark:
        raise CorruptLegacyLedgerArchive(
            f"legacy Ledger archive {archive.id} ref-code boundary does not match"
        )
    archived_project = content.get("project")
    if (
        not isinstance(archived_project, dict)
        or archived_project.get("id") != archive.project_id
        or not isinstance(archived_project.get("slug"), str)
        or not archived_project["slug"]
    ):
        raise CorruptLegacyLedgerArchive(
            f"legacy Ledger archive {archive.id} project identity does not match"
        )
    return ArchiveReadback(archive=archive, content=content)


def export_archive(session: Session, archive_id: int, path: str | Path) -> Path:
    """Write the exact canonical bytes covered by the stored SHA-256."""

    readback = verify_archive(session, archive_id)
    output = Path(path)
    output.write_bytes(_canonical_bytes(readback.content))
    return output


def _validate_retirement_target(session: Session, plan: RetirementPlan) -> None:
    dependency_ids = [row["id"] for row in plan.content["dependencies"]]
    direct_entries = [
        row
        for row in plan.content["audit_log"]
        if row["entity_type"] == audit.DEPENDENCY
        and row["entity_id"] in dependency_ids
    ]
    admissions_by_dependency: dict[int, list[dict[str, Any]]] = {
        dependency_id: [] for dependency_id in dependency_ids
    }
    for entry in direct_entries:
        if entry["action"] in (audit.ACCEPT_CANDIDATE, audit.MERGE_CANDIDATE):
            admissions_by_dependency[entry["entity_id"]].append(entry)
    if any(len(entries) != 1 for entries in admissions_by_dependency.values()):
        raise LegacyLedgerArchiveError(
            "every retired Dependency must have exactly one legacy Admission"
        )
    admissions = [entries[0] for entries in admissions_by_dependency.values()]
    if any(
        entry["human_principal"] is not None
        or entry["actor"] not in LEGACY_ADMISSION_ACTORS
        for entry in admissions
    ):
        raise LegacyLedgerArchiveError(
            "retirement target contains an attributable or unknown Admission"
        )
    candidate_ids = {
        _positive_int((entry.get("after_json") or {}).get("candidate_id"))
        for entry in admissions
    }
    if None in candidate_ids or len(candidate_ids) != len(dependency_ids):
        raise LegacyLedgerArchiveError(
            "legacy Admissions do not identify one unique Candidate each"
        )
    archived_candidates = {
        row["id"]: row for row in plan.content["originating_candidates"]
    }
    if set(archived_candidates) != candidate_ids:
        raise LegacyLedgerArchiveError(
            "an originating Candidate is missing from the archive"
        )
    if any(
        candidate["project_id"] != plan.project_id
        or candidate["state"] != "accepted"
        for candidate in archived_candidates.values()
    ):
        raise LegacyLedgerArchiveError(
            "originating Candidates are not preserved accepted lineage"
        )
    if session.scalar(
        select(func.count(Candidate.id)).where(
            Candidate.merged_into.in_(dependency_ids)
        )
    ):
        raise LegacyLedgerArchiveError(
            "a Candidate still points at a Dependency selected for retirement"
        )
    if session.scalar(
        select(func.count(ReconfirmationReceipt.audit_log_id)).where(
            ReconfirmationReceipt.dependency_id.in_(dependency_ids)
        )
    ):
        raise LegacyLedgerArchiveError(
            "a Reconfirmation receipt still points at a retirement target"
        )
    if session.scalar(
        select(func.count(AutomaticCarryForwardReceipt.audit_log_id)).where(
            AutomaticCarryForwardReceipt.dependency_id.in_(dependency_ids)
        )
    ):
        raise LegacyLedgerArchiveError(
            "an Automatic Carry-Forward receipt still points at a retirement target"
        )


def _verify_retirement_audit(
    session: Session, archive: LegacyLedgerArchive
) -> AuditLog:
    entries = list(
        session.scalars(
            select(AuditLog).where(
                AuditLog.entity_type == audit.PROJECT,
                AuditLog.entity_id == archive.project_id,
                AuditLog.action == audit.RETIRE_LEGACY_LEDGER,
            )
        ).all()
    )
    if len(entries) != 1:
        raise CorruptLegacyLedgerArchive(
            "legacy Ledger retirement must have exactly one project audit entry"
        )
    entry = entries[0]
    if (
        entry.actor != RETIREMENT_ACTOR
        or entry.human_principal is not None
        or entry.before_json
        != {
            "dependency_count": archive.dependency_count,
            "content_sha256": archive.content_sha256,
        }
        or entry.after_json
        != {
            "archive_id": archive.id,
            "dependency_count": 0,
            "content_sha256": archive.content_sha256,
        }
    ):
        raise CorruptLegacyLedgerArchive(
            "legacy Ledger retirement audit does not match its archive"
        )
    return entry


def _for_dependencies(
    session: Session, model: type, dependency_ids: list[int]
) -> list[Any]:
    return list(
        session.scalars(
            select(model)
            .where(model.dependency_id.in_(dependency_ids or [0]))
            .order_by(model.id)
        ).all()
    )


def _row_content(row: Any) -> dict[str, Any]:
    return {
        attribute.key: _json_value(getattr(row, attribute.key))
        for attribute in sqlalchemy_inspect(row).mapper.column_attrs
    }


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return deepcopy(value)


def _canonical_jsonb(value: Any) -> Any:
    if isinstance(value, float):
        return 0.0 if value == 0 else value
    if isinstance(value, dict):
        return {key: _canonical_jsonb(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical_jsonb(item) for item in value]
    return value


def _content_sha256(content: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(content)).hexdigest()


def _canonical_bytes(content: dict[str, Any]) -> bytes:
    return json.dumps(
        content,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value
