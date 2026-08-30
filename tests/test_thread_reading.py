"""The bounded conversation reader over routed inbound threads (ADR-0062)."""

from __future__ import annotations

from email.message import EmailMessage
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from corridor import email_intake, thread_reading
from corridor.config import settings
from corridor.db import Session, engine
from corridor.models import (
    Candidate,
    InboundMessage,
    InboundThreadReading,
    Project,
)


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    yield session
    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "store"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))


def receive(session, *, message_id, body, references=None, sender="utility@example.test"):
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "intake@corridor.test"
    message["Message-ID"] = message_id
    if references:
        message["References"] = references
        message["In-Reply-To"] = references.split()[-1]
    message.set_content(body)
    return email_intake.receive_message(
        session, raw_bytes=message.as_bytes(), service_address="intake@corridor.test"
    )


TURNS = (
    ("<size-1@example.test>", "CSJ A-100: the casing here — it's a 12 inch line."),
    ("<size-2@example.test>", "No, it's a 16 inch line."),
    ("<size-3@example.test>", "Actually maybe a 20 inch line."),
    ("<size-4@example.test>", "No, you're right, it has to be a 16 inch line."),
)


def routed_thread(session, turns=TURNS) -> int:
    project = Project(
        slug=f"threads-{uuid4().hex[:12]}", name="Thread Fixture", is_synthetic=True
    )
    session.add(project)
    session.flush()
    email_intake.register_project_identifier(
        session, project=project, kind="csj", value="A-100"
    )
    references = None
    thread_id = None
    for index, (message_id, body) in enumerate(turns):
        received = receive(
            session,
            message_id=message_id,
            body=body,
            references=references,
            sender=f"person{index}@example.test",
        )
        assert received.project_id == project.id
        thread_id = received.thread_id
        references = message_id
    return thread_id


class ArcRuntime:
    """A fake bounded runtime that reads every turn and returns one packet."""

    prompt_version = "thread-reading-fake-v1"
    model = None

    def __init__(self, packet_for=None):
        self.packet_for = packet_for

    def read(self, case, read_turn):
        texts = {turn.turn_ref: read_turn(turn.turn_ref) for turn in case.turns}
        if self.packet_for is not None:
            return self.packet_for(case, texts)
        # Default: the four-turn size argument concludes on its closing turn.
        return {
            "resolution": "concluded",
            "turn_context": [
                {"turn_ref": turn.turn_ref, "quote": texts[turn.turn_ref].strip()}
                for turn in case.turns
            ],
            "claim": {
                "turn_ref": case.closing_turn_ref,
                "quote": "it has to be a 16 inch line",
                "fields": {
                    "value": "16",
                    "description": "it has to be a 16 inch line",
                },
            },
        }


def test_four_turn_argument_yields_exactly_one_claim_cited_to_the_closing_turn(session):
    thread_id = routed_thread(session)
    candidates_before = session.scalar(select(func.count(Candidate.id)))

    outcome = thread_reading.read_thread(
        session, thread_id, runtime=ArcRuntime()
    )

    assert outcome.resolution == "concluded"
    assert session.scalar(select(func.count(Candidate.id))) == candidates_before + 1
    candidate = session.get(Candidate, outcome.candidate_id)
    closing = session.scalars(
        select(InboundMessage)
        .where(InboundMessage.thread_id == thread_id)
        .order_by(InboundMessage.id.desc())
    ).first()
    assert candidate.kind == "event"
    assert candidate.state == "pending"  # ordinary admission, nothing committed
    assert candidate.payload_json["fields"]["value"] == "16"
    citation = candidate.payload_json["citations"][0]
    assert citation["document_id"] == closing.document_id
    assert citation["verified"] is True
    assert "it has to be a 16 inch line" in citation["quote"]
    # Every turn is retained as context with its speaker from stored senders.
    reading = session.get(InboundThreadReading, outcome.reading_id)
    assert [entry["speaker"] for entry in reading.turn_context_json] == [
        "person0@example.test",
        "person1@example.test",
        "person2@example.test",
        "person3@example.test",
    ]
    assert reading.closing_message_id == closing.id

    # Re-reading the unchanged thread is idempotent: still exactly one claim.
    again = thread_reading.read_thread(session, thread_id, runtime=ArcRuntime())
    assert again.created is False
    assert again.candidate_id == outcome.candidate_id
    assert session.scalar(select(func.count(Candidate.id))) == candidates_before + 1


def test_unresolved_thread_yields_zero_claims_and_one_open_question(session):
    thread_id = routed_thread(session, TURNS[:3])

    def unresolved_packet(case, texts):
        return {
            "resolution": "unresolved",
            "turn_context": [
                {"turn_ref": turn.turn_ref, "quote": texts[turn.turn_ref].strip()}
                for turn in case.turns
            ],
            "open_question": (
                "Size under discussion — thread ended on "
                "'Actually maybe a 20 inch line.' — pin it down."
            ),
            "question_quote": "Actually maybe a 20 inch line.",
            "question_turn_ref": case.closing_turn_ref,
        }

    candidates_before = session.scalar(select(func.count(Candidate.id)))
    outcome = thread_reading.read_thread(
        session, thread_id, runtime=ArcRuntime(unresolved_packet)
    )
    assert outcome.resolution == "unresolved"
    assert outcome.candidate_id is None
    assert session.scalar(select(func.count(Candidate.id))) == candidates_before
    reading = session.get(InboundThreadReading, outcome.reading_id)
    assert "maybe a 20 inch line" in reading.open_question


def test_claim_must_cite_the_closing_turn_with_a_verified_quote(session):
    thread_id = routed_thread(session)

    def cites_earlier_turn(case, texts):
        return {
            "resolution": "concluded",
            "turn_context": [
                {"turn_ref": case.turns[0].turn_ref, "quote": texts[case.turns[0].turn_ref].strip()}
            ],
            "claim": {
                "turn_ref": case.turns[1].turn_ref,
                "quote": "it's a 16 inch line",
                "fields": {"value": "16"},
            },
        }

    with pytest.raises(thread_reading.ThreadReadingRefused):
        thread_reading.read_thread(
            session, thread_id, runtime=ArcRuntime(cites_earlier_turn)
        )

    def invents_a_quote(case, texts):
        return {
            "resolution": "concluded",
            "turn_context": [
                {"turn_ref": case.closing_turn_ref, "quote": "words nobody wrote"}
            ],
            "claim": {
                "turn_ref": case.closing_turn_ref,
                "quote": "words nobody wrote",
                "fields": {"value": "16"},
            },
        }

    with pytest.raises(thread_reading.ThreadReadingRefused):
        thread_reading.read_thread(
            session, thread_id, runtime=ArcRuntime(invents_a_quote)
        )
    assert (
        session.scalar(select(func.count(InboundThreadReading.id)))
        == 0
    )


def test_authority_shaped_output_and_budget_overruns_are_refused(session):
    thread_id = routed_thread(session)

    def authority_packet(case, texts):
        return {
            "resolution": "concluded",
            "turn_context": [
                {"turn_ref": case.closing_turn_ref, "quote": texts[case.closing_turn_ref].strip()}
            ],
            "claim": {
                "turn_ref": case.closing_turn_ref,
                "quote": "it has to be a 16 inch line",
                "fields": {"value": "16", "admit_immediately": "yes"},
            },
        }

    with pytest.raises(thread_reading.ThreadReadingRefused):
        thread_reading.read_thread(
            session, thread_id, runtime=ArcRuntime(authority_packet)
        )

    with pytest.raises(thread_reading.ThreadReadingBudgetExceeded):
        thread_reading.read_thread(
            session,
            thread_id,
            runtime=ArcRuntime(),
            budget=thread_reading.ThreadReadingBudget(max_turn_reads=2),
        )
    assert session.scalar(select(func.count(Candidate.id))) == 0
    assert session.scalar(select(func.count(InboundThreadReading.id))) == 0


def test_only_a_routed_thread_can_be_read(session):
    project = Project(
        slug=f"threads-{uuid4().hex[:12]}", name="Unrouted", is_synthetic=True
    )
    session.add(project)
    session.flush()
    received = receive(
        session,
        message_id="<unrouted@example.test>",
        body="nothing identifies a project here",
    )
    assert received.project_id is None
    with pytest.raises(thread_reading.ThreadReadingRefused):
        thread_reading.read_thread(
            session, received.thread_id, runtime=ArcRuntime()
        )
