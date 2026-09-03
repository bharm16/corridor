"""Retain a weekly Coordination Report reading and prepare its external PDF.

A scheduled publication does two things a live report cannot.  It **retains** one
coherent internal reading so the week's state stays readable after the working
view moves on, and it **prepares** — never releases — the exact external PDF a
project person may later authorize.  Both supplement the working view; neither
becomes a ``ReportRun`` and neither advances the comparison baseline, which
ADR-0053 fixes to the last Report Approved for Release and nothing else.

The comparison predecessor is selected once, per occurrence, from that one
predicate (``changes.last_released_report``) and bound durably with the reading,
so refreshing the working view, preparing another provenance mode, or retrying
the same occurrence cannot silently re-point the predecessor or erase the
weekly window.  The external PDF reuses the established render-and-retain
boundary (``report_release.prepare_external_report``), so its exact bytes,
digest, Evaluation, provenance mode, and covered identities are bound the same
way a manual preparation binds them — and only a separate designated-human act
can release it (ADR-0040).

Each retained reading also names the accepted Project Record revision it was
taken against (#602).  That reference is what the reading is bound to;
``snapshot_json`` beside it is a **rebuildable compatibility cache**, retained
only until #603 proves that rebuilding a reading from its revision gives the
same answer.  The revision is read in the same writing transaction as the
reading itself, so a retained row can never name one taken a moment apart from
the state it recorded.

This module owns no schedule, timer, or clock.  The one supervised Due Work
runtime (#332) discovers, claims, and retries occurrences; this is the bounded,
idempotent work one claimed occurrence performs.  A repeated trigger, a
competing worker, an abandoned claim, or a retry converges on the occurrence's
one retained row rather than producing a second snapshot or artifact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from corridor.changes import accepted_revision_id, last_released_report, snapshot
from corridor.export import to_pdf_bytes
from corridor.models import (
    DueWorkOccurrence,
    DueWorkSchedule,
    ExternalReportArtifact,
    ExternalReportRelease,
    Project,
    ScheduledReportPublication,
)
from corridor.report import build_report, render
from corridor.report_release import RenderedExternalReport, prepare_external_report

# The one server-owned handler key this module's work runs under.  It matches
# ``due_work.HANDLER_REPORT_PUBLICATION``; the constant lives here because this
# module is the lower layer and the runtime imports its execution, never the
# reverse.
HANDLER_KEY = "report_publication"

# v2 names the accepted Project Record revision the reading was produced
# against (#602); v1 receipts named none, and are readable exactly as they
# were written.
_RESULT_SCHEMA_VERSION = "report-publication-result-v2"


class ReportPublicationRefusal(ValueError):
    """A scheduled reading cannot be rendered, retained, or prepared safely."""


@dataclass(frozen=True)
class RetainedPublication:
    """One retained weekly reading and the release state of its prepared PDF."""

    public_id: str
    occurrence_public_id: str
    provenance_mode: str
    evaluated_on: date
    observed_at: datetime
    ruleset_version: str
    predecessor_release_id: int | None
    window_start: date | None
    comparison_window_days: int | None
    revision_id: int | None
    prepared_artifact_id: int | None
    prepared_artifact_name: str | None
    prepared_artifact_sha256: str | None
    state: str  # snapshot_only | prepared | released
    release_id: int | None


@dataclass(frozen=True)
class FailedPublicationSlot:
    """A due publication slot that produced no retained reading."""

    occurrence_public_id: str
    due_at: datetime
    state: str
    last_error_code: str | None


@dataclass(frozen=True)
class ProjectPublicationHistory:
    """Everything the ordinary report controls show about scheduled work."""

    retained: tuple[RetainedPublication, ...]
    failed_slots: tuple[FailedPublicationSlot, ...]


def execute_report_publication(
    session_factory,
    *,
    occurrence_id: int,
    schedule_id: int,
    clock,
    render_pdf=to_pdf_bytes,
) -> dict:
    """Retain one coherent reading for a claimed occurrence, idempotently.

    The occurrence's retained row is the convergence point: if one already
    exists this returns its summary without re-reading the project or
    re-rendering a PDF, so a retry, an abandoned-claim recovery, or a competing
    worker never produces a second snapshot or artifact.

    The reading and, when the schedule declared external preparation, the PDF
    bytes are taken once, from one ``build_report`` call, so the retained
    snapshot and the prepared artifact can never combine readings taken at
    different times.  The observation is the actual execution moment; a delayed
    run records the reading it can support now and never backdates
    execution-time facts into a historical slot.
    """

    observed_at = _aware_utc(clock.now())
    observation_date = observed_at.date()

    with session_factory() as reading:
        existing = _by_occurrence(reading, occurrence_id)
        if existing is not None:
            return summarize_publication(existing)
        schedule = reading.get(DueWorkSchedule, schedule_id)
        if schedule is None:
            raise ReportPublicationRefusal("publication schedule disappeared")
        scope = dict(schedule.scope_json)
        project_id = int(scope["project_id"])
        provenance_mode = str(scope["provenance_mode"])
        prepare_external = bool(scope["prepare_external_pdf"])
        configuration_version = schedule.configuration_version
        project = reading.get(Project, project_id)
        if project is None:
            raise ReportPublicationRefusal(f"project {project_id} does not exist")
        project_slug = project.slug

    document_only = provenance_mode == "document-only"

    try:
        with session_factory() as writing:
            with writing.begin():
                existing = _by_occurrence(writing, occurrence_id)
                if existing is not None:
                    return summarize_publication(existing)

                report = build_report(
                    writing,
                    project_id,
                    today=observation_date,
                    document_only=document_only,
                )
                if report.evaluation is None:
                    raise ReportPublicationRefusal(
                        "a retained reading requires one Evaluation"
                    )
                reading_snapshot = snapshot(
                    writing,
                    project_id,
                    evaluation=report.evaluation,
                    committed_dates=report.committed_dates,
                )

                predecessor = last_released_report(
                    writing, project_id, provenance_mode=provenance_mode
                )
                window_start = (
                    predecessor.evaluated_on if predecessor is not None else None
                )
                comparison_window_days = (
                    max(0, (observation_date - window_start).days)
                    if window_start is not None
                    else None
                )

                prepared_artifact_id = None
                if prepare_external:
                    # The expensive PDF render is pure CPU over the in-memory
                    # report HTML: it touches no row and holds no project
                    # mutation lock (only the prior read SELECTs, which do not
                    # block writers under MVCC).
                    artifact_name = (
                        f"{project_slug}-coordination-"
                        f"{report.evaluation.today.isoformat()}"
                        f"{'-document-only' if document_only else ''}.pdf"
                    )
                    rendered = RenderedExternalReport(
                        artifact_name=artifact_name,
                        pdf_bytes=render_pdf(render(report)),
                        report=report,
                    )
                    artifact = prepare_external_report(
                        writing,
                        project_id=project_id,
                        rendered=rendered,
                    )
                    prepared_artifact_id = artifact.id

                publication = ScheduledReportPublication(
                    public_id=_public_id(occurrence_id, project_id, observation_date),
                    occurrence_id=occurrence_id,
                    schedule_id=schedule_id,
                    project_id=project_id,
                    # Read in this same writing transaction, beside the reading
                    # it binds, so the retained row names the accepted revision
                    # its own snapshot was taken against rather than whichever
                    # one a later reader happens to find (#602).
                    revision_id=accepted_revision_id(writing, project_id),
                    configuration_version=configuration_version,
                    provenance_mode=provenance_mode,
                    predecessor_release_id=(
                        predecessor.id if predecessor is not None else None
                    ),
                    prepared_artifact_id=prepared_artifact_id,
                    evaluated_on=observation_date,
                    window_start=window_start,
                    comparison_window_days=comparison_window_days,
                    ruleset_version=report.ruleset_version,
                    thresholds_json=reading_snapshot["thresholds"],
                    snapshot_json=reading_snapshot,
                    observed_at=observed_at,
                )
                writing.add(publication)
                writing.flush([publication])
                return summarize_publication(publication)
    except IntegrityError:
        # The occurrence was retained concurrently; its bytes and predecessor
        # stand.  The rolled-back transaction discarded this attempt's artifact,
        # so convergence leaves no orphaned preparation.
        with session_factory() as reread:
            existing = _by_occurrence(reread, occurrence_id)
            if existing is None:
                raise
            return summarize_publication(existing)


def summarize_publication(publication: ScheduledReportPublication) -> dict:
    """A bounded, self-describing receipt of one retained reading."""

    return {
        "schema_version": _RESULT_SCHEMA_VERSION,
        "project_id": publication.project_id,
        "configuration_version": publication.configuration_version,
        "provenance_mode": publication.provenance_mode,
        "observed_at": _iso(publication.observed_at),
        "evaluated_on": publication.evaluated_on.isoformat(),
        "outcome": "retained",
        "snapshot_public_id": publication.public_id,
        # The reference the reading is bound to, so the receipt an operator
        # reads says which accepted revision it was produced against (#602).
        "accepted_revision_id": publication.revision_id,
        "prepared": publication.prepared_artifact_id is not None,
        "prepared_artifact_id": publication.prepared_artifact_id,
        "has_prior_release": publication.predecessor_release_id is not None,
        "comparison_window_days": publication.comparison_window_days,
    }


def project_publication_history(
    session: Session, project_id: int
) -> ProjectPublicationHistory:
    """Read retained readings and failed slots for the ordinary report controls.

    Prepared, released, and snapshot-only readings are distinguished from one
    another, and a slot whose occurrence failed with no retained row is shown as
    a failure rather than a false success, so operations recovery is visible
    without a released artifact ever being invented.
    """

    publications = session.scalars(
        select(ScheduledReportPublication)
        .where(ScheduledReportPublication.project_id == project_id)
        .order_by(
            ScheduledReportPublication.observed_at.desc(),
            ScheduledReportPublication.id.desc(),
        )
    ).all()

    artifact_ids = [
        publication.prepared_artifact_id
        for publication in publications
        if publication.prepared_artifact_id is not None
    ]
    releases_by_artifact: dict[int, ExternalReportRelease] = {}
    if artifact_ids:
        releases_by_artifact = {
            release.artifact_id: release
            for release in session.scalars(
                select(ExternalReportRelease).where(
                    ExternalReportRelease.project_id == project_id,
                    ExternalReportRelease.artifact_id.in_(artifact_ids),
                )
            )
        }

    retained: list[RetainedPublication] = []
    for publication in publications:
        artifact = (
            session.get(ExternalReportArtifact, publication.prepared_artifact_id)
            if publication.prepared_artifact_id is not None
            else None
        )
        release = (
            releases_by_artifact.get(publication.prepared_artifact_id)
            if publication.prepared_artifact_id is not None
            else None
        )
        if publication.prepared_artifact_id is None:
            state = "snapshot_only"
        elif release is not None:
            state = "released"
        else:
            state = "prepared"
        retained.append(
            RetainedPublication(
                public_id=publication.public_id,
                occurrence_public_id=_occurrence_public_id(
                    session, publication.occurrence_id
                ),
                provenance_mode=publication.provenance_mode,
                evaluated_on=publication.evaluated_on,
                observed_at=publication.observed_at,
                ruleset_version=publication.ruleset_version,
                predecessor_release_id=publication.predecessor_release_id,
                revision_id=publication.revision_id,
                window_start=publication.window_start,
                comparison_window_days=publication.comparison_window_days,
                prepared_artifact_id=publication.prepared_artifact_id,
                prepared_artifact_name=artifact.artifact_name if artifact else None,
                prepared_artifact_sha256=artifact.pdf_sha256 if artifact else None,
                state=state,
                release_id=release.id if release is not None else None,
            )
        )

    retained_occurrence_ids = {
        publication.occurrence_id for publication in publications
    }
    failed_rows = session.scalars(
        select(DueWorkOccurrence)
        .join(
            DueWorkSchedule,
            DueWorkSchedule.id == DueWorkOccurrence.scheduled_job_id,
        )
        .where(
            DueWorkSchedule.project_id == project_id,
            DueWorkSchedule.handler_key == HANDLER_KEY,
            DueWorkOccurrence.state.in_(("failed", "retry_due")),
        )
        .order_by(DueWorkOccurrence.due_at.desc(), DueWorkOccurrence.id.desc())
    ).all()
    failed_slots = tuple(
        FailedPublicationSlot(
            occurrence_public_id=occurrence.public_id,
            due_at=occurrence.due_at,
            state=occurrence.state,
            last_error_code=occurrence.last_error_code,
        )
        for occurrence in failed_rows
        if occurrence.id not in retained_occurrence_ids
    )
    return ProjectPublicationHistory(
        retained=tuple(retained), failed_slots=failed_slots
    )


def _by_occurrence(
    session: Session, occurrence_id: int
) -> ScheduledReportPublication | None:
    return session.scalars(
        select(ScheduledReportPublication).where(
            ScheduledReportPublication.occurrence_id == occurrence_id
        )
    ).first()


def _occurrence_public_id(session: Session, occurrence_id: int) -> str:
    public_id = session.scalar(
        select(DueWorkOccurrence.public_id).where(
            DueWorkOccurrence.id == occurrence_id
        )
    )
    return public_id or f"due-occurrence:{occurrence_id}"


def _public_id(occurrence_id: int, project_id: int, observation_date: date) -> str:
    digest = sha256(
        f"{occurrence_id}:{project_id}:{observation_date.isoformat()}".encode()
    ).hexdigest()
    return f"report-pub:{digest[:32]}"


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ReportPublicationRefusal("publication clock must supply an aware datetime")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _aware_utc(value).isoformat()
