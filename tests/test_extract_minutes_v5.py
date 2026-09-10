"""Deterministic Action Item membership for Minutes v5."""

from __future__ import annotations

from datetime import date
from hashlib import sha256

import pytest
from sqlalchemy import select

from corridor.db import Session, engine
from corridor.extract_batch import extract_documents
from corridor.extract_minutes_v5 import (
    MIN_PAGE_CHARS,
    PROMPT_PATH,
    PROMPT_VERSION,
    SCHEMA,
    extract_document,
    extract_page_candidates,
    to_candidate,
)
from corridor.extractor_lineage import deployed_extractor_config
from corridor.llm import RequestConfiguration
from corridor.models import (
    Candidate,
    DocPage,
    Document,
    ExtractionRun,
    Fact,
    Project,
    SourceFactAppendReceipt,
)

from model_client_support import FakeModelClient
from pdf_fixture_support import PdfFixture


CHAIN = (
    "Equistar to provide a chain of title on the ROW agreement that is in DOW’s name (Due \n"
    "date of 01/2025)."
)
PROPERTY = (
    "Equistar to provide property interest documentation in the correct project location \n"
    "(Easement received appears to be further south)."
)
AS_BUILT = (
    "Equistar to research if they have as-built documentation at siphon ditch. Complete, \n"
    "Stacy emailed the as built for both pipeline crossings on 2/12."
)
LJA = (
    "LJA to follow up with RODSSUE regarding the depth of cover throughout the crossing \n"
    "within the ROW."
)
PAGE_1438 = f"""Meeting Notes and attendance. {'registered text ' * 12}
Action Items:
1. {CHAIN}
2. {PROPERTY}
3. {LJA}
4.
 {AS_BUILT}
Meeting Notes
 2
"""
PAGE_1435 = f"""Meeting Notes and attendance. {'registered text ' * 12}
Action Items:
1. {CHAIN}
2. {PROPERTY}
3. LJA to review the depth of cover data points provided on 10/09 in relation to the ditch
depth.
4. {LJA}
Meeting Notes
 2
"""
OUTSIDE = "Equistar will submit the signed exhibit by March 2025."


def _timing():
    return {
        "text": "01/2025",
        "precision": "month",
        "start_date": "2025-01-01",
        "end_date": "2025-01-31",
    }


def _event(event_type, quote, *, timing=None):
    return {
        "event_type": event_type,
        "external_org": "Equistar",
        "stated_party": "Equistar",
        "conflict_ref": None,
        "committed_date": timing,
        "previous_timing": None,
        "quote": quote,
        "confidence": 0.99,
    }


MINUTES_CONFIGURATION = RequestConfiguration(
    model="gpt-5.6-luna", base_url="https://provider.example/v1"
)


def stub_client(events):
    """The shared recording double, answering with these events every call."""
    return FakeModelClient({"events": events}, configuration=MINUTES_CONFIGURATION)


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
    project = Project(slug="minutes-v5-test", name="Minutes v5 test", is_synthetic=True)
    session.add(project)
    session.flush()
    document = Document(
        project_id=project.id,
        sha256="5" * 64,
        filename="Meeting Notes/Equistar/2025.02.12 GPB1 Equistar notes final.pdf",
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
            page_no=2,
            text=PAGE_1438,
            text_source="text_layer",
        )
    )
    session.flush()
    return document


def _meaning(candidates):
    return tuple(
        (candidate.payload_json["fields"], candidate.payload_json["citations"])
        for candidate in candidates
    )


def _replace_page(session, document, text):
    page = session.scalar(select(DocPage).where(DocPage.document_id == document.id))
    page.text = text
    session.flush()


def _variants():
    property_timing = {
        "text": "further south",
        "precision": "approximate",
        "start_date": None,
        "end_date": None,
    }
    return (
        [],
        [
            _event("commitment", CHAIN, timing=_timing()),
            _event("closure", AS_BUILT),
        ],
        [
            _event("commitment", CHAIN, timing=_timing()),
            _event("commitment", CHAIN, timing=_timing()),
            _event("closure", AS_BUILT),
            _event("closure", AS_BUILT),
        ],
        [
            _event("closure", CHAIN),
            _event("commitment", PROPERTY, timing=property_timing),
            _event("commitment", AS_BUILT, timing=property_timing),
        ],
    )


def test_action_item_membership_survives_model_omission(session, document):
    omitted = extract_document(session, document, client=stub_client([]))
    complete = extract_document(
        session,
        document,
        client=stub_client(
            [
                _event("commitment", CHAIN, timing=_timing()),
                _event("closure", AS_BUILT),
            ]
        ),
    )

    assert _meaning(omitted) == _meaning(complete)
    assert len(omitted) == 2
    assert {candidate.prompt_version for candidate in omitted} == {PROMPT_VERSION}


@pytest.mark.parametrize(
    ("page_text", "expected_quotes"),
    (
        (PAGE_1435, (CHAIN,)),
        (PAGE_1438, (CHAIN, AS_BUILT)),
    ),
)
def test_live_shape_candidate_sets_repeat_across_adversarial_model_results(
    session, document, page_text, expected_quotes
):
    _replace_page(session, document, page_text)

    observed = []
    for events in (*_variants(), *_variants()):
        candidates = extract_document(
            session,
            document,
            client=stub_client(events),
        )
        observed.append(_meaning(candidates))
        assert tuple(
            candidate.payload_json["fields"]["description"]
            for candidate in candidates
        ) == expected_quotes

    assert all(meaning == observed[0] for meaning in observed)


def test_exact_action_quotes_keep_registered_line_breaks(session, document):
    candidates = extract_document(session, document, client=stub_client([]))

    assert [candidate.payload_json["fields"]["description"] for candidate in candidates] == [
        CHAIN,
        AS_BUILT,
    ]
    assert candidates[0].payload_json["fields"]["committed_date"] == _timing()
    assert candidates[1].payload_json["fields"]["event_type"] == "closure"
    assert "conflict_ref" not in candidates[1].payload_json["fields"]


def test_project_side_and_untimed_action_items_stay_out_even_when_model_adds_them(
    session, document
):
    project_side = "LJA will deliver its review by 03/2025."
    text = PAGE_1438.replace(LJA, project_side)
    _replace_page(session, document, text)
    model_items = [
        _event("commitment", PROPERTY, timing={
            "text": "further south",
            "precision": "approximate",
            "start_date": None,
            "end_date": None,
        }),
        {
            **_event("commitment", project_side, timing={
                "text": "03/2025",
                "precision": "month",
                "start_date": "2025-03-01",
                "end_date": "2025-03-31",
            }),
            "external_org": "LJA",
            "stated_party": "LJA",
        },
    ]

    candidates = extract_document(
        session,
        document,
        client=stub_client(model_items),
    )

    assert [candidate.payload_json["fields"]["description"] for candidate in candidates] == [
        CHAIN,
        AS_BUILT,
    ]


def test_action_item_date_change_requires_two_exact_timings_and_change_wording(
    session, document
):
    changed = "Equistar changed delivery from 01/2025 to 03/2025."
    two_without_change = "Equistar to coordinate 01/2025 and 03/2025."
    _replace_page(
        session,
        document,
        f"{'registered text ' * 15}\nAction Items:\n1. {changed}\n2. {two_without_change}\nMeeting Notes\n",
    )

    [candidate] = extract_document(session, document, client=stub_client([]))

    fields = candidate.payload_json["fields"]
    assert fields["event_type"] == "committed_date_change"
    assert fields["previous_timing"] == _timing()
    assert fields["committed_date"] == {
        "text": "03/2025",
        "precision": "month",
        "start_date": "2025-03-01",
        "end_date": "2025-03-31",
    }


def test_model_still_supplies_supported_statements_outside_action_items(
    session, document
):
    _replace_page(session, document, f"{OUTSIDE}\n{PAGE_1438}")
    march = {
        "text": "March 2025",
        "precision": "month",
        "start_date": "2025-03-01",
        "end_date": "2025-03-31",
    }

    candidates = extract_document(
        session,
        document,
        client=stub_client([_event("commitment", OUTSIDE, timing=march)]),
    )

    assert {
        candidate.payload_json["fields"]["description"] for candidate in candidates
    } == {OUTSIDE, CHAIN, AS_BUILT}


def test_duplicate_literal_outside_action_items_remains_model_eligible(
    session, document
):
    other_party = "Kinder Morgan will deliver the title package by 03/2025."
    _replace_page(
        session,
        document,
        f"{'registered text ' * 15}\n{other_party}\nAction Items:\n1. {other_party}\n"
        f"2. {CHAIN}\nMeeting Notes\n",
    )
    march = {
        "text": "03/2025",
        "precision": "month",
        "start_date": "2025-03-01",
        "end_date": "2025-03-31",
    }
    model_item = {
        **_event("commitment", other_party, timing=march),
        "external_org": "Kinder Morgan",
        "stated_party": "Kinder Morgan",
    }

    candidates = extract_document(
        session,
        document,
        client=stub_client([model_item]),
    )

    assert {
        candidate.payload_json["fields"]["description"] for candidate in candidates
    } == {CHAIN, other_party}


def test_trailing_prose_is_not_absorbed_into_last_numbered_action_item(
    session, document
):
    trailing = "Equistar completed an unrelated delivery."
    _replace_page(
        session,
        document,
        f"{'registered text ' * 15}\nAction Items:\n1. {CHAIN}\n\n{trailing}\n"
        "Meeting Notes\n",
    )

    [candidate] = extract_document(session, document, client=stub_client([]))

    assert candidate.payload_json["fields"]["description"] == CHAIN
    assert candidate.payload_json["fields"]["event_type"] == "commitment"


def test_production_batch_seam_emits_action_items_when_model_returns_none(
    session, document, tmp_path
):
    source_path = tmp_path / "minutes-v5-production.pdf"
    fixture = PdfFixture()
    fixture.add_page()
    page = fixture.add_page(width=1200, height=1600)
    page.text_box((72, 72, 1128, 1528), PAGE_1438, fontsize=10)
    fixture.save(source_path)
    # The page text the extractor reads is the text the fixture declares it
    # placed, not a reading of the file: the seam's own reader is under test.
    _replace_page(session, document, page.expected_text)
    document.sha256 = sha256(source_path.read_bytes()).hexdigest()
    document.pages = 2
    document._stored_path = str(source_path)
    client = stub_client([])
    config = deployed_extractor_config("minutes", client=client)

    created = extract_documents(
        session,
        [document],
        client=client,
        system=PROMPT_PATH.read_text(),
        schema=SCHEMA,
        min_page_chars=MIN_PAGE_CHARS,
        to_candidate=to_candidate,
        page_candidates=extract_page_candidates,
        items_key="events",
        prompt_version=PROMPT_VERSION,
        extractor_config=config,
        commit=False,
    )

    assert len(created) == 2
    [run] = session.scalars(
        select(ExtractionRun).where(ExtractionRun.document_id == document.id)
    ).all()
    assert run.prompt_version == "minutes_v5"
    assert run.schema_version == "minutes_v5"
    assert run.candidate_count == 2
    assert session.scalars(
        select(Candidate)
        .where(Candidate.source_document_id == document.id)
        .order_by(Candidate.id)
    ).all() == created
    assert len(session.scalars(select(Fact)).all()) == 2
    assert len(session.scalars(select(SourceFactAppendReceipt)).all()) == 1
