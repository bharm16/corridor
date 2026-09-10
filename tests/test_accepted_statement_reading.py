"""Accepted source-neutral statements retain exact authority, scope and provenance."""

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone
from hashlib import sha256
import re

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from corridor.accepted_statement_reading import AcceptedStatementReadingRefused, read_native_statements
from corridor.db import engine
from corridor.fact_decisions import record_human_fact_decision
from corridor.facts import append_recorded_applies_to_fact, append_recorded_statement_timing_fact, append_recorded_statement_wording_fact
from corridor.models import Document, Fact, Project, ProjectRecordRevision, RecordedVerbalOrigin, SourceSegment
from corridor.principals import HumanPrincipal
from corridor.source_segments import recorded_verbal_statement_segment
from corridor.statement_values import StatementTiming


RECORDED_AT = datetime(2025, 3, 3, 14, 30, tzinfo=timezone.utc)


@pytest.fixture
def session():
    with engine.connect() as connection:
        transaction = connection.begin()
        with Session(bind=connection) as scoped:
            yield scoped
        if transaction.is_active:
            transaction.rollback()


@pytest.fixture
def verbal_facts(session):
    project = Project(slug="native-statement-reading", name="Native statement reading", is_synthetic=True)
    session.add(project)
    session.flush()
    words = "The utility expects work to finish around October."
    origin = RecordedVerbalOrigin(project_id=project.id, recorded_by="local:original-recorder",
        recorded_at=RECORDED_AT, conversation_date=date(2025, 3, 2), exact_text=words,
        content_sha256=sha256(words.encode()).hexdigest())
    session.add(origin)
    session.flush()
    segment = recorded_verbal_statement_segment(project_id=project.id, recorded_verbal_origin_id=origin.id, exact_text=words)
    session.add(segment)
    session.flush()
    subject = "statement:native-fixture"
    wording = append_recorded_statement_wording_fact(session, segment=segment, subject_key=subject,
        description=words, recorded_by=origin.recorded_by)
    timing = append_recorded_statement_timing_fact(session, segment=segment, subject_key=subject,
        timings=(("new", StatementTiming.approximate("around October")),), recorded_by=origin.recorded_by)
    scope = append_recorded_applies_to_fact(session, segment=segment, subject_key=subject,
        dependency_ids=(), recorded_by=origin.recorded_by)
    return project, origin, wording, timing, scope


def decide(session, fact, actor, key, *, predecessor=None, disposition="include"):
    return record_human_fact_decision(session, fact, principal=HumanPrincipal(actor),
        command_type="coordinate_statement" if disposition == "include" else "mark_do_not_add" if disposition == "do_not_add" else "restore_do_not_add",
        idempotency_key=key, expected_predecessor=predecessor, disposition=disposition)


def test_verbal_fields_keep_distinct_decisions_original_recorder_and_unknown_scope(session, verbal_facts):
    project, origin, wording, timing, scope = verbal_facts
    words_decision = decide(session, wording, "local:wording-owner", "words")
    timing_decision = decide(session, timing, "local:timing-owner", "timing")
    scope_decision = decide(session, scope, "local:scope-owner", "scope")
    [statement] = read_native_statements(session, project.id, scope_decision.revision.id)
    assert statement.subject_key == wording.subject_key
    assert statement.wording == wording.text_value
    assert statement.fields["statement_wording"].decision_id == words_decision.decision.id
    assert statement.fields["statement_wording"].actor == "local:wording-owner"
    assert statement.fields["statement_timing"].revision_id == timing_decision.revision.id
    assert statement.fields["statement_timing"].actor == "local:timing-owner"
    assert statement.fields["applies_to"].actor == "local:scope-owner"
    assert statement.fields["statement_wording"].decided_at == words_decision.decision.decided_at
    assert statement.applies_to_mode == "unknown"
    assert statement.applies_to_subject_keys == statement.applies_to_dependency_ids == ()
    assert statement.timings[0].text == "around October" and statement.timings[0].precision == "approximate"
    assert statement.timings[0].start_date is statement.timings[0].end_date is None
    assert not hasattr(statement, "published_promised_for") and not hasattr(statement, "ready")
    assert all(source.recorded_verbal_origin_id == origin.id and source.recorded_by == origin.recorded_by
               and source.recorded_at == RECORDED_AT and source.conversation_date == origin.conversation_date
               and source.document_id is None and source.locator_validation_status == "valid" for source in statement.sources)
    with pytest.raises(FrozenInstanceError):
        statement.subject_key = "changed"
    with pytest.raises(TypeError):
        statement.fields["statement_wording"] = None
    with pytest.raises(TypeError):
        statement.fields["statement_timing"].value["timings"][0]["text"] = "invented exact date"


def test_do_not_add_and_restore_do_not_invent_acceptance_or_rewrite_as_of(session, verbal_facts):
    project, _, wording, _, _ = verbal_facts
    included = decide(session, wording, "local:accepting-person", "include")
    before = read_native_statements(session, project.id, included.revision.id)
    blocked = decide(session, wording, "local:excluding-person", "exclude", predecessor=included.decision.id, disposition="do_not_add")
    assert read_native_statements(session, project.id, blocked.revision.id) == ()
    assert read_native_statements(session, project.id, included.revision.id) == before
    restored = decide(session, wording, "local:restoring-person", "restore", predecessor=blocked.decision.id, disposition="restore")
    assert read_native_statements(session, project.id, restored.revision.id) == ()
    accepted = decide(session, wording, "local:new-accepting-person", "accept-again", predecessor=restored.decision.id)
    [current] = read_native_statements(session, project.id, accepted.revision.id)
    assert current.fields["statement_wording"].actor == "local:new-accepting-person"
    assert read_native_statements(session, project.id, included.revision.id) == before


def test_retired_documentary_locator_keeps_accepted_words_and_explicit_replay_blocker(session):
    project = Project(slug="retained-statement-source", name="Retained source", is_synthetic=True)
    session.add(project)
    session.flush()
    document = Document(project_id=project.id, sha256="d"*64, filename="historical-minutes.pdf", doc_type="minutes", pages=1, parse_status="parsed")
    session.add(document)
    session.flush()
    words = "The utility will submit its plan in October."
    segment = SourceSegment(project_id=project.id, document_id=document.id, kind="prose_span", exact_text=words,
        content_sha256=sha256(words.encode()).hexdigest(), ordinal=1, page_no=1, start_offset=0, end_offset=len(words))
    session.add(segment)
    session.flush()
    fact = append_recorded_statement_wording_fact(session, segment=segment, subject_key="statement:retained", description=words, recorded_by="local:original-person")
    result = decide(session, fact, "local:accepting-person", "retain-words")
    [reading] = read_native_statements(session, project.id, result.revision.id)
    assert reading.wording == words
    assert reading.applies_to_mode == "not_recorded"
    assert all(s.document_id == document.id and s.page_no == 1 and s.locator_validation_status == "not_re_readable" for s in reading.sources)
    assert any("retired prose reader" in blocker for blocker in reading.coverage_blockers)
    assert reading.fields["statement_wording"].actor == "local:accepting-person"


def test_email_statement_reader_uses_native_sources_without_legacy_population_queries(session):
    from test_email_spine import deliver, fixture_client, message_bytes
    from corridor.email_spine import capture_email_thread
    from corridor.support_assessments import FactProposition, record_support_assessment
    from corridor.models import FactSource
    project, envelope = deliver(session, message_bytes(body="We will finish in October.\n"))
    captured = capture_email_thread(session, envelope, client=fixture_client())
    fact = session.get(Fact, captured.source_fact_id)
    segment_ids = tuple(session.scalars(select(FactSource.source_segment_id).where(FactSource.fact_id == fact.id)))
    assessment = record_support_assessment(session, project_id=project.id, proposition=FactProposition(fact.id),
        source_segment_ids=tuple(dict.fromkeys(segment_ids)), evidence_role="value_support", assessment="supported", authority=HumanPrincipal("local:source-reader"))
    accepted = decide(session, fact, "local:email-owner", "email-accepted")
    forbidden = re.compile(r"\b(?:dependencies|dependency_events|candidates|evidence_links|work_decisions|operative_support)\b", re.I)
    def inspect_sql(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().lower().startswith("select"):
            assert not forbidden.search(statement), statement
    event.listen(session.get_bind(), "before_cursor_execute", inspect_sql)
    try:
        [reading] = read_native_statements(session, project.id, accepted.revision.id)
    finally:
        event.remove(session.get_bind(), "before_cursor_execute", inspect_sql)
    assert reading.wording == "We will finish in October.\n"
    assert all(source.kind == "email_span" and source.locator_validation_status == "valid" for source in reading.sources)
    assert [a.assessment_id for a in reading.fields["statement_wording"].assessments] == [assessment.id]
    assert not session.new and not session.dirty and not session.deleted


def test_minutes_statement_is_separate_from_adopted_ucm_subjects(session):
    from minutes_fixture_support import MinutesClient, adopted_project, minutes_document
    from test_minutes_spine import accept_statement
    from corridor.minutes_spine import capture_minutes
    project = adopted_project(session)
    document = minutes_document(session, project, "Action Items:\n1. Utility A: UC-1 work will finish in September 2026.")
    captured = capture_minutes(session, document, client=MinutesClient(), source_family="statement-reader")
    subject = accept_statement(session, project, captured)
    boundary = session.scalar(select(ProjectRecordRevision.id).where(ProjectRecordRevision.project_id == project.id).order_by(ProjectRecordRevision.id.desc()).limit(1))
    [reading] = read_native_statements(session, project.id, boundary)
    assert reading.subject_key == subject
    assert reading.timings[0].precision == "month"
    assert reading.timings[0].start_date == date(2026, 9, 1)
    assert reading.timings[0].end_date == date(2026, 9, 30)
    assert reading.applies_to_subject_keys and reading.applies_to_dependency_ids == ()
    assert reading.applies_to_mode == "selected"
    assert any(source.document_id == document.id for source in reading.sources)


def test_wrong_project_revision_is_refused_before_reading_any_statement(session, verbal_facts):
    project, _, wording, _, _ = verbal_facts
    accepted = decide(session, wording, "local:alice", "original")
    other = Project(slug="other-statement-project", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    with pytest.raises(AcceptedStatementReadingRefused, match="does not belong"):
        read_native_statements(session, other.id, accepted.revision.id)


def test_selected_legacy_scope_ids_remain_separate_from_native_subject_keys(session, verbal_facts):
    from corridor.models import Dependency, FactSource
    project, _, wording, _, _ = verbal_facts
    constraint = Dependency(project_id=project.id, ref_code="legacy-scope", dep_type="utility_relocation", title="Legacy scope identity")
    session.add(constraint)
    session.flush()
    source_id = session.scalar(select(FactSource.source_segment_id).where(FactSource.fact_id == wording.id, FactSource.role == "value_source"))
    scoped = append_recorded_applies_to_fact(session, segment=session.get(SourceSegment, source_id),
        subject_key=wording.subject_key, dependency_ids=(constraint.id,), recorded_by="local:scope-recorder")
    decide(session, wording, "local:wording-owner", "scope-wording")
    accepted = decide(session, scoped, "local:scope-owner", "selected-scope")
    [reading] = read_native_statements(session, project.id, accepted.revision.id)
    assert reading.applies_to_mode == "selected"
    assert reading.applies_to_dependency_ids == (constraint.id,)
    assert reading.applies_to_subject_keys == ()
    assert reading.fields["statement_wording"].fact_subject_key == wording.subject_key


@pytest.mark.parametrize("alteration", ["wording", "timing", "scope", "omission"])
def test_shared_snapshot_cannot_forge_payloads_or_omit_accepted_statements(session, verbal_facts, alteration):
    from dataclasses import replace
    from corridor.record_projection import read_native_record_values
    project, _, wording, timing, scope = verbal_facts
    decide(session, wording, "local:wording-owner", "snapshot-words")
    decide(session, timing, "local:timing-owner", "snapshot-timing")
    boundary = decide(session, scope, "local:scope-owner", "snapshot-scope").revision.id
    actual = read_native_record_values(session, project.id, boundary)
    assert read_native_statements(session, project.id, boundary, values=actual) == read_native_statements(session, project.id, boundary)
    if alteration == "omission":
        supplied = ()
    else:
        target_type = {"wording": "statement_wording", "timing": "statement_timing", "scope": "applies_to"}[alteration]
        original = next(value for value in actual if value.fact_type == target_type)
        if alteration == "wording":
            changed = replace(original, text_value="Invented accepted wording")
        elif alteration == "timing":
            changed = replace(original, statement_timings=(replace(original.statement_timings[0], text="an invented date"),))
        else:
            changed = replace(original, applies_to_subject_keys=("invented-constraint",))
        supplied = tuple(changed if value.decision_id == original.decision_id else value for value in actual)
    with pytest.raises(AcceptedStatementReadingRefused, match="complete native snapshot"):
        read_native_statements(session, project.id, boundary, values=supplied)
