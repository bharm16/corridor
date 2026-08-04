"""Tiered matrix extraction (ADR-0006).

Tier 1 is the point: the model says what the columns *mean* and the page's
own word boxes say what they *contain*. The model never writes a digit, so
the transcription error class the validation gate measured at 5.1% cannot
occur — these tests assert that the values come from the page rather than
from the model, which is the whole claim.

Shape and wiring only. Quality is measured by a real run against real
documents (#76), never by a unit test. CI never calls a model.

Geometry is real here rather than faked: the fixtures are PDFs generated
at test time, so `find_tables()` and the word-box reader do their actual
work without depending on a fetched corpus.
"""

import pymupdf
import pytest
from sqlalchemy import select

from corridor.extract import NoMatrixFound
from corridor.db import Session, engine
from corridor.extract_matrix import (
    PROMPT_VERSION,
    TIER_STRUCTURE,
    TIER_TRANSCRIBE,
    extract_document,
)
from corridor.models import Candidate, DocPage, Document, Project

# A TxDOT-shaped page: the owner is a column, every row states its own.
TXDOT_ROWS = [
    ["Utility ID", "Utility Owner", "Utility Type", "Start Station", "End Station", "Sheet No."],
    ["FOC1-133", "AT&T Texas (SWBT)", "Telecom", "1092+92", "1093+74", "12"],
    ["FOC1-134", "AT&T Texas (SWBT)", "Telecom", "1093+25", "1102+04", "13"],
]

# An FDOT-shaped page: no owner column at all — the page header states it.
FDOT_ROWS = [
    ["Conflict #", "Station Begin", "Station End", "Facility Description"],
    ["1", "203+40.00", "206+40.00", "BTV, Size UNK"],
    ["2", "206+93.00", "206+93.00", "BTV Pedestal"],
]


def write_pdf(path, rows, banner=None):
    """A real PDF with a real ruled table, so geometry does its actual work."""
    doc = pymupdf.open()
    page = doc.new_page(width=792, height=612)
    x0, y0, width, height = 40, 70, 118, 24
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            rect = pymupdf.Rect(
                x0 + c * width, y0 + r * height,
                x0 + (c + 1) * width, y0 + (r + 1) * height,
            )
            page.draw_rect(rect, color=(0, 0, 0), width=0.6)
            page.insert_textbox(rect + (3, 5, -3, -3), cell, fontsize=7)
    if banner:
        page.insert_text((40, 50), banner, fontsize=9)
    doc.save(path)
    doc.close()
    return path


class StubClient:
    """Recorded responses. CI never calls a model."""

    def __init__(self, responses, model="gpt-5.6-luna"):
        self.responses = list(responses)
        self.model = model
        self.max_workers = 2
        self.calls = []

    def complete(self, *, system, user, schema, images=(), logprobs=False):
        self.calls.append(
            {"system": system, "user": user, "schema": schema,
             "images": [str(i) for i in images], "logprobs": logprobs}
        )
        return self.responses.pop(0) if self.responses else structure()


def structure(*, matrix_table=0, header_row=0, columns=None, owner=None, is_matrix=True):
    if columns is None:
        columns = [
            {"index": 0, "canonical_field": "utility_id"},
            {"index": 1, "canonical_field": "external_org"},
            {"index": 2, "canonical_field": "utility_type"},
            {"index": 3, "canonical_field": "station_from"},
            {"index": 4, "canonical_field": "station_to"},
            {"index": 5, "canonical_field": None},
        ]
    return {
        "is_utility_matrix": is_matrix,
        "page_attributes": {"external_org": owner},
        "matrix_table": matrix_table,
        "header_row": header_row,
        "columns": columns,
    }


FDOT_COLUMNS = [
    {"index": 0, "canonical_field": "utility_id"},
    {"index": 1, "canonical_field": "station_from"},
    {"index": 2, "canonical_field": "station_to"},
    {"index": 3, "canonical_field": "utility_type"},
]


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
    p = Project(slug="tier-test", name="Tiered Extraction Test", is_synthetic=True)
    session.add(p)
    session.flush()
    return p


def make_document(session, project, tmp_path, rows, *, banner=None, sha="a", pages=1):
    """A Document whose stored PDF and page text are both real."""
    pdf = write_pdf(tmp_path / f"{sha}.pdf", rows, banner=banner)
    doc = Document(
        project_id=project.id,
        sha256=sha * 64,
        filename=f"{sha}-matrix.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=pages,
    )
    session.add(doc)
    session.flush()
    image = tmp_path / f"{sha}-0001.png"
    image.write_bytes(b"\x89PNG page image")
    with pymupdf.open(pdf) as opened:
        text = opened[0].get_text()
    session.add(
        DocPage(
            document_id=doc.id, page_no=1, text=text,
            image_path=str(image), text_source="text_layer",
        )
    )
    session.flush()
    doc._pdf_path = str(pdf)
    return doc


@pytest.fixture(autouse=True)
def stored_pdf_points_at_the_fixture(monkeypatch):
    """The content-addressed store is not populated in tests."""
    import corridor.extract_matrix as module

    monkeypatch.setattr(
        module, "stored_pdf", lambda document: getattr(document, "_pdf_path", None)
    )


# ------------------------------------------------- Tier 1: values from the page


def test_values_come_from_the_page_not_from_the_model(session, project, tmp_path):
    """The claim ADR-0006 rests on.

    The stub returns a mapping and nothing else — it has no opportunity to
    supply a station number. Every value below was read off the PDF.
    """
    doc = make_document(session, project, tmp_path, TXDOT_ROWS)

    candidates = extract_document(session, doc, client=StubClient([structure()]))

    fields = [c.payload_json["fields"] for c in candidates]
    assert len(candidates) == 2
    assert fields[0]["utility_id"] == "FOC1-133"
    assert fields[0]["external_org"] == "AT&T Texas (SWBT)"
    assert fields[0]["station_from"] == "1092+92"
    assert fields[1]["station_to"] == "1102+04"


def test_the_header_row_is_not_a_candidate(session, project, tmp_path):
    doc = make_document(session, project, tmp_path, TXDOT_ROWS)

    candidates = extract_document(session, doc, client=StubClient([structure()]))

    assert all(
        c.payload_json["fields"]["utility_id"] != "Utility ID" for c in candidates
    )


def test_an_unmapped_column_is_recorded_by_its_printed_name(
    session, project, tmp_path
):
    """The document says something the Ledger has no field for.

    Reported to a human rather than guessed at, because a wrong mapping
    files a value under the wrong heading for every row on the page.
    """
    doc = make_document(session, project, tmp_path, TXDOT_ROWS)

    candidates = extract_document(session, doc, client=StubClient([structure()]))

    assert candidates[0].payload_json["unmapped_columns"] == ["Sheet No."]
    assert "Sheet No." not in str(candidates[0].payload_json["fields"])


def test_a_canonical_field_the_extractor_does_not_know_is_treated_as_unmapped(
    session, project, tmp_path
):
    """The extractor never invents a field; the vocabulary is versioned."""
    doc = make_document(session, project, tmp_path, TXDOT_ROWS)
    columns = [
        {"index": 0, "canonical_field": "utility_id"},
        {"index": 1, "canonical_field": "external_org"},
        {"index": 2, "canonical_field": "invented_field"},
        {"index": 3, "canonical_field": "station_from"},
        {"index": 4, "canonical_field": "station_to"},
        {"index": 5, "canonical_field": None},
    ]

    candidates = extract_document(
        session, doc, client=StubClient([structure(columns=columns)])
    )

    assert "invented_field" not in candidates[0].payload_json["fields"]
    assert "Utility Type" in candidates[0].payload_json["unmapped_columns"]


def test_the_page_number_comes_from_our_code(session, project, tmp_path):
    doc = make_document(session, project, tmp_path, TXDOT_ROWS)

    candidates = extract_document(session, doc, client=StubClient([structure()]))

    assert all(c.payload_json["citations"][0]["page"] == 1 for c in candidates)
    assert all(c.source_pages == [1] for c in candidates)


def test_candidates_record_model_prompt_version_and_tier(session, project, tmp_path):
    doc = make_document(session, project, tmp_path, TXDOT_ROWS)

    candidate = extract_document(session, doc, client=StubClient([structure()]))[0]

    assert candidate.model == "gpt-5.6-luna"
    assert candidate.prompt_version == PROMPT_VERSION
    assert candidate.payload_json["tier"] == TIER_STRUCTURE


def test_rows_read_from_the_page_verify(session, project, tmp_path):
    """Values read off the page are on the page, by construction."""
    doc = make_document(session, project, tmp_path, TXDOT_ROWS)

    candidates = extract_document(session, doc, client=StubClient([structure()]))

    assert all(c.citations_verified for c in candidates)
    assert all(c.payload_json["unverified_fields"] == [] for c in candidates)


# -------------------------------------------------- Tier 1: page attributes


def test_every_row_inherits_the_pages_external_party(session, project, tmp_path):
    """FDOT names the owner once in the page header and never in a column."""
    doc = make_document(
        session, project, tmp_path, FDOT_ROWS,
        banner="UTILITY AGENCY OWNER: AT&T TCA",
    )

    candidates = extract_document(
        session, doc,
        client=StubClient([structure(columns=FDOT_COLUMNS, owner="AT&T TCA")]),
    )

    assert len(candidates) == 2
    assert all(
        c.payload_json["fields"]["external_org"] == "AT&T TCA" for c in candidates
    )
    assert all(c.citations_verified for c in candidates)


def test_a_row_that_states_its_own_party_wins(session, project, tmp_path):
    """TxDOT repeats the owner per row. Both layouts are ordinary."""
    doc = make_document(session, project, tmp_path, TXDOT_ROWS)

    candidates = extract_document(
        session, doc, client=StubClient([structure(owner="Comcast")])
    )

    assert candidates[0].payload_json["fields"]["external_org"] == "AT&T Texas (SWBT)"


def test_a_page_attribute_absent_from_the_page_marks_its_rows_unverified(
    session, project, tmp_path
):
    """One bad inherited value is every row on the page, and it must look like it."""
    doc = make_document(
        session, project, tmp_path, FDOT_ROWS,
        banner="UTILITY AGENCY OWNER: AT&T TCA",
    )

    candidates = extract_document(
        session, doc,
        client=StubClient([structure(columns=FDOT_COLUMNS, owner="Verizon Florida")]),
    )

    assert len(candidates) == 2
    assert not any(c.citations_verified for c in candidates)
    assert all(
        c.payload_json["unverified_fields"] == ["external_org"] for c in candidates
    )


# ------------------------------------------------------------ Tier 1: outcomes


def test_a_matrix_with_no_conflict_rows_returns_empty(session, project, tmp_path):
    doc = make_document(session, project, tmp_path, [TXDOT_ROWS[0]])

    assert extract_document(session, doc, client=StubClient([structure()])) == []


def test_a_document_with_no_matrix_page_raises(session, project, tmp_path):
    """`NoMatrixFound` keeps its meaning: unreadable is not the same as empty."""
    doc = make_document(session, project, tmp_path, TXDOT_ROWS)

    with pytest.raises(NoMatrixFound):
        extract_document(
            session, doc,
            client=StubClient([structure(matrix_table=None, is_matrix=False)]),
        )


def test_a_document_with_no_page_images_raises(session, project, tmp_path):
    doc = make_document(session, project, tmp_path, TXDOT_ROWS)
    for page in session.scalars(
        select(DocPage).where(DocPage.document_id == doc.id)
    ):
        page.image_path = None
    session.flush()

    with pytest.raises(NoMatrixFound, match="page image"):
        extract_document(session, doc, client=StubClient([]))


def test_a_group_title_band_is_not_a_row(session, project, tmp_path):
    """FDOT prints a full-width band mid-table, above a run of rows.

    Inheriting the page's External Party, a band satisfies both required
    fields off its single cell — nine phantom rows on SR 789, one per page.
    A band contributes one of its own fields; a conflict row contributes
    several.
    """
    banded = [
        FDOT_ROWS[0],
        ["FROM C/L CONST GULF OF MEXICO DR.", "", "", ""],
        *FDOT_ROWS[1:],
    ]
    doc = make_document(
        session, project, tmp_path, banded,
        banner="UTILITY AGENCY OWNER: AT&T TCA",
    )

    candidates = extract_document(
        session, doc,
        client=StubClient([structure(columns=FDOT_COLUMNS, owner="AT&T TCA")]),
    )

    ids = [c.payload_json["fields"]["utility_id"] for c in candidates]
    assert ids == ["1", "2"]


def test_a_mapping_without_the_required_fields_yields_nothing(
    session, project, tmp_path
):
    """Guards a legend table the model mistook for the matrix."""
    doc = make_document(session, project, tmp_path, TXDOT_ROWS)
    columns = [{"index": i, "canonical_field": None} for i in range(6)]

    assert extract_document(
        session, doc, client=StubClient([structure(columns=columns)])
    ) == []


# ---------------------------------------------------------- what the model sees


def test_the_model_is_shown_the_page_image_and_the_cells_geometry_read(
    session, project, tmp_path
):
    """The image gives context; the numbered cells give an exact join key,
    so a mapping never depends on the model counting columns."""
    doc = make_document(session, project, tmp_path, TXDOT_ROWS)
    client = StubClient([structure()])

    extract_document(session, doc, client=client)

    call = client.calls[0]
    assert len(call["images"]) == 1
    assert call["images"][0].endswith(".png")
    assert "Page 1 of" in call["user"]
    assert "Utility ID" in call["user"] and "[0]" in call["user"]


def test_the_model_is_never_asked_for_a_value(session, project, tmp_path):
    """The schema has no place to put one. That is the design."""
    doc = make_document(session, project, tmp_path, TXDOT_ROWS)
    client = StubClient([structure()])

    extract_document(session, doc, client=client)

    properties = client.calls[0]["schema"]["properties"]
    assert set(properties) == {
        "is_utility_matrix", "page_attributes", "matrix_table",
        "header_row", "columns",
    }
    assert "rows" not in properties


# -------------------------------------------------------- tier selection


def test_a_page_without_a_text_layer_takes_the_transcription_tier(
    session, project, tmp_path
):
    """No word boxes means no values to read; the model has to write them."""
    doc = make_document(session, project, tmp_path, TXDOT_ROWS)
    page = session.scalars(select(DocPage).where(DocPage.document_id == doc.id)).one()
    page.text_source = "ocr"
    session.flush()

    transcribed = {
        "is_utility_matrix": True,
        "page_attributes": {"external_org": None},
        "rows": [
            {
                "utility_id": "FOC1-133",
                "external_org": "AT&T Texas (SWBT)",
                "station_from": "1092+92",
                "quote": "FOC1-133 AT&T Texas (SWBT)",
                "confidence": 0.9,
            }
        ],
    }

    candidates = extract_document(
        session, doc, client=StubClient([transcribed])
    )

    assert len(candidates) == 1
    assert candidates[0].payload_json["tier"] == TIER_TRANSCRIBE
    assert candidates[0].payload_json["fields"]["station_from"] == "1092+92"


def test_candidates_are_added_to_the_session(session, project, tmp_path):
    doc = make_document(session, project, tmp_path, TXDOT_ROWS)

    extract_document(session, doc, client=StubClient([structure()]))

    stored = session.scalars(
        select(Candidate).where(Candidate.source_document_id == doc.id)
    ).all()
    assert len(stored) == 2
