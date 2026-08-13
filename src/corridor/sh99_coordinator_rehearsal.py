"""Seal the bounded SH 99 coordinator rehearsal without changing product behavior.

The coordinator exercise is evidence about the already-shipped guided-statement,
Report, and release paths.  It is deliberately not another application service:
the acceptance bundle records the pinned inputs, separate operations and
coordinator timings, the release identity, and any assistance or failure.  A
bundle can therefore prove an unqualified pass only when the measured run had
neither assistance nor an error; it never turns an internal rehearsal into
customer-usability evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from html import unescape
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import subprocess
import tempfile
from time import monotonic
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from corridor.m8_acceptance_bundle import VerificationResult, publish_verified_bundle, verify_bundle
from corridor.m8_acceptance_database import (
    DatabaseProvisioner,
    provision_disposable_postgres,
    read_migration_head,
)
from corridor.models import (
    Candidate,
    Dependency,
    DependencyEvent,
    DependencyEventScope,
    DependencyEventTiming,
    EventAdmissionOutcome,
    ExternalReportRelease,
    Project,
    ProjectRosterEntry,
    StatementCoordinationReceipt,
)
from corridor.principals import HumanPrincipal
from corridor.report_release import retrieve_released_external_report
from corridor.sh99_admission_acceptance import (
    _database_url,
    _dump_source_database,
    _project_state,
    _read_project_state,
    _require_clean_source,
    _restore_source_database,
    _source_database,
    _source_migration_head,
)
from corridor.web.app import app, get_human_principal, get_session
from corridor.work_list import build_work_list


BUNDLE_SCHEMA_VERSION = "corridor.sh99-coordinator-rehearsal-bundle.v1"
BUNDLE_FILES = ("receipt.json", "canonical-content.json", "environment.json")
CLAIM_BOUNDARY = {
    "internal_workflow_rehearsal": True,
    "customer_usability_validation": False,
    "provisional_targets": True,
}
DATABASE_PREFIX = "corridor_sh99_coordinator_rehearsal_"
_RELEASE_ID_ASSISTANCE = (
    "release required an artifact identity not exposed by a coordinator screen"
)
_SUPPORTING_EVIDENCE_ASSISTANCE = (
    "supporting party Evidence was entered from the pinned source page because the "
    "coordination screen does not expose that page text"
)
_KINER_MORGAN_PHRASE = "March 2026 completion timeline"
_EQUISTAR_PHRASE = "chain of title on the ROW agreement"
_COORDINATE_HREF = re.compile(
    r'<a\s+class="action"\s+href="(?P<href>/statements/[^\"]+/coordinate)">',
    re.IGNORECASE,
)
_EVIDENCE_HEADER = re.compile(
    r'<span class="muted">(?P<filename>.*?)\s+·\s+registered page\s+(?P<page>\d+)</span>',
    re.DOTALL,
)


class CorruptSH99CoordinatorRehearsalBundle(ValueError):
    """An exported coordinator-rehearsal bundle does not match its identity."""


@dataclass(frozen=True)
class CoordinatorRehearsalCapture:
    """The immutable facts observed in one bounded coordinator exercise."""

    inputs: dict[str, Any]
    operations: dict[str, Any]
    coordinator: dict[str, Any]
    outcome: dict[str, Any]
    verification: dict[str, Any]


@dataclass(frozen=True)
class CoordinatorRehearsalBundleSummary:
    """Caller-held identities for a newly published closed bundle."""

    bundle_dir: Path
    manifest_path: Path
    integrity_manifest_sha256: str
    canonical_content_sha256: str


@dataclass(frozen=True)
class SH99CoordinatorRehearsalConfig:
    """Pins the source and the previously approved mechanical operation."""

    project_slug: str
    source_database_url: str
    postgres_admin_url: str
    expected_clean_git_revision: str
    shared_admission_receipt_path: Path
    expected_shared_admission_receipt_sha256: str
    approved_shared_state_receipt: str
    shared_backfill_elapsed_seconds: float
    output_dir: Path
    coordinator_subject: str = "local:sh99-coordinator"
    coordinator_display_name: str = "SH 99 Coordinator"


@dataclass(frozen=True)
class SH99CoordinatorRehearsalSummary:
    """The sealed result of a replay run on a disposable cloned database."""

    bundle: CoordinatorRehearsalBundleSummary
    database_name: str
    status: str


def publish_coordinator_rehearsal_bundle(
    output_dir: Path,
    capture: CoordinatorRehearsalCapture,
) -> CoordinatorRehearsalBundleSummary:
    """Publish one immutable, independently verifiable rehearsal observation."""

    canonical_content = _canonical_content(capture)
    receipt = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        **canonical_content,
    }
    environment = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "rehearsed_at": datetime.now(timezone.utc).isoformat(),
        "source_revision": capture.inputs["source_revision"],
    }
    manifest_path, manifest_sha256, canonical_sha256 = publish_verified_bundle(
        output_dir,
        exports={
            "receipt.json": receipt,
            "canonical-content.json": canonical_content,
            "environment.json": environment,
        },
        canonical_content=canonical_content,
        bundle_schema_version=BUNDLE_SCHEMA_VERSION,
        bundle_files=BUNDLE_FILES,
        error_cls=ValueError,
        corrupt_bundle_error_cls=CorruptSH99CoordinatorRehearsalBundle,
        canonical_json=_canonical_json,
        sha256=_sha256,
        json_sha256=_json_sha256,
        temp_prefix="corridor-sh99-coordinator-rehearsal",
        self_verification_failure="new coordinator rehearsal bundle failed self-verification",
    )
    return CoordinatorRehearsalBundleSummary(
        bundle_dir=output_dir,
        manifest_path=manifest_path,
        integrity_manifest_sha256=manifest_sha256,
        canonical_content_sha256=canonical_sha256,
    )


def verify_coordinator_rehearsal_bundle(
    bundle_dir: Path, *, expected_integrity_manifest_sha256: str
) -> VerificationResult:
    """Verify the closed export without a database or a mutable output path."""

    return verify_bundle(
        bundle_dir,
        expected_integrity_manifest_sha256=expected_integrity_manifest_sha256,
        bundle_schema_version=BUNDLE_SCHEMA_VERSION,
        bundle_files=BUNDLE_FILES,
        corrupt_bundle_error_cls=CorruptSH99CoordinatorRehearsalBundle,
        sha256=_sha256,
        json_sha256=_json_sha256,
    )


def run_sh99_coordinator_rehearsal(
    config: SH99CoordinatorRehearsalConfig,
    *,
    provision_database: DatabaseProvisioner | None = None,
) -> SH99CoordinatorRehearsalSummary:
    """Run the existing coordinator UI on a pinned disposable SH 99 clone.

    Operations may inspect and clone the source database.  The measured
    coordinator segment uses only the existing web application routes; its
    output records the two current product gaps rather than patching around
    them.  The original source is never changed.
    """

    if provision_database is None:
        provision_database = _provision_database
    source = _require_clean_source(config.expected_clean_git_revision)
    source_head = _source_migration_head()
    if read_migration_head(
        config.source_database_url,
        repo_root=Path(__file__).resolve().parents[2],
        error_cls=ValueError,
    ) != source_head:
        raise ValueError("shared database migration head does not match checked-out source")
    source_database = _source_database(config.source_database_url)
    shared_admission_receipt = _verified_shared_admission_receipt(config)
    source_state = _read_project_state(config.source_database_url, config.project_slug)
    _require_admitted_source_state(source_state)

    operations_started = monotonic()
    source_dump_sha256: str | None = None
    database_name = "not-provisioned"
    interactions: list[str] = []
    assistance: list[str] = []
    errors: list[str] = []
    release_identity: dict[str, Any] | None = None
    verification: dict[str, Any] = {}
    coordinator_elapsed = 0.0
    scenario_timings: dict[str, float] = {}
    with tempfile.TemporaryDirectory(prefix="corridor-sh99-coordinator-source-") as parent:
        dump_path = Path(parent) / "source.dump"
        _dump_source_database(source_database, dump_path)
        source_dump_sha256 = _sha256(dump_path.read_bytes())
        with provision_database(config.postgres_admin_url) as database:
            database_name = database.name
            if database.migration_head != source_head:
                raise ValueError(
                    "disposable database migration head does not match checked-out source"
                )
            _restore_source_database(source_database, dump_path, database.name)
            clone_url = _database_url(config.postgres_admin_url, database.name)
            if _read_project_state(clone_url, config.project_slug) != source_state:
                raise ValueError("restored coordinator clone does not match the pinned source state")

            clone_engine = create_engine(clone_url, poolclass=NullPool, future=True)
            try:
                with Session(clone_engine) as session:
                    _seed_coordinator(session, config)
                    session.commit()
                    operations_elapsed = monotonic() - operations_started
                    try:
                        journey = _run_ordinary_interface_journey(session, config)
                        interactions.extend(journey["interactions"])
                        assistance.extend(journey["assistance"])
                        coordinator_elapsed = journey["elapsed_seconds"]
                        scenario_timings = journey["scenario_timings"]
                        release_identity = journey["released_pdf"]
                    except _JourneyFailure as exc:
                        interactions.extend(exc.interactions)
                        assistance.extend(exc.assistance)
                        errors.append(str(exc))
                        coordinator_elapsed = exc.elapsed_seconds
                        scenario_timings = exc.scenario_timings
                    try:
                        verification = _verify_clone_result(
                            session,
                            project_slug=config.project_slug,
                            release_identity=release_identity,
                        )
                    except (RuntimeError, ValueError) as exc:
                        errors.append(f"post-rehearsal verification: {exc}")
                        verification = {"valid": False, "error": str(exc)}
            finally:
                clone_engine.dispose()

    coverage = _run_scope_coverage()
    verification["automated_scope_coverage"] = coverage
    if coverage["returncode"] != 0:
        errors.append("selected and all-active scope coverage did not pass")
    if source_dump_sha256 is None:
        raise RuntimeError("source dump digest was not captured")
    status = _rehearsal_status(
        assistance=assistance,
        errors=errors,
        scenario_timings=scenario_timings,
    )
    inputs = {
        "source_revision": source["revision"],
        "source_snapshot_sha256": _json_sha256(source_state),
        "source_dump_sha256": source_dump_sha256,
        "shared_admission_receipt": shared_admission_receipt,
        "approved_shared_state_receipt": config.approved_shared_state_receipt,
        "corpus_inputs": [
            {
                "document_id": document["id"],
                "filename": document["filename"],
                "sha256": document["sha256"],
            }
            for document in source_state["documents"]
        ],
        "active_runs": source_state["active_runs"],
        "policy_runs": source_state["policy_runs"],
        "seeded_coordinator": {
            "subject": config.coordinator_subject,
            "display_name": config.coordinator_display_name,
        },
        "scenario_candidates": {"7296": 7296, "7129": 7129, "7587": 7587},
    }
    bundle = publish_coordinator_rehearsal_bundle(
        config.output_dir,
        CoordinatorRehearsalCapture(
            inputs=inputs,
            operations={
                "elapsed_seconds": operations_elapsed,
                "backfill_elapsed_seconds": config.shared_backfill_elapsed_seconds,
                "source_database_mutated": False,
            },
            coordinator={
                "elapsed_seconds": coordinator_elapsed,
                "scenario_timings": scenario_timings,
                "interactions": interactions,
            },
            outcome={
                "status": status,
                "assistance": assistance,
                "errors": errors,
                "deviations": [],
                "released_pdf": release_identity,
            },
            verification=verification,
        ),
    )
    return SH99CoordinatorRehearsalSummary(
        bundle=bundle,
        database_name=database_name,
        status=status,
    )


def _provision_database(admin_url: str):
    return provision_disposable_postgres(
        admin_url,
        repo_root=Path(__file__).resolve().parents[2],
        error_cls=ValueError,
        database_prefix=DATABASE_PREFIX,
    )


def _verified_shared_admission_receipt(
    config: SH99CoordinatorRehearsalConfig,
) -> dict[str, str]:
    """Bind the rehearsal to the closed receipt of the actual shared operation."""

    try:
        receipt_bytes = config.shared_admission_receipt_path.read_bytes()
        receipt = json.loads(receipt_bytes)
        protected = receipt["protected_candidates"]
    except (KeyError, OSError, json.JSONDecodeError) as exc:
        raise ValueError("prior shared SH 99 Admission receipt is incomplete") from exc
    receipt_sha256 = _sha256(receipt_bytes)
    if receipt_sha256 != config.expected_shared_admission_receipt_sha256:
        raise ValueError("prior shared SH 99 Admission receipt does not match its pin")
    if receipt.get("schema_version") != "corridor.issue-250.shared-admission-audit.v1":
        raise ValueError("prior shared SH 99 Admission receipt has an unsupported schema")
    checks = receipt.get("checks")
    if not isinstance(checks, dict) or not all(checks.values()):
        raise ValueError("prior shared SH 99 Admission receipt did not validate every check")
    expected = {
        "7587": "abstained",
        "7129": "abstained",
        "7296": "abstained",
    }
    for candidate_id, outcome in expected.items():
        observed = protected.get(candidate_id)
        if not isinstance(observed, dict):
            raise ValueError(f"prior Admission receipt omits Candidate {candidate_id}")
        admission = observed.get("event_outcome")
        if not isinstance(admission, dict) or admission.get("outcome") != outcome:
            raise ValueError(
                f"prior shared Admission receipt does not prove Candidate {candidate_id} abstained"
            )
        if observed.get("state") != "pending" or admission.get("dependency_event_id") is not None:
            raise ValueError(
                f"prior shared Admission receipt does not keep Candidate {candidate_id} as pending residue"
            )
    return {
        "sha256": receipt_sha256,
        "schema_version": receipt["schema_version"],
        "approval_comment_url": str(receipt.get("approval_comment_url") or ""),
        "approved_source_revision": str(receipt.get("approved_source_revision") or ""),
        "acceptance_manifest_sha256": str(receipt.get("acceptance_manifest_sha256") or ""),
        "after_sha256": str(receipt.get("after_sha256") or ""),
    }


def _require_admitted_source_state(source_state: dict[str, Any]) -> None:
    candidates = {candidate["id"]: candidate for candidate in source_state["candidates"]}
    for candidate_id in (7129, 7296, 7587):
        if candidates.get(candidate_id, {}).get("state") != "pending":
            raise ValueError(f"shared state does not leave Candidate {candidate_id} pending")
    outcomes = {
        outcome["candidate_id"]: outcome
        for outcome in source_state["event_admission_outcomes"]
    }
    abstention = outcomes.get(7587)
    if abstention is None or abstention.get("outcome") != "abstained":
        raise ValueError("shared state does not retain Candidate 7587 Admission Abstention")
    if abstention.get("dependency_event_id") is not None:
        raise ValueError("Candidate 7587 Admission Abstention created a statement")


def _seed_coordinator(session: Session, config: SH99CoordinatorRehearsalConfig) -> None:
    project = session.scalar(select(Project).where(Project.slug == config.project_slug))
    if project is None:
        raise ValueError(f"no project with slug {config.project_slug!r}")
    HumanPrincipal(config.coordinator_subject)
    existing = session.scalar(
        select(ProjectRosterEntry).where(
            ProjectRosterEntry.project_id == project.id,
            ProjectRosterEntry.principal_subject == config.coordinator_subject,
        )
    )
    if existing is None:
        session.add(
            ProjectRosterEntry(
                project_id=project.id,
                principal_subject=config.coordinator_subject,
                display_name=config.coordinator_display_name,
            )
        )
        session.flush()
        return
    if existing.display_name != config.coordinator_display_name:
        raise ValueError("seeded coordinator identity disagrees with existing project roster")


class _JourneyFailure(RuntimeError):
    """One normal-interface operation failed before the rehearsal could finish."""

    def __init__(
        self,
        message: str,
        *,
        interactions: list[str],
        assistance: list[str],
        elapsed_seconds: float,
        scenario_timings: dict[str, float],
    ) -> None:
        super().__init__(message)
        self.interactions = interactions
        self.assistance = assistance
        self.elapsed_seconds = elapsed_seconds
        self.scenario_timings = scenario_timings


def _run_ordinary_interface_journey(
    session: Session, config: SH99CoordinatorRehearsalConfig
) -> dict[str, Any]:
    """Exercise existing HTTP controls without querying during coordinator time."""

    interactions: list[str] = []
    assistance: list[str] = []
    scenario_timings: dict[str, float] = {}
    started = monotonic()
    sentinel = object()
    old_session = app.dependency_overrides.get(get_session, sentinel)
    old_principal = app.dependency_overrides.get(get_human_principal, sentinel)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(
        config.coordinator_subject
    )
    try:
        with TestClient(app) as client:
            _record_interaction(interactions, "coordinator_home")
            home = client.get(f"/work/{config.project_slug}")
            _require_response(
                home.status_code,
                200,
                "coordinator home did not load",
                interactions,
                assistance,
                started,
                scenario_timings,
            )
            km_started = monotonic()
            km_url, km_screen = _find_statement_screen(
                client,
                home.text,
                _KINER_MORGAN_PHRASE,
                interactions,
                assistance,
                started,
                scenario_timings,
            )
            _record_interaction(interactions, "save_kinder_morgan_statement")
            assistance.append(_SUPPORTING_EVIDENCE_ASSISTANCE)
            saved_km = client.post(
                km_url,
                data=_kinder_morgan_form(km_screen, config.coordinator_display_name),
                follow_redirects=False,
            )
            _require_response(
                saved_km.status_code,
                303,
                "Kinder Morgan guided Save did not complete",
                interactions,
                assistance,
                started,
                scenario_timings,
            )
            scenario_timings["7296"] = monotonic() - km_started

            _record_interaction(interactions, "coordinator_home_after_kinder_morgan")
            refreshed_home = client.get(f"/work/{config.project_slug}")
            _require_response(
                refreshed_home.status_code,
                200,
                "coordinator home did not refresh",
                interactions,
                assistance,
                started,
                scenario_timings,
            )
            equistar_started = monotonic()
            equistar_url, equistar_screen = _find_statement_screen(
                client,
                refreshed_home.text,
                _EQUISTAR_PHRASE,
                interactions,
                assistance,
                started,
                scenario_timings,
            )
            _record_interaction(interactions, "save_equistar_statement")
            saved_equistar = client.post(
                equistar_url,
                data=_equistar_form(equistar_screen, config.coordinator_display_name),
                follow_redirects=False,
            )
            _require_response(
                saved_equistar.status_code,
                303,
                "Equistar guided Save did not complete",
                interactions,
                assistance,
                started,
                scenario_timings,
            )

            _record_interaction(interactions, "render_internal_report")
            rendered = client.post(f"/reports/{config.project_slug}/render")
            _require_response(
                rendered.status_code,
                201,
                "automatic internal Report did not render",
                interactions,
                assistance,
                started,
                scenario_timings,
            )
            artifact_id = rendered.json().get("artifact_id")
            if not isinstance(artifact_id, int) or artifact_id <= 0:
                raise _journey_failure(
                    "Report render did not return a fixed artifact identity",
                    interactions,
                    assistance,
                    started,
                    scenario_timings,
                )
            assistance.append(_RELEASE_ID_ASSISTANCE)
            _record_interaction(interactions, "release_fixed_pdf")
            released = client.post(
                f"/reports/{config.project_slug}/release",
                data={"artifact_id": str(artifact_id)},
            )
            _require_response(
                released.status_code,
                201,
                "fixed PDF release did not complete",
                interactions,
                assistance,
                started,
                scenario_timings,
            )
            scenario_timings["7129_and_release"] = monotonic() - equistar_started
            release = released.json()
            return {
                "elapsed_seconds": monotonic() - started,
                "scenario_timings": scenario_timings,
                "interactions": interactions,
                "assistance": assistance,
                "released_pdf": {
                    "release_id": release.get("release_id"),
                    "artifact_name": release.get("artifact_name"),
                    "sha256": release.get("pdf_sha256"),
                },
            }
    except ValueError as exc:
        raise _journey_failure(
            str(exc),
            interactions,
            assistance,
            started,
            scenario_timings,
        ) from exc
    finally:
        _restore_override(get_session, old_session, sentinel)
        _restore_override(get_human_principal, old_principal, sentinel)


def _record_interaction(interactions: list[str], name: str) -> None:
    """Record named product actions without leaking a coordinator-facing ID."""

    interactions.append(name)


def _find_statement_screen(
    client: TestClient,
    home_html: str,
    phrase: str,
    interactions: list[str],
    assistance: list[str],
    started: float,
    scenario_timings: dict[str, float],
) -> tuple[str, str]:
    urls = _COORDINATE_HREF.findall(home_html)
    if len(urls) != 1:
        raise _journey_failure(
            "coordinator home does not distinguish the required statement among "
            f"{len(urls)} identical work-list cards",
            interactions,
            assistance,
            started,
            scenario_timings,
        )
    for url in urls:
        _record_interaction(interactions, "open_statement_from_work_list")
        screen = client.get(unescape(url))
        _require_response(
            screen.status_code,
            200,
            "statement screen did not load",
            interactions,
            assistance,
            started,
            scenario_timings,
        )
        if phrase in unescape(screen.text):
            return unescape(url), screen.text
    raise _journey_failure(
        f"work list did not expose the required statement containing {phrase!r}",
        interactions,
        assistance,
        started,
        scenario_timings,
    )


def _kinder_morgan_form(screen_html: str, coordinator_display_name: str) -> dict[str, str]:
    fields = _ScreenFields(screen_html)
    filename, page_no = _candidate_evidence_identity(screen_html)
    return {
        **fields.hidden,
        "affected_external_org_id": fields.option_value("affected_external_org_id", "Kinder Morgan"),
        "stated_party": "Kinder Morgan",
        "stated_external_org_id": fields.option_value("stated_external_org_id", "Kinder Morgan"),
        "event_date": "2025-01-16",
        "description": (
            "Kinder Morgan stated that the March 2026 completion timeline appears "
            "unattainable and proposed extending it to May 16."
        ),
        "new_timing_text": "May 16th",
        "new_timing_precision": "day",
        "new_timing_start_date": "2026-05-16",
        "new_timing_end_date": "2026-05-16",
        "previous_timing_text": "March 2026",
        "previous_timing_precision": "month",
        "previous_timing_start_date": "2026-03-01",
        "previous_timing_end_date": "2026-03-31",
        "supporting_document_id": fields.option_value("supporting_document_id", filename),
        "supporting_page_no": page_no,
        "supporting_quote": "Kinder Morgan Management Meeting Highlights",
        "scope_mode": "unknown",
        "internal_owner_roster_entry_id": fields.option_value(
            "internal_owner_roster_entry_id", coordinator_display_name
        ),
        "next_action": "Confirm the revised completion plan with Kinder Morgan",
        "action_due_date_unknown_reason": "awaiting_schedule_information",
        "milestone_impact": "not_yet_known",
    }


def _equistar_form(screen_html: str, coordinator_display_name: str) -> dict[str, str]:
    fields = _ScreenFields(screen_html)
    return {
        **fields.hidden,
        "affected_external_org_id": fields.option_value("affected_external_org_id", "Equistar"),
        "stated_party": "Equistar",
        "stated_external_org_id": fields.option_value("stated_external_org_id", "Equistar"),
        "event_date": "2024-12-04",
        "description": (
            "Equistar committed to provide a chain of title for the ROW agreement "
            "in DOW’s name, due in January 2025."
        ),
        "new_timing_text": "01/2025",
        "new_timing_precision": "month",
        "new_timing_start_date": "2025-01-01",
        "new_timing_end_date": "2025-01-31",
        "scope_mode": "unknown",
        "internal_owner_roster_entry_id": fields.option_value(
            "internal_owner_roster_entry_id", coordinator_display_name
        ),
        "next_action": "Confirm the chain-of-title delivery with Equistar",
        "action_due_date_unknown_reason": "awaiting_external_information",
    }


class _ScreenFields(HTMLParser):
    """Read form labels and browser-carried hidden values from an existing screen."""

    def __init__(self, source: str) -> None:
        super().__init__()
        self.hidden: dict[str, str] = {}
        self.options: dict[str, list[tuple[str, str]]] = {}
        self._select_name: str | None = None
        self._option_value: str | None = None
        self._option_text: list[str] = []
        self.feed(source)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "input" and values.get("type") == "hidden" and values.get("name"):
            self.hidden[values["name"]] = values.get("value") or ""
        elif tag == "select":
            self._select_name = values.get("name")
        elif tag == "option" and self._select_name is not None:
            self._option_value = values.get("value") or ""
            self._option_text = []

    def handle_data(self, data: str) -> None:
        if self._option_value is not None:
            self._option_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "option" and self._select_name is not None and self._option_value is not None:
            self.options.setdefault(self._select_name, []).append(
                (self._option_value, "".join(self._option_text).strip())
            )
            self._option_value = None
            self._option_text = []
        elif tag == "select":
            self._select_name = None

    def option_value(self, field: str, label: str) -> str:
        for value, text in self.options.get(field, []):
            if text == label and value:
                return value
        raise ValueError(f"coordinator screen does not offer {label!r} for {field}")


def _candidate_evidence_identity(screen_html: str) -> tuple[str, str]:
    match = _EVIDENCE_HEADER.search(screen_html)
    if match is None:
        raise ValueError("coordinator screen does not display Candidate Evidence identity")
    filename = unescape(re.sub(r"<[^>]+>", "", match.group("filename"))).strip()
    return filename, match.group("page")


def _require_response(
    observed: int,
    expected: int,
    message: str,
    interactions: list[str],
    assistance: list[str],
    started: float,
    scenario_timings: dict[str, float],
) -> None:
    if observed != expected:
        raise _journey_failure(
            f"{message}: expected HTTP {expected}, observed {observed}",
            interactions,
            assistance,
            started,
            scenario_timings,
        )


def _journey_failure(
    message: str,
    interactions: list[str],
    assistance: list[str],
    started: float,
    scenario_timings: dict[str, float],
) -> _JourneyFailure:
    return _JourneyFailure(
        message,
        interactions=interactions,
        assistance=assistance,
        elapsed_seconds=monotonic() - started,
        scenario_timings=scenario_timings,
    )


def _restore_override(key, previous: object, sentinel: object) -> None:
    if previous is sentinel:
        app.dependency_overrides.pop(key, None)
    else:
        app.dependency_overrides[key] = previous


def _verify_clone_result(
    session: Session,
    *,
    project_slug: str,
    release_identity: dict[str, Any] | None,
) -> dict[str, Any]:
    """Read durable effects after timing stops; this is verifier work, not UI work."""

    project = session.scalar(select(Project).where(Project.slug == project_slug))
    if project is None:
        raise RuntimeError("coordinator clone lost its project")
    candidates = {candidate.id: candidate for candidate in session.scalars(
        select(Candidate).where(Candidate.project_id == project.id, Candidate.id.in_((7296, 7129, 7587)))
    )}
    receipts = {
        receipt.candidate_id: receipt
        for receipt in session.scalars(
            select(StatementCoordinationReceipt).where(
                StatementCoordinationReceipt.candidate_id.in_((7296, 7129))
            )
        )
    }
    km_receipt = receipts.get(7296)
    equistar_receipt = receipts.get(7129)
    if km_receipt is None or equistar_receipt is None:
        raise ValueError("guided coordinator flow did not persist both grouping receipts")
    km_event = session.get(DependencyEvent, km_receipt.dependency_event_id)
    equistar_event = session.get(DependencyEvent, equistar_receipt.dependency_event_id)
    if km_event is None or equistar_event is None:
        raise RuntimeError("guided coordinator receipt has no durable statement")
    _require(km_event.event_type == "committed_date_change", "Candidate 7296 is not a Committed Date Change")
    _require(km_event.timing_direction == "later", "Candidate 7296 direction is not later")
    _require(km_event.scope_mode == "unknown", "Candidate 7296 scope is not unknown")
    _require(equistar_event.event_type == "commitment", "Candidate 7129 is not a Commitment")
    _require(equistar_event.scope_mode == "unknown", "Candidate 7129 scope is not unknown")
    equistar_timing = session.scalar(
        select(DependencyEventTiming).where(
            DependencyEventTiming.event_id == equistar_event.id,
            DependencyEventTiming.kind == "new",
        )
    )
    _require(
        equistar_timing is not None
        and equistar_timing.precision == "month"
        and equistar_timing.start_date is not None
        and equistar_timing.start_date.isoformat() == "2025-01-01"
        and equistar_timing.end_date is not None
        and equistar_timing.end_date.isoformat() == "2025-01-31",
        "Candidate 7129 did not preserve January 2025 month precision",
    )
    scope_links = {
        candidate_id: session.scalars(
            select(DependencyEventScope).where(
                DependencyEventScope.event_id == receipt.dependency_event_id
            )
        ).all()
        for candidate_id, receipt in receipts.items()
    }
    _require(not scope_links[7296] and not scope_links[7129], "unknown scope changed a Dependency")
    work_list = build_work_list(session, project.id)
    equistar_work = next(
        (
            item
            for item in (*work_list.immediate, *work_list.backlog)
            if item.statement_event_id == equistar_event.id
        ),
        None,
    )
    _require(
        equistar_work is not None and equistar_work.past_due is not None,
        "Candidate 7129 did not produce its party-level past-due derivation",
    )
    _require(
        candidates.get(7587) is not None and candidates[7587].state == "pending",
        "Candidate 7587 did not remain pending",
    )
    abstention = session.scalar(
        select(EventAdmissionOutcome).where(EventAdmissionOutcome.candidate_id == 7587)
    )
    _require(
        abstention is not None
        and abstention.outcome == "abstained"
        and abstention.dependency_event_id is None,
        "Candidate 7587 no longer has its Admission Abstention",
    )
    if release_identity is None or not isinstance(release_identity.get("release_id"), int):
        raise ValueError("coordinator flow did not produce a release receipt")
    release = retrieve_released_external_report(session, project.id, release_identity["release_id"])
    released_party_event_ids = {
        entry["current_statement_event_id"]
        for entry in release.record_context_json.get("party_statements", [])
    }
    _require(
        {km_event.id, equistar_event.id}.issubset(released_party_event_ids),
        "released PDF does not cover both open unknown-scope statements",
    )
    return {
        "candidate_7296": {
            "state": candidates[7296].state,
            "statement_event_id": km_event.id,
            "event_type": km_event.event_type,
            "direction": km_event.timing_direction,
            "scope": km_event.scope_mode,
            "coordination_receipt_id": km_receipt.id,
        },
        "candidate_7129": {
            "state": candidates[7129].state,
            "statement_event_id": equistar_event.id,
            "event_type": equistar_event.event_type,
            "scope": equistar_event.scope_mode,
            "timing": {
                "text": equistar_timing.text,
                "precision": equistar_timing.precision,
                "start_date": equistar_timing.start_date.isoformat(),
                "end_date": equistar_timing.end_date.isoformat(),
            },
            "past_due": {
                "evaluated_on": equistar_work.past_due.evaluated_on.isoformat(),
                "due_after": equistar_work.past_due.due_after.isoformat(),
                "ruleset_version": equistar_work.past_due.ruleset_version,
            },
            "coordination_receipt_id": equistar_receipt.id,
        },
        "candidate_7587": {
            "state": candidates[7587].state,
            "admission_outcome": abstention.outcome,
            "statement_event_id": abstention.dependency_event_id,
        },
        "release": {
            "release_id": release.id,
            "artifact_name": release.artifact_name,
            "sha256": release.pdf_sha256,
            "evaluated_on": release.evaluated_on.isoformat(),
            "ruleset_version": release.ruleset_version,
            "provenance_mode": release.provenance_mode,
            "released_by": release.released_by,
            "released_at": release.released_at.isoformat(),
            "record_context": release.record_context_json,
            "evaluation_context": release.evaluation_context_json,
        },
    }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _run_scope_coverage() -> dict[str, Any]:
    """Record the existing public all-active/selected coverage outside timed work."""

    argv = [
        "uv",
        "run",
        "pytest",
        "tests/test_statement_coordination.py::test_command_records_each_explicit_scope_mode",
        "-q",
    ]
    completed = subprocess.run(
        argv,
        cwd=Path(__file__).resolve().parents[2],
        check=False,
        capture_output=True,
        text=True,
    )
    output = (completed.stdout + completed.stderr).strip()
    return {
        "argv": argv,
        "returncode": completed.returncode,
        "output_sha256": _sha256(output.encode()),
        "covered_scope_modes": ["selected", "all_active"],
    }


def _rehearsal_status(
    *,
    assistance: list[str],
    errors: list[str],
    scenario_timings: dict[str, float],
) -> str:
    targets = {"7296": 5 * 60, "7129_and_release": 3 * 60}
    target_miss = any(
        scenario_timings.get(name, float("inf")) > target
        for name, target in targets.items()
    )
    return "passed" if not assistance and not errors and not target_miss else "failed"


def _canonical_content(capture: CoordinatorRehearsalCapture) -> dict[str, Any]:
    _require_capture_shape(capture)
    outcome = dict(capture.outcome)
    assistance = outcome.get("assistance")
    errors = outcome.get("errors")
    unqualified_pass = (
        outcome["status"] == "passed" and not assistance and not errors
    )
    supplied_unqualified = outcome.pop("unqualified_pass", None)
    if supplied_unqualified is True and not unqualified_pass:
        raise ValueError("an assisted or failed rehearsal cannot be an unqualified pass")
    outcome["unqualified_pass"] = unqualified_pass
    return {
        "claim_boundary": CLAIM_BOUNDARY,
        "inputs": capture.inputs,
        "operations": capture.operations,
        "coordinator": capture.coordinator,
        "outcome": outcome,
        "verification": capture.verification,
    }


def _require_capture_shape(capture: CoordinatorRehearsalCapture) -> None:
    required_inputs = {
        "source_revision",
        "source_snapshot_sha256",
        "shared_admission_receipt",
        "corpus_inputs",
        "active_runs",
        "policy_runs",
        "seeded_coordinator",
        "scenario_candidates",
    }
    missing_inputs = sorted(required_inputs - set(capture.inputs))
    if missing_inputs:
        raise ValueError(f"rehearsal inputs are missing {', '.join(missing_inputs)}")
    if capture.outcome.get("status") not in {"passed", "failed"}:
        raise ValueError("rehearsal outcome status must be passed or failed")
    for section, values, fields in (
        ("operations", capture.operations, {"elapsed_seconds", "backfill_elapsed_seconds"}),
        ("coordinator", capture.coordinator, {"elapsed_seconds", "scenario_timings", "interactions"}),
        ("outcome", capture.outcome, {"assistance", "errors", "deviations", "released_pdf"}),
    ):
        missing = sorted(fields - set(values))
        if missing:
            raise ValueError(f"{section} is missing {', '.join(missing)}")
    if not isinstance(capture.outcome["assistance"], list):
        raise ValueError("rehearsal assistance must be a list")
    if not isinstance(capture.outcome["errors"], list):
        raise ValueError("rehearsal errors must be a list")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_sha256(value: Any) -> str:
    return _sha256(_canonical_json(value))
