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

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from typing import Any, Protocol

from sqlalchemy import and_, false, or_, select, text
from sqlalchemy.orm import Session

from corridor import dependency_admission, event_admission, policy
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
    DisputeSettlement,
    DocPage,
    Document,
    DocumentQuarantine,
    EventAdmissionAcceptanceReceipt,
    EventAdmissionActivation,
    EventAdmissionOutcome,
    EventCohortReceipt,
    EvidenceLink,
    ExternalParty,
    ExternalPartyStatement,
    ExternalReportArtifact,
    ExternalReportRelease,
    ExtractionRun,
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
    ExtractionConfiguration,
    ObservedPreflight,
    compare_candidate_sets,
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

    @property
    def extraction_equal(self) -> bool:
        return not self.extraction_failures and all(
            comparison.equal for comparison in self.extraction_comparisons
        )


class GitObserver(Protocol):
    def __call__(self, repo_root: Path) -> GitCheckoutObservation: ...


class PromptSourceResolver(Protocol):
    def __call__(
        self, prompt_version: str, repo_root: Path
    ) -> Sequence[tuple[str, bytes]]: ...


ExtractionOperation = Callable[[Session, Document], int | ExtractionRun]
ActiveRunOperation = Callable[[Session, int, int], Any]
AdmissionOperation = Callable[[Session, int], Any]


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


def observe_product_proving_preflight(
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


def load_extraction_run_candidate_set(
    session: Session,
    run_id: int,
    *,
    repo_root: Path | str | None = None,
    prompt_source_resolver: PromptSourceResolver | None = None,
) -> ExtractionRunCandidateSet:
    """Load one immutable, completed run and bind it to deployed prompt bytes."""

    run = session.get(ExtractionRun, run_id)
    if run is None:
        raise ValueError(f"Extraction Run {run_id} does not exist")
    if run.outcome != "completed" or run.page_errors != 0:
        raise ValueError(f"Extraction Run {run_id} is not complete without errors")
    if not isinstance(run.candidate_inputs_json, list):
        raise ValueError(
            f"Extraction Run {run_id} has no immutable Candidate input snapshot"
        )
    if run.candidate_count != len(run.candidate_inputs_json):
        raise ValueError(
            f"Extraction Run {run_id} Candidate input count does not match its receipt"
        )
    if not run.prompt_version or not run.schema_version:
        raise ValueError(
            f"Extraction Run {run_id} has incomplete extractor configuration"
        )
    candidates: tuple[Mapping[str, Any], ...] = tuple(
        _require_candidate_input(run, value)
        for value in run.candidate_inputs_json
    )
    root = (
        Path(repo_root).resolve()
        if repo_root is not None
        else Path(__file__).resolve().parents[2]
    )
    resolver = prompt_source_resolver or deployed_prompt_sources
    sources = tuple(sorted(resolver(run.prompt_version, root), key=lambda item: item[0]))
    if not sources or any(
        not isinstance(name, str)
        or not name
        or not isinstance(source_bytes, bytes)
        for name, source_bytes in sources
    ):
        raise ValueError(
            f"Extraction Run {run_id} deployed prompt sources are invalid"
        )
    configuration = ExtractionConfiguration(
        prompt_version=run.prompt_version,
        model=run.model,
        schema_version=run.schema_version,
        prompt_sha256=policy.source_digest(sources),
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
    *,
    repo_root: Path | str | None = None,
    prompt_source_resolver: PromptSourceResolver | None = None,
) -> CandidateSetComparison:
    """Compare two completed attempts for the same exact Document."""

    baseline = load_extraction_run_candidate_set(
        session,
        baseline_run_id,
        repo_root=repo_root,
        prompt_source_resolver=prompt_source_resolver,
    )
    fresh = load_extraction_run_candidate_set(
        session,
        fresh_run_id,
        repo_root=repo_root,
        prompt_source_resolver=prompt_source_resolver,
    )
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


def deployed_prompt_sources(
    prompt_version: str, repo_root: Path
) -> Sequence[tuple[str, bytes]]:
    """Resolve every deployed prompt/reader source used by a run version.

    Old prompt files remain immutable in the repository, so historical Runs
    stay verifiable.  Native spreadsheet extraction has no prompt; its exact
    reader source is the configuration-bearing deployed bytes instead.
    """

    filenames: tuple[str, ...]
    if prompt_version.startswith("minutes_v"):
        filenames = (f"prompts/{prompt_version}.md",)
    elif prompt_version.startswith("agreement_v"):
        filenames = (f"prompts/{prompt_version}.md",)
    elif prompt_version.startswith("matrix_tiered_v"):
        suffix = prompt_version.removeprefix("matrix_tiered_v")
        filenames = (
            f"prompts/matrix_structure_v{suffix}.md",
            "prompts/matrix_v1.md",
        )
    elif prompt_version == "sheet_native_v1":
        filenames = ("src/corridor/extract_sheet.py",)
    else:
        raise ValueError(
            f"no deployed prompt-source registry for {prompt_version!r}"
        )
    sources: list[tuple[str, bytes]] = []
    for filename in filenames:
        path = repo_root / filename
        if not path.is_file():
            raise ValueError(
                f"deployed prompt source {filename!r} does not exist"
            )
        sources.append((filename, path.read_bytes()))
    return tuple(sources)


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


def residual_candidate_ids_from_active_runs(
    session: Session,
    *,
    project_slug: str,
    active_runs: Mapping[int, int],
) -> tuple[int, ...]:
    """Return pending Candidates owned by the exact fresh Active Runs.

    This is an after-Admission observation.  It refuses a stale or cross-
    project mapping before reading residue, so a proving receipt cannot blend
    Candidates from a superseded extraction attempt into the practitioner
    work population.
    """

    if not active_runs:
        raise ValueError("residual Candidate observation needs Active Runs")
    project = _require_project(session, project_slug)
    documents = _exact_documents(
        session, project.id, tuple(active_runs.keys())
    )
    run_pairs = []
    for document in documents:
        run_id = active_runs[document.id]
        active = session.get(
            ActiveExtractionRun,
            document.id,
            populate_existing=True,
        )
        run = session.get(ExtractionRun, run_id)
        if (
            active is None
            or active.extraction_run_id != run_id
            or run is None
            or run.document_id != document.id
            or run.outcome != "completed"
            or run.page_errors != 0
        ):
            raise ValueError(
                f"Document {document.id} does not have the exact completed fresh Active Run"
            )
        run_pairs.append(
            and_(
                Candidate.source_document_id == document.id,
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


def run_bounded_product_proving_operations(
    session: Session,
    *,
    project_slug: str,
    baseline_runs: Mapping[int, int],
    extraction_operation: ExtractionOperation,
    active_run_operation: ActiveRunOperation,
    admission_operation: AdmissionOperation | None = None,
    repo_root: Path | str | None = None,
    prompt_source_resolver: PromptSourceResolver | None = None,
) -> BoundedProductProvingOperations:
    """Extract exact Documents, compare, then declare/admit only on equality.

    Extraction attempts are allowed to leave their immutable receipts.  Active
    Run declaration and Admission are a separate phase and are never entered
    after an extractor error, a failed run, a configuration mismatch, or a
    semantic Candidate difference.
    """

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
            fresh_run_id = result.id if isinstance(result, ExtractionRun) else result
            if not isinstance(fresh_run_id, int) or fresh_run_id <= 0:
                raise ValueError("extractor did not return one Extraction Run id")
            if fresh_run_id == baseline_runs[document.id]:
                raise ValueError("extractor returned the pinned baseline Run")
            fresh_run_ids[document.id] = fresh_run_id
            comparison = compare_extraction_runs(
                session,
                baseline_runs[document.id],
                fresh_run_id,
                repo_root=repo_root,
                prompt_source_resolver=prompt_source_resolver,
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
        return BoundedProductProvingOperations(
            project_id=project.id,
            fresh_run_ids=fresh_run_ids,
            extraction_comparisons=tuple(comparisons),
            extraction_failures=tuple(failures),
            active_run_document_ids=(),
            admission_started=False,
            admission_result=None,
        )

    admission = admission_operation or _run_admission_without_declaration
    # One savepoint keeps a declaration failure from leaving a partially
    # changed Active Run set.  The caller retains transaction/commit control.
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
    return BoundedProductProvingOperations(
        project_id=project.id,
        fresh_run_ids=fresh_run_ids,
        extraction_comparisons=tuple(comparisons),
        extraction_failures=(),
        active_run_document_ids=tuple(item.id for item in documents),
        admission_started=True,
        admission_result=admission_result,
    )


def _run_admission_without_declaration(session: Session, project_id: int) -> Any:
    """Run the two Admission families after exact Runs were declared."""

    return {
        "dependencies": dependency_admission.run_dependency_admission(
            session, project_id
        ),
        "events": event_admission.run_event_admission(session, project_id),
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
                    column.foreign_keys,
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
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _json_sha256(value: Any) -> str:
    return sha256(_canonical_json(value).encode()).hexdigest()


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _is_git_revision(value: str) -> bool:
    return len(value) == 40 and all(
        character in "0123456789abcdef" for character in value
    )
