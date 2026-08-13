"""Replay the real SH 99 Admission operation on a pinned isolated clone.

The first SH 99 acceptance boundary proved the event policy on a small
synthetic fixture.  That was useful policy coverage, but it did not prove the
operation that would run against the shared project: Active Run declaration,
Dependency Admission, and Event Admission over the real Candidate population.

This boundary captures a read-only, content-addressed PostgreSQL data snapshot,
restores it into a newly migrated disposable PostgreSQL 16 database, and invokes
the exact ``make admission ARGS=\"load sh99-grand-parkway\"`` command twice.
The shared database is read only throughout; only the disposable clone changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from corridor import audit
from corridor.exceptions import evaluate
from corridor.m8_acceptance_bundle import VerificationResult, publish_verified_bundle, verify_bundle
from corridor.m8_acceptance_database import (
    DatabaseProvisioner,
    ProvisionedDatabase,
    provision_disposable_postgres,
    read_migration_head,
    require_local_postgres_host,
)
from corridor.models import (
    ActiveExtractionRun,
    ActiveRunDeclaration,
    Assertion,
    Candidate,
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
    EvidenceLink,
    ExternalOrg,
    ExtractionRun,
    PolicyRun,
    Project,
)
from corridor.m8_acceptance_publication import publish_directory_once


SNAPSHOT_SCHEMA_VERSION = "corridor.sh99-real-admission-source-snapshot.v1"
BUNDLE_SCHEMA_VERSION = "corridor.sh99-real-admission-bundle.v1"
DATABASE_PREFIX = "corridor_sh99_real_admission_acceptance_"
BUNDLE_FILES = ("receipt.json", "canonical-content.json", "environment.json")
HUMAN_APPROVAL_GATE = (
    "No shared SH 99 database was changed. A designated human must separately "
    "approve any shared-database Admission operation."
)
REPO_ROOT = Path(__file__).resolve().parents[2]
_DATABASE_NAME = re.compile(r"^[A-Za-z0-9_]+$")


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


def run_sh99_admission_acceptance(
    config: SH99AdmissionAcceptanceConfig,
    *,
    provision_database: DatabaseProvisioner | None = None,
) -> SH99AdmissionAcceptanceSummary:
    """Pin and replay the shared SH 99 operation without mutating its database."""

    if provision_database is None:
        provision_database = _provision_database
    source = _require_clean_source(config.expected_clean_git_revision)
    source_head = _source_migration_head()
    source_database = _source_database(config.source_database_url)
    shared_head = read_migration_head(
        config.source_database_url, repo_root=REPO_ROOT, error_cls=ValueError
    )
    if shared_head != source_head:
        raise ValueError("shared database migration head does not match checked-out source")

    with tempfile.TemporaryDirectory(prefix="corridor-sh99-real-admission-") as parent:
        dump_path = Path(parent) / "source.dump"
        source_state = _read_project_state(config.source_database_url, config.project_slug)
        _dump_source_database(source_database, dump_path)
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
            _restore_source_database(source_database, dump_path, database.name)
            clone_before = _read_project_state(
                _database_url(config.postgres_admin_url, database.name),
                config.project_slug,
            )
            if clone_before != source_state:
                raise ValueError("restored SH 99 clone does not match its pinned source state")

            clone_url = _database_url(config.postgres_admin_url, database.name)
            first_operation = _run_shared_operation(clone_url, config.project_slug)
            after_first_run = _read_project_state(clone_url, config.project_slug)
            second_operation = _run_shared_operation(clone_url, config.project_slug)
            after_second_run = _read_project_state(clone_url, config.project_slug)
            first_run = _run_receipt(clone_before, after_first_run, first_operation)
            second_run = _run_receipt(after_first_run, after_second_run, second_operation)
            protected = _protected_cases(first_run, after_second_run)
            _require_protected_outcomes(protected, second_run)

        late_refusal = _late_refusal_receipt(
            config=config,
            source_database=source_database,
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


def _provision_database(admin_url: str):
    return provision_disposable_postgres(
        admin_url,
        repo_root=REPO_ROOT,
        error_cls=ValueError,
        database_prefix=DATABASE_PREFIX,
    )


def _require_clean_source(expected_checkout_revision: str) -> dict[str, str]:
    revision = _git("rev-parse", "HEAD")
    if _git("status", "--porcelain"):
        raise ValueError("SH 99 Admission replay requires a clean source checkout")
    if revision != expected_checkout_revision:
        raise ValueError("checkout revision does not match the caller-provided pin")
    return {"expected_checkout_revision": expected_checkout_revision, "revision": revision}


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _source_migration_head() -> str:
    completed = subprocess.run(
        ["uv", "run", "alembic", "heads"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    heads = [
        match.group(1)
        for line in completed.stdout.splitlines()
        if (match := re.match(r"^([0-9a-f]+) \(head\)$", line.strip()))
    ]
    if completed.returncode or len(heads) != 1:
        raise ValueError("checked-out source must declare exactly one Alembic head")
    return heads[0]


def _source_database(url: str) -> dict[str, str]:
    parsed = make_url(url)
    if parsed.get_backend_name() != "postgresql":
        raise ValueError("SH 99 Admission replay requires PostgreSQL")
    require_local_postgres_host(parsed.host, error_cls=ValueError)
    if not parsed.database or not _DATABASE_NAME.fullmatch(parsed.database):
        raise ValueError("source database name is invalid")
    if not parsed.username or not _DATABASE_NAME.fullmatch(parsed.username):
        raise ValueError("source database username is invalid")
    return {"database": parsed.database, "username": parsed.username}


def _database_url(admin_url: str, database_name: str) -> str:
    return make_url(admin_url).set(database=database_name).render_as_string(
        hide_password=False
    )


def _dump_source_database(source_database: dict[str, str], dump_path: Path) -> None:
    """Capture PostgreSQL 16 data inside the local Compose service, read only."""

    with dump_path.open("wb") as output:
        completed = subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "postgres",
                "pg_dump",
                "--format=custom",
                "--data-only",
                "--no-owner",
                "--no-privileges",
                "--exclude-table-data=alembic_version",
                "--username",
                source_database["username"],
                "--dbname",
                source_database["database"],
            ],
            cwd=REPO_ROOT,
            stdout=output,
            stderr=subprocess.PIPE,
            text=False,
            check=False,
        )
    if completed.returncode or not dump_path.stat().st_size:
        detail = completed.stderr.decode(errors="replace").strip().splitlines()
        raise ValueError(
            "could not capture the shared database"
            + (f": {detail[-1]}" if detail else "")
        )


def _restore_source_database(
    source_database: dict[str, str], dump_path: Path, database_name: str
) -> None:
    """Restore the captured data only into an already-migrated disposable clone."""

    with dump_path.open("rb") as source:
        completed = subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "postgres",
                "pg_restore",
                "--data-only",
                "--disable-triggers",
                "--exit-on-error",
                "--no-owner",
                "--no-privileges",
                "--username",
                source_database["username"],
                "--dbname",
                database_name,
            ],
            cwd=REPO_ROOT,
            stdin=source,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    if completed.returncode:
        detail = completed.stderr.decode(errors="replace").strip().splitlines()
        raise ValueError(
            "could not restore the pinned SH 99 source snapshot"
            + (f": {detail[-1]}" if detail else "")
        )


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


def _read_project_state(database_url: str, project_slug: str) -> dict[str, Any]:
    engine = create_engine(database_url, poolclass=NullPool, future=True)
    try:
        with Session(engine) as session:
            session.execute(text("set transaction read only"))
            state = _project_state(session, project_slug)
            session.rollback()
            return state
    finally:
        engine.dispose()


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


def _created_ledger_identities(before: dict[str, Any], after: dict[str, Any]) -> dict[str, list[int]]:
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
        "dependency_event_evidence": (before["ledger"]["dependency_event_evidence"], after["ledger"]["dependency_event_evidence"]),
        "admission_audits": (before["admission_audits"], after["admission_audits"]),
    }
    return {name: _new_identifiers(old, new) for name, (old, new) in collections.items()}


def _new_rows(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[dict[str, Any]]:
    old_ids = {item["id"] for item in before}
    return [item for item in after if item["id"] not in old_ids]


def _new_identifiers(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[int]:
    return [item["id"] for item in _new_rows(before, after)]


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
    source_database: dict[str, str],
    source_head: str,
    dump_path: Path,
    provision_database: DatabaseProvisioner,
) -> dict[str, Any]:
    """Prove an isolated late write refusal leaves Ledger and Candidates untouched."""

    with provision_database(config.postgres_admin_url) as database:
        if database.migration_head != source_head:
            raise ValueError("disposable refusal database is not at the source migration head")
        _restore_source_database(source_database, dump_path, database.name)
        clone_url = _database_url(config.postgres_admin_url, database.name)
        before = _read_project_state(clone_url, config.project_slug)
        _install_dependency_refusal(clone_url)
        operation = _run_shared_operation(
            clone_url,
            config.project_slug,
            expected_failure_marker="forced SH 99 acceptance dependency refusal",
        )
        after = _read_project_state(clone_url, config.project_slug)
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


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_sha256(value: Any) -> str:
    return _sha256(_canonical_json(value))
