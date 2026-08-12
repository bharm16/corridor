"""Report outputs: PDF and XLSX.

The XLSX is the ledger in the shape a project can actually use — and it
carries the citation columns rather than dropping them, because a
spreadsheet that loses the provenance is just the matrix they already had.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from corridor.dependency_events import verbal_attribution
from corridor.exceptions import Evaluation, format_exception_label
from corridor.ledger import browse, primary_evidence
from corridor.models import Project

COLUMNS = [
    "Ref",
    "Source ID",
    "External party",
    "Type",
    "Title",
    "Station from",
    "Station to",
    "Status",
    "Resolution strategy",
    "Committed date",
    "Committed date source",
    "Need date",
    "Ready",
    "Exceptions",
    "Evidence document",
    "Evidence page",
    "Evidence quote",
]


def _committed_date_source(row) -> str | None:
    """Name only the source class of a date the workbook actually publishes."""
    if row.committed_date is None:
        return None
    if row.committed_event is None:
        return "Legacy compatibility projection"
    return verbal_attribution(row.committed_event) or "Cited statement"


def to_pdf(html: str, path: Path | str) -> Path:
    """Render the report HTML to PDF.

    WeasyPrint carries native dependencies; a missing one should fail loudly
    here rather than silently producing no file.
    """
    from weasyprint import HTML

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    HTML(string=html).write_pdf(str(path))
    return path


def to_xlsx(
    session: Session,
    project_id: int,
    path: Path | str,
    *,
    evaluation: Evaluation,
) -> Path:
    """The ledger as a workbook, at the evaluation the report published.

    Required, not defaulted: this workbook is the artefact a project
    forwards to an External Party, and a second reading here would let it
    disagree with the report it was sent alongside.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font

    project = session.get(Project, project_id)
    rows = browse(session, project_id, limit=100_000, evaluation=evaluation)

    by_dependency = {
        dependency_id: [format_exception_label(e) for e in found]
        for dependency_id, found in evaluation.by_dependency().items()
    }
    evidence_by_dependency = primary_evidence(
        session, [row.dependency.id for row in rows]
    )

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Ledger"

    sheet.append(COLUMNS)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(vertical="top")

    for row in rows:
        dependency = row.dependency
        evidence = evidence_by_dependency.get(dependency.id)
        sheet.append(
            [
                dependency.ref_code,
                dependency.source_ref,
                row.org_name,
                dependency.dep_type,
                dependency.title,
                dependency.station_from,
                dependency.station_to,
                dependency.status,
                dependency.resolution_strategy,
                row.committed_date,
                _committed_date_source(row),
                dependency.need_date,
                "yes" if row.is_ready else "no",
                ", ".join(sorted(by_dependency.get(dependency.id, ()))),
                evidence.filename if evidence else None,
                evidence.page_no if evidence else None,
                evidence.quote if evidence else None,
            ]
        )

    widths = {"A": 12, "B": 12, "C": 24, "D": 18, "E": 30, "H": 12, "I": 11,
              "J": 14, "K": 58, "L": 12, "N": 34, "O": 40, "Q": 60}
    for column, width in widths.items():
        sheet.column_dimensions[column].width = width
    sheet.freeze_panes = "A2"

    # A second sheet naming what produced these numbers. Without it the
    # export is a snapshot with no way to reproduce or date it.
    meta = workbook.create_sheet("Provenance")
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
        ["Note", "Exception columns are derived from the passed evaluation, not stored."]
    )
    meta.column_dimensions["A"].width = 18
    meta.column_dimensions["B"].width = 60

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)
    return path
