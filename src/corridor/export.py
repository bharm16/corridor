"""Report outputs: PDF and XLSX.

The XLSX is the ledger in the shape a project can actually use — and it
carries the citation columns rather than dropping them, because a
spreadsheet that loses the provenance is just the matrix they already had.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.exceptions import RULESET_VERSION, evaluate
from corridor.ledger import browse
from corridor.models import Document, EvidenceLink, Project

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
    "Need date",
    "Ready",
    "Exceptions",
    "Evidence document",
    "Evidence page",
    "Evidence quote",
]


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


def to_xlsx(session: Session, project_id: int, path: Path | str) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font

    project = session.get(Project, project_id)
    rows = browse(session, project_id, limit=100_000)

    by_dependency: dict[int, list[str]] = {}
    for exception in evaluate(session, project_id):
        by_dependency.setdefault(exception.dependency_id, []).append(exception.rule)

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Ledger"

    sheet.append(COLUMNS)
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(vertical="top")

    for row in rows:
        dependency = row.dependency
        evidence = _primary_evidence(session, dependency.id)
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
                dependency.committed_date,
                dependency.need_date,
                "yes" if row.is_ready else "no",
                ", ".join(sorted(by_dependency.get(dependency.id, ()))),
                evidence[0] if evidence else None,
                evidence[1] if evidence else None,
                evidence[2] if evidence else None,
            ]
        )

    widths = {"A": 12, "B": 12, "C": 24, "D": 18, "E": 30, "H": 12, "I": 11,
              "J": 14, "K": 12, "M": 34, "N": 40, "P": 60}
    for column, width in widths.items():
        sheet.column_dimensions[column].width = width
    sheet.freeze_panes = "A2"

    # A second sheet naming what produced these numbers. Without it the
    # export is a snapshot with no way to reproduce or date it.
    meta = workbook.create_sheet("Provenance")
    meta.append(["Project", project.name if project else str(project_id)])
    meta.append(["Ruleset version", RULESET_VERSION])
    meta.append(["Records", len(rows)])
    meta.append(
        ["Note", "Exception columns are computed at export time, not stored."]
    )
    meta.column_dimensions["A"].width = 18
    meta.column_dimensions["B"].width = 60

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)
    return path


def _primary_evidence(session: Session, dependency_id: int):
    row = session.execute(
        select(EvidenceLink, Document)
        .join(Document, EvidenceLink.document_id == Document.id)
        .where(
            EvidenceLink.dependency_id == dependency_id,
            EvidenceLink.verified.is_(True),
        )
        .order_by(EvidenceLink.id)
        .limit(1)
    ).first()
    if row is None:
        return None
    link, document = row
    return (document.filename, link.page_no, link.quote)
