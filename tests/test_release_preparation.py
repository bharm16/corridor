"""One request to prepare an issue, and what came of it (#675).

Every instant is declared. Nothing here reads a clock: the cutoff, the
confirmation, the request and each attempt's start and finish are all supplied,
and the web routes take theirs from ``get_review_clock``.

The tests are grouped the way the ticket's rules are: what the request
revalidates, what the worker does with it, what the derived status says, what
the section offers, and what neither the section nor the portfolio may become.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import html
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from fastapi.testclient import TestClient

from corridor.access import COORDINATION, enroll_member
from corridor.config import settings
from corridor.db import Session, engine
from corridor.issue_coverage import confirm_coverage, derive_coverage_reading
from corridor.issue_profile import effective_issue_inventory
from corridor.issue_rendering import NO_PRIOR_COMPARISON_STATEMENT
from corridor.models import (
    IssueCoverageDeclaration,
    Project,
    ProjectRecordRevision,
    ReleaseCandidate,
    ReleasePreparationAttempt,
    ReleasePreparationRequest,
)
from corridor.object_storage import content_store
from corridor.principals import HumanPrincipal
from corridor.project_portfolio import STATE_PRECEDENCE, read_portfolio
from corridor.project_workflow import PREPARATION_FAILED, issue_readiness
from corridor.release_preparation import (
    FAILED,
    NOT_REQUESTED,
    PREPARED,
    PREPARING,
    PreparationRequestRefused,
    pending_request_ids,
    preparation_standing,
    request_preparation,
)
from corridor.release_preparation_worker import (
    PreparationInputs,
    record_failed_attempt,
    run_preparation_request,
)
from corridor.web.app import (
    app,
    get_human_principal,
    get_review_clock,
    get_session,
)
from corridor.web.issue_section import PREPARE_ACTION, PREPARING as SECTION_PREPARING
from corridor.web.issue_section import issue_view

from coverage_support import declare_coverage
from later_revision_support import BASELINE_ROWS, adopt, workbook_bytes
from packet_review_support import configure_issue
from corridor.release_candidate import RENDERER_FAILED

from test_release_candidate import (
    BINDING,
    CHASE,
    CUTOFF,
    PREPARED_AT,
    TEMPLATE,
    UCM_RENDERER,
    WEEKLY,
    _preparation,
)


COORDINATOR = HumanPrincipal("local:coordinator")
OPERATOR = HumanPrincipal("local:operator")

JANUARY = datetime(2026, 1, 5, tzinfo=timezone.utc)
REQUESTED_AT = datetime(2026, 3, 2, 8, 0, tzinfo=timezone.utc)
STARTED_AT = datetime(2026, 3, 2, 8, 1, tzinfo=timezone.utc)
FINISHED_AT = datetime(2026, 3, 2, 8, 2, tzinfo=timezone.utc)


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    scoped = Session(bind=connection)
    yield scoped
    scoped.close()
    if transaction.is_active:
        transaction.rollback()
    connection.close()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    return content_store()


@pytest.fixture
def adopted(session, tmp_path, store):
    project = Project(
        slug=f"prep-{uuid4().hex[:8]}", name="Preparation", is_synthetic=True
    )
    session.add(project)
    session.flush()
    enroll_member(
        session,
        project_id=project.id,
        email="coordinator@example.test",
        principal=COORDINATOR,
        display_name="Coordinator",
        designations=[COORDINATION],
        operator=OPERATOR,
    )
    body = workbook_bytes(tmp_path / "ucm.xlsx", BASELINE_ROWS)
    revision_id, _ = adopt(session, project, body, tmp_path)
    return _Adopted(project=project, revision_id=revision_id, template_bytes=body)


class _Adopted:
    def __init__(self, project, revision_id, template_bytes):
        self.project = project
        self.revision_id = revision_id
        self.template_bytes = template_bytes


@pytest.fixture
def client(session):
    """The app shares the test's transaction and its declared instant."""

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: COORDINATOR
    app.dependency_overrides[get_review_clock] = lambda: (lambda: CUTOFF)
    with TestClient(app) as made:
        yield made
    app.dependency_overrides.clear()


def configure(session, adopted, *, artifacts=(WEEKLY, CHASE)):
    return configure_issue(
        session,
        adopted.project,
        principal=COORDINATOR,
        effective_from=JANUARY,
        ucm=UCM_RENDERER,
        artifacts=tuple(artifacts),
    )


def declared(session, adopted, *, variant="week"):
    return declare_coverage(
        session,
        adopted.project,
        cutoff=CUTOFF,
        principal=COORDINATOR,
        confirmed_at=PREPARED_AT,
        variant=variant,
    )


def ask(session, adopted, declaration, **overrides):
    arguments = {
        "project_id": adopted.project.id,
        "declaration": declaration,
        "accepted_revision_id": adopted.revision_id,
        "requested_by": COORDINATOR,
        "requested_at": REQUESTED_AT,
        "idempotency_key": f"prepare:{declaration.declaration_digest}",
    }
    arguments.update(overrides)
    return request_preparation(session, **arguments)


def inputs(adopted):
    return PreparationInputs(
        preparation=_preparation(adopted.project.id, adopted.revision_id),
        templates=TEMPLATE,
        first_issue_behavior=NO_PRIOR_COMPARISON_STATEMENT,
        template_bytes=adopted.template_bytes,
        binding=BINDING,
    )


def prose(body: str) -> str:
    return html.unescape(body)


def week(client, adopted):
    page = client.get(f"/work/{adopted.project.slug}")
    assert page.status_code == 200, page.text
    return page.text


def prepare_via_route(client, adopted, view, **overrides):
    data = {
        "issue_profile_id": view.coverage.profile_id,
        "issue_profile_version": view.coverage.profile_version,
        "accepted_revision_id": view.accepted_revision_id,
        "cutoff": view.coverage.cutoff.isoformat(),
        "derived_reading_digest": view.coverage.reading_digest,
    }
    data.update(overrides)
    return client.post(f"/work/{adopted.project.slug}/issue/prepare", data=data)


def _requests(session, adopted) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(ReleasePreparationRequest)
            .where(ReleasePreparationRequest.project_id == adopted.project.id)
        )
    )


def _declarations(session, adopted) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(IssueCoverageDeclaration)
            .where(IssueCoverageDeclaration.project_id == adopted.project.id)
        )
    )


# --- what the request revalidates -------------------------------------------


def test_one_request_records_what_the_coordinator_was_looking_at(session, adopted):
    configure(session, adopted)
    declaration = declared(session, adopted)

    row = ask(session, adopted, declaration)

    assert row.coverage_declaration_id == declaration.id
    assert row.accepted_revision_id == adopted.revision_id
    assert row.issue_profile_version == declaration.issue_profile_version
    assert row.source_cutoff == declaration.cutoff_at
    assert row.requested_by_principal == COORDINATOR.subject


def test_a_repeated_request_converges_rather_than_queueing_a_second(
    session, adopted
):
    configure(session, adopted)
    declaration = declared(session, adopted)

    first = ask(session, adopted, declaration)
    second = ask(session, adopted, declaration)

    assert first.id == second.id
    assert _requests(session, adopted) == 1


def test_a_request_naming_a_revision_that_moved_is_refused(session, adopted):
    """The record moving is exactly what makes a prepared issue wrong."""

    configure(session, adopted)
    declaration = declared(session, adopted)

    with pytest.raises(PreparationRequestRefused, match="accepted record moved"):
        ask(session, adopted, declaration, accepted_revision_id=adopted.revision_id - 1)
    assert _requests(session, adopted) == 0


def test_a_request_naming_another_projects_declaration_is_refused(session, adopted):
    configure(session, adopted)
    declaration = declared(session, adopted)
    other = Project(slug=f"other-{uuid4().hex[:8]}", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()

    with pytest.raises(PreparationRequestRefused, match="another project"):
        request_preparation(
            session,
            project_id=other.id,
            declaration=declaration,
            accepted_revision_id=adopted.revision_id,
            requested_by=COORDINATOR,
            requested_at=REQUESTED_AT,
            idempotency_key="prepare:elsewhere",
        )


def test_a_naive_request_instant_is_refused(session, adopted):
    configure(session, adopted)
    declaration = declared(session, adopted)

    with pytest.raises(PreparationRequestRefused, match="time-zone-aware"):
        ask(session, adopted, declaration, requested_at=datetime(2026, 3, 2, 8, 0))


def test_a_submitted_request_cannot_be_edited(session, adopted):
    configure(session, adopted)
    row = ask(session, adopted, declared(session, adopted))
    session.flush()

    with pytest.raises(DBAPIError, match="immutable"):
        session.execute(
            ReleasePreparationRequest.__table__.update()
            .where(ReleasePreparationRequest.id == row.id)
            .values(requested_by_principal="local:someone-else")
        )


# --- what the derived status says -------------------------------------------


def test_status_is_derived_from_the_append_only_records(session, adopted):
    configure(session, adopted)
    assert preparation_standing(
        session, project_id=adopted.project.id
    ).state == NOT_REQUESTED

    row = ask(session, adopted, declared(session, adopted))
    standing = preparation_standing(session, project_id=adopted.project.id)
    assert standing.state == PREPARING
    assert standing.in_flight
    assert standing.request_id == row.id

    record_failed_attempt(
        session,
        request_id=row.id,
        project_id=adopted.project.id,
        reason="the customer's template bytes could not be retrieved",
        started_at=STARTED_AT,
        finished_at=FINISHED_AT,
    )
    standing = preparation_standing(session, project_id=adopted.project.id)
    assert standing.state == FAILED
    assert standing.candidate_id is None
    assert "template bytes" in standing.reason


def test_a_pending_request_is_one_no_attempt_has_finished(session, adopted):
    configure(session, adopted)
    row = ask(session, adopted, declared(session, adopted))

    assert pending_request_ids(session, project_id=adopted.project.id) == (row.id,)

    record_failed_attempt(
        session,
        request_id=row.id,
        project_id=adopted.project.id,
        reason="nothing was produced",
        started_at=STARTED_AT,
        finished_at=FINISHED_AT,
    )
    assert pending_request_ids(session, project_id=adopted.project.id) == ()


def test_an_attempt_cannot_name_a_candidate_and_a_failure_together(
    session, adopted
):
    """#529's three outcomes, made unrepresentable in each other's shape."""

    configure(session, adopted)
    row = ask(session, adopted, declared(session, adopted))
    session.flush()

    with pytest.raises(DBAPIError, match="ck_release_preparation_attempts_result"):
        session.add(
            ReleasePreparationAttempt(
                request_id=row.id,
                project_id=adopted.project.id,
                outcome="prepared",
                candidate_id=None,
                reason="but also it failed",
                started_at=STARTED_AT,
                finished_at=FINISHED_AT,
            )
        )
        session.flush()


# --- what a failure becomes -------------------------------------------------


def test_a_failed_preparation_is_a_current_issue_technical_blocker(
    session, adopted
):
    configure(session, adopted)
    row = ask(session, adopted, declared(session, adopted))
    record_failed_attempt(
        session,
        request_id=row.id,
        project_id=adopted.project.id,
        reason="the renderer could not produce the workbook",
        started_at=STARTED_AT,
        finished_at=FINISHED_AT,
    )

    problems = issue_readiness(session, project_id=adopted.project.id, as_of=CUTOFF)

    assert PREPARATION_FAILED in {problem.code for problem in problems}


def test_a_preparation_in_flight_asks_nothing_of_the_coordinator(session, adopted):
    """No human action is required while a worker holds the request."""

    configure(session, adopted)
    ask(session, adopted, declared(session, adopted))

    problems = issue_readiness(session, project_id=adopted.project.id, as_of=CUTOFF)

    assert PREPARATION_FAILED not in {problem.code for problem in problems}


def test_preparation_is_never_a_sixth_primary_portfolio_state(session, adopted):
    """The portfolio's five states stay five, in flight and after a failure.

    A project being prepared asks nothing of a coordinator, and a failed
    preparation is something to put right before the current issue goes out —
    which is a state the portfolio already has.
    """

    assert len(STATE_PRECEDENCE) == 5

    configure(session, adopted)
    row = ask(session, adopted, declared(session, adopted))
    in_flight = read_portfolio(
        session, principal_subject=COORDINATOR.subject, as_of=CUTOFF
    ).standings
    assert {one.state for one in in_flight} <= set(STATE_PRECEDENCE)

    record_failed_attempt(
        session,
        request_id=row.id,
        project_id=adopted.project.id,
        reason="the renderer could not produce the workbook",
        started_at=STARTED_AT,
        finished_at=FINISHED_AT,
    )
    after = read_portfolio(
        session, principal_subject=COORDINATOR.subject, as_of=CUTOFF
    ).standings
    assert {one.state for one in after} <= set(STATE_PRECEDENCE)
    assert [one.state for one in after if one.project_id == adopted.project.id] == [
        "issue_blocked"
    ]


# --- what the section offers ------------------------------------------------


def test_the_section_shows_the_derived_reading_and_one_action(
    session, adopted, client, store
):
    configure(session, adopted)

    view = issue_view(session, project_id=adopted.project.id, as_of=CUTOFF)
    assert view.coverage is not None
    assert view.may_prepare

    body = prose(week(client, adopted))
    assert PREPARE_ACTION in body
    assert "Sources for this issue" in body
    # The coordinator is told what confirming means, and that they are not
    # being asked to recreate any of it.
    assert "cannot be edited here" in body


def test_the_section_says_it_is_preparing_and_offers_nothing(
    session, adopted, client
):
    configure(session, adopted)
    ask(session, adopted, declared(session, adopted))

    view = issue_view(session, project_id=adopted.project.id, as_of=CUTOFF)
    assert view.state == SECTION_PREPARING
    assert view.may_prepare is False
    assert view.may_authorize is False

    body = week(client, adopted)
    assert "/issue/prepare" not in body
    assert "Preparing this issue" in prose(body)


def test_the_section_says_it_is_preparing_even_with_a_candidate_on_the_page(
    session, adopted, client, store
):
    """The in-flight state outranks whatever the project already holds.

    A project whose last candidate is still authorizable, and whose coordinator
    has asked for a fresh one, is being prepared: offering the approval beside
    a preparation in flight would put two acts on one section and let a
    coordinator approve the issue they just asked to replace.
    """

    from test_release_candidate import _prepare as prepare_candidate

    configure(session, adopted)
    prepare_candidate(
        session,
        adopted,
        store,
        coverage_declaration_id=declared(session, adopted, variant="first").id,
    )
    ask(session, adopted, declared(session, adopted, variant="second"))

    view = issue_view(session, project_id=adopted.project.id, as_of=CUTOFF)

    assert view.prepared
    assert view.state == SECTION_PREPARING
    assert view.may_authorize is False
    assert view.may_prepare is False
    assert "/issue/prepare" not in week(client, adopted)


def test_the_action_appends_two_separately_identified_records(
    session, adopted, client, store
):
    configure(session, adopted)
    view = issue_view(session, project_id=adopted.project.id, as_of=CUTOFF)

    response = prepare_via_route(client, adopted, view)

    assert response.status_code == 202, response.text
    assert _declarations(session, adopted) == 1
    assert _requests(session, adopted) == 1
    declaration = session.scalars(select(IssueCoverageDeclaration)).first()
    requested = session.scalars(select(ReleasePreparationRequest)).first()
    assert declaration.derived_reading_digest == view.coverage.reading_digest
    assert requested.coverage_declaration_id == declaration.id
    assert requested.requested_by_principal == COORDINATOR.subject
    # And it returned immediately: nothing was rendered and nothing attached.
    assert (
        session.scalar(
            select(func.count())
            .select_from(ReleaseCandidate)
            .where(ReleaseCandidate.project_id == adopted.project.id)
        )
        == 0
    )


def test_the_action_refuses_a_digest_other_than_the_one_displayed(
    session, adopted, client, store
):
    configure(session, adopted)
    view = issue_view(session, project_id=adopted.project.id, as_of=CUTOFF)

    response = prepare_via_route(
        client, adopted, view, derived_reading_digest="0" * 64
    )

    assert response.status_code == 409
    assert "changed after it was shown" in prose(response.text)
    assert _declarations(session, adopted) == 0
    assert _requests(session, adopted) == 0


def test_the_action_refuses_a_profile_version_that_moved(
    session, adopted, client, store
):
    configure(session, adopted)
    view = issue_view(session, project_id=adopted.project.id, as_of=CUTOFF)

    response = prepare_via_route(client, adopted, view, issue_profile_version=99)

    assert response.status_code == 409
    assert "not the one in force" in prose(response.text)
    assert _requests(session, adopted) == 0


def test_the_action_refuses_a_revision_that_moved(session, adopted, client, store):
    configure(session, adopted)
    view = issue_view(session, project_id=adopted.project.id, as_of=CUTOFF)

    response = prepare_via_route(
        client, adopted, view, accepted_revision_id=adopted.revision_id - 1
    )

    assert response.status_code == 409
    assert "accepted record moved" in prose(response.text)
    # The savepoint gave up both appends together: a coordinator whose request
    # was refused has not silently confirmed coverage either.
    assert _requests(session, adopted) == 0
    assert _declarations(session, adopted) == 0


def test_the_action_refuses_a_cutoff_that_has_not_arrived(
    session, adopted, client, store
):
    configure(session, adopted)
    view = issue_view(session, project_id=adopted.project.id, as_of=CUTOFF)

    response = prepare_via_route(
        client,
        adopted,
        view,
        cutoff=(CUTOFF + timedelta(days=7)).isoformat(),
    )

    assert response.status_code == 409
    assert "has not arrived" in prose(response.text)
    assert _requests(session, adopted) == 0


def test_a_resubmitted_action_queues_nothing_twice(
    session, adopted, client, store
):
    configure(session, adopted)
    view = issue_view(session, project_id=adopted.project.id, as_of=CUTOFF)

    assert prepare_via_route(client, adopted, view).status_code == 202
    session.expire_all()
    # The section now says it is preparing, and the form is gone; a resubmitted
    # POST still converges rather than queueing a second preparation.
    assert prepare_via_route(client, adopted, view).status_code == 202

    assert _declarations(session, adopted) == 1
    assert _requests(session, adopted) == 1


# --- what the worker does ---------------------------------------------------


@pytest.mark.usefixtures("store")
def test_the_worker_prepares_through_the_three_phases(
    runtime_database, tmp_path, monkeypatch
):
    """One request in, one candidate and one attempt out, over real commits."""

    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    factory = runtime_database.session_factory

    with factory() as setup:
        project = Project(
            slug=f"prep-run-{uuid4().hex[:8]}", name="Prep Run", is_synthetic=True
        )
        setup.add(project)
        setup.flush()
        enroll_member(
            setup,
            project_id=project.id,
            email="coordinator@example.test",
            principal=COORDINATOR,
            display_name="Coordinator",
            designations=[COORDINATION],
            operator=OPERATOR,
        )
        body = workbook_bytes(tmp_path / "runtime.xlsx", BASELINE_ROWS)
        revision_id, _ = adopt(setup, project, body, tmp_path)
        adopted = _Adopted(
            project=project, revision_id=revision_id, template_bytes=body
        )
        configure(setup, adopted)
        declaration = declared(setup, adopted)
        request = ask(setup, adopted, declaration)
        request_id = int(request.id)
        project_id = int(project.id)
        setup.commit()

    attempt = run_preparation_request(
        factory,
        request_id=request_id,
        inputs=inputs(adopted),
        started_at=STARTED_AT,
        finished_at=FINISHED_AT,
    )

    assert attempt.outcome == "prepared"
    assert attempt.candidate_id is not None
    with factory() as check:
        candidate = check.get_one(ReleaseCandidate, attempt.candidate_id)
        # The candidate references the confirmed declaration by identity, and
        # the preparation is attributed to the person who asked for it.
        assert candidate.coverage_declaration_id == declaration.id
        assert candidate.prepared_by_principal == COORDINATOR.subject
        assert (
            preparation_standing(check, project_id=project_id).state == PREPARED
        )


@pytest.mark.usefixtures("store")
def test_a_refused_preparation_leaves_no_partial_candidate(
    runtime_database, tmp_path, monkeypatch
):
    """The failure mode the ticket names, proved on real committed state."""

    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    factory = runtime_database.session_factory

    with factory() as setup:
        project = Project(
            slug=f"prep-fail-{uuid4().hex[:8]}", name="Prep Fail", is_synthetic=True
        )
        setup.add(project)
        setup.flush()
        enroll_member(
            setup,
            project_id=project.id,
            email="coordinator@example.test",
            principal=COORDINATOR,
            display_name="Coordinator",
            designations=[COORDINATION],
            operator=OPERATOR,
        )
        body = workbook_bytes(tmp_path / "runtime.xlsx", BASELINE_ROWS)
        revision_id, _ = adopt(setup, project, body, tmp_path)
        adopted = _Adopted(
            project=project, revision_id=revision_id, template_bytes=body
        )
        configure(setup, adopted)
        request = ask(setup, adopted, declared(setup, adopted))
        request_id = int(request.id)
        project_id = int(project.id)
        setup.commit()

    # The template bytes the preparation is handed are not the ones the
    # customer registered, which #529 refuses with a bounded reason.
    wrong = inputs(adopted)
    attempt = run_preparation_request(
        factory,
        request_id=request_id,
        inputs=PreparationInputs(
            preparation=wrong.preparation,
            templates=wrong.templates,
            first_issue_behavior=wrong.first_issue_behavior,
            template_bytes=b"not the registered workbook",
            binding=wrong.binding,
        ),
        started_at=STARTED_AT,
        finished_at=FINISHED_AT,
    )

    assert attempt.outcome == "refused"
    assert attempt.candidate_id is None
    # The bounded code names the guard that actually caught it, not merely
    # "something went wrong": the UCM renderer could not produce a workbook
    # from those bytes, so the configured set was never complete.
    assert attempt.refusal_code == RENDERER_FAILED
    with factory() as check:
        assert (
            check.scalar(
                select(func.count())
                .select_from(ReleaseCandidate)
                .where(ReleaseCandidate.project_id == project_id)
            )
            == 0
        )
        assert preparation_standing(check, project_id=project_id).state == FAILED
