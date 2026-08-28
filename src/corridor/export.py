"""Report outputs: PDF and XLSX.

The XLSX is the ledger in the shape a project can actually use — and it
carries the citation columns rather than dropping them, because a
spreadsheet that loses the provenance is just the matrix they already had.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from corridor.dependency_events import StatementPublication
from corridor.exceptions import Evaluation, format_exception_label
from corridor.ledger import primary_evidence
from corridor.models import Project
from corridor.project_reading import freeze_project_reading
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
) -> Path:
    """The ledger as a workbook, at the evaluation the report published.

    Both inputs are required: this workbook is the artefact a project
    forwards to an External Party. The evaluation supplies its Exceptions;
    the paired statement publication supplies date and provenance cells.
    Refusing a mismatched pair prevents either side from taking a second
    reading that disagrees with the report it was sent alongside.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font

    reading = freeze_project_reading(
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
    evidence_by_dependency = primary_evidence(
        session, [row.dependency.id for row in rows]
    )

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = label("constraint_log")

    sheet.append(COLUMNS)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(vertical="top")

    for row in rows:
        dependency = row.dependency
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
    meta.column_dimensions["A"].width = 18
    meta.column_dimensions["B"].width = 60

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)
    return path
