"""Candidate statement facts are prepared once for every read adapter."""

from datetime import date

import pytest

from corridor.candidate_statement_facts import prepare_candidate_statement_facts
from corridor.db import Session, engine
from corridor.models import Candidate, DocPage, Document, ExternalOrg, Project


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    value = Session(bind=connection)
    yield value
    value.close()
    transaction.rollback()
    connection.close()


def _candidate_with_page(session, *, quote, page_text, fields):
    project = Project(
        slug="candidate-statement-facts-test",
        name="Candidate Statement Facts Test",
        is_synthetic=True,
    )
    party = ExternalOrg(name="Kinder Morgan", aliases=["KM"])
    session.add_all((project, party))
    session.flush()
    document = Document(
        project_id=project.id,
        sha256="a" * 64,
        filename="coordination-minutes.xlsx",
        doc_type="minutes",
        parse_status="parsed",
    )
    session.add(document)
    session.flush()
    session.add(
        DocPage(
            document_id=document.id,
            page_no=1,
            text=page_text,
            text_source="cells",
        )
    )
    candidate = Candidate(
        project_id=project.id,
        source_document_id=document.id,
        source_pages=[1],
        kind="event",
        state="pending",
        citations_verified=True,
        payload_json={
            "fields": fields,
            "citations": [
                {
                    "document_id": document.id,
                    "page": 1,
                    "quote": quote,
                    "verified": True,
                }
            ],
        },
    )
    session.add(candidate)
    session.flush()
    return party, document, candidate


def test_preparation_returns_only_evidence_supported_statement_facts(session):
    quote = "Kinder Morgan will complete the relocation in June 2026."
    party, document, candidate = _candidate_with_page(
        session,
        quote=quote,
        page_text=quote,
        fields={
            "event_type": "commitment",
            "description": quote,
            "external_org": "Kinder Morgan",
            "stated_party": "Kinder Morgan",
            "event_date": "2026-05-20",
            "committed_date": {
                "text": "June 2026",
                "precision": "month",
                "start_date": "2026-06-01",
                "end_date": "2026-06-30",
            },
        },
    )

    facts = prepare_candidate_statement_facts(session, candidate)

    assert facts.evidence_is_complete is True
    assert facts.evidence_is_reviewable is True
    assert facts.affected_party.wording == "Kinder Morgan"
    assert facts.affected_party.external_org_id == party.id
    assert facts.stated_party.wording == "Kinder Morgan"
    assert facts.stated_party.external_org_id == party.id
    assert facts.description == quote
    assert facts.description_is_supported is True
    assert facts.event_date == date(2026, 5, 20)
    assert facts.event_date_is_valid is True
    assert facts.new_timing.timing.text == "June 2026"
    assert facts.new_timing.timing.precision == "month"
    assert facts.new_timing.timing.start_date == date(2026, 6, 1)
    assert facts.new_timing.timing.end_date == date(2026, 6, 30)
    assert facts.new_timing.is_supported is True
    assert facts.cited_evidence[0].document_id == document.id
    assert facts.cited_evidence[0].page_no == 1


def test_preparation_distinguishes_visible_context_from_cited_support(session):
    quote = "The relocation schedule was discussed."
    _party, _document, candidate = _candidate_with_page(
        session,
        quote=quote,
        page_text=(
            "Kinder Morgan coordination minutes. "
            "Kinder Morgan will complete the relocation in June 2026."
        ),
        fields={
            "event_type": "commitment",
            "description": quote,
            "external_org": "Kinder Morgan",
            "stated_party": "Kinder Morgan",
            "committed_date": {
                "text": "June 2026",
                "precision": "month",
                "start_date": "2026-06-01",
                "end_date": "2026-06-30",
            },
        },
    )

    facts = prepare_candidate_statement_facts(session, candidate)

    assert facts.stated_party.is_supported is False
    assert facts.stated_party.is_visible is True
    assert facts.stated_party.external_org_id is None
    assert facts.stated_party.visible_external_org_id is not None
    assert facts.new_timing.is_supported is False
    assert facts.new_timing.is_visible is True


def test_preparation_refuses_boolean_citation_identities(session):
    quote = "Kinder Morgan will complete the relocation in June 2026."
    _party, _document, candidate = _candidate_with_page(
        session,
        quote=quote,
        page_text=quote,
        fields={
            "event_type": "commitment",
            "description": quote,
            "external_org": "Kinder Morgan",
            "stated_party": "Kinder Morgan",
        },
    )
    candidate.payload_json["citations"][0]["document_id"] = True

    facts = prepare_candidate_statement_facts(session, candidate)

    assert facts.evidence_is_complete is False
    assert facts.evidence == ()


def test_preparation_keeps_legacy_party_fallback_separate_from_visible_day_words(
    session,
):
    quote = "Kinder Morgan will complete the relocation by June 1, 2026."
    _party, _document, candidate = _candidate_with_page(
        session,
        quote=quote,
        page_text=quote,
        fields={
            "event_type": "commitment",
            "description": quote,
            "external_org": "Kinder Morgan",
            "committed_date": "2026-06-01",
        },
    )

    facts = prepare_candidate_statement_facts(session, candidate)

    assert facts.stated_party.wording == ""
    assert facts.source_stated_party_wording == "Kinder Morgan"
    assert facts.new_timing.timing.text == "2026-06-01"
    assert facts.new_timing.visible_timing.text == "June 1, 2026"
