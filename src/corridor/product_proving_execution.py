"""Live observation and bounded operations for Product Proving.

The receipt contract in :mod:`corridor.product_proving_run` verifies facts it
is given.  This module owns the other half of that boundary: observing those
facts from the checkout and Project Record, reading immutable Extraction Run
inputs, and measuring the rows the proving pass actually changed.

Nothing here accepts caller-authored "observed" JSON.  The one injected value
is the canonical database fingerprint produced by the database-baseline seam;
all remaining observations are recomputed at the point of use.
"""

from __future__ import annotations

from corridor import digests
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import subprocess
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import and_, false, or_, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from corridor import dependency_admission, event_admission, policy
from corridor.extraction_runs import extractor_configuration
from corridor.extractor_lineage import canonical_json_bytes, validate_config_json_shape
from corridor.facts import proposal_input_snapshots
from corridor.models import (
    ActiveExtractionRun,
    ActiveRunDeclaration,
    Assertion,
    AuditLog,
    AutomaticCarryForwardOutcome,
    AutomaticCarryForwardReceipt,
    Candidate,
    CandidateDisposition,
    CohortReceipt,
    CommitmentLineage,
    Dependency,
    DependencyAdmissionOutcome,
    DependencyDismissal,
    DependencyEventEvidence,
    DependencyEventMigrationReceipt,
    DependencyEventScope,
    DependencyEventScopeDecision,
    DependencyEventTiming,
    DependencyEvidenceSufficiency,
    DocumentationFieldConfirmation,
    DisputeSettlement,
    DocPage,
    Document,
    DocumentQuarantine,
    DocumentRenditionDerivation,
    EventAdmissionAcceptanceReceipt,
    EventAdmissionActivation,
    EventAdmissionOutcome,
    EventCohortReceipt,
    EvidenceInvestigationCandidateReviewStart,
    EvidenceInvestigationEvaluationReceipt,
    EvidenceInvestigationPacketReceipt,
    EvidenceInvestigationReviewObservation,
    EvidenceInvestigationRun,
    EvidenceInvestigationShadowCase,
    EvidenceInvestigationShadowExecution,
    EvidenceInvestigationShadowOutcome,
    EvidenceInvestigationStepReceipt,
    EvidenceLink,
    ExternalParty,
    ExternalPartyStatement,
    ExternalReportArtifact,
    ExternalReportRelease,
    ExtractionRun,
    ExtractionMeasurementCaseState,
    LegacyLedgerArchive,
    Milestone,
    MilestoneRegistration,
    OperativeSupport,
    PolicyApproval,
    PolicyRun,
    Project,
    ProjectRosterEntry,
    ReconfirmationReceipt,
    ReportRun,
    RetiredDependencyStatus,
    RevisionComparisonFinding,
    RevisionComparisonRun,
    StatementCoordinationReceipt,
    StatementCoordinationReversal,
    StatementCoordinationReversalEffect,
    WorkDecision,
    WorkDecisionMilestoneImpact,
)
from corridor.product_proving_run import (
    CandidateSetComparison,
    ExpectedPreflight,
    ExtractionConfiguration,
    ObservedPreflight,
    compare_candidate_sets,
    verify_preflight,
)
from corridor.product_proving_database import (
    DatabaseFingerprint,
    VerifiedProductProvingDatabaseBaseline,
    fingerprint_database_url,
    observe_database_connection_identity,
)


@dataclass(frozen=True)
class GitCheckoutObservation:
    """The three Git facts Product Proving pins before its first write."""

    source_revision: str
    origin_main_revision: str
    clean_worktree: bool


@dataclass(frozen=True)
class ExtractionRunCandidateSet:
    """One completed Extraction Run and its immutable extractor-time inputs."""

    run_id: int
    document_id: int
    configuration: ExtractionConfiguration
    candidates: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class ProjectRowFingerprint:
    """Exact identity and both exact and pass-equivalent row content digests."""

    identity: Mapping[str, Any]
    content_sha256: str
    stable_content_sha256: str


@dataclass(frozen=True)
class ProjectWriteSetSnapshot:
    """All protected Product Proving families for one exact Project."""

    project_id: int
    project_slug: str
    rows: Mapping[str, tuple[ProjectRowFingerprint, ...]]


@dataclass(frozen=True)
class ProjectWriteSetDiff:
    """Rows created, deleted, or changed between two live captures."""

    project_id: int
    project_slug: str
    created: Mapping[str, tuple[ProjectRowFingerprint, ...]]
    deleted: Mapping[str, tuple[ProjectRowFingerprint, ...]]
    updated: Mapping[
        str, tuple[tuple[ProjectRowFingerprint, ProjectRowFingerprint], ...]
    ]

    @property
    def empty(self) -> bool:
        return not any(self.created.values()) and not any(
            self.deleted.values()
        ) and not any(self.updated.values())

    def as_write_set(self) -> dict[str, list[dict[str, Any]]]:
        """Render the measured diff for ``ProductProvingPass.write_set``."""

        output: dict[str, list[dict[str, Any]]] = {}
        for table_name in sorted(
            set(self.created) | set(self.deleted) | set(self.updated)
        ):
            changes: list[dict[str, Any]] = []
            changes.extend(
                {"operation": "created", **_fingerprint_json(item)}
                for item in self.created.get(table_name, ())
            )
            changes.extend(
                {"operation": "deleted", **_fingerprint_json(item)}
                for item in self.deleted.get(table_name, ())
            )
            changes.extend(
                {
                    "operation": "updated",
                    "before": _fingerprint_json(before),
                    "after": _fingerprint_json(after),
                }
                for before, after in self.updated.get(table_name, ())
            )
            if changes:
                output[table_name] = changes
        return output

    def stable_change_counts(self) -> Mapping[str, Mapping[str, int]]:
        """ID-independent multisets suitable for comparing restored passes."""

        result: dict[str, dict[str, int]] = {}
        for table_name in sorted(
            set(self.created) | set(self.deleted) | set(self.updated)
        ):
            digests = [
                f"created:{item.stable_content_sha256}"
                for item in self.created.get(table_name, ())
            ]
            digests.extend(
                f"deleted:{item.stable_content_sha256}"
                for item in self.deleted.get(table_name, ())
            )
            for before, after in self.updated.get(table_name, ()):
                digests.extend(
                    (
                        f"updated-before:{before.stable_content_sha256}",
                        f"updated-after:{after.stable_content_sha256}",
                    )
                )
            if digests:
                result[table_name] = dict(sorted(Counter(digests).items()))
        return result


@dataclass(frozen=True)
class BoundedProductProvingOperations:
    """Terminal result of extraction comparison and, only on a pass, Admission."""

    project_id: int
    fresh_run_ids: Mapping[int, int]
    extraction_comparisons: tuple[CandidateSetComparison, ...]
    extraction_failures: tuple[str, ...]
    active_run_document_ids: tuple[int, ...]
    admission_started: bool
    admission_result: Any | None
    observed_active_runs: Mapping[int, int]
    admission_policy_receipts: tuple["AdmissionPolicyReceipt", ...]
    residual_candidate_ids: tuple[int, ...]
    before_write_set: ProjectWriteSetSnapshot
    after_write_set: ProjectWriteSetSnapshot
    write_set: ProjectWriteSetDiff

    @property
    def extraction_equal(self) -> bool:
        return not self.extraction_failures and all(
            comparison.equal for comparison in self.extraction_comparisons
        )


@dataclass(frozen=True)
class AdmissionPolicyReceipt:
    """A newly committed Admission Policy Run re-read after durability."""

    run_id: int
    family: str
    policy_version: str
    policy_sha256: str
    applied_count: int
    abstained_count: int


@dataclass(frozen=True)
class LiveProductProvingOperationsCapture:
    """Caller-held pins joined to observations produced by the live seam."""

    expected: ExpectedPreflight
    observed: ObservedPreflight
    operations: BoundedProductProvingOperations
    database_baseline_manifest_sha256: str
    database_baseline_dump_sha256: str
    database_baseline_state_sha256: str
    database_baseline_fingerprint: DatabaseFingerprint
    database_source_identity: Mapping[str, object]
    database_source_connection_identity: Mapping[str, object]
    pass_number: int
    execution_id: str
    started_at: str
    prior_restore_operation_id: str | None
    prior_restore_bundle_manifest_sha256: str | None
    prior_restore_bundle_canonical_sha256: str | None


class GitObserver(Protocol):
    def __call__(self, repo_root: Path) -> GitCheckoutObservation: ...


ExtractionOperation = Callable[[Session, Document], int | ExtractionRun]
ActiveRunOperation = Callable[[Session, int, int], Any]
AdmissionOperation = Callable[[Session, int], Any]
CommitOperation = Callable[[Session], None]


def observe_git_checkout(repo_root: Path | str) -> GitCheckoutObservation:
    """Read HEAD, origin/main, and porcelain status from the actual checkout."""

    root = Path(repo_root).resolve()

    def git(*arguments: str) -> str:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise ValueError(f"cannot observe Git checkout: {detail}")
        return completed.stdout.strip()

    git("fetch", "--quiet", "origin", "main")
    source_revision = git("rev-parse", "HEAD")
    origin_main_revision = git("rev-parse", "origin/main")
    if not _is_git_revision(source_revision) or not _is_git_revision(
        origin_main_revision
    ):
        raise ValueError("Git checkout did not produce exact commit revisions")
    status = git("status", "--porcelain", "--untracked-files=normal")
    return GitCheckoutObservation(
        source_revision=source_revision,
        origin_main_revision=origin_main_revision,
        clean_worktree=not status,
    )


def _observe_product_proving_preflight_from_fingerprint(
    session: Session,
    *,
    project_slug: str,
    document_ids: Sequence[int],
    baseline_fingerprint: str | Callable[[], str],
    repo_root: Path | str | None = None,
    git_observer: GitObserver = observe_git_checkout,
) -> ObservedPreflight:
    """Construct ``ObservedPreflight`` entirely from live authoritative state.

    ``baseline_fingerprint`` is the canonical digest returned by the guarded
    database-baseline module.  It may be supplied directly or through a reader
    callable so a proving runner can fingerprint the database at this exact
    boundary.  No other observed field is accepted from a caller.
    """

    root = (
        Path(repo_root).resolve()
        if repo_root is not None
        else Path(__file__).resolve().parents[2]
    )
    git = git_observer(root)
    project = _require_project(session, project_slug)
    selected_documents = _exact_documents(
        session, project.id, document_ids
    )
    active_runs: dict[int, int] = {}
    for document in selected_documents:
        active = session.get(ActiveExtractionRun, document.id)
        if active is None:
            raise ValueError(
                f"Document {document.id} has no declared Active Extraction Run"
            )
        active_runs[document.id] = active.extraction_run_id

    dependency_document_ids = dependency_admission.declared_matrix_document_ids(
        session, project.id
    )
    dependency_policy = dependency_admission._canonical_policy(  # noqa: SLF001
        session, project, dependency_document_ids
    )
    event_policy_version = event_admission.normal_event_admission_policy_version(
        session, project.id
    )
    event_policy = event_admission.canonical_event_admission_policy(
        project, event_policy_version
    )
    policy_digests = {
        "dependency-admission": policy.canonical_sha256(dependency_policy),
        "event-admission": policy.canonical_sha256(event_policy),
    }

    milestone_sources: dict[str, str] = {}
    milestones = session.scalars(
        select(Milestone)
        .where(Milestone.project_id == project.id)
        .order_by(Milestone.code)
    ).all()
    for milestone in milestones:
        if milestone.current_registration_id is None:
            raise ValueError(
                f"Milestone {milestone.code!r} has no current Registration"
            )
        registration = session.get(
            MilestoneRegistration, milestone.current_registration_id
        )
        if registration is None or registration.milestone_id != milestone.id:
            raise ValueError(
                f"Milestone {milestone.code!r} current Registration is invalid"
            )
        milestone_sources[milestone.code] = (
            registration.source_sha256
            or _legacy_milestone_source_sha256(registration)
        )

    fingerprint = (
        baseline_fingerprint()
        if callable(baseline_fingerprint)
        else baseline_fingerprint
    )
    if not _is_sha256(fingerprint):
        raise ValueError("canonical database baseline fingerprint is invalid")

    return ObservedPreflight(
        source_revision=git.source_revision,
        origin_main_revision=git.origin_main_revision,
        clean_worktree=git.clean_worktree,
        migration_head=_live_migration_head(session),
        policy_digests=policy_digests,
        documents={item.id: item.sha256 for item in selected_documents},
        baseline_runs=active_runs,
        milestone_sources=milestone_sources,
        baseline_fingerprint=fingerprint,
    )


def observe_product_proving_preflight(
    session: Session,
    *,
    project_slug: str,
    document_ids: Sequence[int],
    verified_baseline: VerifiedProductProvingDatabaseBaseline,
    database_url: str,
    repo_root: Path | str | None = None,
    git_observer: GitObserver = observe_git_checkout,
    _fingerprint_for_test: Callable[[str], DatabaseFingerprint] | None = None,
) -> ObservedPreflight:
    """Observe preflight only against a verified baseline and its live database."""
    read_fingerprint = _fingerprint_for_test or fingerprint_database_url
    live_fingerprint = read_fingerprint(database_url)
    if live_fingerprint != verified_baseline.fingerprint:
        raise ValueError("live database does not match the verified proving baseline")
    baseline_source = verified_baseline.baseline.get("source_database") or {}
    baseline_connection = verified_baseline.baseline.get("source_connection") or {}
    require_proving_session_database_identity(
        session,
        database_url=database_url,
        expected_identity=baseline_source,
    )
    if observe_database_connection_identity(database_url).as_dict() != baseline_connection:
        raise ValueError("live PostgreSQL server identity differs from the baseline")
    observed = _observe_product_proving_preflight_from_fingerprint(
        session,
        project_slug=project_slug,
        document_ids=document_ids,
        baseline_fingerprint=live_fingerprint.state_sha256,
        repo_root=repo_root,
        git_observer=git_observer,
    )
    checkout = verified_baseline.baseline.get("checkout") or {}
    if (
        checkout.get("revision") != observed.source_revision
        or checkout.get("migration_head") != observed.migration_head
    ):
        raise ValueError("verified baseline checkout pins do not match live preflight")
    return observed


def proving_database_identity(database_url: str) -> dict[str, object]:
    """Return the exact non-secret PostgreSQL identity used by proving receipts."""
    parsed = make_url(database_url)
    if (
        parsed.get_backend_name() != "postgresql"
        or not parsed.database
        or not parsed.username
        or parsed.query
    ):
        raise ValueError("Product Proving database URL identity is unsafe")
    return {
        "backend": parsed.get_backend_name(),
        "host": parsed.host,
        "port": parsed.port,
        "database": parsed.database,
        "username": parsed.username,
    }


def require_proving_session_database_identity(
    session: Session,
    *,
    database_url: str,
    expected_identity: Mapping[str, object],
) -> None:
    """Bind a live Session and its configured engine to one exact database URL."""

    current_database, current_user = session.execute(
        text("select current_database(), current_user")
    ).one()
    bound = session.get_bind()
    bound_engine = getattr(bound, "engine", bound)
    bound_url = getattr(bound_engine, "url", None)
    if (
        not current_database
        or current_database != expected_identity.get("database")
        or current_user != expected_identity.get("username")
        or proving_database_identity(database_url) != dict(expected_identity)
        or bound_url is None
        or proving_database_identity(str(bound_url)) != dict(expected_identity)
    ):
        raise ValueError("live Session does not use the verified baseline database")


def load_extraction_run_candidate_set(
    session: Session,
    run_id: int,
) -> ExtractionRunCandidateSet:
    """Load one immutable completed run from its extractor-time receipt."""

    run = session.get(ExtractionRun, run_id)
    if run is None:
        raise ValueError(f"Extraction Run {run_id} does not exist")
    if run.outcome != "completed" or run.page_errors != 0:
        raise ValueError(f"Extraction Run {run_id} is not complete without errors")
    snapshot_values = (
        run.candidate_inputs_json
        if isinstance(run.candidate_inputs_json, list)
        else proposal_input_snapshots(session, run)
    )
    if not isinstance(snapshot_values, list) or (
        run.candidate_count and not snapshot_values
    ):
        raise ValueError(
            f"Extraction Run {run_id} has no immutable Candidate input snapshot"
        )
    if run.candidate_count != len(snapshot_values):
        raise ValueError(
            f"Extraction Run {run_id} Candidate input count does not match its receipt"
        )
    if not run.prompt_version or not run.schema_version:
        raise ValueError(
            f"Extraction Run {run_id} has incomplete extractor configuration"
        )
    candidates: tuple[Mapping[str, Any], ...] = tuple(
        _require_candidate_input(run, value)
        for value in snapshot_values
    )
    # Stored once by digest and referenced by the run (#605).
    config_json = extractor_configuration(session, run)
    if (
        not isinstance(config_json, dict)
        or not run.prompt_sha256
        or not run.schema_sha256
        or not run.postprocessor_sha256
        or not run.extractor_config_sha256
        or not isinstance(run.token_usage_json, dict)
    ):
        raise ValueError(f"Extraction Run {run_id} has no extractor-time config receipt")
    validate_config_json_shape(config_json)
    if sha256(canonical_json_bytes(config_json)).hexdigest() != run.extractor_config_sha256:
        raise ValueError(f"Extraction Run {run_id} config receipt digest is invalid")
    expected_config = {
        "prompt_version": run.prompt_version,
        "model": run.model,
        "schema_version": run.schema_version,
        "prompt_sha256": run.prompt_sha256,
        "schema_sha256": run.schema_sha256,
        "postprocessor_sha256": run.postprocessor_sha256,
    }
    if any(config_json.get(name) != value for name, value in expected_config.items()):
        raise ValueError(f"Extraction Run {run_id} config receipt disagrees with its row")
    configuration = ExtractionConfiguration(
        prompt_version=run.prompt_version,
        model=run.model,
        schema_version=run.schema_version,
        prompt_sha256=run.prompt_sha256,
        schema_sha256=run.schema_sha256,
        postprocessor_sha256=run.postprocessor_sha256,
        config_sha256=run.extractor_config_sha256,
    )
    return ExtractionRunCandidateSet(
        run_id=run.id,
        document_id=run.document_id,
        configuration=configuration,
        candidates=candidates,
    )


def compare_extraction_runs(
    session: Session,
    baseline_run_id: int,
    fresh_run_id: int,
) -> CandidateSetComparison:
    """Compare two completed attempts for the same exact Document."""

    baseline = load_extraction_run_candidate_set(session, baseline_run_id)
    fresh = load_extraction_run_candidate_set(session, fresh_run_id)
    if baseline.document_id != fresh.document_id:
        raise ValueError("Extraction Runs do not belong to the same Document")
    return compare_candidate_sets(
        document_id=baseline.document_id,
        baseline_run_id=baseline.run_id,
        fresh_run_id=fresh.run_id,
        baseline=baseline.candidates,
        fresh=fresh.candidates,
        baseline_configuration=baseline.configuration,
        fresh_configuration=fresh.configuration,
    )


def capture_project_write_set(
    session: Session, project_slug: str
) -> ProjectWriteSetSnapshot:
    """Read every #320 protected family from the actual Project Record."""

    project = _require_project(session, project_slug)
    documents = _rows(
        session, Document, Document.project_id == project.id
    )
    document_ids = _ids(documents)
    extraction_runs = _rows(
        session, ExtractionRun, ExtractionRun.document_id.in_(document_ids)
    )
    extraction_run_ids = _ids(extraction_runs)
    candidates = _rows(
        session, Candidate, Candidate.project_id == project.id
    )
    candidate_ids = _ids(candidates)
    dependencies = _rows(
        session, Dependency, Dependency.project_id == project.id
    )
    dependency_ids = _ids(dependencies)
    statements = _rows(
        session,
        ExternalPartyStatement,
        ExternalPartyStatement.project_id == project.id,
    )
    statement_ids = _ids(statements)
    external_party_ids = tuple(
        sorted(
            {
                value
                for value in (
                    *(dependency.external_org_id for dependency in dependencies),
                    *(
                        statement.affected_external_org_id
                        for statement in statements
                    ),
                    *(
                        statement.stated_external_org_id
                        for statement in statements
                    ),
                )
                if value is not None
            }
        )
    )
    lineages = _rows(
        session,
        CommitmentLineage,
        CommitmentLineage.project_id == project.id,
    )
    lineage_ids = _ids(lineages)
    milestones = _rows(
        session, Milestone, Milestone.project_id == project.id
    )
    milestone_ids = _ids(milestones)
    policy_runs = _rows(
        session, PolicyRun, PolicyRun.project_id == project.id
    )
    policy_run_ids = _ids(policy_runs)
    dispositions = _rows(
        session,
        CandidateDisposition,
        CandidateDisposition.candidate_id.in_(candidate_ids),
    )
    disposition_ids = _ids(dispositions)
    measurement_case_states = _rows(
        session,
        ExtractionMeasurementCaseState,
        ExtractionMeasurementCaseState.project_id == project.id,
    )
    work_decisions = _rows(
        session,
        WorkDecision,
        or_(
            WorkDecision.dependency_id.in_(dependency_ids),
            WorkDecision.commitment_lineage_id.in_(lineage_ids),
        ),
    )
    work_decision_ids = _ids(work_decisions)
    scope_decisions = _rows(
        session,
        DependencyEventScopeDecision,
        DependencyEventScopeDecision.event_id.in_(statement_ids),
    )
    scope_decision_ids = _ids(scope_decisions)
    scope_links = _rows(
        session,
        DependencyEventScope,
        DependencyEventScope.event_id.in_(statement_ids),
    )
    scope_link_ids = _ids(scope_links)
    evidence = _rows(
        session,
        EvidenceLink,
        or_(
            EvidenceLink.document_id.in_(document_ids),
            EvidenceLink.dependency_id.in_(dependency_ids),
        ),
    )
    evidence_ids = _ids(evidence)
    coordination_receipts = _rows(
        session,
        StatementCoordinationReceipt,
        StatementCoordinationReceipt.candidate_id.in_(candidate_ids),
    )
    coordination_receipt_ids = _ids(coordination_receipts)
    reversals = _rows(
        session,
        StatementCoordinationReversal,
        StatementCoordinationReversal.candidate_id.in_(candidate_ids),
    )
    reversal_ids = _ids(reversals)
    comparison_runs = _rows(
        session,
        RevisionComparisonRun,
        RevisionComparisonRun.project_id == project.id,
    )
    comparison_run_ids = _ids(comparison_runs)
    investigation_runs = _rows(
        session,
        EvidenceInvestigationRun,
        EvidenceInvestigationRun.project_id == project.id,
    )
    investigation_run_ids = _ids(investigation_runs)
    shadow_cases = _rows(
        session,
        EvidenceInvestigationShadowCase,
        EvidenceInvestigationShadowCase.project_id == project.id,
    )
    shadow_case_ids = _ids(shadow_cases)
    investigation_evaluations = tuple(
        value
        for value in session.scalars(
            select(EvidenceInvestigationEvaluationReceipt).order_by(
                EvidenceInvestigationEvaluationReceipt.id
            )
        ).all()
        if set(value.selected_run_ids_json or ()) & set(investigation_run_ids)
    )

    model_rows: list[tuple[type[Any], Sequence[Any]]] = [
        (Project, (project,)),
        (Document, documents),
        (DocPage, _rows(session, DocPage, DocPage.document_id.in_(document_ids))),
        (
            DocumentQuarantine,
            _rows(
                session,
                DocumentQuarantine,
                DocumentQuarantine.document_id.in_(document_ids),
            ),
        ),
        (
            DocumentRenditionDerivation,
            _rows(
                session,
                DocumentRenditionDerivation,
                DocumentRenditionDerivation.project_id == project.id,
            ),
        ),
        (ExtractionRun, extraction_runs),
        (
            ActiveExtractionRun,
            _rows(
                session,
                ActiveExtractionRun,
                ActiveExtractionRun.document_id.in_(document_ids),
            ),
        ),
        (
            ActiveRunDeclaration,
            _rows(
                session,
                ActiveRunDeclaration,
                ActiveRunDeclaration.document_id.in_(document_ids),
            ),
        ),
        (Candidate, candidates),
        (CandidateDisposition, dispositions),
        (ExtractionMeasurementCaseState, measurement_case_states),
        (EvidenceInvestigationRun, investigation_runs),
        (
            EvidenceInvestigationStepReceipt,
            _rows(
                session,
                EvidenceInvestigationStepReceipt,
                EvidenceInvestigationStepReceipt.run_id.in_(
                    investigation_run_ids
                ),
            ),
        ),
        (
            EvidenceInvestigationPacketReceipt,
            _rows(
                session,
                EvidenceInvestigationPacketReceipt,
                EvidenceInvestigationPacketReceipt.run_id.in_(
                    investigation_run_ids
                ),
            ),
        ),
        (EvidenceInvestigationShadowCase, shadow_cases),
        (
            EvidenceInvestigationShadowExecution,
            _rows(
                session,
                EvidenceInvestigationShadowExecution,
                or_(
                    EvidenceInvestigationShadowExecution.shadow_case_id.in_(
                        shadow_case_ids
                    ),
                    EvidenceInvestigationShadowExecution.run_id.in_(
                        investigation_run_ids
                    ),
                ),
            ),
        ),
        (
            EvidenceInvestigationReviewObservation,
            _rows(
                session,
                EvidenceInvestigationReviewObservation,
                EvidenceInvestigationReviewObservation.shadow_case_id.in_(
                    shadow_case_ids
                ),
            ),
        ),
        (
            EvidenceInvestigationCandidateReviewStart,
            _rows(
                session,
                EvidenceInvestigationCandidateReviewStart,
                EvidenceInvestigationCandidateReviewStart.project_id == project.id,
            ),
        ),
        (
            EvidenceInvestigationShadowOutcome,
            _rows(
                session,
                EvidenceInvestigationShadowOutcome,
                EvidenceInvestigationShadowOutcome.shadow_case_id.in_(
                    shadow_case_ids
                ),
            ),
        ),
        (EvidenceInvestigationEvaluationReceipt, investigation_evaluations),
        (
            CohortReceipt,
            _rows(session, CohortReceipt, CohortReceipt.project_id == project.id),
        ),
        (
            EventCohortReceipt,
            _rows(
                session,
                EventCohortReceipt,
                EventCohortReceipt.project_id == project.id,
            ),
        ),
        (PolicyApproval, _rows(session, PolicyApproval, PolicyApproval.project_id == project.id)),
        (PolicyRun, policy_runs),
        (
            DependencyAdmissionOutcome,
            _rows(
                session,
                DependencyAdmissionOutcome,
                DependencyAdmissionOutcome.policy_run_id.in_(policy_run_ids),
            ),
        ),
        (
            EventAdmissionOutcome,
            _rows(
                session,
                EventAdmissionOutcome,
                EventAdmissionOutcome.policy_run_id.in_(policy_run_ids),
            ),
        ),
        (
            EventAdmissionAcceptanceReceipt,
            _rows(
                session,
                EventAdmissionAcceptanceReceipt,
                EventAdmissionAcceptanceReceipt.project_id == project.id,
            ),
        ),
        (
            EventAdmissionActivation,
            _rows(
                session,
                EventAdmissionActivation,
                EventAdmissionActivation.project_id == project.id,
            ),
        ),
        (Dependency, dependencies),
        (
            ExternalParty,
            _rows(
                session,
                ExternalParty,
                ExternalParty.id.in_(external_party_ids),
            ),
        ),
        (
            Assertion,
            _rows(session, Assertion, Assertion.dependency_id.in_(dependency_ids)),
        ),
        (EvidenceLink, evidence),
        (
            DependencyEvidenceSufficiency,
            _rows(
                session,
                DependencyEvidenceSufficiency,
                DependencyEvidenceSufficiency.dependency_id.in_(dependency_ids),
            ),
        ),
        (
            DocumentationFieldConfirmation,
            _rows(
                session,
                DocumentationFieldConfirmation,
                DocumentationFieldConfirmation.dependency_id.in_(dependency_ids),
            ),
        ),
        (
            OperativeSupport,
            _rows(
                session,
                OperativeSupport,
                OperativeSupport.dependency_id.in_(dependency_ids),
            ),
        ),
        (
            DependencyDismissal,
            _rows(
                session,
                DependencyDismissal,
                DependencyDismissal.dependency_id.in_(dependency_ids),
            ),
        ),
        (
            DisputeSettlement,
            _rows(
                session,
                DisputeSettlement,
                DisputeSettlement.dependency_id.in_(dependency_ids),
            ),
        ),
        (
            RetiredDependencyStatus,
            _rows(
                session,
                RetiredDependencyStatus,
                RetiredDependencyStatus.dependency_id.in_(dependency_ids),
            ),
        ),
        (CommitmentLineage, lineages),
        (ExternalPartyStatement, statements),
        (
            DependencyEventTiming,
            _rows(
                session,
                DependencyEventTiming,
                DependencyEventTiming.event_id.in_(statement_ids),
            ),
        ),
        (
            DependencyEventMigrationReceipt,
            _rows(
                session,
                DependencyEventMigrationReceipt,
                DependencyEventMigrationReceipt.event_id.in_(statement_ids),
            ),
        ),
        (DependencyEventScopeDecision, scope_decisions),
        (DependencyEventScope, scope_links),
        (
            DependencyEventEvidence,
            _rows(
                session,
                DependencyEventEvidence,
                DependencyEventEvidence.event_id.in_(statement_ids),
            ),
        ),
        (WorkDecision, work_decisions),
        (
            WorkDecisionMilestoneImpact,
            _rows(
                session,
                WorkDecisionMilestoneImpact,
                WorkDecisionMilestoneImpact.work_decision_id.in_(work_decision_ids),
            ),
        ),
        (
            ProjectRosterEntry,
            _rows(
                session,
                ProjectRosterEntry,
                ProjectRosterEntry.project_id == project.id,
            ),
        ),
        (StatementCoordinationReceipt, coordination_receipts),
        (StatementCoordinationReversal, reversals),
        (
            StatementCoordinationReversalEffect,
            _rows(
                session,
                StatementCoordinationReversalEffect,
                StatementCoordinationReversalEffect.reversal_id.in_(reversal_ids),
            ),
        ),
        (
            ReconfirmationReceipt,
            _rows(
                session,
                ReconfirmationReceipt,
                ReconfirmationReceipt.dependency_id.in_(dependency_ids),
            ),
        ),
        (
            AutomaticCarryForwardReceipt,
            _rows(
                session,
                AutomaticCarryForwardReceipt,
                AutomaticCarryForwardReceipt.project_id == project.id,
            ),
        ),
        (
            AutomaticCarryForwardOutcome,
            _rows(
                session,
                AutomaticCarryForwardOutcome,
                AutomaticCarryForwardOutcome.project_id == project.id,
            ),
        ),
        (Milestone, milestones),
        (
            MilestoneRegistration,
            _rows(
                session,
                MilestoneRegistration,
                MilestoneRegistration.milestone_id.in_(milestone_ids),
            ),
        ),
        (
            RevisionComparisonRun,
            comparison_runs,
        ),
        (
            RevisionComparisonFinding,
            _rows(
                session,
                RevisionComparisonFinding,
                RevisionComparisonFinding.revision_comparison_run_id.in_(
                    comparison_run_ids
                ),
            ),
        ),
        (
            ReportRun,
            _rows(session, ReportRun, ReportRun.project_id == project.id),
        ),
        (
            ExternalReportArtifact,
            _rows(
                session,
                ExternalReportArtifact,
                ExternalReportArtifact.project_id == project.id,
            ),
        ),
        (
            ExternalReportRelease,
            _rows(
                session,
                ExternalReportRelease,
                ExternalReportRelease.project_id == project.id,
            ),
        ),
        (
            LegacyLedgerArchive,
            _rows(
                session,
                LegacyLedgerArchive,
                LegacyLedgerArchive.project_id == project.id,
            ),
        ),
    ]

    audit_predicates = [
        and_(AuditLog.entity_type == "project", AuditLog.entity_id == project.id),
        and_(AuditLog.entity_type == "candidate", AuditLog.entity_id.in_(candidate_ids)),
        and_(AuditLog.entity_type == "dependency", AuditLog.entity_id.in_(dependency_ids)),
        and_(AuditLog.entity_type == "milestone", AuditLog.entity_id.in_(milestone_ids)),
        and_(
            AuditLog.entity_type == "commitment_lineage",
            AuditLog.entity_id.in_(lineage_ids),
        ),
    ]
    model_rows.append(
        (AuditLog, _rows(session, AuditLog, or_(*audit_predicates)))
    )

    # The precomputed ids above are deliberately read even when a family has
    # no rows: they make scope derivation explicit and prevent a broad table
    # query from silently replacing a project-local one.
    del (
        extraction_run_ids,
        disposition_ids,
        scope_decision_ids,
        scope_link_ids,
        evidence_ids,
        coordination_receipt_ids,
    )

    rows = _fingerprint_model_rows(model_rows)
    return ProjectWriteSetSnapshot(
        project_id=project.id,
        project_slug=project.slug,
        rows=dict(sorted(rows.items())),
    )


def diff_project_write_sets(
    before: ProjectWriteSetSnapshot, after: ProjectWriteSetSnapshot
) -> ProjectWriteSetDiff:
    """Derive the exact protected write set between two live captures."""

    if (
        before.project_id != after.project_id
        or before.project_slug != after.project_slug
    ):
        raise ValueError("Project write-set captures do not name the same Project")
    created: dict[str, tuple[ProjectRowFingerprint, ...]] = {}
    deleted: dict[str, tuple[ProjectRowFingerprint, ...]] = {}
    updated: dict[
        str, tuple[tuple[ProjectRowFingerprint, ProjectRowFingerprint], ...]
    ] = {}
    for table_name in sorted(set(before.rows) | set(after.rows)):
        old = {
            _canonical_json(item.identity): item
            for item in before.rows.get(table_name, ())
        }
        new = {
            _canonical_json(item.identity): item
            for item in after.rows.get(table_name, ())
        }
        created[table_name] = tuple(new[key] for key in sorted(new.keys() - old.keys()))
        deleted[table_name] = tuple(old[key] for key in sorted(old.keys() - new.keys()))
        updated[table_name] = tuple(
            (old[key], new[key])
            for key in sorted(old.keys() & new.keys())
            if old[key].content_sha256 != new[key].content_sha256
        )
    return ProjectWriteSetDiff(
        project_id=before.project_id,
        project_slug=before.project_slug,
        created=created,
        deleted=deleted,
        updated=updated,
    )


def run_bounded_product_proving_operations(
    session: Session,
    *,
    project_slug: str,
    baseline_runs: Mapping[int, int],
    extraction_operation: ExtractionOperation,
    active_run_operation: ActiveRunOperation,
    admission_operation: AdmissionOperation | None = None,
    _commit_for_test: CommitOperation | None = None,
) -> BoundedProductProvingOperations:
    """Extract exact Documents, compare, then declare/admit only on equality.

    Extraction attempts are allowed to leave their immutable receipts.  Active
    Run declaration and Admission are a separate phase and are never entered
    after an extractor error, a failed run, a configuration mismatch, or a
    semantic Candidate difference.
    """

    commit = _commit_for_test or _commit_session
    if not baseline_runs:
        raise ValueError("Product Proving needs at least one baseline Run")
    project = _require_project(session, project_slug)
    documents = _exact_documents(
        session, project.id, tuple(baseline_runs.keys())
    )
    for document in documents:
        active = session.get(ActiveExtractionRun, document.id)
        if active is None or active.extraction_run_id != baseline_runs[document.id]:
            raise ValueError(
                f"Document {document.id} does not have its pinned baseline Active Run"
            )

    before_write_set = capture_project_write_set(session, project_slug)
    before_policy_run_ids = set(
        session.scalars(
            select(PolicyRun.id).where(PolicyRun.project_id == project.id)
        ).all()
    )
    fresh_run_ids: dict[int, int] = {}
    comparisons: list[CandidateSetComparison] = []
    failures: list[str] = []
    for document in documents:
        try:
            with session.begin_nested():
                result = extraction_operation(session, document)
                session.flush()
                for pinned_document in documents:
                    still_active = session.get(
                        ActiveExtractionRun,
                        pinned_document.id,
                        populate_existing=True,
                    )
                    if (
                        still_active is None
                        or still_active.extraction_run_id
                        != baseline_runs[pinned_document.id]
                    ):
                        raise ValueError(
                            "extraction operation changed an Active Run before comparison"
                        )
                fresh_run_id = (
                    result.id if isinstance(result, ExtractionRun) else result
                )
                if not isinstance(fresh_run_id, int) or fresh_run_id <= 0:
                    raise ValueError("extractor did not return one Extraction Run id")
                if fresh_run_id == baseline_runs[document.id]:
                    raise ValueError("extractor returned the pinned baseline Run")
                prospective_runs = {**fresh_run_ids, document.id: fresh_run_id}
                extraction_snapshot = capture_project_write_set(
                    session, project_slug
                )
                validate_bounded_operations_write_set(
                    session,
                    before=before_write_set,
                    after=extraction_snapshot,
                    baseline_runs=baseline_runs,
                    fresh_run_ids=prospective_runs,
                    admission_started=False,
                )
            fresh_run_ids[document.id] = fresh_run_id
            comparison = compare_extraction_runs(
                session,
                baseline_runs[document.id],
                fresh_run_id,
            )
            comparisons.append(comparison)
            if not comparison.equal:
                failures.append(
                    f"Document {document.id}: {len(comparison.added)} added / "
                    f"{len(comparison.missing)} missing canonical Candidates"
                )
        except Exception as error:  # terminal extraction evidence, not a pass
            failures.append(f"Document {document.id}: {error}")

    if failures:
        return _commit_and_observe_operations(
            session,
            project_slug=project_slug,
            project_id=project.id,
            baseline_runs=baseline_runs,
            before_write_set=before_write_set,
            before_policy_run_ids=before_policy_run_ids,
            fresh_run_ids=fresh_run_ids,
            comparisons=tuple(comparisons),
            failures=tuple(failures),
            active_run_document_ids=(),
            admission_started=False,
            admission_result=None,
            commit=commit,
        )

    if admission_operation is None:
        write_candidate_ids = tuple(
            session.scalars(
                select(Candidate.id)
                .where(
                    Candidate.project_id == project.id,
                    Candidate.extraction_run_id.in_(tuple(fresh_run_ids.values())),
                )
                .order_by(Candidate.id)
            ).all()
        )

        def admission(db: Session, project_id: int) -> Any:
            return _run_admission_without_declaration(
                db,
                project_id,
                write_candidate_ids=write_candidate_ids,
            )

    else:
        admission = admission_operation
    try:
        # One savepoint keeps a declaration/Admission failure from leaving a
        # partial Project Record while retaining the completed extraction
        # receipts outside it.
        with session.begin_nested():
            for document in documents:
                active_run_operation(
                    session, document.id, fresh_run_ids[document.id]
                )
            session.flush()
            for document in documents:
                active = session.get(
                    ActiveExtractionRun,
                    document.id,
                    populate_existing=True,
                )
                if (
                    active is None
                    or active.extraction_run_id != fresh_run_ids[document.id]
                ):
                    raise ValueError(
                        f"Document {document.id} fresh Run was not explicitly declared"
                    )
            admission_result = admission(session, project.id)
            session.flush()
            provisional_after = capture_project_write_set(session, project_slug)
            validate_bounded_operations_write_set(
                session,
                before=before_write_set,
                after=provisional_after,
                baseline_runs=baseline_runs,
                fresh_run_ids=fresh_run_ids,
                admission_started=True,
            )
    except Exception as error:
        # The savepoint rollback restores the database, but ORM instances read
        # and mutated during declaration/Admission can still carry their
        # pre-rollback attributes.  Failure capture must begin from a database
        # re-read or it can falsely report rolled-back Active Run changes.
        session.expire_all()
        return _commit_and_observe_operations(
            session,
            project_slug=project_slug,
            project_id=project.id,
            baseline_runs=baseline_runs,
            before_write_set=before_write_set,
            before_policy_run_ids=before_policy_run_ids,
            fresh_run_ids=fresh_run_ids,
            comparisons=tuple(comparisons),
            failures=(f"Active Run / Admission: {error}",),
            active_run_document_ids=(),
            admission_started=False,
            admission_result=None,
            commit=commit,
        )
    return _commit_and_observe_operations(
        session,
        project_slug=project_slug,
        project_id=project.id,
        baseline_runs=baseline_runs,
        before_write_set=before_write_set,
        before_policy_run_ids=before_policy_run_ids,
        fresh_run_ids=fresh_run_ids,
        comparisons=tuple(comparisons),
        failures=(),
        active_run_document_ids=tuple(item.id for item in documents),
        admission_started=True,
        admission_result=admission_result,
        commit=commit,
    )


_EXTRACTION_WRITE_TABLES = frozenset({"extraction_runs", "candidates"})
_ADMISSION_WRITE_TABLES = frozenset(
    {
        *_EXTRACTION_WRITE_TABLES,
        "active_extraction_runs",
        "active_run_declarations",
        "policy_runs",
        "dependency_admission_outcomes",
        "event_admission_outcomes",
        "dependencies",
        "external_orgs",
        "evidence_links",
        "operative_support",
        "assertions",
        "commitment_lineages",
        "dependency_events",
        "dependency_event_timings",
        "dependency_event_scope_decisions",
        "dependency_event_scopes",
        "dependency_event_evidence",
        "candidate_dispositions",
        "audit_log",
    }
)


def validate_bounded_operations_write_set(
    session: Session,
    *,
    before: ProjectWriteSetSnapshot,
    after: ProjectWriteSetSnapshot,
    baseline_runs: Mapping[int, int],
    fresh_run_ids: Mapping[int, int],
    admission_started: bool,
) -> ProjectWriteSetDiff:
    """Refuse writes outside the exact extraction/Admission packet.

    The allowlist is only the first gate.  Every created Run and Candidate is
    matched to one exact Document/Run pair, every Admission outcome must name
    one of those Candidates, and every downstream Project Record identity is
    traced through those outcomes.  Tables used by the later practitioner and
    Report phases are deliberately absent.
    """

    if not set(fresh_run_ids).issubset(baseline_runs):
        raise ValueError("fresh Extraction Runs widened the bounded Document set")
    if admission_started and set(fresh_run_ids) != set(baseline_runs):
        raise ValueError("Admission requires one compared fresh Run per Document")
    write_set = diff_project_write_sets(before, after)
    if any(write_set.deleted.values()):
        raise ValueError("bounded Product Proving operations deleted protected rows")
    changed_tables = _changed_tables(write_set)
    allowed = (
        _ADMISSION_WRITE_TABLES
        if admission_started
        else _EXTRACTION_WRITE_TABLES
    )
    unexpected = sorted(changed_tables - allowed)
    if unexpected:
        raise ValueError(
            "bounded Product Proving operations changed out-of-packet tables: "
            + ", ".join(unexpected)
        )

    expected_run_ids = set(fresh_run_ids.values())
    created_run_ids = _write_set_ids(write_set, "extraction_runs", "created")
    if created_run_ids != expected_run_ids:
        raise ValueError("Extraction Run writes do not equal the returned fresh Runs")
    if _write_set_ids(write_set, "extraction_runs", "updated"):
        raise ValueError("an immutable Extraction Run was updated")
    runs = tuple(
        session.scalars(
            select(ExtractionRun).where(ExtractionRun.id.in_(expected_run_ids))
        ).all()
    )
    expected_pairs = {(document_id, run_id) for document_id, run_id in fresh_run_ids.items()}
    if {(run.document_id, run.id) for run in runs} != expected_pairs:
        raise ValueError("fresh Extraction Runs do not belong to the bounded Documents")

    fresh_candidates = tuple(
        session.scalars(
            select(Candidate)
            .where(
                Candidate.project_id == before.project_id,
                Candidate.extraction_run_id.in_(expected_run_ids),
            )
            .order_by(Candidate.id)
        ).all()
    )
    fresh_candidate_ids = {candidate.id for candidate in fresh_candidates}
    if _write_set_ids(write_set, "candidates", "created") != fresh_candidate_ids:
        raise ValueError("Candidate writes are not exactly owned by the fresh Runs")
    if _write_set_ids(write_set, "candidates", "updated") - fresh_candidate_ids:
        raise ValueError("operations changed a Candidate outside the fresh Runs")
    if any(
        fresh_run_ids.get(candidate.source_document_id)
        != candidate.extraction_run_id
        for candidate in fresh_candidates
    ):
        raise ValueError("a fresh Candidate crossed the bounded Document/Run pairs")

    if not admission_started:
        return write_set

    bounded_document_ids = set(fresh_run_ids)
    active_ids = _write_set_ids(
        write_set,
        "active_extraction_runs",
        "updated",
        identity_key="document_id",
    ) | _write_set_ids(
        write_set,
        "active_extraction_runs",
        "created",
        identity_key="document_id",
    )
    if active_ids != bounded_document_ids:
        raise ValueError("Active Run writes do not equal the compared Documents")
    declarations = tuple(
        session.scalars(
            select(ActiveRunDeclaration).where(
                ActiveRunDeclaration.id.in_(
                    _write_set_ids(write_set, "active_run_declarations", "created")
                )
            )
        ).all()
    )
    if {
        (declaration.document_id, declaration.extraction_run_id)
        for declaration in declarations
    } != expected_pairs:
        raise ValueError("Active Run declarations do not name the exact fresh Runs")

    policy_run_ids = _write_set_ids(write_set, "policy_runs", "created")
    policy_runs = tuple(
        session.scalars(
            select(PolicyRun).where(PolicyRun.id.in_(policy_run_ids))
        ).all()
    )
    if any(
        run.project_id != before.project_id
        or run.family not in {"dependency-admission", "event-admission"}
        for run in policy_runs
    ):
        raise ValueError("a Policy Run is outside the two Admission families")
    dependency_outcomes = tuple(
        session.scalars(
            select(DependencyAdmissionOutcome).where(
                DependencyAdmissionOutcome.policy_run_id.in_(policy_run_ids)
            )
        ).all()
    )
    event_outcomes = tuple(
        session.scalars(
            select(EventAdmissionOutcome).where(
                EventAdmissionOutcome.policy_run_id.in_(policy_run_ids)
            )
        ).all()
    )
    if (
        {item.id for item in dependency_outcomes}
        != _write_set_ids(write_set, "dependency_admission_outcomes", "created")
        or {item.id for item in event_outcomes}
        != _write_set_ids(write_set, "event_admission_outcomes", "created")
    ):
        raise ValueError("Admission outcomes do not belong to the new Policy Runs")
    if any(
        outcome.candidate_id not in fresh_candidate_ids
        for outcome in (*dependency_outcomes, *event_outcomes)
    ):
        raise ValueError("an Admission outcome names a Candidate outside the packet")

    dependency_ids = {
        value
        for value in (
            *(outcome.dependency_id for outcome in dependency_outcomes),
        )
        if value is not None
    }
    event_ids = {
        value
        for value in (outcome.dependency_event_id for outcome in event_outcomes)
        if value is not None
    }
    lineage_ids = {
        value
        for value in (outcome.commitment_lineage_id for outcome in event_outcomes)
        if value is not None
    }
    scope_decision_ids = {
        value
        for value in (outcome.scope_decision_id for outcome in event_outcomes)
        if value is not None
    }
    disposition_ids = {
        value
        for value in (outcome.candidate_disposition_id for outcome in event_outcomes)
        if value is not None
    }
    audit_ids = {
        value
        for value in (outcome.audit_log_id for outcome in event_outcomes)
        if value is not None
    }
    scopes = tuple(
        session.scalars(
            select(DependencyEventScope).where(
                DependencyEventScope.event_id.in_(event_ids)
            )
        ).all()
    )
    dependency_ids.update(scope.dependency_id for scope in scopes)

    _require_changed_ids_within(write_set, "dependencies", dependency_ids)
    _require_changed_ids_within(write_set, "dependency_events", event_ids)
    _require_changed_ids_within(write_set, "commitment_lineages", lineage_ids)
    _require_changed_ids_within(
        write_set, "dependency_event_scope_decisions", scope_decision_ids
    )
    _require_changed_ids_within(
        write_set, "candidate_dispositions", disposition_ids
    )
    _require_changed_ids_within(write_set, "audit_log", audit_ids | _admission_audit_ids(
        session,
        write_set,
        dependency_ids=dependency_ids,
        lineage_ids=lineage_ids,
    ))

    _require_foreign_key_subset(
        session,
        write_set,
        Assertion,
        "assertions",
        "dependency_id",
        dependency_ids,
    )
    _require_foreign_key_subset(
        session,
        write_set,
        OperativeSupport,
        "operative_support",
        "dependency_id",
        dependency_ids,
    )
    _require_foreign_key_subset(
        session,
        write_set,
        DependencyEventTiming,
        "dependency_event_timings",
        "event_id",
        event_ids,
    )
    _require_foreign_key_subset(
        session,
        write_set,
        DependencyEventScope,
        "dependency_event_scopes",
        "event_id",
        event_ids,
    )
    _require_foreign_key_subset(
        session,
        write_set,
        DependencyEventEvidence,
        "dependency_event_evidence",
        "event_id",
        event_ids,
        identity_key="evidence_link_id",
    )
    evidence_ids = _write_set_ids(write_set, "evidence_links", "created")
    evidence = tuple(
        session.scalars(
            select(EvidenceLink).where(EvidenceLink.id.in_(evidence_ids))
        ).all()
    )
    if any(
        item.document_id not in bounded_document_ids
        or (
            item.dependency_id is not None
            and item.dependency_id not in dependency_ids
        )
        for item in evidence
    ):
        raise ValueError("created Evidence is not traceable to the bounded packet")

    changed_party_ids = _all_changed_ids(write_set, "external_orgs")
    traced_parties = {
        value
        for value in session.scalars(
            select(Dependency.external_org_id).where(
                Dependency.id.in_(dependency_ids)
            )
        ).all()
        if value is not None
    }
    traced_parties.update(
        value
        for row in session.execute(
            select(
                ExternalPartyStatement.affected_external_org_id,
                ExternalPartyStatement.stated_external_org_id,
            ).where(ExternalPartyStatement.id.in_(event_ids))
        ).all()
        for value in row
        if value is not None
    )
    if changed_party_ids - traced_parties:
        raise ValueError("an External Party write is not used by an admitted record")
    return write_set


def residual_candidate_ids_from_operations(
    session: Session,
    operations: BoundedProductProvingOperations,
) -> tuple[int, ...]:
    """Read residue only from the complete, durable operations packet."""

    if not operations.admission_started or not operations.extraction_equal:
        raise ValueError("residual Candidates require passed durable Admission")
    comparison_runs: dict[int, int] = {}
    for comparison in operations.extraction_comparisons:
        if comparison.document_id in comparison_runs:
            raise ValueError("operations repeat an extraction comparison Document")
        if not comparison.equal:
            raise ValueError("operations contain a failed extraction comparison")
        comparison_runs[comparison.document_id] = comparison.fresh_run_id
    exact_runs = dict(operations.fresh_run_ids)
    if (
        comparison_runs != exact_runs
        or set(operations.active_run_document_ids) != set(exact_runs)
        or len(operations.active_run_document_ids) != len(exact_runs)
        or dict(operations.observed_active_runs) != exact_runs
    ):
        raise ValueError(
            "residual Candidate scope is not the complete compared Active Run set"
        )
    project_slug = operations.after_write_set.project_slug
    project = _require_project(session, project_slug)
    if project.id != operations.project_id:
        raise ValueError("operations residue names a different live Project")
    _exact_documents(session, project.id, tuple(exact_runs))
    run_pairs = []
    for document_id, run_id in sorted(exact_runs.items()):
        active = session.get(
            ActiveExtractionRun,
            document_id,
            populate_existing=True,
        )
        run = session.get(ExtractionRun, run_id)
        if (
            active is None
            or active.extraction_run_id != run_id
            or run is None
            or run.document_id != document_id
            or run.outcome != "completed"
            or run.page_errors != 0
        ):
            raise ValueError(
                f"Document {document_id} no longer has its exact fresh Active Run"
            )
        run_pairs.append(
            and_(
                Candidate.source_document_id == document_id,
                Candidate.extraction_run_id == run_id,
            )
        )
    return tuple(
        session.scalars(
            select(Candidate.id)
            .where(
                Candidate.project_id == project.id,
                Candidate.state == "pending",
                or_(*run_pairs),
            )
            .order_by(Candidate.id)
        ).all()
    )


def capture_live_product_proving_operations(
    session: Session,
    *,
    project_slug: str,
    expected: ExpectedPreflight,
    verified_baseline: VerifiedProductProvingDatabaseBaseline,
    database_url: str,
    pass_number: int,
    extraction_operation: ExtractionOperation,
    active_run_operation: ActiveRunOperation,
    admission_operation: AdmissionOperation | None = None,
    repo_root: Path | str | None = None,
    git_observer: GitObserver = observe_git_checkout,
    _commit_for_test: CommitOperation | None = None,
    _fingerprint_for_test: Callable[[str], DatabaseFingerprint] | None = None,
    prior_restore_bundle_dir: Path | str | None = None,
    prior_restore_manifest_sha256: str | None = None,
) -> LiveProductProvingOperationsCapture:
    """Run the operations phase from live pins without observed JSON input.

    This is the CLI-facing composition seam: observe and verify preflight,
    execute the bounded durable operation, and return its comparisons,
    before/after protected snapshots, write set, committed receipts, and
    residuals as one typed capture.
    """

    if pass_number not in {1, 2}:
        raise ValueError("Product Proving operations pass number must be 1 or 2")
    prior_values = (prior_restore_bundle_dir, prior_restore_manifest_sha256)
    if pass_number == 1 and any(value is not None for value in prior_values):
        raise ValueError("Product Proving pass 1 cannot name a prior restore")
    if pass_number == 2 and any(value is None for value in prior_values):
        raise ValueError("Product Proving pass 2 requires the verified pass 1 restore")
    if set(expected.documents) != set(expected.baseline_runs):
        raise ValueError("expected Document and baseline Run pins must be exact")
    observed = observe_product_proving_preflight(
        session,
        project_slug=project_slug,
        document_ids=tuple(expected.documents),
        verified_baseline=verified_baseline,
        database_url=database_url,
        repo_root=repo_root,
        git_observer=git_observer,
        _fingerprint_for_test=_fingerprint_for_test,
    )
    verify_preflight(expected, observed)
    prior_restore_operation_id = None
    prior_restore_bundle_canonical_sha256 = None
    if pass_number == 2:
        # Runtime import avoids making the restore bundle depend on itself
        # through frontend capture -> execution.
        from corridor.product_proving_restore import (
            verify_product_proving_restore_bundle,
        )

        prior = verify_product_proving_restore_bundle(
            Path(prior_restore_bundle_dir),
            expected_integrity_manifest_sha256=str(prior_restore_manifest_sha256),
        )
        receipt = prior.receipt
        source = verified_baseline.baseline.get("source_database") or {}
        if (
            not prior.valid
            or receipt.pass_number != 1
            or receipt.database_baseline_manifest_sha256
            != verified_baseline.manifest_sha256
            or receipt.database_baseline_dump_sha256 != verified_baseline.dump_sha256
            or receipt.database_baseline_state_sha256
            != verified_baseline.fingerprint.state_sha256
            or receipt.database_baseline_schema_sha256
            != verified_baseline.fingerprint.schema_sha256
            or receipt.restored_state_sha256
            != verified_baseline.fingerprint.state_sha256
            or dict(receipt.source_database_identity) != source
            or dict(receipt.source_connection_identity)
            != (verified_baseline.baseline.get("source_connection") or {})
        ):
            raise ValueError("Product Proving pass 2 prior restore is not its baseline")
        prior_restore_operation_id = receipt.restore_operation_id
        prior_restore_bundle_canonical_sha256 = prior.canonical_content_sha256
    execution_id = str(uuid4())
    started_at = datetime.now(timezone.utc).isoformat()
    operations = run_bounded_product_proving_operations(
        session,
        project_slug=project_slug,
        baseline_runs=expected.baseline_runs,
        extraction_operation=extraction_operation,
        active_run_operation=active_run_operation,
        admission_operation=admission_operation,
        _commit_for_test=_commit_for_test,
    )
    return LiveProductProvingOperationsCapture(
        expected=expected,
        observed=observed,
        operations=operations,
        database_baseline_manifest_sha256=verified_baseline.manifest_sha256,
        database_baseline_dump_sha256=verified_baseline.dump_sha256,
        database_baseline_state_sha256=verified_baseline.fingerprint.state_sha256,
        database_baseline_fingerprint=verified_baseline.fingerprint,
        database_source_identity=dict(
            verified_baseline.baseline.get("source_database") or {}
        ),
        database_source_connection_identity=dict(
            verified_baseline.baseline.get("source_connection") or {}
        ),
        pass_number=pass_number,
        execution_id=execution_id,
        started_at=started_at,
        prior_restore_operation_id=prior_restore_operation_id,
        prior_restore_bundle_manifest_sha256=(
            str(prior_restore_manifest_sha256) if pass_number == 2 else None
        ),
        prior_restore_bundle_canonical_sha256=(
            prior_restore_bundle_canonical_sha256 if pass_number == 2 else None
        ),
    )


def _commit_and_observe_operations(
    session: Session,
    *,
    project_slug: str,
    project_id: int,
    baseline_runs: Mapping[int, int],
    before_write_set: ProjectWriteSetSnapshot,
    before_policy_run_ids: set[int],
    fresh_run_ids: Mapping[int, int],
    comparisons: tuple[CandidateSetComparison, ...],
    failures: tuple[str, ...],
    active_run_document_ids: tuple[int, ...],
    admission_started: bool,
    admission_result: Any | None,
    commit: CommitOperation,
) -> BoundedProductProvingOperations:
    provisional_after = capture_project_write_set(session, project_slug)
    validate_bounded_operations_write_set(
        session,
        before=before_write_set,
        after=provisional_after,
        baseline_runs=baseline_runs,
        fresh_run_ids=fresh_run_ids,
        admission_started=admission_started,
    )
    commit(session)
    session.expire_all()
    after_write_set = capture_project_write_set(session, project_slug)
    write_set = validate_bounded_operations_write_set(
        session,
        before=before_write_set,
        after=after_write_set,
        baseline_runs=baseline_runs,
        fresh_run_ids=fresh_run_ids,
        admission_started=admission_started,
    )
    observed_active_runs = _observe_exact_active_runs(
        session, project_id, tuple(baseline_runs)
    )
    expected_active_runs = fresh_run_ids if admission_started else baseline_runs
    if dict(observed_active_runs) != dict(expected_active_runs):
        raise ValueError("durable Active Runs do not match the terminal operation")
    policy_receipts = _observe_new_policy_receipts(
        session, project_id, before_policy_run_ids
    )
    if not admission_started and policy_receipts:
        raise ValueError("Admission Policy Runs were committed after a stopped operation")
    operations = BoundedProductProvingOperations(
        project_id=project_id,
        fresh_run_ids=dict(fresh_run_ids),
        extraction_comparisons=comparisons,
        extraction_failures=failures,
        active_run_document_ids=active_run_document_ids,
        admission_started=admission_started,
        admission_result=admission_result,
        observed_active_runs=observed_active_runs,
        admission_policy_receipts=policy_receipts,
        residual_candidate_ids=(),
        before_write_set=before_write_set,
        after_write_set=after_write_set,
        write_set=write_set,
    )
    if admission_started:
        operations = replace(
            operations,
            residual_candidate_ids=residual_candidate_ids_from_operations(
                session, operations
            ),
        )
    return operations


def _commit_session(session: Session) -> None:
    """Production durability boundary; tests may inject a non-committing seam."""

    session.commit()


def _observe_exact_active_runs(
    session: Session, project_id: int, document_ids: Sequence[int]
) -> Mapping[int, int]:
    documents = _exact_documents(session, project_id, document_ids)
    observed = {}
    for document in documents:
        active = session.get(
            ActiveExtractionRun,
            document.id,
            populate_existing=True,
        )
        if active is None:
            raise ValueError(f"Document {document.id} has no durable Active Run")
        observed[document.id] = active.extraction_run_id
    return observed


def _observe_new_policy_receipts(
    session: Session, project_id: int, prior_ids: set[int]
) -> tuple[AdmissionPolicyReceipt, ...]:
    runs = tuple(
        session.scalars(
            select(PolicyRun)
            .where(
                PolicyRun.project_id == project_id,
                PolicyRun.id.not_in(prior_ids),
            )
            .order_by(PolicyRun.id)
        ).all()
    )
    return tuple(
        AdmissionPolicyReceipt(
            run_id=run.id,
            family=run.family,
            policy_version=run.policy_version,
            policy_sha256=run.policy_sha256,
            applied_count=run.applied_count,
            abstained_count=run.abstained_count,
        )
        for run in runs
    )


def _changed_tables(write_set: ProjectWriteSetDiff) -> set[str]:
    return {
        table_name
        for table_name in set(write_set.created)
        | set(write_set.deleted)
        | set(write_set.updated)
        if write_set.created.get(table_name)
        or write_set.deleted.get(table_name)
        or write_set.updated.get(table_name)
    }


def _write_set_ids(
    write_set: ProjectWriteSetDiff,
    table_name: str,
    operation: str,
    *,
    identity_key: str = "id",
) -> set[int]:
    if operation == "created":
        values = write_set.created.get(table_name, ())
    elif operation == "deleted":
        values = write_set.deleted.get(table_name, ())
    elif operation == "updated":
        values = tuple(after for _before, after in write_set.updated.get(table_name, ()))
    else:
        raise ValueError(f"unknown write-set operation {operation!r}")
    identities = set()
    for value in values:
        identity = value.identity.get(identity_key)
        if not isinstance(identity, int):
            raise ValueError(
                f"{table_name} write identity lacks integer {identity_key}"
            )
        identities.add(identity)
    return identities


def _all_changed_ids(
    write_set: ProjectWriteSetDiff,
    table_name: str,
    *,
    identity_key: str = "id",
) -> set[int]:
    return _write_set_ids(
        write_set, table_name, "created", identity_key=identity_key
    ) | _write_set_ids(
        write_set, table_name, "updated", identity_key=identity_key
    )


def _require_changed_ids_within(
    write_set: ProjectWriteSetDiff,
    table_name: str,
    allowed_ids: set[int],
    *,
    identity_key: str = "id",
) -> None:
    unexpected = _all_changed_ids(
        write_set, table_name, identity_key=identity_key
    ) - allowed_ids
    if unexpected:
        raise ValueError(
            f"{table_name} writes are not traceable to fresh Admission outcomes"
        )


def _require_foreign_key_subset(
    session: Session,
    write_set: ProjectWriteSetDiff,
    model: type[Any],
    table_name: str,
    foreign_key_name: str,
    allowed_foreign_ids: set[int],
    *,
    identity_key: str = "id",
) -> None:
    changed_ids = _all_changed_ids(
        write_set, table_name, identity_key=identity_key
    )
    if not changed_ids:
        return
    identity_column = getattr(model, identity_key)
    values = tuple(
        session.scalars(
            select(model).where(identity_column.in_(changed_ids))
        ).all()
    )
    if len(values) != len(changed_ids) or any(
        getattr(value, foreign_key_name) not in allowed_foreign_ids
        for value in values
    ):
        raise ValueError(
            f"{table_name} writes are not traceable to fresh Admission outcomes"
        )


def _admission_audit_ids(
    session: Session,
    write_set: ProjectWriteSetDiff,
    *,
    dependency_ids: set[int],
    lineage_ids: set[int],
) -> set[int]:
    audit_ids = _all_changed_ids(write_set, "audit_log")
    if not audit_ids:
        return set()
    audits = tuple(
        session.scalars(select(AuditLog).where(AuditLog.id.in_(audit_ids))).all()
    )
    allowed = {
        audit.id
        for audit in audits
        if (
            audit.entity_type == "dependency"
            and audit.entity_id in dependency_ids
        )
        or (
            audit.entity_type == "commitment_lineage"
            and audit.entity_id in lineage_ids
        )
    }
    return allowed


def _run_admission_without_declaration(
    session: Session,
    project_id: int,
    *,
    write_candidate_ids: Sequence[int],
) -> Any:
    """Run the two Admission families after exact Runs were declared."""

    candidates = tuple(
        session.scalars(
            select(Candidate).where(
                Candidate.project_id == project_id,
                Candidate.id.in_(write_candidate_ids),
            )
        ).all()
    )
    by_kind = {
        kind: tuple(sorted(candidate.id for candidate in candidates if candidate.kind == kind))
        for kind in ("dependency", "event")
    }
    if {candidate.id for candidate in candidates} != set(write_candidate_ids):
        raise ValueError("Product Proving Admission scope lost a fresh Candidate")
    unexpected_kinds = sorted(
        {candidate.kind for candidate in candidates} - {"dependency", "event"}
    )
    if unexpected_kinds:
        raise ValueError(
            "Product Proving Admission scope contains unsupported Candidate kinds: "
            + ", ".join(unexpected_kinds)
        )

    return {
        "dependencies": dependency_admission.run_dependency_admission(
            session,
            project_id,
            write_candidate_ids=by_kind["dependency"],
        ),
        "events": event_admission.run_event_admission(
            session,
            project_id,
            write_candidate_ids=by_kind["event"],
        ),
    }


def _require_project(session: Session, slug: str) -> Project:
    project = session.scalar(select(Project).where(Project.slug == slug))
    if project is None:
        raise ValueError(f"Project {slug!r} does not exist")
    return project


def _exact_documents(
    session: Session, project_id: int, document_ids: Sequence[int]
) -> tuple[Document, ...]:
    ids = tuple(document_ids)
    if not ids or len(set(ids)) != len(ids) or any(
        not isinstance(value, int) or value <= 0 for value in ids
    ):
        raise ValueError("Product Proving Document ids must be unique positive ids")
    documents = tuple(
        session.scalars(
            select(Document).where(Document.id.in_(ids)).order_by(Document.id)
        ).all()
    )
    if len(documents) != len(ids) or any(
        document.project_id != project_id for document in documents
    ):
        raise ValueError("Product Proving Documents do not exactly match the Project")
    return documents


def _live_migration_head(session: Session) -> str:
    revisions = tuple(
        session.scalars(text("select version_num from alembic_version order by version_num"))
    )
    if len(revisions) != 1 or not revisions[0]:
        raise ValueError("database does not have one exact migration head")
    return str(revisions[0])


def _legacy_milestone_source_sha256(
    registration: MilestoneRegistration,
) -> str:
    """Canonical source identity for pre-registration-digest legacy rows."""

    return policy.canonical_sha256(
        {
            "schema_version": "corridor.legacy-milestone-source.v1",
            "source_name": registration.source_name,
            "source_row": registration.source_row_json,
        }
    )


def _require_candidate_input(
    run: ExtractionRun, value: Any
) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(
            f"Extraction Run {run.id} Candidate input snapshot is malformed"
        )
    if value.get("source_document_id") != run.document_id:
        raise ValueError(
            f"Extraction Run {run.id} Candidate input names another Document"
        )
    if value.get("prompt_version") != run.prompt_version:
        raise ValueError(
            f"Extraction Run {run.id} Candidate prompt does not match its receipt"
        )
    if value.get("model") != run.model:
        raise ValueError(
            f"Extraction Run {run.id} Candidate model does not match its receipt"
        )
    return value


def _rows(session: Session, model: type[Any], criterion: Any) -> tuple[Any, ...]:
    primary_keys = tuple(model.__table__.primary_key.columns)
    query = select(model).where(criterion if criterion is not None else false())
    if primary_keys:
        query = query.order_by(*primary_keys)
    return tuple(session.scalars(query).all())


def _ids(values: Sequence[Any]) -> tuple[int, ...]:
    return tuple(value.id for value in values)


@dataclass(frozen=True)
class _RawProjectRow:
    key: int
    model: type[Any]
    value: Any
    exact: Mapping[str, Any]
    primary_names: frozenset[str]

    @property
    def table_name(self) -> str:
        return self.model.__table__.name


_GENERATED_TIME_FIELDS = frozenset(
    {
        "adjudicated_at",
        "approved_at",
        "captured_at",
        "completed_at",
        "created_at",
        "declared_at",
        "designated_at",
        "dismissed_at",
        "evaluated_at",
        "frozen_at",
        "generated_at",
        "observed_at",
        "recorded_at",
        "released_at",
        "rendered_at",
        "retired_at",
        "sealed_at",
        "settled_at",
        "started_at",
        "ts",
    }
)


def _fingerprint_model_rows(
    model_rows: Sequence[tuple[type[Any], Sequence[Any]]],
) -> dict[str, tuple[ProjectRowFingerprint, ...]]:
    """Fingerprint rows while preserving relationship meaning across IDs.

    A stable digest omits a row's generated primary identity and operation
    timestamps.  Schema-declared foreign keys are not dropped: each is
    replaced with the referenced row's own stable content digest.  If the
    referenced row is outside the protected capture or is ambiguous, the
    exact foreign-key value remains.  That conservative fallback can reject
    equivalent passes, but it cannot manufacture equivalence after a
    relationship changed.

    IDs embedded in opaque JSON are also retained.  Only the schema can tell
    us that a number is an identity and what it references; guessing from a
    JSON key name would recreate the masking bug this function prevents.
    """

    raw_rows: list[_RawProjectRow] = []
    by_table: dict[str, list[_RawProjectRow]] = {}
    for model, values in model_rows:
        table = model.__table__
        by_table.setdefault(table.name, [])
        primary_names = frozenset(column.name for column in table.primary_key.columns)
        for value in values:
            exact = {
                column.name: _json_value(getattr(value, column.name))
                for column in table.columns
            }
            row = _RawProjectRow(
                key=id(value),
                model=model,
                value=value,
                exact=exact,
                primary_names=primary_names,
            )
            raw_rows.append(row)
            by_table.setdefault(table.name, []).append(row)

    reference_index: dict[tuple[str, str, str], list[_RawProjectRow]] = {}
    for row in raw_rows:
        for column in row.model.__table__.columns:
            reference_index.setdefault(
                (
                    row.table_name,
                    column.name,
                    _canonical_json(row.exact[column.name]),
                ),
                [],
            ).append(row)

    base_digests: dict[int, str] = {}
    stable_digests: dict[int, str] = {}

    def known_candidate_json(value: Any) -> Any:
        """Remap the identities in the two documented Candidate snapshots."""

        if isinstance(value, list):
            return [known_candidate_json(item) for item in value]
        if not isinstance(value, Mapping):
            return value
        normalized: dict[str, Any] = {}
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            name = str(key)
            if name == "confidence":
                # Confidence is an extractor aid, not a Candidate fact or
                # Evidence member in the repeatability contract.
                continue
            if name == "candidate_id":
                # The containing immutable snapshot owns the candidate facts;
                # this is only the generated row identity assigned before the
                # Run existed.
                continue
            target_table = (
                "projects"
                if name == "project_id"
                else "documents"
                if name in {"document_id", "source_document_id"}
                else None
            )
            if target_table is not None and isinstance(item, int):
                targets = reference_index.get(
                    (target_table, "id", _canonical_json(item)), ()
                )
                if len(targets) == 1:
                    normalized[name] = {
                        "table": target_table,
                        "content_sha256": base_digest(targets[0]),
                    }
                    continue
            normalized[name] = known_candidate_json(item)
        return normalized

    def stable_column_value(row: _RawProjectRow, column_name: str) -> Any:
        value = row.exact[column_name]
        if row.table_name == "candidates" and column_name == "confidence":
            return None
        if (row.table_name, column_name) in {
            ("candidates", "payload_json"),
            ("extraction_runs", "candidate_inputs_json"),
        }:
            return known_candidate_json(value)
        return value

    def base_digest(row: _RawProjectRow) -> str:
        cached = base_digests.get(row.key)
        if cached is not None:
            return cached
        content = {}
        for column in row.model.__table__.columns:
            if (
                column.name in row.primary_names
                or column.name in _GENERATED_TIME_FIELDS
                or column.foreign_keys
            ):
                continue
            content[column.name] = stable_column_value(row, column.name)
        digest = _json_sha256(content)
        base_digests[row.key] = digest
        return digest

    def stable_digest(row: _RawProjectRow, stack: frozenset[int]) -> str:
        cached = stable_digests.get(row.key)
        if cached is not None:
            return cached
        content: dict[str, Any] = {}
        for column in row.model.__table__.columns:
            if column.name in _GENERATED_TIME_FIELDS:
                continue
            foreign_keys = tuple(
                sorted(
                    (
                        item
                        for item in column.foreign_keys
                        if _column_is_standalone_identity(item.column)
                    ),
                    key=lambda item: (
                        item.column.table.name,
                        item.column.name,
                    ),
                )
            )
            # A primary key that is also a relationship (join/projection
            # tables) must keep that relationship. A generated scalar PK may
            # be omitted.
            if column.name in row.primary_names and not foreign_keys:
                continue
            exact_value = row.exact[column.name]
            if not foreign_keys or exact_value is None:
                content[column.name] = stable_column_value(row, column.name)
                continue
            resolved: list[dict[str, str]] = []
            for foreign_key in foreign_keys:
                target_table = foreign_key.column.table.name
                target_column = foreign_key.column.name
                targets = reference_index.get(
                    (
                        target_table,
                        target_column,
                        _canonical_json(exact_value),
                    ),
                    (),
                )
                if len(targets) != 1:
                    continue
                [target] = targets
                target_digest = (
                    base_digest(target)
                    if target.key in stack
                    else stable_digest(target, stack | {row.key})
                )
                resolved.append(
                    {
                        "table": target_table,
                        "content_sha256": target_digest,
                    }
                )
            content[column.name] = (
                {"references": sorted(resolved, key=_canonical_json)}
                if resolved
                else exact_value
            )
        digest = _json_sha256(content)
        stable_digests[row.key] = digest
        return digest

    output: dict[str, tuple[ProjectRowFingerprint, ...]] = {}
    for table_name, values in sorted(by_table.items()):
        fingerprints = []
        for row in values:
            identity = {
                name: row.exact[name] for name in sorted(row.primary_names)
            }
            fingerprints.append(
                ProjectRowFingerprint(
                    identity=identity,
                    content_sha256=_json_sha256(row.exact),
                    stable_content_sha256=stable_digest(row, frozenset()),
                )
            )
        output[table_name] = tuple(fingerprints)
    return output


def _column_is_standalone_identity(column: Any) -> bool:
    """Whether one referenced value, without composite peers, finds one row."""

    table = column.table
    if column.primary_key and len(table.primary_key.columns) == 1:
        return True
    return any(
        constraint.__class__.__name__ == "UniqueConstraint"
        and len(constraint.columns) == 1
        and column.name in constraint.columns
        for constraint in table.constraints
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return {"bytes": len(value), "sha256": sha256(value).hexdigest()}
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _fingerprint_json(item: ProjectRowFingerprint) -> dict[str, Any]:
    return {
        "identity": dict(item.identity),
        "content_sha256": item.content_sha256,
        "stable_content_sha256": item.stable_content_sha256,
    }


def _canonical_json(value: Any) -> str:
    return digests.canonical_json(value).decode()


_json_sha256 = digests.canonical_sha256


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _is_git_revision(value: str) -> bool:
    return len(value) == 40 and all(
        character in "0123456789abcdef" for character in value
    )
