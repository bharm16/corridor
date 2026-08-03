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

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.changes import Diff, diff_since_last
from corridor.exceptions import RULESET_VERSION, evaluate
from corridor.ledger import LedgerRow, browse
from corridor.models import Dependency, Document, EvidenceLink, Milestone, Project

# Enough to act on in a weekly meeting. More than this and nobody reads it.
CRITICAL_ITEM_COUNT = 15


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
class Section:
    title: str
    note: str = ""
    columns: list[str] = field(default_factory=list)
    rows: list[list[Cell]] = field(default_factory=list)
    empty_message: str = "Nothing to report."


@dataclass
class Report:
    project_name: str
    generated_at: datetime
    ruleset_version: str = RULESET_VERSION
    summary: list[Cell] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    diff: Diff | None = None
    coverage_note: str = ""

    @property
    def cells(self) -> list[Cell]:
        return [
            *self.summary,
            *(c for s in self.sections for row in s.rows for c in row),
        ]


def _derived(label: str, value: str, records) -> Cell:
    return Cell(label, value, Derivation(RULESET_VERSION, tuple(records)))


def build_report(
    session: Session, project_id: int, *, today: date | None = None
) -> Report:
    today = today or datetime.now(timezone.utc).date()
    project = session.get(Project, project_id)
    rows = browse(session, project_id, limit=100_000)
    by_id = {r.dependency.id: r for r in rows}
    ids = tuple(by_id)

    with_evidence = {r.dependency.id for r in rows if r.evidence_count}
    ready = {r.dependency.id for r in rows if r.is_ready}
    pct = (100 * len(with_evidence) / len(ids)) if ids else 0.0

    report = Report(
        project_name=project.name if project else f"project {project_id}",
        generated_at=datetime.now(timezone.utc),
        summary=[
            _derived("Dependencies", str(len(ids)), ids),
            _derived("Ready", str(len(ready)), ready),
            _derived("With verified evidence", str(len(with_evidence)), with_evidence),
            _derived("% with verified evidence", f"{pct:.1f}%", ids),
        ],
        diff=diff_since_last(session, project_id),
        coverage_note=_coverage_note(session, project_id, len(ids)),
    )

    report.sections = [
        _milestone_rollup(session, project_id, rows),
        _critical_items(session, rows, today),
        _exceptions_summary(session, project_id),
        _changes_since_last(report.diff),
        _aging(rows, today),
        _appendix(rows),
    ]
    return report


def _coverage_note(session: Session, project_id: int, in_ledger: int) -> str:
    """State what the report does *not* cover.

    The ledger holds only adjudicated records, which is correct — nothing
    enters without a human keystroke. But a report over 6 of 1,340 extracted
    candidates is a report about almost nothing, and saying so is the
    difference between an honest document and a misleading one.
    """
    from corridor.models import Candidate

    pending = session.scalar(
        select(__import__("sqlalchemy").func.count())
        .select_from(Candidate)
        .where(Candidate.project_id == project_id, Candidate.state == "pending")
    )
    if not pending:
        return ""
    return (
        f"{pending} extracted candidate(s) are still awaiting adjudication and "
        f"are not represented below. This report covers the {in_ledger} record(s) "
        "in the ledger."
    )


def _milestone_rollup(
    session: Session, project_id: int, rows: list[LedgerRow]
) -> Section:
    milestones = session.scalars(
        select(Milestone)
        .where(Milestone.project_id == project_id)
        .order_by(Milestone.need_date)
    ).all()

    section = Section(
        "Milestone readiness",
        columns=["Milestone", "Need date", "Total", "Ready", "At risk", "Blocked", "% evidenced"],
        empty_message="No milestones imported, so nothing is measured against a date.",
    )

    groups: list[tuple[str, str, list[LedgerRow]]] = [
        (m.name, m.need_date.isoformat() if m.need_date else "—",
         [r for r in rows if r.dependency.milestone_id == m.id])
        for m in milestones
    ]
    unlinked = [r for r in rows if r.dependency.milestone_id is None]
    if unlinked:
        groups.append(("Not linked to a milestone", "—", unlinked))

    for name, need_date, group in groups:
        ids = [r.dependency.id for r in group]
        ready = [r.dependency.id for r in group if r.is_ready]
        at_risk = [
            r.dependency.id
            for r in group
            if not r.is_ready and any(
                e.rule in ("OVERDUE", "DUE_SOON", "CONTRADICTION") for e in r.exceptions
            )
        ]
        blocked = [r.dependency.id for r in group if r.dependency.status == "blocked"]
        evidenced = [r.dependency.id for r in group if r.evidence_count]
        pct = (100 * len(evidenced) / len(ids)) if ids else 0.0
        section.rows.append(
            [
                _derived("Milestone", name, ids),
                _derived("Need date", need_date, ids),
                _derived("Total", str(len(ids)), ids),
                _derived("Ready", str(len(ready)), ready),
                _derived("At risk", str(len(at_risk)), at_risk),
                _derived("Blocked", str(len(blocked)), blocked),
                _derived("% evidenced", f"{pct:.0f}%", evidenced),
            ]
        )
    return section


def _critical_items(
    session: Session, rows: list[LedgerRow], today: date
) -> Section:
    """Top items by need-date proximity times criticality."""
    weight = {"critical": 3.0, "high": 2.0, "normal": 1.0}

    def urgency(row: LedgerRow) -> float:
        base = weight.get(row.dependency.criticality, 1.0)
        if row.dependency.need_date:
            days = (row.dependency.need_date - today).days
            proximity = 1.0 if days <= 0 else max(0.1, 1.0 - min(days, 365) / 365)
        else:
            proximity = 0.3
        return base * proximity + row.worst_severity / 10

    ranked = sorted(
        (r for r in rows if not r.is_ready), key=urgency, reverse=True
    )[:CRITICAL_ITEM_COUNT]

    section = Section(
        "Critical items",
        note=f"Top {CRITICAL_ITEM_COUNT} not-ready records by need-date proximity and criticality.",
        columns=["Ref", "External party", "Committed", "Need", "Status", "Worst exception"],
        empty_message="Every record is ready.",
    )
    for row in ranked:
        cited = _primary_evidence(session, row.dependency.id)
        worst = max(row.exceptions, key=lambda e: e.severity, default=None)
        section.rows.append(
            [
                Cell("Ref", row.dependency.ref_code, cited),
                Cell("External party", row.org_name or "—", cited),
                Cell(
                    "Committed",
                    row.dependency.committed_date.isoformat()
                    if row.dependency.committed_date
                    else "—",
                    cited,
                ),
                Cell(
                    "Need",
                    row.dependency.need_date.isoformat()
                    if row.dependency.need_date
                    else "—",
                    cited,
                ),
                Cell("Status", row.dependency.status, cited),
                _derived(
                    "Worst exception",
                    worst.rule if worst else "—",
                    (row.dependency.id,),
                ),
            ]
        )
    return section


def _exceptions_summary(session: Session, project_id: int) -> Section:
    found = evaluate(session, project_id)
    by_rule: dict[str, list] = {}
    for exception in found:
        by_rule.setdefault(exception.rule, []).append(exception)

    section = Section(
        "Exceptions",
        columns=["Rule", "Count", "Worst offender", "Why"],
        empty_message="No exceptions.",
    )
    for rule, items in sorted(by_rule.items(), key=lambda kv: -len(kv[1])):
        ids = [e.dependency_id for e in items]
        worst = max(items, key=lambda e: e.severity)
        section.rows.append(
            [
                _derived("Rule", rule, ids),
                _derived("Count", str(len(items)), ids),
                _derived("Worst offender", worst.ref_code, (worst.dependency_id,)),
                _derived("Why", worst.detail, (worst.dependency_id,)),
            ]
        )
    return section


def _changes_since_last(diff: Diff | None) -> Section:
    section = Section(
        "Changes since last report",
        columns=["Ref", "Change", "Detail"],
        empty_message="Nothing changed since the previous report.",
    )
    if diff is None:
        return section

    if diff.is_first_report:
        section.note = "First report for this project — there is nothing to compare against."
        section.empty_message = section.note
        return section

    section.note = f"Compared against the report of {diff.previous_ts:%Y-%m-%d %H:%M} UTC."
    if diff.ruleset_changed:
        section.note += (
            f" The ruleset changed ({diff.previous_ruleset} → {RULESET_VERSION}), "
            "so exception churn is suppressed — a rule appearing may mean the rule "
            "changed rather than the project moving."
        )

    for change in diff.changes:
        section.rows.append(
            [
                _derived("Ref", change.ref_code, ()),
                _derived("Change", change.kind, ()),
                _derived("Detail", change.detail, ()),
            ]
        )
    return section


def _aging(rows: list[LedgerRow], today: date) -> Section:
    overdue = [
        r
        for r in rows
        if r.dependency.committed_date
        and r.dependency.committed_date < today
        and any(e.rule == "OVERDUE" for e in r.exceptions)
    ]
    overdue.sort(key=lambda r: r.dependency.committed_date)

    section = Section(
        "Aging",
        columns=["Ref", "External party", "Committed", "Days overdue"],
        empty_message="Nothing is overdue.",
    )
    for row in overdue:
        days = (today - row.dependency.committed_date).days
        section.rows.append(
            [
                _derived("Ref", row.dependency.ref_code, (row.dependency.id,)),
                _derived("External party", row.org_name or "—", (row.dependency.id,)),
                _derived(
                    "Committed",
                    row.dependency.committed_date.isoformat(),
                    (row.dependency.id,),
                ),
                _derived("Days overdue", str(days), (row.dependency.id,)),
            ]
        )
    return section


def _appendix(rows: list[LedgerRow]) -> Section:
    section = Section(
        "Appendix — full ledger",
        columns=["Ref", "Source ID", "External party", "Station", "Status", "Ready", "Exceptions"],
        empty_message="The ledger is empty.",
    )
    for row in rows:
        ids = (row.dependency.id,)
        station = row.dependency.station_from or "—"
        if row.dependency.station_to:
            station += f" → {row.dependency.station_to}"
        section.rows.append(
            [
                _derived("Ref", row.dependency.ref_code, ids),
                _derived("Source ID", row.dependency.source_ref or "—", ids),
                _derived("External party", row.org_name or "—", ids),
                _derived("Station", station, ids),
                _derived("Status", row.dependency.status, ids),
                _derived("Ready", "yes" if row.is_ready else "no", ids),
                _derived(
                    "Exceptions",
                    ", ".join(sorted(e.rule for e in row.exceptions)) or "—",
                    ids,
                ),
            ]
        )
    return section


def _primary_evidence(session: Session, dependency_id: int) -> Assertion | None:
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
    return Assertion(document.id, document.filename, link.page_no, link.quote)


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
        kind = "assertion" if isinstance(p, Assertion) else "derivation"
        title = (
            html.escape(p.quote)
            if isinstance(p, Assertion)
            else "records: " + (", ".join(str(i) for i in p.record_ids[:20]) or "computed")
        )
        return (
            f'<td class="{kind}">{html.escape(cell.value)}'
            f'<span class="marker" title="{title}">{html.escape(p.marker)}</span></td>'
        )

    def section_html(section: Section) -> str:
        head = "".join(f"<th>{html.escape(c)}</th>" for c in section.columns)
        note = f'<p class="note">{html.escape(section.note)}</p>' if section.note else ""
        if not section.rows:
            return (
                f"<h2>{html.escape(section.title)}</h2>{note}"
                f'<p class="empty">{html.escape(section.empty_message)}</p>'
            )
        body = "".join(
            "<tr>" + "".join(cell_html(c) for c in row) + "</tr>" for row in section.rows
        )
        return (
            f"<h2>{html.escape(section.title)}</h2>{note}"
            f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
        )

    summary = "".join(
        f'<div class="stat"><span class="n">{html.escape(c.value)}</span>'
        f'<span class="k">{html.escape(c.label)}</span>'
        f'<span class="marker" title="records: '
        f'{", ".join(str(i) for i in c.provenance.record_ids[:20])}">'
        f"{html.escape(c.provenance.marker)}</span></div>"
        for c in report.summary
    )
    coverage = (
        f'<p class="coverage">{html.escape(report.coverage_note)}</p>'
        if report.coverage_note
        else ""
    )

    return f"""<!doctype html>
<meta charset="utf-8">
<title>Readiness — {html.escape(report.project_name)}</title>
<style>
 :root {{ --line:#e2e2e2; --muted:#666; --warn:#b54708; }}
 body {{ font: 13px/1.5 system-ui, sans-serif; margin: 2rem auto; max-width: 60rem;
        color: #111; background:#fff; padding: 0 1.5rem; }}
 h1 {{ font-size: 1.5rem; margin-bottom: .2rem; }}
 h2 {{ font-size: .85rem; text-transform: uppercase; letter-spacing: .04em;
      color: var(--muted); margin: 2.2rem 0 .5rem; border-bottom: 1px solid var(--line);
      padding-bottom: .3rem; }}
 .stat {{ display: inline-block; margin: 0 2rem 1rem 0; }}
 .stat .n {{ display: block; font-size: 1.7rem; font-weight: 600; }}
 .stat .k {{ color: var(--muted); }}
 .coverage {{ background:#fffaeb; border:1px solid #fedf89; color: var(--warn);
             padding:.5rem .7rem; border-radius:4px; font-size:.85rem; }}
 .note {{ color: var(--muted); font-size: .82rem; margin: .2rem 0 .6rem; }}
 .empty {{ color: var(--muted); font-style: italic; }}
 table {{ border-collapse: collapse; width: 100%; margin-bottom: .5rem; }}
 th {{ text-align: left; font-size: .78rem; color: var(--muted); font-weight: 600;
      border-bottom: 1px solid var(--line); padding: .3rem .5rem; }}
 td {{ border-bottom: 1px solid #f2f2f2; padding: .3rem .5rem; vertical-align: top;
      font-variant-numeric: tabular-nums; }}
 .marker {{ color: #06c; font-size: .72em; margin-left: .35rem; cursor: help;
           white-space: nowrap; }}
 .derivation .marker {{ color: #690; }}
 footer {{ margin-top: 3rem; color: var(--muted); font-size: .8rem;
          border-top: 1px solid var(--line); padding-top: .8rem; }}
 @page {{ size: A4; margin: 1.5cm; }}
</style>
<h1>Readiness — {html.escape(report.project_name)}</h1>
<p class="note">Generated {report.generated_at:%Y-%m-%d %H:%M} UTC · ruleset {report.ruleset_version}</p>
{coverage}
{summary}
{"".join(section_html(s) for s in report.sections)}
<footer>
Every figure is an Assertion (a quote on a cited page) or a Derivation
(a computation over cited records). Hover any marker for its source.
No figure in this report is uncited.
</footer>
"""


def today() -> date:
    return datetime.now(timezone.utc).date()


def main(argv: list[str]) -> int:
    """`make report ARGS="<slug>"` — build, export, and record a run."""
    import sys
    from pathlib import Path

    from corridor.changes import record_run
    from corridor.db import Session as SessionFactory
    from corridor.export import to_pdf, to_xlsx

    slug = argv[0] if argv else "nhhip-3c2"
    out = Path("out/report.html")
    with SessionFactory() as session:
        project = session.scalars(
            select(Project).where(Project.slug == slug)
        ).first()
        if project is None:
            print(f"no project {slug!r}", file=sys.stderr)
            return 1

        report = build_report(session, project.id)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render(report))
        to_xlsx(session, project.id, Path("out/ledger.xlsx"))
        try:
            to_pdf(out.read_text(), Path("out/report.pdf"))
            pdf = " · out/report.pdf"
        except Exception as exc:
            pdf = f" · PDF skipped ({type(exc).__name__})"

        record_run(session, project.id, output_path=str(out))
        session.commit()

    print(f"{out}{pdf} · out/ledger.xlsx")
    print(f"{len(report.cells)} cells, every one cited · ruleset {RULESET_VERSION}")
    if report.coverage_note:
        print(f"note: {report.coverage_note}")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
