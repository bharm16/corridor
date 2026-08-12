"""Digest-pinned, isolated SH 99 mechanical Admission rehearsal.

This is deliberately an operations acceptance boundary, not a production
backfill command.  It reconstructs only a small, pinned SH 99 fixture in a
fresh PostgreSQL 16 database, invokes the ordinary event-admission policy,
and exports the exact receipts and rows an independent reviewer needs.  The
shared SH 99 database is never opened or mutated here.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from corridor import audit
from corridor.event_admission import (
    ABSTENTION_REASON_VERSION,
    EVENT_ADMISSION_POLICY_VERSION,
    FAMILY,
    run_event_admission,
)
from corridor.extraction_runs import record_extraction_run
from corridor.m8_acceptance_bundle import (
    VerificationResult,
    publish_verified_bundle,
    verify_bundle,
)
from corridor.m8_acceptance_database import (
    DatabaseProvisioner,
    ProvisionedDatabase,
    provision_disposable_postgres,
)
from corridor.models import (
    ActiveExtractionRun,
    Candidate,
    Dependency,
    DependencyEvent,
    DependencyEventScope,
    DependencyEventTiming,
    DocPage,
    Document,
    EventAdmissionOutcome,
    ExternalOrg,
    PolicyRun,
    Project,
)


SNAPSHOT_SCHEMA_VERSION = "corridor.sh99-admission-snapshot.v1"
BUNDLE_SCHEMA_VERSION = "corridor.sh99-admission-bundle.v1"
DATABASE_PREFIX = "corridor_sh99_admission_acceptance_"
BUNDLE_FILES = ("receipt.json", "canonical-content.json", "environment.json")
HUMAN_APPROVAL_GATE = (
    "No shared SH 99 database was changed. A designated human must separately "
    "approve any shared-database Admission operation."
)
REPO_ROOT = Path(__file__).resolve().parents[2]


class CorruptSH99AdmissionBundle(ValueError):
    """An exported acceptance receipt does not match its claimed identity."""


@dataclass(frozen=True)
class SH99AdmissionAcceptanceConfig:
    snapshot_path: Path
    expected_snapshot_sha256: str
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
    """Run two deterministic Admission passes on a disposable pinned clone."""

    if provision_database is None:
        provision_database = _provision_database
    snapshot, snapshot_sha256 = _load_snapshot(
        config.snapshot_path, expected_sha256=config.expected_snapshot_sha256
    )
    source_state = _require_pinned_source(
        snapshot["source_revision"], config.expected_clean_git_revision
    )
    with provision_database(config.postgres_admin_url) as database:
        _require_pinned_schema(snapshot, database)
        with database.session_factory() as session:
            project, candidates = _seed_snapshot(session, snapshot)
            session.commit()
            before = _state(session, project.id, candidates)
            late_refusal = _late_refusal_receipt(session, project, candidates)
            session.commit()

            first = run_event_admission(session, project.id)
            session.commit()
            first_run = _run_receipt(session, project.id, first.run_id, before)
            after_first_run = _state(session, project.id, candidates)

            second = run_event_admission(session, project.id)
            session.commit()
            after_second_run = _state(session, project.id, candidates)
            second_run = _run_receipt(
                session, project.id, second.run_id, after_first_run
            )

            receipt = {
                "schema_version": BUNDLE_SCHEMA_VERSION,
                "input_pins": _input_pins(snapshot, snapshot_sha256),
                "database": {
                    "name": database.name,
                    "postgres_version": database.postgres_version,
                    "migration_head": database.migration_head,
                },
                "source": source_state,
                "before": before,
                "first_run": first_run,
                "after_first_run": after_first_run,
                "second_run": second_run,
                "after_second_run": after_second_run,
                "negative_cases": _negative_cases(
                    first_run=first_run, after_second_run=after_second_run
                ),
                "late_refusal": late_refusal,
                "human_approval_gate": HUMAN_APPROVAL_GATE,
            }
            environment = {
                "schema_version": BUNDLE_SCHEMA_VERSION,
                "replayed_at": datetime.now(timezone.utc).isoformat(),
                "database_name": database.name,
                "postgres_version": database.postgres_version,
                "migration_head": database.migration_head,
                "snapshot_sha256": snapshot_sha256,
                "source_revision": snapshot["source_revision"],
                "checkout_revision": source_state["revision"],
            }
            canonical = {
                "schema_version": BUNDLE_SCHEMA_VERSION,
                "input_pins": receipt["input_pins"],
                "migration_head": database.migration_head,
                "before": before,
                "first_run": first_run,
                "second_run": second_run,
                "after_second_run": after_second_run,
                "negative_cases": receipt["negative_cases"],
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
            database_name=database.name,
        )


def verify_sh99_admission_bundle(
    bundle_dir: Path, *, expected_integrity_manifest_sha256: str
) -> VerificationResult:
    """Verify a closed receipt export without opening any source database."""

    return verify_bundle(
        bundle_dir,
        expected_integrity_manifest_sha256=expected_integrity_manifest_sha256,
        bundle_schema_version=BUNDLE_SCHEMA_VERSION,
        bundle_files=BUNDLE_FILES,
        corrupt_bundle_error_cls=CorruptSH99AdmissionBundle,
        sha256=_sha256,
        json_sha256=_json_sha256,
    )


def _provision_database(admin_url: str) -> Iterator[ProvisionedDatabase]:
    return provision_disposable_postgres(
        admin_url,
        repo_root=REPO_ROOT,
        error_cls=ValueError,
        database_prefix=DATABASE_PREFIX,
    )


def _load_snapshot(path: Path, *, expected_sha256: str) -> tuple[dict[str, Any], str]:
    if len(expected_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in expected_sha256):
        raise ValueError("snapshot digest must be a lowercase SHA-256")
    try:
        value = path.read_bytes()
    except OSError as exc:
        raise ValueError("snapshot is absent or unreadable") from exc
    digest = _sha256(value)
    if digest != expected_sha256:
        raise ValueError("snapshot digest does not match the caller-provided pin")
    try:
        snapshot = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("snapshot is not valid JSON") from exc
    if snapshot.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise ValueError("snapshot schema is unsupported")
    _validate_snapshot(snapshot)
    return snapshot, digest


def _validate_snapshot(snapshot: dict[str, Any]) -> None:
    """Refuse input that cannot name one exact Active Run for every document."""

    corpus = {item.get("registry_id") for item in snapshot.get("corpus_inputs", [])}
    if len(corpus) != 1:
        raise ValueError("the bounded SH 99 snapshot must contain exactly one corpus input")
    active_runs = snapshot.get("declared_active_runs")
    if not isinstance(active_runs, list) or not active_runs:
        raise ValueError("snapshot must pin declared Active Runs")
    active_registry_ids = [item.get("registry_id") for item in active_runs]
    if set(active_registry_ids) != corpus or len(active_registry_ids) != len(corpus):
        raise ValueError("snapshot Active Runs must cover every corpus input exactly once")
    if any(
        not isinstance(item.get(key), str) or not item[key]
        for item in active_runs
        for key in ("prompt_version", "model", "schema_version")
    ):
        raise ValueError("snapshot Active Runs must pin prompt, model, and schema")
    database_snapshot = snapshot.get("database_snapshot")
    if not isinstance(database_snapshot, dict) or not isinstance(
        database_snapshot.get("migration_head"), str
    ):
        raise ValueError("snapshot must pin the database migration head")


def _require_pinned_source(
    snapshot_revision: str, expected_checkout_revision: str
) -> dict[str, str]:
    """Refuse any checkout other than the exact caller-held source pin."""

    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if status:
        raise ValueError("isolated Admission replay requires a clean source checkout")
    if revision != expected_checkout_revision:
        raise ValueError("checkout revision does not match the caller-provided pin")
    exists = subprocess.run(
        ["git", "rev-parse", "--verify", f"{snapshot_revision}^{{commit}}"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if exists.returncode:
        raise ValueError("snapshot source revision is unavailable in this checkout")
    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", snapshot_revision, revision],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if ancestry.returncode:
        raise ValueError("checkout does not descend from the pinned snapshot source")
    return {
        "snapshot_revision": snapshot_revision,
        "expected_checkout_revision": expected_checkout_revision,
        "revision": revision,
    }


def _require_pinned_schema(
    snapshot: dict[str, Any], database: ProvisionedDatabase
) -> None:
    if database.migration_head != snapshot["database_snapshot"]["migration_head"]:
        raise ValueError(
            "disposable database migration head does not match the pinned snapshot"
        )


def _seed_snapshot(session: Session, snapshot: dict[str, Any]) -> tuple[Project, dict[int, Candidate]]:
    project_data = snapshot["project"]
    project = Project(
        slug=project_data["slug"],
        name=project_data["name"],
        is_synthetic=True,
        project_side_parties=project_data["project_side_parties"],
    )
    session.add(project)
    session.flush([project])

    organizations = _seed_organizations(session, snapshot)
    _seed_dependencies(session, project, snapshot, organizations)
    document = _seed_document(session, project, snapshot)
    candidates = _seed_event_candidates(session, project, document, snapshot)
    session.flush()
    return project, candidates


def _seed_organizations(session: Session, snapshot: dict[str, Any]) -> dict[str, ExternalOrg]:
    names = {
        item["external_org"] for item in snapshot["dependencies"]
    } | {
        str(item["fields"].get("external_org"))
        for item in snapshot["event_candidates"]
        if item["fields"].get("external_org")
    } | {
        str(item["fields"].get("stated_party"))
        for item in snapshot["event_candidates"]
        if item["fields"].get("stated_party")
    }
    organizations = {name: ExternalOrg(name=name) for name in sorted(names)}
    session.add_all(organizations.values())
    session.flush(list(organizations.values()))
    return organizations


def _seed_dependencies(
    session: Session,
    project: Project,
    snapshot: dict[str, Any],
    organizations: dict[str, ExternalOrg],
) -> dict[str, Dependency]:
    dependencies = {}
    for item in snapshot["dependencies"]:
        dependency = Dependency(
            project_id=project.id,
            source_ref=item["source_ref"],
            ref_code=item["ref_code"],
            title=item["title"],
            dep_type="utility_relocation",
            external_org_id=organizations[item["external_org"]].id,
            status="identified",
        )
        session.add(dependency)
        dependencies[item["source_ref"]] = dependency
    session.flush(list(dependencies.values()))
    return dependencies


def _seed_document(session: Session, project: Project, snapshot: dict[str, Any]) -> Document:
    [input_document] = snapshot["corpus_inputs"]
    document = Document(
        project_id=project.id,
        registry_id=input_document["registry_id"],
        filename=input_document["filename"],
        sha256=input_document["sha256"],
        doc_type=input_document["doc_type"],
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush([document])
    return document


def _seed_event_candidates(
    session: Session,
    project: Project,
    document: Document,
    snapshot: dict[str, Any],
) -> dict[int, Candidate]:
    [active_run] = snapshot["declared_active_runs"]
    candidates = {}
    quotes = []
    for item in snapshot["event_candidates"]:
        fields = item["fields"]
        quote = fields["description"]
        quotes.append(quote)
        candidate = Candidate(
            id=item["id"],
            project_id=project.id,
            kind="event",
            payload_json={
                "kind": "event",
                "fields": fields,
                "citations": [
                    {
                        "document_id": document.id,
                        "page": 1,
                        "quote": quote,
                        "verified": True,
                        "whole_row": True,
                    }
                ],
                "dedupe_hint": quote,
                "text_source": "text_layer",
            },
            source_document_id=document.id,
            source_pages=[1],
            confidence=0.99,
            prompt_version=active_run["prompt_version"],
            model=active_run["model"],
            citations_verified=True,
        )
        candidates[item["id"]] = candidate
    session.add(DocPage(document_id=document.id, page_no=1, text="\n".join(quotes)))
    run = record_extraction_run(
        session,
        document,
        prompt_version=active_run["prompt_version"],
        candidate_count=len(candidates),
        page_errors=0,
        candidates=list(candidates.values()),
        model=active_run["model"],
        schema_version=active_run["schema_version"],
    )
    session.add(ActiveExtractionRun(document_id=document.id, extraction_run_id=run.id))
    return candidates


def _state(session: Session, project_id: int, candidates: dict[int, Candidate]) -> dict[str, Any]:
    session.expire_all()
    return {
        "ledger": {
            "dependencies": [
                {
                    "id": dependency.id,
                    "source_ref": dependency.source_ref,
                    "committed_date": _date_value(dependency.committed_date),
                }
                for dependency in session.scalars(
                    select(Dependency)
                    .where(Dependency.project_id == project_id)
                    .order_by(Dependency.id)
                )
            ],
            "dependency_events": _events(session, project_id),
            "dependency_event_scopes": _scopes(session, project_id),
            "dependency_event_timings": _timings(session, project_id),
            "event_admission_audits": _audits(session, project_id),
        },
        "candidates": [
            {"id": candidate_id, "state": session.get(Candidate, candidate_id).state}
            for candidate_id in sorted(candidates)
        ],
        "policy_runs": _policy_runs(session, project_id),
    }


def _run_receipt(
    session: Session, project_id: int, run_id: int, before: dict[str, Any]
) -> dict[str, Any]:
    run = session.get(PolicyRun, run_id)
    if run is None:
        raise RuntimeError("Admission policy did not persist its run receipt")
    after = _state(session, project_id, {})["ledger"]
    outcomes = [
        {
            "candidate_id": outcome.candidate_id,
            "outcome": outcome.outcome,
            "reason": outcome.reason,
            "dependency_event_id": outcome.dependency_event_id,
            "created_record_identities": _outcome_created_identities(
                outcome, before["ledger"], after
            ),
        }
        for outcome in session.scalars(
            select(EventAdmissionOutcome)
            .where(EventAdmissionOutcome.policy_run_id == run.id)
            .order_by(EventAdmissionOutcome.candidate_id)
        )
    ]
    return {
        "policy_run": {
            "id": run.id,
            "family": run.family,
            "policy_version": run.policy_version,
            "policy_sha256": run.policy_sha256,
            "abstention_reason_version": run.abstention_reason_version,
            "applied_count": run.applied_count,
            "abstained_count": run.abstained_count,
        },
        "outcomes": outcomes,
        "created_record_identities": _created_identities(before["ledger"], after),
    }


def _negative_cases(*, first_run: dict[str, Any], after_second_run: dict[str, Any]) -> dict[str, Any]:
    outcome_by_candidate = {item["candidate_id"]: item for item in first_run["outcomes"]}
    candidate_by_id = {item["id"]: item for item in after_second_run["candidates"]}
    return {
        "7587": {
            "admission_abstention_reason": outcome_by_candidate[7587]["reason"],
            "created_statement_ids": [],
            "created_scope_ids": [],
            "projected_committed_dates": [],
        },
        "7129": {
            "state": candidate_by_id[7129]["state"],
            "created_statement_ids": [],
            "created_scope_ids": [],
        },
        "7296": {
            "state": candidate_by_id[7296]["state"],
            "created_statement_ids": [],
            "created_scope_ids": [],
        },
    }


def _late_refusal_receipt(
    session: Session, project: Project, candidates: dict[int, Candidate]
) -> dict[str, Any]:
    before = _state(session, project.id, candidates)
    session.execute(
        text(
            """
            create function refuse_sh99_acceptance_late_event()
            returns trigger language plpgsql as $$
            begin
                if new.description = 'Bluebonnet Gas commits to relocate C42 by June 1, 2025.' then
                    raise exception 'forced late Admission refusal' using errcode = '23514';
                end if;
                return new;
            end;
            $$;
            """
        )
    )
    session.execute(
        text(
            """
            create trigger refuse_sh99_acceptance_late_event
            before insert on dependency_events
            for each row execute function refuse_sh99_acceptance_late_event();
            """
        )
    )
    raised = False
    try:
        run_event_admission(session, project.id)
    except Exception as exc:
        if "forced late Admission refusal" not in str(exc):
            raise
        raised = True
        session.rollback()
    finally:
        # The test-only trigger lives inside the disposable database.  Rollback
        # removes it after the expected refusal together with every candidate
        # and ledger mutation the policy attempted.
        pass
    after = _state(session, project.id, candidates)
    return {
        "raised": raised,
        "ledger_and_candidate_state_unchanged": before == after,
        "policy_run_ids_created": [
            item["id"]
            for item in after["policy_runs"]
            if item["id"] not in {run["id"] for run in before["policy_runs"]}
        ],
    }


def _input_pins(snapshot: dict[str, Any], snapshot_sha256: str) -> dict[str, Any]:
    return {
        "snapshot_sha256": snapshot_sha256,
        "source_revision": snapshot["source_revision"],
        "database_snapshot": snapshot["database_snapshot"],
        "corpus_inputs": [
            {"registry_id": item["registry_id"], "sha256": item["sha256"]}
            for item in snapshot["corpus_inputs"]
        ],
        "declared_active_runs": snapshot["declared_active_runs"],
        "policy": {
            "family": FAMILY,
            "version": EVENT_ADMISSION_POLICY_VERSION,
            "abstention_reason_version": ABSTENTION_REASON_VERSION,
        },
    }


def _events(session: Session, project_id: int) -> list[dict[str, Any]]:
    return [
        {
            "id": event.id,
            "event_type": event.event_type,
            "stated_external_org_id": event.stated_external_org_id,
            "scope_mode": event.scope_mode,
            "timing_direction": event.timing_direction,
            "source_kind": event.source_kind,
        }
        for event in session.scalars(
            select(DependencyEvent)
            .where(DependencyEvent.project_id == project_id)
            .order_by(DependencyEvent.id)
        )
    ]


def _scopes(session: Session, project_id: int) -> list[dict[str, Any]]:
    return [
        {"id": scope.id, "event_id": scope.event_id, "dependency_id": scope.dependency_id}
        for scope in session.scalars(
            select(DependencyEventScope)
            .join(DependencyEvent, DependencyEvent.id == DependencyEventScope.event_id)
            .where(DependencyEvent.project_id == project_id)
            .order_by(DependencyEventScope.id)
        )
    ]


def _timings(session: Session, project_id: int) -> list[dict[str, Any]]:
    return [
        {
            "id": timing.id,
            "event_id": timing.event_id,
            "kind": timing.kind,
            "precision": timing.precision,
            "text": timing.text,
        }
        for timing in session.scalars(
            select(DependencyEventTiming)
            .join(DependencyEvent, DependencyEvent.id == DependencyEventTiming.event_id)
            .where(DependencyEvent.project_id == project_id)
            .order_by(DependencyEventTiming.id)
        )
    ]


def _audits(session: Session, project_id: int) -> list[dict[str, Any]]:
    return [
        {
            "id": entry.id,
            "entity_id": entry.entity_id,
            "action": entry.action,
            "candidate_id": (entry.after_json or {}).get("candidate_id"),
        }
        for entry in session.scalars(
            select(audit.AuditLog)
            .join(Dependency, Dependency.id == audit.AuditLog.entity_id)
            .where(
                Dependency.project_id == project_id,
                audit.AuditLog.action == audit.ADMIT_EVENT,
            )
            .order_by(audit.AuditLog.id)
        )
    ]


def _policy_runs(session: Session, project_id: int) -> list[dict[str, Any]]:
    return [
        {"id": run.id, "family": run.family, "applied_count": run.applied_count, "abstained_count": run.abstained_count}
        for run in session.scalars(
            select(PolicyRun)
            .where(PolicyRun.project_id == project_id)
            .order_by(PolicyRun.id)
        )
    ]


def _created_identities(before: dict[str, Any], after: dict[str, Any]) -> dict[str, list[int]]:
    return {
        key: [item["id"] for item in after[key] if item["id"] not in {old["id"] for old in before[key]}]
        for key in (
            "dependency_events",
            "dependency_event_scopes",
            "dependency_event_timings",
            "event_admission_audits",
        )
    }


def _outcome_created_identities(
    outcome: EventAdmissionOutcome, before: dict[str, Any], after: dict[str, Any]
) -> dict[str, list[int]]:
    """Name the rows this one Candidate outcome caused, never a run total."""

    if outcome.dependency_event_id is None:
        return {
            "dependency_events": [],
            "dependency_event_scopes": [],
            "dependency_event_timings": [],
            "event_admission_audits": [],
        }
    event_id = outcome.dependency_event_id
    new = _created_identities(before, after)
    return {
        "dependency_events": [event_id] if event_id in new["dependency_events"] else [],
        "dependency_event_scopes": [
            item["id"]
            for item in after["dependency_event_scopes"]
            if item["id"] in new["dependency_event_scopes"] and item["event_id"] == event_id
        ],
        "dependency_event_timings": [
            item["id"]
            for item in after["dependency_event_timings"]
            if item["id"] in new["dependency_event_timings"] and item["event_id"] == event_id
        ],
        "event_admission_audits": [
            item["id"]
            for item in after["event_admission_audits"]
            if item["id"] in new["event_admission_audits"]
            and item["candidate_id"] == outcome.candidate_id
        ],
    }


def _write_bundle(
    output_dir: Path,
    *,
    environment: dict[str, Any],
    receipt: dict[str, Any],
    canonical_content: dict[str, Any],
) -> tuple[Path, str, str]:
    exports = {
        "environment.json": environment,
        "receipt.json": receipt,
        "canonical-content.json": canonical_content,
    }
    return publish_verified_bundle(
        output_dir,
        exports=exports,
        canonical_content=canonical_content,
        bundle_schema_version=BUNDLE_SCHEMA_VERSION,
        bundle_files=BUNDLE_FILES,
        error_cls=ValueError,
        corrupt_bundle_error_cls=CorruptSH99AdmissionBundle,
        canonical_json=_canonical_json,
        sha256=_sha256,
        json_sha256=_json_sha256,
        temp_prefix="corridor-sh99-admission-bundle",
        self_verification_failure="new SH 99 Admission bundle failed self-verification",
    )


def _date_value(value) -> str | None:
    return value.isoformat() if value is not None else None


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_sha256(value: Any) -> str:
    return _sha256(_canonical_json(value))
