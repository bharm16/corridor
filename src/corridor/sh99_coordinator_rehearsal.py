"""Seal the bounded SH 99 coordinator rehearsal without changing product behavior.

The coordinator exercise is evidence about the already-shipped guided-statement,
Report, and release paths.  It is deliberately not another application service:
the acceptance bundle records the pinned inputs, separate operations and
coordinator timings, the release identity, and any assistance or failure.  A
bundle can therefore prove an unqualified pass only when the measured run had
neither assistance nor an error; it never turns an internal rehearsal into
customer-usability evidence.

The earlier SH 99 Admission replay supplied the shared mechanical-policy
evidence.  This successor treats that database as a read-only predecessor: it
restores a data-only dump into a disposable database at the same revision,
upgrades only the clone to the checkout's exact head, and rechecks the shared
head and state after disposal.  Direct database repair, raw technical
identifiers, and test-only screens remain disqualifying in the timed journey.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import hashlib
from html import unescape
from html.parser import HTMLParser
import json
import math
import os
import platform
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from time import monotonic
from typing import Any

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from corridor.m8_acceptance_bundle import VerificationResult, publish_verified_bundle, verify_bundle
from corridor.m8_acceptance_database import (
    DatabaseProvisioner,
    provision_disposable_postgres,
    read_migration_head,
    upgrade_provisioned_postgres,
)
from corridor.dependency_events import published_party_statements
from corridor.models import (
    Candidate,
    CommitmentLineage,
    DependencyEvent,
    DependencyEventScope,
    DependencyEventTiming,
    DocPage,
    Document,
    EventAdmissionOutcome,
    ExternalReportRelease,
    Project,
    ProjectRosterEntry,
    StatementCoordinationReceipt,
)
from corridor.exceptions import RULESET_VERSION, Thresholds
from corridor.principals import HumanPrincipal
from corridor.report_release import retrieve_released_external_report
from corridor.sh99_admission_acceptance import (
    read_rehearsal_project_state,
)
from corridor.rehearsal_environment import SealedRehearsalEnvironment
from corridor.web.app import app, get_human_principal, get_session
from corridor.work_list import build_work_list


LEGACY_BUNDLE_SCHEMA_VERSION = "corridor.sh99-coordinator-rehearsal-bundle.v2"
BUNDLE_SCHEMA_VERSION = "corridor.sh99-coordinator-rehearsal-bundle.v3"
BUNDLE_FILES = (
    "receipt.json",
    "canonical-content.json",
    "environment.json",
    "released-report.pdf",
)
CLAIM_BOUNDARY = {
    "internal_workflow_rehearsal": True,
    "customer_usability_validation": False,
    "provisional_targets": True,
}
DATABASE_PREFIX = "corridor_sh99_coordinator_rehearsal_"

# Bundle v3 is the immutable #265 replay contract.  Keep these values frozen
# here instead of consulting future exception-engine defaults while verifying
# an already sealed bundle.  A new replay whose ordinary Report policy changes
# must publish a successor schema rather than reinterpret v3 evidence.
_V3_EVALUATION_THRESHOLD_ITEMS = (
    ("stale_days", 14),
    ("due_soon_days", 30),
    ("action_due_soon_days", 7),
)
_KINDER_MORGAN_QUOTE = (
    "The March 2026 completion timeline seems unattainable. "
    "Propose extending to May 16th."
)
_KINDER_MORGAN_SOURCE_QUOTE = (
    "The March 2026 completion timeline seems unattainable. Propose extending to \n"
    "May 16th."
)
_EQUISTAR_QUOTE = (
    "Equistar to provide a chain of title on the ROW agreement that is in "
    "DOW’s name (Due date of 01/2025)."
)
_AIR_PRODUCTS_INVITATION_QUOTE = (
    "Air Products will be invited to the TxDOT-Utility Owners-DB Proposers "
    "Workshop on May 8th, 2025. At this event, the DB Contractor PUAA process "
    "will be explained."
)
_SCENARIO_SOURCES = {
    7296: {
        "document_name": (
            "Meeting Notes/Kinder Morgan/2025.01.16 GPB1 Kinder Morgan notes final.pdf"
        ),
        "document_date": "2025-01-16",
        "page": 2,
        "quote": _KINDER_MORGAN_SOURCE_QUOTE,
    },
    7129: {
        "document_name": (
            "Meeting Notes/Equistar/2024.12.04 GPB1 Equistar notes final.pdf"
        ),
        "document_date": "2024-12-04",
        "page": 2,
        "quote": _EQUISTAR_QUOTE,
    },
    7587: {
        "document_name": (
            "Meeting Notes/Air Products/2025.04.14 GPB1 Air Products notes final.pdf"
        ),
        "document_date": "2025-04-14",
        "page": 1,
        "quote": _AIR_PRODUCTS_INVITATION_QUOTE,
    },
}
_VISIBLE_EVIDENCE_HEADER = re.compile(
    r"^(?P<filename>.+?)\s+·\s+registered page\s+(?P<page>\d+)$"
)
_STATEMENT_COORDINATE_ROUTE = re.compile(
    r"^/statements/[^/?#]+/[^/?#]+/coordinate$"
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
    released_pdf_bytes: bytes | None = None


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
    expected_source_migration_head: str
    expected_target_migration_head: str
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


@dataclass(frozen=True)
class _CloneVerification:
    """Read-only post-journey facts, with an optional fixed release artifact."""

    facts: dict[str, Any]
    released_pdf_bytes: bytes | None


@dataclass(frozen=True)
class _DurableRelease:
    """Release bytes and receipt captured before broader semantic checks."""

    row: ExternalReportRelease
    facts: dict[str, Any]
    pdf_bytes: bytes


def publish_coordinator_rehearsal_bundle(
    output_dir: Path,
    capture: CoordinatorRehearsalCapture,
) -> CoordinatorRehearsalBundleSummary:
    """Publish one immutable, independently verifiable rehearsal observation."""

    canonical_content = _canonical_content(capture)
    released_pdf_bytes = capture.released_pdf_bytes or b""
    _validate_capture_release(canonical_content, released_pdf_bytes)
    receipt = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        **canonical_content,
    }
    environment = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "rehearsed_at": datetime.now(timezone.utc).isoformat(),
        "source_revision": capture.inputs["source_revision"],
        "source_migration_head": capture.inputs["source_migration_head"],
        "details": capture.inputs["environment_details"],
    }
    manifest_path, manifest_sha256, canonical_sha256 = publish_verified_bundle(
        output_dir,
        exports={
            "receipt.json": receipt,
            "canonical-content.json": canonical_content,
            "environment.json": environment,
            "released-report.pdf": released_pdf_bytes,
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
    verify_coordinator_rehearsal_bundle(
        output_dir,
        expected_integrity_manifest_sha256=manifest_sha256,
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

    schema_version = _manifest_schema_version(bundle_dir)
    verified = verify_bundle(
        bundle_dir,
        expected_integrity_manifest_sha256=expected_integrity_manifest_sha256,
        bundle_schema_version=schema_version,
        bundle_files=BUNDLE_FILES,
        corrupt_bundle_error_cls=CorruptSH99CoordinatorRehearsalBundle,
        sha256=_sha256,
        json_sha256=_json_sha256,
    )
    _verify_retained_release(bundle_dir)
    if schema_version == BUNDLE_SCHEMA_VERSION:
        _verify_v3_semantics(bundle_dir)
    return verified


def _manifest_schema_version(bundle_dir: Path) -> str:
    """Select only the frozen v2 reader or the current v3 reader."""

    manifest_path = Path(bundle_dir) / "manifest.json"
    if manifest_path.is_symlink():
        raise CorruptSH99CoordinatorRehearsalBundle(
            "manifest.json must not be a symlink"
        )
    try:
        manifest = json.loads(manifest_path.read_bytes())
        schema_version = manifest["schema_version"]
    except (KeyError, OSError, json.JSONDecodeError, TypeError) as exc:
        raise CorruptSH99CoordinatorRehearsalBundle(
            "manifest.json is absent or invalid"
        ) from exc
    if schema_version not in {LEGACY_BUNDLE_SCHEMA_VERSION, BUNDLE_SCHEMA_VERSION}:
        raise CorruptSH99CoordinatorRehearsalBundle(
            "manifest.json has an unsupported schema"
        )
    return schema_version


def _verify_retained_release(bundle_dir: Path) -> None:
    """Bind the retained PDF bytes to the sealed release receipt, if any."""

    try:
        canonical = json.loads((bundle_dir / "canonical-content.json").read_bytes())
        released_pdf_bytes = (bundle_dir / "released-report.pdf").read_bytes()
        outcome_release = canonical["outcome"]["released_pdf"]
        release = canonical["verification"].get("release")
    except (KeyError, OSError, json.JSONDecodeError, AttributeError) as exc:
        raise CorruptSH99CoordinatorRehearsalBundle(
            "coordinator release receipt is absent or invalid"
        ) from exc
    if outcome_release is None:
        if released_pdf_bytes:
            raise CorruptSH99CoordinatorRehearsalBundle(
                "unreleased rehearsal retains unexpected PDF bytes"
            )
        if release is not None:
            raise CorruptSH99CoordinatorRehearsalBundle(
                "unreleased rehearsal has a release verification receipt"
            )
        return
    if not isinstance(outcome_release, dict) or not isinstance(release, dict):
        raise CorruptSH99CoordinatorRehearsalBundle("released PDF receipt is invalid")
    digest = _sha256(released_pdf_bytes)
    if not released_pdf_bytes.startswith(b"%PDF-") or digest != outcome_release.get("sha256"):
        raise CorruptSH99CoordinatorRehearsalBundle(
            "retained released PDF does not match its receipt digest"
        )
    required = {
        "release_id",
        "artifact_name",
        "sha256",
        "evaluated_on",
        "ruleset_version",
        "provenance_mode",
        "released_by",
        "released_at",
        "record_context",
        "evaluation_context",
    }
    if not required <= set(release):
        raise CorruptSH99CoordinatorRehearsalBundle("released PDF receipt is incomplete")
    for field in ("release_id", "artifact_name", "sha256"):
        if outcome_release.get(field) != release.get(field):
            raise CorruptSH99CoordinatorRehearsalBundle(
                "released PDF outcome and receipt disagree"
            )
    if digest != release["sha256"]:
        raise CorruptSH99CoordinatorRehearsalBundle(
            "retained released PDF does not match release verification"
        )


def _validate_capture_release(
    canonical_content: dict[str, Any], released_pdf_bytes: bytes
) -> None:
    """Reject an internally inconsistent current-schema release before sealing."""

    outcome_release = canonical_content["outcome"]["released_pdf"]
    release = canonical_content["verification"].get("release")
    if outcome_release is None:
        if released_pdf_bytes:
            raise ValueError("retained PDF bytes require a released PDF receipt")
        if release is not None:
            raise ValueError("an unreleased rehearsal cannot have a release receipt")
        return
    if not released_pdf_bytes:
        raise ValueError("a released PDF receipt requires retained PDF bytes")
    if not isinstance(outcome_release, dict) or not isinstance(release, dict):
        raise ValueError("a released PDF requires a complete verification receipt")
    digest = _sha256(released_pdf_bytes)
    if not released_pdf_bytes.startswith(b"%PDF-") or digest != outcome_release.get(
        "sha256"
    ):
        raise ValueError("retained released PDF does not match its receipt digest")
    required = {
        "release_id",
        "artifact_name",
        "sha256",
        "evaluated_on",
        "ruleset_version",
        "provenance_mode",
        "released_by",
        "released_by_display",
        "released_at",
        "record_context",
        "evaluation_context",
    }
    if not required <= set(release):
        raise ValueError("released PDF verification receipt is incomplete")
    for field in ("release_id", "artifact_name", "sha256"):
        if outcome_release.get(field) != release.get(field):
            raise ValueError("released PDF outcome and verification disagree")


def _verify_v3_semantics(bundle_dir: Path) -> None:
    """Recompute current-schema pass meaning from sealed facts."""

    try:
        canonical = json.loads((Path(bundle_dir) / "canonical-content.json").read_bytes())
        receipt = json.loads((Path(bundle_dir) / "receipt.json").read_bytes())
        environment = json.loads((Path(bundle_dir) / "environment.json").read_bytes())
        outcome = canonical["outcome"]
    except (KeyError, OSError, json.JSONDecodeError, TypeError) as exc:
        raise CorruptSH99CoordinatorRehearsalBundle(
            "v3 coordinator semantics are absent or invalid"
        ) from exc
    try:
        _require_capture_shape(
            CoordinatorRehearsalCapture(
                inputs=canonical["inputs"],
                operations=canonical["operations"],
                coordinator=canonical["coordinator"],
                outcome=canonical["outcome"],
                verification=canonical["verification"],
            )
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CorruptSH99CoordinatorRehearsalBundle(
            "v3 coordinator receipt shape is absent or invalid"
        ) from exc
    if receipt != {"schema_version": BUNDLE_SCHEMA_VERSION, **canonical}:
        raise CorruptSH99CoordinatorRehearsalBundle(
            "v3 receipt does not match canonical content"
        )
    if environment.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        raise CorruptSH99CoordinatorRehearsalBundle(
            "v3 environment has an unsupported schema"
        )
    try:
        expected_pass = _semantic_unqualified_pass(
            operations=canonical["operations"],
            coordinator=canonical["coordinator"],
            outcome=outcome,
            verification=canonical["verification"],
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise CorruptSH99CoordinatorRehearsalBundle(
            "v3 pass evidence is absent or invalid"
        ) from exc
    expected_status = "passed" if expected_pass else "failed"
    if outcome.get("status") != expected_status:
        raise CorruptSH99CoordinatorRehearsalBundle(
            "v3 outcome status does not match its sealed evidence"
        )
    if outcome.get("unqualified_pass") is not expected_pass:
        raise CorruptSH99CoordinatorRehearsalBundle(
            "v3 unqualified-pass claim does not match its sealed evidence"
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
    repo_root = Path(__file__).resolve().parents[2]
    asset_root = Path.cwd().resolve()
    evaluation_thresholds = _v3_evaluation_thresholds()
    if asdict(Thresholds()) != evaluation_thresholds:
        raise RuntimeError(
            "ordinary Report Evaluation defaults no longer match the frozen v3 contract"
        )
    rehearsal = SealedRehearsalEnvironment.open(
        source_database_url=config.source_database_url,
        expected_checkout_revision=config.expected_clean_git_revision,
        repo_root=repo_root,
        compose_root=asset_root,
        expected_database_migration_head=config.expected_source_migration_head,
    )
    source = rehearsal.checkout
    checkout_head = rehearsal.checkout_migration_head
    if checkout_head != config.expected_target_migration_head:
        raise ValueError("checked-out migration head does not match the explicit target")
    _require_direct_migration_successor(
        repo_root,
        source_revision=config.expected_source_migration_head,
        target_revision=config.expected_target_migration_head,
    )
    source_head_before = rehearsal.database_migration_head
    if source_head_before != config.expected_source_migration_head:
        raise ValueError("shared database is not at the explicitly pinned source head")
    shared_admission_receipt = _verified_shared_admission_receipt(config)
    source_state_before = read_rehearsal_project_state(
        config.source_database_url, config.project_slug
    )
    _require_admitted_source_state(source_state_before)
    scenario_source_receipts = _read_scenario_input_receipts(
        config.source_database_url,
        config.project_slug,
        asset_root=asset_root,
    )
    environment_details = _environment_details(
        config.source_database_url, source_head_before
    )
    environment_details["target_migration_head"] = config.expected_target_migration_head

    operations_started = monotonic()
    source_dump_sha256: str | None = None
    database_name = "not-provisioned"
    interactions: list[str] = []
    assistance: list[str] = []
    errors: list[str] = []
    release_identity: dict[str, Any] | None = None
    released_pdf_bytes: bytes | None = None
    verification: dict[str, Any] = {}
    coordinator_elapsed = 0.0
    scenario_timings: dict[str, float] = {}
    page_image_fetches: dict[str, dict[str, Any]] = {}
    clone_upgrade: dict[str, Any] | None = None
    coverage: dict[str, Any] = {
        "argv": [],
        "returncode": 1,
        "output_sha256": _sha256(b"not run"),
        "covered_scope_modes": ["selected", "all_active"],
    }
    with tempfile.TemporaryDirectory(prefix="corridor-sh99-coordinator-source-") as parent:
        dump_path = Path(parent) / "source.dump"
        rehearsal.capture(dump_path)
        source_dump_sha256 = _sha256(dump_path.read_bytes())
        with provision_database(
            config.postgres_admin_url,
            config.expected_source_migration_head,
        ) as database:
            database_name = database.name
            if database.migration_head != config.expected_source_migration_head:
                raise ValueError(
                    "disposable database was not provisioned at the source head"
                )
            rehearsal.restore(dump_path, database.name)
            clone_url = rehearsal.clone_url(
                config.postgres_admin_url, database.name
            )
            clone_state_at_source = read_rehearsal_project_state(
                clone_url, config.project_slug
            )
            if clone_state_at_source != source_state_before:
                raise ValueError(
                    "restored predecessor clone does not match the pinned source state"
                )
            if _read_scenario_input_receipts(
                clone_url,
                config.project_slug,
                asset_root=asset_root,
            ) != scenario_source_receipts:
                raise ValueError(
                    "restored predecessor clone changed a scenario source receipt"
                )
            upgrade_receipt = upgrade_provisioned_postgres(
                database,
                admin_url=config.postgres_admin_url,
                repo_root=repo_root,
                error_cls=ValueError,
                database_prefix=DATABASE_PREFIX,
                expected_current_revision=config.expected_source_migration_head,
                target_revision=config.expected_target_migration_head,
            )
            clone_upgrade = asdict(upgrade_receipt)
            if read_rehearsal_project_state(
                clone_url, config.project_slug
            ) != source_state_before:
                raise ValueError(
                    "disposable clone domain state changed during the schema upgrade"
                )
            if _read_scenario_input_receipts(
                clone_url, config.project_slug, asset_root=asset_root
            ) != scenario_source_receipts:
                raise ValueError(
                    "schema upgrade changed a scenario source receipt"
                )

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
                        released_pdf_bytes = journey["released_pdf_bytes"]
                        page_image_fetches = journey["page_image_fetches"]
                    except _JourneyFailure as exc:
                        interactions.extend(exc.interactions)
                        assistance.extend(exc.assistance)
                        errors.append(str(exc))
                        coordinator_elapsed = exc.elapsed_seconds
                        scenario_timings = exc.scenario_timings
                        release_identity = exc.release_identity
                        released_pdf_bytes = exc.released_pdf_bytes
                        page_image_fetches = exc.page_image_fetches
                    durable_release: _DurableRelease | None = None
                    try:
                        durable_release = _capture_durable_release(
                            session,
                            project_slug=config.project_slug,
                            release_identity=release_identity,
                        )
                        verification["release"] = durable_release.facts
                        release_identity = {
                            field: durable_release.facts[field]
                            for field in ("release_id", "artifact_name", "sha256")
                        }
                        if (
                            released_pdf_bytes is not None
                            and released_pdf_bytes != durable_release.pdf_bytes
                        ):
                            raise ValueError(
                                "ordinary reviewed bytes differ from durable release bytes"
                            )
                        released_pdf_bytes = durable_release.pdf_bytes
                    except (LookupError, RuntimeError, ValueError) as exc:
                        durable_release = None
                        errors.append(f"post-rehearsal verification: {exc}")
                        verification = {
                            **verification,
                            "valid": False,
                            "error": str(exc),
                        }
                    if durable_release is not None:
                        try:
                            clone_verification = _verify_clone_result(
                                session,
                                project_slug=config.project_slug,
                                release=durable_release.row,
                                candidate_7587_source_receipt=(
                                    scenario_source_receipts["7587"]
                                ),
                                expected_released_by=config.coordinator_subject,
                                expected_released_by_display=(
                                    config.coordinator_display_name
                                ),
                            )
                            verification = {
                                **clone_verification.facts,
                                "release": durable_release.facts,
                                "page_image_fetches": _verify_page_image_fetches(
                                    page_image_fetches, scenario_source_receipts
                                ),
                                "valid": True,
                            }
                        except (RuntimeError, ValueError) as exc:
                            errors.append(f"post-rehearsal verification: {exc}")
                            verification = {
                                **verification,
                                "valid": False,
                                "error": str(exc),
                            }
            finally:
                clone_engine.dispose()
            coverage = _run_scope_coverage(clone_url)

    source_head_after = read_migration_head(
        config.source_database_url,
        repo_root=repo_root,
        error_cls=ValueError,
    )
    source_state_after = read_rehearsal_project_state(
        config.source_database_url, config.project_slug
    )
    scenario_source_receipts_after = _read_scenario_input_receipts(
        config.source_database_url,
        config.project_slug,
        asset_root=asset_root,
    )
    source_database_mutated = (
        source_head_after != source_head_before
        or source_state_after != source_state_before
        or scenario_source_receipts_after != scenario_source_receipts
    )
    if source_database_mutated:
        errors.append("shared source head or domain state changed during the replay")
        verification["valid"] = False
    verification["automated_scope_coverage"] = coverage
    if coverage["returncode"] != 0:
        errors.append("selected and all-active scope coverage did not pass")
        verification["valid"] = False
    if source_dump_sha256 is None:
        raise RuntimeError("source dump digest was not captured")
    deviations = _rehearsal_deviations(assistance, errors)
    operations_receipt = {
        "elapsed_seconds": operations_elapsed,
        "backfill_elapsed_seconds": config.shared_backfill_elapsed_seconds,
        "source_database_mutated": source_database_mutated,
        "source_migration_head_before": source_head_before,
        "source_migration_head_after": source_head_after,
        "source_state_sha256_before": _json_sha256(source_state_before),
        "source_state_sha256_after": _json_sha256(source_state_after),
        "source_scenario_receipts_sha256_before": _json_sha256(
            scenario_source_receipts
        ),
        "source_scenario_receipts_sha256_after": _json_sha256(
            scenario_source_receipts_after
        ),
        "clone_upgrade": clone_upgrade,
    }
    coordinator_receipt = {
        "elapsed_seconds": coordinator_elapsed,
        "scenario_timings": scenario_timings,
        "interactions": interactions,
        "retries": [],
    }
    outcome_facts = {
        "assistance": assistance,
        "errors": errors,
        "deviations": deviations,
        "released_pdf": release_identity,
    }
    status = (
        "passed"
        if _semantic_unqualified_pass(
            operations=operations_receipt,
            coordinator=coordinator_receipt,
            outcome=outcome_facts,
            verification=verification,
        )
        else "failed"
    )
    inputs = {
        "source_revision": source["revision"],
        "source_snapshot_sha256": _json_sha256(source_state_before),
        "source_dump_sha256": source_dump_sha256,
        "source_migration_head": source_head_before,
        "target_migration_head": config.expected_target_migration_head,
        "environment_details": environment_details,
        "shared_admission_receipt": shared_admission_receipt,
        "approved_shared_state_receipt": config.approved_shared_state_receipt,
        "corpus_inputs": [
            {
                "document_id": document["id"],
                "filename": document["filename"],
                "sha256": document["sha256"],
            }
            for document in source_state_before["documents"]
        ],
        "active_runs": source_state_before["active_runs"],
        "policy_identities": _policy_identities(source_state_before["policy_runs"]),
        "report_publication": {
            "ruleset_version": RULESET_VERSION,
            "provenance_mode": "all-supported-sources",
            "evaluation_thresholds": evaluation_thresholds,
        },
        "seeded_coordinator": {
            "subject": config.coordinator_subject,
            "display_name": config.coordinator_display_name,
        },
        "scenario_candidates": {"7296": 7296, "7129": 7129, "7587": 7587},
        "scenario_source_receipts": scenario_source_receipts,
    }
    bundle = publish_coordinator_rehearsal_bundle(
        config.output_dir,
        CoordinatorRehearsalCapture(
            inputs=inputs,
            operations=operations_receipt,
            coordinator=coordinator_receipt,
            outcome={
                "status": status,
                **outcome_facts,
            },
            verification=verification,
            released_pdf_bytes=released_pdf_bytes,
        ),
    )
    return SH99CoordinatorRehearsalSummary(
        bundle=bundle,
        database_name=database_name,
        status=status,
    )


def _provision_database(admin_url: str, migration_revision: str):
    return provision_disposable_postgres(
        admin_url,
        repo_root=Path(__file__).resolve().parents[2],
        error_cls=ValueError,
        database_prefix=DATABASE_PREFIX,
        migration_revision=migration_revision,
    )


def _require_direct_migration_successor(
    repo_root: Path, *, source_revision: str, target_revision: str
) -> None:
    """Require an explicit direct edge retained beneath the checkout's sole head."""

    scripts = ScriptDirectory.from_config(AlembicConfig(str(repo_root / "alembic.ini")))
    heads = tuple(scripts.get_heads())
    target = scripts.get_revision(target_revision)
    revisions_to_target = {
        revision.revision
        for revision in scripts.walk_revisions(
            base=target_revision, head=heads[0] if len(heads) == 1 else "heads"
        )
    }
    if len(heads) != 1 or target_revision not in revisions_to_target:
        raise ValueError("expected target migration is not beneath the checkout's sole head")
    if target is None or target.down_revision != source_revision:
        raise ValueError("expected source migration is not the target's direct predecessor")


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


def _read_scenario_input_receipts(
    database_url: str, project_slug: str, *, asset_root: Path
) -> dict[str, dict[str, Any]]:
    """Read and hash the three exact scenario pages without mutating the source."""

    engine = create_engine(database_url, poolclass=NullPool, future=True)
    try:
        with Session(engine) as session:
            session.execute(text("set transaction read only"))
            project = session.scalar(select(Project).where(Project.slug == project_slug))
            if project is None:
                raise ValueError(f"no project with slug {project_slug!r}")
            receipts: dict[str, dict[str, Any]] = {}
            for candidate_id, expected in _SCENARIO_SOURCES.items():
                candidate = session.get(Candidate, candidate_id)
                if candidate is None or candidate.project_id != project.id:
                    raise ValueError(f"scenario Candidate {candidate_id} is absent")
                document = session.get(Document, candidate.source_document_id)
                if document is None or document.project_id != project.id:
                    raise ValueError(
                        f"scenario Candidate {candidate_id} has no project document"
                    )
                page = session.scalar(
                    select(DocPage).where(
                        DocPage.document_id == document.id,
                        DocPage.page_no == expected["page"],
                    )
                )
                if page is None:
                    raise ValueError(
                        f"scenario Candidate {candidate_id} has no registered source page"
                    )
                observed_date = (
                    document.doc_date.isoformat() if document.doc_date else None
                )
                if (
                    document.filename != expected["document_name"]
                    or observed_date != expected["document_date"]
                ):
                    raise ValueError(
                        f"scenario Candidate {candidate_id} document context changed"
                    )
                receipts[str(candidate_id)] = _scenario_input_receipt(
                    candidate,
                    document,
                    page,
                    asset_root=asset_root,
                    expected_quote=str(expected["quote"]),
                )
            session.rollback()
            return receipts
    finally:
        engine.dispose()


def _scenario_input_receipt(
    candidate,
    document,
    page,
    *,
    asset_root: Path,
    expected_quote: str,
) -> dict[str, Any]:
    """Seal one exact cited DocPage and its ordinary page-image asset."""

    citations = (candidate.payload_json or {}).get("citations") or ()
    matching = [
        citation
        for citation in citations
        if citation.get("document_id") == document.id
        and citation.get("page") == page.page_no
        and citation.get("quote") == expected_quote
        and citation.get("verified") is True
    ]
    if len(matching) != 1 or candidate.source_document_id != document.id:
        raise ValueError(
            f"scenario Candidate {candidate.id} does not have one exact verified citation"
        )
    if page.document_id != document.id or _normalized_visible_text(
        expected_quote
    ) not in _normalized_visible_text(page.text):
        raise ValueError(
            f"scenario Candidate {candidate.id} quote is absent from its registered page"
        )
    if not page.image_path:
        raise ValueError(f"scenario Candidate {candidate.id} has no registered page image")
    root = Path(asset_root).resolve()
    image_path = Path(page.image_path)
    if not image_path.is_absolute():
        image_path = root / image_path
    try:
        image_path = image_path.resolve()
        image_path.relative_to(root)
        image_bytes = image_path.read_bytes()
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"scenario Candidate {candidate.id} page image is absent or outside the runtime asset root"
        ) from exc
    if not image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError(
            f"scenario Candidate {candidate.id} page image is not a PNG"
        )
    document_date = getattr(document, "doc_date", None)
    return {
        "candidate_id": candidate.id,
        "document_id": document.id,
        "document_name": document.filename,
        "document_date": (
            document_date.isoformat()
            if hasattr(document_date, "isoformat")
            else str(document_date or "")
        ),
        "document_sha256": document.sha256,
        "page": page.page_no,
        "quote": expected_quote,
        "text_source": page.text_source,
        "page_text_sha256": _sha256(page.text.encode()),
        "page_image": {
            "url": f"/page-image/{document.id}/{page.page_no}",
            "bytes": len(image_bytes),
            "sha256": _sha256(image_bytes),
        },
    }


def _environment_details(database_url: str, migration_head: str) -> dict[str, str]:
    """Capture enough runtime context to interpret a replay without reopening it."""

    engine = create_engine(database_url, poolclass=NullPool, future=True)
    try:
        with engine.connect() as connection:
            database_version = str(connection.scalar(text("SHOW server_version")))
    finally:
        engine.dispose()
    return {
        "database_server_version": database_version,
        "migration_head": migration_head,
        "platform": platform.platform(),
        "python_version": sys.version.split()[0],
        "web_interface": "fastapi-testclient",
    }


def _policy_identities(policy_runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pin policy authority fields, not incidental admission-run counters."""

    return [
        {
            "run_id": policy_run["id"],
            "family": policy_run["family"],
            "approval_id": policy_run["policy_approval_id"],
            "policy_version": policy_run["policy_version"],
            "policy_sha256": policy_run["policy_sha256"],
            "abstention_reason_version": policy_run["abstention_reason_version"],
        }
        for policy_run in policy_runs
    ]


def _rehearsal_deviations(assistance: list[str], errors: list[str]) -> list[dict[str, str]]:
    """Distinguish observed departures from a clean run from the pass/fail result."""

    deviations = [
        {"kind": "assistance", "detail": detail}
        for detail in assistance
    ]
    deviations.extend({"kind": "error", "detail": detail} for detail in errors)
    return deviations


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

    def __init__(self, message: str, progress: "_JourneyProgress") -> None:
        super().__init__(message)
        self.interactions = progress.interactions
        self.assistance = progress.assistance
        self.elapsed_seconds = monotonic() - progress.started
        self.scenario_timings = progress.scenario_timings
        self.page_image_fetches = progress.page_image_fetches
        self.release_identity = progress.release_identity
        self.released_pdf_bytes = progress.released_pdf_bytes


@dataclass
class _JourneyProgress:
    """The mutable, coordinator-timed state carried through ordinary UI actions."""

    interactions: list[str]
    assistance: list[str]
    started: float
    scenario_timings: dict[str, float]
    page_image_fetches: dict[str, dict[str, Any]]
    release_identity: dict[str, Any] | None
    released_pdf_bytes: bytes | None

    @classmethod
    def start(cls) -> "_JourneyProgress":
        return cls(
            interactions=[],
            assistance=[],
            started=monotonic(),
            scenario_timings={},
            page_image_fetches={},
            release_identity=None,
            released_pdf_bytes=None,
        )

    def failure(self, message: str) -> _JourneyFailure:
        return _JourneyFailure(message, self)


def _run_ordinary_interface_journey(
    session: Session, config: SH99CoordinatorRehearsalConfig
) -> dict[str, Any]:
    """Exercise existing HTTP controls without querying during coordinator time."""

    progress = _JourneyProgress.start()
    sentinel = object()
    old_session = app.dependency_overrides.get(get_session, sentinel)
    old_principal = app.dependency_overrides.get(get_human_principal, sentinel)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(
        config.coordinator_subject
    )
    try:
        with TestClient(app) as client:
            _record_interaction(progress.interactions, "coordinator_home")
            home = client.get(f"/work/{config.project_slug}")
            _require_response(
                home.status_code,
                200,
                "coordinator home did not load",
                progress,
            )
            km_started = monotonic()
            km_url, km_screen = _find_statement_screen(
                client,
                home.text,
                party="Kinder Morgan",
                exact_quote=_KINDER_MORGAN_QUOTE,
                source_context=(
                    str(_SCENARIO_SOURCES[7296]["document_name"]),
                    str(_SCENARIO_SOURCES[7296]["document_date"]),
                ),
                progress=progress,
            )
            _record_interaction(progress.interactions, "save_kinder_morgan_statement")
            supporting_evidence = _supporting_evidence_selection(
                km_screen,
                exact_quote="Kinder Morgan Management Meeting Highlights",
            )
            progress.page_image_fetches["7296"] = _fetch_evidence_page_image(
                client,
                supporting_evidence,
                progress=progress,
            )
            saved_km = client.post(
                km_url,
                data=_kinder_morgan_form(
                    km_screen,
                    config.coordinator_display_name,
                    supporting=supporting_evidence,
                ),
                follow_redirects=False,
            )
            _require_response(
                saved_km.status_code,
                303,
                "Kinder Morgan guided Save did not complete",
                progress,
            )
            progress.scenario_timings["7296"] = monotonic() - km_started

            _record_interaction(progress.interactions, "coordinator_home_after_kinder_morgan")
            refreshed_home = client.get(f"/work/{config.project_slug}")
            _require_response(
                refreshed_home.status_code,
                200,
                "coordinator home did not refresh",
                progress,
            )
            equistar_started = monotonic()
            equistar_url, equistar_screen = _find_statement_screen(
                client,
                refreshed_home.text,
                party="Equistar",
                exact_quote=_EQUISTAR_QUOTE,
                source_context=(
                    str(_SCENARIO_SOURCES[7129]["document_name"]),
                    str(_SCENARIO_SOURCES[7129]["document_date"]),
                ),
                progress=progress,
            )
            equistar_evidence = _supporting_evidence_selection(
                equistar_screen,
                exact_quote=_EQUISTAR_QUOTE,
            )
            progress.page_image_fetches["7129"] = _fetch_evidence_page_image(
                client,
                equistar_evidence,
                progress=progress,
            )
            _record_interaction(progress.interactions, "save_equistar_statement")
            saved_equistar = client.post(
                equistar_url,
                data=_equistar_form(
                    equistar_screen,
                    config.coordinator_display_name,
                    supporting=equistar_evidence,
                ),
                follow_redirects=False,
            )
            _require_response(
                saved_equistar.status_code,
                303,
                "Equistar guided Save did not complete",
                progress,
            )

            report_url = _visible_link_url(
                refreshed_home.text,
                text_value="Prepare External Report",
            )
            _record_interaction(progress.interactions, "open_external_report_release")
            report_workspace = client.get(report_url)
            _require_response(
                report_workspace.status_code,
                200,
                "External Report release screen did not load",
                progress,
            )
            render_form = _visible_form(
                report_workspace.text,
                button_text="Render fixed PDF for review",
            )
            if render_form.method != "post" or render_form.fields.get("ordinary") != "1":
                raise _journey_failure(
                    "External Report screen does not expose the ordinary render control",
                    progress,
                )
            _record_interaction(progress.interactions, "render_fixed_pdf_for_review")
            rendered = client.post(render_form.action, data=render_form.fields)
            _require_response(
                rendered.status_code,
                201,
                "ordinary fixed PDF render did not complete",
                progress,
            )
            review = _report_review_controls(rendered.text)
            if "artifact_id" in review.release_form.fields:
                raise _journey_failure(
                    "Report release form exposes a coordinator-facing artifact identity",
                    progress,
                )
            _record_interaction(progress.interactions, "review_fixed_pdf_preview")
            preview_bytes = _fetch_pdf(
                client,
                review.preview_url,
                message="fixed PDF preview did not load",
                progress=progress,
            )
            _record_interaction(progress.interactions, "download_fixed_pdf_for_review")
            downloaded_bytes = _fetch_pdf(
                client,
                review.download_url,
                message="fixed PDF review download did not load",
                progress=progress,
            )
            if (
                preview_bytes != downloaded_bytes
                or _sha256(downloaded_bytes) != review.sha256
            ):
                raise _journey_failure(
                    "fixed PDF preview, download, and displayed SHA-256 do not agree",
                    progress,
                )
            _record_interaction(progress.interactions, "release_fixed_pdf")
            released = client.post(
                review.release_form.action,
                data=review.release_form.fields,
            )
            _require_response(
                released.status_code,
                201,
                "fixed PDF release did not complete",
                progress,
            )
            # A 201 means the immutable release now exists.  Preserve the exact
            # reviewed bytes and browser-visible identity before parsing the
            # returned history so any later UI/verification failure can still
            # seal honest evidence of what was released.
            progress.release_identity = {
                "artifact_name": review.artifact_name,
                "sha256": review.sha256,
            }
            progress.released_pdf_bytes = downloaded_bytes
            history_entry = _matching_release_history(
                released.text,
                artifact_name=review.artifact_name,
                sha256=review.sha256,
                released_by_display=config.coordinator_display_name,
                hidden_principal=config.coordinator_subject,
            )
            release_id = _release_id_from_download(history_entry.download_url)
            progress.release_identity = {
                "release_id": release_id,
                "artifact_name": review.artifact_name,
                "sha256": review.sha256,
            }
            _record_interaction(progress.interactions, "retrieve_released_pdf")
            released_bytes = _fetch_pdf(
                client,
                history_entry.download_url,
                message="released PDF did not remain retrievable",
                progress=progress,
            )
            if released_bytes != downloaded_bytes:
                raise _journey_failure(
                    "released PDF bytes differ from the reviewed fixed PDF",
                    progress,
                )
            progress.released_pdf_bytes = released_bytes
            _record_interaction(progress.interactions, "reload_release_history")
            reloaded_history = client.get(report_url)
            _require_response(
                reloaded_history.status_code,
                200,
                "immutable release history did not reload",
                progress,
            )
            reloaded_entry = _matching_release_history(
                reloaded_history.text,
                artifact_name=review.artifact_name,
                sha256=review.sha256,
                released_by_display=config.coordinator_display_name,
                hidden_principal=config.coordinator_subject,
            )
            if reloaded_entry.download_url != history_entry.download_url:
                raise _journey_failure(
                    "immutable release history changed the released PDF control",
                    progress,
                )
            progress.scenario_timings["7129_and_release"] = monotonic() - equistar_started
            return {
                "elapsed_seconds": monotonic() - progress.started,
                "scenario_timings": progress.scenario_timings,
                "interactions": progress.interactions,
                "assistance": progress.assistance,
                "page_image_fetches": progress.page_image_fetches,
                "released_pdf": progress.release_identity,
                "released_pdf_bytes": progress.released_pdf_bytes,
            }
    except ValueError as exc:
        raise _journey_failure(
            str(exc),
            progress,
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
    *,
    party: str,
    exact_quote: str,
    source_context: tuple[str, ...],
    progress: _JourneyProgress,
) -> tuple[str, str]:
    cards = _ImmediateWorkCardParser(home_html).cards
    matches = [
        card
        for card in cards
        if _normalized_visible_text(card.quote)
        == _normalized_visible_text(exact_quote)
        and _normalized_visible_text(party) in _normalized_visible_text(card.text)
        and all(
            _normalized_visible_text(value)
            in _normalized_visible_text(card.source_context)
            for value in source_context
        )
    ]
    if len(matches) != 1:
        raise _journey_failure(
            "coordinator home does not expose exactly one immediate statement card "
            "matching the visible party, exact wording, and source context",
            progress,
        )
    review_actions = _statement_review_actions(matches[0])
    if len(review_actions) != 1:
        raise _journey_failure(
            "matched statement card does not expose exactly one visible statement "
            "review action with its ordinary coordinate route",
            progress,
        )
    url = review_actions[0]
    _record_interaction(progress.interactions, "open_statement_from_work_list")
    screen = client.get(url)
    _require_response(
        screen.status_code,
        200,
        "statement screen did not load",
        progress,
    )
    return url, screen.text


@dataclass(frozen=True)
class _VisibleWorkCard:
    """One immediate card as rendered to the coordinator."""

    actions: tuple[tuple[str, str], ...]
    text: str
    quote: str
    source_context: str


def _statement_review_actions(card: _VisibleWorkCard) -> list[str]:
    """Select the paired visible purpose and opaque ordinary route."""

    matches: list[str] = []
    for href, visible_text in card.actions:
        purpose_matches = (
            _normalized_visible_text(visible_text) == "review extracted statement"
        )
        route_matches = _STATEMENT_COORDINATE_ROUTE.fullmatch(href) is not None
        if purpose_matches != route_matches:
            return []
        if purpose_matches:
            matches.append(href)
    return matches


class _ImmediateWorkCardParser(HTMLParser):
    """Read only immediate cards, excluding both searchable backlog sections."""

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: list[_VisibleWorkCard] = []
        self._stack: list[tuple[str, frozenset[str]]] = []
        self._backlog_depth = 0
        self._card_depth: int | None = None
        self._text: list[str] = []
        self._quote: list[str] = []
        self._source: list[str] = []
        self._actions: list[tuple[str, str]] = []
        self._action_depth: int | None = None
        self._action_href = ""
        self._action_text: list[str] = []
        self._in_quote = 0
        self._in_source = 0
        self.feed(source)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = frozenset((values.get("class") or "").split())
        self._stack.append((tag, classes))
        if tag == "section" and "backlog" in classes:
            self._backlog_depth += 1
        if (
            tag == "section"
            and "item" in classes
            and self._backlog_depth == 0
            and self._card_depth is None
        ):
            self._card_depth = len(self._stack)
            self._text = []
            self._quote = []
            self._source = []
            self._actions = []
        if self._card_depth is None:
            return
        if tag == "blockquote":
            self._in_quote += 1
        if tag == "p" and "source" in classes:
            self._in_source += 1
        if (
            tag == "a"
            and "action" in classes
            and values.get("href")
            and self._action_depth is None
        ):
            self._action_depth = len(self._stack)
            self._action_href = unescape(values["href"])
            self._action_text = []

    def handle_data(self, data: str) -> None:
        if self._card_depth is None:
            return
        self._text.append(data)
        if self._in_quote:
            self._quote.append(data)
        if self._in_source:
            self._source.append(data)
        if self._action_depth is not None:
            self._action_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._stack:
            return
        open_tag, classes = self._stack[-1]
        if self._card_depth is not None:
            if tag == "blockquote" and self._in_quote:
                self._in_quote -= 1
            if tag == "p" and "source" in classes and self._in_source:
                self._in_source -= 1
            if tag == "a" and self._action_depth == len(self._stack):
                self._actions.append(
                    (self._action_href, " ".join(self._action_text))
                )
                self._action_depth = None
                self._action_href = ""
                self._action_text = []
            if (
                tag == "section"
                and open_tag == "section"
                and len(self._stack) == self._card_depth
            ):
                self.cards.append(
                    _VisibleWorkCard(
                        actions=tuple(self._actions),
                        text=" ".join(self._text),
                        quote=" ".join(self._quote),
                        source_context=" ".join(self._source),
                    )
                )
                self._card_depth = None
                self._in_quote = 0
                self._in_source = 0
                self._action_depth = None
        if tag == "section" and "backlog" in classes and self._backlog_depth:
            self._backlog_depth -= 1
        self._stack.pop()


def _normalized_visible_text(value: str) -> str:
    """Compare rendered wording without making whitespace an identifier."""

    return " ".join(unescape(value).split()).casefold()


def _kinder_morgan_form(
    screen_html: str,
    coordinator_display_name: str,
    *,
    supporting: _VisibleEvidence | None = None,
) -> dict[str, str]:
    fields = _ScreenFields(screen_html)
    if supporting is None:
        supporting = _supporting_evidence_selection(
            screen_html,
            exact_quote="Kinder Morgan Management Meeting Highlights",
        )
    return {
        **fields.hidden,
        "affected_external_org_id": fields.external_party_option_value(
            "affected_external_org_id", "Kinder Morgan"
        ),
        "stated_party": "Kinder Morgan",
        "stated_external_org_id": fields.external_party_option_value(
            "stated_external_org_id", "Kinder Morgan"
        ),
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
        "supporting_page_index": supporting.page_index,
        "supporting_quote": "Kinder Morgan Management Meeting Highlights",
        "scope_mode": "unknown",
        "internal_owner_roster_entry_id": fields.option_value(
            "internal_owner_roster_entry_id", coordinator_display_name
        ),
        "next_action": "Confirm the revised completion plan with Kinder Morgan",
        "action_due_date_unknown_reason": "awaiting_schedule_information",
        "milestone_impact": "not_yet_known",
    }


def _equistar_form(
    screen_html: str,
    coordinator_display_name: str,
    *,
    supporting: _VisibleEvidence | None = None,
) -> dict[str, str]:
    fields = _ScreenFields(screen_html)
    if supporting is None:
        supporting = _supporting_evidence_selection(
            screen_html,
            exact_quote=_EQUISTAR_QUOTE,
        )
    return {
        **fields.hidden,
        "affected_external_org_id": fields.external_party_option_value(
            "affected_external_org_id", "Equistar"
        ),
        "stated_party": "Equistar",
        "stated_external_org_id": fields.external_party_option_value(
            "stated_external_org_id", "Equistar"
        ),
        "event_date": "2024-12-04",
        "description": (
            "Equistar committed to provide a chain of title for the ROW agreement "
            "in DOW’s name, due in January 2025."
        ),
        "new_timing_text": "01/2025",
        "new_timing_precision": "month",
        "new_timing_start_date": "2025-01-01",
        "new_timing_end_date": "2025-01-31",
        "supporting_page_index": supporting.page_index,
        "supporting_quote": _EQUISTAR_QUOTE,
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

    def external_party_option_value(self, field: str, suggestion: str) -> str:
        """Resolve one visible External Party without interpreting its opaque value."""

        normalized_suggestion = _normalized_visible_text(suggestion)
        options = [
            (value, _normalized_visible_text(text))
            for value, text in self.options.get(field, [])
            if value
        ]
        exact = [
            value
            for value, normalized_text in options
            if normalized_text == normalized_suggestion
        ]
        if len(exact) == 1:
            return exact[0]
        if not exact and normalized_suggestion:
            prefix = f"{normalized_suggestion} "
            expanded = [
                value
                for value, normalized_text in options
                if normalized_text.startswith(prefix)
            ]
            if len(expanded) == 1:
                return expanded[0]
        raise ValueError(
            "coordinator screen does not offer one unambiguous visible External "
            f"Party for {suggestion!r} in {field}"
        )


@dataclass(frozen=True)
class _VisibleEvidence:
    """One registered page block and its opaque browser selection value."""

    page_index: str
    filename: str
    page_no: int
    text: str
    image_url: str | None


@dataclass
class _EvidenceBlock:
    header: str
    text: str
    image_urls: list[str]


class _VisibleEvidenceParser(HTMLParser):
    """Pair rendered Evidence blocks with the radio controls shown below them."""

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[_EvidenceBlock] = []
        self.radios: dict[str, bool] = {}
        self._section_depth = 0
        self._block_depth: int | None = None
        self._block_text: list[str] = []
        self._header_text: list[str] = []
        self._image_urls: list[str] = []
        self._in_header = 0
        self.feed(source)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = frozenset((values.get("class") or "").split())
        if tag == "section":
            self._section_depth += 1
            if "evidence" in classes and self._block_depth is None:
                self._block_depth = self._section_depth
                self._block_text = []
                self._header_text = []
                self._image_urls = []
        if self._block_depth is not None:
            if tag == "span" and "muted" in classes:
                self._in_header += 1
            if tag == "img" and values.get("src"):
                self._image_urls.append(unescape(values["src"]))
        if tag == "input" and values.get("name") == "supporting_page_index":
            value = values.get("value") or ""
            if value:
                self.radios[value] = "disabled" not in values

    def handle_data(self, data: str) -> None:
        if self._block_depth is None:
            return
        self._block_text.append(data)
        if self._in_header:
            self._header_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._block_depth is not None and tag == "span" and self._in_header:
            self._in_header -= 1
        if tag != "section":
            return
        if self._block_depth == self._section_depth:
            self.blocks.append(
                _EvidenceBlock(
                    header=" ".join(self._header_text),
                    text=" ".join(self._block_text),
                    image_urls=list(self._image_urls),
                )
            )
            self._block_depth = None
            self._in_header = 0
        self._section_depth -= 1


def _supporting_evidence_selection(
    screen_html: str, *, exact_quote: str
) -> _VisibleEvidence:
    """Select one enabled Evidence option by its visible registered-page context."""

    parsed = _VisibleEvidenceParser(screen_html)
    matches: list[_VisibleEvidence] = []
    for ordinal, block in enumerate(parsed.blocks):
        page_index = str(ordinal)
        if _normalized_visible_text(exact_quote) not in _normalized_visible_text(
            block.text
        ):
            continue
        header = _VISIBLE_EVIDENCE_HEADER.fullmatch(" ".join(block.header.split()))
        if header is None:
            raise ValueError("supporting Evidence does not display its document and page")
        if parsed.radios.get(page_index) is not True:
            raise ValueError("matching supporting Evidence is not enabled for selection")
        if len(block.image_urls) != 1:
            raise ValueError("matching supporting Evidence does not expose one page image")
        matches.append(
            _VisibleEvidence(
                page_index=page_index,
                filename=header.group("filename"),
                page_no=int(header.group("page")),
                text=" ".join(block.text.split()),
                image_url=block.image_urls[0],
            )
        )
    if len(matches) != 1:
        raise ValueError(
            "coordinator screen does not expose exactly one enabled supporting Evidence block"
        )
    return matches[0]


def _fetch_evidence_page_image(
    client: TestClient,
    evidence: _VisibleEvidence,
    *,
    progress: _JourneyProgress,
) -> dict[str, Any]:
    """Fetch the ordinary rendered-page URL and bind its PNG bytes."""

    if not evidence.image_url:
        raise _journey_failure(
            "selected supporting Evidence does not expose a rendered page image",
            progress,
        )
    _record_interaction(progress.interactions, "review_supporting_page_image")
    response = client.get(evidence.image_url)
    _require_response(
        response.status_code,
        200,
        "supporting Evidence page image did not load",
        progress,
    )
    content_type = str(response.headers.get("content-type") or "").split(";", 1)[0]
    image_bytes = bytes(response.content)
    if content_type.casefold() != "image/png" or not image_bytes.startswith(
        b"\x89PNG\r\n\x1a\n"
    ):
        raise _journey_failure(
            "supporting Evidence page image is not a nonempty PNG",
            progress,
        )
    return {
        "url": evidence.image_url,
        "document_name": evidence.filename,
        "registered_page": evidence.page_no,
        "sha256": _sha256(image_bytes),
        "bytes": len(image_bytes),
    }


def _verify_page_image_fetches(
    observed: dict[str, dict[str, Any]],
    source_receipts: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Bind ordinary image responses to the pre-timed registered-page receipts."""

    verified: dict[str, dict[str, Any]] = {}
    for candidate_id in ("7296", "7129"):
        fetch = observed.get(candidate_id)
        source = source_receipts.get(candidate_id)
        if not isinstance(fetch, dict) or not isinstance(source, dict):
            raise ValueError(
                f"Candidate {candidate_id} lacks a supporting page-image receipt"
            )
        expected_image = source.get("page_image")
        if not isinstance(expected_image, dict):
            raise ValueError(
                f"Candidate {candidate_id} source image receipt is incomplete"
            )
        expected = {
            "url": expected_image.get("url"),
            "document_name": source.get("document_name"),
            "registered_page": source.get("page"),
            "sha256": expected_image.get("sha256"),
            "bytes": expected_image.get("bytes"),
        }
        if fetch != expected:
            raise ValueError(
                f"Candidate {candidate_id} ordinary page image differs from its pinned asset"
            )
        verified[candidate_id] = dict(fetch)
    return verified


@dataclass(frozen=True)
class _VisibleLink:
    href: str
    text: str


@dataclass(frozen=True)
class _VisibleForm:
    action: str
    method: str
    fields: dict[str, str]
    text: str


class _VisibleControlParser(HTMLParser):
    """Read ordinary links/forms and fixed-artifact metadata from one page."""

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[_VisibleLink] = []
        self.forms: list[_VisibleForm] = []
        self.headings: list[str] = []
        self.codes: list[str] = []
        self.pdf_objects: list[str] = []
        self._link_href: str | None = None
        self._link_text: list[str] = []
        self._form: dict[str, Any] | None = None
        self._capture: tuple[str, list[str]] | None = None
        self.feed(source)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "a" and values.get("href"):
            self._link_href = unescape(values["href"])
            self._link_text = []
        elif tag == "form":
            if self._form is not None:
                raise ValueError("ordinary coordinator page nests form controls")
            self._form = {
                "action": unescape(values.get("action") or ""),
                "method": (values.get("method") or "get").casefold(),
                "fields": {},
                "text": [],
            }
        elif tag == "input" and self._form is not None:
            name = values.get("name")
            if name and (values.get("type") or "text").casefold() == "hidden":
                self._form["fields"][name] = values.get("value") or ""
        elif tag in {"h2", "code"}:
            self._capture = (tag, [])
        elif (
            tag == "object"
            and values.get("type") == "application/pdf"
            and values.get("data")
        ):
            self.pdf_objects.append(unescape(values["data"]))

    def handle_data(self, data: str) -> None:
        if self._link_href is not None:
            self._link_text.append(data)
        if self._form is not None:
            self._form["text"].append(data)
        if self._capture is not None:
            self._capture[1].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._link_href is not None:
            self.links.append(
                _VisibleLink(self._link_href, " ".join(self._link_text))
            )
            self._link_href = None
            self._link_text = []
        elif tag == "form" and self._form is not None:
            self.forms.append(
                _VisibleForm(
                    action=self._form["action"],
                    method=self._form["method"],
                    fields=dict(self._form["fields"]),
                    text=" ".join(self._form["text"]),
                )
            )
            self._form = None
        if self._capture is not None and tag == self._capture[0]:
            text_value = " ".join(self._capture[1]).strip()
            if tag == "h2":
                self.headings.append(text_value)
            else:
                self.codes.append(text_value)
            self._capture = None


def _visible_link_url(source: str, *, text_value: str) -> str:
    controls = _VisibleControlParser(source)
    matches = [
        link.href
        for link in controls.links
        if _normalized_visible_text(link.text)
        == _normalized_visible_text(text_value)
    ]
    if len(matches) != 1:
        raise ValueError(f"page does not expose exactly one {text_value!r} link")
    return matches[0]


def _visible_form(source: str, *, button_text: str) -> _VisibleForm:
    controls = _VisibleControlParser(source)
    matches = [
        form
        for form in controls.forms
        if _normalized_visible_text(button_text)
        in _normalized_visible_text(form.text)
    ]
    if len(matches) != 1:
        raise ValueError(f"page does not expose exactly one {button_text!r} form")
    form = matches[0]
    if not form.action:
        raise ValueError(f"{button_text!r} form has no browser-carried action")
    return form


@dataclass(frozen=True)
class _ReportReviewControls:
    artifact_name: str
    sha256: str
    preview_url: str
    download_url: str
    release_form: _VisibleForm


def _report_review_controls(source: str) -> _ReportReviewControls:
    controls = _VisibleControlParser(source)
    release_form = _visible_form(source, button_text="Release this exact PDF")
    downloads = [
        link.href
        for link in controls.links
        if _normalized_visible_text(link.text)
        == _normalized_visible_text("Download this exact PDF")
    ]
    if (
        len(controls.headings) != 1
        or len(controls.codes) != 1
        or len(controls.pdf_objects) != 1
        or len(downloads) != 1
        or re.fullmatch(r"[0-9a-f]{64}", controls.codes[0]) is None
    ):
        raise ValueError("fixed PDF review does not expose one complete artifact")
    return _ReportReviewControls(
        artifact_name=controls.headings[0],
        sha256=controls.codes[0],
        preview_url=controls.pdf_objects[0],
        download_url=downloads[0],
        release_form=release_form,
    )


@dataclass(frozen=True)
class _VisibleReleaseHistoryEntry:
    artifact_name: str
    sha256: str
    download_url: str
    text: str


class _ReleaseHistoryParser(HTMLParser):
    """Read immutable history cards without assuming newest or only entry."""

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.entries: list[_VisibleReleaseHistoryEntry] = []
        self._section_depth = 0
        self._entry_depth: int | None = None
        self._text: list[str] = []
        self._name: list[str] = []
        self._digest: list[str] = []
        self._download_links: list[str] = []
        self._in_name = 0
        self._in_digest = 0
        self.feed(source)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = frozenset((values.get("class") or "").split())
        if tag == "section":
            self._section_depth += 1
            if "release" in classes and self._entry_depth is None:
                self._entry_depth = self._section_depth
                self._text = []
                self._name = []
                self._digest = []
                self._download_links = []
        if self._entry_depth is None:
            return
        if tag == "h3":
            self._in_name += 1
        elif tag == "code":
            self._in_digest += 1
        elif tag == "a" and values.get("href"):
            self._download_links.append(unescape(values["href"]))

    def handle_data(self, data: str) -> None:
        if self._entry_depth is None:
            return
        self._text.append(data)
        if self._in_name:
            self._name.append(data)
        if self._in_digest:
            self._digest.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._entry_depth is not None:
            if tag == "h3" and self._in_name:
                self._in_name -= 1
            elif tag == "code" and self._in_digest:
                self._in_digest -= 1
        if tag != "section":
            return
        if self._entry_depth == self._section_depth:
            if len(self._download_links) == 1:
                self.entries.append(
                    _VisibleReleaseHistoryEntry(
                        artifact_name=" ".join(self._name).strip(),
                        sha256=" ".join(self._digest).strip(),
                        download_url=self._download_links[0],
                        text=" ".join(self._text),
                    )
                )
            self._entry_depth = None
            self._in_name = 0
            self._in_digest = 0
        self._section_depth -= 1


def _matching_release_history(
    source: str,
    *,
    artifact_name: str,
    sha256: str,
    released_by_display: str,
    hidden_principal: str,
) -> _VisibleReleaseHistoryEntry:
    matches = [
        entry
        for entry in _ReleaseHistoryParser(source).entries
        if entry.artifact_name == artifact_name and entry.sha256 == sha256
    ]
    if len(matches) != 1:
        raise ValueError("release history does not retain exactly one reviewed PDF")
    entry = matches[0]
    normalized_text = _normalized_visible_text(entry.text)
    if _normalized_visible_text(released_by_display) not in normalized_text:
        raise ValueError("release history does not freeze the coordinator display name")
    if _normalized_visible_text(hidden_principal) in normalized_text:
        raise ValueError("release history exposes the coordinator principal")
    return entry


def _release_id_from_download(download_url: str) -> int:
    match = re.fullmatch(r"/reports/[^/]+/releases/(?P<identity>\d+)/download", download_url)
    if match is None:
        raise ValueError("release history download control is not bound to one release")
    return int(match.group("identity"))


def _fetch_pdf(
    client: TestClient,
    url: str,
    *,
    message: str,
    progress: _JourneyProgress,
) -> bytes:
    response = client.get(url)
    _require_response(response.status_code, 200, message, progress)
    content_type = str(response.headers.get("content-type") or "").split(";", 1)[0]
    pdf_bytes = bytes(response.content)
    if content_type.casefold() != "application/pdf" or not pdf_bytes.startswith(b"%PDF-"):
        raise _journey_failure(f"{message}: response is not a fixed PDF", progress)
    return pdf_bytes


def _require_response(
    observed: int,
    expected: int,
    message: str,
    progress: _JourneyProgress,
) -> None:
    if observed != expected:
        raise _journey_failure(
            f"{message}: expected HTTP {expected}, observed {observed}",
            progress,
        )


def _journey_failure(
    message: str,
    progress: _JourneyProgress,
) -> _JourneyFailure:
    return progress.failure(message)


def _restore_override(key, previous: object, sentinel: object) -> None:
    if previous is sentinel:
        app.dependency_overrides.pop(key, None)
    else:
        app.dependency_overrides[key] = previous


def _capture_durable_release(
    session: Session,
    *,
    project_slug: str,
    release_identity: dict[str, Any] | None,
) -> _DurableRelease:
    """Capture immutable release facts before any later scenario assertion."""

    project = session.scalar(select(Project).where(Project.slug == project_slug))
    if project is None:
        raise RuntimeError("coordinator clone lost its project")
    if not isinstance(release_identity, dict):
        raise ValueError("coordinator flow did not produce a release receipt")
    artifact_name = release_identity.get("artifact_name")
    pdf_sha256 = release_identity.get("sha256")
    if (
        not isinstance(artifact_name, str)
        or not artifact_name
        or re.fullmatch(r"[0-9a-f]{64}", str(pdf_sha256)) is None
    ):
        raise ValueError("coordinator flow release identity is incomplete")
    release_id = release_identity.get("release_id")
    if isinstance(release_id, int) and not isinstance(release_id, bool):
        release = retrieve_released_external_report(session, project.id, release_id)
    else:
        matching = list(
            session.scalars(
                select(ExternalReportRelease).where(
                    ExternalReportRelease.project_id == project.id,
                    ExternalReportRelease.artifact_name == artifact_name,
                    ExternalReportRelease.pdf_sha256 == pdf_sha256,
                )
            )
        )
        if len(matching) != 1:
            raise ValueError(
                "durable release history does not identify exactly one reviewed PDF"
            )
        release = retrieve_released_external_report(
            session, project.id, matching[0].id
        )
    if (
        release.pdf_sha256 != pdf_sha256
        or release.artifact_name != artifact_name
    ):
        raise ValueError("durable release disagrees with the ordinary reviewed PDF")
    facts = {
        "release_id": release.id,
        "artifact_name": release.artifact_name,
        "sha256": release.pdf_sha256,
        "evaluated_on": release.evaluated_on.isoformat(),
        "ruleset_version": release.ruleset_version,
        "provenance_mode": release.provenance_mode,
        "released_by": release.released_by,
        "released_by_display": release.released_by_display,
        "released_at": release.released_at.isoformat(),
        "record_context": release.record_context_json,
        "evaluation_context": release.evaluation_context_json,
    }
    return _DurableRelease(
        row=release,
        facts=facts,
        pdf_bytes=bytes(release.pdf_bytes),
    )


def _verify_clone_result(
    session: Session,
    *,
    project_slug: str,
    release: ExternalReportRelease,
    candidate_7587_source_receipt: dict[str, Any],
    expected_released_by: str,
    expected_released_by_display: str,
) -> _CloneVerification:
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
    _require(candidates[7296].state == "accepted", "Candidate 7296 did not become accepted")
    _require(equistar_event.event_type == "commitment", "Candidate 7129 is not a Commitment")
    _require(equistar_event.scope_mode == "unknown", "Candidate 7129 scope is not unknown")
    _require(candidates[7129].state == "accepted", "Candidate 7129 did not become accepted")
    km_timings = _timings_by_kind(session, km_event.id)
    _require_timing(
        km_timings.get("previous"),
        text_value="March 2026",
        precision="month",
        start="2026-03-01",
        end="2026-03-31",
        message="Candidate 7296 did not preserve the prior March 2026 timing",
    )
    _require_timing(
        km_timings.get("new"),
        text_value="May 16th",
        precision="day",
        start="2026-05-16",
        end="2026-05-16",
        message="Candidate 7296 did not preserve the later May 16 timing",
    )
    equistar_timings = _timings_by_kind(session, equistar_event.id)
    equistar_timing = equistar_timings.get("new")
    _require_timing(
        equistar_timing,
        text_value="01/2025",
        precision="month",
        start="2025-01-01",
        end="2025-01-31",
        message="Candidate 7129 did not preserve January 2025 month precision",
    )
    km_lineage = session.get(CommitmentLineage, km_event.commitment_lineage_id)
    _require(
        km_lineage is not None
        and km_lineage.milestone_impact == "not_yet_known"
        and km_receipt.milestone_impact_decision_id is not None,
        "Candidate 7296 did not persist the exact Milestone Impact plan",
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
    before_due = _statement_work_item(
        build_work_list(session, project.id, today=date(2025, 1, 31)), equistar_event.id
    )
    equistar_work = _statement_work_item(
        build_work_list(session, project.id, today=date(2025, 2, 1)), equistar_event.id
    )
    _require(
        before_due is not None
        and before_due.past_due is None
        and equistar_work is not None
        and equistar_work.past_due is not None
        and equistar_work.dependency_id is None,
        "Candidate 7129 did not become past due only after January 31 at party level",
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
    candidate_7587_receipts = list(
        session.scalars(
            select(StatementCoordinationReceipt).where(
                StatementCoordinationReceipt.candidate_id == 7587
            )
        )
    )
    work_facts_7587 = _candidate_7587_work_facts(
        build_work_list(session, project.id, today=date(2025, 2, 1))
    )
    _require(
        not candidate_7587_receipts
        and work_facts_7587["forbidden_ledger_work_count"] == 0
        and work_facts_7587["forbidden_past_due_count"] == 0,
        "Candidate 7587 created a Commitment, Work Item, or past-due derivation",
    )
    _require(
        release.project_id == project.id
        and release.released_by == expected_released_by
        and release.released_by_display == expected_released_by_display,
        "released PDF does not freeze the exact coordinator actor and display",
    )
    open_party_statements = _open_party_statements(session, project.id)
    expected_party_statements = _party_statement_context(open_party_statements)
    released_party_statements = release.record_context_json.get("party_statements")
    _require(
        isinstance(released_party_statements, list),
        "released PDF has no frozen party-statement context",
    )
    expected_current_event_ids = {
        entry["current_statement_event_id"] for entry in expected_party_statements
    }
    released_open_party_statements = [
        entry
        for entry in released_party_statements
        if entry.get("current_statement_event_id") in expected_current_event_ids
    ]
    _require(
        released_open_party_statements == expected_party_statements
        and len(released_open_party_statements) == len(expected_party_statements),
        "released PDF does not cover exactly every open unknown-scope statement",
    )
    rendered_text = _require_report_pdf_contents(
        release.pdf_bytes, open_party_statements
    )
    candidate_7587_attribution = _candidate_source_attribution_counts(
        release.record_context_json,
        rendered_text,
        context_external_org=str(
            (candidates[7587].payload_json or {}).get("fields", {}).get(
                "external_org"
            )
            or ""
        ),
        candidate_description=str(
            (candidates[7587].payload_json or {}).get("fields", {}).get(
                "description"
            )
            or ""
        ),
        source_receipt=candidate_7587_source_receipt,
    )
    _require(
        candidate_7587_attribution["report_context_count"] == 0
        and candidate_7587_attribution["released_pdf_count"] == 0,
        "Candidate 7587 source attribution appeared in released Report evidence",
    )
    facts = {
        "candidate_7296": {
            "state": candidates[7296].state,
            "statement_event_id": km_event.id,
            "event_type": km_event.event_type,
            "direction": km_event.timing_direction,
            "scope": km_event.scope_mode,
            "timings": _timing_receipt(km_timings),
            "milestone_impact": km_lineage.milestone_impact,
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
                "not_past_due_on": "2025-01-31",
            },
            "coordination_receipt_id": equistar_receipt.id,
        },
        "candidate_7587": {
            "state": candidates[7587].state,
            "admission_outcome": abstention.outcome,
            "statement_event_id": abstention.dependency_event_id,
            "coordination_receipt_count": len(candidate_7587_receipts),
            **work_facts_7587,
            **candidate_7587_attribution,
        },
    }
    return _CloneVerification(facts=facts, released_pdf_bytes=bytes(release.pdf_bytes))


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _timings_by_kind(session: Session, event_id: int) -> dict[str, DependencyEventTiming]:
    """Read the one preserved timing of each declared kind for an event."""

    return {
        timing.kind: timing
        for timing in session.scalars(
            select(DependencyEventTiming)
            .where(DependencyEventTiming.event_id == event_id)
            .order_by(DependencyEventTiming.id)
        )
    }


def _require_timing(
    timing: DependencyEventTiming | None,
    *,
    text_value: str,
    precision: str,
    start: str,
    end: str,
    message: str,
) -> None:
    """Reject a scalar date or altered wording in a source-preserving timing."""

    _require(
        timing is not None
        and timing.text == text_value
        and timing.precision == precision
        and timing.start_date is not None
        and timing.start_date.isoformat() == start
        and timing.end_date is not None
        and timing.end_date.isoformat() == end,
        message,
    )


def _timing_receipt(
    timings: dict[str, DependencyEventTiming],
) -> dict[str, dict[str, str | None]]:
    """Serialize both source timings so a sealed result retains their precision."""

    return {
        kind: {
            "text": timing.text,
            "precision": timing.precision,
            "start_date": timing.start_date.isoformat() if timing.start_date else None,
            "end_date": timing.end_date.isoformat() if timing.end_date else None,
        }
        for kind, timing in sorted(timings.items())
    }


def _statement_work_item(work_list, event_id: int):
    """Find one party-level statement item without treating it as a Dependency."""

    return next(
        (
            item
            for item in (*work_list.immediate, *work_list.backlog)
            if item.statement_event_id == event_id
        ),
        None,
    )


def _candidate_7587_work_facts(work_list) -> dict[str, int]:
    """Separate the lawful pending source card from forbidden Ledger-derived work."""

    all_items = (
        *work_list.immediate,
        *work_list.backlog,
        *getattr(work_list, "candidate_backlog", ()),
    )
    pending_cards = [
        item
        for item in all_items
        if item.kind == "candidate" and item.candidate_id == 7587
    ]
    ledger_items = [
        item
        for item in (*work_list.immediate, *work_list.backlog)
        if item.kind != "candidate"
        and (
            item.candidate_id == 7587
            or item.source_candidate_id == 7587
        )
    ]
    return {
        "pending_candidate_card_count": len(pending_cards),
        "forbidden_ledger_work_count": len(ledger_items),
        "forbidden_past_due_count": sum(
            item.past_due is not None for item in ledger_items
        ),
    }


def _open_party_statements(session: Session, project_id: int):
    """Read exactly the party-level statements the frozen Report must include."""

    return tuple(
        statement
        for statement in published_party_statements(
            session, project_id=project_id, document_only=False
        )
        if not statement.is_closed
    )


def _party_statement_context(statements) -> list[dict[str, int | None]]:
    """Serialize exactly the statement identities selected by the Report seam."""

    return [
        {
            "commitment_lineage_id": statement.current_event.commitment_lineage_id,
            "current_statement_event_id": statement.current_event.id,
            "published_statement_event_id": (
                statement.event.id if statement.event is not None else None
            ),
            "scope_decision_id": statement.scope_decision.id,
        }
        for statement in statements
    ]


def _candidate_source_attribution_counts(
    record_context: dict[str, Any],
    rendered_text: str,
    *,
    context_external_org: str,
    candidate_description: str,
    source_receipt: dict[str, Any],
) -> dict[str, int]:
    """Count only one Candidate's full frozen source attribution signature."""

    document_id = source_receipt.get("document_id")
    document_name = source_receipt.get("document_name")
    page = source_receipt.get("page")
    quote = source_receipt.get("quote")
    if (
        not isinstance(document_id, int)
        or isinstance(document_id, bool)
        or not isinstance(document_name, str)
        or not document_name
        or not isinstance(page, int)
        or isinstance(page, bool)
        or page <= 0
        or not isinstance(quote, str)
        or not quote
        or not isinstance(context_external_org, str)
        or not context_external_org
        or not isinstance(candidate_description, str)
        or not candidate_description
    ):
        raise ValueError("Candidate source attribution receipt is incomplete")
    expected_source_context = (
        f"{document_name} · page {page} · “{quote}”"
    )
    displays = record_context.get("party_statement_display", ())
    if not isinstance(displays, list):
        raise ValueError("released Report party-statement display context is invalid")
    matching_displays = [
        entry
        for entry in displays
        if isinstance(entry, dict)
        and _normalized_visible_text(str(entry.get("source_context") or ""))
        == _normalized_visible_text(expected_source_context)
    ]
    normalized_pdf = _normalized_visible_text(rendered_text)
    marker_pattern = re.escape(f"[d{document_id} p.{page}]".casefold())
    display_signatures = {
        (context_external_org, candidate_description),
        *(
            (
                str(entry.get("external_party") or ""),
                str(entry.get("supported_statement") or ""),
            )
            for entry in matching_displays
        ),
    }
    pdf_count = 0
    for displayed_party, statement in display_signatures:
        normalized_party = _normalized_visible_text(displayed_party)
        normalized_statement = _normalized_visible_text(statement)
        if not normalized_party or not normalized_statement:
            continue
        pdf_pattern = re.compile(
            rf"{re.escape(normalized_party)}\s+{marker_pattern}.{{0,2048}}?"
            rf"{re.escape(normalized_statement)}\s+{marker_pattern}"
        )
        pdf_count += len(tuple(pdf_pattern.finditer(normalized_pdf)))
    return {
        "report_context_count": len(matching_displays),
        "released_pdf_count": pdf_count,
    }


def _require_report_pdf_contents(pdf_bytes: bytes, statements) -> str:
    """Require complete current or retained legacy report labels and source facts."""

    import fitz

    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
            rendered_text = "\n".join(page.get_text() for page in document)
    except (RuntimeError, ValueError) as exc:
        raise ValueError("released PDF bytes are not readable") from exc
    # Keep exact label profiles here: a saved PDF is not rerendered through the
    # current vocabulary, and a partial mix of column sets is not a valid report.
    profiles = (
        (
            {
                "Organization commitments",
                "Organization",
                "Supported statement",
                "Timing",
                "Timing precision",
                "Statement type",
                "Applies to",
                "Open / past-due status",
                "Assigned to",
                "Next action",
                "Action due date",
                "Effect on key dates",
                "Not yet known",
            },
            "Unstated organization",
            "Change to promised timing · later",
        ),
        (
            {
                "External Party commitments",
                "External Party",
                "Supported statement",
                "Timing",
                "Timing precision",
                "Statement type",
                "Commitment Scope",
                "Open / past-due status",
                "Internal Owner",
                "Next Action",
                "Action Due",
                "Milestone Impact",
                "Scope not yet known",
            },
            "Unstated External Party",
            "Committed Date Change · later",
        ),
    )
    required = set()
    for statement in statements:
        event = statement.event
        _require(event is not None, "released Report has unsupported current statement")
        required.add(event.description)
        for profile, unstated_label, changed_timing_label in profiles:
            profile.update(
                {
                    event.stated_party or unstated_label,
                    changed_timing_label
                    if event.event_type == "committed_date_change"
                    else "Commitment",
                }
            )
        required.update(timing.text for timing in statement.timings)
        required.update(timing.precision for timing in statement.timings)
        if statement.plan.internal_owner:
            required.add(statement.plan.internal_owner)
        if statement.plan.next_action:
            required.add(statement.plan.next_action)
        if statement.plan.action_due_date is None and statement.plan.next_action_decision:
            required.add("Date not yet known")
        if statement.plan.milestone_impact:
            required.add(statement.plan.milestone_impact.replace("_", " ").capitalize())
    normalized_rendered_text = _normalized_visible_text(rendered_text)
    missing_profiles = [
        sorted(
            value
            for value in required | profile
            if _normalized_visible_text(value) not in normalized_rendered_text
        )
        for profile, _, _ in profiles
    ]
    missing = min(missing_profiles, key=len)
    _require(not missing, "released PDF omits required Report fields: " + ", ".join(missing))
    return rendered_text


def _run_scope_coverage(database_url: str) -> dict[str, Any]:
    """Record public scope coverage against the still-live disposable clone."""

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
        env={**os.environ, "DATABASE_URL": database_url},
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


def _semantic_unqualified_pass(
    *,
    operations: dict[str, Any],
    coordinator: dict[str, Any],
    outcome: dict[str, Any],
    verification: dict[str, Any],
) -> bool:
    """Derive the sole v3 pass state from complete, independently checked facts."""

    targets = {"7296": 5 * 60, "7129_and_release": 3 * 60}
    scenario_timings = coordinator.get("scenario_timings")
    if not isinstance(scenario_timings, dict):
        return False
    timings_valid = all(
        isinstance(scenario_timings.get(name), (int, float))
        and not isinstance(scenario_timings.get(name), bool)
        and math.isfinite(float(scenario_timings[name]))
        and 0 <= float(scenario_timings[name]) <= target
        for name, target in targets.items()
    )
    release = outcome.get("released_pdf")
    verified_release = verification.get("release")
    release_valid = (
        isinstance(release, dict)
        and isinstance(release.get("release_id"), int)
        and not isinstance(release.get("release_id"), bool)
        and isinstance(release.get("artifact_name"), str)
        and bool(release.get("artifact_name"))
        and re.fullmatch(r"[0-9a-f]{64}", str(release.get("sha256"))) is not None
        and isinstance(verified_release, dict)
        and isinstance(verified_release.get("released_by_display"), str)
        and bool(verified_release.get("released_by_display"))
        and all(
            release.get(field) == verified_release.get(field)
            for field in ("release_id", "artifact_name", "sha256")
        )
    )
    return bool(
        timings_valid
        and not outcome.get("assistance")
        and not outcome.get("errors")
        and not outcome.get("deviations")
        and not coordinator.get("retries")
        and operations.get("source_database_mutated") is False
        and release_valid
        and _semantic_verification_complete(coordinator, verification)
    )


def _semantic_verification_complete(
    coordinator: dict[str, Any], verification: dict[str, Any]
) -> bool:
    """Require the concrete post-run facts behind a current-schema pass."""

    candidate_7296 = verification.get("candidate_7296")
    candidate_7129 = verification.get("candidate_7129")
    candidate_7587 = verification.get("candidate_7587")
    if not all(
        isinstance(candidate, dict)
        for candidate in (candidate_7296, candidate_7129, candidate_7587)
    ):
        return False
    assert isinstance(candidate_7296, dict)
    assert isinstance(candidate_7129, dict)
    assert isinstance(candidate_7587, dict)
    km_timings = candidate_7296.get("timings")
    equistar_timing = candidate_7129.get("timing")
    equistar_past_due = candidate_7129.get("past_due")
    if not all(
        isinstance(value, dict)
        for value in (km_timings, equistar_timing, equistar_past_due)
    ):
        return False
    assert isinstance(km_timings, dict)
    km_previous = km_timings.get("previous")
    km_new = km_timings.get("new")
    if not isinstance(km_previous, dict) or not isinstance(km_new, dict):
        return False
    candidate_facts_valid = (
        candidate_7296.get("state") == "accepted"
        and candidate_7296.get("event_type") == "committed_date_change"
        and candidate_7296.get("direction") == "later"
        and candidate_7296.get("scope") == "unknown"
        and candidate_7296.get("milestone_impact") == "not_yet_known"
        and km_previous
        == {
            "text": "March 2026",
            "precision": "month",
            "start_date": "2026-03-01",
            "end_date": "2026-03-31",
        }
        and km_new
        == {
            "text": "May 16th",
            "precision": "day",
            "start_date": "2026-05-16",
            "end_date": "2026-05-16",
        }
        and candidate_7129.get("state") == "accepted"
        and candidate_7129.get("event_type") == "commitment"
        and candidate_7129.get("scope") == "unknown"
        and equistar_timing
        == {
            "text": "01/2025",
            "precision": "month",
            "start_date": "2025-01-01",
            "end_date": "2025-01-31",
        }
        and equistar_past_due.get("evaluated_on") == "2025-02-01"
        and equistar_past_due.get("due_after") == "2025-01-31"
        and equistar_past_due.get("not_past_due_on") == "2025-01-31"
        and candidate_7587.get("state") == "pending"
        and candidate_7587.get("admission_outcome") == "abstained"
        and candidate_7587.get("statement_event_id") is None
        and candidate_7587.get("coordination_receipt_count") == 0
        and candidate_7587.get("forbidden_ledger_work_count") == 0
        and candidate_7587.get("forbidden_past_due_count") == 0
        and candidate_7587.get("report_context_count") == 0
        and candidate_7587.get("released_pdf_count") == 0
    )
    page_image_fetches = verification.get("page_image_fetches")
    images_valid = isinstance(page_image_fetches, dict) and set(
        page_image_fetches
    ) == {"7296", "7129"}
    if images_valid:
        for receipt in page_image_fetches.values():
            images_valid = bool(
                isinstance(receipt, dict)
                and set(receipt)
                == {"url", "document_name", "registered_page", "sha256", "bytes"}
                and str(receipt.get("url") or "").startswith("/page-image/")
                and isinstance(receipt.get("document_name"), str)
                and bool(receipt.get("document_name"))
                and isinstance(receipt.get("registered_page"), int)
                and not isinstance(receipt.get("registered_page"), bool)
                and receipt["registered_page"] > 0
                and re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("sha256")))
                is not None
                and isinstance(receipt.get("bytes"), int)
                and not isinstance(receipt.get("bytes"), bool)
                and receipt["bytes"] > 8
            )
            if not images_valid:
                break
    coverage = verification.get("automated_scope_coverage")
    coverage_valid = (
        isinstance(coverage, dict)
        and coverage.get("returncode") == 0
        and coverage.get("covered_scope_modes") == ["selected", "all_active"]
    )
    required_interactions = (
        "save_kinder_morgan_statement",
        "save_equistar_statement",
        "open_external_report_release",
        "render_fixed_pdf_for_review",
        "review_fixed_pdf_preview",
        "download_fixed_pdf_for_review",
        "release_fixed_pdf",
        "retrieve_released_pdf",
        "reload_release_history",
    )
    interactions = coordinator.get("interactions")
    interaction_positions: list[int] = []
    if isinstance(interactions, list):
        start = 0
        for required in required_interactions:
            try:
                position = interactions.index(required, start)
            except ValueError:
                interaction_positions = []
                break
            interaction_positions.append(position)
            start = position + 1
    return bool(
        verification.get("valid") is True
        and candidate_facts_valid
        and images_valid
        and coverage_valid
        and len(interaction_positions) == len(required_interactions)
    )


def _canonical_content(capture: CoordinatorRehearsalCapture) -> dict[str, Any]:
    _require_capture_shape(capture)
    outcome = dict(capture.outcome)
    unqualified_pass = _semantic_unqualified_pass(
        operations=capture.operations,
        coordinator=capture.coordinator,
        outcome=outcome,
        verification=capture.verification,
    )
    expected_status = "passed" if unqualified_pass else "failed"
    if outcome["status"] != expected_status:
        raise ValueError(
            "declared outcome status does not match timings, assistance, errors, "
            "deviations, retries, release, source safety, and verification"
        )
    supplied_unqualified = outcome.pop("unqualified_pass", None)
    if supplied_unqualified is not None and supplied_unqualified is not unqualified_pass:
        raise ValueError("declared unqualified pass does not match rehearsal evidence")
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
        "source_migration_head",
        "target_migration_head",
        "source_dump_sha256",
        "environment_details",
        "shared_admission_receipt",
        "approved_shared_state_receipt",
        "corpus_inputs",
        "active_runs",
        "policy_identities",
        "report_publication",
        "seeded_coordinator",
        "scenario_candidates",
        "scenario_source_receipts",
    }
    missing_inputs = sorted(required_inputs - set(capture.inputs))
    if missing_inputs:
        raise ValueError(f"rehearsal inputs are missing {', '.join(missing_inputs)}")
    if capture.outcome.get("status") not in {"passed", "failed"}:
        raise ValueError("rehearsal outcome status must be passed or failed")
    for section, values, fields in (
        (
            "operations",
            capture.operations,
            {
                "elapsed_seconds",
                "backfill_elapsed_seconds",
                "source_database_mutated",
                "source_migration_head_before",
                "source_migration_head_after",
                "source_state_sha256_before",
                "source_state_sha256_after",
                "source_scenario_receipts_sha256_before",
                "source_scenario_receipts_sha256_after",
                "clone_upgrade",
            },
        ),
        (
            "coordinator",
            capture.coordinator,
            {"elapsed_seconds", "scenario_timings", "interactions", "retries"},
        ),
        ("outcome", capture.outcome, {"assistance", "errors", "deviations", "released_pdf"}),
    ):
        missing = sorted(fields - set(values))
        if missing:
            raise ValueError(f"{section} is missing {', '.join(missing)}")
    if not isinstance(capture.outcome["assistance"], list):
        raise ValueError("rehearsal assistance must be a list")
    if not isinstance(capture.outcome["errors"], list):
        raise ValueError("rehearsal errors must be a list")
    _require_v3_integrity_receipts(capture)
    _require_v3_release_metadata_bindings(capture)


def _require_v3_integrity_receipts(capture: CoordinatorRehearsalCapture) -> None:
    """Require exact source-page and predecessor-upgrade receipts for every v3 run."""

    inputs = capture.inputs
    operations = capture.operations
    source_head = inputs["source_migration_head"]
    target_head = inputs["target_migration_head"]
    if any(
        re.fullmatch(r"[0-9a-f]{12}", str(value)) is None
        for value in (source_head, target_head)
    ):
        raise ValueError("source and target migration heads must be exact revisions")
    if any(
        re.fullmatch(r"[0-9a-f]{64}", str(inputs.get(field))) is None
        for field in ("source_snapshot_sha256", "source_dump_sha256")
    ):
        raise ValueError("source snapshot and dump digests must be exact SHA-256 values")
    if not str(inputs.get("approved_shared_state_receipt") or "").strip():
        raise ValueError("approved shared-state receipt is required")
    candidates = inputs.get("scenario_candidates")
    receipts = inputs.get("scenario_source_receipts")
    if candidates != {"7296": 7296, "7129": 7129, "7587": 7587}:
        raise ValueError("scenario Candidate identities are incomplete")
    if not isinstance(receipts, dict) or set(receipts) != {"7296", "7129", "7587"}:
        raise ValueError("scenario source receipts must cover exactly 7296, 7129, and 7587")
    for candidate_id, receipt in receipts.items():
        if not isinstance(receipt, dict):
            raise ValueError(f"scenario source receipt {candidate_id} is invalid")
        required = {
            "candidate_id",
            "document_id",
            "document_name",
            "document_date",
            "document_sha256",
            "page",
            "quote",
            "text_source",
            "page_text_sha256",
            "page_image",
        }
        image = receipt.get("page_image")
        if (
            not required <= set(receipt)
            or receipt.get("candidate_id") != int(candidate_id)
            or not isinstance(receipt.get("document_id"), int)
            or not isinstance(receipt.get("page"), int)
            or int(receipt["page"]) <= 0
            or not all(
                isinstance(receipt.get(field), str) and bool(receipt[field])
                for field in ("document_name", "document_date", "quote", "text_source")
            )
            or re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("document_sha256")))
            is None
            or re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("page_text_sha256")))
            is None
            or not isinstance(image, dict)
            or image.get("url")
            != f"/page-image/{receipt.get('document_id')}/{receipt.get('page')}"
            or not isinstance(image.get("bytes"), int)
            or image["bytes"] <= 8
            or re.fullmatch(r"[0-9a-f]{64}", str(image.get("sha256"))) is None
        ):
            raise ValueError(f"scenario source receipt {candidate_id} is incomplete")
    clone_upgrade = operations.get("clone_upgrade")
    if (
        not isinstance(clone_upgrade, dict)
        or not str(clone_upgrade.get("database_name") or "").startswith(DATABASE_PREFIX)
        or clone_upgrade.get("from_revision") != source_head
        or clone_upgrade.get("to_revision") != target_head
        or clone_upgrade.get("verified_revision") != target_head
        or operations.get("source_migration_head_before") != source_head
        or operations.get("source_migration_head_after") != source_head
        or operations.get("source_state_sha256_before")
        != operations.get("source_state_sha256_after")
        or operations.get("source_scenario_receipts_sha256_before")
        != operations.get("source_scenario_receipts_sha256_after")
    ):
        raise ValueError("predecessor clone or unchanged-source receipt is invalid")


def _require_v3_release_metadata_bindings(
    capture: CoordinatorRehearsalCapture,
) -> None:
    """Bind immutable release metadata to the independently sealed v3 inputs."""

    publication = capture.inputs.get("report_publication")
    coordinator = capture.inputs.get("seeded_coordinator")
    if (
        not isinstance(publication, dict)
        or not all(
            isinstance(publication.get(field), str) and bool(publication[field])
            for field in ("ruleset_version", "provenance_mode")
        )
        or publication.get("evaluation_thresholds") != _v3_evaluation_thresholds()
        or not isinstance(coordinator, dict)
        or not all(
            isinstance(coordinator.get(field), str) and bool(coordinator[field])
            for field in ("subject", "display_name")
        )
    ):
        raise ValueError("Report publication or seeded coordinator receipt is incomplete")
    release = capture.verification.get("release")
    if release is None:
        return
    evaluation = release.get("evaluation_context") if isinstance(release, dict) else None
    if not isinstance(release, dict) or not isinstance(evaluation, dict):
        raise ValueError("release evaluation receipt is incomplete")
    if (
        release.get("ruleset_version") != publication["ruleset_version"]
        or release.get("provenance_mode") != publication["provenance_mode"]
        or release.get("released_by") != coordinator["subject"]
        or release.get("released_by_display") != coordinator["display_name"]
        or evaluation.get("evaluated_on") != release.get("evaluated_on")
        or evaluation.get("ruleset_version") != release.get("ruleset_version")
        or evaluation.get("thresholds") != publication["evaluation_thresholds"]
    ):
        raise ValueError("release metadata does not match its sealed inputs")
    evaluated_on = release.get("evaluated_on")
    released_at = release.get("released_at")
    try:
        parsed_evaluated_on = date.fromisoformat(evaluated_on)
        parsed_released_at = datetime.fromisoformat(released_at)
    except (TypeError, ValueError) as exc:
        raise ValueError("release evaluation or timestamp is not canonical ISO") from exc
    if (
        parsed_evaluated_on.isoformat() != evaluated_on
        or parsed_released_at.tzinfo is None
        or parsed_released_at.utcoffset() is None
    ):
        raise ValueError("release timestamp must retain an explicit timezone")


def _v3_evaluation_thresholds() -> dict[str, int]:
    """Return the exact ordinary Report Evaluation policy sealed by bundle v3."""

    return dict(_V3_EVALUATION_THRESHOLD_ITEMS)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_sha256(value: Any) -> str:
    return _sha256(_canonical_json(value))
