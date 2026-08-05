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
  describes an out-of-service facility was #128, decided by ADR-0012: a
  blank row carrying the phrase is retired numbering, a populated one is
  a conflict whose facility is out of service. This report still shows
  the retired rows rather than hiding them — the labeller must count the
  same set the rule names, because a gold set that counted retired
  numbering as conflicts reads recall as 162/263, failing the gate on
  bookkeeping rather than on extraction.

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

from corridor.eval import REQUIRED_COLUMNS
from corridor.geometry import page_tables, row_quote
from corridor.models import Candidate, DocPage, Document, Project
from corridor.storage import stored_file
from corridor.verify import normalize

# What the reviewer fills in. `critical` is not in the eval's required set
# — a gold set may decline to label criticality — but it is the column the
# M7 gate's ≥95% bar is computed from, so a worksheet without it invites a
# labelling pass that cannot produce the gate's number.
WORKSHEET_COLUMNS = (*REQUIRED_COLUMNS, "page", "critical")

# One phrase list, imported from its single home so this report and the
# Ledger cannot drift apart the day it grows (ADR-0012); a test pins the
# sharing by identity. Spelling carries no signal — both spellings appear
# on retired rows — but content discriminates perfectly: 101 of the 102
# occurrences sit on Retired Rows, one on the fully populated row 210.
#
# The *matcher* here is deliberately looser than the extractor's: this
# report flags any cell containing a phrase, where `is_retired_row`
# retires only on whole-cell equality. Over-reporting is the safe
# direction for a document a human is about to read — a note like
# `Not used for potable supply` should reach the reviewer's eyes without
# retiring anything, and an unknown phrase still surfaces its row with
# its cells shown.
from corridor.vocabulary import RETIREMENT_PHRASES, is_retired_row  # noqa: E402

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
        "Write the worksheet from the **document**, not from this file.",
        "Count a row when it names a facility, not when it merely bears a",
        "number: a blank row with a retirement phrase is retired numbering",
        "and is not counted; a populated row is counted whatever its notes",
        "say (ADR-0012). One row per conflict, `critical` by ADR-0009's",
        "rule — yes when the document says the facility is relocated,",
        "removed or abandoned; no for retain-and-protect or a vertical",
        "adjustment; blank when the document has not settled. Use this",
        "file afterwards to check what you and the extractor disagreed",
        "about.",
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



# The file-safety rules of a gate run, out of `main` so they can be
# tested without driving the whole command. Both protect an artifact a
# human made, and neither had a test.
GOLD_DIR = Path("gold")
WORKSHEET_DIR = Path("out/gold")


def machine_gold_paths(slug: str, *, directory: Path = GOLD_DIR) -> tuple[Path, Path]:
    """Where a machine-authored gold set and its sidecar are written.

    `<slug>.machine.csv`, never `<slug>.csv`. The hand-authored name is
    the stricter artifact and keeps it (#81 as amended): a machine gold
    set is a ceiling, and letting it claim the name a person's labelling
    would use is how a ceiling gets read as a floor.
    """
    return directory / f"{slug}.machine.csv", directory / f"{slug}.machine.md"


def write_worksheet(path: Path) -> bool:
    """Write a blank worksheet unless one is already there.

    Returns False when it left an existing file alone. The worksheet is
    hours of human labelling and this file is regenerable, so the
    regenerable one yields.
    """
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(worksheet())
    return True


def main(argv: list[str]) -> int:
    """`make gold ARGS="<slug>"`"""
    import sys

    from corridor.db import Session as SessionFactory

    if not argv:
        print("usage: python -m corridor.gold <slug> [--author]", file=sys.stderr)
        return 2

    slug = argv[0]
    author = "--author" in argv[1:]
    with SessionFactory() as session:
        project = session.scalars(select(Project).where(Project.slug == slug)).first()
        if project is None:
            print(f"no project {slug!r}", file=sys.stderr)
            return 1

        if author:
            # The amended path (#81): a machine-authored gold set, stamped
            # as the ceiling it is. Never overwrites a hand-authored
            # gold/<slug>.csv — the stricter artifact keeps its name.
            gold = author_machine_gold(session, project.id)
            csv_path, sidecar = machine_gold_paths(slug)
            csv_path.parent.mkdir(parents=True, exist_ok=True)
            csv_path.write_text(gold_csv(gold))
            sidecar.write_text(render_machine_gold(gold))
            labelled = sum(1 for r in gold.rows if r.critical)
            yes = sum(1 for r in gold.rows if r.critical == "yes")
            print(
                f"{len(gold.rows)} gold rows ({yes} yes / {labelled - yes} no / "
                f"{len(gold.rows) - labelled} blank); excluded {gold.retired} "
                f"retired, {gold.empty_slots} empty slots"
            )
            print(f"gold:    {csv_path}")
            print(f"sidecar: {sidecar} (the ceiling caveat travels with it)")
            return 0

        prep = prepare(session, project.id)
        out = Path("out/gold")
        out.mkdir(parents=True, exist_ok=True)

        report = out / f"{slug}-disagreements.md"
        report.write_text(render(prep))

        sheet = out / f"{slug}-worksheet.csv"
        if not write_worksheet(sheet):
            print(f"{sheet} exists — left alone", flush=True)

        print(f"{prep.extracted_total} extracted, {len(prep.unread)} not")
        if prep.contested:
            print(f"{len(prep.contested)} row(s) disagree with themselves — see #128")
        print(f"report:    {report}")
        print(f"worksheet: {sheet} (blank, on purpose)")
    return 0




# ---------------- machine-authored gold: the ceiling (#81 as amended)
#
# The original criterion — a wholly hand-authored denominator — was amended
# by the maintainer on 2026-08-04, before the 9540 seal was lifted: "I'm
# not hand labeling anything. find another way." This is the other way,
# and everything it produces says what it is: a **semi-independent
# ceiling**, not the full measurement the unamended criterion bought. The
# enumeration and the extractor share PyMuPDF's table detection, so a
# region that library drops is invisible to both; the page-image checklist
# in the sidecar is what narrows that, whenever a human chooses to look.
#
# What keeps the ceiling honest is what it does NOT share: quote-
# containment matching borrows no column mapping from the model, and the
# critical marks are read from the grid at header-anchored positions —
# through the one vocabulary (`WSDOT_APPENDIX_U`) and the one line
# (`is_critical`) that already encode ADR-0009, so the labelling rule and
# the Ledger's derivation stay one sentence even here.

# The band this authoring refuses to run without: the spanning group cell
# both WSDOT contracts print above their marked resolution columns.
#
# It anchored on `509 relocation needed` until the M7 cold run, and that
# refused on the holdout — correctly, and for the wrong reason. 9540 is
# the same Appendix U, but names its columns `RELOCATION` / `PROTECTION IN
# PLACE` / `ABANDON/ DEACTIVATE/ REMOVE` where 9424 names them after its
# own route number. Anchoring on the family's group header instead of one
# contract's column is both more general and more honest about what this
# authoring actually requires: a marked resolution group, whatever the
# marks are called.
_ANCHOR = "recommended resolution"

# The identity columns, both contracts' spellings. The M7 cold run turned
# this into a rule rather than a list: **only the structure of this form is
# stable across contracts — every name varies.** 9424 prints `Owner`,
# `ID Conflict`, and names its marks after its route number; 9540 prints
# `UTILITY OWNER`, `UTILITY ID`, and names its marks after the work. So
# the anchor is the group band, the marks are read through a vocabulary,
# and these are enumerated per contract rather than guessed at.
# How deep a header can sit on a page. The band, a title row, and the
# headings — nothing in this corpus goes deeper, and a "header" found
# further down is a data row that happens to read like one.
_HEADER_SEARCH_DEPTH = 6

_OWNER_HEADINGS = ("owner", "utility owner")
_ID_HEADINGS = ("conflict id", "id conflict", "utility id")
_NOTES_HEADINGS = ("notes",)


class LayoutAnchorMissing(LookupError):
    """No page carries the anchored header this authoring is written for."""


@dataclass(frozen=True)
class MachineGoldRow:
    source_ref: str
    page: int
    critical: str  # "yes" | "no" | "" — blank stays out of the denominator


@dataclass(frozen=True)
class MachineGold:
    project: str
    document: str
    rows: tuple[MachineGoldRow, ...]
    # What was read and excluded, for the sidecar's accounting.
    retired: int
    empty_slots: int
    page_images: tuple[tuple[int, str | None], ...]


def author_machine_gold(session: Session, project_id: int) -> MachineGold:
    """A gold set from the independent grid reading (#81 as amended).

    No model and no extractor column mapping: the resolution group is
    found by the band both WSDOT contracts print above it, the marks are
    cells at the columns beneath, and the canonical reading of each marked
    heading goes through the same vocabulary the Ledger uses — one
    sentence, both sides.
    """
    from corridor.adjudicate import WSDOT_APPENDIX_U
    from corridor.models import is_critical

    project = session.get(Project, project_id)
    if project is None:
        raise LookupError(f"no project {project_id}")
    documents = session.scalars(
        select(Document)
        .where(Document.project_id == project_id, Document.doc_type == "matrix")
        .order_by(Document.doc_date, Document.id)
    ).all()
    if not documents:
        raise LookupError(f"no matrix document in {project.slug}")

    rows: list[MachineGoldRow] = []
    retired = empty_slots = 0
    anchored = False
    images: dict[tuple[str, int], str | None] = {}

    # Every matrix in the project, not the first. 9424 published one
    # document and 9540 publishes six — one per utility kind — so a
    # `.first()` here authored a denominator covering a fourteenth of the
    # project and would have scored the extractor against it. The M7 cold
    # run found that; no fixture could, because every fixture had one.
    for document in documents:
        path = stored_file(document)
        if path is None:
            raise LookupError(f"no stored file for {document.filename}")
        for page in session.scalars(
            select(DocPage).where(DocPage.document_id == document.id)
        ):
            images[(document.filename, page.page_no)] = page.image_path

        with pymupdf.open(Path(path)) as pdf:
            grids = {}
            for page_no, page in enumerate(pdf, start=1):
                tables = page_tables(page)
                if tables:
                    grids[page_no] = max(tables, key=len)

            # The band anchors the **document**, not each page: 9540's
            # Power listing prints it on page 1 and its continuation page
            # reprints only the column headings. Requiring it per page
            # dropped that page whole — ten conflicts absent from the
            # denominator, which is a gold set that does not cover its own
            # document. Requiring it nowhere would accept any form that
            # happened to use these words, so it is required once.
            if not any(
                _norm(c) == _ANCHOR for grid in grids.values() for row in grid for c in row
            ):
                continue

            for page_no, grid in sorted(grids.items()):
                # Each page finds its own header by the resolution
                # headings it prints; a continuation page prints them too.
                header_index = next(
                    (
                        i
                        for i in range(min(len(grid), _HEADER_SEARCH_DEPTH))
                        if any(WSDOT_APPENDIX_U.read(c or "") for c in grid[i])
                    ),
                    None,
                )
                if header_index is None:
                    continue
                anchored = True
                headings = [_norm(c) for c in grid[header_index]]

                owner_col = _column(headings, _OWNER_HEADINGS)
                id_col = _column(headings, _ID_HEADINGS)
                notes_col = _column(headings, _NOTES_HEADINGS)
                # Every column whose heading the vocabulary can read is a
                # resolution mark column; the heading's canonical strategy
                # is what a mark under it asserts.
                mark_cols = {
                    index: WSDOT_APPENDIX_U.read(grid[header_index][index] or "")
                    for index in range(len(headings))
                    if WSDOT_APPENDIX_U.read(grid[header_index][index] or "")
                }

                for raw in grid[header_index + 1 :]:
                    owner = (
                        (raw[owner_col] or "").strip()
                        if owner_col is not None and owner_col < len(raw)
                        else ""
                    )
                    ref = (
                        (raw[id_col] or "").strip()
                        if id_col is not None and id_col < len(raw)
                        else ""
                    )
                    notes = (
                        (raw[notes_col] or "").strip()
                        if notes_col is not None and notes_col < len(raw)
                        else ""
                    )

                    if not owner and not ref:
                        continue  # furniture, or a wholly empty line
                    if not owner:
                        # An id and no facility: retired numbering when the
                        # phrase says so, an empty slot when nothing does.
                        # Neither names a facility; neither is counted
                        # (ADR-0012).
                        if is_retired_row({"utility_id": ref, "notes": notes}):
                            retired += 1
                        else:
                            empty_slots += 1
                        continue

                    strategies = {
                        strategy
                        for index, strategy in mark_cols.items()
                        if index < len(raw) and (raw[index] or "").strip()
                    }
                    sides = {is_critical(s) for s in strategies}
                    critical = (
                        ("yes" if sides == {True} else "no")
                        if len(sides) == 1
                        else ""  # unsettled or unmarked: out of the denominator
                    )
                    rows.append(
                        MachineGoldRow(
                            source_ref=ref, page=page_no, critical=critical
                        )
                    )

    if not anchored:
        raise LayoutAnchorMissing(
            f"no page of {documents[0].filename} prints the anchored band "
            f"({_ANCHOR!r}) above headings this vocabulary can read. This "
            "authoring is written for the WSDOT Appendix U form; a "
            "different layout is a human decision, not a fallback."
        )

    return MachineGold(
        project=project.slug,
        document=", ".join(d.filename for d in documents),
        rows=tuple(rows),
        retired=retired,
        empty_slots=empty_slots,
        page_images=tuple(
            (page_no, path) for (_, page_no), path in sorted(images.items())
        ),
    )


def _norm(cell) -> str:
    return " ".join(str(cell or "").split()).casefold()


def _column(headings: list[str], wanted: tuple[str, ...]) -> int | None:
    for index, heading in enumerate(headings):
        if heading in wanted:
            return index
    return None


def gold_csv(gold: MachineGold) -> str:
    lines = ["source_ref,page,critical"]
    lines += [f"{r.source_ref},{r.page},{r.critical}" for r in gold.rows]
    return "\n".join(lines) + "\n"


def render_machine_gold(gold: MachineGold) -> str:
    labelled = sum(1 for r in gold.rows if r.critical)
    yes = sum(1 for r in gold.rows if r.critical == "yes")
    lines = [
        f"# Machine-authored gold set — {gold.project}",
        "",
        f"Document: `{gold.document}`",
        "",
        "**This number is a ceiling, not a full measurement** (#81 as",
        "amended, 2026-08-04). The enumeration behind it shares PyMuPDF's",
        "table detection with the extractor: a region that library drops is",
        "invisible to both readings and cannot be missed here. What it does",
        "not share is the part that usually errs — no model, no extractor",
        "column mapping; the header is anchored by its own printed text and",
        "the critical marks are grid cells read through the same vocabulary",
        "the Ledger uses (ADR-0009, ADR-0012).",
        "",
        f"- gold rows: {len(gold.rows)}",
        f"- labelled for criticality: {labelled} ({yes} yes / {labelled - yes} no); "
        f"{len(gold.rows) - labelled} blank — unsettled or unmarked, out of the ≥95% denominator",
        f"- excluded: {gold.retired} retired rows, {gold.empty_slots} empty slots (ADR-0012)",
        "",
        "## Strengthening the ceiling toward a measurement",
        "",
        "One look per page image closes the shared blind spot. Optional,",
        "any time after the run:",
        "",
    ]
    for page_no, image in gold.page_images:
        lines.append(f"- [ ] page {page_no} — `{image or 'no image'}`")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
