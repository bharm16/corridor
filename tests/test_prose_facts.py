"""Pending statement wording Facts over exact Minutes prose spans."""

from datetime import date
import os
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from corridor.db import Session, engine
from corridor.config import settings
from corridor.extract_batch import extract_documents
from corridor.extract_minutes_v5 import (
    MIN_PAGE_CHARS,
    PROMPT_PATH,
    PROMPT_VERSION,
    SCHEMA,
    extract_page_candidates,
    to_candidate,
)
from corridor.extractor_lineage import deployed_extractor_config
from corridor.event_admission import waiting_statements
from corridor.extraction_runs import (
    append_source_facts,
    declare_active_run,
    record_extraction_run,
)
from corridor.facts import (
    FACT_TYPE_CONTRACTS,
    FactValidationError,
    proposal_input_snapshots,
    replay_fact,
)
from corridor.ingest import ingest_document
from corridor.prose_spans import is_prose_segment, prose_segment_filter
from corridor.models import (
    Candidate,
    DependencyEvent,
    DocPage,
    ExtractionRun,
    ExternalOrg,
    Fact,
    FactSource,
    Project,
    SourceFactAppendReceipt,
    SourceSegment,
)
from corridor.principals import HumanPrincipal
from corridor.revision_comparison import _run_inputs

from pdf_fixture_support import PdfFixture


STATEMENT = "Equistar will submit the signed exhibit by March 2025."
REAL_MINUTES_SHA256 = (
    "ada13da24574950264d974f8a352fd07a3dba042ac2b37e825f8409730182eaf"
)
CORPUS_STORE = Path(
    os.environ.get("CORRIDOR_TEST_CORPUS_STORE", settings.corpus_store)
)
REAL_MINUTES = (
    CORPUS_STORE / REAL_MINUTES_SHA256[:2] / f"{REAL_MINUTES_SHA256}.pdf"
)
needs_minutes_corpus = pytest.mark.skipif(
    not REAL_MINUTES.exists(),
    reason="run `make corpus` to fetch the SH99 Equistar meeting notes",
)
OPERATOR = HumanPrincipal("local:prose-fact-operator")


class StubClient:
    model = "gpt-5.6-luna"
    effort = "none"
    flex = False
    base_url = "https://provider.example/v1"
    max_workers = 1

    def complete(self, *, system, user, schema):
        return {"events": []}


def _minutes_pdf(tmp_path, *, name="equistar-minutes.pdf", statement=STATEMENT):
    path = tmp_path / name
    fixture = PdfFixture()
    fixture.add_page().text(
        (72, 72),
        ("Registered coordination context.\n" * 8)
        +
        "Action Items:\n"
        f"1. {statement}\n"
        "Meeting Notes",
    )
    return fixture.save(path)


def test_scoped_append_creates_pending_statement_wording_with_role_tagged_spans(
    tmp_path,
):
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        project = Project(
            slug="prose-fact-test", name="Prose Fact Test", is_synthetic=True
        )
        session.add(project)
        session.flush()
        path = _minutes_pdf(tmp_path)
        document = ingest_document(
            session,
            project_id=project.id,
            path=path,
            filename="Meeting Notes/Equistar/2025-02-12 Equistar notes.pdf",
            doc_type="minutes",
            doc_date=date(2025, 2, 12),
            images_dir=tmp_path / "images",
        )
        page = session.scalars(
            select(DocPage).where(DocPage.document_id == document.id)
        ).one()
        [candidate] = extract_page_candidates(document, page, [], None)

        result = append_source_facts(
            session,
            document,
            idempotency_key="minutes:statement-wording:test",
            prompt_version=PROMPT_VERSION,
            schema_version=PROMPT_VERSION,
            candidate_count=1,
            page_errors=0,
            candidates=(candidate,),
            model=None,
            allow_unsealed_legacy=True,
            source_path=path,
        )

        [fact] = result.facts
        assert (
            fact.fact_type,
            fact.subject_kind,
            fact.subject_key,
            fact.text_value,
            fact.transformation,
        ) == (
            "statement_wording",
            "statement_candidate",
            f"candidate:{candidate.id}",
            STATEMENT,
            "exact_prose_span_v1",
        )
        sources = session.scalars(
            select(FactSource)
            .where(FactSource.fact_id == fact.id)
            .order_by(FactSource.role)
        ).all()
        assert [(source.role, source.ordinal) for source in sources] == [
            ("attribution_source", 1),
            ("value_source", 1),
        ]
        assert len({source.source_segment_id for source in sources}) == 1
        segment = session.get(SourceSegment, sources[0].source_segment_id)
        assert is_prose_segment(segment)
        assert segment.exact_text == STATEMENT
        assert replay_fact(session, document, fact, path) == STATEMENT
        other_path = _minutes_pdf(
            tmp_path,
            name="other-minutes.pdf",
            statement="Equistar will submit a different exhibit by April 2025.",
        )
        other_document = ingest_document(
            session,
            project_id=project.id,
            path=other_path,
            filename="Meeting Notes/Equistar/2025-02-13 Other notes.pdf",
            doc_type="minutes",
            doc_date=date(2025, 2, 13),
            images_dir=tmp_path / "other-images",
        )
        foreign_segment = session.scalars(
            select(SourceSegment)
            .where(
                SourceSegment.document_id == other_document.id,
                prose_segment_filter(SourceSegment),
            )
            .order_by(SourceSegment.ordinal)
        ).first()
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(
                    FactSource(
                        project_id=project.id,
                        document_id=document.id,
                        fact_id=fact.id,
                        source_segment_id=foreign_segment.id,
                        role="context",
                        ordinal=1,
                    )
                )
                session.flush()
        assert candidate.state == "pending"
        assert session.scalars(select(DependencyEvent)).all() == []
        assert (
            FACT_TYPE_CONTRACTS["statement_wording"].automatic_segment_kinds
            == frozenset()
        )
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def test_invalid_statement_span_rolls_back_the_complete_scoped_append(tmp_path):
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        project = Project(
            slug="prose-rollback-test", name="Prose Rollback Test", is_synthetic=True
        )
        session.add(project)
        session.flush()
        path = _minutes_pdf(tmp_path)
        document = ingest_document(
            session,
            project_id=project.id,
            path=path,
            filename="Meeting Notes/Equistar/2025-02-12 Equistar notes.pdf",
            doc_type="minutes",
            doc_date=date(2025, 2, 12),
            images_dir=tmp_path / "images",
        )
        page = session.scalars(
            select(DocPage).where(DocPage.document_id == document.id)
        ).one()
        [candidate] = extract_page_candidates(document, page, [], None)
        paraphrase = "Equistar will submit a paraphrased exhibit."
        candidate.payload_json["fields"]["description"] = paraphrase
        candidate.payload_json["citations"][0]["quote"] = paraphrase

        with pytest.raises(FactValidationError, match="one exact prose span"):
            append_source_facts(
                session,
                document,
                idempotency_key="minutes:statement-wording:invalid",
                prompt_version=PROMPT_VERSION,
                schema_version=PROMPT_VERSION,
                candidate_count=1,
                page_errors=0,
                candidates=(candidate,),
                model=None,
                allow_unsealed_legacy=True,
                source_path=path,
            )

        assert session.scalars(select(Candidate)).all() == []
        assert session.scalars(select(ExtractionRun)).all() == []
        assert session.scalars(select(Fact)).all() == []
        assert session.scalars(select(SourceFactAppendReceipt)).all() == []
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def test_plain_run_writer_cannot_bypass_the_scoped_statement_fact_command(tmp_path):
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        project = Project(
            slug="prose-command-boundary-test",
            name="Prose Command Boundary Test",
            is_synthetic=True,
        )
        session.add(project)
        session.flush()
        path = _minutes_pdf(tmp_path)
        document = ingest_document(
            session,
            project_id=project.id,
            path=path,
            filename="Meeting Notes/Equistar/2025-02-12 Equistar notes.pdf",
            doc_type="minutes",
            doc_date=date(2025, 2, 12),
            images_dir=tmp_path / "images",
        )
        page = session.scalars(
            select(DocPage).where(DocPage.document_id == document.id)
        ).one()
        [candidate] = extract_page_candidates(document, page, [], None)

        record_extraction_run(
            session,
            document,
            prompt_version=PROMPT_VERSION,
            schema_version=PROMPT_VERSION,
            candidate_count=1,
            page_errors=0,
            candidates=(candidate,),
            model=None,
            allow_unsealed_legacy=True,
        )

        assert session.scalars(select(Fact)).all() == []
        assert session.scalars(select(SourceFactAppendReceipt)).all() == []
        assert candidate.state == "pending"
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def test_mixed_event_proposals_append_only_contract_eligible_wording(tmp_path):
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        project = Project(
            slug="mixed-prose-fact-test",
            name="Mixed Prose Fact Test",
            is_synthetic=True,
        )
        session.add(project)
        session.flush()
        path = _minutes_pdf(tmp_path)
        document = ingest_document(
            session,
            project_id=project.id,
            path=path,
            filename="Meeting Notes/Equistar/2025-02-12 Equistar notes.pdf",
            doc_type="minutes",
            doc_date=date(2025, 2, 12),
            images_dir=tmp_path / "images",
        )
        page = session.scalars(
            select(DocPage).where(DocPage.document_id == document.id)
        ).one()
        [wording] = extract_page_candidates(document, page, [], None)
        other_proposal = Candidate(
            project_id=project.id,
            source_document_id=document.id,
            source_pages=[1],
            kind="event",
            state="pending",
            citations_verified=False,
            payload_json={
                "fields": {"description": "A legacy event summary."},
                "citations": [],
            },
            prompt_version=PROMPT_VERSION,
            model=None,
        )

        result = append_source_facts(
            session,
            document,
            idempotency_key="minutes:mixed-proposals",
            prompt_version=PROMPT_VERSION,
            schema_version=PROMPT_VERSION,
            candidate_count=2,
            page_errors=0,
            candidates=(wording, other_proposal),
            model=None,
            allow_unsealed_legacy=True,
            source_path=path,
        )

        [fact] = result.facts
        assert fact.text_value == STATEMENT
        assert result.run.candidate_count == 2
        assert wording.state == other_proposal.state == "pending"
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def test_minutes_batch_routes_statement_facts_through_the_scoped_append_command(
    tmp_path,
):
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        project = Project(
            slug="prose-batch-test", name="Prose Batch Test", is_synthetic=True
        )
        session.add_all((project, ExternalOrg(name="Equistar")))
        session.flush()
        path = _minutes_pdf(tmp_path)
        document = ingest_document(
            session,
            project_id=project.id,
            path=path,
            filename="Meeting Notes/Equistar/2025-02-12 Equistar notes.pdf",
            doc_type="minutes",
            doc_date=date(2025, 2, 12),
            images_dir=tmp_path / "images",
        )
        document._stored_path = str(path)
        client = StubClient()
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

        assert len(created) == 1
        [fact] = session.scalars(
            select(Fact).where(Fact.fact_type == "statement_wording")
        ).all()
        [receipt] = session.scalars(select(SourceFactAppendReceipt)).all()
        assert fact.extraction_run_id == receipt.extraction_run_id
        run = session.get(ExtractionRun, receipt.extraction_run_id)
        [snapshot] = proposal_input_snapshots(session, run)
        assert snapshot["payload_json"]["fields"] == {
            **created[0].payload_json["fields"],
            "description": STATEMENT,
        }
        assert "statement_wording" not in snapshot["payload_json"]["fields"]
        declare_active_run(session, document.id, run.id, principal=OPERATOR)
        [waiting] = waiting_statements(session, project.id)
        assert waiting["event_type"] == "commitment"
        assert waiting["external_org"] == "Equistar"
        assert waiting["description"] == STATEMENT
        assert waiting["attachable"] is True

        created[0].payload_json = {
            **created[0].payload_json,
            "fields": {
                "event_type": "closure",
                "external_org": "Changed",
                "stated_party": "Changed",
                "description": "Changed",
            },
        }
        created[0].state = "rejected"
        session.flush()
        [sealed] = _run_inputs(session, document, run)
        assert sealed["state"] == "pending"
        assert sealed["payload_json"]["fields"] == snapshot["payload_json"]["fields"]
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def test_minutes_batch_refuses_fact_append_when_original_bytes_are_unavailable(
    tmp_path,
):
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        project = Project(
            slug="missing-prose-source-test",
            name="Missing Prose Source Test",
            is_synthetic=True,
        )
        session.add(project)
        session.flush()
        path = _minutes_pdf(tmp_path)
        document = ingest_document(
            session,
            project_id=project.id,
            path=path,
            filename="Meeting Notes/Equistar/2025-02-12 Equistar notes.pdf",
            doc_type="minutes",
            doc_date=date(2025, 2, 12),
            images_dir=tmp_path / "images",
        )
        client = StubClient()
        config = deployed_extractor_config("minutes", client=client)

        with pytest.raises(ValueError, match="requires registered original bytes"):
            extract_documents(
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

        assert session.scalars(select(Candidate)).all() == []
        assert session.scalars(select(ExtractionRun)).all() == []
        assert session.scalars(select(Fact)).all() == []
        assert session.scalars(select(SourceFactAppendReceipt)).all() == []
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@needs_minutes_corpus
def test_real_dev_corpus_minutes_append_replayable_pending_statement_facts(tmp_path):
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        project = Project(
            slug="real-prose-fact-test",
            name="Real Prose Fact Test",
            is_synthetic=True,
        )
        session.add(project)
        session.flush()
        document = ingest_document(
            session,
            project_id=project.id,
            path=REAL_MINUTES,
            filename="Meeting Notes/Equistar/2025.02.12 GPB1 Equistar notes final.pdf",
            doc_type="minutes",
            doc_date=date(2025, 2, 12),
            images_dir=tmp_path / "images",
            expected_sha256=REAL_MINUTES_SHA256,
        )
        page = session.scalars(
            select(DocPage).where(
                DocPage.document_id == document.id,
                DocPage.page_no == 2,
            )
        ).one()
        candidates = tuple(extract_page_candidates(document, page, [], None))

        result = append_source_facts(
            session,
            document,
            idempotency_key="minutes:real-statement-wording",
            prompt_version=PROMPT_VERSION,
            schema_version=PROMPT_VERSION,
            candidate_count=len(candidates),
            page_errors=0,
            candidates=candidates,
            model=None,
            allow_unsealed_legacy=True,
            source_path=REAL_MINUTES,
        )

        assert len(candidates) == len(result.facts) == 2
        assert [
            replay_fact(session, document, fact, REAL_MINUTES)
            for fact in result.facts
        ] == [
            candidate.payload_json["fields"]["description"]
            for candidate in candidates
        ]
        assert {fact.fact_type for fact in result.facts} == {"statement_wording"}
        assert all(candidate.state == "pending" for candidate in candidates)
        assert session.scalars(select(DependencyEvent)).all() == []
    finally:
        session.close()
        transaction.rollback()
        connection.close()
