"""Stable External Party Statement extraction from Meeting Minutes."""

from datetime import date

import pytest
from sqlalchemy import select

from corridor.db import Session, engine
from corridor.extract_minutes_v4 import (
    EXTERNAL_PARTY_STATEMENT_TYPES,
    PROMPT_VERSION,
    SCHEMA,
    _to_candidate,
    extract_document,
)
from corridor.models import DocPage, Document, Project
from corridor.product_proving_run import ExtractionConfiguration, compare_candidate_sets


CHAIN = (
    "Equistar to provide a chain of title on the ROW agreement that is in DOW’s "
    "name (Due \ndate of 01/2025)."
)
PROPERTY = (
    "Equistar to provide property interest documentation in the correct project "
    "location \n(Easement received appears to be further south)."
)
AS_BUILT = (
    "Equistar to research if they have as-built documentation at siphon ditch. "
    "Complete, \nStacy emailed the as built for both pipeline crossings on 2/12."
)
UJUA = (
    "UJUA - Currently, we will wait for the right-of-way maps, provided this "
    "\ndoes not hinder the progress of resolving these conflicts."
)
PAGE_TEXT = "Meeting Notes and attendance. " * 8 + "\n".join(
    (CHAIN, PROPERTY, AS_BUILT, UJUA)
)


class StubClient:
    model = "gpt-5.6-luna"

    def __init__(self, events):
        self.events = events
        self.calls = []

    def complete(self, *, system, user, schema):
        self.calls.append({"system": system, "user": user, "schema": schema})
        return {"events": self.events}


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    yield session
    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def document(session):
    project = Project(slug="minutes-v4-test", name="Minutes v4 test", is_synthetic=True)
    session.add(project)
    session.flush()
    document = Document(
        project_id=project.id,
        sha256="4" * 64,
        filename="Meeting Notes/Equistar/2025.02.12 Equistar notes.pdf",
        doc_type="minutes",
        doc_date=date(2025, 2, 12),
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush()
    session.add(
        DocPage(
            document_id=document.id,
            page_no=1,
            text=PAGE_TEXT,
            text_source="text_layer",
        )
    )
    session.flush()
    return document


def _event(
    event_type,
    quote,
    *,
    committed_date=None,
    previous_timing=None,
    stated_party=None,
    conflict_ref=None,
):
    return {
        "event_type": event_type,
        "external_org": "Equistar",
        "stated_party": stated_party,
        "conflict_ref": conflict_ref,
        "committed_date": committed_date,
        "previous_timing": previous_timing,
        "quote": quote,
        "confidence": 0.99,
    }


def _month(text="01/2025"):
    return {
        "text": text,
        "precision": "month",
        "start_date": "2025-01-01",
        "end_date": "2025-01-31",
    }


def _raw(candidate):
    return {
        "candidate_id": candidate.id,
        "project_id": candidate.project_id,
        "kind": candidate.kind,
        "source_document_id": candidate.source_document_id,
        "payload_json": candidate.payload_json,
        "source_pages": candidate.source_pages,
        "prompt_version": candidate.prompt_version,
        "model": candidate.model,
        "citations_verified": candidate.citations_verified,
        "state": candidate.state,
    }


def _configuration():
    return ExtractionConfiguration(
        prompt_version=PROMPT_VERSION,
        model="gpt-5.6-luna",
        schema_version=PROMPT_VERSION,
        prompt_sha256="4" * 64,
        schema_sha256="5" * 64,
        postprocessor_sha256="6" * 64,
        config_sha256="7" * 64,
    )


def test_schema_names_only_external_party_statement_types():
    event_types = SCHEMA["properties"]["events"]["items"]["properties"][
        "event_type"
    ]["enum"]
    assert tuple(event_types) == EXTERNAL_PARTY_STATEMENT_TYPES
    assert "response" not in event_types
    assert "status_change" not in event_types


def test_commitment_facts_come_from_registered_document_and_exact_evidence(
    session, document
):
    client = StubClient(
        [_event("commitment", CHAIN, committed_date=_month(), stated_party=None)]
    )
    [candidate] = extract_document(session, document, client=client)

    assert candidate.prompt_version == PROMPT_VERSION == "minutes_v4"
    assert candidate.citations_verified is True
    assert candidate.payload_json["fields"] == {
        "event_type": "commitment",
        "description": CHAIN,
        "external_org": "Equistar",
        "event_date": "2025-02-12",
        "stated_party": "Equistar",
        "committed_date": _month(),
    }


def test_month_timing_text_is_selected_from_evidence_not_model_span(
    session, document
):
    model_timing = _month("Due date of 01/2025")
    client = StubClient(
        [_event("commitment", CHAIN, committed_date=model_timing)]
    )

    [candidate] = extract_document(session, document, client=client)

    assert candidate.payload_json["fields"]["committed_date"] == _month()


def test_unsupported_status_and_untimed_action_do_not_create_candidates(
    session, document
):
    client = StubClient(
        [
            _event("status_change", UJUA),
            _event("commitment", PROPERTY, committed_date=None),
        ]
    )

    assert extract_document(session, document, client=client) == []


def test_explicit_completion_is_stable_closure_even_when_model_calls_it_response(
    session, document
):
    page = session.scalar(select(DocPage).where(DocPage.document_id == document.id))
    candidate = _to_candidate(
        document,
        page,
        _event("response", AS_BUILT, conflict_ref="PL20"),
        "gpt-5.6-luna",
    )

    assert candidate.payload_json["fields"] == {
        "event_type": "closure",
        "description": AS_BUILT,
        "external_org": "Equistar",
        "event_date": "2025-02-12",
        "stated_party": "Equistar",
    }


def test_future_delivery_remains_a_commitment_not_a_closure(session, document):
    quote = "Equistar will have documents delivered by 01/2025."
    page = session.scalar(select(DocPage).where(DocPage.document_id == document.id))
    page.text += f"\n{quote}"

    candidate = _to_candidate(
        document,
        page,
        _event("commitment", quote, committed_date=_month()),
        "gpt-5.6-luna",
    )

    assert candidate.payload_json["fields"]["event_type"] == "commitment"
    assert candidate.payload_json["fields"]["committed_date"] == _month()


def test_quote_supported_actor_overrides_folder_party(session, document):
    quote = "Kinder Morgan will deliver the revised package by 01/2025."
    page = session.scalar(select(DocPage).where(DocPage.document_id == document.id))
    page.text += f"\n{quote}"

    candidate = _to_candidate(
        document,
        page,
        {
            **_event("commitment", quote, committed_date=_month()),
            "external_org": "Kinder Morgan",
            "stated_party": "Kinder Morgan",
        },
        "gpt-5.6-luna",
    )

    assert candidate.payload_json["fields"]["external_org"] == "Kinder Morgan"
    assert candidate.payload_json["fields"]["stated_party"] == "Kinder Morgan"


@pytest.mark.parametrize(
    "quote",
    (
        "PL19 - Kinder Morgan to provide title by 01/2025.",
        "After review, Kinder Morgan will provide title by 01/2025.",
    ),
)
def test_minutes_label_or_intro_does_not_hide_the_stated_party(
    session, document, quote
):
    page = session.scalar(select(DocPage).where(DocPage.document_id == document.id))
    page.text += f"\n{quote}"

    candidate = _to_candidate(
        document,
        page,
        {
            **_event("commitment", quote, committed_date=_month()),
            "external_org": "Kinder Morgan",
            "stated_party": "Kinder Morgan",
        },
        "gpt-5.6-luna",
    )

    assert candidate.payload_json["fields"]["external_org"] == "Kinder Morgan"
    assert candidate.payload_json["fields"]["stated_party"] == "Kinder Morgan"


def test_affected_party_mention_does_not_manufacture_a_speaker(session, document):
    quote = "TxDOT expects Equistar's package by 01/2025."
    page = session.scalar(select(DocPage).where(DocPage.document_id == document.id))
    page.text += f"\n{quote}"

    candidate = _to_candidate(
        document,
        page,
        _event(
            "commitment",
            quote,
            committed_date=_month(),
            stated_party="Equistar",
        ),
        "gpt-5.6-luna",
    )

    assert candidate.payload_json["fields"]["external_org"] == "Equistar"
    assert "stated_party" not in candidate.payload_json["fields"]


def test_approximate_timing_keeps_only_the_exact_timing_words(session, document):
    quote = "Kinder Morgan will proceed after TxDOT confirms the ROW exhibit."
    page = session.scalar(select(DocPage).where(DocPage.document_id == document.id))
    page.text += f"\n{quote}"
    timing = {
        "text": "after TxDOT confirms the ROW exhibit",
        "precision": "approximate",
        "start_date": None,
        "end_date": None,
    }

    candidate = _to_candidate(
        document,
        page,
        {
            **_event("commitment", quote, committed_date=timing),
            "external_org": "Kinder Morgan",
            "stated_party": "Kinder Morgan",
        },
        "gpt-5.6-luna",
    )

    assert candidate.payload_json["fields"]["committed_date"] == timing


def test_captured_v3_variants_converge_under_v4_candidate_contract(
    session, document
):
    page = session.scalar(select(DocPage).where(DocPage.document_id == document.id))
    baseline_items = (
        _event("commitment", CHAIN, committed_date=_month()),
        _event("commitment", PROPERTY),
        _event("response", AS_BUILT, conflict_ref="PL20"),
        _event("status_change", UJUA, conflict_ref="UJUA"),
    )
    fresh_items = (
        _event("status_change", "PL19 remains in place."),
        _event("response", "The action items belong to Casey."),
        _event("commitment", CHAIN, committed_date=_month(), stated_party="Equistar"),
        _event("commitment", PROPERTY, stated_party="Equistar"),
        _event("closure", AS_BUILT),
        _event("response", UJUA),
    )
    page.text += "\nPL19 remains in place.\nThe action items belong to Casey."
    baseline = tuple(
        candidate
        for item in baseline_items
        if (candidate := _to_candidate(document, page, item, "gpt-5.6-luna"))
        is not None
    )
    fresh = tuple(
        candidate
        for item in fresh_items
        if (candidate := _to_candidate(document, page, item, "gpt-5.6-luna"))
        is not None
    )

    comparison = compare_candidate_sets(
        document_id=document.id,
        baseline_run_id=1,
        fresh_run_id=2,
        baseline=[_raw(candidate) for candidate in baseline],
        fresh=[_raw(candidate) for candidate in fresh],
        baseline_configuration=_configuration(),
        fresh_configuration=_configuration(),
    )

    assert comparison.equal
    assert len(baseline) == len(fresh) == 2
