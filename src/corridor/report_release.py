"""Seal one already-rendered External Report PDF under a human's authority.

Internal Reports are regenerated whenever the Ledger changes.  An external
release has the opposite job: it preserves the exact PDF a project person
authorized, plus the Evaluation and frozen statement reading behind it.
This module is the public authority for that act; callers may prepare a PDF,
but none may substitute a Report URL, output path, or regenerating callback.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from pathlib import PurePath
from typing import TYPE_CHECKING

import pymupdf
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from corridor.export import to_pdf_bytes
from corridor.models import (
    DependencyEvent,
    ExternalReportArtifact,
    ExternalReportRelease,
    Project,
    ProjectRosterEntry,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.project_reading import validate_frozen_reading
from corridor.report import Report, assert_no_bare_cells, build_report, render

if TYPE_CHECKING:
    from corridor.dependency_events import PublishedPartyStatement


_UNRECORDED_ACTOR_DISPLAY = "Project person (display name not recorded)"
_LEGACY_ACTOR_DISPLAY = (
    "Project person (display name not retained in this legacy release)"
)
_PARTY_STATEMENT_REPORT_FIELDS = (
    ("external_party", "External Party"),
    ("supported_statement", "Supported statement"),
    ("timing", "Timing"),
    ("timing_precision", "Timing precision"),
    ("statement_type", "Statement type"),
    ("commitment_scope", "Commitment Scope"),
    ("open_status", "Open / past-due status"),
    ("internal_owner", "Internal Owner"),
    ("next_action", "Next Action"),
    ("action_due", "Action Due"),
    ("milestone_impact", "Milestone Impact"),
)


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
class ExternalReportCoveredPartyStatement:
    """One frozen party-level statement described without technical identity."""

    external_party: str
    supported_statement: str
    source_context: str
    statement_version_ids: tuple[int, ...]


@dataclass(frozen=True)
class ExternalReportReleaseHistory:
    """Project-language release history with the technical digest retained."""

    release_id: int
    artifact_name: str
    released_by: str
    released_by_display: str
    released_at: datetime
    evaluated_on: date
    ruleset_version: str
    provenance_mode: str
    covered_dependency_count: int
    covered_records: tuple[str, ...]
    covered_statement_version_count: int
    covered_party_statements: tuple[ExternalReportCoveredPartyStatement, ...]
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
    covered_party_statements: tuple[ExternalReportCoveredPartyStatement, ...]
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
    record_context = _record_context(report)
    _validate_party_statement_pdf_context(rendered.pdf_bytes, record_context)
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
        record_context_json=record_context,
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
    legacy_displays = _legacy_party_statement_displays(
        session, project_id, (artifact.record_context_json,)
    )
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
        covered_party_statements=_covered_party_statements(
            artifact.record_context_json, legacy_displays
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
    released_by_display = session.scalar(
        select(ProjectRosterEntry.display_name).where(
            ProjectRosterEntry.project_id == project_id,
            ProjectRosterEntry.principal_subject == principal.subject,
        )
    )
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
        released_by_display=released_by_display or _UNRECORDED_ACTOR_DISPLAY,
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
    """Read immutable metadata without reopening a Report or loading PDF blobs."""
    receipts = session.execute(
        select(
            ExternalReportRelease.id,
            ExternalReportRelease.artifact_name,
            ExternalReportRelease.released_by,
            ExternalReportRelease.released_by_display,
            ExternalReportRelease.released_at,
            ExternalReportRelease.evaluated_on,
            ExternalReportRelease.ruleset_version,
            ExternalReportRelease.provenance_mode,
            ExternalReportRelease.record_context_json,
            ExternalReportRelease.pdf_sha256,
        )
        .where(ExternalReportRelease.project_id == project_id)
        .order_by(ExternalReportRelease.released_at.desc(), ExternalReportRelease.id.desc())
    ).all()
    legacy_displays = _legacy_party_statement_displays(
        session,
        project_id,
        tuple(receipt.record_context_json for receipt in receipts),
    )
    return tuple(
        ExternalReportReleaseHistory(
            release_id=receipt.id,
            artifact_name=receipt.artifact_name,
            released_by=receipt.released_by,
            released_by_display=(
                receipt.released_by_display or _LEGACY_ACTOR_DISPLAY
            ),
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
            covered_party_statements=_covered_party_statements(
                receipt.record_context_json, legacy_displays
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
    covered_ids = tuple(dependency_id for dependency_id, _ in report.covered_records)
    if len(covered_ids) != len(set(covered_ids)):
        raise ReleaseRefusal("Report covered records disagree with its statement reading")
    try:
        validate_frozen_reading(
            project_id=project_id,
            evaluation=evaluation,
            statement_publication=publication,
            dependency_ids=covered_ids,
            document_only=report.document_only,
        )
    except ValueError as exc:
        raise ReleaseRefusal(str(exc)) from exc
    if evaluation.statement_publication is not publication:
        raise ReleaseRefusal("Evaluation and Report do not share one frozen statement reading")
    if dict(report.committed_dates) != dict(evaluation.committed_dates):
        raise ReleaseRefusal("Report and Evaluation Committed Date readings are inconsistent")
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
    visible_party_statements = tuple(
        statement for statement in publication.party_statements if not statement.is_closed
    )
    party_statements = [
        {
            "commitment_lineage_id": statement.current_event.commitment_lineage_id,
            "current_statement_event_id": statement.current_event.id,
            "published_statement_event_id": (
                statement.event.id if statement.event is not None else None
            ),
            "scope_decision_id": statement.scope_decision.id,
        }
        for statement in visible_party_statements
    ]
    report_fields = _party_statement_report_fields(report)
    context = {"dependencies": dependencies, "party_statements": party_statements}
    if visible_party_statements:
        context["party_statement_display"] = [
            {
                "current_statement_event_id": statement.current_event.id,
                **_party_statement_display_context(
                    statement, publication.document_only
                ),
                **(
                    {"report_fields": report_fields[statement.current_event.id]}
                    if statement.current_event.id in report_fields
                    else {}
                ),
            }
            for statement in visible_party_statements
        ]
    return context


def _party_statement_report_fields(report: Report) -> dict[int, dict[str, str]]:
    """Copy visible statement rows under stable release-context field names."""
    publication = report.statement_publication
    assert publication is not None
    visible_statements = tuple(
        statement for statement in publication.party_statements if not statement.is_closed
    )
    section = next(
        (
            candidate
            for candidate in report.sections
            if candidate.title == "External Party commitments"
        ),
        None,
    )
    rows = tuple(section.rows) if section is not None else ()
    if len(rows) != len(visible_statements):
        raise ReleaseRefusal(
            "the frozen Report statement identities do not match its visible rows"
        )
    expected_labels = {label for _, label in _PARTY_STATEMENT_REPORT_FIELDS}
    result = {}
    for statement, row in zip(visible_statements, rows, strict=True):
        values_by_label = {cell.label: cell.value for cell in row}
        if len(values_by_label) != len(row) or set(values_by_label) != expected_labels:
            raise ReleaseRefusal(
                "the frozen Report statement row does not match the release field contract"
            )
        result[statement.current_event.id] = {
            field_id: values_by_label[label]
            for field_id, label in _PARTY_STATEMENT_REPORT_FIELDS
        }
    return result


def _validate_party_statement_pdf_context(pdf_bytes: bytes, context: dict) -> None:
    """Prove retained statement fields are visible in the exact prepared bytes."""
    displays = tuple(context.get("party_statement_display", ()))
    if not displays:
        return
    try:
        with pymupdf.open(stream=pdf_bytes, filetype="pdf") as pdf:
            visible_text = _normalized_visible_text(
                "\n".join(page.get_text() for page in pdf)
            )
    except Exception as exc:
        raise ReleaseRefusal(
            "External Report PDF cannot be read to verify its frozen statement fields"
        ) from exc

    field_labels = dict(_PARTY_STATEMENT_REPORT_FIELDS)
    expected_pairs: Counter[str] = Counter()
    for display in displays:
        fields = display.get("report_fields")
        if not isinstance(fields, dict) or set(fields) != set(field_labels):
            raise ReleaseRefusal(
                "frozen External Party statement fields are incomplete"
            )
        for field_id, value in fields.items():
            expected_pairs[
                _normalized_visible_text(f"{field_labels[field_id]} {value}")
            ] += 1
    if any(
        visible_text.count(expected) < count
        for expected, count in expected_pairs.items()
    ):
        raise ReleaseRefusal(
            "PDF does not contain its frozen External Party statement fields"
        )


def _normalized_visible_text(value: object) -> str:
    return " ".join(str(value).split()).casefold()


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


def _covered_party_statements(
    context: dict,
    legacy_displays: dict[int, tuple[str, str, str]],
) -> tuple[ExternalReportCoveredPartyStatement, ...]:
    entries = tuple(context.get("party_statements", ()))
    frozen_display = {
        entry["current_statement_event_id"]: entry
        for entry in context.get("party_statement_display", ())
    }

    def display(entry: dict) -> tuple[str, str, str]:
        retained = frozen_display.get(entry.get("current_statement_event_id"))
        if all(
            retained and retained.get(key)
            for key in ("external_party", "supported_statement", "source_context")
        ):
            return (
                retained["external_party"],
                retained["supported_statement"],
                retained["source_context"],
            )
        published_event_id = entry.get("published_statement_event_id")
        if published_event_id is not None:
            return legacy_displays.get(
                published_event_id,
                (
                    "External Party name not retained",
                    "Statement wording not retained in this legacy release context",
                    "Source context not retained in this legacy release context",
                ),
            )
        current_display = legacy_displays.get(entry.get("current_statement_event_id"))
        return (
            (
                current_display[0]
                if current_display is not None
                else "External Party name not retained"
            ),
            "Current statement unsupported in this provenance mode",
            "Not published; source context not retained in this legacy release context",
        )

    covered = []
    for entry in entries:
        external_party, supported_statement, source_context = display(entry)
        covered.append(
            ExternalReportCoveredPartyStatement(
                external_party=external_party,
                supported_statement=supported_statement,
                source_context=source_context,
                statement_version_ids=tuple(
                    sorted(
                        {
                            entry[key]
                            for key in (
                                "current_statement_event_id",
                                "published_statement_event_id",
                            )
                            if entry.get(key) is not None
                        }
                    )
                ),
            )
        )
    return tuple(covered)


def _legacy_party_statement_displays(
    session: Session,
    project_id: int,
    contexts: tuple[dict, ...],
) -> dict[int, tuple[str, str, str]]:
    """Describe identity-only legacy contexts without inventing provenance.

    Exact event wording and party remain safe to read by retained identity.
    Cited filename, page, and quote did not travel in these old contexts, so
    live Evidence and Document rows must never be substituted for them.
    """
    legacy_event_ids = set()
    for context in contexts:
        frozen_ids = {
            entry.get("current_statement_event_id")
            for entry in context.get("party_statement_display", ())
        }
        legacy_event_ids.update(
            entry.get("published_statement_event_id")
            or entry.get("current_statement_event_id")
            for entry in context.get("party_statements", ())
            if entry.get("current_statement_event_id") not in frozen_ids
        )
    legacy_event_ids.discard(None)
    if not legacy_event_ids:
        return {}

    events_by_id = {
        event.id: event
        for event in session.scalars(
            select(DependencyEvent).where(
                DependencyEvent.project_id == project_id,
                DependencyEvent.id.in_(legacy_event_ids),
            )
        )
    }
    displays: dict[int, tuple[str, str, str]] = {}
    for event_id, event in events_by_id.items():
        if event.source_kind == "verbal":
            heard_on = (
                event.event_date.isoformat()
                if event.event_date
                else "date not recorded"
            )
            source_context = f"Verbal statement · conversation {heard_on}"
        else:
            source_context = (
                "Cited source context not retained in this legacy release context"
            )
        displays[event_id] = (
            event.stated_party or "Unstated External Party",
            event.description,
            source_context,
        )
    return displays


def _party_statement_display_context(
    statement: PublishedPartyStatement, document_only: bool
) -> dict[str, str]:
    event = statement.event
    current_event = statement.current_event
    external_party = (
        (event or current_event).stated_party or "Unstated External Party"
    )
    if event is None:
        mode = "Documents only" if document_only else "selected provenance"
        return {
            "external_party": external_party,
            "supported_statement": (
                "Current statement unsupported in this provenance mode"
            ),
            "source_context": f"Not published in the {mode} Report",
        }
    cited = statement.cited_provenance
    if cited is not None:
        source_context = (
            f"{cited.filename} · page {cited.page_no} · “{cited.quote}”"
        )
    else:
        heard_on = (
            event.event_date.isoformat()
            if event.event_date is not None
            else "date not recorded"
        )
        source_context = f"Verbal statement · conversation {heard_on}"
    return {
        "external_party": external_party,
        "supported_statement": event.description,
        "source_context": source_context,
    }
