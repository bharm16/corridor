"""The weekly readiness report, and the rule that no cell is bare.

Every published figure is one of two things (ADR-0003):

- an **Assertion** — traced to a quote on a page, shown as `[D12 p.4]`
- a **Derivation** — traced to a computation over cited records, carrying
  the ruleset version and the record IDs it covered

Aggregates cannot cite a page, so "zero uncited assertions" was
unenforceable for exactly the numbers a reader looks at first. Splitting
provenance into two kinds generalizes the rule instead of exempting
anything: rendering refuses to emit a value that carries neither.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor.ledger import is_ready
from corridor.models import Dependency, Document, EvidenceLink, ExternalOrg, Project

RULESET_VERSION = "v0.1"


class BareCell(Exception):
    """A published value carrying no provenance of either kind."""


@dataclass(frozen=True)
class Assertion:
    document_id: int
    filename: str
    page_no: int
    quote: str

    @property
    def marker(self) -> str:
        return f"[D{self.document_id} p.{self.page_no}]"


@dataclass(frozen=True)
class Derivation:
    ruleset_version: str
    record_ids: tuple[int, ...]

    @property
    def marker(self) -> str:
        return f"[{self.ruleset_version} over {len(self.record_ids)} records]"


@dataclass
class Cell:
    label: str
    value: str
    provenance: Assertion | Derivation | None = None


@dataclass
class Report:
    project_name: str
    generated_at: datetime
    summary: list[Cell] = field(default_factory=list)
    rows: list[list[Cell]] = field(default_factory=list)

    @property
    def cells(self) -> list[Cell]:
        return [*self.summary, *(c for row in self.rows for c in row)]


def build_report(session: Session, project_id: int) -> Report:
    project = session.get(Project, project_id)
    dependencies = session.scalars(
        select(Dependency)
        .where(Dependency.project_id == project_id)
        .order_by(Dependency.ref_code)
    ).all()
    ids = tuple(d.id for d in dependencies)

    with_evidence = set(
        session.scalars(
            select(EvidenceLink.dependency_id)
            .where(
                EvidenceLink.dependency_id.in_(ids or (0,)),
                EvidenceLink.verified.is_(True),
            )
            .group_by(EvidenceLink.dependency_id)
            .having(func.count() > 0)
        ).all()
    )
    ready = {d.id for d in dependencies if is_ready(session, d.id)}

    def derived(label: str, value: str, records) -> Cell:
        return Cell(label, value, Derivation(RULESET_VERSION, tuple(records)))

    pct = (100 * len(with_evidence) / len(ids)) if ids else 0.0
    report = Report(
        project_name=project.name if project else f"project {project_id}",
        generated_at=datetime.now(timezone.utc),
        summary=[
            derived("Dependencies", str(len(ids)), ids),
            derived("With verified evidence", str(len(with_evidence)), with_evidence),
            derived("Ready", str(len(ready)), ready),
            derived("% with verified evidence", f"{pct:.1f}%", ids),
        ],
    )

    for dependency in dependencies:
        link, document = _primary_evidence(session, dependency.id)
        org = (
            session.get(ExternalOrg, dependency.external_org_id)
            if dependency.external_org_id
            else None
        )
        cited = (
            Assertion(document.id, document.filename, link.page_no, link.quote)
            if link and document
            else None
        )
        report.rows.append(
            [
                Cell("Ref", dependency.ref_code, cited),
                # The identifier a utility coordinator recognizes. Not
                # unique in the source, which is why it is not our Ref.
                Cell("Source ID", dependency.source_ref or "—", cited),
                Cell("External party", org.name if org else "—", cited),
                Cell(
                    "Station",
                    f"{dependency.station_from or '—'} → {dependency.station_to or '—'}",
                    cited,
                ),
                Cell("Status", dependency.status, cited),
                derived(
                    "Ready", "yes" if dependency.id in ready else "no", (dependency.id,)
                ),
            ]
        )
    return report


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
    return row if row else (None, None)


def assert_no_bare_cells(report: Report) -> None:
    bare = [c for c in report.cells if c.provenance is None]
    if bare:
        raise BareCell(
            f"{len(bare)} cell(s) published without provenance: "
            + ", ".join(f"{c.label}={c.value!r}" for c in bare[:5])
        )


def render(report: Report) -> str:
    """Rendering enforces the rule; it is not a convention to remember."""
    assert_no_bare_cells(report)

    def cell_html(cell: Cell) -> str:
        p = cell.provenance
        title = (
            html.escape(p.quote)
            if isinstance(p, Assertion)
            else f"records: {', '.join(str(i) for i in p.record_ids[:20])}"
        )
        kind = "assertion" if isinstance(p, Assertion) else "derivation"
        return (
            f'<td class="{kind}">{html.escape(cell.value)}'
            f'<span class="marker" title="{title}">{html.escape(p.marker)}</span></td>'
        )

    head = "".join(f"<th>{html.escape(c.label)}</th>" for c in report.rows[0]) if report.rows else ""
    body = "".join(
        "<tr>" + "".join(cell_html(c) for c in row) + "</tr>" for row in report.rows
    )
    summary = "".join(
        f'<div class="stat"><span class="n">{html.escape(c.value)}</span>'
        f'<span class="k">{html.escape(c.label)}</span>'
        f'<span class="marker" title="records: '
        f'{", ".join(str(i) for i in c.provenance.record_ids[:20])}">'
        f"{html.escape(c.provenance.marker)}</span></div>"
        for c in report.summary
    )

    return f"""<!doctype html>
<meta charset="utf-8">
<title>Readiness — {html.escape(report.project_name)}</title>
<style>
 body {{ font: 14px/1.5 system-ui, sans-serif; margin: 2rem; color: #111; }}
 .stat {{ display: inline-block; margin-right: 2rem; }}
 .stat .n {{ display: block; font-size: 1.8rem; font-weight: 600; }}
 .stat .k {{ color: #555; }}
 table {{ border-collapse: collapse; margin-top: 1.5rem; width: 100%; }}
 th, td {{ border-bottom: 1px solid #ddd; padding: .4rem .6rem; text-align: left; }}
 .marker {{ color: #06c; font-size: .75em; margin-left: .4rem; cursor: help; }}
 .derivation .marker {{ color: #690; }}
</style>
<h1>Readiness — {html.escape(report.project_name)}</h1>
<p>Generated {report.generated_at:%Y-%m-%d %H:%M} UTC · ruleset {RULESET_VERSION}</p>
{summary}
<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>
<p style="color:#555;margin-top:2rem">
Every figure is an Assertion (quote on a cited page) or a Derivation
(computation over cited records). Hover a marker for its source.</p>
"""


def today() -> date:
    return datetime.now(timezone.utc).date()
