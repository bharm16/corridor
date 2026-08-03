"""Vision extraction of a matrix page.

Shape and wiring only. Quality is measured by a real run against real
documents (#68), never by a unit test — a stub client returns whatever it
was handed, so asserting on it would only prove the fixture.

CI never calls a model.
"""

import pytest
from sqlalchemy import select

from corridor.db import Session, engine
from corridor.extract import NoMatrixFound
from corridor.extract_matrix import PROMPT_VERSION, extract_document
from corridor.models import Candidate, DocPage, Document, Project

PAGE_TEXT = (
    "NHHIP Segment 3C-2 Utility Conflict Matrix\n"
    "Utility ID Utility Owner Utility Type Material OH/ UG Baseline "
    "Alignment Location Start Location End Start Station End Station\n"
    "FOC1-133 AT&T Texas (SWBT) Telecom FOC UG IH 10 Providence Street "
    "Hardy Street West of Elysian 1092+92 1093+74\n"
    "FOC1-134 AT&T Texas (SWBT) Telecom FOC UG IH 10 Rothwell Street "
    "Hardy Street Semmes Street 1093+25 1102+04\n"
)


class StubClient:
    """Recorded responses. CI never calls a model."""

    def __init__(self, responses, model="gpt-5.6-luna"):
        self.responses = list(responses)
        self.model = model
        self.max_workers = 2
        self.calls = []

    def complete(self, *, system, user, schema, images=()):
        self.calls.append(
            {"system": system, "user": user, "schema": schema, "images": list(images)}
        )
        return self.responses.pop(0) if self.responses else page()


def row(**over):
    base = {
        "utility_id": "FOC1-133",
        "external_org": "AT&T Texas (SWBT)",
        "utility_type": "Telecom",
        "size": None,
        "material": "FOC",
        "oh_ug": "UG",
        "baseline": "IH 10",
        "orientation": None,
        "alignment": "Providence Street",
        "location_start": "Hardy Street",
        "location_end": "West of Elysian",
        "station_from": "1092+92",
        "station_to": "1093+74",
        "offset_from": None,
        "offset_to": None,
        "offset_side": None,
        "potential_conflict": None,
        "sue_level": None,
        "external_org_contact": None,
        "committed_date": None,
        "notes": None,
        "quote": "FOC1-133 AT&T Texas (SWBT) Telecom FOC UG IH 10",
        "confidence": 0.92,
    }
    base.update(over)
    return base


def page(rows=None, *, is_matrix=True, attributes=None):
    return {
        "is_utility_matrix": is_matrix,
        "page_attributes": attributes or {"external_org": None},
        "rows": [row()] if rows is None else rows,
    }


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
def document(session, tmp_path):
    project = Project(slug="mx-test", name="Matrix Test", is_synthetic=True)
    session.add(project)
    session.flush()
    doc = Document(
        project_id=project.id,
        sha256="a" * 64,
        filename="nhhip-seg3c2-utilities-inventory-2-13-2026.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=2,
    )
    session.add(doc)
    session.flush()
    for page_no in (1, 2):
        image = tmp_path / f"{page_no:04d}.png"
        image.write_bytes(b"\x89PNG page image")
        session.add(
            DocPage(
                document_id=doc.id,
                page_no=page_no,
                text=PAGE_TEXT,
                image_path=str(image),
            )
        )
    session.flush()
    return doc


def test_a_matrix_page_produces_candidates_with_a_model_and_a_confidence(
    session, document
):
    """Both were absent under the deterministic parser: `None` and 1.0."""
    client = StubClient([page(), page(rows=[])])

    candidates = extract_document(session, document, client=client)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.model == "gpt-5.6-luna"
    assert candidate.confidence == 0.92
    assert candidate.prompt_version == PROMPT_VERSION
    assert candidate.payload_json["fields"]["utility_id"] == "FOC1-133"
    assert candidate.citations_verified is True


def test_the_page_number_comes_from_our_code(session, document):
    """A hallucinated page must be impossible, not merely unlikely.

    The model is never asked for a page number and never given the chance
    to supply one, so nothing it returns can change where a citation
    points.
    """
    client = StubClient([page(), page()])

    candidates = extract_document(session, document, client=client)

    assert sorted(c.payload_json["citations"][0]["page"] for c in candidates) == [1, 2]
    assert sorted(c.source_pages[0] for c in candidates) == [1, 2]


def test_the_page_image_and_the_stored_page_text_are_both_sent(session, document):
    """The model reads the image; verification reads the text layer."""
    client = StubClient([page(), page()])

    extract_document(session, document, client=client)

    call = client.calls[0]
    assert len(call["images"]) == 1
    assert str(call["images"][0]).endswith("0001.png")
    assert "Page 1 of" in call["user"]


def test_a_fabricated_quote_is_kept_and_marked_unverified(session, document):
    client = StubClient(
        [page(rows=[row(quote="FOC9-999 Comcast of Houston Gas 2414+50")]), page(rows=[])]
    )

    candidates = extract_document(session, document, client=client)

    assert len(candidates) == 1
    assert candidates[0].citations_verified is False
    assert candidates[0].payload_json["citations"][0]["verified"] is False


def test_a_field_value_absent_from_the_page_is_kept_and_marked_unverified(
    session, document
):
    """The defect this whole extractor exists to make impossible.

    Citation verification is row-level and fuzzy, so a transcribed
    `1140+00` where the document says `1092+92` sits inside a valid row
    quote and passes. The queue sorts verified Candidates to the top, so
    without a field check that wrong value reaches a reviewer wearing a
    green check.
    """
    client = StubClient([page(rows=[row(station_from="1140+00")]), page(rows=[])])

    candidates = extract_document(session, document, client=client)

    candidate = candidates[0]
    assert candidate.citations_verified is False
    # The quote itself is fine — only the field is wrong, and the payload
    # has to say which one.
    assert candidate.payload_json["citations"][0]["verified"] is True
    assert candidate.payload_json["unverified_fields"] == ["station_from"]
    assert candidate.payload_json["fields"]["station_from"] == "1140+00"


def test_a_page_with_no_rows_yields_nothing_and_does_not_raise(session, document):
    """A matrix with no conflicts is a correct answer."""
    client = StubClient([page(rows=[]), page(rows=[])])

    assert extract_document(session, document, client=client) == []


def test_a_document_with_no_matrix_page_raises(session, document):
    """`NoMatrixFound` keeps its meaning: unreadable is not the same as empty."""
    client = StubClient([page(rows=[], is_matrix=False)] * 2)

    with pytest.raises(NoMatrixFound):
        extract_document(session, document, client=client)


def test_a_document_with_no_page_images_raises(session, document):
    """Nothing to read is unreadable, not a project with no conflicts."""
    for page_row in session.scalars(
        select(DocPage).where(DocPage.document_id == document.id)
    ):
        page_row.image_path = None
    session.flush()

    with pytest.raises(NoMatrixFound, match="page image"):
        extract_document(session, document, client=StubClient([]))


def test_a_row_with_no_quote_is_not_a_candidate(session, document):
    """A citation with no quote asserts nothing and cannot be checked."""
    client = StubClient([page(rows=[row(quote="")]), page(rows=[])])

    assert extract_document(session, document, client=client) == []


def test_empty_cells_do_not_become_fields(session, document):
    client = StubClient([page(), page(rows=[])])

    fields = extract_document(session, document, client=client)[0].payload_json["fields"]

    assert "size" not in fields and "notes" not in fields
    assert fields["material"] == "FOC"


def test_every_page_of_the_document_is_read(session, document):
    client = StubClient([page(), page()])

    extract_document(session, document, client=client)

    assert len(client.calls) == 2


# --------------------------------------------------- page-scoped attributes


PAGE_SCOPED_TEXT = (
    "Project # 148800011  Description: SR 789 Gulf of Mexico Dr @ Broadway RAB\n"
    "Phase #: IV  Plans Date: 11/20/2025\n"
    "UTILITY AGENCY OWNER: Comcast\n"
    "Conflict # Station Begin Station End Offset Facility Description\n"
    "1 203+40.00 206+40.00 30.00' RT. BTV, Size UNK Prop. Storm Pipe (Possible)\n"
    "2 206+93.00 206+93.00 54.00' RT. BTV Pedestal Pedestal\n"
)


def scoped_row(**over):
    """An FDOT row: no owner column at all — the page states it once."""
    base = {name: None for name in row()}
    base.update(
        utility_id="1",
        station_from="203+40.00",
        station_to="206+40.00",
        offset_from="30.00'",
        offset_side="RT.",
        utility_type="BTV, Size UNK",
        quote="1 203+40.00 206+40.00 30.00' RT. BTV, Size UNK",
        confidence=0.95,
    )
    base.update(over)
    return base


@pytest.fixture
def scoped_document(session, tmp_path):
    project = Project(slug="fdot-test", name="Page-Scoped Test", is_synthetic=True)
    session.add(project)
    session.flush()
    doc = Document(
        project_id=project.id,
        sha256="b" * 64,
        filename="45373015201-utility-conflict-matrix.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(doc)
    session.flush()
    image = tmp_path / "0001.png"
    image.write_bytes(b"\x89PNG page image")
    session.add(
        DocPage(
            document_id=doc.id,
            page_no=1,
            text=PAGE_SCOPED_TEXT,
            image_path=str(image),
        )
    )
    session.flush()
    return doc


def test_every_row_on_a_page_inherits_that_pages_attributes(
    session, scoped_document
):
    """FDOT names the External Party once in the page header.

    Strictly better than asking the model to repeat the owner on 29 rows:
    the owner becomes one string verified once against the page, with no
    per-row transcription surface at all.
    """
    client = StubClient(
        [
            page(
                rows=[scoped_row(), scoped_row(utility_id="2")],
                attributes={"external_org": "Comcast"},
            )
        ]
    )

    candidates = extract_document(session, scoped_document, client=client)

    assert len(candidates) == 2
    assert all(
        c.payload_json["fields"]["external_org"] == "Comcast" for c in candidates
    )
    assert all(c.citations_verified for c in candidates)


def test_a_row_that_states_its_own_party_is_unaffected(session, document):
    """TxDOT repeats the owner on every row. Both are ordinary."""
    client = StubClient(
        [
            page(attributes={"external_org": "Comcast"}),
            page(rows=[]),
        ]
    )

    candidates = extract_document(session, document, client=client)

    assert candidates[0].payload_json["fields"]["external_org"] == "AT&T Texas (SWBT)"


def test_a_page_attribute_absent_from_the_page_marks_its_rows_unverified(
    session, scoped_document
):
    """One bad inherited value is 29 suspect rows, and it has to look like it."""
    client = StubClient(
        [
            page(
                rows=[scoped_row(), scoped_row(utility_id="2")],
                attributes={"external_org": "Verizon Florida"},
            )
        ]
    )

    candidates = extract_document(session, scoped_document, client=client)

    assert len(candidates) == 2
    assert not any(c.citations_verified for c in candidates)
    assert all(
        c.payload_json["unverified_fields"] == ["external_org"] for c in candidates
    )


def test_a_page_with_no_attributes_inherits_nothing(session, scoped_document):
    client = StubClient([page(rows=[scoped_row()])])

    fields = extract_document(session, scoped_document, client=client)[0].payload_json[
        "fields"
    ]

    assert "external_org" not in fields


def test_candidates_are_added_to_the_session(session, document):
    extract_document(session, document, client=StubClient([page(), page(rows=[])]))

    stored = session.scalars(
        select(Candidate).where(Candidate.source_document_id == document.id)
    ).all()
    assert len(stored) == 1
