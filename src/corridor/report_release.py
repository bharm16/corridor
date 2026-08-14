"""Seal one already-rendered External Report PDF under a human's authority.

Internal Reports are regenerated whenever the Ledger changes.  An external
release has the opposite job: it preserves the exact PDF a project person
authorized, plus the Evaluation and frozen statement reading behind it.
This module is the public authority for that act; callers may prepare a PDF,
but none may substitute a Report URL, output path, or regenerating callback.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from pathlib import PurePath

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from corridor.export import to_pdf_bytes
from corridor.models import ExternalReportArtifact, ExternalReportRelease, Project
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.report import Report, assert_no_bare_cells, build_report, render


class ReleaseRefusal(ValueError):
    """The supplied artifact or its frozen Report context is not releasable."""


class NoSuchReleasedReport(LookupError):
    """The requested release does not belong to this project."""


class ReleasedArtifactIntegrityError(ReleaseRefusal):
    """Stored bytes no longer match the receipt's content digest."""


@dataclass(frozen=True)
class RenderedExternalReport:
    """The only release input: already-rendered bytes paired with one Report."""

    artifact_name: str
    pdf_bytes: bytes
    report: Report


@dataclass(frozen=True)
class ExternalReportReleaseHistory:
    """Project-language release history with the technical digest retained."""

    release_id: int
    artifact_name: str
    released_by: str
    released_at: datetime
    evaluated_on: date
    ruleset_version: str
    provenance_mode: str
    covered_dependency_count: int
    covered_records: tuple[str, ...]
    covered_statement_version_count: int
    pdf_sha256: str


@dataclass(frozen=True)
class ExternalReportArtifactReview:
    """Frozen project-language context for one prepared PDF review."""

    artifact_id: int
    artifact_name: str
    rendered_at: datetime
    evaluated_on: date
    ruleset_version: str
    provenance_mode: str
    covered_records: tuple[str, ...]
    covered_statement_versions: tuple[int, ...]
    pdf_sha256: str


def render_external_report_pdf(
    session: Session,
    project_id: int,
    *,
    today: date | None = None,
    document_only: bool = False,
) -> RenderedExternalReport:
    """Prepare one PDF and its one frozen Report reading for a release control.

    Rendering occurs before the release service is called.  The service only
    receives the fixed bytes below and therefore cannot authorize a later
    regenerated Report.
    """
    project = session.get(Project, project_id)
    if project is None:
        raise NoSuchReleasedReport(f"no project {project_id}")
    report = build_report(
        session, project_id, today=today, document_only=document_only
    )
    evaluated_on = report.evaluation.today if report.evaluation is not None else None
    if evaluated_on is None:
        raise ReleaseRefusal("a released Report requires one Evaluation")
    return RenderedExternalReport(
        artifact_name=(
            f"{project.slug}-readiness-{evaluated_on.isoformat()}"
            f"{'-document-only' if document_only else ''}.pdf"
        ),
        pdf_bytes=to_pdf_bytes(render(report)),
        report=report,
    )


def prepare_external_report(
    session: Session,
    *,
    project_id: int,
    rendered: RenderedExternalReport,
) -> ExternalReportArtifact:
    """Store one fixed rendered PDF for a later, separate release decision.

    This is the sole boundary that turns a Report and its PDF bytes into a
    durable artifact.  A human release receives only this artifact identity,
    so it cannot recreate a Report after the Ledger has changed.
    """
    if session.get(Project, project_id) is None:
        raise ReleaseRefusal(f"no project {project_id}")
    _validate_rendered_report(project_id, rendered)
    report = rendered.report
    evaluation = report.evaluation
    assert evaluation is not None
    artifact = ExternalReportArtifact(
        project_id=project_id,
        artifact_name=rendered.artifact_name,
        format="pdf",
        pdf_bytes=bytes(rendered.pdf_bytes),
        pdf_sha256=sha256(rendered.pdf_bytes).hexdigest(),
        evaluated_on=evaluation.today,
        ruleset_version=evaluation.ruleset_version,
        evaluation_context_json=_evaluation_context(report),
        provenance_mode=(
            "document-only" if report.document_only else "all-supported-sources"
        ),
        record_context_json=_record_context(report),
    )
    with session.begin_nested():
        session.add(artifact)
        session.flush()
    return artifact


def review_prepared_external_report(
    session: Session, project_id: int, artifact_id: int
) -> ExternalReportArtifactReview:
    """Read the exact retained context a person reviews before release."""
    artifact = retrieve_prepared_external_report(session, project_id, artifact_id)
    return ExternalReportArtifactReview(
        artifact_id=artifact.id,
        artifact_name=artifact.artifact_name,
        rendered_at=artifact.rendered_at,
        evaluated_on=artifact.evaluated_on,
        ruleset_version=artifact.ruleset_version,
        provenance_mode=artifact.provenance_mode,
        covered_records=tuple(
            entry["ref_code"]
            for entry in artifact.record_context_json.get("dependencies", ())
        ),
        covered_statement_versions=_statement_version_ids(
            artifact.record_context_json
        ),
        pdf_sha256=artifact.pdf_sha256,
    )


def retrieve_prepared_external_report(
    session: Session, project_id: int, artifact_id: int
) -> ExternalReportArtifact:
    """Retrieve fixed pre-release bytes only after checking their digest."""
    artifact = session.get(ExternalReportArtifact, artifact_id)
    if artifact is None or artifact.project_id != project_id:
        raise ReleaseRefusal(
            f"no rendered External Report {artifact_id} in project {project_id}"
        )
    _validate_prepared_artifact(artifact)
    return artifact


def release_external_report(
    session: Session,
    *,
    project_id: int,
    artifact_id: int,
    principal: HumanPrincipal,
) -> ExternalReportRelease:
    """Persist one immutable, human-authorized receipt for one stored PDF.

    A renderer, Report, URL, output path, and ReportRun id are deliberately
    absent from this boundary.  The release acts on an artifact prepared by a
    separate command, which makes the person choose exact prior bytes rather
    than whatever a later Report regeneration would produce.
    """
    principal = require_human_principal(principal)
    if session.get(Project, project_id) is None:
        raise ReleaseRefusal(f"no project {project_id}")
    artifact = session.get(ExternalReportArtifact, artifact_id)
    if artifact is None or artifact.project_id != project_id:
        raise ReleaseRefusal(
            f"no rendered External Report {artifact_id} in project {project_id}"
        )
    _validate_prepared_artifact(artifact)
    existing = session.scalar(
        select(ExternalReportRelease).where(
            ExternalReportRelease.artifact_id == artifact.id
        )
    )
    if existing is not None:
        return existing
    receipt = ExternalReportRelease(
        project_id=project_id,
        artifact_id=artifact.id,
        artifact_name=artifact.artifact_name,
        format=artifact.format,
        pdf_bytes=bytes(artifact.pdf_bytes),
        pdf_sha256=artifact.pdf_sha256,
        evaluated_on=artifact.evaluated_on,
        ruleset_version=artifact.ruleset_version,
        evaluation_context_json=deepcopy(artifact.evaluation_context_json),
        provenance_mode=artifact.provenance_mode,
        record_context_json=deepcopy(artifact.record_context_json),
        released_by=principal.subject,
    )
    try:
        with session.begin_nested():
            session.add(receipt)
            session.flush()
    except IntegrityError:
        existing = session.scalar(
            select(ExternalReportRelease).where(
                ExternalReportRelease.artifact_id == artifact.id
            )
        )
        if existing is None:
            raise
        return existing
    return receipt


def retrieve_released_external_report(
    session: Session, project_id: int, release_id: int
) -> ExternalReportRelease:
    """Retrieve a sealed artifact only after checking its retained bytes."""
    receipt = session.get(ExternalReportRelease, release_id)
    if receipt is None or receipt.project_id != project_id:
        raise NoSuchReleasedReport(
            f"no released External Report {release_id} in project {project_id}"
        )
    if not receipt.digest_is_valid:
        raise ReleasedArtifactIntegrityError(
            f"released External Report {release_id} does not match its SHA-256 digest"
        )
    return receipt


def external_report_release_history(
    session: Session, project_id: int
) -> tuple[ExternalReportReleaseHistory, ...]:
    """Read immutable release history without reopening a live Report."""
    receipts = session.scalars(
        select(ExternalReportRelease)
        .where(ExternalReportRelease.project_id == project_id)
        .order_by(ExternalReportRelease.released_at.desc(), ExternalReportRelease.id.desc())
    ).all()
    return tuple(
        ExternalReportReleaseHistory(
            release_id=receipt.id,
            artifact_name=receipt.artifact_name,
            released_by=receipt.released_by,
            released_at=receipt.released_at,
            evaluated_on=receipt.evaluated_on,
            ruleset_version=receipt.ruleset_version,
            provenance_mode=receipt.provenance_mode,
            covered_dependency_count=len(
                receipt.record_context_json.get("dependencies", ())
            ),
            covered_records=tuple(
                entry["ref_code"]
                for entry in receipt.record_context_json.get("dependencies", ())
            ),
            covered_statement_version_count=_statement_version_count(
                receipt.record_context_json
            ),
            pdf_sha256=receipt.pdf_sha256,
        )
        for receipt in receipts
    )


def _validate_rendered_report(
    project_id: int, rendered: RenderedExternalReport
) -> None:
    if not isinstance(rendered.pdf_bytes, bytes) or not rendered.pdf_bytes.startswith(
        b"%PDF-"
    ):
        raise ReleaseRefusal("External Report release accepts already-rendered PDF bytes only")
    if not rendered.pdf_bytes.rstrip():
        raise ReleaseRefusal("External Report PDF bytes are empty")
    _validate_artifact_name(rendered.artifact_name)
    report = rendered.report
    evaluation = report.evaluation
    publication = report.statement_publication
    if evaluation is None or publication is None:
        raise ReleaseRefusal("released content requires one frozen Evaluation and statement reading")
    if report.document_only != publication.document_only:
        raise ReleaseRefusal("Report provenance mode disagrees with its statement reading")
    if evaluation.project_id != project_id or publication.project_id != project_id:
        raise ReleaseRefusal("released content belongs to another project")
    if evaluation.statement_publication is not publication:
        raise ReleaseRefusal("Evaluation and Report do not share one frozen statement reading")
    if evaluation.statement_publication_fingerprint != publication.fingerprint:
        raise ReleaseRefusal("Evaluation and Report statement versions are inconsistent")
    if dict(report.committed_dates) != dict(evaluation.committed_dates):
        raise ReleaseRefusal("Report and Evaluation Committed Date readings are inconsistent")
    if dict(evaluation.committed_dates) != publication.committed_dates:
        raise ReleaseRefusal("Evaluation and statement reading have inconsistent Committed Dates")
    covered_ids = tuple(dependency_id for dependency_id, _ in report.covered_records)
    if len(covered_ids) != len(set(covered_ids)) or set(covered_ids) != set(
        publication.by_dependency
    ):
        raise ReleaseRefusal("Report covered records disagree with its statement reading")
    try:
        assert_no_bare_cells(report)
    except Exception as exc:
        raise ReleaseRefusal("released content lacks supported provenance") from exc


def _validate_prepared_artifact(artifact: ExternalReportArtifact) -> None:
    """Refuse a tampered stored artifact before it can be copied into history."""
    if artifact.format != "pdf" or not artifact.pdf_bytes.startswith(b"%PDF-"):
        raise ReleaseRefusal("stored External Report artifact is not a PDF")
    if not artifact.pdf_bytes.rstrip():
        raise ReleaseRefusal("stored External Report PDF bytes are empty")
    _validate_artifact_name(artifact.artifact_name)
    if not artifact.digest_is_valid:
        raise ReleasedArtifactIntegrityError(
            f"rendered External Report {artifact.id} does not match its SHA-256 digest"
        )


def _validate_artifact_name(artifact_name: object) -> None:
    if not isinstance(artifact_name, str) or not artifact_name.strip():
        raise ReleaseRefusal("a released PDF needs an artifact name")
    path = PurePath(artifact_name)
    if path.name != artifact_name or "/" in artifact_name or "\\" in artifact_name:
        raise ReleaseRefusal("a release stores PDF bytes, never a filesystem path")
    if not artifact_name.lower().endswith(".pdf"):
        raise ReleaseRefusal("External Report release supports PDF only")


def _record_context(report: Report) -> dict:
    """Serialize exact record and statement-version identities without rereading.

    The Report already carries its frozen StatementPublication.  Querying
    current statement tables here could attach later state to an earlier PDF,
    so this copies only identities from that one frozen object.
    """
    publication = report.statement_publication
    assert publication is not None
    ref_codes = dict(report.covered_records)
    dependencies = [
        {
            "dependency_id": dependency_id,
            "ref_code": ref_codes[dependency_id],
            "current_statement_event_id": (
                statement.current_event.id if statement.current_event is not None else None
            ),
            "published_statement_event_id": (
                statement.event.id if statement.event is not None else None
            ),
        }
        for dependency_id, statement in sorted(publication.by_dependency.items())
    ]
    party_statements = [
        {
            "commitment_lineage_id": statement.current_event.commitment_lineage_id,
            "current_statement_event_id": statement.current_event.id,
            "published_statement_event_id": (
                statement.event.id if statement.event is not None else None
            ),
            "scope_decision_id": statement.scope_decision.id,
        }
        for statement in publication.party_statements
    ]
    return {"dependencies": dependencies, "party_statements": party_statements}


def _evaluation_context(report: Report) -> dict:
    """Persist every configured input that made the released Evaluation."""
    evaluation = report.evaluation
    assert evaluation is not None
    thresholds = evaluation.thresholds
    return {
        "evaluated_on": evaluation.today.isoformat(),
        "ruleset_version": evaluation.ruleset_version,
        "thresholds": {
            "stale_days": thresholds.stale_days,
            "due_soon_days": thresholds.due_soon_days,
            "action_due_soon_days": thresholds.action_due_soon_days,
        },
    }


def _statement_version_count(context: dict) -> int:
    return len(_statement_version_ids(context))


def _statement_version_ids(context: dict) -> tuple[int, ...]:
    version_ids = {
        entry[key]
        for group in ("dependencies", "party_statements")
        for entry in context.get(group, ())
        for key in ("current_statement_event_id", "published_statement_event_id")
        if entry.get(key) is not None
    }
    return tuple(sorted(version_ids))
