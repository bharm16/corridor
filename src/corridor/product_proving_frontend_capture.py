"""Observe a Product Proving practitioner pass from durable application facts.

The browser driver is deliberately not an authority for the receipt.  It may
provide timings, usability notes, and hashes of screenshots it retained.  All
claims about Candidate outcomes, frontend writes, append-only history, Report
bytes, and release binding are re-read from the live Project Record after the
ordinary frontend workflow has committed.

This module does not perform project work.  It is the post-frontend capture
boundary between the real application and ``ProductProvingPass``.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Literal, Mapping
from uuid import UUID

import pymupdf
from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor import audit
from corridor.frontend_request_receipts import (
    ROUTE_CONTRACTS as REGISTERED_FRONTEND_ROUTE_CONTRACTS,
    SCHEMA_VERSION as FRONTEND_REQUEST_SCHEMA,
)
from corridor.models import (
    AuditLog,
    Assertion,
    Candidate,
    CandidateDisposition,
    Dependency,
    DependencyEventEvidence,
    DependencyEventTiming,
    EvidenceLink,
    EvidenceInvestigationCandidateReviewStart,
    ExternalPartyStatement,
    ExternalReportArtifact,
    ExternalReportRelease,
    ExtractionRun,
    ReportRun,
    WorkDecision,
)
from corridor.m8_acceptance_bundle import publish_verified_bundle, verify_bundle
from corridor.product_proving_execution import (
    LiveProductProvingOperationsCapture,
    ProjectWriteSetDiff,
    ProjectWriteSetSnapshot,
    capture_project_write_set,
    diff_project_write_sets,
    require_proving_session_database_identity,
    validate_bounded_operations_write_set,
)
from corridor.product_proving_database import (
    DatabaseFingerprint,
    fingerprint_database_url,
    observe_database_connection_identity,
    parse_database_connection_identity,
    parse_database_fingerprint,
)
from corridor.principals import HumanPrincipal
from corridor.product_proving_run import (
    CandidateSetComparison,
    ExpectedPreflight,
    ExtractionConfiguration,
    FRONTEND_EVIDENCE_KIND,
    ObservedPreflight,
    ProductProvingPass,
    verify_preflight,
    verify_product_proving_pass,
)
from corridor.statement_coordination import pending_candidate_authority_gap


ResidualOutcome = Literal["supported", "not_relevant", "unresolved"]
FactualCorrectionOutcome = Literal[
    "preserved_predecessor", "no_structured_correction_observed"
]

_SUPPORTED_ACTIONS = frozenset(
    {
        audit.ACCEPT_CANDIDATE,
        audit.MERGE_CANDIDATE,
        audit.ATTACH_STATEMENT,
        audit.COORDINATE_STATEMENT,
    }
)
_WORK_DECISION_ACTIONS = frozenset(
    {
        audit.ASSIGN_INTERNAL_OWNER,
        audit.SET_NEXT_ACTION,
        audit.COMPLETE_NEXT_ACTION,
        audit.CANCEL_NEXT_ACTION,
        audit.SET_MILESTONE_IMPACT,
        audit.DEFER_WORK,
        audit.RESUME_WORK,
    }
)
_PROVENANCE_ORDER = (
    "Assertion",
    "Derivation",
    "Work Decision",
    "Verbal",
)
_PROVENANCE_CLASSES = frozenset(_PROVENANCE_ORDER)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MIN_SCREENSHOT_WIDTH = 800
_MIN_SCREENSHOT_HEIGHT = 450
_MAX_SCREENSHOT_DIMENSION = 16_384
_FRONTEND_SUBJECT_KEYS = frozenset(
    {
        "project_id",
        "candidate_id",
        "dependency_id",
        "commitment_lineage_id",
        "statement_event_id",
        "predecessor_statement_event_id",
        "successor_statement_event_id",
        "work_decision_id",
        "artifact_id",
        "release_id",
        "report_run_id",
        "check_configuration_id",
    }
)
_PRODUCT_PROVING_ROUTE_CONTRACT_DOCUMENTATION = {
    "coordinate_statement_screen": (
        "/statements/{slug}/{candidate_id}/coordinate",
        "GET",
        frozenset({200}),
    ),
    "save_coordinated_statement": (
        "/statements/{slug}/{candidate_id}/coordinate",
        "POST",
        frozenset({303}),
    ),
    "save_admitted_statement_scope": (
        "/statements/{slug}/{candidate_id}/admitted/scope",
        "POST",
        frozenset({400}),
    ),
    "keep_unresolved_statement": (
        "/statements/{slug}/{candidate_id}/keep-unresolved",
        "POST",
        frozenset({303}),
    ),
    "save_admitted_statement_owner": (
        "/statements/{slug}/{candidate_id}/admitted/owner",
        "POST",
        frozenset({303}),
    ),
    "save_admitted_statement_next_action": (
        "/statements/{slug}/{candidate_id}/admitted/next-action",
        "POST",
        frozenset({303}),
    ),
    "mark_waiting_statement_not_relevant": (
        "/statements/{slug}/{candidate_id}/not-relevant",
        "POST",
        frozenset({303}),
    ),
    "correct_statement_screen": (
        "/statements/{slug}/{candidate_id}/correct",
        "GET",
        frozenset({200}),
    ),
    "correct_statement_scope_from_screen": (
        "/statements/{slug}/{candidate_id}/correct/scope",
        "POST",
        frozenset({303, 400, 409}),
    ),
    "correct_statement_facts_from_screen": (
        "/statements/{slug}/{candidate_id}/correct/facts",
        "POST",
        frozenset({303}),
    ),
    "reports": ("/reports/{slug}", "GET", frozenset({200})),
    "review_report": (
        "/reports/{slug}/prepared/{artifact_id}",
        "GET",
        frozenset({200}),
    ),
    "download_prepared_report": (
        "/reports/{slug}/prepared/{artifact_id}/download",
        "GET",
        frozenset({200}),
    ),
    "preview_prepared_report": (
        "/reports/{slug}/prepared/{artifact_id}/preview",
        "GET",
        frozenset({200}),
    ),
    "release_prepared_report": (
        "/reports/{slug}/prepared/{artifact_id}/release",
        "POST",
        frozenset({201}),
    ),
    "render_report": (
        "/reports/{slug}/render",
        "POST",
        frozenset({201}),
    ),
    "release_report": (
        "/reports/{slug}/release",
        "POST",
        frozenset({201}),
    ),
    "coordinator_home": ("/work/{slug}", "GET", frozenset({200})),
    "queue": ("/queue/{slug}", "GET", frozenset({200})),
    "internal_report": ("/internal-report/{slug}", "GET", frozenset({200})),
    "internal_report_full": (
        "/internal-report/{slug}/full",
        "GET",
        frozenset({200}),
    ),
    "internal_report_alerts": (
        "/internal-report/{slug}/alerts/{rule}",
        "GET",
        frozenset({200}),
    ),
    "internal_report_workbook": (
        "/internal-report/{slug}/workbook.xlsx",
        "GET",
        frozenset({200}),
    ),
    "operations_checks": (
        "/operations/{slug}/checks",
        "GET",
        frozenset({200}),
    ),
    "operations_checks_preview": (
        "/operations/{slug}/checks/preview",
        "POST",
        frozenset({200, 400}),
    ),
    "save_operations_checks": (
        "/operations/{slug}/checks",
        "POST",
        frozenset({303, 400}),
    ),
    "assign_owner": (
        "/dependencies/{dependency_id}/owner",
        "POST",
        frozenset({303}),
    ),
        "record_next_action": (
            "/dependencies/{dependency_id}/action",
            "POST",
            frozenset({303}),
        ),
        "confirm_documentation_approval": (
            "/dependencies/{dependency_id}/documentation/confirm-approval",
            "POST",
            frozenset({303}),
        ),
        "keep_unresolved_candidate": (
        "/candidates/{candidate_id}/keep-unresolved",
        "POST",
        frozenset({303}),
    ),
    "accept": (
        "/candidates/{candidate_id}/accept",
        "POST",
        frozenset({303}),
    ),
    "edit_accept": (
        "/candidates/{candidate_id}/edit-accept",
        "POST",
        frozenset({303}),
    ),
    "merge": (
        "/candidates/{candidate_id}/merge",
        "POST",
        frozenset({303}),
    ),
    "reject": (
        "/candidates/{candidate_id}/reject",
        "POST",
        frozenset({303}),
    ),
}
# The write seam owns the canonical registry; the local literal above remains
# readable documentation of the Product Proving subset, while validation uses
# exactly the contracts the routes use to write their receipts.
if _PRODUCT_PROVING_ROUTE_CONTRACT_DOCUMENTATION != dict(
    REGISTERED_FRONTEND_ROUTE_CONTRACTS
):
    raise RuntimeError("Product Proving frontend route contracts drifted")
_FRONTEND_ROUTE_CONTRACTS = REGISTERED_FRONTEND_ROUTE_CONTRACTS
FRONTEND_PASS_BUNDLE_SCHEMA_VERSION = "corridor.product-proving-frontend-pass.v1"
FRONTEND_PASS_BUNDLE_FILES = (
    "canonical-content.json",
    "receipt.json",
    "approved-export.pdf",
    "screenshot-evidence.json",
)


class CorruptFrontendPassBundle(ValueError):
    """A per-pass proving bundle no longer matches its immutable manifest."""


@dataclass(frozen=True)
class FrontendPassMetadata:
    """Non-authoritative measurements retained alongside observed receipts."""

    pass_number: int
    operations_elapsed_seconds: float
    practitioner_elapsed_seconds: float
    non_blocking_friction: tuple[str, ...] = ()
    screenshot_sha256: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RefusedInvalidActionObservation:
    """Exact snapshots bracketing one visibly refused frontend action."""

    before: ProjectWriteSetSnapshot
    after: ProjectWriteSetSnapshot
    write_set: ProjectWriteSetDiff
    refused: bool
    frontend_request_audit_id: int
    route_name: str
    status: int
    principal: str


@dataclass(frozen=True)
class ResidualCandidateObservation:
    """One originally residual Candidate and the receipts for its honest result."""

    candidate_id: int
    candidate_kind: str
    candidate_state: str
    outcome: ResidualOutcome
    review_start_id: int
    principal: str
    disposition_id: int | None
    audit_log_id: int
    route_action: str


@dataclass(frozen=True)
class WorkDecisionChangeObservation:
    """One appended Work Decision successor whose predecessor still exists."""

    predecessor_decision_id: int
    successor_decision_id: int
    field: str
    dependency_id: int | None
    commitment_lineage_id: int | None
    audit_log_id: int


@dataclass(frozen=True)
class FactualCorrectionObservation:
    """The packet-supported correction, or an honest absence of one."""

    outcome: FactualCorrectionOutcome
    audit_log_id: int | None
    predecessor_statement_event_id: int | None
    successor_statement_event_id: int | None
    supporting_candidate_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class ReportReleaseObservation:
    """The exact retained Report, prepared artifact, and Approved Export."""

    report_run_id: int
    artifact_id: int
    release_id: int
    pdf_sha256: str
    pdf_bytes: bytes
    provenance_classes: tuple[str, ...]


@dataclass(frozen=True)
class FrontendRequestObservation:
    """One server-observed ordinary route used by the practitioner."""

    audit_log_id: int
    route_name: str
    route_template: str
    method: str
    status: int
    principal: str
    request_fields_sha256: str
    subject: Mapping[str, int]


@dataclass(frozen=True)
class ObservedFrontendPass:
    """Auditable frontend observations plus the receipt contract they establish."""

    operations_capture: LiveProductProvingOperationsCapture
    product_proving_pass: ProductProvingPass
    residual_candidates: tuple[ResidualCandidateObservation, ...]
    invalid_action: RefusedInvalidActionObservation
    work_decision_changes: tuple[WorkDecisionChangeObservation, ...]
    factual_correction: FactualCorrectionObservation
    report_release: ReportReleaseObservation
    frontend_requests: tuple[FrontendRequestObservation, ...]
    screenshot_sha256: Mapping[str, str]
    final_write_set_snapshot: ProjectWriteSetSnapshot
    terminal_database_fingerprint: DatabaseFingerprint


@dataclass(frozen=True)
class FrontendPassBundleSummary:
    """Caller-held identities for one immutable, self-verified pass bundle."""

    bundle_dir: Path
    manifest_path: Path
    integrity_manifest_sha256: str
    canonical_content_sha256: str


@dataclass(frozen=True)
class VerifiedFrontendPass:
    """A pass reconstructed and validated without the source database."""

    valid: bool
    bundle_dir: Path
    integrity_manifest_sha256: str
    canonical_content_sha256: str
    product_proving_pass: ProductProvingPass
    expected: ExpectedPreflight
    observed: ObservedPreflight
    database_baseline_manifest_sha256: str
    database_baseline_dump_sha256: str
    database_baseline_state_sha256: str
    database_baseline_schema_sha256: str
    database_source_identity: Mapping[str, object]
    database_source_connection_identity: Mapping[str, object]
    screenshot_sha256: Mapping[str, str]
    terminal_database_state_sha256: str
    pass_number: int
    execution_id: str
    started_at: str
    prior_restore_operation_id: str | None
    prior_restore_bundle_manifest_sha256: str | None
    prior_restore_bundle_canonical_sha256: str | None


def observe_refused_invalid_action(
    session: Session,
    *,
    before: ProjectWriteSetSnapshot,
    after: ProjectWriteSetSnapshot,
) -> RefusedInvalidActionObservation:
    """Prove one server-observed 400 response changed no domain state."""

    measured = diff_project_write_sets(before, after)
    if any(measured.deleted.values()) or any(measured.updated.values()):
        raise ValueError("invalid frontend action wrote protected Project state")
    changed_tables = {table for table, rows in measured.created.items() if rows}
    if changed_tables != {"audit_log"}:
        raise ValueError("invalid frontend action wrote protected Project state")
    audit_ids = _created_ids(measured, "audit_log")
    receipts = tuple(
        session.scalars(
            select(AuditLog).where(
                AuditLog.id.in_(audit_ids),
                AuditLog.action == audit.PRODUCT_PROVING_FRONTEND_REQUEST,
            )
        ).all()
    )
    if len(receipts) != 1 or audit_ids != {receipts[0].id}:
        raise ValueError("invalid action lacks one server-observed frontend receipt")
    receipt = receipts[0]
    after_json = receipt.after_json or {}
    if (
        after_json.get("schema_version") != FRONTEND_REQUEST_SCHEMA
        or after_json.get("route_name") != "correct_statement_scope_from_screen"
        or after_json.get("method") != "POST"
        or after_json.get("status") != 400
        or not receipt.human_principal
    ):
        raise ValueError("invalid action frontend receipt is not the expected refusal")
    empty = ProjectWriteSetDiff(
        project_id=before.project_id,
        project_slug=before.project_slug,
        created={table: () for table in measured.created},
        deleted={table: () for table in measured.deleted},
        updated={table: () for table in measured.updated},
    )
    return RefusedInvalidActionObservation(
        before=before,
        after=after,
        write_set=empty,
        refused=True,
        frontend_request_audit_id=receipt.id,
        route_name=str(after_json["route_name"]),
        status=400,
        principal=receipt.human_principal,
    )


def capture_product_proving_frontend_pass(
    session: Session,
    *,
    operations_capture: LiveProductProvingOperationsCapture,
    metadata: FrontendPassMetadata,
    invalid_action_before: ProjectWriteSetSnapshot,
    invalid_action_after: ProjectWriteSetSnapshot,
    database_url: str,
    _fingerprint_for_test: Callable[[str], DatabaseFingerprint] | None = None,
) -> ObservedFrontendPass:
    """Build a proving-pass receipt only from live, attributable receipts.

    In particular there are no parameters for Candidate ids or outcomes,
    frontend actions, correction success, Work Decision preservation, Report
    identities, PDF bytes, provenance classes, write sets, or workaround flags.
    Supplying those as caller assertions is therefore impossible at this seam.
    """

    metadata = _validated_metadata(metadata)
    verify_preflight(operations_capture.expected, operations_capture.observed)
    _validate_database_baseline_identity(operations_capture)
    _validate_execution_identity(operations_capture)
    if metadata.pass_number != operations_capture.pass_number:
        raise ValueError("frontend metadata pass number disagrees with live operations")
    operations = operations_capture.operations
    if not operations.admission_started or not operations.extraction_equal:
        raise ValueError("frontend capture requires passed durable Admission")
    if operations.extraction_failures:
        raise ValueError("frontend capture cannot follow an extraction failure")
    if operations.before_write_set.project_id != operations.project_id:
        raise ValueError("operations write set names a different Project")

    # Re-run the operations validator rather than trusting the dataclass fields
    # supplied by an orchestration layer. It remains valid after practitioner
    # work because it compares the immutable operation-time snapshots.
    measured_operations = validate_bounded_operations_write_set(
        session,
        before=operations.before_write_set,
        after=operations.after_write_set,
        baseline_runs=operations_capture.expected.baseline_runs,
        fresh_run_ids=operations.fresh_run_ids,
        admission_started=operations.admission_started,
    )
    if measured_operations != operations.write_set:
        raise ValueError("operations write-set receipt is not its measured diff")

    invalid_action = observe_refused_invalid_action(
        session,
        before=invalid_action_before,
        after=invalid_action_after,
    )
    if (
        invalid_action.before.project_id != operations.project_id
        or invalid_action.before.project_slug
        != operations.before_write_set.project_slug
    ):
        raise ValueError("invalid action snapshots name a different proving Project")

    session.expire_all()
    final_snapshot = capture_project_write_set(
        session, operations.before_write_set.project_slug
    )
    read_fingerprint = _fingerprint_for_test or fingerprint_database_url
    require_proving_session_database_identity(
        session,
        database_url=database_url,
        expected_identity=operations_capture.database_source_identity,
    )
    if observe_database_connection_identity(database_url).as_dict() != dict(
        operations_capture.database_source_connection_identity
    ):
        raise ValueError("frontend capture database server identity changed")
    terminal_fingerprint = parse_database_fingerprint(
        read_fingerprint(database_url).as_dict()
    )
    if (
        terminal_fingerprint.state_sha256
        == operations_capture.observed.baseline_fingerprint
    ):
        raise ValueError("frontend pass made no observable database-state transition")
    if (
        terminal_fingerprint.schema_sha256
        != operations_capture.database_baseline_fingerprint.schema_sha256
        or terminal_fingerprint.schema_objects
        != operations_capture.database_baseline_fingerprint.schema_objects
    ):
        raise ValueError("frontend pass changed the verified database schema")
    final_diff = diff_project_write_sets(operations.before_write_set, final_snapshot)
    if any(final_diff.deleted.values()):
        raise ValueError("frontend pass deleted protected append-only Project state")
    frontend_diff = diff_project_write_sets(operations.after_write_set, final_snapshot)
    if any(frontend_diff.deleted.values()):
        raise ValueError("frontend phase deleted protected Project state")

    residuals = _observe_residual_candidates(
        session,
        operations.residual_candidate_ids,
        operations.fresh_run_ids,
        frontend_diff,
    )
    decision_changes = _observe_work_decision_changes(
        session, operations.project_id, frontend_diff
    )
    correction = _observe_factual_correction(
        session,
        operations.project_id,
        operations.fresh_run_ids,
        frontend_diff,
    )
    extraction_run_receipts = _observe_extraction_run_receipts(
        session, operations.fresh_run_ids
    )
    report_release = _observe_report_release(
        session, operations.project_id, frontend_diff
    )
    frontend_requests = _observe_frontend_requests(
        session,
        frontend_diff=frontend_diff,
        residuals=residuals,
        decision_changes=decision_changes,
        correction=correction,
        invalid_action=invalid_action,
        report_release=report_release,
    )
    _require_screenshot_hash_labels(
        metadata.screenshot_sha256,
        _required_screenshot_bindings(
            frontend_requests=frontend_requests,
            residuals=residuals,
            invalid_action=invalid_action,
            report_release=report_release,
        ),
    )
    _require_practitioner_work_precedes_report(
        session,
        residuals=residuals,
        decision_changes=decision_changes,
        correction=correction,
        invalid_action=invalid_action,
        frontend_requests=frontend_requests,
        report_release=report_release,
    )

    outcomes = {item.candidate_id: item.outcome for item in residuals}
    frontend_actions = tuple(
        [f"review_candidate:{item.candidate_id}" for item in residuals]
        + [f"{item.route_action}:{item.candidate_id}" for item in residuals]
        + ["refuse_invalid_action"]
        + [
            f"change_work_decision:{item.successor_decision_id}"
            for item in decision_changes
        ]
        + (
            [f"correct_statement_facts:{correction.successor_statement_event_id}"]
            if correction.outcome == "preserved_predecessor"
            else []
        )
        + [
            f"review_report:{report_release.artifact_id}",
            f"release_approved_export:{report_release.release_id}",
        ]
    )
    write_set = _complete_write_set(
        operations.before_write_set, final_snapshot, final_diff
    )
    proving_pass = ProductProvingPass(
        pass_number=metadata.pass_number,
        restored_baseline_fingerprint=(
            operations_capture.observed.baseline_fingerprint
        ),
        extraction_comparisons=operations.extraction_comparisons,
        extraction_run_receipts=extraction_run_receipts,
        extraction_failures=operations.extraction_failures,
        admission_completed=operations.admission_started,
        residual_candidate_ids=tuple(item.candidate_id for item in residuals),
        residual_outcomes=outcomes,
        frontend_kind=FRONTEND_EVIDENCE_KIND,
        frontend_actions=frontend_actions,
        invalid_action_refused=invalid_action.refused,
        invalid_action_write_set=invalid_action.write_set.as_write_set(),
        factual_correction_outcome=correction.outcome,
        work_decision_change_preserved_predecessor=bool(decision_changes),
        report_pdf_sha256=report_release.pdf_sha256,
        approved_export_sha256=report_release.pdf_sha256,
        approved_export_bytes=report_release.pdf_bytes,
        report_provenance_classes=report_release.provenance_classes,
        write_set=write_set,
        operations_elapsed_seconds=metadata.operations_elapsed_seconds,
        practitioner_elapsed_seconds=metadata.practitioner_elapsed_seconds,
        non_blocking_friction=metadata.non_blocking_friction,
        # This capture mode has no caller-controlled terminal-workaround flag.
        # A workflow that needed one must not invoke the successful seam.
        workarounds=(),
    )
    return ObservedFrontendPass(
        operations_capture=operations_capture,
        product_proving_pass=proving_pass,
        residual_candidates=residuals,
        invalid_action=invalid_action,
        work_decision_changes=decision_changes,
        factual_correction=correction,
        report_release=report_release,
        frontend_requests=frontend_requests,
        screenshot_sha256=dict(metadata.screenshot_sha256),
        final_write_set_snapshot=final_snapshot,
        terminal_database_fingerprint=terminal_fingerprint,
    )


def publish_frontend_pass_bundle(
    output_dir: Path | str,
    observation: ObservedFrontendPass,
    *,
    screenshot_evidence: Mapping[str, bytes | Path],
) -> FrontendPassBundleSummary:
    """Seal one typed live observation before its database is restored."""

    _validate_observed_frontend_consistency(observation)
    screenshots = _screenshot_evidence_json(observation, screenshot_evidence)
    canonical = _frontend_pass_canonical(observation)
    if _contains_key(canonical, "approved_export_bytes"):
        raise ValueError("canonical frontend pass must not embed Approved Export bytes")
    manifest_path, manifest_sha256, canonical_sha256 = publish_verified_bundle(
        Path(output_dir),
        exports={
            "canonical-content.json": canonical,
            "receipt.json": canonical,
            "approved-export.pdf": (
                observation.product_proving_pass.approved_export_bytes
            ),
            "screenshot-evidence.json": screenshots,
        },
        canonical_content=canonical,
        bundle_schema_version=FRONTEND_PASS_BUNDLE_SCHEMA_VERSION,
        bundle_files=FRONTEND_PASS_BUNDLE_FILES,
        error_cls=ValueError,
        corrupt_bundle_error_cls=CorruptFrontendPassBundle,
        canonical_json=_canonical_json,
        sha256=_sha256_bytes,
        json_sha256=_json_sha256,
        temp_prefix="corridor-product-proving-frontend-pass",
        self_verification_failure=(
            "new Product Proving frontend-pass bundle failed self-verification"
        ),
    )
    verified = verify_frontend_pass_bundle(
        output_dir,
        expected_integrity_manifest_sha256=manifest_sha256,
    )
    if verified.product_proving_pass != observation.product_proving_pass:
        raise ValueError("self-verified frontend pass changed its typed receipt")
    return FrontendPassBundleSummary(
        bundle_dir=Path(output_dir),
        manifest_path=manifest_path,
        integrity_manifest_sha256=manifest_sha256,
        canonical_content_sha256=canonical_sha256,
    )


def verify_frontend_pass_bundle(
    bundle_dir: Path | str,
    *,
    expected_integrity_manifest_sha256: str,
) -> VerifiedFrontendPass:
    """Reconstruct and verify a pass without PostgreSQL or the checkout."""

    root = Path(bundle_dir)
    verified = verify_bundle(
        root,
        expected_integrity_manifest_sha256=expected_integrity_manifest_sha256,
        bundle_schema_version=FRONTEND_PASS_BUNDLE_SCHEMA_VERSION,
        bundle_files=FRONTEND_PASS_BUNDLE_FILES,
        corrupt_bundle_error_cls=CorruptFrontendPassBundle,
        sha256=_sha256_bytes,
        json_sha256=_json_sha256,
    )
    try:
        canonical = json.loads((root / "canonical-content.json").read_bytes())
        receipt = json.loads((root / "receipt.json").read_bytes())
        screenshot_payload = json.loads(
            (root / "screenshot-evidence.json").read_bytes()
        )
        pdf_bytes = (root / "approved-export.pdf").read_bytes()
    except (OSError, json.JSONDecodeError) as exc:
        raise CorruptFrontendPassBundle(
            "frontend-pass canonical evidence is unreadable"
        ) from exc
    if receipt != canonical:
        raise CorruptFrontendPassBundle(
            "frontend-pass receipt does not match canonical content"
        )
    if canonical.get("schema_version") != FRONTEND_PASS_BUNDLE_SCHEMA_VERSION:
        raise CorruptFrontendPassBundle("frontend-pass canonical schema is unsupported")
    if _contains_key(canonical, "approved_export_bytes"):
        raise CorruptFrontendPassBundle("canonical receipt embeds PDF bytes")

    try:
        expected = _expected_preflight_from_json(
            canonical["operations_capture"]["expected"]
        )
        observed = _observed_preflight_from_json(
            canonical["operations_capture"]["observed"]
        )
        proving_pass = _product_proving_pass_from_json(
            canonical["observed_frontend_pass"]["product_proving_pass"],
            pdf_bytes,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CorruptFrontendPassBundle(
            "frontend-pass typed receipt cannot be reconstructed"
        ) from exc
    try:
        verify_preflight(expected, observed)
    except ValueError as exc:
        raise CorruptFrontendPassBundle(
            "frontend-pass preflight pins are inconsistent"
        ) from exc
    baseline = canonical["operations_capture"].get("database_baseline") or {}
    baseline_manifest = baseline.get("manifest_sha256")
    baseline_dump = baseline.get("dump_sha256")
    baseline_state = baseline.get("state_sha256")
    baseline_fingerprint_raw = baseline.get("fingerprint")
    baseline_source = baseline.get("source_identity")
    baseline_connection = baseline.get("source_connection_identity")
    try:
        baseline_fingerprint = parse_database_fingerprint(baseline_fingerprint_raw)
        parsed_connection = parse_database_connection_identity(baseline_connection)
    except ValueError as exc:
        raise CorruptFrontendPassBundle(
            "frontend-pass database baseline identity is malformed"
        ) from exc
    if (
        any(
            not isinstance(value, str) or _SHA256.fullmatch(value) is None
            for value in (baseline_manifest, baseline_dump, baseline_state)
        )
        or baseline_state != expected.baseline_fingerprint
        or baseline_fingerprint.state_sha256 != baseline_state
        or not isinstance(baseline_source, dict)
        or not baseline_source.get("database")
        or parsed_connection.database != baseline_source.get("database")
        or parsed_connection.username != baseline_source.get("username")
    ):
        raise CorruptFrontendPassBundle(
            "frontend-pass database baseline identities are invalid"
        )
    execution = canonical["operations_capture"].get("execution")
    try:
        execution_values = _validated_serialized_execution_identity(execution)
    except ValueError as exc:
        raise CorruptFrontendPassBundle(
            "frontend-pass execution identity is invalid"
        ) from exc
    if execution_values["pass_number"] != proving_pass.pass_number:
        raise CorruptFrontendPassBundle(
            "frontend-pass execution number disagrees with its receipt"
        )

    try:
        _verify_reconstructed_frontend_links(canonical, proving_pass)
        screenshot_sha256 = _verify_screenshot_evidence(
            screenshot_payload,
            canonical["observed_frontend_pass"].get("screenshot_sha256"),
            _serialized_required_screenshot_bindings(
                canonical["observed_frontend_pass"]
            ),
        )
        if (
            not pdf_bytes.startswith(b"%PDF-")
            or _sha256_bytes(pdf_bytes) != proving_pass.approved_export_sha256
        ):
            raise CorruptFrontendPassBundle(
                "frontend-pass Approved Export does not match its digest"
            )
        _verify_one_pass_contract(expected, observed, proving_pass)
        terminal = canonical["observed_frontend_pass"].get(
            "terminal_database_fingerprint"
        )
        terminal_fingerprint = parse_database_fingerprint(terminal)
        terminal_state = terminal_fingerprint.state_sha256
        if (
            terminal_state == baseline_state
            or terminal_fingerprint.schema_sha256 != baseline_fingerprint.schema_sha256
            or terminal_fingerprint.schema_objects
            != baseline_fingerprint.schema_objects
        ):
            raise CorruptFrontendPassBundle("terminal database fingerprint is invalid")
    except CorruptFrontendPassBundle:
        raise
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise CorruptFrontendPassBundle(
            "frontend-pass typed evidence is internally inconsistent"
        ) from exc
    return VerifiedFrontendPass(
        valid=verified.valid,
        bundle_dir=root,
        integrity_manifest_sha256=verified.integrity_manifest_sha256,
        canonical_content_sha256=verified.canonical_content_sha256,
        product_proving_pass=proving_pass,
        expected=expected,
        observed=observed,
        database_baseline_manifest_sha256=str(baseline_manifest),
        database_baseline_dump_sha256=str(baseline_dump),
        database_baseline_state_sha256=str(baseline_state),
        database_baseline_schema_sha256=baseline_fingerprint.schema_sha256,
        database_source_identity=dict(baseline_source),
        database_source_connection_identity=dict(baseline_connection),
        screenshot_sha256=screenshot_sha256,
        terminal_database_state_sha256=str(terminal_state),
        pass_number=int(execution_values["pass_number"]),
        execution_id=str(execution_values["execution_id"]),
        started_at=str(execution_values["started_at"]),
        prior_restore_operation_id=execution_values["prior_restore_operation_id"],
        prior_restore_bundle_manifest_sha256=execution_values[
            "prior_restore_bundle_manifest_sha256"
        ],
        prior_restore_bundle_canonical_sha256=execution_values[
            "prior_restore_bundle_canonical_sha256"
        ],
    )


def _validate_observed_frontend_consistency(
    observation: ObservedFrontendPass,
) -> None:
    capture = observation.operations_capture
    verify_preflight(capture.expected, capture.observed)
    _validate_database_baseline_identity(capture)
    _validate_execution_identity(capture)
    proving_pass = observation.product_proving_pass
    if proving_pass.pass_number != capture.pass_number:
        raise ValueError("typed frontend pass number disagrees with its execution")
    _verify_one_pass_contract(capture.expected, capture.observed, proving_pass)
    if tuple(item.candidate_id for item in observation.residual_candidates) != (
        proving_pass.residual_candidate_ids
    ) or {
        item.candidate_id: item.outcome for item in observation.residual_candidates
    } != dict(proving_pass.residual_outcomes):
        raise ValueError("typed residual observations disagree with the pass receipt")
    if (
        observation.invalid_action.refused != proving_pass.invalid_action_refused
        or observation.invalid_action.write_set.as_write_set()
        != proving_pass.invalid_action_write_set
    ):
        raise ValueError("typed invalid-action observation disagrees with the receipt")
    if bool(observation.work_decision_changes) != (
        proving_pass.work_decision_change_preserved_predecessor
    ):
        raise ValueError("typed Work Decision observation disagrees with the receipt")
    if observation.factual_correction.outcome != (
        proving_pass.factual_correction_outcome
    ):
        raise ValueError(
            "typed factual-correction observation disagrees with the receipt"
        )
    report = observation.report_release
    if (
        report.pdf_sha256 != proving_pass.approved_export_sha256
        or report.pdf_bytes != proving_pass.approved_export_bytes
        or report.provenance_classes != proving_pass.report_provenance_classes
    ):
        raise ValueError("typed Report release observation disagrees with the receipt")
    if not observation.screenshot_sha256 or any(
        not isinstance(label, str)
        or not label
        or not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
        for label, digest in observation.screenshot_sha256.items()
    ):
        raise ValueError("typed frontend screenshots are absent")
    if not observation.frontend_requests:
        raise ValueError("typed server-observed frontend requests are absent")
    _require_screenshot_hash_labels(
        observation.screenshot_sha256,
        _required_screenshot_bindings(
            frontend_requests=observation.frontend_requests,
            residuals=observation.residual_candidates,
            invalid_action=observation.invalid_action,
            report_release=observation.report_release,
        ),
    )


def _verify_one_pass_contract(
    expected: ExpectedPreflight,
    observed: ObservedPreflight,
    proving_pass: ProductProvingPass,
) -> None:
    verify_preflight(expected, observed)
    verify_product_proving_pass(expected, proving_pass)


def _frontend_pass_canonical(observation: ObservedFrontendPass) -> dict[str, Any]:
    capture = observation.operations_capture
    operations = capture.operations
    report = asdict(observation.report_release)
    report.pop("pdf_bytes")
    return {
        "schema_version": FRONTEND_PASS_BUNDLE_SCHEMA_VERSION,
        "operations_capture": {
            "expected": asdict(capture.expected),
            "observed": asdict(capture.observed),
            "execution": {
                "pass_number": capture.pass_number,
                "execution_id": capture.execution_id,
                "started_at": capture.started_at,
                "prior_restore_operation_id": capture.prior_restore_operation_id,
                "prior_restore_bundle_manifest_sha256": (
                    capture.prior_restore_bundle_manifest_sha256
                ),
                "prior_restore_bundle_canonical_sha256": (
                    capture.prior_restore_bundle_canonical_sha256
                ),
            },
            "database_baseline": {
                "manifest_sha256": capture.database_baseline_manifest_sha256,
                "dump_sha256": capture.database_baseline_dump_sha256,
                "state_sha256": capture.database_baseline_state_sha256,
                "fingerprint": capture.database_baseline_fingerprint.as_dict(),
                "source_identity": dict(capture.database_source_identity),
                "source_connection_identity": dict(
                    capture.database_source_connection_identity
                ),
            },
            "operations": {
                "project_id": operations.project_id,
                "fresh_run_ids": dict(operations.fresh_run_ids),
                "extraction_comparisons": [
                    asdict(item) for item in operations.extraction_comparisons
                ],
                "extraction_failures": list(operations.extraction_failures),
                "active_run_document_ids": list(operations.active_run_document_ids),
                "admission_started": operations.admission_started,
                "observed_active_runs": dict(operations.observed_active_runs),
                "admission_policy_receipts": [
                    asdict(item) for item in operations.admission_policy_receipts
                ],
                "residual_candidate_ids": list(operations.residual_candidate_ids),
                "before_write_set": asdict(operations.before_write_set),
                "after_write_set": asdict(operations.after_write_set),
                "write_set": asdict(operations.write_set),
            },
        },
        "observed_frontend_pass": {
            "product_proving_pass": _product_proving_pass_json(
                observation.product_proving_pass
            ),
            "residual_candidates": [
                asdict(item) for item in observation.residual_candidates
            ],
            "invalid_action": asdict(observation.invalid_action),
            "work_decision_changes": [
                asdict(item) for item in observation.work_decision_changes
            ],
            "factual_correction": asdict(observation.factual_correction),
            "report_release": report,
            "frontend_requests": [
                asdict(item) for item in observation.frontend_requests
            ],
            "screenshot_sha256": dict(sorted(observation.screenshot_sha256.items())),
            "final_write_set_snapshot": asdict(observation.final_write_set_snapshot),
            "terminal_database_fingerprint": (
                observation.terminal_database_fingerprint.as_dict()
            ),
        },
    }


def _product_proving_pass_json(proving_pass: ProductProvingPass) -> dict[str, Any]:
    value = asdict(proving_pass)
    value.pop("approved_export_bytes")
    return value


def _screenshot_evidence_json(
    observation: ObservedFrontendPass,
    evidence: Mapping[str, bytes | Path],
) -> dict[str, Any]:
    expected = dict(observation.screenshot_sha256)
    bindings = _required_screenshot_bindings(
        frontend_requests=observation.frontend_requests,
        residuals=observation.residual_candidates,
        invalid_action=observation.invalid_action,
        report_release=observation.report_release,
    )
    _require_screenshot_hash_labels(expected, bindings)
    if set(evidence) != set(expected):
        raise ValueError("screenshot evidence does not equal the retained hash set")
    entries = []
    for label in sorted(expected):
        source = evidence[label]
        if isinstance(source, Path):
            if source.is_symlink() or not source.is_file():
                raise ValueError(f"screenshot evidence {label!r} is not a regular file")
            value = source.read_bytes()
        elif isinstance(source, bytes):
            value = source
        else:
            raise ValueError(f"screenshot evidence {label!r} must be bytes or a Path")
        digest = _sha256_bytes(value)
        dimensions = _sensible_png_dimensions(value)
        if dimensions is None:
            raise ValueError(
                f"screenshot evidence {label!r} is not a sensible browser PNG"
            )
        if digest != expected[label]:
            raise ValueError(f"screenshot evidence {label!r} does not match its digest")
        width, height = dimensions
        entries.append(
            {
                "label": label,
                "sha256": digest,
                "width": width,
                "height": height,
                "binding": bindings[label],
                "bytes_base64": base64.b64encode(value).decode("ascii"),
            }
        )
    return {
        "schema_version": FRONTEND_PASS_BUNDLE_SCHEMA_VERSION,
        "screenshots": entries,
    }


def _verify_screenshot_evidence(
    payload: object,
    expected_metadata: object,
    expected_bindings: Mapping[str, Mapping[str, object]],
) -> Mapping[str, str]:
    if not isinstance(payload, dict) or (
        payload.get("schema_version") != FRONTEND_PASS_BUNDLE_SCHEMA_VERSION
    ):
        raise CorruptFrontendPassBundle("screenshot evidence schema is invalid")
    if not isinstance(expected_metadata, dict):
        raise CorruptFrontendPassBundle("screenshot hash metadata is invalid")
    screenshots = payload.get("screenshots")
    if not isinstance(screenshots, list):
        raise CorruptFrontendPassBundle("screenshot evidence list is invalid")
    observed: dict[str, str] = {}
    for entry in screenshots:
        if not isinstance(entry, dict) or set(entry) != {
            "label",
            "sha256",
            "width",
            "height",
            "binding",
            "bytes_base64",
        }:
            raise CorruptFrontendPassBundle("screenshot evidence entry is invalid")
        label = entry.get("label")
        digest = entry.get("sha256")
        width = entry.get("width")
        height = entry.get("height")
        binding = entry.get("binding")
        encoded = entry.get("bytes_base64")
        if (
            not isinstance(label, str)
            or not label
            or label in observed
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
            or not isinstance(width, int)
            or not isinstance(height, int)
            or not isinstance(binding, dict)
            or not isinstance(encoded, str)
        ):
            raise CorruptFrontendPassBundle("screenshot evidence identity is invalid")
        try:
            value = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise CorruptFrontendPassBundle(
                "screenshot evidence bytes are invalid"
            ) from exc
        if _sha256_bytes(value) != digest:
            raise CorruptFrontendPassBundle(
                f"screenshot evidence {label!r} does not match its digest"
            )
        if _sensible_png_dimensions(value) != (width, height):
            raise CorruptFrontendPassBundle(
                f"screenshot evidence {label!r} has invalid browser dimensions"
            )
        if binding != expected_bindings.get(label):
            raise CorruptFrontendPassBundle(
                f"screenshot evidence {label!r} has the wrong frontend binding"
            )
        observed[label] = digest
    normalized_expected = {
        str(label): str(digest) for label, digest in expected_metadata.items()
    }
    if observed != normalized_expected:
        raise CorruptFrontendPassBundle(
            "screenshot evidence does not match canonical metadata"
        )
    return dict(sorted(observed.items()))


def _sensible_png_dimensions(value: bytes) -> tuple[int, int] | None:
    if not value.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    if len(value) < 24 or value[12:16] != b"IHDR":
        return None
    width = int.from_bytes(value[16:20], "big")
    height = int.from_bytes(value[20:24], "big")
    if (
        width < _MIN_SCREENSHOT_WIDTH
        or height < _MIN_SCREENSHOT_HEIGHT
        or width > _MAX_SCREENSHOT_DIMENSION
        or height > _MAX_SCREENSHOT_DIMENSION
        or not 0.5 <= width / height <= 4.0
    ):
        return None
    try:
        with pymupdf.open(stream=value, filetype="png") as image:
            if image.page_count != 1:
                return None
    except Exception:
        return None
    return width, height


def _required_screenshot_bindings(
    *,
    frontend_requests: tuple[FrontendRequestObservation, ...],
    residuals: tuple[ResidualCandidateObservation, ...] | tuple[object, ...],
    invalid_action: RefusedInvalidActionObservation | object,
    report_release: ReportReleaseObservation | object,
) -> dict[str, dict[str, object]]:
    def request(
        route_name: str,
        *,
        method: str,
        status: int,
        subject_key: str | None = None,
        subject_id: int | None = None,
        audit_log_id: int | None = None,
    ) -> FrontendRequestObservation:
        matches = [
            item
            for item in frontend_requests
            if item.route_name == route_name
            and item.method == method
            and item.status == status
            and (audit_log_id is None or item.audit_log_id == audit_log_id)
            and (subject_key is None or item.subject.get(subject_key) == subject_id)
        ]
        if not matches:
            raise ValueError(
                f"required screenshot has no exact frontend route {route_name}"
            )
        return matches[-1]

    def binding(item: FrontendRequestObservation) -> dict[str, object]:
        return {
            "audit_log_id": item.audit_log_id,
            "route_name": item.route_name,
            "route_template": item.route_template,
            "method": item.method,
            "status": item.status,
            "principal": item.principal,
            "request_fields_sha256": item.request_fields_sha256,
            "subject": dict(item.subject),
        }

    work_list = request("coordinator_home", method="GET", status=200)
    invalid = request(
        "correct_statement_scope_from_screen",
        method="POST",
        status=400,
        audit_log_id=getattr(invalid_action, "frontend_request_audit_id"),
    )
    preview = request(
        "preview_prepared_report",
        method="GET",
        status=200,
        subject_key="artifact_id",
        subject_id=getattr(report_release, "artifact_id"),
    )
    release = request(
        "release_prepared_report",
        method="POST",
        status=201,
        subject_key="release_id",
        subject_id=getattr(report_release, "release_id"),
    )
    result = {
        "work-list": binding(work_list),
        "invalid-action-refusal": binding(invalid),
        "report-preview": binding(preview),
        "approved-export-release": binding(release),
    }
    for residual in residuals:
        candidate_id = getattr(residual, "candidate_id")
        route = (
            "queue"
            if getattr(residual, "candidate_kind") == "dependency"
            else "coordinate_statement_screen"
        )
        result[f"candidate-{candidate_id}-review"] = binding(
            request(
                route,
                method="GET",
                status=200,
                subject_key="candidate_id",
                subject_id=candidate_id,
            )
        )
    return dict(sorted(result.items()))


def _require_screenshot_hash_labels(
    screenshot_sha256: Mapping[str, str],
    bindings: Mapping[str, Mapping[str, object]],
) -> None:
    if set(screenshot_sha256) != set(bindings):
        raise ValueError(
            "frontend screenshot labels do not equal the required route evidence"
        )
    if any(
        not isinstance(digest, str) or _SHA256.fullmatch(digest) is None
        for digest in screenshot_sha256.values()
    ):
        raise ValueError("frontend screenshot hash metadata is invalid")


def _serialized_required_screenshot_bindings(
    observed: Mapping[str, object],
) -> dict[str, dict[str, object]]:
    raw_requests = observed.get("frontend_requests")
    raw_residuals = observed.get("residual_candidates")
    invalid = observed.get("invalid_action")
    report = observed.get("report_release")
    if (
        not isinstance(raw_requests, list)
        or not isinstance(raw_residuals, list)
        or not isinstance(invalid, dict)
        or not isinstance(report, dict)
    ):
        raise CorruptFrontendPassBundle(
            "canonical screenshot route evidence is invalid"
        )
    try:
        requests = tuple(
            FrontendRequestObservation(
                audit_log_id=_positive_int(item["audit_log_id"]),
                route_name=str(item["route_name"]),
                route_template=str(item["route_template"]),
                method=str(item["method"]),
                status=int(item["status"]),
                principal=str(item["principal"]),
                request_fields_sha256=str(item["request_fields_sha256"]),
                subject={
                    str(key): _positive_int(value)
                    for key, value in dict(item["subject"]).items()
                },
            )
            for item in raw_requests
            if isinstance(item, dict)
        )
        residuals = tuple(
            SimpleScreenshotResidual(
                candidate_id=_positive_int(item["candidate_id"]),
                candidate_kind=str(item["candidate_kind"]),
            )
            for item in raw_residuals
            if isinstance(item, dict)
        )
        return _required_screenshot_bindings(
            frontend_requests=requests,
            residuals=residuals,
            invalid_action=SimpleScreenshotInvalid(
                frontend_request_audit_id=_positive_int(
                    invalid["frontend_request_audit_id"]
                )
            ),
            report_release=SimpleScreenshotReport(
                artifact_id=_positive_int(report["artifact_id"]),
                release_id=_positive_int(report["release_id"]),
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CorruptFrontendPassBundle(
            "canonical screenshot bindings cannot be reconstructed"
        ) from exc


@dataclass(frozen=True)
class SimpleScreenshotResidual:
    candidate_id: int
    candidate_kind: str


@dataclass(frozen=True)
class SimpleScreenshotInvalid:
    frontend_request_audit_id: int


@dataclass(frozen=True)
class SimpleScreenshotReport:
    artifact_id: int
    release_id: int


def _expected_preflight_from_json(value: object) -> ExpectedPreflight:
    if not isinstance(value, dict) or set(value) != {
        "source_revision",
        "origin_main_revision",
        "migration_head",
        "policy_digests",
        "documents",
        "baseline_runs",
        "milestone_sources",
        "baseline_fingerprint",
    }:
        raise ValueError("Expected Preflight receipt is invalid")
    return ExpectedPreflight(
        source_revision=str(value["source_revision"]),
        origin_main_revision=str(value["origin_main_revision"]),
        migration_head=str(value["migration_head"]),
        policy_digests=_string_mapping(value["policy_digests"]),
        documents=_integer_key_mapping(value["documents"]),
        baseline_runs=_integer_key_mapping(value["baseline_runs"]),
        milestone_sources=_string_mapping(value["milestone_sources"]),
        baseline_fingerprint=str(value["baseline_fingerprint"]),
    )


def _observed_preflight_from_json(value: object) -> ObservedPreflight:
    if not isinstance(value, dict) or set(value) != {
        "source_revision",
        "origin_main_revision",
        "clean_worktree",
        "migration_head",
        "policy_digests",
        "documents",
        "baseline_runs",
        "milestone_sources",
        "baseline_fingerprint",
    }:
        raise ValueError("Observed Preflight receipt is invalid")
    if not isinstance(value["clean_worktree"], bool):
        raise ValueError("Observed Preflight worktree flag is invalid")
    return ObservedPreflight(
        source_revision=str(value["source_revision"]),
        origin_main_revision=str(value["origin_main_revision"]),
        clean_worktree=value["clean_worktree"],
        migration_head=str(value["migration_head"]),
        policy_digests=_string_mapping(value["policy_digests"]),
        documents=_integer_key_mapping(value["documents"]),
        baseline_runs=_integer_key_mapping(value["baseline_runs"]),
        milestone_sources=_string_mapping(value["milestone_sources"]),
        baseline_fingerprint=str(value["baseline_fingerprint"]),
    )


def _product_proving_pass_from_json(
    value: object,
    pdf_bytes: bytes,
) -> ProductProvingPass:
    if not isinstance(value, dict):
        raise ValueError("Product Proving pass receipt is invalid")
    expected_fields = {item.name for item in fields(ProductProvingPass)} - {
        "approved_export_bytes"
    }
    if set(value) != expected_fields:
        raise ValueError("Product Proving pass fields are incomplete")
    comparisons = tuple(
        _candidate_comparison_from_json(item)
        for item in _required_list(value["extraction_comparisons"])
    )
    outcomes_raw = value["residual_outcomes"]
    if not isinstance(outcomes_raw, dict):
        raise ValueError("residual outcome receipt is invalid")
    outcomes = {int(key): str(item) for key, item in outcomes_raw.items()}
    write_set = value["write_set"]
    if not isinstance(write_set, dict):
        raise ValueError("Product Proving write set is invalid")
    invalid_write_set = value["invalid_action_write_set"]
    if not isinstance(invalid_write_set, dict):
        raise ValueError("invalid-action write set is invalid")
    return ProductProvingPass(
        pass_number=_positive_int(value["pass_number"]),
        restored_baseline_fingerprint=str(value["restored_baseline_fingerprint"]),
        extraction_comparisons=comparisons,
        extraction_run_receipts=tuple(
            dict(item)
            for item in _required_list(value["extraction_run_receipts"])
            if isinstance(item, dict)
        ),
        extraction_failures=tuple(
            str(item) for item in _required_list(value["extraction_failures"])
        ),
        admission_completed=_required_bool(value["admission_completed"]),
        residual_candidate_ids=tuple(
            _positive_int(item)
            for item in _required_list(value["residual_candidate_ids"])
        ),
        residual_outcomes=outcomes,
        frontend_kind=str(value["frontend_kind"]),
        frontend_actions=tuple(
            str(item) for item in _required_list(value["frontend_actions"])
        ),
        invalid_action_refused=_required_bool(value["invalid_action_refused"]),
        invalid_action_write_set=dict(invalid_write_set),
        factual_correction_outcome=str(value["factual_correction_outcome"]),
        work_decision_change_preserved_predecessor=_required_bool(
            value["work_decision_change_preserved_predecessor"]
        ),
        report_pdf_sha256=str(value["report_pdf_sha256"]),
        approved_export_sha256=str(value["approved_export_sha256"]),
        approved_export_bytes=pdf_bytes,
        report_provenance_classes=tuple(
            str(item) for item in _required_list(value["report_provenance_classes"])
        ),
        write_set={
            str(key): list(changes)
            for key, changes in write_set.items()
            if isinstance(changes, list)
        },
        operations_elapsed_seconds=_nonnegative_number(
            value["operations_elapsed_seconds"]
        ),
        practitioner_elapsed_seconds=_nonnegative_number(
            value["practitioner_elapsed_seconds"]
        ),
        non_blocking_friction=tuple(
            str(item) for item in _required_list(value["non_blocking_friction"])
        ),
        workarounds=tuple(str(item) for item in _required_list(value["workarounds"])),
    )


def _candidate_comparison_from_json(value: object) -> CandidateSetComparison:
    if not isinstance(value, dict) or set(value) != {
        "document_id",
        "baseline_run_id",
        "fresh_run_id",
        "baseline_configuration",
        "fresh_configuration",
        "added",
        "missing",
        "matched_sha256",
    }:
        raise ValueError("Candidate comparison receipt is invalid")
    return CandidateSetComparison(
        document_id=_positive_int(value["document_id"]),
        baseline_run_id=_positive_int(value["baseline_run_id"]),
        fresh_run_id=_positive_int(value["fresh_run_id"]),
        baseline_configuration=_extraction_configuration_from_json(
            value["baseline_configuration"]
        ),
        fresh_configuration=_extraction_configuration_from_json(
            value["fresh_configuration"]
        ),
        added=tuple(
            dict(item)
            for item in _required_list(value["added"])
            if isinstance(item, dict)
        ),
        missing=tuple(
            dict(item)
            for item in _required_list(value["missing"])
            if isinstance(item, dict)
        ),
        matched_sha256=tuple(
            str(item) for item in _required_list(value["matched_sha256"])
        ),
    )


def _extraction_configuration_from_json(value: object) -> ExtractionConfiguration:
    if not isinstance(value, dict) or set(value) != {
        item.name for item in fields(ExtractionConfiguration)
    }:
        raise ValueError("Extraction Configuration receipt is invalid")
    return ExtractionConfiguration(
        prompt_version=str(value["prompt_version"]),
        model=str(value["model"]) if value["model"] is not None else None,
        schema_version=str(value["schema_version"]),
        prompt_sha256=str(value["prompt_sha256"]),
        schema_sha256=str(value["schema_sha256"]),
        postprocessor_sha256=str(value["postprocessor_sha256"]),
        config_sha256=str(value["config_sha256"]),
    )


def _verify_reconstructed_frontend_links(
    canonical: Mapping[str, Any],
    proving_pass: ProductProvingPass,
) -> None:
    operations = canonical["operations_capture"].get("operations") or {}
    observed = canonical["observed_frontend_pass"]
    if tuple(operations.get("residual_candidate_ids") or ()) != (
        proving_pass.residual_candidate_ids
    ):
        raise CorruptFrontendPassBundle(
            "operations residue disagrees with the frontend pass"
        )
    if operations.get("admission_started") is not True or (
        operations.get("extraction_failures") or []
    ):
        raise CorruptFrontendPassBundle(
            "frontend pass does not follow successful operations"
        )
    residual_rows = observed.get("residual_candidates")
    if not isinstance(residual_rows, list):
        raise CorruptFrontendPassBundle("typed residual observations are invalid")
    residual_outcomes = {
        int(item["candidate_id"]): item["outcome"]
        for item in residual_rows
        if isinstance(item, dict) and "candidate_id" in item and "outcome" in item
    }
    if residual_outcomes != dict(proving_pass.residual_outcomes):
        raise CorruptFrontendPassBundle(
            "typed residual observations disagree with the receipt"
        )
    invalid = observed.get("invalid_action") or {}
    invalid_diff = invalid.get("write_set") or {}
    if invalid.get("refused") is not True or not _serialized_diff_empty(invalid_diff):
        raise CorruptFrontendPassBundle(
            "invalid action was not observed as an empty diff"
        )
    requests = observed.get("frontend_requests")
    if not isinstance(requests, list) or not requests:
        raise CorruptFrontendPassBundle("server-observed frontend requests are absent")
    request_fields = {item.name for item in fields(FrontendRequestObservation)}
    for item in requests:
        if (
            not isinstance(item, dict)
            or set(item) != request_fields
            or not isinstance(item.get("audit_log_id"), int)
            or item["audit_log_id"] <= 0
            or not isinstance(item.get("route_name"), str)
            or not isinstance(item.get("route_template"), str)
            or not item["route_template"].startswith("/")
            or item.get("method") not in {"GET", "POST"}
            or isinstance(item.get("status"), bool)
            or not isinstance(item.get("status"), int)
            or not isinstance(item.get("principal"), str)
            or not isinstance(item.get("request_fields_sha256"), str)
            or _SHA256.fullmatch(item["request_fields_sha256"]) is None
            or not isinstance(item.get("subject"), dict)
            or not item["subject"]
            or not set(item["subject"]) <= _FRONTEND_SUBJECT_KEYS
            or "project_id" not in item["subject"]
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
                for value in item["subject"].values()
            )
            or not _frontend_route_receipt_is_valid(
                route_name=item.get("route_name"),
                route_template=item.get("route_template"),
                method=item.get("method"),
                status=item.get("status"),
            )
        ):
            raise CorruptFrontendPassBundle(
                "server-observed frontend request is malformed"
            )
    route_names = {
        item.get("route_name") for item in requests if isinstance(item, dict)
    }
    request_ids = {
        item.get("audit_log_id") for item in requests if isinstance(item, dict)
    }
    request_principals = {
        item.get("principal") for item in requests if isinstance(item, dict)
    }
    request_projects = {
        (item.get("subject") or {}).get("project_id")
        for item in requests
        if isinstance(item, dict)
    }
    if (
        len(request_ids) != len(requests)
        or len(request_principals) != 1
        or request_projects != {operations.get("project_id")}
    ):
        raise CorruptFrontendPassBundle(
            "server-observed frontend request scope is inconsistent"
        )
    if (
        not {
            "coordinator_home",
            "correct_statement_scope_from_screen",
            "render_report",
            "review_report",
            "preview_prepared_report",
            "release_prepared_report",
        }
        <= route_names
    ):
        raise CorruptFrontendPassBundle("frontend request route coverage is incomplete")
    if invalid.get("frontend_request_audit_id") not in request_ids:
        raise CorruptFrontendPassBundle("invalid action frontend receipt is absent")
    changes = observed.get("work_decision_changes")
    if not isinstance(changes, list) or bool(changes) != (
        proving_pass.work_decision_change_preserved_predecessor
    ):
        raise CorruptFrontendPassBundle("Work Decision observation is invalid")
    correction = observed.get("factual_correction") or {}
    if correction.get("outcome") != proving_pass.factual_correction_outcome:
        raise CorruptFrontendPassBundle("factual-correction observation is invalid")
    if (
        correction.get("outcome") == "preserved_predecessor"
        and "correct_statement_facts_from_screen" not in route_names
    ):
        raise CorruptFrontendPassBundle("factual correction frontend request is absent")
    report = observed.get("report_release") or {}
    if (
        report.get("pdf_sha256") != proving_pass.approved_export_sha256
        or tuple(report.get("provenance_classes") or ())
        != proving_pass.report_provenance_classes
    ):
        raise CorruptFrontendPassBundle("Report release observation is invalid")


def _serialized_diff_empty(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return all(
        isinstance(group, dict)
        and all(isinstance(rows, list) and not rows for rows in group.values())
        for key, group in value.items()
        if key in {"created", "deleted", "updated"}
    ) and {"created", "deleted", "updated"} <= set(value)


def _integer_key_mapping(value: object) -> Mapping[int, Any]:
    if not isinstance(value, dict):
        raise ValueError("integer-key mapping is invalid")
    return {int(key): item for key, item in value.items()}


def _string_mapping(value: object) -> Mapping[str, str]:
    if not isinstance(value, dict) or any(
        not isinstance(item, str) for item in value.values()
    ):
        raise ValueError("string mapping is invalid")
    return {str(key): item for key, item in value.items()}


def _required_list(value: object) -> list:
    if not isinstance(value, list):
        raise ValueError("receipt sequence is invalid")
    return value


def _positive_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("receipt identity is invalid")
    return value


def _required_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("receipt boolean is invalid")
    return value


def _nonnegative_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError("receipt timing is invalid")
    return float(value)


def _contains_key(value: object, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_key(item, key) for item in value)
    return False


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def _sha256_bytes(value: bytes) -> str:
    return sha256(value).hexdigest()


def _json_sha256(value: Any) -> str:
    return _sha256_bytes(_canonical_json(value))


def _validate_database_baseline_identity(
    operations_capture: LiveProductProvingOperationsCapture,
) -> None:
    values = (
        operations_capture.database_baseline_manifest_sha256,
        operations_capture.database_baseline_dump_sha256,
        operations_capture.database_baseline_state_sha256,
    )
    if any(
        not isinstance(value, str) or _SHA256.fullmatch(value) is None
        for value in values
    ):
        raise ValueError("verified database baseline identities are invalid")
    if (
        operations_capture.database_baseline_state_sha256
        != operations_capture.expected.baseline_fingerprint
        or operations_capture.database_baseline_state_sha256
        != operations_capture.observed.baseline_fingerprint
    ):
        raise ValueError("database baseline state identity disagrees with preflight")
    baseline_fingerprint = parse_database_fingerprint(
        operations_capture.database_baseline_fingerprint.as_dict()
    )
    if (
        baseline_fingerprint != operations_capture.database_baseline_fingerprint
        or baseline_fingerprint.state_sha256
        != operations_capture.database_baseline_state_sha256
    ):
        raise ValueError("database baseline fingerprint identity is inconsistent")
    source = operations_capture.database_source_identity
    if not isinstance(source, Mapping) or not source.get("database"):
        raise ValueError("database baseline source identity is absent")
    try:
        connection = parse_database_connection_identity(
            operations_capture.database_source_connection_identity
        )
    except ValueError as exc:
        raise ValueError("database baseline connection identity is absent") from exc
    if connection.database != source.get(
        "database"
    ) or connection.username != source.get("username"):
        raise ValueError("database baseline connection identity disagrees with URL")


def _validate_execution_identity(
    operations_capture: LiveProductProvingOperationsCapture,
) -> None:
    _validated_serialized_execution_identity(
        {
            "pass_number": operations_capture.pass_number,
            "execution_id": operations_capture.execution_id,
            "started_at": operations_capture.started_at,
            "prior_restore_operation_id": (
                operations_capture.prior_restore_operation_id
            ),
            "prior_restore_bundle_manifest_sha256": (
                operations_capture.prior_restore_bundle_manifest_sha256
            ),
            "prior_restore_bundle_canonical_sha256": (
                operations_capture.prior_restore_bundle_canonical_sha256
            ),
        }
    )


def _validated_serialized_execution_identity(value: object) -> dict[str, Any]:
    required = {
        "pass_number",
        "execution_id",
        "started_at",
        "prior_restore_operation_id",
        "prior_restore_bundle_manifest_sha256",
        "prior_restore_bundle_canonical_sha256",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("Product Proving execution identity is incomplete")
    pass_number = value["pass_number"]
    if pass_number not in {1, 2} or isinstance(pass_number, bool):
        raise ValueError("Product Proving execution pass number is invalid")
    try:
        execution_id = str(UUID(str(value["execution_id"])))
        started_at = datetime.fromisoformat(str(value["started_at"]))
    except (TypeError, ValueError) as exc:
        raise ValueError("Product Proving execution identity is malformed") from exc
    if execution_id != value["execution_id"] or started_at.tzinfo is None:
        raise ValueError("Product Proving execution identity is not canonical")
    prior_operation = value["prior_restore_operation_id"]
    prior_manifest = value["prior_restore_bundle_manifest_sha256"]
    prior_canonical = value["prior_restore_bundle_canonical_sha256"]
    if pass_number == 1:
        if any(
            item is not None
            for item in (prior_operation, prior_manifest, prior_canonical)
        ):
            raise ValueError("Product Proving pass 1 cannot name a prior restore")
    else:
        try:
            parsed_prior = str(UUID(str(prior_operation)))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Product Proving pass 2 prior restore id is invalid"
            ) from exc
        if (
            parsed_prior != prior_operation
            or not isinstance(prior_manifest, str)
            or _SHA256.fullmatch(prior_manifest) is None
            or not isinstance(prior_canonical, str)
            or _SHA256.fullmatch(prior_canonical) is None
        ):
            raise ValueError("Product Proving pass 2 prior restore is invalid")
    return dict(value)


def _validated_metadata(metadata: FrontendPassMetadata) -> FrontendPassMetadata:
    if (
        isinstance(metadata.pass_number, bool)
        or not isinstance(metadata.pass_number, int)
        or metadata.pass_number <= 0
    ):
        raise ValueError("Product Proving pass number must be positive")
    for label, value in (
        ("operations", metadata.operations_elapsed_seconds),
        ("practitioner", metadata.practitioner_elapsed_seconds),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"Product Proving {label} elapsed time is invalid")
    if any(
        not isinstance(item, str) or not item.strip()
        for item in metadata.non_blocking_friction
    ):
        raise ValueError("non-blocking friction entries must be meaningful")
    screenshots = dict(metadata.screenshot_sha256)
    if not screenshots:
        raise ValueError("real frontend capture requires retained screenshot hashes")
    if any(
        not isinstance(label, str)
        or not label.strip()
        or not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
        for label, digest in screenshots.items()
    ):
        raise ValueError("frontend screenshot hash metadata is invalid")
    return FrontendPassMetadata(
        pass_number=metadata.pass_number,
        operations_elapsed_seconds=float(metadata.operations_elapsed_seconds),
        practitioner_elapsed_seconds=float(metadata.practitioner_elapsed_seconds),
        non_blocking_friction=tuple(metadata.non_blocking_friction),
        screenshot_sha256=dict(sorted(screenshots.items())),
    )


def _observe_residual_candidates(
    session: Session,
    residual_candidate_ids: tuple[int, ...],
    fresh_run_ids: Mapping[int, int],
    frontend_diff: ProjectWriteSetDiff,
) -> tuple[ResidualCandidateObservation, ...]:
    if len(set(residual_candidate_ids)) != len(residual_candidate_ids):
        raise ValueError("operations repeat a residual Candidate identity")
    residual_ids = set(residual_candidate_ids)
    candidates = tuple(
        session.scalars(
            select(Candidate)
            .where(Candidate.id.in_(residual_ids))
            .order_by(Candidate.id)
        ).all()
    )
    if {item.id for item in candidates} != residual_ids:
        raise ValueError("an operations residual Candidate no longer exists")
    if any(
        candidate.extraction_run_id != fresh_run_ids.get(candidate.source_document_id)
        for candidate in candidates
    ):
        raise ValueError("a residual Candidate is outside the exact fresh Runs")

    created_review_ids = _created_ids(
        frontend_diff, "evidence_investigation_candidate_review_starts"
    )
    starts = tuple(
        session.scalars(
            select(EvidenceInvestigationCandidateReviewStart).where(
                EvidenceInvestigationCandidateReviewStart.id.in_(created_review_ids)
            )
        ).all()
    )
    starts_by_candidate = _one_by(starts, "candidate_id", "review start")
    if set(starts_by_candidate) != residual_ids:
        raise ValueError("real frontend did not open every exact residual Candidate")

    created_disposition_ids = _created_ids(frontend_diff, "candidate_dispositions")
    dispositions = tuple(
        session.scalars(
            select(CandidateDisposition).where(
                CandidateDisposition.id.in_(created_disposition_ids)
            )
        ).all()
    )
    dispositions_by_candidate = _one_by(
        dispositions, "candidate_id", "Candidate disposition"
    )
    if set(dispositions_by_candidate) - residual_ids:
        raise ValueError("frontend disposed a Candidate outside the exact residue")

    created_audit_ids = _created_ids(frontend_diff, "audit_log")
    audit_rows = tuple(
        session.scalars(
            select(AuditLog).where(AuditLog.id.in_(created_audit_ids))
        ).all()
    )
    observations = []
    for candidate in candidates:
        start = starts_by_candidate[candidate.id]
        _require_attributable_principal(start.principal)
        disposition = dispositions_by_candidate.get(candidate.id)
        if candidate.state in {"accepted", "merged"}:
            action_receipt = _supported_candidate_audit(candidate, audit_rows)
            if action_receipt.human_principal != start.principal:
                raise ValueError(
                    f"Candidate {candidate.id} support is not attributable to its reviewer"
                )
            if action_receipt.action == audit.COORDINATE_STATEMENT:
                if disposition is None or disposition.disposition != "accepted":
                    raise ValueError(
                        f"Candidate {candidate.id} guided Save lacks its accepted disposition"
                    )
            elif disposition is not None:
                raise ValueError(
                    f"Candidate {candidate.id} has an unrelated disposition receipt"
                )
            if disposition is not None and disposition.recorded_by != start.principal:
                raise ValueError(
                    f"Candidate {candidate.id} disposition has another recorder"
                )
            if candidate.kind == "dependency" and candidate.state == "accepted":
                dependency = session.get(Dependency, action_receipt.entity_id)
                created_dependencies = _created_ids(frontend_diff, "dependencies")
                created_evidence = _created_ids(frontend_diff, "evidence_links")
                created_assertions = _created_ids(frontend_diff, "assertions")
                evidence_ids = set(
                    session.scalars(
                        select(EvidenceLink.id).where(
                            EvidenceLink.id.in_(created_evidence),
                            EvidenceLink.dependency_id == action_receipt.entity_id,
                        )
                    ).all()
                )
                assertion_ids = set(
                    session.scalars(
                        select(Assertion.id).where(
                            Assertion.id.in_(created_assertions),
                            Assertion.dependency_id == action_receipt.entity_id,
                        )
                    ).all()
                )
                if (
                    dependency is None
                    or dependency.id not in created_dependencies
                    or not evidence_ids
                    or not assertion_ids
                ):
                    raise ValueError(
                        f"Candidate {candidate.id} support is not its frontend-created Dependency"
                    )
            elif candidate.kind == "dependency" and (
                candidate.merged_into != action_receipt.entity_id
                or session.get(Dependency, action_receipt.entity_id) is None
            ):
                raise ValueError(
                    f"Candidate {candidate.id} merge is not linked to its Dependency"
                )
            outcome: ResidualOutcome = "supported"
        elif candidate.state == "rejected":
            if (
                disposition is None
                or disposition.disposition != "not_relevant"
                or not disposition.reason
                or disposition.recorded_by != start.principal
            ):
                raise ValueError(
                    f"Candidate {candidate.id} has no attributable Not Relevant disposition"
                )
            action_receipt = _single_audit(
                audit_rows,
                candidate_id=candidate.id,
                actions={audit.MARK_STATEMENT_NOT_RELEVANT},
            )
            after = action_receipt.after_json or {}
            if (
                action_receipt.human_principal != start.principal
                or after.get("candidate_disposition_id") != disposition.id
                or after.get("reason") != disposition.reason
                or after.get("confirmed") is not True
            ):
                raise ValueError(
                    f"Candidate {candidate.id} Not Relevant audit is not its disposition"
                )
            outcome = "not_relevant"
        elif candidate.state == "pending":
            if disposition is not None:
                raise ValueError(
                    f"Candidate {candidate.id} is pending with a new disposition"
                )
            action_receipt = _single_audit(
                audit_rows,
                candidate_id=candidate.id,
                actions={audit.KEEP_CANDIDATE_UNRESOLVED},
            )
            before = action_receipt.before_json or {}
            after = action_receipt.after_json or {}
            gap = pending_candidate_authority_gap(
                session, candidate.project_id, candidate.id
            )
            if gap is None:
                raise ValueError(
                    f"Candidate {candidate.id} no longer has a structured authority gap"
                )
            expected_after: dict[str, object] = {
                "candidate_state": "pending",
                "authority_gap": gap.code,
            }
            if gap.source_family == "dependency-admission":
                expected_after.update(
                    {
                        "source_family": gap.source_family,
                        "abstention_reason": gap.abstention_reason,
                        "dependency_admission_outcome_id": gap.outcome_id,
                        "policy_run_id": gap.policy_run_id,
                    }
                )
            elif gap.source_family == "external-party-statement":
                expected_after.update(
                    {
                        "affected_external_org_id": gap.affected_external_org_id,
                        "matching_open_commitment_lineage_ids": list(
                            gap.matching_commitment_lineage_ids
                        ),
                    }
                )
            if (
                action_receipt.human_principal != start.principal
                or before != {"candidate_state": "pending"}
                or after != expected_after
            ):
                raise ValueError(
                    f"Candidate {candidate.id} unresolved receipt has no exact authority gap"
                )
            outcome = "unresolved"
        else:
            raise ValueError(
                f"Candidate {candidate.id} has no honest terminal residual outcome"
            )
        observations.append(
            ResidualCandidateObservation(
                candidate_id=candidate.id,
                candidate_kind=candidate.kind,
                candidate_state=candidate.state,
                outcome=outcome,
                review_start_id=start.id,
                principal=start.principal,
                disposition_id=disposition.id if disposition is not None else None,
                audit_log_id=action_receipt.id,
                route_action=action_receipt.action,
            )
        )
    return tuple(observations)


def _supported_candidate_audit(
    candidate: Candidate, audit_rows: tuple[AuditLog, ...]
) -> AuditLog:
    allowed = (
        {audit.MERGE_CANDIDATE}
        if candidate.state == "merged"
        else set(_SUPPORTED_ACTIONS - {audit.MERGE_CANDIDATE})
    )
    return _single_audit(
        audit_rows,
        candidate_id=candidate.id,
        actions=allowed,
    )


def _single_audit(
    audit_rows: tuple[AuditLog, ...],
    *,
    candidate_id: int,
    actions: set[str],
) -> AuditLog:
    matches = []
    for entry in audit_rows:
        after = entry.after_json or {}
        names_candidate = (
            entry.entity_type == audit.CANDIDATE and entry.entity_id == candidate_id
        ) or after.get("candidate_id") == candidate_id
        if entry.action in actions and names_candidate:
            matches.append(entry)
    if len(matches) != 1:
        raise ValueError(
            f"Candidate {candidate_id} requires exactly one route-specific audit receipt"
        )
    return matches[0]


def _observe_work_decision_changes(
    session: Session,
    project_id: int,
    frontend_diff: ProjectWriteSetDiff,
) -> tuple[WorkDecisionChangeObservation, ...]:
    created_ids = _created_ids(frontend_diff, "work_decisions")
    created = tuple(
        session.scalars(
            select(WorkDecision).where(WorkDecision.id.in_(created_ids))
        ).all()
    )
    changes = tuple(
        sorted(
            (item for item in created if item.predecessor_decision_id is not None),
            key=lambda item: item.id,
        )
    )
    if not changes:
        raise ValueError("frontend pass did not append a Work Decision change")
    audit_rows = tuple(
        session.scalars(
            select(AuditLog).where(
                AuditLog.id.in_(_created_ids(frontend_diff, "audit_log"))
            )
        ).all()
    )
    observations = []
    for successor in changes:
        _require_attributable_principal(successor.recorded_by)
        predecessor = session.get(WorkDecision, successor.predecessor_decision_id)
        if (
            predecessor is None
            or predecessor.field != successor.field
            or predecessor.dependency_id != successor.dependency_id
            or predecessor.commitment_lineage_id != successor.commitment_lineage_id
            or successor.before_value != predecessor.after_value
        ):
            raise ValueError(
                f"Work Decision {successor.id} does not preserve its predecessor"
            )
        subject_id = successor.dependency_id or successor.commitment_lineage_id
        entity_type = (
            audit.DEPENDENCY
            if successor.dependency_id is not None
            else audit.COMMITMENT_LINEAGE
        )
        receipts = [
            entry
            for entry in audit_rows
            if entry.action in _WORK_DECISION_ACTIONS
            and entry.entity_type == entity_type
            and entry.entity_id == subject_id
            and (entry.after_json or {}).get("work_decision_id") == successor.id
            and entry.human_principal == successor.recorded_by
        ]
        if len(receipts) != 1:
            raise ValueError(
                f"Work Decision {successor.id} lacks its attributable route receipt"
            )
        observations.append(
            WorkDecisionChangeObservation(
                predecessor_decision_id=predecessor.id,
                successor_decision_id=successor.id,
                field=successor.field,
                dependency_id=successor.dependency_id,
                commitment_lineage_id=successor.commitment_lineage_id,
                audit_log_id=receipts[0].id,
            )
        )
    return tuple(observations)


def _observe_factual_correction(
    session: Session,
    project_id: int,
    fresh_run_ids: Mapping[int, int],
    frontend_diff: ProjectWriteSetDiff,
) -> FactualCorrectionObservation:
    bounded_document_ids = set(fresh_run_ids)
    created_audit_ids = _created_ids(frontend_diff, "audit_log")
    audits = tuple(
        session.scalars(
            select(AuditLog).where(
                AuditLog.id.in_(created_audit_ids),
                AuditLog.action == audit.CORRECT_STATEMENT_FACTS,
            )
        ).all()
    )
    if not audits:
        # A Committed Date Change is a new attributable statement, not a
        # correction of an earlier statement.  Without an attributable
        # successor there is no exact predecessor/successor fact pair to
        # compare, so raw Candidate membership cannot manufacture correction
        # support. Positive support is proven below only from the appended
        # successor, its exact bounded Evidence, and its frontend request.
        return FactualCorrectionObservation(
            outcome="no_structured_correction_observed",
            audit_log_id=None,
            predecessor_statement_event_id=None,
            successor_statement_event_id=None,
            supporting_candidate_ids=(),
        )
    if len(audits) != 1:
        raise ValueError("frontend pass recorded more than one factual correction")
    entry = audits[0]
    before_id = (entry.before_json or {}).get("statement_event_id")
    after_id = (entry.after_json or {}).get("statement_event_id")
    if (
        entry.entity_type != audit.COMMITMENT_LINEAGE
        or not isinstance(before_id, int)
        or not isinstance(after_id, int)
        or before_id <= 0
        or after_id <= 0
        or before_id == after_id
        or not entry.human_principal
    ):
        raise ValueError("factual correction audit is malformed")
    _require_attributable_principal(entry.human_principal)
    predecessor = session.get(ExternalPartyStatement, before_id)
    successor = session.get(ExternalPartyStatement, after_id)
    if (
        predecessor is None
        or successor is None
        or predecessor.project_id != project_id
        or successor.project_id != project_id
        or predecessor.commitment_lineage_id != entry.entity_id
        or successor.commitment_lineage_id != entry.entity_id
        or successor.supersedes_event_id != predecessor.id
        or successor.created_by != entry.human_principal
    ):
        raise ValueError(
            "factual correction did not preserve its statement predecessor"
        )
    if _statement_fact_signature(session, predecessor) == _statement_fact_signature(
        session, successor
    ):
        raise ValueError("factual correction did not change an External Party fact")
    evidence = tuple(
        session.scalars(
            select(EvidenceLink)
            .join(
                DependencyEventEvidence,
                DependencyEventEvidence.evidence_link_id == EvidenceLink.id,
            )
            .where(DependencyEventEvidence.event_id == successor.id)
            .order_by(EvidenceLink.id)
        ).all()
    )
    evidence_document_ids = {item.document_id for item in evidence}
    if not evidence_document_ids or not evidence_document_ids <= bounded_document_ids:
        raise ValueError(
            "factual correction successor is not supported by the bounded packet"
        )
    correction_requests = tuple(
        session.scalars(
            select(AuditLog).where(
                AuditLog.id.in_(created_audit_ids),
                AuditLog.action == audit.PRODUCT_PROVING_FRONTEND_REQUEST,
                AuditLog.after_json["route_name"].astext
                == "correct_statement_facts_from_screen",
                AuditLog.after_json["status"].as_integer() == 303,
            )
        ).all()
    )
    if len(correction_requests) != 1:
        raise ValueError("factual correction lacks its ordinary frontend request")
    request = correction_requests[0]
    subject = (request.after_json or {}).get("subject") or {}
    candidate_id = subject.get("candidate_id")
    candidate = session.get(Candidate, candidate_id)
    if (
        not isinstance(candidate_id, int)
        or candidate is None
        or candidate.project_id != project_id
        or candidate.kind != "event"
        or candidate.extraction_run_id not in set(fresh_run_ids.values())
        or request.human_principal != entry.human_principal
        or subject.get("commitment_lineage_id") != entry.entity_id
        or subject.get("predecessor_statement_event_id") != predecessor.id
        or subject.get("successor_statement_event_id") != successor.id
        or not _candidate_cites_any_evidence(candidate, evidence)
    ):
        raise ValueError(
            "factual correction is not tied to exact bounded Candidate Evidence"
        )
    return FactualCorrectionObservation(
        outcome="preserved_predecessor",
        audit_log_id=entry.id,
        predecessor_statement_event_id=predecessor.id,
        successor_statement_event_id=successor.id,
        supporting_candidate_ids=(candidate.id,),
    )


def _statement_fact_signature(
    session: Session, statement: ExternalPartyStatement
) -> tuple[object, ...]:
    timings = tuple(
        (
            item.kind,
            item.text,
            item.precision,
            item.start_date.isoformat() if item.start_date is not None else None,
            item.end_date.isoformat() if item.end_date is not None else None,
        )
        for item in session.scalars(
            select(DependencyEventTiming)
            .where(DependencyEventTiming.event_id == statement.id)
            .order_by(DependencyEventTiming.kind, DependencyEventTiming.id)
        ).all()
    )
    return (
        statement.event_type,
        statement.affected_external_org_id,
        statement.stated_external_org_id,
        statement.stated_party,
        statement.attribution_state,
        statement.event_date.isoformat() if statement.event_date is not None else None,
        statement.description,
        statement.timing_direction,
        timings,
    )


def _candidate_cites_any_evidence(
    candidate: Candidate, evidence: tuple[EvidenceLink, ...]
) -> bool:
    citations = (candidate.payload_json or {}).get("citations") or []
    evidence_keys = {(item.document_id, item.page_no, item.quote) for item in evidence}
    return any(
        isinstance(citation, dict)
        and (
            citation.get("document_id"),
            citation.get("page"),
            citation.get("quote"),
        )
        in evidence_keys
        for citation in citations
    )


def _observe_extraction_run_receipts(
    session: Session,
    fresh_run_ids: Mapping[int, int],
) -> tuple[Mapping[str, object], ...]:
    """Re-read exact committed extractor lineage; never copy caller JSON."""

    run_ids = set(fresh_run_ids.values())
    runs = tuple(
        session.scalars(
            select(ExtractionRun)
            .where(ExtractionRun.id.in_(run_ids))
            .order_by(ExtractionRun.document_id)
        ).all()
    )
    if {run.id for run in runs} != run_ids or len(run_ids) != len(fresh_run_ids):
        raise ValueError("fresh Extraction Run receipts are incomplete")
    receipts = []
    for run in runs:
        if fresh_run_ids.get(run.document_id) != run.id:
            raise ValueError("fresh Extraction Run receipt crossed its Document")
        if (
            run.outcome != "completed"
            or run.page_errors != 0
            or not run.prompt_sha256
            or not run.schema_sha256
            or not run.postprocessor_sha256
            or not run.extractor_config_sha256
            or not isinstance(run.token_usage_json, dict)
        ):
            raise ValueError(f"Extraction Run {run.id} has no complete lineage receipt")
        receipts.append(
            {
                "run_id": run.id,
                "document_id": run.document_id,
                "outcome": run.outcome,
                "page_errors": run.page_errors,
                "candidate_count": run.candidate_count,
                "prompt_version": run.prompt_version,
                "model": run.model,
                "schema_version": run.schema_version,
                "prompt_sha256": run.prompt_sha256,
                "schema_sha256": run.schema_sha256,
                "postprocessor_sha256": run.postprocessor_sha256,
                "extractor_config": dict(run.extractor_config_json or {}),
                "extractor_config_sha256": run.extractor_config_sha256,
                "token_usage": dict(run.token_usage_json),
            }
        )
    return tuple(receipts)


def _observe_report_release(
    session: Session,
    project_id: int,
    frontend_diff: ProjectWriteSetDiff,
) -> ReportReleaseObservation:
    report_run = _latest_created(
        session,
        ReportRun,
        _created_ids(frontend_diff, "report_runs"),
        project_id,
        "Report Run",
        "ts",
    )
    artifact = _latest_created(
        session,
        ExternalReportArtifact,
        _created_ids(frontend_diff, "external_report_artifacts"),
        project_id,
        "prepared Report artifact",
        "rendered_at",
    )
    release = _latest_created(
        session,
        ExternalReportRelease,
        _created_ids(frontend_diff, "external_report_releases"),
        project_id,
        "Approved Export release",
        "released_at",
    )
    if release.artifact_id != artifact.id:
        raise ValueError("Approved Export does not bind the pass's latest artifact")
    _require_attributable_principal(release.released_by)
    exact_digest = sha256(bytes(artifact.pdf_bytes)).hexdigest()
    if (
        artifact.pdf_sha256 != exact_digest
        or release.pdf_sha256 != exact_digest
        or bytes(release.pdf_bytes) != bytes(artifact.pdf_bytes)
        or artifact.record_context_json != release.record_context_json
        or artifact.evaluation_context_json != release.evaluation_context_json
        or artifact.provenance_mode != release.provenance_mode
        or artifact.ruleset_version != release.ruleset_version
        or report_run.ruleset_version != artifact.ruleset_version
    ):
        raise ValueError("Approved Export is not the exact reviewed Report artifact")
    if not bytes(artifact.pdf_bytes).startswith(b"%PDF-"):
        raise ValueError("prepared External Report is not a PDF")
    if (
        report_run.ts > release.released_at
        or artifact.rendered_at > release.released_at
    ):
        raise ValueError("Report, review artifact, and release order is impossible")
    _validate_report_context_binding(report_run, artifact)
    provenance = _retained_provenance_classes(artifact)
    required = {"Assertion", "Derivation", "Work Decision"}
    if not required <= set(provenance):
        raise ValueError(
            "retained Report does not prove all required provenance classes"
        )
    return ReportReleaseObservation(
        report_run_id=report_run.id,
        artifact_id=artifact.id,
        release_id=release.id,
        pdf_sha256=exact_digest,
        pdf_bytes=bytes(release.pdf_bytes),
        provenance_classes=provenance,
    )


def _observe_frontend_requests(
    session: Session,
    *,
    frontend_diff: ProjectWriteSetDiff,
    residuals: tuple[ResidualCandidateObservation, ...],
    decision_changes: tuple[WorkDecisionChangeObservation, ...],
    correction: FactualCorrectionObservation,
    invalid_action: RefusedInvalidActionObservation,
    report_release: ReportReleaseObservation,
) -> tuple[FrontendRequestObservation, ...]:
    audit_ids = _created_ids(frontend_diff, "audit_log")
    rows = tuple(
        session.scalars(
            select(AuditLog)
            .where(
                AuditLog.id.in_(audit_ids),
                AuditLog.action == audit.PRODUCT_PROVING_FRONTEND_REQUEST,
            )
            .order_by(AuditLog.id)
        ).all()
    )
    observations = []
    for row in rows:
        payload = row.after_json or {}
        subject = payload.get("subject")
        route_template = payload.get("route_template")
        request_fields_sha256 = payload.get("request_fields_sha256")
        if (
            payload.get("schema_version") != FRONTEND_REQUEST_SCHEMA
            or not isinstance(payload.get("route_name"), str)
            or not isinstance(route_template, str)
            or not route_template.startswith("/")
            or payload.get("method") not in {"GET", "POST"}
            or isinstance(payload.get("status"), bool)
            or not isinstance(payload.get("status"), int)
            or not isinstance(request_fields_sha256, str)
            or _SHA256.fullmatch(request_fields_sha256) is None
            or not isinstance(subject, dict)
            or not subject
            or not set(subject) <= _FRONTEND_SUBJECT_KEYS
            or "project_id" not in subject
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
                for value in subject.values()
            )
            or not row.human_principal
            or row.actor != row.human_principal
            or row.entity_type != audit.PROJECT
            or row.entity_id != subject.get("project_id")
            or row.entity_id != frontend_diff.project_id
            or not _frontend_route_receipt_is_valid(
                route_name=payload.get("route_name"),
                route_template=route_template,
                method=payload.get("method"),
                status=payload.get("status"),
            )
        ):
            raise ValueError("frontend request receipt is malformed")
        observations.append(
            FrontendRequestObservation(
                audit_log_id=row.id,
                route_name=payload["route_name"],
                route_template=route_template,
                method=payload["method"],
                status=payload["status"],
                principal=row.human_principal,
                request_fields_sha256=request_fields_sha256,
                subject=dict(subject),
            )
        )
    if not observations:
        raise ValueError("frontend phase has no server-observed requests")
    principals = {item.principal for item in observations}
    if len(principals) != 1:
        raise ValueError("frontend requests do not share one practitioner")

    def require(
        route: str,
        *,
        method: str,
        status: int,
        subject_key: str | None = None,
        subject_id: int | None = None,
    ) -> FrontendRequestObservation:
        matches = [
            item
            for item in observations
            if item.route_name == route
            and item.method == method
            and item.status == status
            and (subject_key is None or item.subject.get(subject_key) == subject_id)
        ]
        if not matches:
            raise ValueError(f"real frontend did not observe required route {route}")
        return matches[-1]

    require("coordinator_home", method="GET", status=200)
    for residual in residuals:
        require(
            "queue"
            if residual.candidate_kind == "dependency"
            else "coordinate_statement_screen",
            method="GET",
            status=200,
            subject_key="candidate_id",
            subject_id=residual.candidate_id,
        )
        action_route = (
            "accept"
            if residual.outcome == "supported"
            and residual.candidate_kind == "dependency"
            else "save_coordinated_statement"
            if residual.outcome == "supported"
            else "mark_waiting_statement_not_relevant"
            if residual.outcome == "not_relevant"
            else "keep_unresolved_candidate"
            if residual.candidate_kind == "dependency"
            else "keep_unresolved_statement"
        )
        require(
            action_route,
            method="POST",
            status=303,
            subject_key="candidate_id",
            subject_id=residual.candidate_id,
        )
    for decision in decision_changes:
        matching_routes = {
            "assign_owner",
            "record_next_action",
            "save_admitted_statement_owner",
            "save_admitted_statement_next_action",
        }
        if not any(
            item.route_name in matching_routes
            and item.method == "POST"
            and item.status == 303
            and item.subject.get("work_decision_id") == decision.successor_decision_id
            for item in observations
        ):
            raise ValueError("Work Decision change lacks its frontend request")
    if correction.outcome == "preserved_predecessor":
        if len(correction.supporting_candidate_ids) != 1:
            raise ValueError("factual correction has no exact supporting Candidate")
        correction_request = require(
            "correct_statement_facts_from_screen",
            method="POST",
            status=303,
            subject_key="candidate_id",
            subject_id=correction.supporting_candidate_ids[0],
        )
        if correction_request.subject != {
            "project_id": correction_request.subject.get("project_id"),
            "candidate_id": correction.supporting_candidate_ids[0],
            "commitment_lineage_id": correction_request.subject.get(
                "commitment_lineage_id"
            ),
            "predecessor_statement_event_id": (
                correction.predecessor_statement_event_id
            ),
            "successor_statement_event_id": correction.successor_statement_event_id,
        } or not correction_request.subject.get("commitment_lineage_id"):
            raise ValueError(
                "factual correction frontend request does not bind its successor"
            )
    if invalid_action.frontend_request_audit_id not in {
        item.audit_log_id for item in observations
    }:
        raise ValueError("invalid action receipt is outside frontend request history")
    require(
        "render_report",
        method="POST",
        status=201,
        subject_key="report_run_id",
        subject_id=report_release.report_run_id,
    )
    require(
        "review_report",
        method="GET",
        status=200,
        subject_key="artifact_id",
        subject_id=report_release.artifact_id,
    )
    require(
        "preview_prepared_report",
        method="GET",
        status=200,
        subject_key="artifact_id",
        subject_id=report_release.artifact_id,
    )
    require(
        "release_prepared_report",
        method="POST",
        status=201,
        subject_key="release_id",
        subject_id=report_release.release_id,
    )
    return tuple(observations)


def _frontend_route_receipt_is_valid(
    *,
    route_name: object,
    route_template: object,
    method: object,
    status: object,
) -> bool:
    if not isinstance(route_name, str):
        return False
    contract = _FRONTEND_ROUTE_CONTRACTS.get(route_name)
    if contract is None:
        return False
    expected_template, expected_method, allowed_statuses = contract
    return (
        route_template == expected_template
        and method == expected_method
        and not isinstance(status, bool)
        and isinstance(status, int)
        and status in allowed_statuses
    )


def _require_practitioner_work_precedes_report(
    session: Session,
    *,
    residuals: tuple[ResidualCandidateObservation, ...],
    decision_changes: tuple[WorkDecisionChangeObservation, ...],
    correction: FactualCorrectionObservation,
    invalid_action: RefusedInvalidActionObservation,
    frontend_requests: tuple[FrontendRequestObservation, ...],
    report_release: ReportReleaseObservation,
) -> None:
    report_run = session.get(ReportRun, report_release.report_run_id)
    artifact = session.get(ExternalReportArtifact, report_release.artifact_id)
    release = session.get(ExternalReportRelease, report_release.release_id)
    if report_run is None or artifact is None or release is None:
        raise ValueError("Report chronology receipts disappeared")
    prerequisite_audit_ids = {
        *(item.audit_log_id for item in residuals),
        *(item.audit_log_id for item in decision_changes),
        invalid_action.frontend_request_audit_id,
    }
    if correction.audit_log_id is not None:
        prerequisite_audit_ids.add(correction.audit_log_id)
    report_routes = {
        "reports",
        "render_report",
        "review_report",
        "preview_prepared_report",
        "download_prepared_report",
        "release_prepared_report",
    }
    prerequisite_audit_ids.update(
        item.audit_log_id
        for item in frontend_requests
        if item.route_name not in report_routes
    )
    audits = tuple(
        session.scalars(
            select(AuditLog).where(AuditLog.id.in_(prerequisite_audit_ids))
        ).all()
    )
    if {item.id for item in audits} != prerequisite_audit_ids or any(
        item.ts > report_run.ts for item in audits
    ):
        raise ValueError("practitioner work was recorded after the final Report")
    decision_ids = {item.successor_decision_id for item in decision_changes}
    decisions = tuple(
        session.scalars(
            select(WorkDecision).where(WorkDecision.id.in_(decision_ids))
        ).all()
    )
    if {item.id for item in decisions} != decision_ids or any(
        item.recorded_at > report_run.ts for item in decisions
    ):
        raise ValueError("Work Decision change occurred after the final Report")
    if not (report_run.ts <= artifact.rendered_at <= release.released_at):
        raise ValueError("Report preparation and release chronology is invalid")
    report_requests = {
        item.route_name: session.get(AuditLog, item.audit_log_id)
        for item in frontend_requests
        if item.route_name
        in {
            "render_report",
            "review_report",
            "preview_prepared_report",
            "release_prepared_report",
        }
    }
    if set(report_requests) != {
        "render_report",
        "review_report",
        "preview_prepared_report",
        "release_prepared_report",
    } or any(item is None for item in report_requests.values()):
        raise ValueError("Report frontend chronology receipts are incomplete")
    render_request = report_requests["render_report"]
    review_request = report_requests["review_report"]
    preview_request = report_requests["preview_prepared_report"]
    release_request = report_requests["release_prepared_report"]
    assert render_request is not None
    assert review_request is not None
    assert preview_request is not None
    assert release_request is not None
    if not (
        report_run.ts
        <= artifact.rendered_at
        <= render_request.ts
        <= review_request.ts
        <= preview_request.ts
        <= release.released_at
        <= release_request.ts
    ) or not (
        render_request.id < review_request.id < preview_request.id < release_request.id
    ):
        raise ValueError(
            "Report must be rendered, reviewed, previewed, and then released"
        )


def _validate_report_context_binding(
    report_run: ReportRun, artifact: ExternalReportArtifact
) -> None:
    snapshot = (report_run.snapshot_json or {}).get("dependencies") or {}
    if not isinstance(snapshot, dict):
        raise ValueError("Report Run dependency snapshot is invalid")
    retained = artifact.record_context_json or {}
    dependencies = retained.get("dependencies") or []
    if not isinstance(dependencies, list):
        raise ValueError("retained Report dependency context is invalid")
    retained_dependencies = {}
    for item in dependencies:
        if not isinstance(item, dict):
            raise ValueError("retained Report dependency entry is invalid")
        ref_code = item.get("ref_code")
        dependency_id = item.get("dependency_id")
        if not isinstance(ref_code, str) or ref_code in retained_dependencies:
            raise ValueError("retained Report dependency identity is invalid")
        retained_dependencies[ref_code] = dependency_id
        source = snapshot.get(ref_code)
        if not isinstance(source, dict) or source.get("id") != dependency_id:
            raise ValueError(
                "prepared artifact is not bound to its pass-created Report Run"
            )
    if set(retained_dependencies) != set(snapshot):
        raise ValueError(
            "prepared artifact dependency coverage differs from Report Run"
        )
    report_identity = retained.get("report_run")
    snapshot_sha256 = sha256(
        json.dumps(
            report_run.snapshot_json,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    if report_identity != {
        "id": report_run.id,
        "snapshot_sha256": snapshot_sha256,
    }:
        raise ValueError("prepared artifact does not name its exact Report Run")
    if retained.get("document_only") is not report_run.document_only:
        raise ValueError("prepared artifact document mode differs from Report Run")
    snapshot_statements = (report_run.snapshot_json or {}).get(
        "external_party_commitments"
    ) or {}
    retained_statements = retained.get("party_statements") or []
    if not isinstance(snapshot_statements, dict) or not isinstance(
        retained_statements, list
    ):
        raise ValueError("prepared artifact statement coverage is invalid")
    retained_mapping: dict[str, dict[str, object]] = {}
    for item in retained_statements:
        if not isinstance(item, dict):
            raise ValueError("prepared artifact statement coverage is invalid")
        lineage_id = item.get("commitment_lineage_id")
        current_event_id = item.get("current_statement_event_id")
        published_event_id = item.get("published_statement_event_id")
        scope_decision_id = item.get("scope_decision_id")
        unsupported_current = item.get("unsupported_current")
        key = str(lineage_id)
        if (
            isinstance(lineage_id, bool)
            or not isinstance(lineage_id, int)
            or lineage_id <= 0
            or isinstance(current_event_id, bool)
            or not isinstance(current_event_id, int)
            or current_event_id <= 0
            or (
                published_event_id is not None
                and (
                    isinstance(published_event_id, bool)
                    or not isinstance(published_event_id, int)
                    or published_event_id <= 0
                )
            )
            or isinstance(scope_decision_id, bool)
            or not isinstance(scope_decision_id, int)
            or scope_decision_id <= 0
            or not isinstance(unsupported_current, bool)
            or key in retained_mapping
        ):
            raise ValueError("prepared artifact statement identity is invalid")
        retained_mapping[key] = {
            "current_event_id": current_event_id,
            "published_event_id": published_event_id,
            "scope_decision_id": scope_decision_id,
            "unsupported_current": unsupported_current,
        }
    snapshot_mapping = {}
    for lineage_id, item in snapshot_statements.items():
        if not isinstance(item, dict):
            raise ValueError("Report Run statement coverage is invalid")
        snapshot_mapping[str(lineage_id)] = {
            "current_event_id": item.get("current_event_id"),
            "published_event_id": item.get("published_event_id"),
            "scope_decision_id": item.get("scope_decision_id"),
            "unsupported_current": item.get("unsupported_current"),
        }
    if retained_mapping != snapshot_mapping:
        raise ValueError("prepared artifact statement coverage differs from Report Run")


def _retained_provenance_classes(
    artifact: ExternalReportArtifact,
) -> tuple[str, ...]:
    retained = artifact.record_context_json or {}
    raw_classes = retained.get("provenance_classes")
    if not isinstance(raw_classes, list) or any(
        not isinstance(item, str) for item in raw_classes
    ):
        raise ValueError("retained Report provenance context is absent or invalid")
    classes = set(raw_classes)
    unknown = classes - _PROVENANCE_CLASSES
    if unknown:
        raise ValueError("retained Report names an unknown provenance class")
    cells = retained.get("report_cells")
    if not isinstance(cells, list) or not cells:
        raise ValueError("retained Report cell provenance is absent")
    cell_classes = set()
    for ordinal, cell in enumerate(cells, start=1):
        if (
            not isinstance(cell, dict)
            or cell.get("ordinal") != ordinal
            or not isinstance(cell.get("label"), str)
            or not isinstance(cell.get("value"), str)
            or cell.get("provenance_class") not in _PROVENANCE_CLASSES
            or not str(cell.get("provenance_marker") or "").strip()
            or not str(cell.get("provenance_drill") or "").strip()
        ):
            raise ValueError("retained Report cell provenance is invalid")
        cell_classes.add(cell["provenance_class"])
    if cell_classes != classes:
        raise ValueError("retained Report provenance summary differs from its cells")
    return tuple(name for name in _PROVENANCE_ORDER if name in classes)


def _latest_created(
    session: Session,
    model: type,
    created_ids: set[int],
    project_id: int,
    label: str,
    timestamp_name: str,
):
    if not created_ids:
        raise ValueError(f"frontend pass created no {label}")
    rows = tuple(
        session.scalars(
            select(model).where(
                model.id.in_(created_ids), model.project_id == project_id
            )
        ).all()
    )
    if {item.id for item in rows} != created_ids:
        raise ValueError(f"frontend {label} identities cross the bounded Project")
    if len(rows) != 1:
        raise ValueError(f"frontend pass must create exactly one {label}")
    return rows[0]


def _created_ids(write_set: ProjectWriteSetDiff, table_name: str) -> set[int]:
    values = set()
    for item in write_set.created.get(table_name, ()):
        identity = item.identity
        value = identity.get("id")
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{table_name} write-set identity is invalid")
        values.add(value)
    return values


def _one_by(rows: tuple, attribute: str, label: str) -> dict[int, object]:
    result = {}
    for row in rows:
        key = getattr(row, attribute)
        if key in result:
            raise ValueError(f"frontend recorded more than one {label} for {key}")
        result[key] = row
    return result


def _require_attributable_principal(value: str) -> None:
    try:
        HumanPrincipal(value)
    except ValueError as exc:
        raise ValueError(
            "frontend receipt is not attributable to a human principal"
        ) from exc


def _complete_write_set(
    before: ProjectWriteSetSnapshot,
    after: ProjectWriteSetSnapshot,
    diff: ProjectWriteSetDiff,
) -> dict[str, list[dict]]:
    measured = diff.as_write_set()
    return {
        table_name: measured.get(table_name, [])
        for table_name in sorted(set(before.rows) | set(after.rows))
    }
