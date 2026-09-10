"""The five minutes capabilities through the source capture/reading seam (#456)."""

from corridor.statement_timing_parser import statement_timing_options
from datetime import datetime, timezone
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from corridor.config import settings
from corridor.db import engine
from corridor.models import Fact, FactSource, ProjectRecordRevision
from minutes_fixture_support import MinutesClient, adopted_project, minutes_document


def test_minutes_timing_options_preserve_exact_month_and_range_precision():
    options = statement_timing_options("September 2026; 2026-10-01 to 2026-10-03; 10/15/2026")
    assert [item.value for item in options] == [
        {"text": "September 2026", "precision": "month", "start_date": "2026-09-01", "end_date": "2026-09-30"},
        {"text": "2026-10-01 to 2026-10-03", "precision": "range", "start_date": "2026-10-01", "end_date": "2026-10-03"},
        {"text": "10/15/2026", "precision": "day", "start_date": "2026-10-15", "end_date": "2026-10-15"},
    ]


def test_a_revised_date_is_not_misread_as_a_range_and_qualifiers_stay_approximate():
    options = statement_timing_options("The date changed from September 2026 to October 2026.")
    assert [option.value["precision"] for option in options] == ["month", "month"]
    assert options[-1].value["text"] == "October 2026"
    [approximate] = statement_timing_options("around October 2026")
    assert approximate.value == {"text": "around October 2026", "precision": "approximate", "start_date": None, "end_date": None}


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "store"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))


@pytest.fixture
def session():
    with engine.connect() as connection:
        transaction = connection.begin()
        with Session(bind=connection) as session:
            yield session
        transaction.rollback()


def accept_statement(session, project, capture):
    from corridor.packet_review import FocusedAnswer, focused_request, packet_request, read_review_items
    from corridor.review_packets import APPLY, resolve_review_packet
    from corridor.principals import HumanPrincipal
    from corridor.support_assessments import FactProposition, record_support_assessment

    outcome = next(row for row in capture.output_json["outcomes"] if row["status"] == "captured")
    principal = HumanPrincipal("local:minutes-owner")
    assessments = []
    for fact_id in outcome["fact_ids"]:
        sources = session.scalars(select(FactSource.source_segment_id).where(FactSource.fact_id == fact_id)).all()
        assessments.append(record_support_assessment(session, project_id=project.id,
            proposition=FactProposition(fact_id), source_segment_ids=tuple(dict.fromkeys(sources)),
            evidence_role="value_support", assessment="supported", authority=principal).id)
    now = datetime.now(timezone.utc)
    reading = read_review_items(session, project_id=project.id, as_of=now)
    item = next(item for item in reading.items if outcome["delta_ids"][0] in item.actionable.delta_ids)
    child = next(child for child in item.children if child.delta_id == outcome["delta_ids"][0])
    assert child.not_ready_reason is None
    request = (focused_request(reading, item, principal=principal, decided_at=now,
        answers=(FocusedAnswer(child.delta_id, APPLY),)) if item.focused else
        packet_request(reading, item, outcome=APPLY, principal=principal, decided_at=now,
                       delta_ids=(child.delta_id,)))
    assert {effect.fact_id for effect in request.children[0].record_effects} == set(outcome["fact_ids"])
    result = resolve_review_packet(session, request)
    assert result.status == "saved", result.refusals
    return outcome["subject_key"]


def test_minutes_commitment_timing_change_and_completion_are_cited_unaccepted_facts(session):
    from corridor.minutes_spine import capture_minutes, inspect_minutes
    import json
    from corridor.facts import replay_fact
    from corridor.storage import stored_file
    from corridor.review_packet_reading import open_deltas
    from corridor.record_projection import read_current_project_record

    project = adopted_project(session)
    first = minutes_document(session, project, "Meeting: Weekly update\nAction Items:\n1. Utility A: UC-1 work will finish in September 2026.")
    captured = capture_minutes(session, first, client=MinutesClient(), source_family="meeting-1")
    assert any(row["status"] == "captured" for row in captured.output_json["outcomes"]), json.dumps(inspect_minutes(session, first))
    outcome = next(row for row in captured.output_json["outcomes"] if row["status"] == "captured")
    assert outcome["scope_state"] == "selected"
    facts = [session.get(Fact, identifier) for identifier in outcome["fact_ids"]]
    assert {fact.fact_type for fact in facts} == {"statement_wording", "statement_timing", "applies_to"}
    for fact in facts:
        replay_fact(session, first, fact, stored_file(first))
    assert capture_minutes(session, first, client=MinutesClient(), source_family="meeting-1").id == captured.id
    subject = accept_statement(session, project, captured)
    accepted_before = read_current_project_record(session, project.id)

    changed_doc = minutes_document(session, project, "Action Items:\n1. Utility A: UC-1 work moves to October 2026.")
    changed = capture_minutes(session, changed_doc, client=MinutesClient("timing_change"), source_family="meeting-1")
    changed_row = next(row for row in changed.output_json["outcomes"] if row["status"] == "captured")
    assert changed_row["predecessor_subject_key"] == subject
    assert read_current_project_record(session, project.id) == accepted_before

    changed_ids = set(changed_row["delta_ids"])
    assert changed_ids

    completion_doc = minutes_document(session, project, "Action Items:\n1. Utility A: UC-1 work is complete.")
    completion = capture_minutes(session, completion_doc, client=MinutesClient("completion_report"), source_family="meeting-1")
    completed_row = next(row for row in completion.output_json["outcomes"] if row["status"] == "captured")
    closure = next(session.get(Fact, identity) for identity in completed_row["fact_ids"] if session.get(Fact, identity).fact_type == "closure_result")
    assert replay_fact(session, completion_doc, closure, stored_file(completion_doc)).closure_kind == "completion_reported"
    assert changed_ids.isdisjoint({delta.id for delta in open_deltas(session, project_id=project.id)})
    assert read_current_project_record(session, project.id) == accepted_before


@pytest.mark.parametrize("text,reason", [
    ("Project Team: UC-1 work will finish in September 2026.", "project_side_speaker"),
    ("Unknown Organization: UC-1 work will finish in September 2026.", "attribution_unresolved"),
    ("> Utility A: UC-1 work will finish in September 2026.", "quoted_history"),
    ("DRAFT\nUtility A: UC-1 work will finish in September 2026.", "draft_source"),
])
def test_non_authoritative_minutes_stay_source_work_without_facts(session, text, reason):
    from corridor.minutes_spine import capture_minutes

    project = adopted_project(session)
    document = minutes_document(session, project, text)
    captured = capture_minutes(session, document, client=MinutesClient())
    assert any(reason in row["reasons"] for row in captured.output_json["outcomes"])
    assert session.scalars(select(Fact).where(Fact.document_id == document.id)).all() == []


def test_unknown_scope_is_read_only_context_on_the_native_work_list(session):
    from corridor.minutes_spine import capture_minutes
    from corridor.packet_review import read_review_items
    from corridor.project_workflow import read_project_workflow
    from datetime import timedelta

    project = adopted_project(session)
    document = minutes_document(session, project, "Utility A: Work will finish in September 2026.")
    receipt = capture_minutes(session, document, client=MinutesClient(scope=False))
    as_of = receipt.recorded_at + timedelta(seconds=1)
    reading = read_review_items(session, project_id=project.id, as_of=as_of)
    assert any("Applies To: not yet known" in child.source_attention_reasons for item in reading.items for child in item.children)
    assert read_project_workflow(session, project_id=project.id, as_of=as_of).review == reading
    from pathlib import Path
    template = Path("src/corridor/web/templates/review.html").read_text()
    assert 'name="scope_mode"' not in template


def test_ambiguous_speakers_and_contradictory_turns_remain_questions(session):
    from corridor.minutes_spine import capture_minutes
    from corridor.models import ExternalOrg
    from corridor.packet_review import read_review_items
    from corridor.project_workflow import read_project_workflow
    from datetime import timedelta

    project = adopted_project(session)
    session.add(ExternalOrg(name="Utility B"))
    session.flush()
    document = minutes_document(session, project, "Utility A / Utility B: UC-1 work will finish in September 2026.")
    receipt = capture_minutes(session, document, client=MinutesClient())
    as_of = receipt.recorded_at + timedelta(seconds=1)
    reading = read_review_items(session, project_id=project.id, as_of=as_of)
    assert reading.source_questions
    assert read_project_workflow(session, project_id=project.id, as_of=as_of).section("review").waiting
    other = minutes_document(session, project, "Utility A: UC-1 work will finish in September 2026.\nUtility A: Maybe October 2026 instead.")
    unresolved = capture_minutes(session, other, client=MinutesClient("unresolved"))
    assert not any(row["fact_ids"] for row in unresolved.output_json["outcomes"])


def test_multiple_predecessors_cannot_be_resolved_by_model_ranking(session):
    from corridor.minutes_spine import capture_minutes

    project = adopted_project(session)
    for family, words in (("fieldwork", "field work"), ("plans", "submit plans")):
        document = minutes_document(session, project, f"Utility A: UC-1 {words} in September 2026.")
        accept_statement(session, project, capture_minutes(session, document, client=MinutesClient(), source_family=family))
    later = minutes_document(session, project, "Utility A: UC-1 work moves to October 2026.")
    receipt = capture_minutes(session, later, client=MinutesClient("timing_change"))
    assert any("predecessor_unresolved" in row["reasons"] for row in receipt.output_json["outcomes"])
    assert not any(row["fact_ids"] for row in receipt.output_json["outcomes"])


def test_minutes_range_and_normal_pipeline_preserve_both_endpoints(session):
    from corridor.pipeline import extract_any
    from corridor.facts import replay_fact
    from corridor.storage import stored_file

    project = adopted_project(session)
    document = minutes_document(session, project, "Utility A: UC-1 field work from 2026-10-01 to 2026-10-03.")
    assert extract_any(session, document, client=MinutesClient()) == []
    timing = session.scalar(select(Fact).where(Fact.document_id == document.id, Fact.fact_type == "statement_timing"))
    replayed = replay_fact(session, document, timing, stored_file(document))
    assert replayed.timings[0][1].precision == "range"
    assert replayed.timings[0][1].start_date.isoformat() == "2026-10-01"
    assert replayed.timings[0][1].end_date.isoformat() == "2026-10-03"


def test_minutes_crash_retry_preserves_one_capture(runtime_database):
    from corridor.minutes_spine import capture_minutes
    from corridor.models import Document, MinutesCapture

    factory = runtime_database.session_factory
    with factory() as setup:
        project = adopted_project(setup)
        document = minutes_document(setup, project, "Utility A: UC-1 work will finish in September 2026.")
        document_id = document.id
        setup.commit()
    with factory() as failed:
        capture_minutes(failed, failed.get(Document, document_id), client=MinutesClient())
        failed.rollback()
    with factory() as resumed:
        captured = capture_minutes(resumed, resumed.get(Document, document_id), client=MinutesClient())
        capture_id = captured.id
        resumed.commit()
    with factory() as replay:
        assert capture_minutes(replay, replay.get(Document, document_id), client=MinutesClient()).id == capture_id
        assert len(replay.scalars(select(MinutesCapture).where(MinutesCapture.document_id == document_id)).all()) == 1
