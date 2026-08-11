import pytest
from sqlalchemy import select

from corridor.db import Session, engine
from corridor.extract_minutes import PROMPT_VERSION, extract_document
from corridor.models import Candidate, Dependency, DocPage, Document, Project

PAGE_TEXT = (
    "Air Liquide Meeting Notes\n"
    "TxDOT - SH 99 / Grand Parkway Segment B-1 Project\n"
    "Date and Time: Monday, August 12, 2024, 1:00 PM-1:30 PM\n\n"
    "Conflicts\n"
    "PL41   STA 6685+06 200 RT to STA 6686+08 200 LT\n"
    "PL42 (Leased by Air Liquide)\n"
    "Same as PL41   STA 6684+80 200 RT to STA 6685+75 200 LT\n\n"
    "Topics Discussed:\n"
    "1. Project Timeline and Deadlines -\n"
    "   a. Design Completion - 01/2025\n"
    "   b. ROW, Util Agreement, Execution - 7/2025\n"
    "2. Discussion on Conflict Resolution Strategies -\n"
    "   a. PL41 - protect-in-place.\n"
)


class StubClient:
    def __init__(self, responses, model="gpt-5.6-luna"):
        self.responses = list(responses)
        self.model = model
        self.calls = []

    def complete(self, *, system, user, schema):
        self.calls.append({"system": system, "user": user, "schema": schema})
        return self.responses.pop(0) if self.responses else {"events": []}


def event(**over):
    base = {
        "event_type": "status_change",
        "description": "PL41 to be protected in place rather than relocated",
        "event_date": "2024-08-12",
        "external_org": "Air Liquide",
        "conflict_ref": "PL41",
        "station_from": "6685+06",
        "station_to": "6686+08",
        "committed_date": None,
        "quote": "PL41 - protect-in-place.",
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
    project = Project(slug="min-test", name="Minutes Test", is_synthetic=True)
    session.add(project)
    session.flush()
    doc = Document(
        project_id=project.id,
        sha256="m" * 64,
        filename="Meeting Notes/Air Liquide/2024.08.12 notes final.pdf",
        doc_type="minutes",
        parse_status="parsed",
        pages=1,
    )
    session.add(doc)
    session.flush()
    session.add(
        DocPage(
            document_id=doc.id, page_no=1, text=PAGE_TEXT, text_source="text_layer"
        )
    )
    session.flush()
    return doc


def test_an_event_becomes_a_verified_candidate(session, document):
    client = StubClient([{"events": [event()]}])
    [candidate] = extract_document(session, document, client=client)

    assert candidate.citations_verified is True
    assert candidate.payload_json["fields"]["conflict_ref"] == "PL41"
    assert candidate.payload_json["fields"]["station_from"] == "6685+06"


def test_minutes_produce_events_not_dependencies(session, document):
    """These notes discuss conflicts that already exist in the matrix.

    Emitting a dependency would create the duplicate that merge exists to
    prevent.
    """
    client = StubClient([{"events": [event()]}])
    [candidate] = extract_document(session, document, client=client)

    assert candidate.kind == "event"
    assert candidate.payload_json["kind"] == "event"
    assert not session.scalars(
        select(Dependency).where(Dependency.project_id == document.project_id)
    ).all()


def test_the_dedupe_hint_carries_party_conflict_and_stationing(session, document):
    """Merge blocks on the resolved party, then scores on stationing."""
    client = StubClient([{"events": [event()]}])
    [candidate] = extract_document(session, document, client=client)
    hint = candidate.payload_json["dedupe_hint"]
    assert "Air Liquide" in hint
    assert "PL41" in hint
    assert "6685+06" in hint


def test_the_page_number_comes_from_us(session, document):
    client = StubClient([{"events": [event()]}])
    [candidate] = extract_document(session, document, client=client)
    assert candidate.payload_json["citations"][0]["page"] == 1
    assert "page" not in event()


def test_a_fabricated_quote_is_kept_but_unverified(session, document):
    client = StubClient(
        [{"events": [event(quote="PL41 will be relocated by December 2025")]}]
    )
    [candidate] = extract_document(session, document, client=client)
    assert candidate.citations_verified is False


def test_an_unknown_event_type_is_dropped(session, document):
    """The schema constrains this, but a model can still return junk."""
    client = StubClient([{"events": [event(event_type="meeting_happened")]}])
    assert extract_document(session, document, client=client) == []


def test_an_event_without_a_description_is_dropped(session, document):
    client = StubClient([{"events": [event(description="")]}])
    assert extract_document(session, document, client=client) == []


def test_an_event_without_a_quote_is_dropped(session, document):
    client = StubClient([{"events": [event(quote="  ")]}])
    assert extract_document(session, document, client=client) == []


def test_nulls_are_omitted_rather_than_stored_empty(session, document):
    client = StubClient(
        [{"events": [event(conflict_ref=None, committed_date=None)]}]
    )
    [candidate] = extract_document(session, document, client=client)
    fields = candidate.payload_json["fields"]
    assert "conflict_ref" not in fields
    assert "committed_date" not in fields


def test_prompt_version_and_model_are_recorded(session, document):
    client = StubClient([{"events": [event()]}])
    [candidate] = extract_document(session, document, client=client)
    assert candidate.prompt_version == PROMPT_VERSION
    assert candidate.model == "gpt-5.6-luna"


def test_the_schema_restricts_event_type_to_the_ledgers_own_enum(session, document):
    from corridor.models import EVENT_TYPES

    client = StubClient([{"events": []}])
    extract_document(session, document, client=client)
    enum = client.calls[0]["schema"]["properties"]["events"]["items"]["properties"][
        "event_type"
    ]["enum"]
    assert set(enum) == set(EVENT_TYPES)


def test_an_empty_response_is_not_a_crash(session, document):
    assert extract_document(session, document, client=StubClient([{}])) == []


# --------------------------------------------------------------- concurrency


def test_complete_many_preserves_input_order():
    """Results are zipped back onto (document, page) pairs by position."""
    from corridor.llm import complete_many

    class Echo:
        max_workers = 4

        def complete(self, *, system, user, schema):
            return {"events": [], "echo": user}

    users = [f"page {i}" for i in range(20)]
    results = complete_many(Echo(), system="s", schema={}, users=users, max_workers=4)
    assert [r.value["echo"] for r in results] == users


def test_one_failing_call_does_not_kill_the_batch():
    """A single bad page costs that page, not the run."""
    from corridor.llm import complete_many

    class Flaky:
        max_workers = 4

        def complete(self, *, system, user, schema):
            if user == "page 3":
                raise RuntimeError("429 forever")
            return {"events": []}

    results = complete_many(
        Flaky(), system="s", schema={}, users=[f"page {i}" for i in range(6)],
        max_workers=4,
    )
    assert results[3].failed
    assert "429 forever" in results[3].error
    assert sum(1 for r in results if r.failed) == 1


def test_batched_extraction_pools_pages_across_documents(session, document):
    """A meeting note is often one page, so a per-document pool would have
    nothing to parallelise."""
    from corridor.extract_batch import extract_documents
    from corridor.extract_minutes import _to_candidate
    from corridor.models import Document, DocPage

    second = Document(
        project_id=document.project_id,
        sha256="n" * 64,
        filename="Meeting Notes/Chevron/2024.08.13 notes final.pdf",
        doc_type="minutes",
        parse_status="parsed",
        pages=1,
    )
    session.add(second)
    session.flush()
    session.add(
        DocPage(document_id=second.id, page_no=1, text=PAGE_TEXT, text_source="text_layer")
    )
    session.flush()

    client = StubClient([{"events": [event()]}, {"events": [event()]}])
    created = extract_documents(
        session,
        [document, second],
        client=client,
        system="s",
        schema=SCHEMA_STUB,
        min_page_chars=200,
        to_candidate=_to_candidate,
        items_key="events",
        max_workers=4,
        prompt_version="minutes_v1",
        commit=False,
    )
    # Both documents' pages went out in one pooled batch.
    assert len(client.calls) == 2
    assert len(created) == 2
    assert {c.source_document_id for c in created} == {document.id, second.id}


def test_already_extracted_lets_a_killed_run_resume(session, document):
    """Without this a restart duplicates every document already finished."""
    from corridor.extract_batch import already_extracted, extract_documents
    from corridor.extract_minutes import PROMPT_VERSION, _to_candidate
    from corridor.models import Candidate

    assert already_extracted(session, document.project_id, PROMPT_VERSION) == set()

    client = StubClient([{"events": [event()]}])
    extract_documents(
        session,
        [document],
        client=client,
        system="s",
        schema=SCHEMA_STUB,
        min_page_chars=200,
        to_candidate=_to_candidate,
        items_key="events",
        prompt_version=PROMPT_VERSION,
        commit=False,
    )

    assert already_extracted(session, document.project_id, PROMPT_VERSION) == {
        document.id
    }
    # A different prompt version is a different run and is not skipped.
    assert already_extracted(session, document.project_id, "minutes_v99") == set()

    session.add(
        Candidate(
            project_id=document.project_id,
            kind="event",
            payload_json={"kind": "event", "fields": {"description": "legacy only"}},
            source_document_id=document.id,
            source_pages=[1],
            confidence=1.0,
            prompt_version="minutes_v99",
            model="stub",
            citations_verified=True,
        )
    )
    session.flush()
    assert already_extracted(session, document.project_id, "minutes_v99") == set()


SCHEMA_STUB = {"type": "object"}
