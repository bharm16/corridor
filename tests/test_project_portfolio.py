"""Every adopted project once, in one derived cross-project reading (#537).

#536 gave one project its own ordered week and this is the reading above it:
the coordinator sees what each project needs without opening any of them, and
a quiet project proves it is quiet at no cost.

The properties under test are the ones the ticket names. The authorization
scope is the coordination designation, not membership and not the customer.
The primary state follows one declared precedence. The counts beside it are
secondary and change nothing. The portfolio and the project's own week are
derived from the same records through the same functions, so a mixed fixture
must produce the same state and the same numbers on both. The whole page is
produced by a bounded batched read whose statement count does not grow with the
portfolio, which is asserted by counting statements rather than by prose.

Nothing here reads a clock: the cutoff is declared, and the deferral test
states two cutoffs rather than waiting for one.

The fixture builders are imported from ``test_project_workflow`` on purpose.
"The portfolio cannot disagree with the project workflow" is only proved if
both are shown the very same projects, built the very same way.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from corridor import analytics
from corridor.analytics import EventFamily
from corridor.db import engine
from corridor.models import (
    DeltaDisposition,
    Document,
    ProjectRosterEntry,
    DeltaFollowUpPlan,
    DeltaRecordDecision,
    DeltaReviewPacketReceipt,
    Project,
    ProjectRecordRevision,
)
from corridor.packet_review import (
    FocusedAnswer,
    focused_request,
    packet_request,
    read_review_items,
)
from corridor.principals import HumanPrincipal
from corridor.project_portfolio import (
    FOLLOW_UP_DUE,
    ISSUE_BLOCKED,
    ISSUE_READY,
    NO_ACTION,
    REVIEW_WAITING,
    SENTENCES,
    STATE_PRECEDENCE,
    landing_section,
    primary_state,
    read_portfolio,
)
from corridor.project_workflow import (
    FOLLOW_UP,
    ISSUE,
    REVIEW,
    read_project_workflow,
)
from corridor.review_packets import (
    DEFER,
    NEEDS_COORDINATION,
    resolve_review_packet,
)
from corridor.web.app import (
    app,
    get_human_principal,
    get_review_clock,
    get_session,
)

from access_support import seed_membership
from packet_review_support import Rendition
from test_project_workflow import (
    CONFLICT,
    COORDINATOR,
    NOW,
    OUTSIDER,
    RETURNS_AT,
    VISIBLE_LEVELS,
    Adopted,
    _cross_source,
    _plan_every_child,
    _project,
)


# The ticket's own candidate customer labels. They stay provisional until the
# repository terminology procedure has run on them, so none of them may appear
# on any screen until it has.
PROVISIONAL_LABELS = ("This Week", "Review needed", "Ready to issue")

AFTER_THE_RETURN = RETURNS_AT + timedelta(days=1)


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    scoped = Session(bind=connection)
    scoped.info["connection"] = connection
    yield scoped
    scoped.close()
    if transaction.is_active:
        transaction.rollback()
    connection.close()


@pytest.fixture
def client(session):
    """The app shares the test's transaction and the test's declared instant."""

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: COORDINATOR
    app.dependency_overrides[get_review_clock] = lambda: (lambda: NOW)
    with TestClient(app) as made:
        yield made
    app.dependency_overrides.clear()


def _portfolio(session: Session, *, principal: HumanPrincipal = COORDINATOR):
    return read_portfolio(
        session, principal_subject=principal.subject, as_of=NOW
    )


class _Statements:
    """Every statement the connection actually sent, counted."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    def __call__(self, conn, cursor, statement, parameters, context, executemany):
        self.sent.append(statement)


def _count_statements(session: Session, work):
    """Run ``work`` and return how many statements reached the database."""

    connection = session.info["connection"]
    counter = _Statements()
    session.flush()
    event.listen(connection, "before_cursor_execute", counter)
    try:
        result = work()
    finally:
        event.remove(connection, "before_cursor_execute", counter)
    return result, len(counter.sent)


# --- the fixture shapes ----------------------------------------------------


def _blocked(session: Session, name: str = "Blocked") -> Project:
    """An accepted record and a source Corridor could not read."""

    project = _project(session, name)
    adopted = Adopted(session, project).accepted(CONFLICT)
    unreadable = adopted.rendition("permit-2026-09.pdf")
    unreadable.document.parse_status = "failed"
    session.flush()
    adopted.template().adopt()
    return project


def _reviewing(session: Session, name: str = "Reviewing") -> Project:
    """Two sources answering one Promised For differently, nothing decided."""

    project = _project(session, name)
    _cross_source(session, project)
    return project


def _ready(session: Session, name: str = "Ready") -> Project:
    """An accepted record, a registered template, and nothing waiting."""

    project = _project(session, name)
    Adopted(session, project).accepted(CONFLICT).template().adopt()
    return project


def _planned(session: Session, name: str = "Planned") -> Project:
    """Every proposed change already answered Needs coordination.

    Nothing is waiting on the coordinator's own judgement here; the answers are
    waiting on the people the Follow-up Plans named. It is in the mixed fixture
    so that "the portfolio counts the same thing the week counts" is tested
    where the two sets actually differ.
    """

    project = _project(session, name)
    _cross_source(session, project)
    _plan_every_child(session, project)
    return project


def _partly_planned(session: Session, name: str = "Partly planned") -> Project:
    """One source answered Needs coordination, the other still undecided.

    The only shape where a review and an outside answer are both waiting, so
    it is the only one that can tell the ordered sections apart.
    """

    project = _project(session, name)
    _cross_source(session, project)
    reading = read_review_items(session, project_id=project.id, as_of=NOW)
    item = next(one for one in reading.items if one.focused)
    result = resolve_review_packet(
        session,
        focused_request(
            reading,
            item,
            principal=COORDINATOR,
            decided_at=NOW,
            answers=[
                FocusedAnswer(
                    delta_id=item.children[0].delta_id,
                    outcome=NEEDS_COORDINATION,
                    question="Which date does the utility actually hold to?",
                    responsible_organization="City Water",
                    return_date=RETURNS_AT,
                )
            ],
        ),
    )
    assert result.status == "saved", result
    session.expire_all()
    return project


def _quiet(session: Session, name: str = "Quiet") -> Project:
    """Adopted, with nothing accepted and nothing proposed."""

    project = _project(session, name)
    Adopted(session, project).adopt()
    return project


def _legacy(session: Session, name: str = "Legacy") -> Project:
    """A project whose record still comes from the legacy path."""

    return _project(session, name)


def _mixed(session: Session) -> dict[str, Project]:
    return {
        "blocked": _blocked(session),
        "reviewing": _reviewing(session),
        "ready": _ready(session),
        "planned": _planned(session),
        "partly_planned": _partly_planned(session),
        "quiet": _quiet(session),
    }


def _spine_counts(session: Session, project: Project) -> tuple[int, ...]:
    """Every row family this page could conceivably be accused of writing."""

    return tuple(
        session.scalar(
            select(func.count())
            .select_from(model)
            .where(model.project_id == project.id)
        )
        for model in (
            ProjectRecordRevision,
            DeltaRecordDecision,
            DeltaDisposition,
            DeltaFollowUpPlan,
            DeltaReviewPacketReceipt,
        )
    )


# --- who is shown, and who is not ------------------------------------------


def test_every_coordinated_adopted_project_appears_exactly_once(session, client):
    """One row per project, on the reading and on the page."""

    projects = _mixed(session)

    reading = _portfolio(session)
    body = client.get("/portfolio").text

    shown = [row.project_id for row in reading.standings]
    assert sorted(shown) == sorted(project.id for project in projects.values())
    assert len(shown) == len(set(shown)), "a project is listed twice"
    for project in projects.values():
        assert body.count(f'href="/work/{project.slug}?from=portfolio"') == 1


def test_a_project_this_person_may_read_but_not_coordinate_is_absent(
    session, client
):
    """Membership is the read boundary; coordination is a separate designation."""

    readable = _reviewing(session, "Readable")
    seed_membership(session, readable, COORDINATOR, designations=())
    coordinated = _ready(session, "Coordinated")

    reading = _portfolio(session)
    body = client.get("/portfolio").text

    assert [row.project_id for row in reading.standings] == [coordinated.id]
    assert readable.slug not in body
    assert readable.name not in body


def test_another_persons_project_is_absent_entirely(session, client):
    """No unauthorized customer or project data reaches the page at all."""

    mine = _ready(session, "Mine")
    theirs = _reviewing(session, "Theirs")
    seed_membership(session, theirs, OUTSIDER)
    session.delete(
        session.scalars(
            select(ProjectRosterEntry).where(
                ProjectRosterEntry.project_id == theirs.id,
                ProjectRosterEntry.principal_subject == COORDINATOR.subject,
            )
        ).one()
    )
    session.flush()

    reading = _portfolio(session)
    body = client.get("/portfolio").text

    assert [row.project_id for row in reading.standings] == [mine.id]
    assert theirs.slug not in body
    assert theirs.name not in body
    assert _portfolio(session, principal=OUTSIDER).standings[0].project_id == theirs.id


def test_a_legacy_project_has_no_week_to_summarize(session, client):
    """ADR-0085 amends only the adopted-project presentation."""

    adopted = _ready(session)
    legacy = _legacy(session)

    reading = _portfolio(session)

    assert [row.project_id for row in reading.standings] == [adopted.id]
    assert legacy.slug not in client.get("/portfolio").text


# --- the one primary state -------------------------------------------------


def test_the_primary_state_follows_the_declared_precedence(session):
    """Blocker, then review, then a candidate to authorize, then follow-up."""

    projects = _mixed(session)
    reading = _portfolio(session)

    states = {
        name: reading.standing(project.id).state
        for name, project in projects.items()
    }
    assert states == {
        "blocked": ISSUE_BLOCKED,
        "reviewing": REVIEW_WAITING,
        "ready": ISSUE_READY,
        # Precedence puts a candidate ready for authorization above accepted
        # follow-up, so a project whose every change is planned shows the
        # issue and counts the plans beside it.
        "planned": ISSUE_READY,
        # A review still waiting outranks both issue states below it, and the
        # Follow-up Plan beside it is a count rather than the state.
        "partly_planned": REVIEW_WAITING,
        "quiet": NO_ACTION,
    }
    assert STATE_PRECEDENCE == (
        ISSUE_BLOCKED,
        REVIEW_WAITING,
        ISSUE_READY,
        FOLLOW_UP_DUE,
        NO_ACTION,
    )


@pytest.mark.parametrize(
    "inputs,expected",
    [
        # A blocker outranks everything, including a review beside it.
        (dict(changes_to_review=3, follow_up_waiting=2, readiness_problems=1,
              accepted_revision_id=7), ISSUE_BLOCKED),
        (dict(changes_to_review=0, follow_up_waiting=0, readiness_problems=1,
              accepted_revision_id=7), ISSUE_BLOCKED),
        # Then a review affecting the current issue, over both issue states.
        (dict(changes_to_review=1, follow_up_waiting=4, readiness_problems=0,
              accepted_revision_id=7), REVIEW_WAITING),
        (dict(changes_to_review=1, follow_up_waiting=0, readiness_problems=0,
              accepted_revision_id=None), REVIEW_WAITING),
        # Then a candidate ready for authorization, over accepted follow-up.
        (dict(changes_to_review=0, follow_up_waiting=2, readiness_problems=0,
              accepted_revision_id=7), ISSUE_READY),
        # Then accepted follow-up, where there is no issue to authorize.
        (dict(changes_to_review=0, follow_up_waiting=1, readiness_problems=0,
              accepted_revision_id=None), FOLLOW_UP_DUE),
        # Coverage cannot block an issue that does not exist yet, which is
        # #536's own rule for its issue section.
        (dict(changes_to_review=0, follow_up_waiting=0, readiness_problems=2,
              accepted_revision_id=None), NO_ACTION),
        (dict(changes_to_review=0, follow_up_waiting=0, readiness_problems=0,
              accepted_revision_id=None), NO_ACTION),
    ],
)
def test_the_precedence_is_exactly_the_declared_order(inputs, expected):
    """Every rung of the precedence, including the ties between two rungs."""

    assert primary_state(**inputs) == expected


@pytest.mark.parametrize(
    "inputs,expected",
    [
        (dict(changes_to_review=1, follow_up_waiting=1, accepted_revision_id=7),
         REVIEW),
        (dict(changes_to_review=0, follow_up_waiting=1, accepted_revision_id=7),
         FOLLOW_UP),
        (dict(changes_to_review=0, follow_up_waiting=0, accepted_revision_id=7),
         ISSUE),
        (dict(changes_to_review=0, follow_up_waiting=0, accepted_revision_id=None),
         REVIEW),
    ],
)
def test_the_landing_section_is_the_first_one_still_holding_work(inputs, expected):
    """#536's own rule, applied to the same numbers rather than re-decided."""

    assert landing_section(**inputs) == expected


def test_a_blocker_outranks_the_review_waiting_beside_it(session):
    """A project with both shows the blocker, and still counts the review."""

    project = _reviewing(session)
    unreadable = Rendition(session, project, f"broken-{uuid4().hex[:6]}.pdf")
    unreadable.document.parse_status = "failed"
    session.flush()

    standing = _portfolio(session).standing(project.id)

    assert standing.state == ISSUE_BLOCKED
    assert standing.changes_to_review > 0, "the review is still counted"
    assert standing.readiness_problems == 1


def test_a_secondary_count_never_changes_the_primary_state(session):
    """Follow-up is counted beside a ready issue without becoming the state."""

    project = _reviewing(session)
    planned = _plan_every_child(session, project)

    standing = _portfolio(session).standing(project.id)

    assert standing.follow_up_waiting == planned > 0
    assert standing.changes_to_review == 0
    # Precedence puts a candidate ready for authorization above accepted
    # follow-up, so the follow-up is a count beside the state, not the state.
    assert standing.state == ISSUE_READY
    assert dict(standing.counts)["Follow-up Plans waiting on an answer"] == planned


# --- the portfolio and the project's own week ------------------------------


def test_the_portfolio_and_the_project_week_never_disagree(session):
    """Same records, same functions, so the same state and the same numbers."""

    projects = _mixed(session)
    reading = _portfolio(session)

    for project in projects.values():
        standing = reading.standing(project.id)
        workflow = read_project_workflow(session, project_id=project.id, as_of=NOW)

        assert standing.landing == workflow.landing
        assert standing.changes_to_review == len(workflow.changes_awaiting_decision)
        assert standing.follow_up_waiting == len(workflow.follow_up)
        assert standing.readiness_problems == len(workflow.readiness)
        assert standing.accepted_revision_id == workflow.accepted_revision_id
        waiting = {
            section.name for section in workflow.sections if section.waiting
        }
        assert standing.quiet == (waiting == set())
        if standing.state == REVIEW_WAITING:
            assert REVIEW in waiting
        if standing.state == FOLLOW_UP_DUE:
            assert FOLLOW_UP in waiting
        if standing.state in (ISSUE_BLOCKED, ISSUE_READY):
            assert ISSUE in waiting


def test_selecting_a_project_opens_the_first_incomplete_section(session, client):
    """The link goes to #536's own landing section, not to a second opinion."""

    projects = _mixed(session)
    reading = _portfolio(session)

    for project in projects.values():
        standing = reading.standing(project.id)
        workflow = read_project_workflow(session, project_id=project.id, as_of=NOW)
        assert standing.landing == workflow.landing

        page = client.get(f"/work/{project.slug}?from=portfolio")
        assert page.status_code == 200
        focused = re.search(r'id="([a-z_]+)" tabindex="-1" autofocus', page.text)
        assert focused is not None
        assert focused.group(1) == standing.landing

    assert {row.landing for row in reading.standings} <= {REVIEW, FOLLOW_UP, ISSUE}


def test_a_quiet_project_says_so_and_costs_no_click(session, client):
    """The zero-click property, read from the page rather than asserted."""

    quiet = _quiet(session)
    busy = _reviewing(session)

    with analytics.capture_events() as events:
        body = client.get("/portfolio").text

    standing = _portfolio(session).standing(quiet.id)
    assert standing.quiet and standing.state == NO_ACTION
    assert standing.changes_to_review == 0
    assert standing.follow_up_waiting == 0
    assert SENTENCES[NO_ACTION] in body
    assert quiet.name in body, "it is still shown; it just asks for nothing"

    presented = events.by_family(EventFamily.PORTFOLIO_READING)
    assert len(presented) == 1
    shown = {
        entry["project_id"]: entry["state"]
        for entry in presented[0].payload["projects"]
    }
    assert shown[quiet.id] == NO_ACTION
    assert shown[busy.id] == REVIEW_WAITING
    assert events.by_family(EventFamily.PROJECT_SELECTION) == []


# --- deferral --------------------------------------------------------------


def test_snoozed_work_is_absent_until_the_date_its_deferral_named(session):
    """Both readings hide it, and both bring it back, from one derivation.

    One source, so the deferral actually holds: a second independent source
    answering the same Promised For is ADR-0084's own wake condition and would
    return the change immediately, which the second half of this test would
    then be unable to distinguish from the dated return.
    """

    project = _project(session, "Snoozed")
    adopted = Adopted(session, project).accepted(CONFLICT).template()
    adopted.answer(
        document="ucm-2026-09.xlsx",
        family="ucm-workbook",
        revision="2026-09",
        value="2026-12-15",
    )
    adopted.adopt()

    before = _portfolio(session).standing(project.id)
    assert before.state == REVIEW_WAITING and before.changes_to_review == 1

    reading = read_review_items(session, project_id=project.id, as_of=NOW)
    item = reading.items[0]
    result = resolve_review_packet(
        session,
        packet_request(
            reading,
            item,
            outcome=DEFER,
            principal=COORDINATOR,
            decided_at=NOW,
            delta_ids=[child.delta_id for child in item.children],
            deferred_until=RETURNS_AT,
        ),
    )
    assert result.status == "saved", result
    session.expire_all()

    held = _portfolio(session).standing(project.id)
    week = read_project_workflow(session, project_id=project.id, as_of=NOW)
    assert held.changes_to_review == 0 == len(week.changes_awaiting_decision)
    assert held.state != REVIEW_WAITING

    returned = read_portfolio(
        session, principal_subject=COORDINATOR.subject, as_of=AFTER_THE_RETURN
    ).standing(project.id)
    later_week = read_project_workflow(
        session, project_id=project.id, as_of=AFTER_THE_RETURN
    )
    assert returned.changes_to_review == len(later_week.changes_awaiting_decision) == 1
    assert returned.state == REVIEW_WAITING


def test_a_newer_independent_source_wakes_snoozed_work_in_both_readings(session):
    """ADR-0084's other wake condition, and the two readings agree about it."""

    project = _reviewing(session)
    reading = read_review_items(session, project_id=project.id, as_of=NOW)
    item = next(one for one in reading.items if one.focused)
    result = resolve_review_packet(
        session,
        focused_request(
            reading,
            item,
            principal=COORDINATOR,
            decided_at=NOW,
            answers=[
                FocusedAnswer(
                    delta_id=child.delta_id,
                    outcome=DEFER,
                    return_date=RETURNS_AT,
                )
                for child in item.children
            ],
        ),
    )
    assert result.status == "saved", result
    session.expire_all()

    standing = _portfolio(session).standing(project.id)
    week = read_project_workflow(session, project_id=project.id, as_of=NOW)
    assert standing.changes_to_review == len(week.changes_awaiting_decision) == 1
    assert standing.state == REVIEW_WAITING


# --- nothing is stored, nothing is completed -------------------------------


def test_the_page_has_no_completion_action_of_any_kind(session, client):
    """No portfolio-work table means nothing to tick, so nothing to tick with."""

    _mixed(session)

    body = client.get("/portfolio").text

    assert "<form" not in body
    assert "<button" not in body
    assert "<input" not in body
    for token in ("done", "complete", "dismiss", "snooze"):
        assert f'value="{token}"' not in body


def test_reading_the_portfolio_records_nothing_about_any_project(session, client):
    """Looking at a project is not an act on it."""

    projects = _mixed(session)
    before = {
        name: _spine_counts(session, project)
        for name, project in projects.items()
    }

    for _ in range(3):
        assert client.get("/portfolio").status_code == 200

    session.expire_all()
    assert {
        name: _spine_counts(session, project)
        for name, project in projects.items()
    } == before


def test_the_reading_is_derived_and_repeats_itself(session):
    """No stored completion flag, owner, cadence, or deferral of its own."""

    _mixed(session)

    assert _portfolio(session).standings == _portfolio(session).standings


def test_a_record_change_moves_the_row_with_no_portfolio_act(session):
    """The row changes because the records changed, never because it was told."""

    project = _blocked(session)
    assert _portfolio(session).standing(project.id).state == ISSUE_BLOCKED

    for document in session.scalars(
        select(Document).where(
            Document.project_id == project.id,
            Document.parse_status != "parsed",
        )
    ).all():
        document.parse_status = "parsed"
    session.flush()

    assert _portfolio(session).standing(project.id).state == ISSUE_READY


# --- one bounded read ------------------------------------------------------


def test_one_bounded_read_produces_the_whole_portfolio(session):
    """Statement count is a constant of the reading, not a multiple of projects."""

    for index in range(2):
        _reviewing(session, f"Small {index}")
    _, small = _count_statements(session, lambda: _portfolio(session))

    for index in range(6):
        _reviewing(session, f"Large {index}")
    large_reading, large = _count_statements(session, lambda: _portfolio(session))

    assert len(large_reading.standings) == 8
    assert small == large, (
        f"the portfolio grew from {small} to {large} statements when it grew "
        "from 2 projects to 8; that is a round trip per project"
    )
    # The design partner's expected portfolio is tens of projects, and the
    # margin here is the whole point: the number below is the reading's shape,
    # not its size. Twelve statements is what it costs today; the ceiling is
    # deliberately close to it so that an accidental extra query is a failure
    # rather than a slow drift back to one round trip per project.
    assert large <= 12, large


# --- how it reads ----------------------------------------------------------


def test_exactly_one_project_takes_focus_and_it_is_the_first_one_waiting(
    session, client
):
    """A keyboard user starts where the work is, not at the document title."""

    quiet = _quiet(session, "Aaa quiet")
    waiting = _reviewing(session, "Zzz reviewing")

    body = client.get("/portfolio").text

    assert body.count("autofocus") == 1
    focused = re.search(r'id="project-([a-z0-9-]+)" tabindex="-1" autofocus', body)
    assert focused is not None
    assert focused.group(1) == waiting.slug
    assert f'id="project-{quiet.slug}" tabindex="-1"' in body


def test_every_project_is_a_region_bound_to_its_own_heading(session, client):
    """Each row is reachable by heading and by landmark."""

    projects = _mixed(session)

    body = client.get("/portfolio").text

    for project in projects.values():
        assert f'aria-labelledby="project-{project.slug}-heading"' in body
        assert f'<h2 id="project-{project.slug}-heading">' in body


def test_the_page_reads_as_one_document_with_descending_headings(session, client):
    """`docs/accessibility-acceptance-checklist.md` §2, for this screen."""

    _mixed(session)

    body = client.get("/portfolio").text

    assert body.count("<main>") == 1
    assert body.count("<h1>") == 1
    levels = [int(level) for level in re.findall(r"<h([1-6])[ >]", body)]
    assert levels[0] == 1
    for previous, level in zip(levels, levels[1:]):
        assert level <= previous + 1, "a heading level is skipped"


def test_every_state_is_printed_in_its_own_words(session, client):
    """Colour is redundant reinforcement; the sentence carries the meaning."""

    projects = _mixed(session)
    reading = _portfolio(session)

    body = client.get("/portfolio").text

    for project in projects.values():
        assert SENTENCES[reading.standing(project.id).state] in body


def test_no_provisional_customer_label_is_printed(session, client):
    """`This Week`, `Review needed`, and `Ready to issue` are not yet approved.

    The repository terminology procedure has not run on any of them, so none
    may be coined here; every state prints the words #536 already prints for
    the same fact. Internal state names stay internal.
    """

    _mixed(session)

    body = client.get("/portfolio").text

    for label in PROVISIONAL_LABELS:
        assert label not in body
    for state in STATE_PRECEDENCE:
        assert state not in body
    assert "Review Packet" not in body
    assert "Release Package" not in body


def test_no_visible_consequence_level_is_asserted(session, client):
    """ADR-0085's three levels need a content inventory that does not exist."""

    _mixed(session)

    body = client.get("/portfolio").text

    for level in VISIBLE_LEVELS:
        assert level not in body


# --- the measurement contract (#558) ---------------------------------------


def test_the_presentation_records_every_project_shown_and_its_state(session, client):
    """One event per reading, carrying the projects and the derived states."""

    projects = _mixed(session)

    with analytics.capture_events() as events:
        assert client.get("/portfolio").status_code == 200

    presented = events.by_family(EventFamily.PORTFOLIO_READING)
    assert len(presented) == 1
    event_record = presented[0]
    assert event_record.occurred_at == NOW, "the cutoff, never a clock"
    assert event_record.binding.code_revision
    assert event_record.payload["project_count"] == len(projects)
    reading = _portfolio(session)
    assert {
        entry["project_id"]: entry["state"]
        for entry in event_record.payload["projects"]
    } == {row.project_id: row.state for row in reading.standings}
    # #491's low-cardinality rule: identity is in the payload, never a label.
    assert set(event_record.metric_labels) == {"surface", "status"}


def test_opening_a_project_from_the_portfolio_is_recorded_once(session, client):
    """Being listed is not being opened; only the click is."""

    quiet = _quiet(session)
    busy = _reviewing(session)

    with analytics.capture_events() as events:
        assert client.get("/portfolio").status_code == 200
        assert client.get(f"/work/{busy.slug}?from=portfolio").status_code == 200

    selected = events.by_family(EventFamily.PROJECT_SELECTION)
    assert [one.payload["project_id"] for one in selected] == [busy.id]
    assert selected[0].payload["state"] == REVIEW_WAITING
    assert selected[0].occurred_at == NOW
    assert quiet.id not in {one.payload["project_id"] for one in selected}


def test_nothing_is_emitted_when_nothing_happens(session, client):
    """A project shown and left alone writes no event of any kind."""

    quiet = _quiet(session)

    with analytics.capture_events() as events:
        assert client.get("/portfolio").status_code == 200

    assert events.by_family(EventFamily.PROJECT_SELECTION) == []
    assert len(events.by_family(EventFamily.PORTFOLIO_READING)) == 1
    assert quiet.id in {
        entry["project_id"]
        for entry in events.by_family(EventFamily.PORTFOLIO_READING)[0].payload[
            "projects"
        ]
    }


def test_opening_a_project_directly_is_not_a_portfolio_selection(session, client):
    """A bookmark or a typed address is not a click on the portfolio."""

    project = _reviewing(session)

    with analytics.capture_events() as events:
        assert client.get(f"/work/{project.slug}").status_code == 200

    assert events.by_family(EventFamily.PROJECT_SELECTION) == []
