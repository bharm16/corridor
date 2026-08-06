"""Preparing diagnostic disagreement reports and machine references.

This module never authors a gold set and never decides anything. It reads
the document a second way, says where that reading and the extractor's
disagree, and attaches the document's own words to each disagreement — so
a reviewer confirms a claim by looking at a page rather than by trusting a
count.

The tests pin that restraint as hard as they pin the mechanics: the
worksheet must come out blank, a row carrying a retirement phrase must be
flagged rather than dropped, and the caveat must survive in the report.
"""

import hashlib
import json
import pymupdf
import pytest

from corridor.db import Session, engine
from corridor.gold import WORKSHEET_COLUMNS, prepare, render, worksheet
from corridor.models import Candidate, DocPage, Document, Project

HEADERS = ["Owner", "Conflict ID", "Facility Type", "Location", "Notes"]
ROWS = [
    HEADERS,
    ["HWD", "1", "Water Main", "S 206th Street", ""],
    ["PSE", "2", "AG Power", "S 208th Street", ""],
    # Retired numbering: an id, and the form's own note.
    ["", "3", "", "", "Not Used"],
    # Populated and never extracted — the shape a real miss would take.
    ["Comcast", "4", "AG TV", "32nd Avenue S", ""],
]


def write_pdf(path, rows):
    doc = pymupdf.open()
    page = doc.new_page(width=792, height=612)
    # Width adapts to the column count: a fixed 140pt puts an 8-column
    # WSDOT-shaped header off the page edge, and geometry cannot read
    # cells that were never drawn.
    x0, y0, height = 40, 70, 26
    width = min(140, (792 - 2 * x0) // max(1, len(rows[0])))
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            rect = pymupdf.Rect(
                x0 + c * width, y0 + r * height,
                x0 + (c + 1) * width, y0 + (r + 1) * height,
            )
            page.draw_rect(rect, color=(0, 0, 0), width=0.6)
            # Shrink until it fits. `insert_textbox` returns a negative
            # number and renders NOTHING when the text overflows, which
            # made the spanning band read as an empty row and the anchor
            # look broken when the fixture was at fault.
            box = rect + (3, 5, -3, -3)
            for size in (7, 6, 5, 4, 3):
                if page.insert_textbox(box, cell, fontsize=size) >= 0:
                    break
    doc.save(path)
    doc.close()
    return path


@pytest.fixture
def session():
    connection = engine.connect()
    trans = connection.begin()
    s = Session(bind=connection)
    yield s
    s.close()
    trans.rollback()
    connection.close()


@pytest.fixture
def project(session):
    p = Project(slug="gold-test", name="Gold Prep Test", is_synthetic=True)
    session.add(p)
    session.flush()
    return p


def make_document(session, project, tmp_path, rows=None):
    pdf = write_pdf(tmp_path / "m.pdf", rows or ROWS)
    doc = Document(
        project_id=project.id,
        sha256="g" * 64,
        filename="matrix.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(doc)
    session.flush()
    with pymupdf.open(pdf) as opened:
        text = opened[0].get_text()
    session.add(
        DocPage(
            document_id=doc.id, page_no=1, text=text,
            image_path=str(tmp_path / "p1.png"), text_source="text_layer",
        )
    )
    session.flush()
    doc._pdf_path = str(pdf)
    return doc


def add_candidate(session, project, document, quote):
    session.add(
        Candidate(
            project_id=project.id,
            kind="dependency",
            payload_json={
                "kind": "dependency",
                "fields": {"utility_id": "x", "external_org": "y"},
                "citations": [
                    {
                        "document_id": document.id,
                        "page": 1,
                        "quote": quote,
                        "verified": True,
                        "whole_row": True,
                    }
                ],
            },
            source_document_id=document.id,
            source_pages=[1],
            prompt_version="matrix_tiered_v3",
        )
    )
    session.flush()


@pytest.fixture(autouse=True)
def stored_file_points_at_the_fixture(monkeypatch):
    import corridor.gold as module

    monkeypatch.setattr(
        module, "stored_file", lambda d: getattr(d, "_pdf_path", None)
    )


# --------------------------------------------------------- what it reports


def test_an_extracted_row_is_not_a_disagreement(session, project, tmp_path):
    """Matched by quote containment, which borrows no column mapping from
    the extractor — the enumeration has to be a second reading, not the
    same reading twice."""
    document = make_document(session, project, tmp_path)
    add_candidate(session, project, document, "HWD 1 Water Main S 206th Street")

    prep = prepare(session, project.id)

    assert not any(
        "HWD" in cell for row in prep.unread for _, cell in row.cells
    )


def test_a_row_with_no_candidate_is_reported_with_its_cells(
    session, project, tmp_path
):
    """The point of the exercise: a row the extractor did not produce,
    shown with what the document actually prints in it."""
    document = make_document(session, project, tmp_path)
    add_candidate(session, project, document, "HWD 1 Water Main S 206th Street")

    prep = prepare(session, project.id)
    comcast = next(
        row for row in prep.unread
        if any("Comcast" in cell for _, cell in row.cells)
    )

    values = [cell for _, cell in comcast.cells]
    assert "AG TV" in values
    assert "32nd Avenue S" in values
    assert comcast.page_no == 1


def test_cells_carry_the_columns_printed_heading(session, project, tmp_path):
    """A cell index means nothing to a reviewer; the heading above it does."""
    document = make_document(session, project, tmp_path)
    add_candidate(session, project, document, "HWD 1 Water Main S 206th Street")

    prep = prepare(session, project.id)
    comcast = next(
        row for row in prep.unread
        if any("Comcast" in cell for _, cell in row.cells)
    )

    assert comcast.heading_for(comcast.cells[0][0]) == "Owner"


# ------------------------------------------ it flags, it never decides


def test_a_retirement_phrase_is_flagged_not_dropped(session, project, tmp_path):
    """#128 is undecided: whether `Not Used` retires a row number or
    describes a facility is a judgement nobody has made. A tool that
    excluded these would be making it silently, and on 9424 that is 97
    rows — far past what a ≥95% bar absorbs."""
    document = make_document(session, project, tmp_path)
    add_candidate(session, project, document, "HWD 1 Water Main S 206th Street")

    prep = prepare(session, project.id)
    retired = [row for row in prep.unread if row.kind == "carries a retirement phrase"]

    assert len(retired) == 1
    assert any("Not Used" in cell for _, cell in retired[0].cells)


def test_an_extracted_row_carrying_a_retirement_phrase_is_contested(
    session, project, tmp_path
):
    """9424 page 9, id 210: a fully populated conflict row whose notes
    read `Not used`. It disagrees with itself, the extractor produced it,
    and only a human can say whether that was right."""
    rows = [
        HEADERS,
        ["PSE", "210", "UG Power", "Military Road", "Not used"],
    ]
    document = make_document(session, project, tmp_path, rows)
    add_candidate(session, project, document, "PSE 210 UG Power Military Road Not used")

    prep = prepare(session, project.id)

    assert len(prep.contested) == 1
    assert any("210" in cell for _, cell in prep.contested[0].cells)


def test_the_header_is_reported_rather_than_assumed_away(
    session, project, tmp_path
):
    """The header is never identified as such and dropped — it appears
    like any other unmatched row, marked by position. Assuming the top
    rows are header would hide a missed leading row, which is exactly the
    failure this exercise exists to catch."""
    document = make_document(session, project, tmp_path)
    add_candidate(session, project, document, "HWD 1 Water Main S 206th Street")

    prep = prepare(session, project.id)

    header = next(
        row for row in prep.unread
        if any("Conflict ID" in cell for _, cell in row.cells)
    )
    assert header.above_body is True


# ------------------------------------------------- the worksheet is blank


def test_the_worksheet_is_blank(session, project, tmp_path):
    """The whole point of the middle path. A worksheet pre-filled with the
    extractor's answers makes the labeller a checker, and a checker
    agrees — which is the anchoring #81 forbids. The reviewer authors the
    denominator; this supplies only the columns."""
    out = worksheet()

    assert out.strip() == ",".join(WORKSHEET_COLUMNS)
    assert "source_ref" in WORKSHEET_COLUMNS
    assert "critical" in WORKSHEET_COLUMNS


def test_the_worksheet_columns_are_the_ones_the_eval_reads():
    """A worksheet the eval cannot read wastes the labelling, and the
    labelling is the expensive part."""
    from corridor.eval import REQUIRED_COLUMNS

    assert set(REQUIRED_COLUMNS) <= set(WORKSHEET_COLUMNS)


# ------------------------------------------------------------- the report


def test_the_report_states_the_shared_blind_spot(session, project, tmp_path):
    """The hole that cannot be closed by code: this enumeration and the
    extractor share PyMuPDF's table detection, so a region that library
    drops is invisible to both. A report that omitted this would be
    claiming an independence it does not have."""
    document = make_document(session, project, tmp_path)
    add_candidate(session, project, document, "HWD 1 Water Main S 206th Street")

    out = render(prepare(session, project.id))

    assert "shares" in out.lower()
    assert "page image" in out.lower()


def test_the_report_tallies_every_page(session, project, tmp_path):
    document = make_document(session, project, tmp_path)
    add_candidate(session, project, document, "HWD 1 Water Main S 206th Street")

    prep = prepare(session, project.id)

    assert [t.page_no for t in prep.tallies] == [1]
    assert prep.tallies[0].extracted == 1


def test_a_project_with_no_extraction_says_so(session, project, tmp_path):
    """Nothing to disagree with is not the same as agreement."""
    # Bound, not discarded: the identity map holds Documents weakly, so a
    # dropped reference lets the fixture's `_pdf_path` vanish with the
    # object and the re-query builds a fresh one without it.
    document = make_document(session, project, tmp_path)

    prep = prepare(session, project.id)
    assert document.filename in prep.document

    assert prep.extracted_total == 0
    assert "no extraction" in render(prep).lower()


def test_position_annotates_a_row_it_never_classifies_it(
    session, project, tmp_path
):
    """The defect this tool nearly shipped with.

    On 9424 a position-first classification filed ids 163 and 256 — the
    only two rows genuinely needing human eyes — into a bucket of
    thirty-three header rows labelled "above the first extracted row".
    Content decides the category; position is a note beside it.
    """
    rows = [
        HEADERS,
        # An identifier-only row above everything the extractor matched.
        ["", "163", "", "", ""],
        ["HWD", "1", "Water Main", "S 206th Street", ""],
    ]
    document = make_document(session, project, tmp_path, rows)
    add_candidate(session, project, document, "HWD 1 Water Main S 206th Street")

    prep = prepare(session, project.id)
    orphan = next(
        row for row in prep.unread
        if any(value == "163" for _, value in row.cells)
    )

    assert orphan.kind == "identifier only"
    assert orphan.above_body is True


# ---------------- machine-authored gold: the ceiling (#81 as amended)


WSDOT_SHAPE = [
    # The spanning group band the family prints above its marked columns —
    # what the authoring anchors on, since the column names differ between
    # contracts (9424 names them after its route number, 9540 does not).
    ["", "", "", "RECOMMENDED RESOLUTION", "", "", "", ""],
    ["Owner", "Conflict ID", "Facility Type", "509 Relocation Needed",
     "ST Relocation Needed", "Retain and Protect", "Abandon / Deactivate", "Notes"],
    # One mark on the critical side.
    ["HWD", "1", "Water Main", "X", "", "", "", ""],
    # One mark on the stays side.
    ["PSE", "2", "AG Power", "", "", "X", "", ""],
    # Two marks that agree: both relocations.
    ["Comcast", "3", "AG TV", "X", "X", "", "", ""],
    # Marks on both sides of the line: unsettled, blank label.
    ["Lumen", "4", "Fiber", "", "X", "X", "", ""],
    # No mark at all: blank label.
    ["Zayo", "5", "Fiber", "", "", "", "", ""],
    # Retired numbering and an empty slot: not conflicts, not rows.
    ["", "6", "", "", "", "", "", "Not Used"],
    ["", "7", "", "", "", "", "", ""],
    # Abandonment is critical (ADR-0009).
    ["HWD", "8", "Sewer", "", "", "", "X", ""],
]


def authored(session, project, tmp_path, rows=None):
    from corridor.gold import author_machine_gold

    # Bound, not discarded: the identity map holds Documents weakly, and a
    # dropped reference takes the fixture's `_pdf_path` with it.
    document = make_document(session, project, tmp_path, rows or WSDOT_SHAPE)
    gold = author_machine_gold(session, project.id)
    assert document.filename in gold.document
    return gold


def test_machine_gold_reads_criticality_from_the_marks(session, project, tmp_path):
    """Header-anchored grid reading, the published table's line: relocation
    and abandonment critical, retain-and-protect not (ADR-0009 via the one
    vocabulary that already encodes it)."""
    gold = authored(session, project, tmp_path)
    by_ref = {row.source_ref: row.critical for row in gold.rows}

    assert by_ref["1"] == "yes"
    assert by_ref["2"] == "no"
    assert by_ref["3"] == "yes"
    assert by_ref["8"] == "yes"


def test_a_row_the_document_left_unsettled_gets_a_blank_label(
    session, project, tmp_path
):
    """Marks on both sides, or no mark: the document has not settled, and
    blank keeps the row out of the >=95% denominator — ADR-0009's "left
    unlabelled", exactly as the hand rule would have written it."""
    gold = authored(session, project, tmp_path)
    by_ref = {row.source_ref: row.critical for row in gold.rows}

    assert by_ref["4"] == ""
    assert by_ref["5"] == ""


def test_retired_rows_and_empty_slots_are_not_gold_rows(
    session, project, tmp_path
):
    """The denominator counts what names a facility (ADR-0012)."""
    gold = authored(session, project, tmp_path)

    assert {row.source_ref for row in gold.rows} == {"1", "2", "3", "4", "5", "8"}


def test_authoring_refuses_a_layout_without_its_anchor(
    session, project, tmp_path
):
    """9540 is expected to be the twin's form. If it is not, the authoring
    stops loudly rather than guessing — a surprise layout is a decision
    for a human, not a fallback for a script."""
    from corridor.gold import LayoutAnchorMissing

    with pytest.raises(LayoutAnchorMissing):
        authored(session, project, tmp_path, rows=ROWS)


def test_machine_gold_is_stamped_as_a_ceiling(session, project, tmp_path):
    """The caveat travels with the artifact, not just the ticket: the
    sidecar says semi-independent, names the shared blind spot, and lists
    the page images."""
    from corridor.gold import render_machine_gold

    gold = authored(session, project, tmp_path)
    out = render_machine_gold(gold)

    assert "ceiling" in out.lower()
    assert "shares" in out.lower()
    assert "#81" in out


def test_the_hand_worksheet_path_is_untouched(session, project, tmp_path):
    """The amendment adds a path; it does not delete the stricter one."""
    out = worksheet()

    assert out.strip() == ",".join(WORKSHEET_COLUMNS)


def test_machine_gold_round_trips_through_the_eval_loader(
    session, project, tmp_path
):
    """The artifact is only worth authoring if the eval can read it."""
    from corridor.eval import load_gold
    from corridor.gold import gold_csv

    gold = authored(session, project, tmp_path)
    path = tmp_path / "machine.csv"
    path.write_text(gold_csv(gold))

    records = load_gold(path)

    assert len(records) == 6
    critical = {r.source_ref: r.critical for r in records}
    assert critical["1"] is True
    assert critical["2"] is False
    # Blank reads as not-critical — the documented contract that keeps an
    # unsettled row out of the ≥95% denominator. None would mean the gold
    # set labels no criticality at all, which this one does.
    assert critical["4"] is False


def test_machine_reference_scope_manifest_binds_csv_method_and_documents(
    session, project, tmp_path
):
    from corridor.gold import gold_csv, machine_reference_scope

    gold = authored(session, project, tmp_path)
    csv_text = gold_csv(gold)

    scope = machine_reference_scope(gold, csv_text.encode())

    assert scope == {
        "schema_version": "corridor.machine-reference-scope.v2",
        "project": project.slug,
        "method": "pymupdf-table-grid",
        "method_version": "1",
        "reference_sha256": hashlib.sha256(csv_text.encode()).hexdigest(),
        "documents": [
            {
                "sha256": gold.documents[0].sha256,
                "filename": gold.documents[0].filename,
            }
        ],
        "limitations": [
            "Semi-independent ceiling: the machine reference and extractor "
            "share PyMuPDF table detection, so a region omitted by that "
            "library is invisible to both."
        ],
        "manifest_provenance": {"kind": "author_time"},
    }


def test_the_anchor_is_the_family_band_not_one_contracts_column(
    session, project, tmp_path
):
    """The M7 cold run's second finding.

    Authoring anchored on `509 Relocation Needed` and refused the holdout,
    which is the same Appendix U with its columns named `RELOCATION` /
    `PROTECTION IN PLACE` / `ABANDON/ DEACTIVATE/ REMOVE`. The refusal was
    right and its reason was wrong: what this authoring needs is a marked
    resolution group, not one contract's route number.
    """
    rows = [
        ["", "", "", "RECOMMENDED RESOLUTION", "", "", ""],
        ["UTILITY OWNER", "UTILITY ID", "FACILITY TYPE", "RELOCATION",
         "PROTECTION IN PLACE", "ABANDON/ DEACTIVATE/ REMOVE", "NOTES"],
        ["PSE", "PSEN-G-1001", "Gas Line", "X", "", "", ""],
        ["City of Fife", "COFI-W-1003", "Water", "", "X", "", ""],
        ["Comcast", "CMCS-C-1001", "Duct", "", "", "X", ""],
        ["AT&T", "ATAT-F-1002", "Duct", "X", "X", "", ""],
    ]
    gold = authored(session, project, tmp_path, rows)
    by_ref = {r.source_ref: r.critical for r in gold.rows}

    assert by_ref["PSEN-G-1001"] == "yes"
    assert by_ref["COFI-W-1003"] == "no"
    assert by_ref["CMCS-C-1001"] == "yes"
    # Marked on both sides: the document has not settled.
    assert by_ref["ATAT-F-1002"] == ""


def test_a_band_without_readable_marks_still_refuses(session, project, tmp_path):
    """The band alone is not enough — a form that groups columns this
    vocabulary cannot read is a human decision, not a fallback."""
    from corridor.gold import LayoutAnchorMissing

    rows = [
        ["", "", "RECOMMENDED RESOLUTION", ""],
        ["UTILITY OWNER", "UTILITY ID", "SOME NEW COLUMN", "NOTES"],
        ["PSE", "X-1", "X", ""],
    ]
    with pytest.raises(LayoutAnchorMissing):
        authored(session, project, tmp_path, rows)


def make_multipage(session, project, tmp_path, pages_rows, sha="mp"):
    """A Document of N real pages, so continuation pages are real."""
    merged = pymupdf.open()
    for i, rows in enumerate(pages_rows):
        one = write_pdf(tmp_path / f"{sha}-{i}.pdf", rows)
        with pymupdf.open(one) as opened:
            merged.insert_pdf(opened)
    path = tmp_path / f"{sha}.pdf"
    merged.save(path)
    merged.close()

    doc = Document(
        project_id=project.id, sha256=sha[0] * 64, filename=f"{sha}.pdf",
        doc_type="matrix", parse_status="parsed", pages=len(pages_rows),
    )
    session.add(doc)
    session.flush()
    with pymupdf.open(path) as opened:
        for i in range(len(pages_rows)):
            session.add(
                DocPage(
                    document_id=doc.id, page_no=i + 1,
                    text=opened[i].get_text(),
                    image_path=str(tmp_path / f"{sha}-{i}.png"),
                    text_source="text_layer",
                )
            )
    session.flush()
    doc._pdf_path = str(path)
    return doc


def test_a_continuation_page_is_read_not_skipped(session, project, tmp_path):
    """The M7 cold run's third finding, and the one that mattered most.

    9540's Power listing runs to two pages: page 1 prints the
    `RECOMMENDED RESOLUTION` band, page 2 reprints the column headings
    without it. Requiring the band on every page dropped page 2 whole —
    ten conflicts absent from the denominator, which is a gold set that
    does not cover its own document.

    The band anchors the *document*; each page finds its header by the
    resolution headings it prints.
    """
    from corridor.gold import author_machine_gold

    headings = [
        "UTILITY OWNER", "UTILITY ID", "FACILITY TYPE", "RELOCATION",
        "PROTECTION IN PLACE", "ABANDON/ DEACTIVATE", "NOTES",
    ]
    first = [
        ["", "", "", "RECOMMENDED RESOLUTION", "", "", ""],
        headings,
        ["PSE", "PSEN-P-1001", "Power", "X", "", "", ""],
    ]
    # No band: the continuation page reprints only the headings.
    second = [
        headings,
        ["TPU", "TCPR-P-1043", "Power", "X", "", "", ""],
        ["TPU", "TCPR-P-1063", "Power", "", "", "X", ""],
    ]
    document = make_multipage(session, project, tmp_path, [first, second])

    gold = author_machine_gold(session, project.id)
    assert document.filename in gold.document

    refs = {r.source_ref for r in gold.rows}
    assert refs == {"PSEN-P-1001", "TCPR-P-1043", "TCPR-P-1063"}
    assert {r.critical for r in gold.rows} == {"yes"}


def test_machine_gold_sidecar_names_the_document_beside_each_page_image(
    session, project, tmp_path
):
    """Two documents can both contribute `page 1`.

    The sidecar is the human checklist that narrows the shared blind spot.
    Once machine gold grew from one matrix to every matrix in the project,
    `page 1` stopped being a unique identifier for that checklist.
    """
    from corridor.gold import author_machine_gold, render_machine_gold

    rows = [
        ["", "", "", "RECOMMENDED RESOLUTION", "", "", ""],
        [
            "UTILITY OWNER",
            "UTILITY ID",
            "FACILITY TYPE",
            "RELOCATION",
            "PROTECTION IN PLACE",
            "ABANDON/ DEACTIVATE",
            "NOTES",
        ],
        ["PSE", "PSEN-P-1001", "Power", "X", "", "", ""],
    ]
    first = make_multipage(session, project, tmp_path, [rows], sha="power")
    second = make_multipage(session, project, tmp_path, [rows], sha="water")

    gold = author_machine_gold(session, project.id)
    out = render_machine_gold(gold)

    assert first.filename in gold.document
    assert second.filename in gold.document
    assert f"{first.filename} page 1" in out
    assert f"{second.filename} page 1" in out


def test_a_document_that_never_prints_the_band_is_still_refused(
    session, project, tmp_path
):
    """Anchoring per document, not per page, must not become anchoring
    nowhere: a form that never groups its resolution columns is still a
    human decision."""
    from corridor.gold import LayoutAnchorMissing

    headings = ["UTILITY OWNER", "UTILITY ID", "RELOCATION", "NOTES"]
    pages = [
        [headings, ["PSE", "P-1", "X", ""]],
        [headings, ["TPU", "P-2", "X", ""]],
    ]
    # Bound: the identity map holds Documents weakly, and a dropped
    # reference takes the fixture's `_pdf_path` with it.
    document = make_multipage(session, project, tmp_path, pages, sha="nb")
    assert document.pages == 2

    with pytest.raises(LayoutAnchorMissing):
        from corridor.gold import author_machine_gold

        author_machine_gold(session, project.id)


# ------------------------------------------------- the file-safety rules


def test_a_worksheet_in_progress_is_never_overwritten(tmp_path):
    """A regenerable helper yields to any existing operator artifact."""
    from corridor.gold import write_worksheet

    sheet = tmp_path / "wsdot-9540-worksheet.csv"

    assert write_worksheet(sheet) is True
    sheet.write_text("source_ref,critical\nPSEN-G-1001,yes\n")

    assert write_worksheet(sheet) is False
    assert "PSEN-G-1001,yes" in sheet.read_text()


def test_a_blank_worksheet_is_blank(tmp_path):
    from corridor.gold import worksheet, write_worksheet

    sheet = tmp_path / "w.csv"
    write_worksheet(sheet)

    assert sheet.read_text() == worksheet()


def test_machine_gold_never_claims_the_hand_authored_name(tmp_path):
    """A machine gold set is a ceiling (#81 as amended). Letting it take
    the name a person's labelling would use is how a ceiling gets read as
    a floor."""
    from corridor.gold import machine_gold_paths, machine_reference_scope_path

    csv_path, sidecar = machine_gold_paths("wsdot-9540", directory=tmp_path)
    scope = machine_reference_scope_path(csv_path)

    assert csv_path.name == "wsdot-9540.machine.csv"
    assert sidecar.name == "wsdot-9540.machine.md"
    assert scope.name == "wsdot-9540.machine.scope.json"
    assert csv_path.name != "wsdot-9540.csv"


def test_spent_wsdot_9540_machine_reference_cannot_be_regenerated():
    from corridor.gold import SpentHoldout, assert_machine_reference_authoring_allowed

    with pytest.raises(SpentHoldout, match="spent.*historical"):
        assert_machine_reference_authoring_allowed("wsdot-9540")


def test_a_complete_machine_reference_cannot_be_reauthored(tmp_path):
    from corridor.gold import (
        SpentHoldout,
        assert_machine_reference_authoring_allowed,
        machine_gold_paths,
        machine_reference_scope_path,
    )

    assert_machine_reference_authoring_allowed(
        "unspent-project", directory=tmp_path
    )
    reference, sidecar = machine_gold_paths(
        "unspent-project", directory=tmp_path
    )
    scope = machine_reference_scope_path(reference)
    reference.write_text("first immutable reference\n")
    sidecar.write_text("first immutable sidecar\n")
    scope.write_text('{"schema_version":"corridor.machine-reference-scope.v2"}\n')

    with pytest.raises(SpentHoldout, match="already authored"):
        assert_machine_reference_authoring_allowed(
            "unspent-project", directory=tmp_path
        )
    assert reference.read_text() == "first immutable reference\n"


def test_partial_machine_reference_publication_is_recoverable(
    session, project, tmp_path
):
    from corridor.gold import (
        author_machine_gold,
        machine_gold_paths,
        machine_reference_scope_path,
        publish_machine_reference,
        render_machine_gold,
        gold_csv,
        machine_reference_scope,
    )

    rows = [
        ["", "", "", "RECOMMENDED RESOLUTION", "", "", ""],
        [
            "UTILITY OWNER",
            "UTILITY ID",
            "FACILITY TYPE",
            "RELOCATION",
            "PROTECTION IN PLACE",
            "ABANDON/ DEACTIVATE",
            "NOTES",
        ],
        ["PSE", "PSEN-P-1001", "Power", "X", "", "", ""],
    ]
    gold = authored(session, project, tmp_path, rows)

    attempts = []

    def fail_after_first(path, payload):
        attempts.append(path.name)
        if len(attempts) == 2:
            raise RuntimeError("mid-publication failure")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(payload)

    with pytest.raises(RuntimeError, match="mid-publication failure"):
        publish_machine_reference(
            project.slug,
            gold,
            directory=tmp_path,
            publisher=fail_after_first,
        )

    csv_path, sidecar = machine_gold_paths(project.slug, directory=tmp_path)
    scope_path = machine_reference_scope_path(csv_path)
    expected_csv = gold_csv(gold).encode()
    expected_sidecar = render_machine_gold(gold).encode()
    expected_scope = (
        json.dumps(machine_reference_scope(gold, expected_csv), indent=2) + "\n"
    ).encode()

    assert csv_path.read_bytes() == expected_csv
    assert not sidecar.exists()
    assert not scope_path.exists()

    publish_machine_reference(project.slug, gold, directory=tmp_path)

    assert csv_path.read_bytes() == expected_csv
    assert sidecar.read_bytes() == expected_sidecar
    assert scope_path.read_bytes() == expected_scope


def test_partial_machine_reference_publication_refuses_divergent_existing_bytes(
    session, project, tmp_path
):
    from corridor.gold import (
        SpentHoldout,
        author_machine_gold,
        machine_gold_paths,
        publish_machine_reference,
    )

    rows = [
        ["", "", "", "RECOMMENDED RESOLUTION", "", "", ""],
        [
            "UTILITY OWNER",
            "UTILITY ID",
            "FACILITY TYPE",
            "RELOCATION",
            "PROTECTION IN PLACE",
            "ABANDON/ DEACTIVATE",
            "NOTES",
        ],
        ["PSE", "PSEN-P-1001", "Power", "X", "", "", ""],
    ]
    gold = authored(session, project, tmp_path, rows)
    csv_path, _sidecar = machine_gold_paths(project.slug, directory=tmp_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text("divergent bytes\n")

    with pytest.raises(SpentHoldout, match="diverges from the authored bytes"):
        publish_machine_reference(project.slug, gold, directory=tmp_path)

    assert csv_path.read_text() == "divergent bytes\n"
