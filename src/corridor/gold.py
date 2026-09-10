"""Prepare diagnostics or author an immutable machine reference.

The diagnostic path still emits a blank worksheet for projects that obtain
independent review elsewhere. The automated path reads the printed grid with
no model and no extractor column mapping, stamps the shared native-reader blind spot,
and writes a semi-independent machine-reference ceiling plus its exact scope
manifest. It is not human gold and cannot prove semantic completeness.

Machine-reference bytes are first-write-only. Once authored, the CSV,
sidecar, and scope manifest are evidence and a later code or library version
must not silently replace them.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.eval import (
    MACHINE_REFERENCE_LIMITATIONS,
    MACHINE_REFERENCE_METHOD,
    MACHINE_REFERENCE_METHOD_VERSION,
    MACHINE_REFERENCE_SCOPE_SCHEMA,
    REQUIRED_COLUMNS,
    SPENT_MEASUREMENT_ARTIFACTS,
)
from corridor.reference_methods import (
    LEGACY_METHOD, NATIVE_METHOD, SPENT_SOURCE_HASHES, is_digest, reference_method,
)
from corridor.token_layers import read_native_pdf
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
# The guard on the reader's side is about mapped fields; this is about
# printed cells.
IDENTIFIER_ONLY_CELLS = 2

_WS = re.compile(r"\s+")


def row_quote(row: list[str | None]) -> str:
    """The whole grid row as one line: non-empty cells joined by spaces.

    This report matches what an extractor cited against what the grid
    prints, and a citation of a whole row is the cells in reading order
    with single spaces between them. Only this report needs the form now,
    so it lives here.
    """
    cells = [_WS.sub(" ", (c or "").replace("\n", " ")).strip() for c in row]
    return " ".join(c for c in cells if c)


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

    reading = read_native_pdf(path, source_sha256=document.sha256)
    for page in reading.pages:
        page_no = page["number"]
        tables = []
        for table in page["tables"]["value"]:
            cells = table["structured_cells"]
            if not cells:
                continue
            grid = [[""] * (1 + max(cell["column"] for cell in cells))
                    for _ in range(1 + max(cell["row"] for cell in cells))]
            for cell in cells:
                grid[cell["row"]][cell["column"]] = cell["text"]
            tables.append(grid)
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

    Containment rather than equality. A citation is normally the whole row,
    but retained candidates from the page reader that #766 retired cite the
    longest window that verified where the assembled row was not contiguous
    on the page, so an equality test would report every one of those rows as
    unextracted.
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
        "the paired-rendition reader's table reconstruction** with the extractor, so a region that",
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



# File-safety rules live outside `main` so first-write-only reference evidence
# and any independently authored worksheet can be tested without the CLI.
GOLD_DIR = Path("gold")
WORKSHEET_DIR = Path("out/gold")


def machine_gold_paths(
    slug: str, *, directory: Path = GOLD_DIR,
    method: str = MACHINE_REFERENCE_METHOD, method_version: str = MACHINE_REFERENCE_METHOD_VERSION,
) -> tuple[Path, Path]:
    """Where a machine reference and its limitations sidecar are written.

    `<slug>.machine.csv`, never `<slug>.csv`. The hand-authored name is
    the stricter artifact and keeps it (#81 as amended): a machine gold
    set is a ceiling, and letting it claim the name a person's labelling
    would use is how a ceiling gets read as a floor.
    """
    contract = reference_method(method, method_version)
    stem = slug if contract == LEGACY_METHOD else f"{slug}.{contract.name}-v{contract.version}"
    return directory / f"{stem}.machine.csv", directory / f"{stem}.machine.md"


def machine_reference_scope_path(reference_path: Path | str) -> Path:
    """Canonical adjacent author-time scope for one machine reference."""
    return Path(reference_path).with_suffix(".scope.json")


class SpentHoldout(Exception):
    """A one-shot reference or measurement must remain historical evidence."""


def assert_machine_reference_authoring_allowed(
    slug: str, *, directory: Path = GOLD_DIR,
    method: str = MACHINE_REFERENCE_METHOD, method_version: str = MACHINE_REFERENCE_METHOD_VERSION,
    document_sha256s: tuple[str, ...] = (),
) -> None:
    """Refuse to regenerate a spent or complete machine reference.

    Partial publication is allowed through so a retry can finish writing the
    missing immutable artifacts after a mid-publication failure. The bytes
    are checked at publication time, where the full deterministic payload is
    available.
    """
    historical = SPENT_MEASUREMENT_ARTIFACTS.get(slug)
    if historical is not None or SPENT_SOURCE_HASHES.intersection(document_sha256s):
        raise SpentHoldout(
            f"{slug} is a spent holdout; preserve its historical measurement "
            f"at {historical or SPENT_MEASUREMENT_ARTIFACTS['wsdot-9540']} "
            "and do not regenerate its machine reference"
        )
    csv_path, sidecar = machine_gold_paths(
        slug, directory=directory, method=method, method_version=method_version,
    )
    scope_path = machine_reference_scope_path(csv_path)
    if all(path.exists() for path in (csv_path, sidecar, scope_path)):
        raise SpentHoldout(
            f"{slug} machine reference is already authored; preserve the "
            f"existing evidence at {csv_path}, {sidecar}, {scope_path}"
        )


def _write_reference_bytes_once(path: Path, payload: bytes) -> bool:
    """Create one immutable artifact, or accept identical recovery bytes.

    This is not a multi-file atomic commit; the filesystem cannot make three
    sibling files appear as one unit. The recoverability guarantee here is
    operation-level: each artifact is created exclusively, any already-written
    partial artifact must byte-match the deterministic payload, and a retry can
    finish the missing siblings without overwriting historical evidence.
    """

    try:
        with path.open("xb") as handle:
            handle.write(payload)
        return True
    except FileExistsError as exc:
        try:
            existing = path.read_bytes()
        except OSError as read_exc:
            raise SpentHoldout(
                f"machine reference artifact {path} already exists but is unreadable: "
                f"{read_exc}"
            ) from read_exc
        if existing != payload:
            raise SpentHoldout(
                f"machine reference artifact {path} already exists and diverges "
                "from the authored bytes; preserve the historical evidence"
            ) from exc
        return False


def machine_reference_artifacts(
    slug: str,
    gold: "MachineGold",
    *,
    directory: Path = GOLD_DIR,
) -> tuple[tuple[Path, bytes], ...]:
    """Deterministic bytes for the three immutable machine-reference artifacts."""
    csv_path, sidecar = machine_gold_paths(
        slug, directory=directory, method=gold.method, method_version=gold.method_version,
    )
    csv_bytes = gold_csv(gold).encode()
    sidecar_bytes = render_machine_gold(gold).encode()
    scope_bytes = (
        json.dumps(machine_reference_scope(gold, csv_bytes), indent=2) + "\n"
    ).encode()
    return (
        (csv_path, csv_bytes),
        (sidecar, sidecar_bytes),
        (machine_reference_scope_path(csv_path), scope_bytes),
    )


def publish_machine_reference(
    slug: str,
    gold: "MachineGold",
    *,
    directory: Path = GOLD_DIR,
    publisher=None,
) -> tuple[Path, Path, Path]:
    """Publish immutable machine-reference artifacts with retry-safe recovery."""
    if slug != gold.project:
        raise ValueError("machine reference publication cannot rename its project")
    assert_machine_reference_authoring_allowed(
        slug, directory=directory, method=gold.method, method_version=gold.method_version,
        document_sha256s=tuple(document.sha256 for document in gold.documents),
    )
    artifacts = machine_reference_artifacts(slug, gold, directory=directory)
    for path, _payload in artifacts:
        path.parent.mkdir(parents=True, exist_ok=True)
    writer = publisher or _write_reference_bytes_once
    for path, payload in artifacts:
        writer(path, payload)
    return tuple(path for path, _payload in artifacts)


def write_worksheet(path: Path) -> bool:
    """Write a blank worksheet unless one is already there.

    Returns False when it left an existing diagnostic artifact alone. This
    helper is regenerable, so it always yields to operator work already there.
    """
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(worksheet())
    return True


def main(argv: list[str]) -> int:
    """`make gold ARGS="<slug>"`"""
    import sys

    from corridor.db import WorkerSession

    import argparse

    parser = argparse.ArgumentParser(description="Prepare diagnostics or manage immutable machine references")
    parser.add_argument("slug")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--author", action="store_true")
    operation.add_argument("--replay", type=Path, help="verify a native reference against its recorded source/configuration")
    parser.add_argument("--method", choices=[LEGACY_METHOD.name, NATIVE_METHOD.name])
    parser.add_argument("--method-version", default="1", choices=["1"])
    parser.add_argument("--directory", type=Path, default=GOLD_DIR)
    parser.add_argument("--reference-manifest", type=Path)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    if args.method and not args.author:
        print("--method selects authoring only; replay reads its recorded manifest", file=sys.stderr)
        return 2
    if args.reference_manifest and not args.replay:
        print("--reference-manifest requires --replay", file=sys.stderr)
        return 2
    slug, author = args.slug, args.author
    with WorkerSession() as session:
        project = session.scalars(select(Project).where(Project.slug == slug)).first()
        if project is None:
            print(f"no project {slug!r}", file=sys.stderr)
            return 1

        if args.replay:
            try:
                result = replay_machine_reference(session, project.id, args.replay,
                                                  manifest_path=args.reference_manifest)
            except (ValueError, LookupError, SpentHoldout) as exc:
                print(str(exc), file=sys.stderr)
                return 1
            print(json.dumps(result, sort_keys=True))
            return 0

        if author:
            # The amended path (#81): a machine-authored gold set, stamped
            # as the ceiling it is. Never overwrites a hand-authored
            # gold/<slug>.csv — the stricter artifact keeps its name.
            try:
                gold = author_machine_gold(
                    session, project.id, directory=args.directory,
                    method=args.method or NATIVE_METHOD.name, method_version=args.method_version,
                )
            except (SpentHoldout, ValueError, LookupError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
            try:
                csv_path, sidecar, scope_path = publish_machine_reference(slug, gold, directory=args.directory)
            except (SpentHoldout, ValueError) as exc:
                print(str(exc), file=sys.stderr)
                return 1
            labelled = sum(1 for r in gold.rows if r.critical)
            yes = sum(1 for r in gold.rows if r.critical == "yes")
            print(
                f"{len(gold.rows)} reference rows ({yes} yes / {labelled - yes} no / "
                f"{len(gold.rows) - labelled} blank); excluded {gold.retired} "
                f"retired, {gold.empty_slots} empty slots"
            )
            print(f"reference: {csv_path}")
            print(f"sidecar: {sidecar} (the ceiling caveat travels with it)")
            print(f"scope:   {scope_path} (required for machine-reference scoring)")
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
# before the 9540 seal was lifted. The replacement removes founder row
# labelling, and everything it produces says what it is: a **semi-independent
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

@dataclass(frozen=True)
class MachineGoldRow:
    source_ref: str
    page: int
    critical: str  # "yes" | "no" | "" — blank stays out of the denominator


@dataclass(frozen=True)
class MachineGoldPageImage:
    filename: str
    page_no: int
    image_path: str | None


@dataclass(frozen=True)
class MachineGoldDocument:
    sha256: str
    filename: str


@dataclass(frozen=True)
class MachineGold:
    project: str
    document: str
    rows: tuple[MachineGoldRow, ...]
    # What was read and excluded, for the sidecar's accounting.
    retired: int
    empty_slots: int
    page_images: tuple[MachineGoldPageImage, ...]
    documents: tuple[MachineGoldDocument, ...] = ()
    method: str = MACHINE_REFERENCE_METHOD
    method_version: str = MACHINE_REFERENCE_METHOD_VERSION
    authoring_json: str | None = None


def author_machine_gold(
    session: Session, project_id: int, *, directory: Path = GOLD_DIR,
    method: str = NATIVE_METHOD.name, method_version: str = NATIVE_METHOD.version,
) -> MachineGold:
    """A gold set from the independent grid reading (#81 as amended).

    No model and no extractor column mapping: the resolution group is
    found by the band both WSDOT contracts print above it, the marks are
    cells at the columns beneath, and the canonical reading of each marked
    heading goes through the same vocabulary the Ledger uses — one
    sentence, both sides.
    """
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

    contract = reference_method(method, method_version)
    assert_machine_reference_authoring_allowed(
        project.slug, directory=directory, method=method, method_version=method_version,
        document_sha256s=tuple(document.sha256 for document in documents),
    )
    if contract != NATIVE_METHOD:
        raise ValueError("legacy matrix reference authoring is retired; use native-pdf-cell-grid for new scopes; archived CSVs remain readable")
    return _author_native_documents(project, documents)


def gold_csv(gold: MachineGold) -> str:
    lines = ["source_ref,page,critical"]
    lines += [f"{r.source_ref},{r.page},{r.critical}" for r in gold.rows]
    return "\n".join(lines) + "\n"


def machine_reference_scope(gold: MachineGold, reference_bytes: bytes) -> dict:
    """Bind a machine reference to its author-time method and document set."""
    if not gold.documents:
        raise ValueError("machine reference scope requires document identities")
    contract = reference_method(gold.method, gold.method_version)
    scope = {
        "schema_version": MACHINE_REFERENCE_SCOPE_SCHEMA,
        "project": gold.project,
        "method": contract.name,
        "method_version": contract.version,
        "reference_sha256": hashlib.sha256(reference_bytes).hexdigest(),
        "documents": [
            {"sha256": document.sha256, "filename": document.filename}
            for document in sorted(gold.documents, key=lambda item: item.sha256)
        ],
        "limitations": list(contract.limitations),
        "manifest_provenance": {"kind": "author_time"},
    }
    if contract == NATIVE_METHOD:
        from corridor.reference_methods import validate_native_authoring

        provenance = json.loads(gold.authoring_json) if gold.authoring_json is not None else None
        scope["native_authoring"] = validate_native_authoring(
            provenance, tuple(document.sha256 for document in gold.documents),
        )
    return scope


def render_machine_gold(gold: MachineGold) -> str:
    contract = reference_method(gold.method, gold.method_version)
    if contract == NATIVE_METHOD:
        return "\n".join([
            f"# Machine reference — {gold.project}", "",
            f"Method: `{contract.name}` version `{contract.version}`.", "",
            *contract.limitations, "",
            f"- reference rows: {len(gold.rows)}",
            f"- critical: {sum(row.critical == 'yes' for row in gold.rows)} yes / "
            f"{sum(row.critical == 'no' for row in gold.rows)} no / "
            f"{sum(row.critical == '' for row in gold.rows)} blank",
            f"- excluded: {gold.retired} retired rows, {gold.empty_slots} empty slots",
            "", "Sources and exact reader/recipe identities are in the adjacent scope manifest.",
            "No model mapping or proposal population supplied this enumeration.", "",
            *(f"- {document.filename}: `{document.sha256}`" for document in gold.documents),
        ]) + "\n"
    labelled = sum(1 for r in gold.rows if r.critical)
    yes = sum(1 for r in gold.rows if r.critical == "yes")
    lines = [
        f"# Machine reference — {gold.project}",
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
        f"- reference rows: {len(gold.rows)}",
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
    for page in gold.page_images:
        lines.append(
            f"- [ ] {page.filename} page {page.page_no} — "
            f"`{page.image_path or 'no image'}`"
        )
    return "\n".join(lines) + "\n"


def _author_native_documents(project: Project, documents) -> MachineGold:
    from corridor.native_reference import authoring_identity, read_reference_document

    provenance = authoring_identity()
    rows, readings, images = [], [], []
    retired = empty_slots = 0
    documents = sorted(documents, key=lambda document: document.sha256)
    for document in documents:
        path = stored_file(document)
        if path is None:
            raise LookupError(f"no stored file for {document.filename}")
        result = read_reference_document(Path(path), document.sha256)
        rows.extend(MachineGoldRow(*row) for row in result.rows)
        readings.append(json.loads(result.reading_json))
        images.extend(MachineGoldPageImage(document.filename, page, None) for page in result.pages)
        retired += result.retired
        empty_slots += result.empty_slots
    if authoring_identity() != provenance:
        raise ValueError("native reference recipe changed during authoring")
    return MachineGold(
        project.slug, ", ".join(document.filename for document in documents),
        tuple(rows), retired, empty_slots, tuple(images),
        tuple(MachineGoldDocument(document.sha256, document.filename) for document in documents),
        method=NATIVE_METHOD.name, method_version=NATIVE_METHOD.version,
        authoring_json=json.dumps({**provenance, "readings": readings}, sort_keys=True),
    )


def replay_machine_reference(
    session: Session, project_id: int, reference_path: Path | str,
    *, manifest_path: Path | str | None = None,
) -> dict:
    """Read-only regeneration of a recorded native recipe; never publish over it."""
    from corridor.eval import NothingToMeasure, verified_machine_reference_scope

    project = session.get(Project, project_id)
    if project is None:
        raise LookupError(f"no project {project_id}")
    reference_path = Path(reference_path)
    manifest_path = Path(manifest_path) if manifest_path else machine_reference_scope_path(reference_path)
    payload = json.loads(manifest_path.read_text())
    raw_documents = payload.get("documents") if isinstance(payload, dict) else None
    if not isinstance(raw_documents, list) or not raw_documents or any(
        not isinstance(item, dict) or not is_digest(item.get("sha256")) for item in raw_documents
    ):
        raise ValueError("machine-reference replay manifest has malformed document scope")
    hashes = tuple(item["sha256"] for item in raw_documents)
    if project.slug in SPENT_MEASUREMENT_ARTIFACTS or SPENT_SOURCE_HASHES.intersection(hashes):
        raise SpentHoldout("spent WSDOT 9540 references cannot be regenerated or rescored")
    documents = tuple(session.scalars(select(Document).where(
        Document.project_id == project_id, Document.sha256.in_(hashes),
    )).all())
    reference_bytes = reference_path.read_bytes()
    try:
        scope = verified_machine_reference_scope(
            manifest_path, reference_path=reference_path, reference_bytes=reference_bytes,
            project_slug=project.slug, documents=documents,
        )
    except NothingToMeasure as exc:
        raise ValueError(str(exc)) from exc
    if scope.method != NATIVE_METHOD.name:
        raise ValueError("legacy reference has no recorded reader configuration; load its retained CSV instead")
    from corridor.native_reference import authoring_identity

    recorded = scope.native_authoring
    if any(recorded[key] != value for key, value in authoring_identity().items()):
        raise ValueError("recorded native reference recipe/configuration is unavailable")
    actual = _author_native_documents(project, documents)
    if json.dumps(json.loads(actual.authoring_json), sort_keys=True) != json.dumps(recorded, sort_keys=True):
        raise ValueError("native reference source/reader configuration or result changed")
    if gold_csv(actual).encode() != reference_bytes:
        raise ValueError("native reference enumeration does not replay byte for byte")
    return {"method": scope.method, "method_version": scope.method_version,
            "reference_sha256": scope.sha256, "rows": len(actual.rows), "replayed": True}


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
