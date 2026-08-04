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

import json
import re
from pathlib import Path

import pymupdf
import pytest
from sqlalchemy import select

from corridor.geometry import NoMatrixFound
from corridor.db import Session, engine
from corridor.extract_matrix import (
    DECLINED_COLUMNS,
    LOCAL_FIELDS,
    PROMPT_VERSION,
    ROW_FIELDS,
    STRUCTURE_PROMPT,
    TEMPLATE_FIELDS,
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


def structure(*, matrix_table=0, header_row=0, columns=None, owner=None,
              is_matrix=True, confidence=0.97):
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
        "mapping_confidence": confidence,
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


def make_multipage_document(session, project, tmp_path, pages_rows, *, sha="m"):
    """A Document of N real pages, each with its own ruled table.

    #101 needs several pages of one document to disagree with each other,
    which `make_document` cannot express — it writes a single page.
    """
    merged = pymupdf.open()
    for index, rows in enumerate(pages_rows):
        one = write_pdf(tmp_path / f"{sha}-p{index}.pdf", rows)
        with pymupdf.open(one) as opened:
            merged.insert_pdf(opened)
    path = tmp_path / f"{sha}-all.pdf"
    merged.save(path)
    merged.close()

    doc = Document(
        project_id=project.id,
        sha256=sha * 64,
        filename=f"{sha}-matrix.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=len(pages_rows),
    )
    session.add(doc)
    session.flush()
    with pymupdf.open(path) as opened:
        for index in range(len(pages_rows)):
            image = tmp_path / f"{sha}-{index:04d}.png"
            image.write_bytes(b"\x89PNG page image")
            session.add(
                DocPage(
                    document_id=doc.id,
                    page_no=index + 1,
                    text=opened[index].get_text(),
                    image_path=str(image),
                    text_source="text_layer",
                )
            )
    session.flush()
    doc._pdf_path = str(path)
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
        "header_row", "columns", "mapping_confidence",
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


def test_a_continuation_page_reuses_the_header_printed_earlier(
    session, project, tmp_path
):
    """A matrix runs on for pages; its headings are printed once.

    Not academic: on the last page of Project A's oldest revision every
    owner cell reads `NA`, which the model reasonably took for a size —
    mapping the owner column to the wrong field and dropping all 44 rows.
    A printed header outranks a mapping inferred from data.
    """
    write_pdf(tmp_path / "b.pdf", [["1", "AT&T TCA", "Telecom", "203+40.00", "206+40.00", "9"]])
    doc = make_document(session, project, tmp_path, TXDOT_ROWS, pages=2)
    with pymupdf.open(tmp_path / "a.pdf") as first, pymupdf.open(tmp_path / "b.pdf") as second:
        merged = pymupdf.open()
        merged.insert_pdf(first)
        merged.insert_pdf(second)
        merged.save(tmp_path / "merged.pdf")
        text = second[0].get_text()
    doc._pdf_path = str(tmp_path / "merged.pdf")
    image = tmp_path / "a-0002.png"
    image.write_bytes(b"\x89PNG page image")
    session.add(
        DocPage(document_id=doc.id, page_no=2, text=text,
                image_path=str(image), text_source="text_layer")
    )
    session.flush()

    # Page 2 has no header, and the model mis-infers the owner column.
    misread = structure(
        header_row=None,
        columns=[
            {"index": 0, "canonical_field": "utility_id"},
            {"index": 1, "canonical_field": "size"},
            {"index": 2, "canonical_field": "utility_type"},
            {"index": 3, "canonical_field": "station_from"},
            {"index": 4, "canonical_field": "station_to"},
            {"index": 5, "canonical_field": None},
        ],
    )

    candidates = extract_document(
        session, doc, client=StubClient([structure(), misread])
    )

    page_two = [c for c in candidates if c.source_pages == [2]]
    assert len(page_two) == 1
    assert page_two[0].payload_json["fields"]["external_org"] == "AT&T TCA"


def transcribed(rows=None, *, owner=None, is_matrix=True, meta=None):
    if rows is None:
        rows = [
            {
                "utility_id": "FOC1-133",
                "external_org": "AT&T Texas (SWBT)",
                "station_from": "1092+92",
                "quote": "FOC1-133 AT&T Texas (SWBT)",
                "confidence": 0.9,
            }
        ]
    result = {
        "is_utility_matrix": is_matrix,
        "page_attributes": {"external_org": owner},
        "rows": rows,
    }
    if meta:
        result["_meta"] = meta
    return result


def blind(session, document, page_no=1):
    """Strip a page's text layer so it has no word boxes to read."""
    page = session.scalars(
        select(DocPage).where(
            DocPage.document_id == document.id, DocPage.page_no == page_no
        )
    ).one()
    page.text_source = "ocr"
    session.flush()
    return page


def test_a_transcribed_value_absent_from_the_ocr_text_is_kept_and_flagged(
    session, project, tmp_path
):
    """The tier where the model still writes values is the tier that needs
    the field check most."""
    doc = make_document(session, project, tmp_path, TXDOT_ROWS)
    blind(session, doc)

    candidates = extract_document(
        session, doc,
        client=StubClient([transcribed([{
            "utility_id": "FOC1-133",
            "external_org": "AT&T Texas (SWBT)",
            "station_from": "1140+00",
            "quote": "FOC1-133 AT&T Texas (SWBT)",
            "confidence": 0.99,
        }])]),
    )

    assert len(candidates) == 1
    assert candidates[0].citations_verified is False
    assert candidates[0].payload_json["unverified_fields"] == ["station_from"]
    assert candidates[0].payload_json["fields"]["station_from"] == "1140+00"


def test_a_low_logprob_numeric_token_sinks_its_row(session, project, tmp_path):
    """The gate proved self-reported confidence is blind to misreads: 96 of
    164 failures sat at 0.98. Token probabilities are the measured
    replacement, and they cost nothing to ask for."""
    doc = make_document(session, project, tmp_path, TXDOT_ROWS)
    blind(session, doc)
    meta = {
        "logprobs": [
            {"token": "FOC1", "logprob": -0.001},
            {"token": "1092", "logprob": -4.2},
            {"token": "+92", "logprob": -0.002},
        ]
    }

    candidates = extract_document(
        session, doc, client=StubClient([transcribed(meta=meta)])
    )

    assert candidates[0].citations_verified is False
    assert candidates[0].payload_json["low_confidence_tokens"] == ["1092"]


def test_a_confident_numeric_token_does_not_sink_its_row(
    session, project, tmp_path
):
    doc = make_document(session, project, tmp_path, TXDOT_ROWS)
    blind(session, doc)
    meta = {"logprobs": [{"token": "1092", "logprob": -0.001}]}

    candidates = extract_document(
        session, doc, client=StubClient([transcribed(meta=meta)])
    )

    assert candidates[0].citations_verified is True
    assert candidates[0].payload_json["low_confidence_tokens"] == []


def test_a_low_logprob_word_token_is_not_flagged(session, project, tmp_path):
    """Prose wanders; digits do not. Only numeric tokens are load-bearing."""
    doc = make_document(session, project, tmp_path, TXDOT_ROWS)
    blind(session, doc)
    meta = {"logprobs": [{"token": " Telecom", "logprob": -5.0}]}

    candidates = extract_document(
        session, doc, client=StubClient([transcribed(meta=meta)])
    )

    assert candidates[0].payload_json["low_confidence_tokens"] == []
    assert candidates[0].citations_verified is True


def test_logprobs_are_requested_only_for_the_transcription_tier(
    session, project, tmp_path
):
    doc = make_document(session, project, tmp_path, TXDOT_ROWS)
    client = StubClient([structure()])
    extract_document(session, doc, client=client)
    assert client.calls[0]["logprobs"] is False

    blind(session, doc)
    client = StubClient([transcribed()])
    extract_document(session, doc, client=client)
    assert client.calls[0]["logprobs"] is True


def test_the_reserved_meta_key_never_reaches_a_candidate(
    session, project, tmp_path
):
    doc = make_document(session, project, tmp_path, TXDOT_ROWS)
    blind(session, doc)

    candidate = extract_document(
        session, doc,
        client=StubClient([transcribed(meta={"logprobs": []})]),
    )[0]

    assert "_meta" not in candidate.payload_json
    assert "_meta" not in candidate.payload_json["fields"]


def test_a_mixed_document_produces_candidates_from_both_tiers(
    session, project, tmp_path
):
    """One document, one call per tier, both tiers' rows coming back.

    The two batches run separately and are the only place their results are
    stitched together; a single-tier fixture never exercises that.
    """
    write_pdf(tmp_path / "b.pdf", TXDOT_ROWS)
    doc = make_document(session, project, tmp_path, TXDOT_ROWS, pages=2)
    with pymupdf.open(tmp_path / "a.pdf") as first, pymupdf.open(tmp_path / "b.pdf") as second:
        merged = pymupdf.open()
        merged.insert_pdf(first)
        merged.insert_pdf(second)
        merged.save(tmp_path / "mixed.pdf")
        text = second[0].get_text()
    doc._pdf_path = str(tmp_path / "mixed.pdf")
    image = tmp_path / "a-0002.png"
    image.write_bytes(b"\x89PNG page image")
    # Page 2 was scanned: no text layer, so no word boxes to read.
    session.add(
        DocPage(document_id=doc.id, page_no=2, text=text,
                image_path=str(image), text_source="ocr")
    )
    session.flush()

    candidates = extract_document(
        session, doc, client=StubClient([structure(), transcribed()])
    )

    tiers = {c.source_pages[0]: c.payload_json["tier"] for c in candidates}
    assert tiers == {1: TIER_STRUCTURE, 2: TIER_TRANSCRIBE}
    assert doc.extraction_tiers == {TIER_STRUCTURE: 1, TIER_TRANSCRIBE: 1}


def test_every_page_failing_is_not_reported_as_an_unhandled_layout(
    session, project, tmp_path
):
    """An outage says nothing about the document.

    Raising `NoMatrixFound` here would tell a reader the layout is
    unhandled when nothing was ever read — the same conflation, one level
    up, that keeps unreadable separate from empty.
    """
    doc = make_document(session, project, tmp_path, TXDOT_ROWS)

    with pytest.raises(RuntimeError) as raised:
        extract_document(
            session, doc, client=StubClient([{"_error": "503 upstream"}])
        )

    assert not isinstance(raised.value, NoMatrixFound)
    assert "failed" in str(raised.value)


def test_a_document_reports_how_many_pages_fell_back(session, project, tmp_path):
    """How often we fall back is a number to watch, not a surprise."""
    doc = make_document(session, project, tmp_path, TXDOT_ROWS)
    blind(session, doc)

    extract_document(session, doc, client=StubClient([transcribed()]))

    assert doc.extraction_tiers == {TIER_TRANSCRIBE: 1}


def test_a_fully_readable_document_reports_no_fallback(
    session, project, tmp_path
):
    doc = make_document(session, project, tmp_path, TXDOT_ROWS)

    extract_document(session, doc, client=StubClient([structure()]))

    assert doc.extraction_tiers == {TIER_STRUCTURE: 1}


def test_candidates_are_added_to_the_session(session, project, tmp_path):
    doc = make_document(session, project, tmp_path, TXDOT_ROWS)

    extract_document(session, doc, client=StubClient([structure()]))

    stored = session.scalars(
        select(Candidate).where(Candidate.source_document_id == doc.id)
    ).all()
    assert len(stored) == 2


# ------------------------------------------------------------- real corpus
#
# Skipped on a clean clone, following the marker the extract tests use.
# These assert against the documents the gate measured, so a change that
# silently stops reading SR 789 fails here rather than in a run.

FDOT_LOCK = Path("corpus/fdot-sr789.lock.json")
real_corpus = pytest.mark.skipif(
    not FDOT_LOCK.exists(), reason="corpus not fetched; run `make corpus`"
)


def _sr789_path():
    """The store is content-addressed, so match on the source URL, not the
    stored filename — which is a sha256."""
    lock = json.loads(FDOT_LOCK.read_text())
    for url, record in lock["sources"].items():
        if "utility-conflict-matrix" in url and record.get("local_path"):
            return record["local_path"]
    return None


@real_corpus
def test_sr789_pages_state_their_external_party_in_the_text_layer():
    """The fact page-scoped attributes exist for.

    Nine pages, nine utilities, each named once in the page header and
    never in a column — which is why the deterministic parser reads none
    of them, and why the owner has to be verifiable against the page text
    rather than transcribed per row.
    """
    path = _sr789_path()
    if path is None:
        pytest.skip("SR 789 not in the lockfile")

    owners = []
    with pymupdf.open(path) as pdf:
        for page in pdf:
            match = re.search(r"UTILITY AGENCY OWNER:\s*(.+)", page.get_text())
            owners.append(match.group(1).strip() if match else None)

    assert None not in owners
    assert len(set(owners)) == 9
    assert "Comcast" in owners


@real_corpus
def test_sr789_geometry_reads_the_table_a_synonym_table_could_not_name():
    """Tier 1's division of labour, on the document that motivated it.

    `find_tables()` locates the table and the word boxes read it cleanly —
    every value Tier 1 stores is already here. What geometry cannot do is
    say what the columns *mean*, and none of these printed headings match
    a TxDOT synonym, which is why the deterministic parser read zero rows
    from this document and why #63 deleted it rather than teaching it more
    synonyms.
    """
    from corridor.geometry import page_tables

    path = _sr789_path()
    if path is None:
        pytest.skip("SR 789 not in the lockfile")

    with pymupdf.open(path) as pdf:
        grids = page_tables(pdf[1])

    assert grids, "geometry found no table"
    grid = grids[0]
    header = [(cell or "").strip() for cell in grid[1]]
    assert "Conflict #" in header
    # Not one of these is a TxDOT heading, and every value is here anyway.
    assert "Station Begin (From C/L Const)" in header
    assert any("203+40.00" in (cell or "") for row in grid for cell in row)


# ------------------------------------ the canonical vocabulary (#97)


def test_every_canonical_field_is_traceable():
    """ADR-0009: the vocabulary answers to a published template.

    `ROW_FIELDS` was derived from Project A, whose document is a Utility
    Inventory rather than a Utility Conflict Matrix. Every field now has
    to say where it comes from — a column of TxDOT's published template,
    or a documented local exception. A field belonging to neither is a
    field nobody can defend.
    """
    assert set(ROW_FIELDS) == set(TEMPLATE_FIELDS) | set(LOCAL_FIELDS)
    assert not (set(TEMPLATE_FIELDS) & set(LOCAL_FIELDS))
    assert all(column.strip() for column in TEMPLATE_FIELDS.values())
    assert all(len(reason) > 30 for reason in LOCAL_FIELDS.values())


def test_the_standard_columns_sh99_dropped_now_have_homes():
    """SH 99 prints TxDOT's template verbatim and lost six fields to it.

    `Utility Subtype`'s values on SH 99 include `Highly Volatile Liquid`
    and `Crude Oil` beside `Sanitary Sewer`. Dropping that is not a
    cosmetic gap.
    """
    for column in (
        "Utility Subtype",
        "Utility Function",
        "Placement Relative to Existing ROW",
        "Operational Status",
        "Utility Conflict Description",
        "Resolution Strategy Selected",
    ):
        assert column in TEMPLATE_FIELDS.values(), column


def test_quality_levels_have_one_home_whatever_the_column_is_headed():
    """`Utility Investigation Completed` is a template field and is refused.

    The only column in this corpus carrying that heading is SH 99's, and
    its cells read `QLB`/`QLC`/`QLD` — quality levels. Offering both fields
    split the same data by project: 1,937 of Project A's rows under
    `sue_level` and 460 of SH 99's under the other, so a corpus-wide read
    of either silently missed a project. That is ADR-0009's divergence
    reproduced in a new field, and one home is the fix.
    """
    assert "Utility Investigation Completed" not in TEMPLATE_FIELDS.values()
    assert TEMPLATE_FIELDS["sue_level"] == "Utility Investigation Quality Level"
    assert "investigation_completed" not in ROW_FIELDS


def test_a_column_the_template_does_not_define_is_declined_with_a_reason():
    """Declining is a decision, so it carries an argument.

    `Early TxDOT Utility Activity` and `AURL or DBA` are real SH 99
    columns and deliberately have no canonical home: they say who
    relocates and when, not what the facility is or what happens to it.
    """
    assert DECLINED_COLUMNS
    for column, reason in DECLINED_COLUMNS.items():
        assert column.strip()
        assert len(reason) > 30, column
    for column in ("Early TxDOT Utility Activity", "AURL or DBA"):
        assert column in DECLINED_COLUMNS


def test_the_prompt_names_every_declined_column():
    """A decision the model never sees is not a decision.

    Declining a column in code alone leaves the model free to map it, and
    a Y/N checkbox looks exactly like a flag: `VVH (Y/N)` on SR 789 and
    `Verified (Y/N)` on Project A both landed in `potential_conflict` on
    some pages and nowhere on others. That is not the model being
    inconsistent — it is being asked a question with no stated answer, and
    it cost a stable one-mapping document three extra mappings.
    """
    prompt = STRUCTURE_PROMPT.read_text()
    for column in DECLINED_COLUMNS:
        assert column in prompt, column


def test_the_prompt_and_the_code_name_the_same_fields():
    """The model is told the vocabulary; the code enforces it.

    Drift between them is silent and one-directional: a field the prompt
    offers but `ROW_FIELDS` omits is mapped by the model and then thrown
    away by `_column_mapping` as unknown, so the column reads as unmapped
    for reasons no reviewer can see on the page.

    Scoped to the canonical-fields section rather than every bulleted line
    in the file: the declined-columns list is bullets too, and reading both
    would only work for as long as no declined column is a single word.
    """
    body = STRUCTURE_PROMPT.read_text().split("## The canonical fields", 1)[1]
    section = body.split("\n## ", 1)[0]
    named = {
        field
        for line in section.splitlines()
        if line.startswith("- `")
        for field in re.findall(r"`(\w+)`", line.split("—")[0])
    }

    assert named == set(ROW_FIELDS)


# ------------------------ one header, one mapping (#101, supersedes #91)


def test_pages_sharing_a_header_get_one_mapping(session, project, tmp_path):
    """The defect, measured on three documents before this landed.

    SH 99's `UCM_to_RIDs` reprints an identical 16-column header on all 17
    pages and got three different readings of it; WSDOT 9424 reprints its
    28-column header on all 11 and got three. The model is not being
    unreliable — it is being asked the same question eleven times and
    answering independently each time.
    """
    rows = [
        ["Utility ID", "Utility Owner", "Utility Type", "Start Station"],
        ["FOC1-1", "AT&T", "Telecom", "1149+00"],
        ["FOC1-2", "AT&T", "Telecom", "1150+00"],
    ]
    document = make_multipage_document(session, project, tmp_path, [rows, rows, rows])

    # Page 2 reads the owner column as a size — the wobble, verbatim.
    agreeing = {
        "is_utility_matrix": True,
        "page_attributes": {"external_org": None},
        "matrix_table": 0,
        "header_row": 0,
        "columns": [
            {"index": 0, "canonical_field": "utility_id"},
            {"index": 1, "canonical_field": "external_org"},
            {"index": 2, "canonical_field": "utility_type"},
            {"index": 3, "canonical_field": "station_from"},
        ],
        "mapping_confidence": 0.9,
    }
    disagreeing = {
        **agreeing,
        "columns": [
            {"index": 0, "canonical_field": "utility_id"},
            {"index": 1, "canonical_field": "size"},
            {"index": 2, "canonical_field": "utility_type"},
            {"index": 3, "canonical_field": "station_from"},
        ],
    }
    client = StubClient([agreeing, disagreeing, agreeing])

    candidates = extract_document(session, document, client=client)

    # The minority reading is overruled, so every page reads the owner
    # column as the owner and no page silently files it as a size.
    assert len(candidates) == 6
    assert all(
        c.payload_json["fields"].get("external_org") == "AT&T" for c in candidates
    )
    assert not any("size" in c.payload_json["fields"] for c in candidates)


def test_a_disagreement_between_pages_is_recorded_not_silently_resolved(
    session, project, tmp_path
):
    """Majority is a decision, and a decision a reviewer cannot see is a
    guess. Blast radius is the reason: one mapping now governs every page
    that shares its header, so 457 rows ride on it rather than 28."""
    rows = [
        ["Utility ID", "Utility Owner", "Utility Type", "Start Station"],
        ["FOC1-1", "AT&T", "Telecom", "1149+00"],
    ]
    document = make_multipage_document(session, project, tmp_path, [rows, rows, rows])

    base = {
        "is_utility_matrix": True,
        "page_attributes": {"external_org": None},
        "matrix_table": 0,
        "header_row": 0,
        "mapping_confidence": 0.9,
        "columns": [
            {"index": 0, "canonical_field": "utility_id"},
            {"index": 1, "canonical_field": "external_org"},
        ],
    }
    other = {**base, "columns": [
        {"index": 0, "canonical_field": "utility_id"},
        {"index": 1, "canonical_field": "size"},
    ]}

    extract_document(session, document, client=StubClient([base, other, base]))

    assert document.header_disagreements == 1


def test_a_header_read_the_same_way_everywhere_records_no_disagreement(
    session, project, tmp_path
):
    rows = [
        ["Utility ID", "Utility Owner"],
        ["FOC1-1", "AT&T"],
    ]
    document = make_multipage_document(session, project, tmp_path, [rows, rows], sha="n")
    answer = {
        "is_utility_matrix": True,
        "page_attributes": {"external_org": None},
        "matrix_table": 0,
        "header_row": 0,
        "mapping_confidence": 0.9,
        "columns": [
            {"index": 0, "canonical_field": "utility_id"},
            {"index": 1, "canonical_field": "external_org"},
        ],
    }

    extract_document(session, document, client=StubClient([answer, answer]))

    assert document.header_disagreements == 0


def test_two_genuinely_different_headers_keep_their_own_mappings(
    session, project, tmp_path
):
    """A mid-document layout change is not a disagreement to be voted on.

    Project A's oldest revision and SH 99's draft both print more than one
    form; folding them onto one mapping would file a column under the wrong
    heading for every row of the minority layout.
    """
    first = [["Utility ID", "Utility Owner"], ["FOC1-1", "AT&T"]]
    second = [["Conflict #", "Facility Description"], ["1", "BTV"]]
    document = make_multipage_document(session, project, tmp_path, [first, second], sha="o")

    a = {
        "is_utility_matrix": True,
        "page_attributes": {"external_org": None},
        "matrix_table": 0,
        "header_row": 0,
        "mapping_confidence": 0.9,
        "columns": [
            {"index": 0, "canonical_field": "utility_id"},
            {"index": 1, "canonical_field": "external_org"},
        ],
    }
    b = {
        **a,
        "page_attributes": {"external_org": "Comcast"},
        "columns": [
            {"index": 0, "canonical_field": "utility_id"},
            {"index": 1, "canonical_field": "utility_type"},
        ],
    }

    candidates = extract_document(session, document, client=StubClient([a, b]))

    owners = {c.payload_json["fields"].get("external_org") for c in candidates}
    assert owners == {"AT&T", "Comcast"}
    assert document.header_disagreements == 0
