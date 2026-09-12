"""Every source question has a permitted disposition or clarification path (#833).

The reason-to-action matrix is recorded on the ticket; these prove it. A Row-2
resolution re-enters the existing capture+comparison path and produces the
Proposed Delta the source would have produced; a Row-3 interpretation records
that the source makes no such assertion and never fabricates one; a Row-4
clarification retains the question without settling it; a Row-5 exclusion
records it out of scope. None moves the accepted record, none rewrites the
immutable capture outcome, and there is no generic Dismiss.
"""

import copy
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from corridor.config import settings
from corridor.minutes_question_disposition import (
    DispositionRequest,
    MinutesQuestionDispositionRefused,
    read_question_dispositions,
    record_question_disposition,
)
from corridor.minutes_reading import read_minutes_work
from corridor.minutes_spine import QuestionResolution, capture_minutes, inspect_minutes
from corridor.models import Fact, FactSource, MinutesQuestionDisposition, ProjectRecordRevision
from corridor.principals import HumanPrincipal
from corridor.review_packet_reading import open_deltas
from minutes_fixture_support import MinutesClient, adopted_project, minutes_document


OWNER = HumanPrincipal("local:minutes-owner")


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "store"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))


def _as_of(capture):
    return capture.recorded_at + timedelta(seconds=1)


def _questions(session, project, capture):
    questions, _ = read_minutes_work(session, project_id=project.id, as_of=_as_of(capture))
    return questions


def only_question(session, project, capture):
    questions = _questions(session, project, capture)
    assert len(questions) == 1, [q.reason_codes for q in questions]
    return questions[0]


def _revisions(session, project):
    return session.scalars(
        select(ProjectRecordRevision.id).where(
            ProjectRecordRevision.project_id == project.id
        )
    ).all()


def accept_statement(session, project, capture):
    """Accept one captured minutes commitment, so a later report has a predecessor."""

    from corridor.packet_review import FocusedAnswer, focused_request, packet_request, read_review_items
    from corridor.review_packets import APPLY, resolve_review_packet
    from corridor.support_assessments import FactProposition, record_support_assessment

    outcome = next(row for row in capture.output_json["outcomes"] if row["status"] == "captured")
    for fact_id in outcome["fact_ids"]:
        sources = session.scalars(select(FactSource.source_segment_id).where(FactSource.fact_id == fact_id)).all()
        record_support_assessment(session, project_id=project.id, proposition=FactProposition(fact_id),
            source_segment_ids=tuple(dict.fromkeys(sources)), evidence_role="value_support",
            assessment="supported", authority=OWNER)
    now = datetime.now(timezone.utc)
    reading = read_review_items(session, project_id=project.id, as_of=now)
    item = next(item for item in reading.items if outcome["delta_ids"][0] in item.delta_ids)
    child = next(child for child in item.children if child.delta_id == outcome["delta_ids"][0])
    request = (focused_request(reading, item, principal=OWNER, decided_at=now,
        answers=(FocusedAnswer(child.delta_id, APPLY),)) if item.focused else
        packet_request(reading, item, outcome=APPLY, principal=OWNER, decided_at=now, delta_ids=(child.delta_id,)))
    assert resolve_review_packet(session, request).status == "saved"


def test_interpreting_non_explicit_completion_never_becomes_a_completion(session):
    project = adopted_project(session)
    first = minutes_document(session, project, "Utility A: UC-1 work will finish in September 2026.")
    accept_statement(session, project, capture_minutes(session, first, client=MinutesClient(), source_family="m1"))

    report = minutes_document(session, project, "Utility A: UC-1 work is progressing well.")
    capture = capture_minutes(session, report, client=MinutesClient("completion_report"), source_family="m1")
    question = only_question(session, project, capture)
    assert question.reason_codes == ("completion_not_explicit",)
    assert "resolve" not in question.permitted_actions

    with pytest.raises(MinutesQuestionDispositionRefused):
        record_question_disposition(
            session,
            _request(question, project, disposition="resolve",
                     resolution=QuestionResolution(resolves=frozenset({"completion_not_explicit"}))),
            principal=OWNER,
        )

    revisions_before = _revisions(session, project)
    record_question_disposition(
        session,
        _request(question, project, disposition="interpret",
                 interpretation="The source reports progress, not completion."),
        principal=OWNER,
    )
    assert _questions(session, project, capture) == ()
    assert not open_deltas(session, project_id=project.id)
    assert _revisions(session, project) == revisions_before, "interpreting recorded a completion"


def _request(question, project, **overrides):
    fields = dict(
        project_id=project.id,
        capture_id=question.capture_id,
        source_family=question.source_family,
        segment_id=question.segment_id,
        question_identity=question.question_id,
        reason_code=question.reason_codes[0],
        disposition="interpret",
        decision_generation=question.decision_generation,
    )
    fields.update(overrides)
    return DispositionRequest(**fields)


def test_interpreting_required_by_records_that_the_source_states_no_promised_timing(session):
    project = adopted_project(session)
    document = minutes_document(session, project, "Utility A: UC-1 work is required by September 2026.")
    capture = capture_minutes(session, document, client=MinutesClient(timing_purpose="required_by"), source_family="m1")

    question = only_question(session, project, capture)
    assert question.reason_codes == ("required_by_is_not_promised_timing",)
    # Row 3: the coordinator may not turn Required By into Promised For.
    assert "resolve" not in question.permitted_actions
    assert question.permitted_actions[0] == "interpret"

    before = copy.deepcopy(capture.output_json)
    revisions_before = _revisions(session, project)

    record_question_disposition(
        session,
        _request(question, project, disposition="interpret",
                 interpretation="A Required By date, not the party's Promised timing."),
        principal=OWNER,
    )

    assert _questions(session, project, capture) == ()
    session.refresh(capture)
    assert capture.output_json == before, "the immutable capture outcome was rewritten"
    assert not open_deltas(session, project_id=project.id)
    assert _revisions(session, project) == revisions_before


def test_resolving_attribution_produces_the_delta_through_the_existing_path(session):
    project = adopted_project(session)
    document = minutes_document(session, project, "Unknown Organization: UC-1 work will finish in September 2026.")
    capture = capture_minutes(session, document, client=MinutesClient(), source_family="m1")

    question = only_question(session, project, capture)
    assert question.reason_codes == ("attribution_unresolved",)
    assert question.permitted_actions[0] == "resolve"
    assert question.resolution_dimension == "organization"

    organization_id = inspect_minutes(session, document)["organizations"][0]["id"]
    revisions_before = _revisions(session, project)

    recorded = record_question_disposition(
        session,
        _request(question, project, disposition="resolve", reason_code="attribution_unresolved",
                 resolution=QuestionResolution(
                     resolves=frozenset({"attribution_unresolved"}), organization_id=organization_id)),
        principal=OWNER,
    )

    assert recorded.produced_delta_ids, "resolving produced no Proposed Delta"
    open_ids = {delta.id for delta in open_deltas(session, project_id=project.id)}
    assert set(recorded.produced_delta_ids) <= open_ids, "the produced delta did not re-enter comparison"
    assert _questions(session, project, capture) == (), "the resolved question still waits"
    assert _revisions(session, project) == revisions_before, "resolving moved the accepted record"


def test_a_scope_question_can_stay_uncertain_as_a_named_clarification(session):
    project = adopted_project(session)
    document = minutes_document(session, project, "Utility A: The work will finish in September 2026.")
    subject_ref = inspect_minutes(session, document)["subjects"][0]["ref"]

    class ScopeClient(MinutesClient):
        def answer(self, call):
            import json

            catalog = json.loads(call.user)
            statements = []
            for source in catalog["segments"]:
                if source["role"] not in {"action_item", "body"}:
                    continue
                timing = [item["ref"] for item in catalog["timings"]
                          if item["segment_id"] == source["id"] and item["purpose"] == "stated"]
                statements.append({
                    "kind": "commitment", "wording_segment_id": source["id"],
                    "attribution_segment_id": source["attribution_segment_id"],
                    "organization_id": catalog["organizations"][0]["id"], "person_id": None,
                    "timing_ref": timing[0] if timing else None, "predecessor_ref": None,
                    "scope": [{"subject_ref": subject_ref, "segment_id": source["id"]}]})
            return {"read_segment_ids": [source["id"] for source in catalog["segments"]], "statements": statements}

    capture = capture_minutes(session, document, client=ScopeClient(), source_family="m1")
    question = only_question(session, project, capture)
    assert question.reason_codes == ("scope_unresolved",)

    record_question_disposition(
        session,
        _request(question, project, disposition="clarify",
                 clarification_question="Which Utility Conflict does this cover?",
                 responsible_party="Utility A"),
        principal=OWNER,
    )

    assert _questions(session, project, capture) == (), "a clarified question still waits on the coordinator"
    assert session.scalars(select(Fact).where(Fact.document_id == document.id)).all() == [], (
        "a clarification forced a scope value the source never gave"
    )
    assert not open_deltas(session, project_id=project.id)
    effective = read_question_dispositions(session, project_id=project.id, source_family="m1")
    assert effective[question.question_id].disposition == "clarify", "the clarification was not retained"


def test_an_out_of_scope_statement_is_excluded_with_a_reason(session):
    project = adopted_project(session)
    document = minutes_document(session, project, "Unknown Organization: UC-1 work will finish in September 2026.")
    capture = capture_minutes(session, document, client=MinutesClient(), source_family="m1")
    question = only_question(session, project, capture)

    record_question_disposition(
        session,
        _request(question, project, disposition="exclude",
                 exclusion_reason="This statement is about a different project."),
        principal=OWNER,
    )

    assert _questions(session, project, capture) == ()
    assert not open_deltas(session, project_id=project.id)


def test_a_row_three_question_cannot_be_resolved_into_a_fabricated_assertion(session):
    project = adopted_project(session)
    document = minutes_document(session, project, "Utility A: UC-1 work is required by September 2026.")
    capture = capture_minutes(session, document, client=MinutesClient(timing_purpose="required_by"), source_family="m1")
    question = only_question(session, project, capture)

    with pytest.raises(MinutesQuestionDispositionRefused):
        record_question_disposition(
            session,
            _request(question, project, disposition="resolve",
                     resolution=QuestionResolution(resolves=frozenset({"required_by_is_not_promised_timing"}))),
            principal=OWNER,
        )
    assert _questions(session, project, capture), "the question was settled by a refused resolution"


def test_a_stale_submission_is_refused_and_nothing_is_written(session):
    project = adopted_project(session)
    document = minutes_document(session, project, "Unknown Organization: UC-1 work will finish in September 2026.")
    capture = capture_minutes(session, document, client=MinutesClient(), source_family="m1")
    question = only_question(session, project, capture)

    record_question_disposition(
        session,
        _request(question, project, disposition="clarify",
                 clarification_question="Who said this?", responsible_party="Utility A"),
        principal=OWNER,
    )
    # A page opened before that write submits at the same generation it saw.
    with pytest.raises(MinutesQuestionDispositionRefused):
        record_question_disposition(
            session,
            _request(question, project, disposition="exclude",
                     exclusion_reason="Different judgement from a stale page.", decision_generation=0),
            principal=OWNER,
        )
    rows = session.scalars(
        select(MinutesQuestionDisposition).where(MinutesQuestionDisposition.project_id == project.id)
    ).all()
    assert len(rows) == 1, "the stale submission wrote a second row"


def test_a_disposition_from_another_project_is_refused(session, project):
    # `project` is a second, unrelated project; the capture belongs to the
    # adopted one, so naming it from another project must be refused.
    adopted = adopted_project(session)
    document = minutes_document(session, adopted, "Unknown Organization: UC-1 work will finish in September 2026.")
    capture = capture_minutes(session, document, client=MinutesClient(), source_family="m1")
    question = only_question(session, adopted, capture)

    with pytest.raises(MinutesQuestionDispositionRefused):
        record_question_disposition(
            session,
            _request(question, project, disposition="exclude", exclusion_reason="cross-project"),
            principal=OWNER,
        )


def test_a_later_revision_re_asks_a_question_a_prior_answer_does_not_fit(session):
    project = adopted_project(session)
    first_doc = minutes_document(session, project, "Unknown Organization: UC-1 work will finish in September 2026.")
    first = capture_minutes(session, first_doc, client=MinutesClient(), source_family="m1")
    first_question = only_question(session, project, first)
    record_question_disposition(
        session,
        _request(first_question, project, disposition="clarify",
                 clarification_question="Who said this?", responsible_party="Utility A"),
        principal=OWNER,
    )
    assert _questions(session, project, first) == ()

    later_doc = minutes_document(session, project, "Mystery Party: UC-1 work will finish in October 2026.")
    later = capture_minutes(session, later_doc, client=MinutesClient(), source_family="m1")
    later_question = only_question(session, project, later)
    assert later_question.reason_codes == ("attribution_unresolved",)
    assert later_question.question_id != first_question.question_id, (
        "a changed statement inherited the earlier answer instead of being re-asked"
    )


# --- the Review page itself: each question its action, no generic Dismiss ----

COORDINATOR = HumanPrincipal("local:coordinator")


@pytest.fixture
def client(session):
    from corridor.web.app import app, get_human_principal, get_review_clock, get_session

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: COORDINATOR
    app.dependency_overrides[get_review_clock] = lambda: (lambda: datetime.now(timezone.utc))
    with TestClient(app) as made:
        yield made
    app.dependency_overrides.clear()


def _member_adopted_project(session):
    from access_support import seed_membership

    project = adopted_project(session)
    seed_membership(session, project, COORDINATOR)
    return project


def _live_questions(session, project):
    questions, _ = read_minutes_work(
        session, project_id=project.id, as_of=datetime.now(timezone.utc)
    )
    return questions


def test_review_page_resolves_a_source_question_into_a_proposed_change(session, client):
    project = _member_adopted_project(session)
    document = minutes_document(session, project, "Unknown Organization: UC-1 work will finish in September 2026.")
    capture_minutes(session, document, client=MinutesClient(), source_family="m1")
    organization_id = inspect_minutes(session, document)["organizations"][0]["id"]

    page = client.get(f"/review/{project.slug}")
    assert page.status_code == 200
    assert "Statements to review" in page.text
    assert "Resolve and re-enter comparison" in page.text
    assert "Dismiss" not in page.text, "the page offers a generic dismiss"

    question = _live_questions(session, project)[0]
    posted = client.post(
        f"/review/{project.slug}/question",
        data={
            "question_id": question.question_id,
            "segment_id": str(question.segment_id),
            "action": "resolve",
            "resolve_organization": str(organization_id),
        },
    )
    assert posted.status_code == 200, posted.text[:400]
    assert _live_questions(session, project) == (), "the resolved question still waits"
    assert open_deltas(session, project_id=project.id), "resolving produced no proposed change"


def test_review_page_records_an_interpretation_without_proposing_anything(session, client):
    project = _member_adopted_project(session)
    document = minutes_document(session, project, "Utility A: UC-1 work is required by September 2026.")
    capture_minutes(session, document, client=MinutesClient(timing_purpose="required_by"), source_family="m1")

    page = client.get(f"/review/{project.slug}")
    assert "Record this interpretation" in page.text
    assert "Resolve and re-enter comparison" not in page.text, (
        "a Row-3 question offered a resolution that would fabricate the assertion"
    )

    question = _live_questions(session, project)[0]
    posted = client.post(
        f"/review/{project.slug}/question",
        data={
            "question_id": question.question_id,
            "segment_id": str(question.segment_id),
            "action": "interpret",
            "interpretation": "A Required By date, not the party's Promised timing.",
        },
    )
    assert posted.status_code == 200, posted.text[:400]
    assert _live_questions(session, project) == ()
    assert not open_deltas(session, project_id=project.id)
