"""Report outputs: PDF and XLSX.

The XLSX is the ledger in the shape a project can actually use — and it
carries the citation columns rather than dropping them, because a
spreadsheet that loses the provenance is just the matrix they already had.

``to_pdf``/``to_pdf_bytes`` render ``report``'s HTML, so they inherit its
boundary: this is the internal and legacy-project artifact. An adopted project's
customer-issued Coordination Report is rendered by
``issue_rendering.render_weekly_report`` into one authorized release package, and
its updated workbook by ``workbook_render`` into the customer's own approved
template — not by this module (ADR-0086, ADR-0091).
"""

from __future__ import annotations

from corridor.accepted_field_reading import accepted_field_text, visible_native_statements, native_field_visible
from corridor.presentation import field_label

from pathlib import Path

from sqlalchemy.orm import Session

from corridor.dependency_events import StatementPublication
from corridor.exceptions import Evaluation, format_exception_label
from corridor.ledger import primary_evidence
from corridor.models import Project
from corridor.project_reading import FrozenProjectReading, freeze_project_reading
from corridor.presentation import (
    documentation_review_label,
    label,
    provenance_label,
    resolution_strategy_label,
)

COLUMNS = [
    "Ref",
    "Source ID",
    label("organization"),
    "Type",
    "Title",
    "Station from",
    "Station to",
    label("resolution_strategy"),
    label("promised_for"),
    "Promised timing source",
    label("required_by"),
    label("documentation_review"),
    label("required_documents"),
    label("constraint_alerts"),
    "Supporting document",
    "Source page",
    label("cited_passage"),
]


def to_pdf(html: str, path: Path | str) -> Path:
    """Render the report HTML to PDF.

    WeasyPrint carries native dependencies; a missing one should fail loudly
    here rather than silently producing no file.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(to_pdf_bytes(html))
    return path


def to_pdf_bytes(html: str) -> bytes:
    """Render report markup to the exact PDF bytes a release can seal.

    The release boundary accepts bytes rather than a renderer callback or a
    pathname.  Keeping this small renderer seam separate lets the release
    service retain exactly what it was handed without asking WeasyPrint to
    regenerate a report later.
    """
    from weasyprint import HTML

    pdf = HTML(string=html).write_pdf()
    if not isinstance(pdf, bytes) or not pdf.startswith(b"%PDF-"):
        raise RuntimeError("the PDF renderer did not return PDF bytes")
    return pdf


def to_xlsx(
    session: Session,
    project_id: int,
    path: Path | str,
    *,
    evaluation: Evaluation,
    statement_publication: StatementPublication,
    internal_working_copy: bool = False,
    frozen_reading: FrozenProjectReading | None = None,
) -> Path:
    """The ledger as a workbook, at the evaluation the report published.

    Both inputs are required: this workbook is the artefact a project
    forwards to an External Party. The evaluation supplies its Exceptions;
    the paired statement publication supplies date and provenance cells.
    Refusing a mismatched pair prevents either side from taking a second
    reading that disagrees with the report it was sent alongside.

    `internal_working_copy` marks the provenance sheet as an internal working
    download rather than an approved external release. It defaults off so the
    bytes stay identical for every existing caller; ADR-0040 seals only a PDF
    for external release, so no XLSX is ever an approved external artifact, and
    the marker states that plainly on the copy a coordinator pulls for itself.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font

    reading = frozen_reading or freeze_project_reading(
        session,
        project_id,
        document_only=statement_publication.document_only,
        evaluation=evaluation,
        statement_publication=statement_publication,
    )
    project = reading.project
    rows = list(reading.rows)

    by_dependency = {
        dependency_id: [format_exception_label(e) for e in found]
        for dependency_id, found in evaluation.by_dependency().items()
    }
    if reading.native_population is not None:
        return native_population_workbook(reading, path, internal_working_copy=internal_working_copy)
    evidence_by_dependency = primary_evidence(
        session, [row.reading.id for row in rows]
    )

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = label("constraint_log")

    sheet.append(COLUMNS)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(vertical="top")

    for row in rows:
        dependency = row.reading
        evidence = evidence_by_dependency.get(dependency.id)
        statement = statement_publication.by_dependency[dependency.id]
        sheet.append(
            [
                dependency.ref_code,
                dependency.source_ref,
                row.org_name,
                dependency.dep_type,
                dependency.title,
                dependency.station_from,
                dependency.station_to,
                (
                    resolution_strategy_label(dependency.resolution_strategy)
                    if dependency.resolution_strategy is not None
                    else None
                ),
                statement.committed_date,
                (
                    f"Cited statement — {statement.cited_provenance.filename} "
                    f"p.{statement.cited_provenance.page_no}: “"
                    f"{statement.cited_provenance.quote}”"
                    if statement.cited_provenance is not None
                    else (
                        f"{provenance_label('verbal')} — {statement.event.stated_party} "
                        f"told {statement.event.created_by} on {statement.event.event_date}"
                        if statement.event is not None
                        and statement.event.source_kind == "verbal"
                        and statement.committed_date is not None
                        else statement.source_attribution
                    )
                ),
                dependency.need_date,
                documentation_review_label(row.is_ready),
                dependency.evidence_required or "Not specified",
                ", ".join(sorted(by_dependency.get(dependency.id, ()))),
                evidence.filename if evidence else None,
                evidence.page_no if evidence else None,
                evidence.quote if evidence else None,
            ]
        )

    widths = {"A": 12, "B": 12, "C": 24, "D": 18, "E": 30, "H": 22, "I": 15,
              "J": 58, "K": 15, "L": 32, "M": 58, "N": 40, "O": 40, "Q": 60}
    for column, width in widths.items():
        sheet.column_dimensions[column].width = width
    sheet.freeze_panes = "A2"

    # A second sheet naming what produced these numbers. Without it the
    # export is a snapshot with no way to reproduce or date it.
    meta = workbook.create_sheet(label("provenance"))
    meta.append(["Project", project.name if project else str(project_id)])
    meta.append(["Ruleset version", evaluation.ruleset_version])
    meta.append(["Evaluated on", evaluation.today.isoformat()])
    meta.append(["STALE threshold (days)", evaluation.thresholds.stale_days])
    meta.append(["DUE_SOON threshold (days)", evaluation.thresholds.due_soon_days])
    meta.append(
        [
            "ACTION_DUE_SOON threshold (days)",
            evaluation.thresholds.action_due_soon_days,
        ]
    )
    meta.append(["Records", len(rows)])
    meta.append(
        ["Note", "Constraint alerts are calculated from the stated check, not stored."]
    )
    if internal_working_copy:
        # ADR-0040: external release seals a fixed PDF, never a workbook. This
        # copy is an internal working download, so it says so on the sheet that
        # names what produced it rather than trusting a filename to travel.
        meta.append(
            [
                "Working view",
                "Internal working copy — not an approved external release.",
            ]
        )
    meta.column_dimensions["A"].width = 18
    meta.column_dimensions["B"].width = 60

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)
    return path


def native_population_workbook(reading: FrozenProjectReading, path, *, internal_working_copy=False):
    """Export native identities and field-exact source cells without legacy rows."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    population = reading.native_population
    if population is None:
        raise ValueError("native workbook requires an accepted population")
    book = Workbook()
    sheet = book.active
    sheet.title = label("constraint_log")
    sheet.append(COLUMNS)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    sources = book.create_sheet("Accepted value sources")
    sources.append(["Record subject", "Source row", "Field", "Accepted value", "Fact", "Decision", "Revision", "Source Segment", "Supporting document", "Source location", "Cited passage"])
    # Every published field comes from the one Constraint reading, so this
    # workbook cannot name a value the report reads differently.
    readings = {row.reading.id: row.reading for row in reading.rows}
    for record in population.open_records:
        held = readings[record.id]
        field = record.fields.get("committed_date")
        timing_source = field.sources[0] if field and field.sources else None
        source = record.source_passages[0] if record.source_passages else None
        sheet.append([held.ref_code, held.source_ref, held.org_name, held.dep_type,
            held.title, held.station_from, held.station_to,
            record.value("resolution_strategy"), reading.statement_publication.committed_dates[record.id],
            None,
            held.need_date, documentation_review_label(False), "Not specified",
            ", ".join(format_exception_label(item) for item in reading.evaluation.for_dependency(record.id)),
            source.filename if source else None, source.locator if source else None, source.quote if source else None])
        for name, value in sorted(record.fields.items()):
            printed = accepted_field_text(value)
            for passage in value.sources or (None,):
                sources.append([record.subject_key, record.source_row_key, field_label(name), printed, value.fact_id,
                    value.decision_id, value.revision_id, passage.source_segment_id if passage else None,
                    passage.filename if passage else None, passage.locator if passage else None, passage.quote if passage else None])
    statements = book.create_sheet("Accepted statements")
    statements.append(["Statement", "Field", "Accepted value", "Fact", "Record Decision", "Revision", "Decided by", "Source traceability"])
    for statement in visible_native_statements(population, document_only=reading.statement_publication.document_only):
        for name, field in statement.fields.items():
            if not native_field_visible(field, document_only=reading.statement_publication.document_only):
                continue
            statements.append([statement.subject_key, field_label(name), str(field.value), field.fact_id,
                field.decision_id, field.revision_id, field.actor,
                "; ".join(f"{source.filename or source.source_class} · {source.locator} · {source.locator_validation_status}" for source in field.sources)])
    plans = book.create_sheet("Follow-up Plans")
    plans.append(["Plan", "Proposed Delta", "Open question", "Responsible person", "Responsible organization", "Return date", "Recorded by", "Revision", "Support Assessments", "Source Segments"])
    for plan in population.follow_up_plans:
        plans.append([plan.plan_id, plan.delta_id, plan.open_question, plan.responsible_principal,
            plan.responsible_organization, plan.return_date.isoformat() if plan.return_date else None,
            plan.recorded_by, plan.revision_id, ", ".join(map(str, plan.support_assessment_ids)),
            ", ".join(map(str, plan.source_segment_ids))])
    meta = book.create_sheet(label("provenance"))
    for row in (("Project", reading.project.name), ("Project Record revision", population.revision_id),
        ("Native population digest", population.fingerprint), ("Evaluated on", reading.evaluation.today.isoformat()),
        ("Ruleset version", reading.evaluation.ruleset_version), ("Records", len(population.open_records)),
        ("Explicitly excluded source rows", len(population.excluded_source_rows)),
        ("Follow-up Plan scope", population.follow_up_scope)):

        meta.append(row)
    if internal_working_copy:
        meta.append(("Working view", "Internal working copy — not an approved external release."))
    sheet.freeze_panes = "A2"
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    book.save(destination)
    return destination
