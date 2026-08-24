"""Replay the real SH 99 Admission operation on a pinned isolated clone.

The first SH 99 acceptance boundary proved the event policy on a small
synthetic fixture.  That was useful policy coverage, but it did not prove the
operation that would run against the shared project: Active Run declaration,
Dependency Admission, and Event Admission over the real Candidate population.

This boundary captures a read-only, content-addressed PostgreSQL data snapshot,
restores it into a newly migrated disposable PostgreSQL 16 database, and invokes
the exact ``make admission ARGS=\"load sh99-grand-parkway\"`` command twice.
The shared database is read only throughout; only the disposable clone changes.

The post-activation seal reuses that same capture and command boundary while
pinning the current acceptance, activation, Active Runs, Candidate, Work Item,
and every permitted write identity. A separate clone framework was rejected:
two implementations of the production-like boundary would make their receipts
comparable in name while proving different operations.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any

from sqlalchemy import create_engine, or_, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from corridor import audit
from corridor.exceptions import evaluate
from corridor.m8_acceptance_bundle import VerificationResult, publish_verified_bundle, verify_bundle
from corridor.m8_acceptance_database import (
    DatabaseProvisioner,
    ProvisionedDatabase,
    provision_disposable_postgres,
)
from corridor.models import (
    ActiveExtractionRun,
    ActiveRunDeclaration,
    Assertion,
    Candidate,
    CandidateDisposition,
    CommitmentLineage,
    Dependency,
    DependencyAdmissionOutcome,
    DependencyEvent,
    DependencyEventEvidence,
    DependencyEventScope,
    DependencyEventScopeDecision,
    DependencyEventTiming,
    Document,
    EventAdmissionOutcome,
    EventAdmissionAcceptanceReceipt,
    EventAdmissionActivation,
    EvidenceLink,
    ExternalOrg,
    ExtractionRun,
    PolicyRun,
    Project,
    ReportRun,
    WorkDecision,
)
from corridor.m8_acceptance_publication import publish_directory_once
from corridor.rehearsal_environment import SealedRehearsalEnvironment


SNAPSHOT_SCHEMA_VERSION = "corridor.sh99-real-admission-source-snapshot.v1"
BUNDLE_SCHEMA_VERSION = "corridor.sh99-real-admission-bundle.v1"
DATABASE_PREFIX = "corridor_sh99_real_admission_acceptance_"
SHARED_SEAL_DATABASE_PREFIX = "corridor_sh99_shared_admission_seal_"
BUNDLE_FILES = ("receipt.json", "canonical-content.json", "environment.json")
SHARED_SEAL_SCHEMA_VERSION = "corridor.sh99-shared-admission-seal.v1"
SHARED_SEAL_BUNDLE_FILES = (
    "receipt.json",
    "receipt.md",
    "canonical-content.json",
    "environment.json",
)
SHARED_SEAL_APPROVAL_GATE = (
    "This passing seal does not authorize a shared Ledger write. A designated "
    "human must approve the exact source revision, current acceptance receipt, "
    "activation, Candidate, Active Runs, and manifest SHA-256."
)
HISTORICAL_ACCEPTANCE_RECEIPT = (
    156,
    "2343632bcb8adcd8c50796f2f70ee1c00e7d7ef188bc40b195ac9018b6263cdf",
)
HISTORICAL_ACTIVATION_ID = 140
HISTORICAL_ACTIVATION_SHA256 = (
    "8bd9eb909949f3b8b505af920900f33d47eb36317c7b690f4e729ea89c1c40f5"
)
HUMAN_APPROVAL_GATE = (
    "No shared SH 99 database was changed. A designated human must separately "
    "approve any shared-database Admission operation."
)
REPO_ROOT = Path(__file__).resolve().parents[2]


class CorruptSH99AdmissionBundle(ValueError):
    """An exported acceptance receipt does not match its claimed identity."""


@dataclass(frozen=True)
class SH99AdmissionAcceptanceConfig:
    project_slug: str
    source_database_url: str
    expected_clean_git_revision: str
    output_dir: Path
    postgres_admin_url: str


@dataclass(frozen=True)
class SH99AdmissionAcceptanceSummary:
    bundle_dir: Path
    manifest_path: Path
    integrity_manifest_sha256: str
    canonical_content_sha256: str
    database_name: str


@dataclass(frozen=True)
class SH99SharedAdmissionSealConfig:
    """Exact caller-held pins for the post-activation shared-operation seal."""

    project_slug: str
    source_database_url: str
    expected_clean_git_revision: str
    output_dir: Path
    postgres_admin_url: str
    expected_acceptance_receipt_id: int
    expected_acceptance_receipt_sha256: str
    expected_activation_id: int
    expected_active_runs: tuple[tuple[int, int], ...]
    expected_candidate_id: int


@dataclass(frozen=True)
class SH99SharedAdmissionSealSummary:
    bundle_dir: Path
    manifest_path: Path
    integrity_manifest_sha256: str
    canonical_content_sha256: str
    database_name: str


def run_sh99_shared_admission_seal(
    config: SH99SharedAdmissionSealConfig,
    *,
    provision_database: DatabaseProvisioner | None = None,
) -> SH99SharedAdmissionSealSummary:
    """Seal the exact ordinary shared Admission path on an isolated clone."""
    if provision_database is None:
        provision_database = _provision_shared_seal_database
    rehearsal = SealedRehearsalEnvironment.open(
        source_database_url=config.source_database_url,
        expected_checkout_revision=config.expected_clean_git_revision,
        repo_root=REPO_ROOT,
    )
    source = rehearsal.checkout
    origin_main_revision = _require_origin_main_revision(source["revision"])
    source_head = rehearsal.checkout_migration_head
    source_database = rehearsal.source_database

    with tempfile.TemporaryDirectory(prefix="corridor-sh99-shared-seal-") as parent:
        dump_path = Path(parent) / "source.dump"
        evaluated_on = date.today()
        source_state = _read_shared_seal_state(
            config.source_database_url,
            config.project_slug,
            evaluated_on=evaluated_on,
        )
        pins = _require_shared_seal_pins(
            source_state,
            config,
            source_revision=source["revision"],
            migration_head=source_head,
        )
        rehearsal.capture(dump_path)
        source_snapshot = {
            "schema_version": SHARED_SEAL_SCHEMA_VERSION,
            "source": {
                **source,
                "origin_main_revision": origin_main_revision,
                "migration_head": source_head,
                "database_name": source_database["database"],
                "postgres_version": source_state["postgres_version"],
            },
            "project_slug": config.project_slug,
            "source_dump_sha256": _sha256(dump_path.read_bytes()),
            "source_state_sha256": _json_sha256(source_state),
            "pins": pins,
            "before": source_state,
        }

        with provision_database(config.postgres_admin_url) as database:
            database_name = database.name
            if database.migration_head != source_head:
                raise ValueError(
                    "disposable database migration head does not match checked-out source"
                )
            rehearsal.restore(dump_path, database.name)
            clone_url = rehearsal.clone_url(config.postgres_admin_url, database.name)
            clone_before = _read_shared_seal_state(
                clone_url, config.project_slug, evaluated_on=evaluated_on
            )
            if clone_before != source_state:
                raise ValueError("restored SH 99 clone does not match its pinned source state")

            first_operation = _run_shared_operation(clone_url, config.project_slug)
            after_first = _read_shared_seal_state(
                clone_url, config.project_slug, evaluated_on=evaluated_on
            )
            second_operation = _run_shared_operation(clone_url, config.project_slug)
            after_second = _read_shared_seal_state(
                clone_url, config.project_slug, evaluated_on=evaluated_on
            )
            first_run = _shared_seal_run_receipt(
                clone_before, after_first, first_operation
            )
            second_run = _shared_seal_run_receipt(
                after_first, after_second, second_operation
            )
            exact_outcome = _require_shared_seal_outcomes(
                clone_before,
                after_first,
                after_second,
                first_run,
                second_run,
                project_slug=config.project_slug,
                expected_candidate_id=config.expected_candidate_id,
            )

    canonical = {
        "schema_version": SHARED_SEAL_SCHEMA_VERSION,
        "source_snapshot": {
            key: value for key, value in source_snapshot.items() if key != "before"
        },
        "first_run": first_run,
        "second_run": second_run,
        "after_second_state_sha256": _json_sha256(after_second),
        "exact_outcome": exact_outcome,
        "human_approval_gate": SHARED_SEAL_APPROVAL_GATE,
    }
    receipt = {
        **canonical,
        "source_snapshot": source_snapshot,
        "after_first": after_first,
        "after_second": after_second,
    }
    environment = {
        "schema_version": SHARED_SEAL_SCHEMA_VERSION,
        "replayed_at": datetime.now(timezone.utc).isoformat(),
        "source_revision": source["revision"],
        "migration_head": source_head,
        "source_dump_sha256": source_snapshot["source_dump_sha256"],
        "source_state_sha256": source_snapshot["source_state_sha256"],
        "shared_database_mutated": False,
    }
    manifest_path, manifest_sha256, canonical_sha256 = _write_shared_seal_bundle(
        config.output_dir,
        environment=environment,
        receipt=receipt,
        receipt_markdown=_shared_seal_markdown(canonical),
        canonical_content=canonical,
    )
    return SH99SharedAdmissionSealSummary(
        bundle_dir=config.output_dir,
        manifest_path=manifest_path,
        integrity_manifest_sha256=manifest_sha256,
        canonical_content_sha256=canonical_sha256,
        database_name=database_name,
    )


def run_sh99_admission_acceptance(
    config: SH99AdmissionAcceptanceConfig,
    *,
    provision_database: DatabaseProvisioner | None = None,
) -> SH99AdmissionAcceptanceSummary:
    """Pin and replay the shared SH 99 operation without mutating its database."""

    if provision_database is None:
        provision_database = _provision_database
    rehearsal = SealedRehearsalEnvironment.open(
        source_database_url=config.source_database_url,
        expected_checkout_revision=config.expected_clean_git_revision,
        repo_root=REPO_ROOT,
    )
    source = rehearsal.checkout
    source_head = rehearsal.checkout_migration_head
    source_database = rehearsal.source_database

    with tempfile.TemporaryDirectory(prefix="corridor-sh99-real-admission-") as parent:
        dump_path = Path(parent) / "source.dump"
        source_state = read_rehearsal_project_state(
            config.source_database_url, config.project_slug
        )
        rehearsal.capture(dump_path)
        source_snapshot = {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "source": {
                **source,
                "migration_head": source_head,
                "database_name": source_database["database"],
            },
            "project_slug": config.project_slug,
            "source_dump_sha256": _sha256(dump_path.read_bytes()),
            "before": source_state,
        }

        with provision_database(config.postgres_admin_url) as database:
            database_name = database.name
            if database.migration_head != source_head:
                raise ValueError(
                    "disposable database migration head does not match checked-out source"
                )
            rehearsal.restore(dump_path, database.name)
            clone_before = read_rehearsal_project_state(
                rehearsal.clone_url(config.postgres_admin_url, database.name),
                config.project_slug,
            )
            if clone_before != source_state:
                raise ValueError("restored SH 99 clone does not match its pinned source state")

            clone_url = rehearsal.clone_url(config.postgres_admin_url, database.name)
            first_operation = _run_shared_operation(clone_url, config.project_slug)
            after_first_run = read_rehearsal_project_state(
                clone_url, config.project_slug
            )
            second_operation = _run_shared_operation(clone_url, config.project_slug)
            after_second_run = read_rehearsal_project_state(
                clone_url, config.project_slug
            )
            first_run = _run_receipt(clone_before, after_first_run, first_operation)
            second_run = _run_receipt(after_first_run, after_second_run, second_operation)
            protected = _protected_cases(first_run, after_second_run)
            _require_protected_outcomes(protected, second_run)

        late_refusal = _late_refusal_receipt(
            config=config,
            rehearsal=rehearsal,
            source_head=source_head,
            dump_path=dump_path,
            provision_database=provision_database,
        )

    receipt = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "source_snapshot": source_snapshot,
        "first_run": first_run,
        "after_first_run": after_first_run,
        "second_run": second_run,
        "after_second_run": after_second_run,
        "protected_cases": protected,
        "late_refusal": late_refusal,
        "human_approval_gate": HUMAN_APPROVAL_GATE,
    }
    environment = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "replayed_at": datetime.now(timezone.utc).isoformat(),
        "source_revision": source["revision"],
        "migration_head": source_head,
        "source_dump_sha256": source_snapshot["source_dump_sha256"],
    }
    canonical = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "source_snapshot": source_snapshot,
        "first_run": first_run,
        "second_run": second_run,
        "after_second_run": after_second_run,
        "protected_cases": protected,
        "late_refusal": late_refusal,
        "human_approval_gate": HUMAN_APPROVAL_GATE,
    }
    manifest_path, manifest_sha256, canonical_sha256 = _write_bundle(
        config.output_dir,
        environment=environment,
        receipt=receipt,
        canonical_content=canonical,
    )
    return SH99AdmissionAcceptanceSummary(
        bundle_dir=config.output_dir,
        manifest_path=manifest_path,
        integrity_manifest_sha256=manifest_sha256,
        canonical_content_sha256=canonical_sha256,
        database_name=database_name,
    )


def verify_sh99_admission_bundle(
    bundle_dir: Path, *, expected_integrity_manifest_sha256: str
) -> VerificationResult:
    """Verify a closed real-SH-99 receipt export without opening PostgreSQL."""

    return verify_bundle(
        bundle_dir,
        expected_integrity_manifest_sha256=expected_integrity_manifest_sha256,
        bundle_schema_version=BUNDLE_SCHEMA_VERSION,
        bundle_files=BUNDLE_FILES,
        corrupt_bundle_error_cls=CorruptSH99AdmissionBundle,
        sha256=_sha256,
        json_sha256=_json_sha256,
    )


def verify_sh99_shared_admission_seal_bundle(
    bundle_dir: Path, *, expected_integrity_manifest_sha256: str
) -> VerificationResult:
    """Verify a shared-operation seal without opening PostgreSQL."""
    return verify_bundle(
        bundle_dir,
        expected_integrity_manifest_sha256=expected_integrity_manifest_sha256,
        bundle_schema_version=SHARED_SEAL_SCHEMA_VERSION,
        bundle_files=SHARED_SEAL_BUNDLE_FILES,
        corrupt_bundle_error_cls=CorruptSH99AdmissionBundle,
        sha256=_sha256,
        json_sha256=_json_sha256,
    )


def _provision_database(admin_url: str):
    return provision_disposable_postgres(
        admin_url,
        repo_root=REPO_ROOT,
        error_cls=ValueError,
        database_prefix=DATABASE_PREFIX,
    )


def _provision_shared_seal_database(admin_url: str):
    return provision_disposable_postgres(
        admin_url,
        repo_root=REPO_ROOT,
        error_cls=ValueError,
        database_prefix=SHARED_SEAL_DATABASE_PREFIX,
    )


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _require_origin_main_revision(source_revision: str) -> str:
    origin_main_revision = _git("rev-parse", "origin/main")
    if source_revision != origin_main_revision:
        raise ValueError("shared Admission seal requires HEAD to equal origin/main")
    return origin_main_revision


def _run_shared_operation(
    database_url: str,
    project_slug: str,
    *,
    expected_failure_marker: str | None = None,
) -> dict[str, Any]:
    """Invoke the exact shared-database command with only its database redirected."""

    argv = ["make", "admission", f"ARGS=load {project_slug}"]
    completed = subprocess.run(
        argv,
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode and expected_failure_marker is None:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        raise ValueError(
            "the isolated shared-operation command failed"
            + (f": {detail[-1]}" if detail else "")
        )
    if (
        expected_failure_marker is not None
        and expected_failure_marker not in completed.stderr
    ):
        raise ValueError("isolated late-refusal operation did not reach its forced refusal")
    return {
        "argv": argv,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        **(
            {"expected_failure_marker": expected_failure_marker}
            if expected_failure_marker is not None
            else {}
        ),
    }


def read_rehearsal_project_state(
    database_url: str, project_slug: str
) -> dict[str, Any]:
    """Read the exact SH 99 scenario state without permitting a write."""
    engine = create_engine(database_url, poolclass=NullPool, future=True)
    try:
        with Session(engine) as session:
            session.execute(text("set transaction read only"))
            state = _project_state(session, project_slug)
            session.rollback()
            return state
    finally:
        engine.dispose()


def _read_shared_seal_state(
    database_url: str,
    project_slug: str,
    *,
    evaluated_on: date | None = None,
) -> dict[str, Any]:
    engine = create_engine(database_url, poolclass=NullPool, future=True)
    try:
        with Session(engine) as session:
            session.execute(text("set transaction read only"))
            state = _shared_seal_state(
                session, project_slug, evaluated_on=evaluated_on
            )
            session.rollback()
            return state
    finally:
        engine.dispose()


def _shared_seal_state(
    session: Session,
    project_slug: str,
    *,
    evaluated_on: date | None = None,
) -> dict[str, Any]:
    from corridor.event_admission import _normal_policy_version
    from corridor.supersession import actionable_candidate_query
    from corridor.work_list import build_work_list

    state = _project_state(session, project_slug)
    project_id = state["project"]["id"]
    candidate_ids = [candidate["id"] for candidate in state["candidates"]]
    dependency_ids = [dependency["id"] for dependency in state["ledger"]["dependencies"]]
    lineage_ids = [
        lineage["id"] for lineage in state["ledger"]["commitment_lineages"]
    ]

    candidate_dispositions = [
        _row(item)
        for item in session.scalars(
            select(CandidateDisposition)
            .where(CandidateDisposition.candidate_id.in_(candidate_ids))
            .order_by(CandidateDisposition.id)
        )
    ]
    work_decisions = [
        _row(item)
        for item in session.scalars(
            select(WorkDecision)
            .where(
                or_(
                    WorkDecision.dependency_id.in_(dependency_ids),
                    WorkDecision.commitment_lineage_id.in_(lineage_ids),
                )
            )
            .order_by(WorkDecision.id)
        )
    ]
    report_runs = [
        _row(item)
        for item in session.scalars(
            select(ReportRun)
            .where(ReportRun.project_id == project_id)
            .order_by(ReportRun.id)
        )
    ]
    acceptance_receipts = [
        _row(item)
        for item in session.scalars(
            select(EventAdmissionAcceptanceReceipt)
            .where(EventAdmissionAcceptanceReceipt.project_id == project_id)
            .order_by(EventAdmissionAcceptanceReceipt.id)
        )
    ]
    activations = [
        _row(item)
        for item in session.scalars(
            select(EventAdmissionActivation)
            .where(EventAdmissionActivation.project_id == project_id)
            .order_by(EventAdmissionActivation.id)
        )
    ]
    relevant_audits = [
        _row(item)
        for item in session.scalars(
            select(audit.AuditLog)
            .where(
                or_(
                    (
                        (audit.AuditLog.entity_type == audit.DEPENDENCY)
                        & audit.AuditLog.entity_id.in_(dependency_ids)
                    ),
                    (
                        (audit.AuditLog.entity_type == audit.COMMITMENT_LINEAGE)
                        & audit.AuditLog.entity_id.in_(lineage_ids)
                    ),
                    (
                        (audit.AuditLog.entity_type == audit.CANDIDATE)
                        & audit.AuditLog.entity_id.in_(candidate_ids)
                    ),
                )
            )
            .order_by(audit.AuditLog.id)
        )
    ]
    event_evidence_link_ids = [
        membership["evidence_link_id"]
        for membership in state["ledger"]["dependency_event_evidence"]
    ]
    statement_evidence_links = [
        _row(item)
        for item in session.scalars(
            select(EvidenceLink)
            .where(EvidenceLink.id.in_(event_evidence_link_ids))
            .order_by(EvidenceLink.id)
        )
    ]
    work = build_work_list(
        session, project_id, today=evaluated_on or date.today()
    )
    actionable_pending_event_candidate_ids = [
        candidate.id
        for candidate in session.scalars(
            actionable_candidate_query(project_id)
            .where(Candidate.kind == "event", Candidate.state == "pending")
            .order_by(Candidate.id)
        )
    ]
    return {
        "postgres_version": str(session.scalar(text("show server_version"))),
        "current_event_admission_policy": _normal_policy_version(
            session, project_id
        ),
        "actionable_pending_event_candidate_ids": (
            actionable_pending_event_candidate_ids
        ),
        "project_state": state,
        "candidate_dispositions": candidate_dispositions,
        "work_decisions": work_decisions,
        "report_runs": report_runs,
        "event_admission_acceptance_receipts": acceptance_receipts,
        "event_admission_activations": activations,
        "relevant_audits": relevant_audits,
        "statement_evidence_links": statement_evidence_links,
        "work_list": {
            "evaluated_on": work.evaluated_on.isoformat(),
            "ruleset_version": work.ruleset_version,
            "immediate": [_seal_work_item(item) for item in work.immediate],
            "backlog": [_seal_work_item(item) for item in work.backlog],
        },
    }


def _seal_work_item(item: Any) -> dict[str, Any]:
    return {
        "kind": item.kind,
        "commitment_lineage_id": item.commitment_lineage_id,
        "statement_event_id": item.statement_event_id,
        "dependency_id": item.dependency_id,
        "candidate_id": item.candidate_id,
        "source_candidate_id": item.source_candidate_id,
        "timing_text": item.timing_text,
        "attention_reasons": list(item.attention_reason_codes),
    }


def _require_shared_seal_pins(
    state: dict[str, Any],
    config: SH99SharedAdmissionSealConfig,
    *,
    source_revision: str,
    migration_head: str,
) -> dict[str, Any]:
    project_state = state["project_state"]
    receipts = {
        item["id"]: item
        for item in state["event_admission_acceptance_receipts"]
    }
    activations = {
        item["id"]: item for item in state["event_admission_activations"]
    }
    historical_id, historical_sha256 = HISTORICAL_ACCEPTANCE_RECEIPT
    historical = receipts.get(historical_id)
    if historical is None or historical.get("receipt_sha256") != historical_sha256:
        raise ValueError("historical acceptance receipt 156 changed or disappeared")
    historical_activation = activations.get(HISTORICAL_ACTIVATION_ID)
    if (
        historical_activation is None
        or historical_activation.get("acceptance_receipt_id") != historical_id
        or historical_activation.get("action") != "activate"
        or _json_sha256(historical_activation) != HISTORICAL_ACTIVATION_SHA256
    ):
        raise ValueError("historical activation 140 changed or disappeared")

    receipt = receipts.get(config.expected_acceptance_receipt_id)
    if (
        receipt is None
        or receipt.get("status") != "passed"
        or receipt.get("receipt_sha256")
        != config.expected_acceptance_receipt_sha256
        or receipt.get("source_revision") != source_revision
        or receipt.get("migration_head") != migration_head
    ):
        raise ValueError("current acceptance receipt does not match the caller pins")
    activation = activations.get(config.expected_activation_id)
    latest_activation_id = max(activations) if activations else None
    if (
        activation is None
        or config.expected_activation_id != latest_activation_id
        or activation.get("acceptance_receipt_id") != receipt["id"]
        or activation.get("action") != "activate"
        or activation.get("policy_version") != receipt["policy_version"]
    ):
        raise ValueError("current activation does not match the caller pins")
    if state["current_event_admission_policy"] != receipt["policy_version"]:
        raise ValueError("current Event Admission policy does not match the receipt")

    active_runs = {
        item["document_id"]: item["extraction_run_id"]
        for item in project_state["active_runs"]
    }
    expected_active_runs = dict(config.expected_active_runs)
    if len(expected_active_runs) != len(config.expected_active_runs):
        raise ValueError("expected Active Run pins repeat a Document")
    if any(
        active_runs.get(document_id) != extraction_run_id
        for document_id, extraction_run_id in expected_active_runs.items()
    ):
        raise ValueError("current Active Runs do not match the caller pins")

    candidate = next(
        (
            item
            for item in project_state["candidates"]
            if item["id"] == config.expected_candidate_id
        ),
        None,
    )
    if candidate is None or candidate.get("state") != "pending":
        raise ValueError("expected Candidate is absent or no longer pending")
    if candidate.get("extraction_run_id") not in expected_active_runs.values():
        raise ValueError("expected Candidate does not belong to an approved Active Run")
    payload = candidate.get("payload_json") or {}
    fields = payload.get("fields") or {}
    citations = payload.get("citations") or []
    if (
        fields.get("event_type") != "commitment"
        or fields.get("stated_party") != "Equistar"
        or fields.get("external_org") != "Equistar"
        or fields.get("conflict_ref") not in (None, "")
        or (fields.get("committed_date") or {}).get("text") != "01/2025"
        or (fields.get("committed_date") or {}).get("precision") != "month"
        or candidate.get("citations_verified") is not True
        or len(citations) != 1
        or citations[0].get("verified") is not True
        or fields.get("description") != citations[0].get("quote")
    ):
        raise ValueError("expected Candidate facts do not match the sealed class")
    return {
        "historical_acceptance_receipt_id": historical_id,
        "historical_acceptance_receipt_sha256": historical_sha256,
        "historical_activation_id": HISTORICAL_ACTIVATION_ID,
        "historical_activation_sha256": HISTORICAL_ACTIVATION_SHA256,
        "acceptance_receipt_id": receipt["id"],
        "acceptance_receipt_sha256": receipt["receipt_sha256"],
        "activation_id": activation["id"],
        "policy_version": receipt["policy_version"],
        "policy_sha256": receipt["policy_sha256"],
        "active_runs": [
            {"document_id": document_id, "extraction_run_id": extraction_run_id}
            for document_id, extraction_run_id in sorted(expected_active_runs.items())
        ],
        "candidate_id": candidate["id"],
        "candidate_payload_sha256": _json_sha256(payload),
        "evidence": {
            "document_id": citations[0].get("document_id"),
            "page": citations[0].get("page"),
            "quote": citations[0].get("quote"),
            "quote_sha256": _sha256(
                str(citations[0].get("quote") or "").encode()
            ),
        },
        "stated_party": fields["stated_party"],
        "affected_party": fields["external_org"],
        "timing": fields["committed_date"],
    }


def _project_state(session: Session, project_slug: str) -> dict[str, Any]:
    project = session.scalar(select(Project).where(Project.slug == project_slug))
    if project is None:
        raise ValueError(f"no project with slug {project_slug!r}")
    documents = list(
        session.scalars(
            select(Document).where(Document.project_id == project.id).order_by(Document.id)
        )
    )
    document_ids = [document.id for document in documents]
    candidates = list(
        session.scalars(
            select(Candidate).where(Candidate.project_id == project.id).order_by(Candidate.id)
        )
    )
    dependencies = list(
        session.scalars(
            select(Dependency).where(Dependency.project_id == project.id).order_by(Dependency.id)
        )
    )
    dependency_ids = [dependency.id for dependency in dependencies]
    events = list(
        session.scalars(
            select(DependencyEvent)
            .where(DependencyEvent.project_id == project.id)
            .order_by(DependencyEvent.id)
        )
    )
    event_ids = [event.id for event in events]
    runs = list(
        session.scalars(
            select(PolicyRun).where(PolicyRun.project_id == project.id).order_by(PolicyRun.id)
        )
    )
    run_ids = [run.id for run in runs]
    organization_ids = {
        org_id
        for org_id in [
            *(dependency.external_org_id for dependency in dependencies),
            *(event.affected_external_org_id for event in events),
            *(event.stated_external_org_id for event in events),
        ]
        if org_id is not None
    }

    def rows(model, criterion, order_column):
        return [
            _row(value)
            for value in session.scalars(select(model).where(criterion).order_by(order_column))
        ]

    active_runs = rows(
        ActiveExtractionRun,
        ActiveExtractionRun.document_id.in_(document_ids),
        ActiveExtractionRun.document_id,
    )
    extraction_runs = rows(
        ExtractionRun,
        ExtractionRun.document_id.in_(document_ids),
        ExtractionRun.id,
    )
    overdue_dependency_ids = sorted(
        exception.dependency_id
        for exception in evaluate(session, project.id)
        if exception.rule == "OVERDUE"
    )
    return {
        "project": _row(project),
        "documents": [_row(document) for document in documents],
        "active_runs": active_runs,
        "active_run_declarations": rows(
            ActiveRunDeclaration,
            ActiveRunDeclaration.document_id.in_(document_ids),
            ActiveRunDeclaration.id,
        ),
        "extraction_runs": extraction_runs,
        "candidates": [_row(candidate) for candidate in candidates],
        "ledger": {
            "dependencies": [_row(dependency) for dependency in dependencies],
            "assertions": rows(Assertion, Assertion.dependency_id.in_(dependency_ids), Assertion.id),
            "evidence_links": rows(
                EvidenceLink, EvidenceLink.dependency_id.in_(dependency_ids), EvidenceLink.id
            ),
            "dependency_events": [_row(event) for event in events],
            "commitment_lineages": rows(
                CommitmentLineage,
                CommitmentLineage.project_id == project.id,
                CommitmentLineage.id,
            ),
            "dependency_event_timings": rows(
                DependencyEventTiming,
                DependencyEventTiming.event_id.in_(event_ids),
                DependencyEventTiming.id,
            ),
            "dependency_event_scope_decisions": rows(
                DependencyEventScopeDecision,
                DependencyEventScopeDecision.event_id.in_(event_ids),
                DependencyEventScopeDecision.id,
            ),
            "dependency_event_scopes": rows(
                DependencyEventScope,
                DependencyEventScope.event_id.in_(event_ids),
                DependencyEventScope.id,
            ),
            "dependency_event_evidence": rows(
                DependencyEventEvidence,
                DependencyEventEvidence.event_id.in_(event_ids),
                DependencyEventEvidence.evidence_link_id,
            ),
        },
        "organizations": rows(ExternalOrg, ExternalOrg.id.in_(organization_ids), ExternalOrg.id),
        "policy_runs": [_row(run) for run in runs],
        "dependency_admission_outcomes": rows(
            DependencyAdmissionOutcome,
            DependencyAdmissionOutcome.policy_run_id.in_(run_ids),
            DependencyAdmissionOutcome.id,
        ),
        "event_admission_outcomes": rows(
            EventAdmissionOutcome,
            EventAdmissionOutcome.policy_run_id.in_(run_ids),
            EventAdmissionOutcome.id,
        ),
        "overdue_dependency_ids": overdue_dependency_ids,
        "admission_audits": [
            _row(entry)
            for entry in session.scalars(
                select(audit.AuditLog)
                .join(Dependency, Dependency.id == audit.AuditLog.entity_id)
                .where(
                    Dependency.project_id == project.id,
                    audit.AuditLog.action.in_((audit.ADMIT_DEPENDENCY, audit.ADMIT_EVENT)),
                )
                .order_by(audit.AuditLog.id)
            )
        ],
    }


def _row(value: Any) -> dict[str, Any]:
    return {
        column.name: _json_value(getattr(value, column.name))
        for column in value.__table__.columns
    }


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


def _run_receipt(
    before: dict[str, Any], after: dict[str, Any], operation: dict[str, Any]
) -> dict[str, Any]:
    return {
        "operation": operation,
        "created_ledger_identities": _created_ledger_identities(before, after),
        "policy_runs": _new_rows(before["policy_runs"], after["policy_runs"]),
        "dependency_outcomes": _new_rows(
            before["dependency_admission_outcomes"], after["dependency_admission_outcomes"]
        ),
        "event_outcomes": _new_rows(
            before["event_admission_outcomes"], after["event_admission_outcomes"]
        ),
    }


def _created_ledger_identities(
    before: dict[str, Any], after: dict[str, Any]
) -> dict[str, list[Any]]:
    collections = {
        "active_run_declarations": (before["active_run_declarations"], after["active_run_declarations"]),
        "dependencies": (before["ledger"]["dependencies"], after["ledger"]["dependencies"]),
        "assertions": (before["ledger"]["assertions"], after["ledger"]["assertions"]),
        "evidence_links": (before["ledger"]["evidence_links"], after["ledger"]["evidence_links"]),
        "dependency_events": (before["ledger"]["dependency_events"], after["ledger"]["dependency_events"]),
        "commitment_lineages": (before["ledger"]["commitment_lineages"], after["ledger"]["commitment_lineages"]),
        "dependency_event_timings": (before["ledger"]["dependency_event_timings"], after["ledger"]["dependency_event_timings"]),
        "dependency_event_scope_decisions": (before["ledger"]["dependency_event_scope_decisions"], after["ledger"]["dependency_event_scope_decisions"]),
        "dependency_event_scopes": (before["ledger"]["dependency_event_scopes"], after["ledger"]["dependency_event_scopes"]),
        "admission_audits": (before["admission_audits"], after["admission_audits"]),
    }
    identities = {
        name: _new_identifiers(old, new)
        for name, (old, new) in collections.items()
    }
    identities["dependency_event_evidence"] = _new_composite_identifiers(
        before["ledger"]["dependency_event_evidence"],
        after["ledger"]["dependency_event_evidence"],
        fields=("event_id", "evidence_link_id"),
    )
    return identities


def _new_rows(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[dict[str, Any]]:
    old_ids = {item["id"] for item in before}
    return [item for item in after if item["id"] not in old_ids]


def _new_identifiers(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[int]:
    return [item["id"] for item in _new_rows(before, after)]


def _new_composite_identifiers(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    *,
    fields: tuple[str, ...],
) -> list[dict[str, Any]]:
    old = {tuple(item[field] for field in fields) for item in before}
    return [
        {field: item[field] for field in fields}
        for item in after
        if tuple(item[field] for field in fields) not in old
    ]


def _shared_seal_run_receipt(
    before: dict[str, Any],
    after: dict[str, Any],
    operation: dict[str, Any],
) -> dict[str, Any]:
    before_project = before["project_state"]
    after_project = after["project_state"]
    return {
        "operation": operation,
        "created_ledger_identities": _created_ledger_identities(
            before_project, after_project
        ),
        "created_candidate_disposition_ids": _new_identifiers(
            before["candidate_dispositions"], after["candidate_dispositions"]
        ),
        "created_work_decision_ids": _new_identifiers(
            before["work_decisions"], after["work_decisions"]
        ),
        "created_report_run_ids": _new_identifiers(
            before["report_runs"], after["report_runs"]
        ),
        "created_statement_evidence_link_ids": _new_identifiers(
            before["statement_evidence_links"], after["statement_evidence_links"]
        ),
        "created_relevant_audit_ids": _new_identifiers(
            before["relevant_audits"], after["relevant_audits"]
        ),
        "policy_runs": _new_rows(
            before_project["policy_runs"], after_project["policy_runs"]
        ),
        "dependency_outcomes": _new_rows(
            before_project["dependency_admission_outcomes"],
            after_project["dependency_admission_outcomes"],
        ),
        "event_outcomes": _new_rows(
            before_project["event_admission_outcomes"],
            after_project["event_admission_outcomes"],
        ),
        "candidate_state_changes": _candidate_state_changes(
            before_project["candidates"], after_project["candidates"]
        ),
        "work_list": after["work_list"],
    }


def _candidate_state_changes(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    old = {item["id"]: item for item in before}
    changes = []
    for item in after:
        prior = old.get(item["id"])
        if prior is None:
            continue
        values = {
            "state": (prior.get("state"), item.get("state")),
            "merged_into": (prior.get("merged_into"), item.get("merged_into")),
            "adjudicated_at": (
                prior.get("adjudicated_at"),
                item.get("adjudicated_at"),
            ),
        }
        if any(before_value != after_value for before_value, after_value in values.values()):
            changes.append(
                {
                    "candidate_id": item["id"],
                    **{
                        name: {"before": pair[0], "after": pair[1]}
                        for name, pair in values.items()
                        if pair[0] != pair[1]
                    },
                }
            )
    return changes


def _require_shared_seal_outcomes(
    before: dict[str, Any],
    after_first: dict[str, Any],
    after_second: dict[str, Any],
    first_run: dict[str, Any],
    second_run: dict[str, Any],
    *,
    project_slug: str,
    expected_candidate_id: int,
) -> dict[str, Any]:
    expected_command = ["make", "admission", f"ARGS=load {project_slug}"]
    if first_run["operation"]["argv"] != expected_command:
        raise ValueError("first replay did not use the exact ordinary Admission command")
    if second_run["operation"]["argv"] != expected_command:
        raise ValueError("second replay did not use the exact ordinary Admission command")
    if "0 conflicts and 1 statements on the record" not in first_run["operation"]["stdout"]:
        raise ValueError("first replay did not admit exactly one statement and no conflict")
    if "0 conflicts and 0 statements on the record" not in second_run["operation"]["stdout"]:
        raise ValueError("second replay was not an idempotent zero-write load")

    identities = first_run["created_ledger_identities"]
    expected_counts = {
        "active_run_declarations": 0,
        "dependencies": 0,
        "assertions": 0,
        "evidence_links": 0,
        "dependency_events": 1,
        "commitment_lineages": 1,
        "dependency_event_timings": 1,
        "dependency_event_scope_decisions": 1,
        "dependency_event_scopes": 0,
        "dependency_event_evidence": 1,
        "admission_audits": 0,
    }
    if {name: len(values) for name, values in identities.items()} != expected_counts:
        raise ValueError("first replay did not create the exact Ledger identity set")
    if len(first_run["created_candidate_disposition_ids"]) != 1:
        raise ValueError("first replay did not create exactly one Candidate disposition")
    if len(first_run["created_statement_evidence_link_ids"]) != 1:
        raise ValueError("first replay did not create exactly one statement Evidence link")
    if len(first_run["created_relevant_audit_ids"]) != 1:
        raise ValueError("first replay did not create exactly one attributable audit entry")
    if first_run["created_work_decision_ids"] or first_run["created_report_run_ids"]:
        raise ValueError("first replay created an unauthorized Work Decision or Report Run")
    if any(
        outcome.get("outcome") in {"admitted", "merged"}
        for outcome in first_run["dependency_outcomes"]
    ):
        raise ValueError("Dependency Admission changed the Ledger")

    admitted_outcomes = [
        outcome
        for outcome in first_run["event_outcomes"]
        if outcome.get("outcome") == "admitted"
    ]
    if (
        len(admitted_outcomes) != 1
        or admitted_outcomes[0].get("candidate_id") != expected_candidate_id
    ):
        raise ValueError("Event Admission did not admit only the expected Candidate")
    population = set(before["actionable_pending_event_candidate_ids"])
    outcomes_by_candidate: dict[int, list[dict[str, Any]]] = {}
    for outcome in first_run["event_outcomes"]:
        outcomes_by_candidate.setdefault(outcome["candidate_id"], []).append(outcome)
    if set(outcomes_by_candidate) != population:
        raise ValueError("Event Admission did not record an outcome for every Candidate")
    if any(
        outcome.get("outcome") != "abstained"
        for candidate_id, outcomes in outcomes_by_candidate.items()
        if candidate_id != expected_candidate_id
        for outcome in outcomes
    ):
        raise ValueError("another Event Candidate received a non-Abstention outcome")
    candidate_changes = first_run["candidate_state_changes"]
    if len(candidate_changes) != 1:
        raise ValueError("first replay changed a Candidate other than the expected one")
    [candidate_change] = candidate_changes
    if (
        candidate_change.get("candidate_id") != expected_candidate_id
        or candidate_change.get("state")
        != {"before": "pending", "after": "accepted"}
        or candidate_change.get("adjudicated_at", {}).get("before") is not None
        or not candidate_change.get("adjudicated_at", {}).get("after")
        or "merged_into" in candidate_change
    ):
        raise ValueError("expected Candidate did not receive only its Admission state")

    after_project = after_first["project_state"]
    event_id = identities["dependency_events"][0]
    lineage_id = identities["commitment_lineages"][0]
    scope_decision_id = identities["dependency_event_scope_decisions"][0]
    timing_id = identities["dependency_event_timings"][0]
    event = _row_with_id(after_project["ledger"]["dependency_events"], event_id)
    lineage = _row_with_id(after_project["ledger"]["commitment_lineages"], lineage_id)
    scope = _row_with_id(
        after_project["ledger"]["dependency_event_scope_decisions"],
        scope_decision_id,
    )
    timing = _row_with_id(
        after_project["ledger"]["dependency_event_timings"], timing_id
    )
    organizations = {
        item["id"]: item for item in after_project["organizations"]
    }
    if (
        event.get("commitment_lineage_id") != lineage_id
        or event.get("event_type") != "commitment"
        or event.get("source_kind") != "cited"
        or organizations[event["stated_external_org_id"]]["name"] != "Equistar"
        or organizations[event["affected_external_org_id"]]["name"] != "Equistar"
        or scope.get("scope_mode") != "unknown"
        or timing.get("text") != "01/2025"
        or timing.get("precision") != "month"
    ):
        raise ValueError("admitted Commitment facts do not match the sealed Candidate")
    if lineage.get("project_id") != after_project["project"]["id"]:
        raise ValueError("admitted Commitment Lineage belongs to another project")

    work_items = [
        item
        for lane in ("immediate", "backlog")
        for item in after_first["work_list"][lane]
        if item.get("source_candidate_id") == expected_candidate_id
    ]
    if len(work_items) != 1:
        raise ValueError("admitted Commitment did not create exactly one Work Item")
    [work_item] = work_items
    if (
        work_item.get("commitment_lineage_id") != lineage_id
        or work_item.get("statement_event_id") != event_id
        or work_item.get("dependency_id") is not None
        or work_item.get("attention_reasons")
        != [
            "past_due",
            "unknown_scope",
            "missing_internal_owner",
            "missing_next_action",
        ]
    ):
        raise ValueError("admitted Commitment Work Item contains non-residual work")

    duplicate_collections = (
        *second_run["created_ledger_identities"].values(),
        second_run["created_candidate_disposition_ids"],
        second_run["created_work_decision_ids"],
        second_run["created_report_run_ids"],
        second_run["created_statement_evidence_link_ids"],
        second_run["created_relevant_audit_ids"],
        second_run["dependency_outcomes"],
        second_run["event_outcomes"],
        second_run["candidate_state_changes"],
    )
    if any(duplicate_collections):
        raise ValueError("second replay created duplicate Admission state or outcomes")
    if after_second["work_list"] != after_first["work_list"]:
        raise ValueError("second replay changed the residual Work Item")
    return {
        "candidate_id": expected_candidate_id,
        "commitment_lineage_id": lineage_id,
        "statement_event_id": event_id,
        "scope_decision_id": scope_decision_id,
        "timing_id": timing_id,
        "candidate_disposition_id": first_run["created_candidate_disposition_ids"][0],
        "statement_evidence_link_id": first_run[
            "created_statement_evidence_link_ids"
        ][0],
        "audit_log_id": first_run["created_relevant_audit_ids"][0],
        "work_item": work_item,
        "second_run_zero_new_outcomes": True,
    }


def _row_with_id(rows: list[dict[str, Any]], identifier: int) -> dict[str, Any]:
    row = next((item for item in rows if item.get("id") == identifier), None)
    if row is None:
        raise ValueError(f"sealed identity {identifier} disappeared from clone state")
    return row


def _protected_cases(first_run: dict[str, Any], after_second_run: dict[str, Any]) -> dict[str, Any]:
    candidate_by_id = {item["id"]: item for item in after_second_run["candidates"]}
    event_outcomes = {
        item["candidate_id"]: item for item in first_run["event_outcomes"]
    }
    return {
        str(candidate_id): _protected_candidate_facts(
            candidate_by_id.get(candidate_id),
            event_outcomes.get(candidate_id),
            after_second_run,
        )
        for candidate_id in (7587, 7129, 7296)
    }


def _protected_candidate_facts(
    candidate: dict[str, Any] | None,
    event_outcome: dict[str, Any] | None,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Record only durable facts the protected Candidate could have created.

    A Candidate does not own a separate statement projection.  Its only route
    to attribution, timing, scope, a committed-date projection, or a derived
    overdue result is an admitted ``DependencyEvent``.  Recording that entire
    route makes an abstention or remaining-pending result independently
    inspectable instead of treating a ``Candidate.state`` value as proof.
    """

    event_id = event_outcome.get("dependency_event_id") if event_outcome else None
    events = [
        event
        for event in state["ledger"]["dependency_events"]
        if event["id"] == event_id
    ]
    scopes = [
        scope
        for scope in state["ledger"]["dependency_event_scopes"]
        if scope["event_id"] == event_id
    ]
    scope_decisions = [
        decision
        for decision in state["ledger"]["dependency_event_scope_decisions"]
        if decision["event_id"] == event_id
    ]
    timings = [
        timing
        for timing in state["ledger"]["dependency_event_timings"]
        if timing["event_id"] == event_id
    ]
    statement_evidence = [
        evidence
        for evidence in state["ledger"]["dependency_event_evidence"]
        if evidence["event_id"] == event_id
    ]
    scoped_dependency_ids = {scope["dependency_id"] for scope in scopes}
    dependency_projections = [
        {
            "dependency_id": dependency["id"],
            "committed_date": dependency["committed_date"],
        }
        for dependency in state["ledger"]["dependencies"]
        if dependency["id"] in scoped_dependency_ids
    ]
    return {
        "state": candidate.get("state") if candidate else None,
        "merged_into": candidate.get("merged_into") if candidate else None,
        "event_outcome": event_outcome,
        "statement_facts": events,
        "scope_decision_ids": [decision["id"] for decision in scope_decisions],
        "scope_ids": [scope["id"] for scope in scopes],
        "timing_ids": [timing["id"] for timing in timings],
        "statement_evidence_ids": [evidence["evidence_link_id"] for evidence in statement_evidence],
        "committed_date_projections": dependency_projections,
        # OVERDUE is computed by the canonical exception reader rather than
        # inferred from a scalar. With no statement and no scope, the
        # protected Candidate has no dependency for which it can be true.
        "past_due_dependency_ids": [
            dependency_id
            for dependency_id in sorted(scoped_dependency_ids)
            if dependency_id in state["overdue_dependency_ids"]
        ],
    }


def _require_protected_outcomes(protected: dict[str, Any], second_run: dict[str, Any]) -> None:
    if protected["7587"]["state"] != "pending":
        raise ValueError("Candidate 7587 did not remain pending after Admission Abstention")
    event_outcome = protected["7587"]["event_outcome"]
    if event_outcome is None or event_outcome.get("outcome") != "abstained":
        raise ValueError("Candidate 7587 did not record an Admission Abstention")
    if event_outcome.get("dependency_event_id") is not None:
        raise ValueError("Candidate 7587 Admission Abstention created a statement")
    if any(protected[str(candidate_id)]["state"] != "pending" for candidate_id in (7129, 7296)):
        raise ValueError("Candidates 7129 and 7296 did not remain pending")
    if any(
        protected[str(candidate_id)]["merged_into"] is not None
        for candidate_id in (7587, 7129, 7296)
    ):
        raise ValueError("protected Candidates were merged despite remaining pending")
    protected_fact_collections = (
        "statement_facts",
        "scope_decision_ids",
        "scope_ids",
        "timing_ids",
        "statement_evidence_ids",
        "committed_date_projections",
        "past_due_dependency_ids",
    )
    if any(
        protected[str(candidate_id)][collection]
        for candidate_id in (7587, 7129, 7296)
        for collection in protected_fact_collections
    ):
        raise ValueError("protected Candidates created unsupported statement or timing facts")
    if any(second_run["created_ledger_identities"].values()):
        raise ValueError("replay created duplicate Ledger or audit facts")


def _late_refusal_receipt(
    *,
    config: SH99AdmissionAcceptanceConfig,
    rehearsal: SealedRehearsalEnvironment,
    source_head: str,
    dump_path: Path,
    provision_database: DatabaseProvisioner,
) -> dict[str, Any]:
    """Prove an isolated late write refusal leaves Ledger and Candidates untouched."""

    with provision_database(config.postgres_admin_url) as database:
        if database.migration_head != source_head:
            raise ValueError("disposable refusal database is not at the source migration head")
        rehearsal.restore(dump_path, database.name)
        clone_url = rehearsal.clone_url(config.postgres_admin_url, database.name)
        before = read_rehearsal_project_state(clone_url, config.project_slug)
        _install_dependency_refusal(clone_url)
        operation = _run_shared_operation(
            clone_url,
            config.project_slug,
            expected_failure_marker="forced SH 99 acceptance dependency refusal",
        )
        after = read_rehearsal_project_state(clone_url, config.project_slug)
        unchanged = {
            "candidates": before["candidates"] == after["candidates"],
            "ledger": before["ledger"] == after["ledger"],
        }
        if not all(unchanged.values()):
            raise ValueError("late refusal changed SH 99 Ledger or Candidate state")
        return {
            "operation": operation,
            "ledger_and_candidate_state_unchanged": unchanged,
            "policy_runs_created": _new_rows(before["policy_runs"], after["policy_runs"]),
        }


def _install_dependency_refusal(database_url: str) -> None:
    engine = create_engine(database_url, poolclass=NullPool, future=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    create function refuse_sh99_real_acceptance_dependency()
                    returns trigger language plpgsql as $$
                    begin
                        raise exception 'forced SH 99 acceptance dependency refusal'
                            using errcode = '23514';
                    end;
                    $$;
                    """
                )
            )
            connection.execute(
                text(
                    """
                    create trigger refuse_sh99_real_acceptance_dependency
                    before insert on dependencies
                    for each row execute function refuse_sh99_real_acceptance_dependency();
                    """
                )
            )
    finally:
        engine.dispose()


def _write_bundle(
    output_dir: Path,
    *,
    environment: dict[str, Any],
    receipt: dict[str, Any],
    canonical_content: dict[str, Any],
) -> tuple[Path, str, str]:
    return publish_verified_bundle(
        output_dir,
        exports={
            "environment.json": environment,
            "receipt.json": receipt,
            "canonical-content.json": canonical_content,
        },
        canonical_content=canonical_content,
        bundle_schema_version=BUNDLE_SCHEMA_VERSION,
        bundle_files=BUNDLE_FILES,
        error_cls=ValueError,
        corrupt_bundle_error_cls=CorruptSH99AdmissionBundle,
        canonical_json=_canonical_json,
        sha256=_sha256,
        json_sha256=_json_sha256,
        temp_prefix="corridor-sh99-real-admission-bundle",
        self_verification_failure="new real SH 99 Admission bundle failed self-verification",
    )


def _write_shared_seal_bundle(
    output_dir: Path,
    *,
    environment: dict[str, Any],
    receipt: dict[str, Any],
    receipt_markdown: bytes,
    canonical_content: dict[str, Any],
) -> tuple[Path, str, str]:
    return publish_verified_bundle(
        output_dir,
        exports={
            "environment.json": environment,
            "receipt.json": receipt,
            "receipt.md": receipt_markdown,
            "canonical-content.json": canonical_content,
        },
        canonical_content=canonical_content,
        bundle_schema_version=SHARED_SEAL_SCHEMA_VERSION,
        bundle_files=SHARED_SEAL_BUNDLE_FILES,
        error_cls=ValueError,
        corrupt_bundle_error_cls=CorruptSH99AdmissionBundle,
        canonical_json=_canonical_json,
        sha256=_sha256,
        json_sha256=_json_sha256,
        temp_prefix="corridor-sh99-shared-admission-seal",
        self_verification_failure="new shared Admission seal failed self-verification",
    )


def _shared_seal_markdown(canonical: dict[str, Any]) -> bytes:
    snapshot = canonical["source_snapshot"]
    pins = snapshot["pins"]
    exact = canonical["exact_outcome"]
    work_item = exact["work_item"]
    lines = [
        "# SH99 shared Admission seal",
        "",
        "This receipt proves the exact ordinary Admission command on a disposable clone.",
        "It does not authorize a shared Ledger write.",
        "",
        f"- Source revision: `{snapshot['source']['revision']}`",
        f"- Migration head: `{snapshot['source']['migration_head']}`",
        f"- PostgreSQL: `{snapshot['source']['postgres_version']}`",
        f"- Source-state SHA-256: `{snapshot['source_state_sha256']}`",
        f"- Acceptance receipt: `{pins['acceptance_receipt_id']}`",
        f"- Activation: `{pins['activation_id']}`",
        f"- Policy: `{pins['policy_version']}`",
        f"- Policy SHA-256: `{pins['policy_sha256']}`",
        f"- Candidate: `{exact['candidate_id']}`",
        f"- Commitment Lineage on clone: `{exact['commitment_lineage_id']}`",
        f"- Statement event on clone: `{exact['statement_event_id']}`",
        f"- Commitment Scope: `not yet known`",
        f"- Work Item Attention Reasons: `{', '.join(work_item['attention_reasons'])}`",
        "- First run: 0 Dependencies and exactly 1 Commitment",
        "- Second run: no new Ledger, Candidate, audit, Work Decision, Report, or policy outcome",
        "",
        SHARED_SEAL_APPROVAL_GATE,
        "",
    ]
    return "\n".join(lines).encode()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_sha256(value: Any) -> str:
    return _sha256(_canonical_json(value))
