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
import pytest

from corridor.db import Session, engine
from corridor.gold import WORKSHEET_COLUMNS, prepare, render, worksheet
from corridor.models import Candidate, DocPage, Document, Project

from pdf_fixture_support import PdfFixture, TextOverflow

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


def draw_table(page, rows):
    # Width adapts to the column count: a fixed 140pt puts an 8-column
    # WSDOT-shaped header off the page edge, and geometry cannot read
    # cells that were never drawn.
    x0, y0, height = 40, 70, 26
    width = min(140, (792 - 2 * x0) // max(1, len(rows[0])))
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            left, top = x0 + c * width, y0 + r * height
            page.rect((left, top, left + width, top + height), width=0.6)
            # Shrink until it fits. A text box that overflows places NOTHING,
            # which made the spanning band read as an empty row and the
            # anchor look broken when the fixture was at fault.
            box = (left + 3, top + 5, left + width - 3, top + height - 3)
            for size in (7, 6, 5, 4, 3):
                try:
                    page.text_box(box, cell, fontsize=size)
                except TextOverflow:
                    continue
                break


def write_pdf(path, pages_rows):
    """A real PDF, one ruled table a page; the fixture declares each page's text."""
    fixture = PdfFixture()
    for rows in pages_rows:
        draw_table(fixture.add_page(width=792, height=612), rows)
    fixture.save(path)
    return fixture


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
    pdf = tmp_path / "m.pdf"
    fixture = write_pdf(pdf, [rows or ROWS])
    doc = Document(
        project_id=project.id,
        sha256=hashlib.sha256(pdf.read_bytes()).hexdigest(),
        filename="matrix.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(doc)
    session.flush()
    session.add(
        DocPage(
            document_id=doc.id, page_no=1, text=fixture.pages[0].expected_text,
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
    from corridor.gold import MachineGold, MachineGoldRow, MachineGoldDocument
    # Publication of already authored bytes is independent of the retired
    # reader, including recovery of an interrupted historical publication.
    gold = MachineGold(project=project.slug, document="matrix.pdf",
        rows=(MachineGoldRow("PSEN-P-1001", 1, "yes"),), retired=0,
        empty_slots=0, page_images=(), documents=(MachineGoldDocument("a" * 64, "matrix.pdf"),))

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
    from corridor.gold import MachineGold, MachineGoldRow, MachineGoldDocument
    # Publication of already authored bytes is independent of the retired
    # reader, including recovery of an interrupted historical publication.
    gold = MachineGold(project=project.slug, document="matrix.pdf",
        rows=(MachineGoldRow("PSEN-P-1001", 1, "yes"),), retired=0,
        empty_slots=0, page_images=(), documents=(MachineGoldDocument("a" * 64, "matrix.pdf"),))
    csv_path, _sidecar = machine_gold_paths(project.slug, directory=tmp_path)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text("divergent bytes\n")

    with pytest.raises(SpentHoldout, match="diverges from the authored bytes"):
        publish_machine_reference(project.slug, gold, directory=tmp_path)

    assert csv_path.read_text() == "divergent bytes\n"
