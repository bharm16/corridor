import pytest
from sqlalchemy import select

from corridor.db import Session, engine
from corridor.extract_agreement import PROMPT_VERSION, extract_document
from corridor.models import Candidate, Dependency, DocPage, Document, Project

from corridor.llm import RequestConfiguration
from model_client_support import FakeModelClient

PAGE_TEXT = (
    "SECTION 4.2 UTILITY RELOCATION. The City shall relocate all water and "
    "sanitary sewer facilities in conflict with the highway improvements at "
    "its sole cost, and shall provide the State thirty (30) days written "
    "notice prior to commencing such relocation work. Relocation shall be "
    "complete no later than June 3, 2026. Upon completion the City shall "
    "furnish the State a written certification of clearance."
)


def stub_client(responses, model="gpt-5.6-luna"):
    """The shared recording double, answering these responses in order."""
    return FakeModelClient(
        [*responses, *([{"obligations": []}] * 8)],
        configuration=RequestConfiguration(model=model),
    )


def obligation(**over):
    base = {
        "title": "City relocates water and sewer in conflict",
        "external_org": "City of Houston",
        "obligation": "Relocate conflicting water and sanitary sewer facilities at its own cost",
        "notice_period": "30 days written notice",
        "committed_date": "2026-06-03",
        "evidence_required": "written certification of clearance",
        "quote": "The City shall relocate all water and sanitary sewer facilities",
        "confidence": 0.9,
    }
    base.update(over)
    return base


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
def document(session):
    project = Project(slug="agr-test", name="Agreement Test", is_synthetic=True)
    session.add(project)
    session.flush()
    doc = Document(
        project_id=project.id,
        sha256="e" * 64,
        filename="city-of-houston-municipal-maintenance-agreement-1-3-1969.pdf",
        doc_type="agreement",
        parse_status="parsed",
        pages=2,
    )
    session.add(doc)
    session.flush()
    session.add(DocPage(document_id=doc.id, page_no=1, text=PAGE_TEXT, text_source="ocr"))
    session.add(DocPage(document_id=doc.id, page_no=2, text="- 2 -", text_source="ocr"))
    session.flush()
    return doc


def test_an_obligation_becomes_a_verified_candidate(session, document):
    client = stub_client([{"obligations": [obligation()]}])
    [candidate] = extract_document(session, document, client=client)

    assert candidate.kind == "dependency"
    assert candidate.citations_verified is True
    assert candidate.payload_json["fields"]["external_org"] == "City of Houston"
    assert candidate.payload_json["fields"]["notice_period"] == "30 days written notice"


def test_the_page_number_comes_from_us_not_the_model(session, document):
    """A citation cannot point at a page the model invented.

    The model is never asked for a page number, so the only thing it can get
    wrong in a citation is the quote — which is then mechanically checked.
    """
    client = stub_client([{"obligations": [obligation()]}])
    [candidate] = extract_document(session, document, client=client)

    citation = candidate.payload_json["citations"][0]
    assert citation["page"] == 1
    assert candidate.source_pages == [1]
    # Nothing in the schema lets the model supply a page.
    assert "page" not in obligation()


def test_a_fabricated_quote_is_kept_but_marked_unverified(session, document):
    """Never dropped. A hallucinated quote is a signal about the extractor."""
    client = stub_client(
        [{"obligations": [obligation(quote="The City shall pay liquidated damages")]}]
    )
    [candidate] = extract_document(session, document, client=client)

    assert candidate.citations_verified is False
    assert candidate.payload_json["citations"][0]["verified"] is False


def test_a_short_page_is_never_sent_to_the_model(session, document):
    """Page 2 is '- 2 -'. Spending tokens to be told 'no obligations' is waste."""
    client = stub_client([{"obligations": []}, {"obligations": []}])
    extract_document(session, document, client=client)
    assert len(client.calls) == 1
    assert "Page 1" in client.calls[0].user


def test_the_page_text_sent_is_the_stored_text(session, document):
    """Verification must run against exactly what the model was shown."""
    client = stub_client([{"obligations": []}])
    extract_document(session, document, client=client)
    assert PAGE_TEXT in client.calls[0].user


def test_nulls_are_omitted_rather_than_stored_as_empty(session, document):
    client = stub_client(
        [{"obligations": [obligation(committed_date=None, notice_period=None)]}]
    )
    [candidate] = extract_document(session, document, client=client)
    fields = candidate.payload_json["fields"]
    assert "committed_date" not in fields
    assert "notice_period" not in fields
    assert fields["title"]


def test_an_obligation_without_a_quote_is_dropped(session, document):
    """No quote means no evidence, and an uncited assertion cannot enter."""
    client = stub_client([{"obligations": [obligation(quote="   ")]}])
    assert extract_document(session, document, client=client) == []


def test_an_obligation_without_a_title_is_dropped(session, document):
    client = stub_client([{"obligations": [obligation(title="")]}])
    assert extract_document(session, document, client=client) == []


def test_prompt_version_and_model_are_recorded(session, document):
    client = stub_client([{"obligations": [obligation()]}], model="gpt-5.6-luna")
    [candidate] = extract_document(session, document, client=client)
    assert candidate.prompt_version == PROMPT_VERSION
    assert candidate.model == "gpt-5.6-luna"


def test_the_ocr_provenance_of_the_page_is_carried(session, document):
    client = stub_client([{"obligations": [obligation()]}])
    [candidate] = extract_document(session, document, client=client)
    assert candidate.payload_json["text_source"] == "ocr"


def test_the_extractor_writes_no_dependencies(session, document):
    """Extractors produce Candidates only. The path in is a human keystroke."""
    client = stub_client([{"obligations": [obligation()]}])
    extract_document(session, document, client=client)
    assert not session.scalars(
        select(Dependency).where(Dependency.project_id == document.project_id)
    ).all()
    assert session.scalars(
        select(Candidate).where(Candidate.project_id == document.project_id)
    ).all()


def test_an_empty_model_response_is_not_a_crash(session, document):
    """A refusal or a length stop returns no content mid-run."""
    client = stub_client([{}])
    assert extract_document(session, document, client=client) == []
