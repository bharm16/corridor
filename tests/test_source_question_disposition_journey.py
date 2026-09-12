"""Source question disposition as a declared #848 scenario (#833).

A selected capability, not a step of the core journey: the Review page's source
questions become actionable only when minutes are in the declared demo or pilot
scope. It is exercised here over the same harness the core journey uses, through
the production route (`POST /review/{slug}/question`) and the production reader
(`read_minutes_work` behind `GET /review/{slug}`), and held to the same
questions — every actionable state has an act, and the claimed workflow has both
a writer and a reader.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import journey_matrix
from access_support import seed_membership
from corridor.config import settings
from corridor.minutes_reading import read_minutes_clarifications, read_minutes_work
from corridor.minutes_spine import capture_minutes, inspect_minutes
from corridor.models import Project
from corridor.principals import HumanPrincipal
from corridor.review_packet_reading import open_deltas
from corridor.web.app import app, get_human_principal, get_review_clock, get_session
from journey_harness import Step, run_scenario
from minutes_fixture_support import MinutesClient, adopted_project, minutes_document


COORDINATOR = HumanPrincipal("local:coordinator")


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "store"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))


@pytest.fixture
def client(session):
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: COORDINATOR
    app.dependency_overrides[get_review_clock] = lambda: (lambda: datetime.now(timezone.utc))
    with TestClient(app) as made:
        yield made
    app.dependency_overrides.clear()


class _ScopeClient(MinutesClient):
    """A reader that proposes a scope the source passage does not carry."""

    def __init__(self, subject_ref):
        self._ref = subject_ref
        super().__init__()

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
                "scope": [{"subject_ref": self._ref, "segment_id": source["id"]}]})
        return {"read_segment_ids": [source["id"] for source in catalog["segments"]], "statements": statements}


@dataclass
class SourceQuestions:
    session: Session
    project: Project
    client: TestClient
    carried: dict = field(default_factory=dict)


def _questions(walk: SourceQuestions):
    questions, _ = read_minutes_work(
        walk.session, project_id=walk.project.id, as_of=datetime.now(timezone.utc)
    )
    return questions


def _question(walk: SourceQuestions, family: str):
    return next(one for one in _questions(walk) if one.source_family == family)


def _still_waiting(walk: SourceQuestions, family: str) -> bool:
    return any(one.source_family == family for one in _questions(walk))


def _post(walk: SourceQuestions, question, **data):
    return walk.client.post(
        f"/review/{walk.project.slug}/question",
        data={"question_id": question.question_id, "segment_id": str(question.segment_id), **data},
    )


def _open_delta_ids(walk: SourceQuestions):
    return {delta.id for delta in open_deltas(walk.session, project_id=walk.project.id)}


def _step_resolve_a_source_question(walk: SourceQuestions) -> None:
    document = minutes_document(walk.session, walk.project, "Unknown Organization: UC-1 work will finish in September 2026.")
    capture_minutes(walk.session, document, client=MinutesClient(), source_family="sq-resolve")
    organization_id = inspect_minutes(walk.session, document)["organizations"][0]["id"]
    before = _open_delta_ids(walk)
    posted = _post(walk, _question(walk, "sq-resolve"), action="resolve", resolve_organization=str(organization_id))
    assert posted.status_code == 200, "resolving from the source was refused: " + posted.text[:200]
    walk.session.expire_all()
    assert _open_delta_ids(walk) - before, "resolving produced no proposed change"
    assert not _still_waiting(walk, "sq-resolve"), "the resolved question still waits"


def _step_interpret_a_source_question(walk: SourceQuestions) -> None:
    document = minutes_document(walk.session, walk.project, "Utility A: UC-1 work is required by September 2026.")
    capture_minutes(walk.session, document, client=MinutesClient(timing_purpose="required_by"), source_family="sq-interpret")
    before = _open_delta_ids(walk)
    posted = _post(walk, _question(walk, "sq-interpret"), action="interpret",
                   interpretation="A Required By date, not the party's Promised timing.")
    assert posted.status_code == 200, "recording the interpretation was refused: " + posted.text[:200]
    walk.session.expire_all()
    assert _open_delta_ids(walk) == before, "an interpretation proposed a change"
    assert not _still_waiting(walk, "sq-interpret"), "the interpreted question still waits"


def _step_clarify_a_source_question(walk: SourceQuestions) -> None:
    document = minutes_document(walk.session, walk.project, "Utility A: The work will finish in September 2026.")
    subject_ref = inspect_minutes(walk.session, document)["subjects"][0]["ref"]
    capture_minutes(walk.session, document, client=_ScopeClient(subject_ref), source_family="sq-clarify")
    question = _question(walk, "sq-clarify")
    assert question.reason_codes == ("scope_unresolved",)
    posted = _post(walk, question, action="clarify",
                   clarification_question="Which Utility Conflict does this cover?", responsible_party="Utility A")
    assert posted.status_code == 200, "recording the clarification was refused: " + posted.text[:200]
    walk.session.expire_all()
    assert not _still_waiting(walk, "sq-clarify"), "a clarified question still waits on the coordinator"
    retained = read_minutes_clarifications(walk.session, project_id=walk.project.id, as_of=datetime.now(timezone.utc))
    assert any(one.responsible_party == "Utility A" for one in retained), "the clarification was not retained"


def _step_exclude_a_source_question(walk: SourceQuestions) -> None:
    document = minutes_document(walk.session, walk.project, "Unknown Organization: UC-2 work will finish in October 2026.")
    capture_minutes(walk.session, document, client=MinutesClient(), source_family="sq-exclude")
    posted = _post(walk, _question(walk, "sq-exclude"), action="exclude",
                   exclusion_reason="This statement is about a different project.")
    assert posted.status_code == 200, "recording the exclusion was refused: " + posted.text[:200]
    walk.session.expire_all()
    assert not _still_waiting(walk, "sq-exclude"), "the excluded question still waits"


SOURCE_QUESTION_STEPS: tuple[Step, ...] = (
    Step(
        name="resolve_a_source_question",
        sentence="the coordinator resolves a source question from the source and "
        "it re-enters comparison as a proposed change",
        owner="#833",
        run=_step_resolve_a_source_question,
    ),
    Step(
        name="interpret_a_source_question",
        sentence="the coordinator records that the source makes no such assertion "
        "and nothing is proposed",
        owner="#833",
        run=_step_interpret_a_source_question,
    ),
    Step(
        name="clarify_a_source_question",
        sentence="a question with insufficient evidence is retained as a named "
        "clarification request and stops waiting on the coordinator",
        owner="#833",
        run=_step_clarify_a_source_question,
    ),
    Step(
        name="exclude_a_source_question",
        sentence="a statement outside the project's scope is recorded out of "
        "scope with a reason",
        owner="#833",
        run=_step_exclude_a_source_question,
    ),
)


def test_the_source_question_capability_runs_as_a_declared_scenario(session, client):
    """Walk the selected capability and report every step against its ticket."""

    project = adopted_project(session)
    seed_membership(session, project, COORDINATOR)
    walk = SourceQuestions(session=session, project=project, client=client)
    report = run_scenario(
        "Source question disposition (#833, a selected capability)",
        SOURCE_QUESTION_STEPS,
        walk,
    )
    print("\n" + report.render(), flush=True)

    assert not report.failures, "a step nothing said could fail did:\n" + report.render()
    assert len(report.passed) == len(SOURCE_QUESTION_STEPS), (
        "this capability is built, so every step of it passes:\n" + report.render()
    )


def test_every_source_question_row_names_a_step_of_this_scenario():
    """A #833 row nothing exercises is a claim, not an inventory entry (#848)."""

    declared = {step.name for step in SOURCE_QUESTION_STEPS}
    mine = {
        row.scenario
        for row in journey_matrix.SELECTED_CAPABILITIES
        if row.owner == "#833"
    }
    assert mine and mine <= declared, (
        "these #833 rows name a step that does not exist: " + ", ".join(sorted(mine - declared))
    )
