"""Bounded raw-Document Product Proving Run contract.

This is the highest product-testing seam.  It does not reimplement extraction,
Admission, Adjudication, the Work List, Report, or Approved Export rules.  It
pins their inputs, records operations separately from server-observed frontend
routes and retained screenshot identities,
and decides whether two complete passes support the narrow ADR-0046 claim.

The receipt is deliberately external to PostgreSQL.  A failed pass stays a
failed pass after a repair, and restoring the sealed development baseline
cannot erase either the failure or the exact reviewed PDF bytes.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Literal, Mapping, Sequence
from uuid import UUID

from corridor.m8_acceptance_bundle import (
    VerificationResult,
    publish_verified_bundle,
    verify_bundle,
)
from corridor.extractor_lineage import validate_config_json_shape


BUNDLE_SCHEMA_VERSION = "corridor.product-proving-run-bundle.v2"
BUNDLE_FAILURE_SCHEMA_VERSION = "corridor.product-proving-run-failure-bundle.v1"
FRONTEND_EVIDENCE_KIND = "server_observed_frontend_routes_with_browser_screenshots"
BUNDLE_FILES = (
    "canonical-content.json",
    "environment.json",
    "receipt.json",
    "receipt.md",
    "pass-1-approved-export.pdf",
    "pass-2-approved-export.pdf",
)
FAILURE_BUNDLE_FILES = (
    "canonical-content.json",
    "environment.json",
    "receipt.json",
    "receipt.md",
)
_OUTCOMES = frozenset({"supported", "not_relevant", "unresolved"})
_FACTUAL_CORRECTION_OUTCOMES = frozenset(
    {"preserved_predecessor", "no_structured_correction_observed"}
)
_PROVENANCE_CLASSES = frozenset(
    {"Assertion", "Derivation", "Work Decision", "Verbal"}
)
_MANDATORY_WRITE_FAMILIES = frozenset(
    {
        "active_extraction_runs",
        "active_run_declarations",
        "assertions",
        "audit_log",
        "candidate_dispositions",
        "candidates",
        "dependencies",
        "dependency_admission_outcomes",
        "event_admission_outcomes",
        "evidence_investigation_candidate_review_starts",
        "evidence_links",
        "external_report_artifacts",
        "external_report_releases",
        "extraction_runs",
        "extraction_measurement_case_states",
        "policy_runs",
        "report_runs",
        "work_decisions",
    }
)
_FRONTEND_ACTION = re.compile(
    r"^(?P<route>[a-z][a-z0-9_]*)(?::(?P<subject>[1-9][0-9]*))?$"
)
_RESIDUAL_FRONTEND_ACTIONS = frozenset(
    {
        "accept_candidate",
        "merge_candidate",
        "attach_statement",
        "coordinate_statement",
        "mark_statement_not_relevant",
        "keep_candidate_unresolved",
    }
)
_SUBJECT_FRONTEND_ACTIONS = frozenset(
    {
        "review_candidate",
        *_RESIDUAL_FRONTEND_ACTIONS,
        "change_work_decision",
        "correct_statement_facts",
        "review_report",
        "release_approved_export",
    }
)


class CorruptProductProvingBundle(ValueError):
    """A published Product Proving receipt does not match its sealed facts."""


@dataclass(frozen=True)
class ExpectedPreflight:
    """Caller-held pins that must match before the first proving write."""

    source_revision: str
    origin_main_revision: str
    migration_head: str
    policy_digests: Mapping[str, str]
    documents: Mapping[int, str]
    baseline_runs: Mapping[int, int]
    milestone_sources: Mapping[str, str]
    baseline_fingerprint: str


@dataclass(frozen=True)
class ObservedPreflight:
    """Read-only observation of the checkout and sealed Project Record."""

    source_revision: str
    origin_main_revision: str
    clean_worktree: bool
    migration_head: str
    policy_digests: Mapping[str, str]
    documents: Mapping[int, str]
    baseline_runs: Mapping[int, int]
    milestone_sources: Mapping[str, str]
    baseline_fingerprint: str


@dataclass(frozen=True)
class ExtractionConfiguration:
    """The exact extractor lineage required before semantic comparison."""

    prompt_version: str
    model: str | None
    schema_version: str
    prompt_sha256: str
    schema_sha256: str
    postprocessor_sha256: str
    config_sha256: str


@dataclass(frozen=True)
class CandidateSetComparison:
    """Semantic comparison of two exact Extraction Runs for one Document."""

    document_id: int
    baseline_run_id: int
    fresh_run_id: int
    baseline_configuration: ExtractionConfiguration
    fresh_configuration: ExtractionConfiguration
    added: tuple[dict[str, Any], ...]
    missing: tuple[dict[str, Any], ...]
    matched_sha256: tuple[str, ...]

    @property
    def equal(self) -> bool:
        return not self.added and not self.missing


@dataclass(frozen=True)
class ProductProvingPass:
    """One terminal pass, including honest failure evidence when present."""

    pass_number: int
    restored_baseline_fingerprint: str
    extraction_comparisons: tuple[CandidateSetComparison, ...]
    extraction_run_receipts: tuple[Mapping[str, Any], ...]
    extraction_failures: tuple[str, ...]
    admission_completed: bool
    residual_candidate_ids: tuple[int, ...]
    residual_outcomes: Mapping[int, Literal["supported", "not_relevant", "unresolved"]]
    frontend_kind: Literal[
        "server_observed_frontend_routes_with_browser_screenshots"
    ]
    frontend_actions: tuple[str, ...]
    invalid_action_refused: bool
    invalid_action_write_set: Mapping[str, Any]
    factual_correction_outcome: Literal[
        "preserved_predecessor", "no_structured_correction_observed"
    ]
    work_decision_change_preserved_predecessor: bool
    report_pdf_sha256: str
    approved_export_sha256: str
    approved_export_bytes: bytes
    report_provenance_classes: tuple[str, ...]
    write_set: Mapping[str, Sequence[Any]]
    operations_elapsed_seconds: float
    practitioner_elapsed_seconds: float
    non_blocking_friction: tuple[str, ...]
    workarounds: tuple[str, ...]


@dataclass(frozen=True)
class DatabaseSourceIdentity:
    """Non-secret identity of the one shared PostgreSQL database."""

    backend: str
    host: str | None
    port: int | None
    database: str
    username: str | None


@dataclass(frozen=True)
class DatabaseConnectionIdentityEvidence:
    """Server-observed database, role, endpoint, cluster, and PostgreSQL identity."""

    database: str
    username: str
    server_address: str
    server_port: str
    system_identifier: str
    postgres_version: str


@dataclass(frozen=True)
class ProductProvingDatabaseBaselineEvidence:
    """Exact immutable database baseline used by both executions."""

    manifest_sha256: str
    dump_sha256: str
    state_sha256: str
    schema_sha256: str
    source_identity: DatabaseSourceIdentity
    source_connection_identity: DatabaseConnectionIdentityEvidence


@dataclass(frozen=True)
class ProductProvingPassExecutionEvidence:
    """One actual pass execution and its independently sealed frontend bundle."""

    execution_id: str
    pass_number: int
    prior_restore_operation_id: str | None
    prior_restore_bundle_manifest_sha256: str | None
    prior_restore_bundle_canonical_sha256: str | None
    started_at: str
    starting_database_state_sha256: str
    starting_database_schema_sha256: str
    frontend_bundle_manifest_sha256: str
    frontend_bundle_canonical_sha256: str
    terminal_database_state_sha256: str


@dataclass(frozen=True)
class ProductProvingRestoreEvidence:
    """One actual restore operation following one exact terminal pass state."""

    restore_operation_id: str
    pass_number: int
    pass_execution_id: str
    pass_bundle_manifest_sha256: str
    pass_bundle_canonical_sha256: str
    restore_bundle_manifest_sha256: str
    restore_bundle_canonical_sha256: str
    previous_database_state_sha256: str
    restored_database_state_sha256: str
    database_baseline_manifest_sha256: str
    database_baseline_dump_sha256: str
    database_baseline_state_sha256: str
    database_baseline_schema_sha256: str
    database_source_identity: DatabaseSourceIdentity
    database_source_connection_identity: DatabaseConnectionIdentityEvidence
    started_at: str
    completed_at: str


@dataclass(frozen=True)
class ProductProvingFinalStateEvidence:
    """The source database identity and state observed after restore 2."""

    database_state_sha256: str
    database_schema_sha256: str
    source_identity: DatabaseSourceIdentity
    source_connection_identity: DatabaseConnectionIdentityEvidence


@dataclass(frozen=True)
class ProductProvingFinalSessionEvidence:
    """Canonical baseline -> pass -> restore -> pass -> restore closure."""

    baseline: ProductProvingDatabaseBaselineEvidence
    pass_one: ProductProvingPassExecutionEvidence
    restore_one: ProductProvingRestoreEvidence
    pass_two: ProductProvingPassExecutionEvidence
    restore_two: ProductProvingRestoreEvidence
    final_state: ProductProvingFinalStateEvidence


@dataclass(frozen=True)
class ProductProvingCapture:
    """The complete two-pass observation and narrow claim boundary."""

    expected: ExpectedPreflight
    observed: ObservedPreflight
    pass_one: ProductProvingPass
    pass_two: ProductProvingPass
    final_session_evidence: ProductProvingFinalSessionEvidence
    final_baseline_fingerprint: str
    simulated_practitioner: bool
    same_project_manual_report_compared: bool
    revision_processing_included: bool


@dataclass(frozen=True)
class ProductProvingFailureCapture:
    """One terminal failure that must remain distinguishable from success."""

    expected: ExpectedPreflight
    observed: ObservedPreflight
    pass_number: int
    phase: str
    errors: tuple[str, ...]
    extraction_comparisons: tuple[CandidateSetComparison, ...]
    extraction_run_receipts: tuple[Mapping[str, Any], ...]
    admission_started: bool
    source_database_mutated: bool
    operations_elapsed_seconds: float
    baseline_dump_sha256: str
    baseline_state_manifest_sha256: str
    restored_baseline_fingerprint: str


@dataclass(frozen=True)
class ProductProvingBundleSummary:
    bundle_dir: Path
    manifest_path: Path
    integrity_manifest_sha256: str
    canonical_content_sha256: str


def compare_candidate_sets(
    *,
    document_id: int,
    baseline_run_id: int,
    fresh_run_id: int,
    baseline: Sequence[Mapping[str, Any]],
    fresh: Sequence[Mapping[str, Any]],
    baseline_configuration: ExtractionConfiguration,
    fresh_configuration: ExtractionConfiguration,
) -> CandidateSetComparison:
    """Compare Candidate meaning while ignoring only identities and ordering.

    Confidence, review state, and dedupe hints are not Candidate facts.  The
    Document, kind, supported fields, exact Evidence, prompt version, model,
    source pages, and mechanical citation result remain in the comparison.
    Duplicate semantic Candidates remain significant through the Counter.
    """

    _validate_extraction_configuration(
        baseline_configuration, baseline, "baseline"
    )
    _validate_extraction_configuration(fresh_configuration, fresh, "fresh")
    if baseline_configuration != fresh_configuration:
        raise ValueError(
            "Product Proving Candidate comparison requires a "
            "configuration-compatible baseline"
        )

    baseline_values = [_canonical_candidate(document_id, item) for item in baseline]
    fresh_values = [_canonical_candidate(document_id, item) for item in fresh]
    baseline_encoded = [_canonical_json(item) for item in baseline_values]
    fresh_encoded = [_canonical_json(item) for item in fresh_values]
    baseline_counts = Counter(baseline_encoded)
    fresh_counts = Counter(fresh_encoded)
    added = _expanded_difference(fresh_counts - baseline_counts)
    missing = _expanded_difference(baseline_counts - fresh_counts)
    matched = tuple(
        sorted(
            sha256(value).hexdigest()
            for value, count in (baseline_counts & fresh_counts).items()
            for _ in range(count)
        )
    )
    return CandidateSetComparison(
        document_id=document_id,
        baseline_run_id=baseline_run_id,
        fresh_run_id=fresh_run_id,
        baseline_configuration=baseline_configuration,
        fresh_configuration=fresh_configuration,
        added=added,
        missing=missing,
        matched_sha256=matched,
    )


def _validate_extraction_configuration(
    configuration: ExtractionConfiguration,
    candidates: Sequence[Mapping[str, Any]],
    label: str,
) -> None:
    """Bind run-level configuration even when a valid run produced no rows."""
    if (
        not configuration.prompt_version.strip()
        or not configuration.schema_version.strip()
        or any(
            not _is_sha256(value)
            for value in (
                configuration.prompt_sha256,
                configuration.schema_sha256,
                configuration.postprocessor_sha256,
                configuration.config_sha256,
            )
        )
    ):
        raise ValueError(f"Product Proving {label} extractor configuration is invalid")
    configurations = {
        (candidate.get("prompt_version"), candidate.get("model"))
        for candidate in candidates
    }
    expected = (configuration.prompt_version, configuration.model)
    if configurations and configurations != {expected}:
        raise ValueError(
            f"Product Proving {label} Candidate set disagrees with its Extraction Run"
        )


def verify_preflight(expected: ExpectedPreflight, observed: ObservedPreflight) -> None:
    """Refuse before writes when any source, schema, policy, or data pin moved."""

    if not observed.clean_worktree:
        raise ValueError("Product Proving requires a clean source checkout")
    checks = (
        ("source revision", expected.source_revision, observed.source_revision),
        ("origin/main", expected.origin_main_revision, observed.origin_main_revision),
        ("migration head", expected.migration_head, observed.migration_head),
        ("policy digest", dict(expected.policy_digests), dict(observed.policy_digests)),
        ("Document identity", dict(expected.documents), dict(observed.documents)),
        (
            "baseline Extraction Run",
            dict(expected.baseline_runs),
            dict(observed.baseline_runs),
        ),
        (
            "Milestone source",
            dict(expected.milestone_sources),
            dict(observed.milestone_sources),
        ),
        (
            "baseline fingerprint",
            expected.baseline_fingerprint,
            observed.baseline_fingerprint,
        ),
    )
    for label, wanted, found in checks:
        if wanted != found:
            raise ValueError(f"Product Proving {label} does not match the caller pin")


def verify_final_session_evidence(
    evidence: ProductProvingFinalSessionEvidence,
    *,
    expected_baseline_fingerprint: str,
    pass_one: ProductProvingPass | Mapping[str, Any],
    pass_two: ProductProvingPass | Mapping[str, Any],
    final_baseline_fingerprint: str,
) -> None:
    """Validate the durable baseline -> pass -> restore chain without a database."""

    if not isinstance(evidence, ProductProvingFinalSessionEvidence) or not all(
        (
            isinstance(evidence.baseline, ProductProvingDatabaseBaselineEvidence),
            isinstance(evidence.pass_one, ProductProvingPassExecutionEvidence),
            isinstance(evidence.restore_one, ProductProvingRestoreEvidence),
            isinstance(evidence.pass_two, ProductProvingPassExecutionEvidence),
            isinstance(evidence.restore_two, ProductProvingRestoreEvidence),
            isinstance(evidence.final_state, ProductProvingFinalStateEvidence),
        )
    ):
        raise ValueError("final Product Proving session evidence is not typed")
    baseline = evidence.baseline
    first = evidence.pass_one
    first_restore = evidence.restore_one
    second = evidence.pass_two
    second_restore = evidence.restore_two
    final = evidence.final_state
    first_started = _parse_aware_datetime(first.started_at)
    first_restore_started = _parse_aware_datetime(first_restore.started_at)
    first_restore_completed = _parse_aware_datetime(first_restore.completed_at)
    second_started = _parse_aware_datetime(second.started_at)
    second_restore_started = _parse_aware_datetime(second_restore.started_at)
    second_restore_completed = _parse_aware_datetime(second_restore.completed_at)
    pass_one_number = _pass_field(pass_one, "pass_number")
    pass_two_number = _pass_field(pass_two, "pass_number")
    pass_one_baseline = _pass_field(pass_one, "restored_baseline_fingerprint")
    pass_two_baseline = _pass_field(pass_two, "restored_baseline_fingerprint")

    _validate_database_source_identity(baseline.source_identity)
    _validate_database_source_identity(final.source_identity)
    _validate_database_connection_identity(baseline.source_connection_identity)
    _validate_database_connection_identity(final.source_connection_identity)
    _validate_database_identity_pair(
        baseline.source_identity, baseline.source_connection_identity
    )
    _validate_database_identity_pair(
        final.source_identity, final.source_connection_identity
    )
    digests = (
        baseline.manifest_sha256,
        baseline.dump_sha256,
        baseline.state_sha256,
        baseline.schema_sha256,
        first.starting_database_state_sha256,
        first.starting_database_schema_sha256,
        first.frontend_bundle_manifest_sha256,
        first.frontend_bundle_canonical_sha256,
        first.terminal_database_state_sha256,
        first_restore.restore_bundle_manifest_sha256,
        first_restore.restore_bundle_canonical_sha256,
        first_restore.pass_bundle_manifest_sha256,
        first_restore.pass_bundle_canonical_sha256,
        first_restore.previous_database_state_sha256,
        first_restore.restored_database_state_sha256,
        first_restore.database_baseline_manifest_sha256,
        first_restore.database_baseline_dump_sha256,
        first_restore.database_baseline_state_sha256,
        first_restore.database_baseline_schema_sha256,
        second.starting_database_state_sha256,
        second.starting_database_schema_sha256,
        second.frontend_bundle_manifest_sha256,
        second.frontend_bundle_canonical_sha256,
        second.terminal_database_state_sha256,
        second_restore.restore_bundle_manifest_sha256,
        second_restore.restore_bundle_canonical_sha256,
        second_restore.pass_bundle_manifest_sha256,
        second_restore.pass_bundle_canonical_sha256,
        second_restore.previous_database_state_sha256,
        second_restore.restored_database_state_sha256,
        second_restore.database_baseline_manifest_sha256,
        second_restore.database_baseline_dump_sha256,
        second_restore.database_baseline_state_sha256,
        second_restore.database_baseline_schema_sha256,
        second.prior_restore_bundle_manifest_sha256,
        second.prior_restore_bundle_canonical_sha256,
        final.database_state_sha256,
        final.database_schema_sha256,
        expected_baseline_fingerprint,
        final_baseline_fingerprint,
    )
    if not all(_is_sha256(value) for value in digests):
        raise ValueError("final Product Proving session contains an invalid digest")

    operation_ids = (
        first.execution_id,
        first_restore.restore_operation_id,
        second.execution_id,
        second_restore.restore_operation_id,
    )
    for value in operation_ids:
        _validate_uuid4(value)
    if len(set(operation_ids)) != len(operation_ids):
        raise ValueError("final Product Proving operation ids are not distinct")
    if not (
        first_started
        <= first_restore_started
        <= first_restore_completed
        <= second_started
        <= second_restore_started
        <= second_restore_completed
    ):
        raise ValueError("final Product Proving operation times are not ordered")

    if baseline.state_sha256 != expected_baseline_fingerprint:
        raise ValueError("final session baseline does not match the preflight baseline")
    if (
        isinstance(first.pass_number, bool)
        or first.pass_number != 1
        or isinstance(pass_one_number, bool)
        or pass_one_number != 1
    ):
        raise ValueError("final session pass 1 identity is invalid")
    if (
        first.prior_restore_operation_id is not None
        or first.prior_restore_bundle_manifest_sha256 is not None
        or first.prior_restore_bundle_canonical_sha256 is not None
    ):
        raise ValueError("pass 1 must not name a prior restore")
    if (
        first.starting_database_state_sha256 != baseline.state_sha256
        or first.starting_database_schema_sha256 != baseline.schema_sha256
        or pass_one_baseline != baseline.state_sha256
    ):
        raise ValueError("pass 1 did not start from the database baseline")
    if (
        isinstance(first_restore.pass_number, bool)
        or first_restore.pass_number != 1
        or first_restore.pass_execution_id != first.execution_id
        or first_restore.pass_bundle_manifest_sha256
        != first.frontend_bundle_manifest_sha256
        or first_restore.pass_bundle_canonical_sha256
        != first.frontend_bundle_canonical_sha256
        or first_restore.previous_database_state_sha256
        != first.terminal_database_state_sha256
    ):
        raise ValueError("restore 1 does not follow pass 1")
    if first_restore.restored_database_state_sha256 != baseline.state_sha256:
        raise ValueError("restore 1 does not close to the database baseline")
    _validate_restore_baseline_identity(first_restore, baseline, 1)

    if (
        isinstance(second.pass_number, bool)
        or second.pass_number != 2
        or isinstance(pass_two_number, bool)
        or pass_two_number != 2
    ):
        raise ValueError("final session pass 2 identity is invalid")
    if second.prior_restore_operation_id != first_restore.restore_operation_id:
        raise ValueError("pass 2 prior restore does not identify restore 1")
    if (
        second.prior_restore_bundle_manifest_sha256
        != first_restore.restore_bundle_manifest_sha256
        or second.prior_restore_bundle_canonical_sha256
        != first_restore.restore_bundle_canonical_sha256
    ):
        raise ValueError("pass 2 prior restore bundle does not identify restore 1")
    if (
        second.starting_database_state_sha256
        != first_restore.restored_database_state_sha256
        or second.starting_database_state_sha256 != baseline.state_sha256
        or second.starting_database_schema_sha256 != baseline.schema_sha256
        or pass_two_baseline != baseline.state_sha256
    ):
        raise ValueError("pass 2 did not start from restore 1")
    if (
        isinstance(second_restore.pass_number, bool)
        or second_restore.pass_number != 2
        or second_restore.pass_execution_id != second.execution_id
        or second_restore.pass_bundle_manifest_sha256
        != second.frontend_bundle_manifest_sha256
        or second_restore.pass_bundle_canonical_sha256
        != second.frontend_bundle_canonical_sha256
        or second_restore.previous_database_state_sha256
        != second.terminal_database_state_sha256
    ):
        raise ValueError("restore 2 does not follow pass 2")
    if second_restore.restored_database_state_sha256 != baseline.state_sha256:
        raise ValueError("restore 2 does not close to the database baseline")
    _validate_restore_baseline_identity(second_restore, baseline, 2)

    terminal_states = {
        first.terminal_database_state_sha256,
        second.terminal_database_state_sha256,
    }
    if len(terminal_states) != 2 or baseline.state_sha256 in terminal_states:
        raise ValueError("pass terminal database states are not distinct")
    if (
        first.frontend_bundle_manifest_sha256
        == second.frontend_bundle_manifest_sha256
        or first.frontend_bundle_canonical_sha256
        == second.frontend_bundle_canonical_sha256
    ):
        raise ValueError("frontend pass bundle identities are not distinct")
    if (
        first_restore.restore_bundle_manifest_sha256
        == second_restore.restore_bundle_manifest_sha256
        or first_restore.restore_bundle_canonical_sha256
        == second_restore.restore_bundle_canonical_sha256
    ):
        raise ValueError("restore bundle identities are not distinct")
    if (
        final.database_state_sha256
        != second_restore.restored_database_state_sha256
        or final.database_state_sha256 != baseline.state_sha256
        or final_baseline_fingerprint != final.database_state_sha256
        or final.database_schema_sha256 != baseline.schema_sha256
        or final.source_identity != baseline.source_identity
        or final.source_connection_identity != baseline.source_connection_identity
    ):
        raise ValueError("final database state does not close to the baseline")


def _validate_database_source_identity(identity: DatabaseSourceIdentity) -> None:
    if (
        not isinstance(identity, DatabaseSourceIdentity)
        or identity.backend != "postgresql"
        or not isinstance(identity.database, str)
        or not identity.database.strip()
    ):
        raise ValueError("database source identity is invalid")
    for value in (identity.host, identity.username):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError("database source identity is invalid")
    if identity.port is not None and (
        isinstance(identity.port, bool)
        or not isinstance(identity.port, int)
        or not 1 <= identity.port <= 65_535
    ):
        raise ValueError("database source identity is invalid")


def _validate_database_connection_identity(
    identity: DatabaseConnectionIdentityEvidence,
) -> None:
    if not isinstance(identity, DatabaseConnectionIdentityEvidence):
        raise ValueError("database source connection identity is invalid")
    values = (
        identity.database,
        identity.username,
        identity.server_address,
        identity.server_port,
        identity.system_identifier,
        identity.postgres_version,
    )
    if any(not isinstance(value, str) for value in values):
        raise ValueError("database source connection identity is invalid")
    if (
        not identity.database.strip()
        or not identity.username.strip()
        or not identity.system_identifier.isdigit()
        or int(identity.system_identifier) <= 0
        or not (
            identity.postgres_version == "16"
            or identity.postgres_version.startswith("16.")
        )
        or bool(identity.server_address) != bool(identity.server_port)
        or (
            identity.server_port
            and (
                not identity.server_port.isdigit()
                or not 1 <= int(identity.server_port) <= 65_535
            )
        )
    ):
        raise ValueError("database source connection identity is invalid")


def _validate_database_identity_pair(
    source: DatabaseSourceIdentity,
    connection: DatabaseConnectionIdentityEvidence,
) -> None:
    if (
        source.database != connection.database
        or source.username != connection.username
    ):
        raise ValueError("database URL and server-observed identities disagree")


def _validate_restore_baseline_identity(
    restore: ProductProvingRestoreEvidence,
    baseline: ProductProvingDatabaseBaselineEvidence,
    number: int,
) -> None:
    _validate_database_source_identity(restore.database_source_identity)
    _validate_database_connection_identity(
        restore.database_source_connection_identity
    )
    _validate_database_identity_pair(
        restore.database_source_identity,
        restore.database_source_connection_identity,
    )
    if (
        restore.database_baseline_manifest_sha256 != baseline.manifest_sha256
        or restore.database_baseline_dump_sha256 != baseline.dump_sha256
        or restore.database_baseline_state_sha256 != baseline.state_sha256
        or restore.database_baseline_schema_sha256 != baseline.schema_sha256
        or restore.database_source_identity != baseline.source_identity
        or restore.database_source_connection_identity
        != baseline.source_connection_identity
    ):
        raise ValueError(f"restore {number} uses a different database baseline")


def _validate_uuid4(value: object) -> None:
    try:
        parsed = UUID(str(value))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("final Product Proving operation id is not a UUID") from exc
    if str(parsed) != value or parsed.version != 4:
        raise ValueError("final Product Proving operation id is not a canonical UUID4")


def _parse_aware_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("final Product Proving operation time is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("final Product Proving operation time is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("final Product Proving operation time must be timezone-aware")
    return parsed


def _pass_field(
    value: ProductProvingPass | Mapping[str, Any], name: str
) -> object:
    if isinstance(value, ProductProvingPass):
        return getattr(value, name)
    if isinstance(value, Mapping):
        return value.get(name)
    return None


def verify_two_pass_capture(capture: ProductProvingCapture) -> None:
    """Recompute whether the capture earns ADR-0046's bounded claim."""

    verify_preflight(capture.expected, capture.observed)
    if not capture.simulated_practitioner:
        raise ValueError("the run must identify its simulated practitioner")
    if capture.same_project_manual_report_compared:
        raise ValueError("manual Report replacement is outside this proving claim")
    if capture.revision_processing_included:
        raise ValueError("Document revision processing is outside this proving run")
    for expected_number, run in enumerate(
        (capture.pass_one, capture.pass_two), start=1
    ):
        _verify_pass(capture.expected, run, expected_number)
    if (
        capture.final_baseline_fingerprint
        != capture.expected.baseline_fingerprint
    ):
        raise ValueError("final database restore does not match the sealed baseline")
    verify_final_session_evidence(
        capture.final_session_evidence,
        expected_baseline_fingerprint=capture.expected.baseline_fingerprint,
        pass_one=capture.pass_one,
        pass_two=capture.pass_two,
        final_baseline_fingerprint=capture.final_baseline_fingerprint,
    )
    _verify_equivalent_passes(capture.pass_one, capture.pass_two)


def publish_product_proving_bundle(
    output_dir: Path, capture: ProductProvingCapture
) -> ProductProvingBundleSummary:
    """Publish both terminal passes outside the database and self-verify them."""

    verify_two_pass_capture(capture)
    canonical = _capture_json(capture)
    receipt = {"schema_version": BUNDLE_SCHEMA_VERSION, **canonical}
    markdown = _receipt_markdown(canonical).encode()
    manifest_path, manifest_sha256, canonical_sha256 = publish_verified_bundle(
        Path(output_dir),
        exports={
            "canonical-content.json": canonical,
            "environment.json": {
                "schema_version": BUNDLE_SCHEMA_VERSION,
                "source_revision": capture.observed.source_revision,
                "origin_main_revision": capture.observed.origin_main_revision,
                "migration_head": capture.observed.migration_head,
            },
            "receipt.json": receipt,
            "receipt.md": markdown,
            "pass-1-approved-export.pdf": capture.pass_one.approved_export_bytes,
            "pass-2-approved-export.pdf": capture.pass_two.approved_export_bytes,
        },
        canonical_content=canonical,
        bundle_schema_version=BUNDLE_SCHEMA_VERSION,
        bundle_files=BUNDLE_FILES,
        error_cls=ValueError,
        corrupt_bundle_error_cls=CorruptProductProvingBundle,
        canonical_json=_canonical_json,
        sha256=_sha256,
        json_sha256=_json_sha256,
        temp_prefix="corridor-product-proving-run",
        self_verification_failure="new Product Proving bundle failed self-verification",
    )
    verify_product_proving_bundle(
        Path(output_dir),
        expected_integrity_manifest_sha256=manifest_sha256,
    )
    return ProductProvingBundleSummary(
        bundle_dir=Path(output_dir),
        manifest_path=manifest_path,
        integrity_manifest_sha256=manifest_sha256,
        canonical_content_sha256=canonical_sha256,
    )


def publish_product_proving_failure_bundle(
    output_dir: Path, capture: ProductProvingFailureCapture
) -> ProductProvingBundleSummary:
    """Seal a terminal failure without allowing success semantics."""

    verify_preflight(capture.expected, capture.observed)
    if capture.pass_number < 1:
        raise ValueError("failed Product Proving pass number must be positive")
    if not capture.phase or not capture.errors:
        raise ValueError("failed Product Proving receipt needs a phase and error")
    if capture.operations_elapsed_seconds < 0:
        raise ValueError("failed Product Proving timing must be non-negative")
    if not all(
        _is_sha256(value)
        for value in (
            capture.baseline_dump_sha256,
            capture.baseline_state_manifest_sha256,
            capture.restored_baseline_fingerprint,
        )
    ):
        raise ValueError("failed Product Proving restoration evidence is invalid")
    if capture.restored_baseline_fingerprint != capture.expected.baseline_fingerprint:
        raise ValueError("failed Product Proving pass did not restore its baseline")
    if capture.phase == "extraction_repeatability" and capture.admission_started:
        raise ValueError("repeatability failure must stop before Admission")
    canonical = asdict(capture)
    receipt = {
        "schema_version": BUNDLE_FAILURE_SCHEMA_VERSION,
        "status": "failed",
        **canonical,
    }
    manifest_path, manifest_sha256, canonical_sha256 = publish_verified_bundle(
        Path(output_dir),
        exports={
            "canonical-content.json": canonical,
            "environment.json": {
                "schema_version": BUNDLE_FAILURE_SCHEMA_VERSION,
                "source_revision": capture.observed.source_revision,
                "origin_main_revision": capture.observed.origin_main_revision,
                "migration_head": capture.observed.migration_head,
            },
            "receipt.json": receipt,
            "receipt.md": _failure_markdown(canonical).encode(),
        },
        canonical_content=canonical,
        bundle_schema_version=BUNDLE_FAILURE_SCHEMA_VERSION,
        bundle_files=FAILURE_BUNDLE_FILES,
        error_cls=ValueError,
        corrupt_bundle_error_cls=CorruptProductProvingBundle,
        canonical_json=_canonical_json,
        sha256=_sha256,
        json_sha256=_json_sha256,
        temp_prefix="corridor-product-proving-failure",
        self_verification_failure="new Product Proving failure bundle is invalid",
    )
    verify_product_proving_failure_bundle(
        Path(output_dir),
        expected_integrity_manifest_sha256=manifest_sha256,
    )
    return ProductProvingBundleSummary(
        bundle_dir=Path(output_dir),
        manifest_path=manifest_path,
        integrity_manifest_sha256=manifest_sha256,
        canonical_content_sha256=canonical_sha256,
    )


def verify_product_proving_bundle(
    bundle_dir: Path, *, expected_integrity_manifest_sha256: str
) -> VerificationResult:
    """Verify a closed receipt without PostgreSQL or the original checkout."""

    verified = verify_bundle(
        Path(bundle_dir),
        expected_integrity_manifest_sha256=expected_integrity_manifest_sha256,
        bundle_schema_version=BUNDLE_SCHEMA_VERSION,
        bundle_files=BUNDLE_FILES,
        corrupt_bundle_error_cls=CorruptProductProvingBundle,
        sha256=_sha256,
        json_sha256=_json_sha256,
    )
    try:
        canonical = json.loads((Path(bundle_dir) / "canonical-content.json").read_bytes())
        receipt = json.loads((Path(bundle_dir) / "receipt.json").read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise CorruptProductProvingBundle("Product Proving receipt is invalid") from exc
    if receipt != {"schema_version": BUNDLE_SCHEMA_VERSION, **canonical}:
        raise CorruptProductProvingBundle("receipt does not match canonical content")
    try:
        _verify_canonical_success_evidence(canonical)
    except (KeyError, TypeError, ValueError) as exc:
        raise CorruptProductProvingBundle(
            "final Product Proving session evidence is invalid"
        ) from exc
    for number in (1, 2):
        pdf = (Path(bundle_dir) / f"pass-{number}-approved-export.pdf").read_bytes()
        expected = canonical[f"pass_{'one' if number == 1 else 'two'}"][
            "approved_export_sha256"
        ]
        if not pdf.startswith(b"%PDF-") or _sha256(pdf) != expected:
            raise CorruptProductProvingBundle(
                f"pass {number} Approved Export does not match its digest"
            )
    return verified


def _verify_canonical_success_evidence(canonical: Mapping[str, Any]) -> None:
    expected = _require_mapping(canonical.get("expected"), "expected preflight")
    first = _require_mapping(canonical.get("pass_one"), "pass 1")
    second = _require_mapping(canonical.get("pass_two"), "pass 2")
    evidence = _final_session_evidence_from_json(
        canonical.get("final_session_evidence")
    )
    if (
        first.get("frontend_kind") != FRONTEND_EVIDENCE_KIND
        or second.get("frontend_kind") != FRONTEND_EVIDENCE_KIND
    ):
        raise ValueError("final receipt frontend evidence kind is invalid")
    verify_final_session_evidence(
        evidence,
        expected_baseline_fingerprint=expected.get("baseline_fingerprint"),
        pass_one=first,
        pass_two=second,
        final_baseline_fingerprint=canonical.get("final_baseline_fingerprint"),
    )
    first_signature = _frontend_action_signature(
        first.get("frontend_actions"),
        first.get("residual_candidate_ids"),
    )
    second_signature = _frontend_action_signature(
        second.get("frontend_actions"),
        second.get("residual_candidate_ids"),
    )
    if first_signature != second_signature:
        raise ValueError("final receipt frontend action behavior is not equivalent")


def _final_session_evidence_from_json(
    value: object,
) -> ProductProvingFinalSessionEvidence:
    root = _exact_mapping(
        value,
        {"baseline", "pass_one", "restore_one", "pass_two", "restore_two", "final_state"},
        "final session evidence",
    )
    baseline = _exact_mapping(
        root["baseline"],
        {
            "manifest_sha256",
            "dump_sha256",
            "state_sha256",
            "schema_sha256",
            "source_identity",
            "source_connection_identity",
        },
        "database baseline evidence",
    )
    pass_keys = {
        "execution_id",
        "pass_number",
        "prior_restore_operation_id",
        "prior_restore_bundle_manifest_sha256",
        "prior_restore_bundle_canonical_sha256",
        "started_at",
        "starting_database_state_sha256",
        "starting_database_schema_sha256",
        "frontend_bundle_manifest_sha256",
        "frontend_bundle_canonical_sha256",
        "terminal_database_state_sha256",
    }
    restore_keys = {
        "restore_operation_id",
        "pass_number",
        "pass_execution_id",
        "pass_bundle_manifest_sha256",
        "pass_bundle_canonical_sha256",
        "restore_bundle_manifest_sha256",
        "restore_bundle_canonical_sha256",
        "previous_database_state_sha256",
        "restored_database_state_sha256",
        "database_baseline_manifest_sha256",
        "database_baseline_dump_sha256",
        "database_baseline_state_sha256",
        "database_baseline_schema_sha256",
        "database_source_identity",
        "database_source_connection_identity",
        "started_at",
        "completed_at",
    }
    final = _exact_mapping(
        root["final_state"],
        {
            "database_state_sha256",
            "database_schema_sha256",
            "source_identity",
            "source_connection_identity",
        },
        "final database state evidence",
    )
    return ProductProvingFinalSessionEvidence(
        baseline=ProductProvingDatabaseBaselineEvidence(
            manifest_sha256=baseline["manifest_sha256"],
            dump_sha256=baseline["dump_sha256"],
            state_sha256=baseline["state_sha256"],
            schema_sha256=baseline["schema_sha256"],
            source_identity=_database_source_identity_from_json(
                baseline["source_identity"]
            ),
            source_connection_identity=_database_connection_identity_from_json(
                baseline["source_connection_identity"]
            ),
        ),
        pass_one=ProductProvingPassExecutionEvidence(
            **dict(_exact_mapping(root["pass_one"], pass_keys, "pass 1 evidence"))
        ),
        restore_one=_restore_evidence_from_json(
            root["restore_one"], restore_keys, "restore 1 evidence"
        ),
        pass_two=ProductProvingPassExecutionEvidence(
            **dict(_exact_mapping(root["pass_two"], pass_keys, "pass 2 evidence"))
        ),
        restore_two=_restore_evidence_from_json(
            root["restore_two"], restore_keys, "restore 2 evidence"
        ),
        final_state=ProductProvingFinalStateEvidence(
            database_state_sha256=final["database_state_sha256"],
            database_schema_sha256=final["database_schema_sha256"],
            source_identity=_database_source_identity_from_json(
                final["source_identity"]
            ),
            source_connection_identity=_database_connection_identity_from_json(
                final["source_connection_identity"]
            ),
        ),
    )


def _database_source_identity_from_json(value: object) -> DatabaseSourceIdentity:
    identity = _exact_mapping(
        value,
        {"backend", "host", "port", "database", "username"},
        "database source identity",
    )
    return DatabaseSourceIdentity(**dict(identity))


def _restore_evidence_from_json(
    value: object, expected_keys: set[str], label: str
) -> ProductProvingRestoreEvidence:
    raw = dict(_exact_mapping(value, expected_keys, label))
    source = _database_source_identity_from_json(raw.pop("database_source_identity"))
    connection = _database_connection_identity_from_json(
        raw.pop("database_source_connection_identity")
    )
    return ProductProvingRestoreEvidence(
        **raw,
        database_source_identity=source,
        database_source_connection_identity=connection,
    )


def _database_connection_identity_from_json(
    value: object,
) -> DatabaseConnectionIdentityEvidence:
    identity = _exact_mapping(
        value,
        {
            "database",
            "username",
            "server_address",
            "server_port",
            "system_identifier",
            "postgres_version",
        },
        "database source connection identity",
    )
    return DatabaseConnectionIdentityEvidence(**dict(identity))


def _exact_mapping(
    value: object, expected_keys: set[str], label: str
) -> Mapping[str, Any]:
    mapping = _require_mapping(value, label)
    if set(mapping) != expected_keys:
        raise ValueError(f"{label} has an invalid shape")
    return mapping


def _require_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} is not an object")
    return value


def verify_product_proving_failure_bundle(
    bundle_dir: Path, *, expected_integrity_manifest_sha256: str
) -> VerificationResult:
    """Verify a terminal failure and refuse any success-shaped receipt."""

    verified = verify_bundle(
        Path(bundle_dir),
        expected_integrity_manifest_sha256=expected_integrity_manifest_sha256,
        bundle_schema_version=BUNDLE_FAILURE_SCHEMA_VERSION,
        bundle_files=FAILURE_BUNDLE_FILES,
        corrupt_bundle_error_cls=CorruptProductProvingBundle,
        sha256=_sha256,
        json_sha256=_json_sha256,
    )
    try:
        canonical = json.loads((Path(bundle_dir) / "canonical-content.json").read_bytes())
        receipt = json.loads((Path(bundle_dir) / "receipt.json").read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise CorruptProductProvingBundle("failure receipt is invalid") from exc
    expected = {
        "schema_version": BUNDLE_FAILURE_SCHEMA_VERSION,
        "status": "failed",
        **canonical,
    }
    if receipt != expected:
        raise CorruptProductProvingBundle(
            "failure receipt does not match canonical content"
        )
    if not canonical.get("errors") or not canonical.get("phase"):
        raise CorruptProductProvingBundle("failure evidence is incomplete")
    if (
        canonical.get("phase") == "extraction_repeatability"
        and canonical.get("admission_started") is not False
    ):
        raise CorruptProductProvingBundle(
            "repeatability failure incorrectly claims Admission started"
        )
    restoration_fields = (
        "baseline_dump_sha256",
        "baseline_state_manifest_sha256",
        "restored_baseline_fingerprint",
    )
    if any(field in canonical for field in restoration_fields):
        if not all(_is_sha256(canonical.get(field)) for field in restoration_fields):
            raise CorruptProductProvingBundle(
                "failure restoration evidence is incomplete"
            )
        expected_baseline = (canonical.get("expected") or {}).get(
            "baseline_fingerprint"
        )
        if canonical["restored_baseline_fingerprint"] != expected_baseline:
            raise CorruptProductProvingBundle(
                "failure receipt does not prove exact baseline restoration"
            )
    return verified


def _verify_pass(
    expected: ExpectedPreflight, run: ProductProvingPass, expected_number: int
) -> None:
    if run.pass_number != expected_number:
        raise ValueError("Product Proving pass numbers are not consecutive")
    if run.restored_baseline_fingerprint != expected.baseline_fingerprint:
        raise ValueError(f"pass {run.pass_number} did not start from the sealed baseline")
    if run.extraction_failures:
        raise ValueError(f"pass {run.pass_number} has a failed Extraction Run")
    comparisons = {item.document_id: item for item in run.extraction_comparisons}
    if len(comparisons) != len(run.extraction_comparisons):
        raise ValueError(f"pass {run.pass_number} repeats a Document comparison")
    if set(comparisons) != set(expected.documents) or any(
        not item.equal for item in run.extraction_comparisons
    ):
        raise ValueError(
            f"pass {run.pass_number} failed Candidate semantic repeatability"
        )
    fresh_run_ids: set[int] = set()
    for document_id, comparison in comparisons.items():
        if comparison.baseline_run_id != expected.baseline_runs.get(document_id):
            raise ValueError(
                f"pass {run.pass_number} comparison does not use its pinned baseline Run"
            )
        if (
            comparison.fresh_run_id <= 0
            or comparison.fresh_run_id == comparison.baseline_run_id
            or comparison.fresh_run_id in fresh_run_ids
        ):
            raise ValueError(f"pass {run.pass_number} fresh Extraction Run is invalid")
        fresh_run_ids.add(comparison.fresh_run_id)
        if comparison.baseline_configuration != comparison.fresh_configuration:
            raise ValueError(
                f"pass {run.pass_number} compared incompatible extractor configurations"
            )
        configuration = comparison.baseline_configuration
        if (
            not configuration.prompt_version.strip()
            or not configuration.schema_version.strip()
            or any(
                not _is_sha256(value)
                for value in (
                    configuration.prompt_sha256,
                    configuration.schema_sha256,
                    configuration.postprocessor_sha256,
                    configuration.config_sha256,
                )
            )
            or any(not _is_sha256(value) for value in comparison.matched_sha256)
        ):
            raise ValueError(
                f"pass {run.pass_number} extraction configuration receipt is invalid"
            )
    run_receipts = {
        receipt.get("run_id"): receipt for receipt in run.extraction_run_receipts
    }
    if (
        len(run_receipts) != len(run.extraction_run_receipts)
        or set(run_receipts) != fresh_run_ids
    ):
        raise ValueError(f"pass {run.pass_number} Extraction Run receipts are incomplete")
    for document_id, comparison in comparisons.items():
        receipt = run_receipts[comparison.fresh_run_id]
        configuration = comparison.fresh_configuration
        usage = receipt.get("token_usage")
        config_json = receipt.get("extractor_config")
        config_shape_valid = False
        if isinstance(config_json, Mapping):
            try:
                validate_config_json_shape(config_json)
                config_shape_valid = True
            except (TypeError, ValueError):
                pass
        if (
            receipt.get("document_id") != document_id
            or receipt.get("outcome") != "completed"
            or receipt.get("page_errors") != 0
            or receipt.get("candidate_count") != len(comparison.matched_sha256)
            or receipt.get("prompt_version") != configuration.prompt_version
            or receipt.get("model") != configuration.model
            or receipt.get("schema_version") != configuration.schema_version
            or receipt.get("prompt_sha256") != configuration.prompt_sha256
            or receipt.get("schema_sha256") != configuration.schema_sha256
            or receipt.get("postprocessor_sha256")
            != configuration.postprocessor_sha256
            or receipt.get("extractor_config_sha256") != configuration.config_sha256
            or not isinstance(config_json, Mapping)
            or not config_shape_valid
            or _sha256(_canonical_json(config_json)) != configuration.config_sha256
            or any(
                config_json.get(name) != value
                for name, value in (
                    ("prompt_version", configuration.prompt_version),
                    ("model", configuration.model),
                    ("schema_version", configuration.schema_version),
                    ("prompt_sha256", configuration.prompt_sha256),
                    ("schema_sha256", configuration.schema_sha256),
                    ("postprocessor_sha256", configuration.postprocessor_sha256),
                )
            )
            or not _valid_token_usage(usage, document_id)
        ):
            raise ValueError(f"pass {run.pass_number} Extraction Run receipt is invalid")
    if not run.admission_completed:
        raise ValueError(f"pass {run.pass_number} stopped before Admission")
    residual_ids = set(run.residual_candidate_ids)
    if set(run.residual_outcomes) != residual_ids or not set(
        run.residual_outcomes.values()
    ) <= _OUTCOMES:
        raise ValueError(
            f"pass {run.pass_number} did not inspect every residual Candidate"
        )
    if run.frontend_kind != FRONTEND_EVIDENCE_KIND:
        raise ValueError("practitioner work has an invalid frontend evidence kind")
    _frontend_action_signature(run.frontend_actions, run.residual_candidate_ids)
    if run.workarounds:
        raise ValueError("practitioner phase used a terminal or database workaround")
    if not run.invalid_action_refused or run.invalid_action_write_set:
        raise ValueError("invalid frontend action was not refused atomically")
    if run.factual_correction_outcome not in _FACTUAL_CORRECTION_OUTCOMES:
        raise ValueError("factual correction outcome is invalid")
    if not run.work_decision_change_preserved_predecessor:
        raise ValueError("Work Decision change did not preserve its predecessor")
    if run.report_pdf_sha256 != run.approved_export_sha256:
        raise ValueError("Approved Export does not bind the exact reviewed Report")
    if _sha256(run.approved_export_bytes) != run.approved_export_sha256:
        raise ValueError("Approved Export bytes do not match the recorded digest")
    if not run.approved_export_bytes.startswith(b"%PDF-"):
        raise ValueError("Approved Export is not a PDF")
    provenance = set(run.report_provenance_classes)
    if (
        not provenance
        or not provenance <= _PROVENANCE_CLASSES
        or not {"Assertion", "Derivation", "Work Decision"} <= provenance
    ):
        raise ValueError("Report provenance classes do not match the bounded record")
    if run.operations_elapsed_seconds < 0 or run.practitioner_elapsed_seconds < 0:
        raise ValueError("Product Proving timings must be non-negative")
    missing_write_families = _MANDATORY_WRITE_FAMILIES - set(run.write_set)
    if missing_write_families:
        raise ValueError(
            "Product Proving write set omits mandatory families: "
            + ", ".join(sorted(missing_write_families))
        )
    _stable_write_set_signature(run.write_set)


def verify_product_proving_pass(
    expected: ExpectedPreflight, run: ProductProvingPass
) -> None:
    """Validate one sealed frontend pass without inventing the final chain."""

    if isinstance(run.pass_number, bool) or run.pass_number not in {1, 2}:
        raise ValueError("Product Proving pass number must be 1 or 2")
    _verify_pass(expected, run, run.pass_number)


def _verify_equivalent_passes(
    first: ProductProvingPass, second: ProductProvingPass
) -> None:
    first_semantics = {
        comparison.document_id: comparison.matched_sha256
        for comparison in first.extraction_comparisons
    }
    second_semantics = {
        comparison.document_id: comparison.matched_sha256
        for comparison in second.extraction_comparisons
    }
    if first_semantics != second_semantics:
        raise ValueError("the second pass changed Candidate semantic outputs")
    if Counter(first.residual_outcomes.values()) != Counter(
        second.residual_outcomes.values()
    ):
        raise ValueError("the second pass changed residual outcome behavior")
    if _stable_write_set_signature(first.write_set) != _stable_write_set_signature(
        second.write_set
    ):
        raise ValueError("the second pass changed declared write-set behavior")
    if first.factual_correction_outcome != second.factual_correction_outcome:
        raise ValueError("the second pass changed factual-correction support")
    if set(first.report_provenance_classes) != set(second.report_provenance_classes):
        raise ValueError("the second pass changed Report provenance behavior")
    if _frontend_action_signature(
        first.frontend_actions, first.residual_candidate_ids
    ) != _frontend_action_signature(
        second.frontend_actions, second.residual_candidate_ids
    ):
        raise ValueError("the second pass changed frontend action behavior")


def _frontend_action_signature(
    actions: Sequence[str], residual_candidate_ids: Sequence[int]
) -> tuple[str, ...]:
    """Validate route-shaped actions and erase only generated subject ids."""

    if (
        not isinstance(actions, (tuple, list))
        or not actions
        or any(not isinstance(action, str) for action in actions)
        or len(set(actions)) != len(actions)
    ):
        raise ValueError("Product Proving frontend actions are invalid")
    residual_ids = tuple(residual_candidate_ids)
    if (
        len(set(residual_ids)) != len(residual_ids)
        or any(
            isinstance(candidate_id, bool)
            or not isinstance(candidate_id, int)
            or candidate_id <= 0
            for candidate_id in residual_ids
        )
    ):
        raise ValueError("Product Proving frontend actions have invalid residual subjects")

    parsed: list[tuple[str, int | None]] = []
    for action in actions:
        match = _FRONTEND_ACTION.fullmatch(action)
        if match is None:
            raise ValueError("Product Proving frontend actions are not route-shaped")
        route = match.group("route")
        subject_text = match.group("subject")
        if route == "refuse_invalid_action":
            if subject_text is not None:
                raise ValueError("Product Proving frontend actions are invalid")
            parsed.append((route, None))
            continue
        if route not in _SUBJECT_FRONTEND_ACTIONS or subject_text is None:
            raise ValueError("Product Proving frontend actions are invalid")
        parsed.append((route, int(subject_text)))

    routes = [route for route, _ in parsed]
    if routes.count("refuse_invalid_action") != 1:
        raise ValueError("Product Proving frontend actions omit the refused action")
    for route in ("review_report", "release_approved_export"):
        if routes.count(route) != 1:
            raise ValueError(f"Product Proving frontend actions omit {route}")
    if routes.count("change_work_decision") < 1:
        raise ValueError("Product Proving frontend actions omit the Work Decision change")

    reviewed = [subject for route, subject in parsed if route == "review_candidate"]
    outcomes = [
        subject for route, subject in parsed if route in _RESIDUAL_FRONTEND_ACTIONS
    ]
    if Counter(reviewed) != Counter(residual_ids) or Counter(outcomes) != Counter(
        residual_ids
    ):
        raise ValueError(
            "Product Proving frontend actions do not cover every residual Candidate"
        )
    return tuple(routes)


def _canonical_candidate(
    expected_document_id: int, candidate: Mapping[str, Any]
) -> dict[str, Any]:
    document_id = candidate.get("source_document_id")
    if document_id != expected_document_id:
        raise ValueError("Candidate belongs to a different source Document")
    payload = candidate.get("payload_json")
    if not isinstance(payload, Mapping):
        raise ValueError("Candidate payload is absent or invalid")
    fields = payload.get("fields")
    citations = payload.get("citations")
    if not isinstance(fields, Mapping) or not isinstance(citations, list):
        raise ValueError("Candidate facts or Evidence are absent or invalid")
    evidence = []
    for citation in citations:
        if not isinstance(citation, Mapping):
            raise ValueError("Candidate Evidence is invalid")
        evidence.append(
            {
                "document_id": citation.get("document_id"),
                "page": citation.get("page"),
                "quote": citation.get("quote"),
                "verified": citation.get("verified"),
                "whole_row": citation.get("whole_row"),
            }
        )
    return {
        "document_id": document_id,
        "kind": candidate.get("kind"),
        "fields": dict(fields),
        "evidence": sorted(evidence, key=_canonical_json),
        "source_pages": sorted(candidate.get("source_pages") or []),
        "prompt_version": candidate.get("prompt_version"),
        "model": candidate.get("model"),
        "citations_verified": candidate.get("citations_verified"),
    }


_SEPARATELY_VERIFIED_WRITE_TABLES = frozenset(
    {
        "active_extraction_runs",
        "active_run_declarations",
        "candidates",
        "external_report_artifacts",
        "external_report_releases",
        "extraction_runs",
    }
)


def _stable_write_set_signature(
    write_set: Mapping[str, Sequence[Any]],
) -> Counter[tuple[str, str, str, str]]:
    """Compare domain writes after separately verified receipt semantics.

    Extraction/Candidate semantics, residual outcomes, frontend review subjects,
    and per-pass fixed-byte Report binding are verified independently before this
    signature is compared. Their receipt rows may still carry pass-specific model
    confidence, token usage, or rendered bytes, so this layer compares their exact
    table/operation counts. All remaining Project Record writes retain full stable
    content comparison.
    """
    signature: Counter[tuple[str, str, str, str]] = Counter()
    if not write_set:
        raise ValueError("Product Proving write set is absent")
    for table_name, changes in write_set.items():
        if not isinstance(table_name, str) or not table_name or not isinstance(
            changes, Sequence
        ):
            raise ValueError("Product Proving write set is invalid")
        for change in changes:
            if not isinstance(change, Mapping):
                raise ValueError("Product Proving write-set change is invalid")
            operation = change.get("operation")
            if operation in {"created", "deleted"}:
                stable = change.get("stable_content_sha256")
                if not _is_sha256(stable):
                    raise ValueError("Product Proving write-set digest is invalid")
                compared = (
                    "separately-verified"
                    if table_name in _SEPARATELY_VERIFIED_WRITE_TABLES
                    else str(stable)
                )
                signature[(table_name, str(operation), compared, "")] += 1
                continue
            if operation == "updated":
                before = change.get("before")
                after = change.get("after")
                before_stable = (
                    before.get("stable_content_sha256")
                    if isinstance(before, Mapping)
                    else None
                )
                after_stable = (
                    after.get("stable_content_sha256")
                    if isinstance(after, Mapping)
                    else None
                )
                if not _is_sha256(before_stable) or not _is_sha256(after_stable):
                    raise ValueError("Product Proving write-set digest is invalid")
                if table_name in _SEPARATELY_VERIFIED_WRITE_TABLES:
                    before_stable = "separately-verified"
                    after_stable = ""
                signature[
                    (table_name, "updated", str(before_stable), str(after_stable))
                ] += 1
                continue
            raise ValueError("Product Proving write-set operation is invalid")
    if not signature:
        raise ValueError("Product Proving write set records no changes")
    return signature


def _expanded_difference(counter: Counter[bytes]) -> tuple[dict[str, Any], ...]:
    return tuple(
        json.loads(value)
        for value, count in sorted(counter.items())
        for _ in range(count)
    )


def _capture_json(capture: ProductProvingCapture) -> dict[str, Any]:
    value = asdict(capture)
    for name in ("pass_one", "pass_two"):
        value[name].pop("approved_export_bytes")
        value[name]["extraction_comparisons"] = [
            asdict(comparison)
            for comparison in getattr(capture, name).extraction_comparisons
        ]
    return value


def _receipt_markdown(canonical: Mapping[str, Any]) -> str:
    first = canonical["pass_one"]
    second = canonical["pass_two"]
    session = canonical["final_session_evidence"]
    baseline = session["baseline"]
    return "\n".join(
        (
            "# SH99 bounded Product Proving Run",
            "",
            "Status: **passed**",
            "",
            "This receipt records two consecutive complete passes through server-observed "
            "Corridor frontend routes with retained browser screenshots under a simulated "
            "practitioner. It is product testing,",
            "not independent practitioner validation.",
            "The formal evidence does not cryptographically distinguish a browser from ",
            "another HTTP client; the actual proving run is separately operated through ",
            "Computer Use.",
            "",
            "No same-project manual Report was compared. This run does not prove replacement ",
            "of a project's manual weekly Report.",
            "",
            f"- Pass 1 Approved Export: `{first['approved_export_sha256']}`",
            f"- Pass 1 execution: `{session['pass_one']['execution_id']}`",
            f"- Restore 1: `{session['restore_one']['restore_operation_id']}`",
            f"- Pass 2 Approved Export: `{second['approved_export_sha256']}`",
            f"- Pass 2 execution: `{session['pass_two']['execution_id']}`",
            f"- Restore 2: `{session['restore_two']['restore_operation_id']}`",
            f"- Baseline manifest: `{baseline['manifest_sha256']}`",
            f"- Baseline schema: `{baseline['schema_sha256']}`",
            f"- Restored baseline: `{canonical['final_baseline_fingerprint']}`",
            "- Document revision processing: excluded",
            "",
        )
    )


def _failure_markdown(canonical: Mapping[str, Any]) -> str:
    comparisons = canonical.get("extraction_comparisons") or []
    return "\n".join(
        (
            "# SH99 bounded Product Proving Run failure",
            "",
            "Status: **failed**",
            "",
            f"- Attempted pass: `{canonical['pass_number']}`",
            f"- Terminal phase: `{canonical['phase']}`",
            f"- Admission started: `{str(canonical['admission_started']).lower()}`",
            f"- Fresh Extraction Runs retained: `{len(canonical['extraction_run_receipts'])}`",
            f"- Candidate comparisons retained: `{len(comparisons)}`",
            f"- Restored baseline: `{canonical['restored_baseline_fingerprint']}`",
            "",
            "Errors:",
            *[f"- {error}" for error in canonical["errors"]],
            "",
            "This receipt is immutable failure evidence under a simulated practitioner ",
            "claim boundary. It grants no operational authority and supports no workflow ",
            "completeness or manual Report replacement claim.",
            "",
        )
    )


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _valid_token_usage(value: object, document_id: int) -> bool:
    if not isinstance(value, Mapping):
        return False
    scope = value.get("scope")
    members = value.get("document_ids")
    if (
        scope not in {"run", "batch"}
        or not isinstance(members, list)
        or any(
            isinstance(item, bool) or not isinstance(item, int) or item <= 0
            for item in members
        )
        or len(set(members)) != len(members)
        or document_id not in members
        or (scope == "run" and members != [document_id])
        or (scope == "batch" and len(members) < 2)
    ):
        return False
    if value.get("measurement") == "unavailable":
        return bool(str(value.get("reason") or "").strip())
    return value.get("measurement") == "exact" and all(
        not isinstance(value.get(name), bool)
        and isinstance(value.get(name), int)
        and value[name] >= 0
        for name in (
            "prompt_tokens",
            "completion_tokens",
            "reasoning_tokens",
            "cached_tokens",
        )
    )


def _sha256(value: bytes) -> str:
    return sha256(value).hexdigest()


def _json_sha256(value: Any) -> str:
    return _sha256(_canonical_json(value))
