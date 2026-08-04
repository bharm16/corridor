"""Preparing a hand-labelled gold set, without authoring one (#88).

The M7 gate's denominator is a human count: a person reads the document
and writes down what is in it, and the extractor's output is scored
against that (ADR-0008, and the criteria on #81). The reason is not
ceremony — a gold set derived from the extractor's own reading measures
nothing, because it inherits the extractor's blind spots and comes back
at 100%.

So this module deliberately does **not** produce a gold set. It reads the
document a second way, says where that reading and the extractor's
disagree, and attaches the document's own words to each disagreement. A
reviewer confirms a claim here by looking at a page — not by trusting a
count — and then authors the denominator themselves.

Two restraints make that real, and both are pinned by tests:

- **The worksheet comes out blank.** A sheet pre-filled with the
  extractor's answers turns the labeller into a checker, and a checker
  agrees. That is the anchoring #81 rules out.
- **Nothing here decides.** A row carrying a retirement phrase is
  *flagged*, never dropped — whether `Not Used` retires a row number or
  describes an out-of-service facility is #128, and it is open. On WSDOT
  9424 that phrase appears on 98 rows; a tool that quietly excluded them
  would be settling the question by omission, at a scale no ≥95% bar
  absorbs.

**The independence is partial, and the report says so.** Rows are matched
by quote containment, which borrows no column mapping from the extractor —
validated on 9424, where it reproduces all 162 extracted rows exactly. But
the enumeration still reads the page through PyMuPDF's table detection,
the same library the extractor reads through, so a region that library
drops is invisible to both readings. Only an eye on the rendered page
image closes that, which is why the report ends with a per-page checklist
rather than a conclusion.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pymupdf
from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.docs import stored_file
from corridor.eval import REQUIRED_COLUMNS
from corridor.geometry import page_tables, row_quote
from corridor.models import Candidate, DocPage, Document, Project
from corridor.verify import normalize

# What the reviewer fills in. `critical` is not in the eval's required set
# — a gold set may decline to label criticality — but it is the column the
# M7 gate's ≥95% bar is computed from, so a worksheet without it invites a
# labelling pass that cannot produce the gate's number.
WORKSHEET_COLUMNS = (*REQUIRED_COLUMNS, "page", "critical")

# Phrases a document uses to retire a row. Enumerated from the corpus
# rather than imagined: WSDOT 9424 prints `Not Used` on 97 blank rows and
# `Not used` on one populated one, and nothing else in this corpus retires
# a row at all.
#
# Under-matching is the safe direction and the reason this list stays
# short. A retirement phrase this does not know still surfaces its row —
# as `identifier only` or `populated`, with its cells shown — so the
# reviewer reads the words themselves. Over-matching would hide a row
# behind a classification nobody checked.
RETIREMENT_PHRASES = ("not used",)

# Below this many populated cells, a row is an identifier and little else.
# Matches `extract_matrix.MIN_ROW_FIELDS`'s reasoning without importing
# it: the guard there is about mapped fields, this is about printed cells.
IDENTIFIER_ONLY_CELLS = 2


@dataclass(frozen=True)
class UnreadRow:
    """A grid row the extractor did not produce, with what it prints."""

    page_no: int
    cells: tuple[tuple[int, str], ...]
    headings: tuple[str, ...]
    kind: str
    # Above the first row any candidate matched — usually the header, but
    # never assumed to be. Carried as a note beside the row's content
    # rather than as its category: on 9424 a position-first classification
    # filed the two rows that actually needed eyes (ids 163 and 256)
    # among thirty-three header rows, which is the quiet burial this
    # exercise exists to prevent.
    above_body: bool = False

    def heading_for(self, index: int) -> str:
        """The printed heading above a cell, or the bare index.

        A column number means nothing to a reviewer holding the page; the
        heading is what lets them find the cell.
        """
        if index < len(self.headings) and self.headings[index]:
            return self.headings[index]
        return f"column {index}"


@dataclass(frozen=True)
class PageTally:
    page_no: int
    extracted: int
    unread: int


@dataclass(frozen=True)
class Preparation:
    project: str
    document: str
    extracted_total: int
    tallies: tuple[PageTally, ...]
    unread: tuple[UnreadRow, ...]
    contested: tuple[UnreadRow, ...]
    page_images: tuple[tuple[int, str | None], ...]


def prepare(session: Session, project_id: int) -> Preparation:
    """Read the matrix a second way and diff it against the extractor."""
    project = session.get(Project, project_id)
    if project is None:
        raise LookupError(f"no project {project_id}")

    document = session.scalars(
        select(Document)
        .where(Document.project_id == project_id, Document.doc_type == "matrix")
        .order_by(Document.doc_date, Document.id)
    ).first()
    if document is None:
        raise LookupError(f"no matrix document in {project.slug}")

    path = stored_file(document)
    if path is None:
        raise LookupError(f"no stored file for {document.filename}")

    quotes = _quotes_by_page(session, document)
    images = {
        page.page_no: page.image_path
        for page in session.scalars(
            select(DocPage).where(DocPage.document_id == document.id)
        )
    }

    tallies: list[PageTally] = []
    unread: list[UnreadRow] = []
    contested: list[UnreadRow] = []

    with pymupdf.open(Path(path)) as pdf:
        for page_no, page in enumerate(pdf, start=1):
            tables = page_tables(page)
            if not tables:
                continue
            grid = max(tables, key=len)
            page_quotes = [q for q in quotes.get(page_no, []) if q]

            rows = [(raw, normalize(row_quote(raw))) for raw in grid]
            matched = [
                any(q in text for q in page_quotes) if text else False
                for _, text in rows
            ]
            first = matched.index(True) if True in matched else len(rows)
            headings = _headings(grid, first)

            found = 0
            for index, ((raw, text), is_matched) in enumerate(zip(rows, matched)):
                if not text:
                    continue
                cells = tuple(
                    (i, (cell or "").strip())
                    for i, cell in enumerate(raw)
                    if (cell or "").strip()
                )
                if is_matched:
                    found += 1
                    if _retirement(cells):
                        # The row disagrees with itself: extracted, and
                        # carrying a phrase that may retire it. Only a
                        # human can say which reading is right (#128).
                        contested.append(
                            UnreadRow(page_no, cells, headings, "extracted, and retired?")
                        )
                    continue
                unread.append(
                    UnreadRow(
                        page_no,
                        cells,
                        headings,
                        _classify(cells),
                        above_body=index < first,
                    )
                )

            tallies.append(
                PageTally(
                    page_no=page_no,
                    extracted=found,
                    unread=sum(1 for r in unread if r.page_no == page_no),
                )
            )

    return Preparation(
        project=project.slug,
        document=document.filename,
        extracted_total=sum(len(v) for v in quotes.values()),
        tallies=tuple(tallies),
        unread=tuple(unread),
        contested=tuple(contested),
        page_images=tuple(sorted(images.items())),
    )


def _quotes_by_page(session: Session, document: Document) -> dict[int, list[str]]:
    """What the extractor cited, per page, normalised for containment.

    Containment rather than equality: a citation is the whole row where
    that verifies and the longest verifiable window where it does not
    (`geometry.best_verifiable_quote`), so an equality test would report
    every fallback row as unextracted.
    """
    quotes: dict[int, list[str]] = {}
    for candidate in session.scalars(
        select(Candidate).where(Candidate.source_document_id == document.id)
    ):
        for citation in candidate.payload_json.get("citations", []):
            page_no = citation.get("page")
            if page_no:
                quotes.setdefault(page_no, []).append(normalize(citation.get("quote", "")))
    return quotes


def _headings(grid, first_body: int) -> tuple[str, ...]:
    """Column labels for the report, from the fullest row above the body.

    Not "the row directly above": on 9424 page 9 a retired row sits
    between the header and the first extracted row, and taking its
    neighbour labelled every cell from data row 185. The header is the
    widest thing above the body, which no retired row competes with.

    A best effort for readability only — nothing downstream depends on it
    being the true header, and rows above it are reported rather than
    assumed away.
    """
    above = grid[:first_body] if 0 < first_body <= len(grid) else []
    if not above:
        return ()
    best = max(above, key=lambda row: sum(1 for c in row if (c or "").strip()))
    return tuple((cell or "").strip() for cell in best)


def _retirement(cells: tuple[tuple[int, str], ...]) -> bool:
    return any(
        phrase in value.casefold() for _, value in cells for phrase in RETIREMENT_PHRASES
    )


def _classify(cells) -> str:
    """What the row contains — never where it sits.

    Position is recorded separately (`above_body`) because it is the
    weaker signal: a header sits above the body, but so does any data row
    the extractor missed near the top of a page, and letting position win
    files the second as the first.
    """
    if _retirement(cells):
        return "carries a retirement phrase"
    if len(cells) <= IDENTIFIER_ONLY_CELLS:
        return "identifier only"
    return "populated"


def worksheet() -> str:
    """A blank sheet — columns and nothing else.

    Deliberately empty. The denominator is the reviewer's own reading of
    the document; anything pre-filled here would be the extractor's
    reading wearing the reviewer's name.
    """
    return ",".join(WORKSHEET_COLUMNS) + "\n"


def render(prep: Preparation) -> str:
    lines = [
        f"# Labelling preparation — {prep.project}",
        "",
        f"Document: `{prep.document}`",
        "",
        "**This is not a gold set and must not be used as one.** It is a",
        "second reading of the document, diffed against the extractor's, so",
        "a reviewer knows where to look hardest. The denominator for the",
        "M7 gate is the reviewer's own count (#81); the worksheet beside",
        "this file is blank on purpose.",
        "",
        "## The limit of this reading",
        "",
        "Rows are matched by quote containment, which borrows no column",
        "mapping from the extractor. But this enumeration **shares",
        "PyMuPDF's table detection** with the extractor, so a region that",
        "library drops is missing from both readings and cannot appear",
        "below. Only an eye on the rendered page image closes that hole,",
        "which is what the checklist at the end is for.",
        "",
    ]

    if prep.extracted_total == 0:
        lines += [
            "## No extraction to diff against",
            "",
            "This project has no candidates for its matrix, so nothing here",
            "is a disagreement — there is only one reading. Run the",
            "extraction first.",
            "",
        ]

    lines += ["## Per page", "", "| page | extracted | not extracted |", "|---:|---:|---:|"]
    for tally in prep.tallies:
        lines.append(f"| {tally.page_no} | {tally.extracted} | {tally.unread} |")
    lines += [
        "",
        f"Extracted in total: **{prep.extracted_total}**. "
        f"Rows this reading found and the extractor did not: "
        f"**{len(prep.unread)}**.",
        "",
    ]

    if prep.contested:
        lines += [
            "## Rows that disagree with themselves",
            "",
            "Extracted, and carrying a phrase that may retire them. Whether",
            "such a phrase retires the row or describes an out-of-service",
            "facility is open (#128) — these need a decision before the",
            "labelling, because the answer changes the denominator.",
            "",
        ]
        lines += _rows(prep.contested)

    by_kind: dict[str, list[UnreadRow]] = {}
    for row in prep.unread:
        by_kind.setdefault(row.kind, []).append(row)

    # Loudest first: a populated row nobody extracted is what a real miss
    # looks like, and it should not be below ninety-seven blank ones.
    order = ["populated", "identifier only", "carries a retirement phrase"]
    for kind in order:
        rows = by_kind.get(kind)
        if not rows:
            continue
        lines += ["", f"## Not extracted — {kind} ({len(rows)})", ""]
        if kind == "populated":
            lines.append(
                "**Read these first.** A row carrying real content that no "
                "candidate matches is what a genuine miss looks like. Rows "
                "marked *(above the body)* are usually the printed header, "
                "which is why the marker is shown rather than used to file "
                "them out of sight."
            )
            lines.append("")
        lines += _rows(rows)

    lines += [
        "",
        "## Before labelling: look at every page",
        "",
        "One line per page. This is the part no code can do — a row lost to",
        "table detection is absent from everything above.",
        "",
    ]
    for page_no, image in prep.page_images:
        tally = next((t for t in prep.tallies if t.page_no == page_no), None)
        counted = f"{tally.extracted} extracted" if tally else "no table found"
        lines.append(f"- [ ] page {page_no} — {counted} — `{image or 'no image'}`")

    lines += [
        "",
        "## Then label",
        "",
        "Write the worksheet from the **document**, not from this file:",
        "one row per conflict, `critical` by ADR-0009's rule — yes when the",
        "document says the facility is relocated, removed or abandoned; no",
        "for retain-and-protect or a vertical adjustment; blank when the",
        "document has not settled. Use this file afterwards to check what",
        "you and the extractor disagreed about.",
        "",
    ]
    return "\n".join(lines)


def _rows(rows: list[UnreadRow]) -> list[str]:
    out = []
    for row in rows:
        shown = ", ".join(
            f"{row.heading_for(index)}: {value!r}" for index, value in row.cells
        )
        where = " *(above the body)*" if row.above_body else ""
        out.append(f"- p{row.page_no}{where} — {shown}")
    return out


def main(argv: list[str]) -> int:
    """`make gold ARGS="<slug>"`"""
    import sys

    from corridor.db import Session as SessionFactory

    if not argv:
        print("usage: python -m corridor.gold <slug>", file=sys.stderr)
        return 2

    slug = argv[0]
    with SessionFactory() as session:
        project = session.scalars(select(Project).where(Project.slug == slug)).first()
        if project is None:
            print(f"no project {slug!r}", file=sys.stderr)
            return 1

        prep = prepare(session, project.id)
        out = Path("out/gold")
        out.mkdir(parents=True, exist_ok=True)

        report = out / f"{slug}-disagreements.md"
        report.write_text(render(prep))

        sheet = out / f"{slug}-worksheet.csv"
        if sheet.exists():
            # Never overwrite labelling in progress: the worksheet is
            # hours of human work and this file is regenerable.
            print(f"{sheet} exists — left alone", flush=True)
        else:
            sheet.write_text(worksheet())

        print(f"{prep.extracted_total} extracted, {len(prep.unread)} not")
        if prep.contested:
            print(f"{len(prep.contested)} row(s) disagree with themselves — see #128")
        print(f"report:    {report}")
        print(f"worksheet: {sheet} (blank, on purpose)")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
