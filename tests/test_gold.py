"""Preparing a hand-labelled gold set (#88, ADR-0008).

This module never authors a gold set and never decides anything. It reads
the document a second way, says where that reading and the extractor's
disagree, and attaches the document's own words to each disagreement — so
a reviewer confirms a claim by looking at a page rather than by trusting a
count.

The tests pin that restraint as hard as they pin the mechanics: the
worksheet must come out blank, a row carrying a retirement phrase must be
flagged rather than dropped, and the caveat must survive in the report.
"""

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
            page.insert_textbox(rect + (3, 5, -3, -3), cell, fontsize=7)
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
