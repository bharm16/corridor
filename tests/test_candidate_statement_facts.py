"""Candidate statement facts are prepared once for every read adapter."""

from datetime import date
from hashlib import sha256

import pytest
from sqlalchemy import select

from corridor.candidate_statement_facts import prepare_candidate_statement_facts
from corridor.db import Session, engine
from corridor.models import (
    Candidate,
    DocPage,
    Document,
    ExternalOrg,
    ExtractedProposal,
    ExtractedProposalFact,
    ExtractionRun,
    ExtractionRunCandidate,
    Fact,
    FactSource,
    Project,
    SourceSegment,
    SubjectResolutionAttempt,
)
from corridor.subject_resolution import unresolved_subject_work_items


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
        prompt_version="minutes-v4-test",
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


def _attach_truthful_statement_segment(session, document, candidate, exact_text):
    """Link one Candidate to its immutable #437 statement-wording source."""

    run = ExtractionRun(
        document_id=document.id,
        prompt_version="minutes-v4-test",
        outcome="completed",
        candidate_count=1,
        page_errors=0,
        model=None,
        schema_version=None,
    )
    session.add(run)
    session.flush()
    session.add(
        ExtractionRunCandidate(
            extraction_run_id=run.id,
            candidate_id=candidate.id,
        )
    )
    session.flush()
    candidate.extraction_run_id = run.id
    session.flush()
    segment = SourceSegment(
        project_id=candidate.project_id,
        document_id=document.id,
        kind="prose_span",
        exact_text=exact_text,
        content_sha256=sha256(exact_text.encode()).hexdigest(),
        ordinal=1,
        sheet_name=None,
        cell_range=None,
        page_no=1,
        start_offset=0,
        end_offset=len(exact_text),
    )
    session.add(segment)
    session.flush()
    fact = Fact(
        project_id=candidate.project_id,
        document_id=document.id,
        extraction_run_id=run.id,
        fact_type="statement_wording",
        subject_kind="statement_candidate",
        subject_key=f"candidate:{candidate.id}",
        text_value=exact_text,
        date_value=None,
        date_range_start=None,
        date_range_end=None,
        external_org_value_id=None,
        document_value_id=None,
        transformation="exact_prose_span_v1",
        recorded_by="extractor:minutes-v4-test",
        content_sha256=sha256(f"fact:{candidate.id}:{exact_text}".encode()).hexdigest(),
    )
    session.add(fact)
    session.flush()
    session.add(
        FactSource(
            project_id=candidate.project_id,
            document_id=document.id,
            fact_id=fact.id,
            source_segment_id=segment.id,
            role="attribution_source",
            ordinal=1,
        )
    )
    proposal = ExtractedProposal(
        project_id=candidate.project_id,
        document_id=document.id,
        extraction_run_id=run.id,
        candidate_id=candidate.id,
        kind="event",
        subject_key=f"candidate:{candidate.id}",
        candidate_metadata_json={},
    )
    session.add(proposal)
    session.flush()
    session.add(
        ExtractedProposalFact(
            project_id=candidate.project_id,
            document_id=document.id,
            extraction_run_id=run.id,
            proposal_id=proposal.id,
            fact_id=fact.id,
            ordinal=1,
        )
    )
    session.flush()
    return segment


def test_preparation_returns_only_evidence_supported_statement_facts(session):
    quote = "KM will complete the relocation in June 2026."
    party, document, candidate = _candidate_with_page(
        session,
        quote=quote,
        page_text=quote,
        fields={
            "event_type": "commitment",
            "description": quote,
            "external_org": "KM",
            "stated_party": "KM",
            "event_date": "2026-05-20",
            "committed_date": {
                "text": "June 2026",
                "precision": "month",
                "start_date": "2026-06-01",
                "end_date": "2026-06-30",
            },
        },
    )
    segment = _attach_truthful_statement_segment(session, document, candidate, quote)

    facts = prepare_candidate_statement_facts(session, candidate)

    assert facts.evidence_is_complete is True
    assert facts.evidence_is_reviewable is True
    assert facts.affected_party.wording == "KM"
    assert facts.affected_party.external_org_id == party.id
    assert facts.stated_party.wording == "KM"
    assert facts.stated_party.external_org_id == party.id
    assert facts.stated_party.resolution_state == "resolved"
    assert facts.stated_party.attention_reason is None
    assert facts.stated_party.rule_identity == "exact-registered-alias-v1"
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
    attempts = session.scalars(
        select(SubjectResolutionAttempt).order_by(SubjectResolutionAttempt.usage)
    ).all()
    assert [(attempt.source_segment_id, attempt.usage) for attempt in attempts] == [
        (segment.id, "affected_subject"),
        (segment.id, "statement_speaker"),
    ]


def test_preparation_preserves_actor_boundary_and_visible_attention_reason(session):
    quote = "LJA Engineering said Kinder Morgan will relocate the line."
    party, document, candidate = _candidate_with_page(
        session,
        quote=quote,
        page_text=quote,
        fields={
            "event_type": "commitment",
            "description": quote,
            "external_org": "Kinder Morgan",
            "stated_party": "LJA Engineering",
        },
    )
    project = session.get(Project, candidate.project_id)
    project.project_side_parties = ["LJA Engineering"]
    session.add(ExternalOrg(name="LJA Engineering", aliases=[]))
    _attach_truthful_statement_segment(session, document, candidate, quote)

    facts = prepare_candidate_statement_facts(session, candidate)

    assert facts.affected_party.external_org_id == party.id
    assert facts.stated_party.external_org_id is None
    assert facts.stated_party.resolution_state == "actor_boundary"
    assert facts.stated_party.attention_reason == "project_side_speaker"
    [work_item] = unresolved_subject_work_items(session, project.id)
    assert work_item.attention_reason == "project_side_speaker"


def test_preparation_never_fuzzy_resolves_an_unregistered_source_name(session):
    quote = "Kinder will complete the relocation in June 2026."
    _party, document, candidate = _candidate_with_page(
        session,
        quote=quote,
        page_text=quote,
        fields={
            "event_type": "commitment",
            "description": quote,
            "external_org": "Kinder",
            "stated_party": "Kinder",
        },
    )
    _attach_truthful_statement_segment(session, document, candidate, quote)

    facts = prepare_candidate_statement_facts(session, candidate)

    assert facts.affected_party.registered_external_org_ids == ()
    assert facts.stated_party.registered_external_org_ids == ()
    assert facts.affected_party.resolution_state == "unresolved"
    assert facts.stated_party.resolution_state == "unresolved"
    assert facts.affected_party.attention_reason == "unregistered_subject_reference"
    assert facts.stated_party.attention_reason == "unregistered_subject_reference"


def test_preparation_never_bypasses_an_ambiguous_spine_source_link(session):
    quote = "Kinder Morgan will complete the relocation in June 2026."
    _party, document, candidate = _candidate_with_page(
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
    _attach_truthful_statement_segment(session, document, candidate, quote)
    fact = session.scalar(
        select(Fact).where(Fact.subject_key == f"candidate:{candidate.id}")
    )
    ambiguous_segment = SourceSegment(
        project_id=candidate.project_id,
        document_id=document.id,
        kind="prose_span",
        exact_text="Kinder Morgan",
        content_sha256=sha256(b"Kinder Morgan").hexdigest(),
        ordinal=2,
        sheet_name=None,
        cell_range=None,
        page_no=1,
        start_offset=len(quote) + 1,
        end_offset=len(quote) + 1 + len("Kinder Morgan"),
    )
    session.add(ambiguous_segment)
    session.flush()
    session.add(
        FactSource(
            project_id=candidate.project_id,
            document_id=document.id,
            fact_id=fact.id,
            source_segment_id=ambiguous_segment.id,
            role="attribution_source",
            ordinal=2,
        )
    )
    session.flush()

    facts = prepare_candidate_statement_facts(session, candidate)

    assert facts.affected_party.registered_external_org_ids == ()
    assert facts.stated_party.registered_external_org_ids == ()
    assert facts.affected_party.attention_reason == (
        "statement_attribution_source_unavailable"
    )
    assert facts.stated_party.attention_reason == (
        "statement_attribution_source_unavailable"
    )
    assert session.scalars(select(SubjectResolutionAttempt)).all() == []


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
