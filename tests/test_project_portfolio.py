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

from datetime import date, datetime, timedelta, timezone
import re
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from corridor import analytics
from corridor.analytics import EventFamily
from corridor.config import settings
from corridor.db import engine
from corridor.object_storage import LocalFilesystemStore
from corridor.models import (
    BLOCKED,
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
from corridor.release_authorization import (
    AuthorizationRefused,
    authorize_release_package,
    current_issue_state_is_issued,
)
from corridor.release_candidate import candidate_is_stale
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
from later_revision_support import BASELINE_ROWS, adopt, workbook_bytes
from packet_review_support import Rendition, append_deltas, modify, subject, support
# The candidate fixtures come from #533's own test module for the same reason
# the project shapes come from #536's: a portfolio row that claims an issue is
# ready to authorize is only honest if the thing it read is the very candidate
# `authorize_release_package` would accept, built the very same way.
from test_release_authorization import (
    BINDING,
    RESOLVE_COMMITTED_DATE,
    Adopted as PreparedProject,
    configure as configure_issued_set,
    coverage_named,
    open_delta,
    prepare as prepare_candidate,
    _replace_output_template,
)
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

# A return date a Follow-up Plan named that the declared cutoff has passed.
OVERDUE_RETURN = NOW - timedelta(days=10)


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
@pytest.fixture
def store(tmp_path, monkeypatch):
    """The content-addressed store a prepared candidate's bytes are retained in."""

    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    return LocalFilesystemStore(tmp_path / "artifacts")


def _cross_source_answers(session: Session, project: Project, revision_id: int):
    """Two retained sources answering one Promised For of an adopted baseline.

    ``test_project_workflow._cross_source`` builds this shape over its own
    hand-registered baseline, which is not a shape a project adopted through
    the real importer can also have — one project, one registered baseline
    source. So the same two answers are appended here against the importer's
    own subjects, and the item they produce is the focused, cross-source one a
    Follow-up Plan can be recorded on (#528, #526).
    """

    for name, family, revision, value in (
        ("ucm-2026-09.xlsx", "ucm-workbook", "2026-09", "2026-12-15"),
        ("minutes-2026-09-02.pdf", "meeting-minutes", "2026-09-02", "2027-01-20"),
    ):
        rendition = Rendition(session=session, project=project, name=name)
        fact, segment = rendition.capture(
            fact_type="committed_date",
            value=value,
            subject_key=subject(3),
            date_value=date.fromisoformat(value),
        )
        support(session, project, fact, segment)
        append_deltas(
            session,
            project,
            rendition,
            source_revision=revision,
            source_family=family,
            values=[
                modify(
                    subject_key=subject(3),
                    field_name="committed_date",
                    accepted_value="2026-05-01",
                    proposed_value=value,
                    baseline_revision=revision_id,
                )
            ],
            is_complete_enumerative_source=False,
            row_accounting_sealed=False,
        )
    session.expire_all()


def _prepared(
    session: Session,
    tmp_path,
    store,
    name: str = "Prepared",
    *,
    chase_returning=None,
    policies=(),
    before_preparing=None,
) -> tuple[PreparedProject, object]:
    """One adopted project holding a candidate nobody has authorized.

    Adopt Baseline through the real importer, the configured issued set, and
    #529's own three phases through #533's own fixture — because a portfolio
    row claiming an issue is ready to authorize is only honest if what it read
    is the candidate ``authorize_release_package`` would accept, built the very
    same way.

    ``chase_returning`` records a Follow-up Plan on every source of the
    cross-source question first, with the return date it names, so the
    candidate is bound to a project that already has an outside ask.
    ``policies`` and ``before_preparing`` are what a test needs to bind a
    candidate against an undecided difference an explicit customer policy waits
    on, which is what makes one ``blocked``.
    """

    project = _project(session, name)
    body = workbook_bytes(tmp_path / f"{project.slug}.xlsx", BASELINE_ROWS)
    revision_id, _ = adopt(session, project, body, tmp_path)
    prepared = PreparedProject(
        project=project, revision_id=revision_id, template_bytes=body
    )
    configure_issued_set(session, prepared, policies=policies)
    if chase_returning is not None:
        _cross_source_answers(session, project, revision_id)
        _plan_returning(session, project, return_date=chase_returning)
        # Recording the plans is one Project Record revision of its own, and a
        # candidate prepared from the revision before them would be stale the
        # moment it was attached — which is #529's rule working, not a fixture
        # detail to route around.
        prepared.revision_id = int(
            session.scalar(
                select(func.max(ProjectRecordRevision.id)).where(
                    ProjectRecordRevision.project_id == project.id
                )
            )
        )
    if before_preparing is not None:
        before_preparing(prepared)
    _, _, candidate = prepare_candidate(session, prepared, store)
    session.expire_all()
    return prepared, candidate


def _authorize(session, prepared, candidate, store, *, at=NOW):
    """Issue one prepared candidate, exactly as #533's own route would."""

    return authorize_release_package(
        session,
        project_id=prepared.project.id,
        candidate_id=candidate.id,
        releaser=COORDINATOR,
        authorized_at=at,
        store=store,
        binding=BINDING,
    )




def _blocked(session: Session, name: str = "Blocked") -> Project:
    """An accepted record and a source Corridor could not read."""

    project = _project(session, name)
    adopted = Adopted(session, project).accepted(CONFLICT)
    unreadable = adopted.rendition("permit-2026-09.pdf")
    unreadable.document.parse_status = "failed"
    session.flush()
    adopted.template().issued().adopt()
    return project


def _reviewing(session: Session, name: str = "Reviewing") -> Project:
    """Two sources answering one Promised For differently, nothing decided."""

    project = _project(session, name)
    _cross_source(session, project).issued()
    return project


def _ready(session: Session, name: str = "Ready") -> Project:
    """An accepted record, a registered template, and nothing waiting."""

    project = _project(session, name)
    Adopted(session, project).accepted(CONFLICT).template().issued().adopt()
    return project


def _planned(session: Session, name: str = "Planned") -> Project:
    """Every proposed change already answered Needs coordination.

    Nothing is waiting on the coordinator's own judgement here; the answers are
    waiting on the people the Follow-up Plans named. It is in the mixed fixture
    so that "the portfolio counts the same thing the week counts" is tested
    where the two sets actually differ.
    """

    project = _project(session, name)
    _cross_source(session, project).issued()
    _plan_every_child(session, project)
    return project


def _partly_planned(session: Session, name: str = "Partly planned") -> Project:
    """One source answered Needs coordination, the other still undecided.

    The only shape where a review and an outside answer are both waiting, so
    it is the only one that can tell the ordered sections apart.
    """

    project = _project(session, name)
    _cross_source(session, project).issued()
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


def _plan_returning(session: Session, project: Project, *, return_date) -> int:
    """``_plan_every_child`` with the return date the test declares.

    #536's own builder names one fixed future date, which is exactly the state
    this ticket has to tell apart from a due one, so the date is a parameter
    here and nothing else about the act changes.
    """

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
                    outcome=NEEDS_COORDINATION,
                    question="Which date does the utility actually hold to?",
                    responsible_organization="City Water",
                    return_date=return_date,
                )
                for child in item.children
            ],
        ),
    )
    assert result.status == "saved", result
    session.expire_all()
    return len(item.children)


def _overdue(session: Session, name: str = "Overdue") -> Project:
    """Every change planned, and the date every plan named has already passed.

    The shape that could not exist before #636: nothing is waiting on the
    coordinator's judgement, nothing is prepared to authorize, and somebody was
    asked a question they are now late answering.
    """

    project = _project(session, name)
    _cross_source(session, project).issued()
    _plan_returning(session, project, return_date=OVERDUE_RETURN)
    return project


def _quiet(session: Session, name: str = "Quiet") -> Project:
    """Adopted, with nothing accepted and nothing proposed."""

    project = _project(session, name)
    Adopted(session, project).adopt()
    return project


def _legacy(session: Session, name: str = "Legacy") -> Project:
    """A project whose record still comes from the legacy path."""

    return _project(session, name)


def _mixed(session: Session, tmp_path, store) -> dict[str, Project]:
    """One project in each of the five states, plus the shapes between them.

    ``prepared`` and ``overdue`` join #537's original six because before #636
    neither of them had a state to occupy: every project holding an accepted
    revision claimed the issue rung whether or not anything was prepared, so
    the rung below it was unreachable and the fixture could not show it.
    """

    return {
        "blocked": _blocked(session),
        "reviewing": _reviewing(session),
        "prepared": _prepared(session, tmp_path, store, "Prepared")[0].project,
        "ready": _ready(session),
        "planned": _planned(session),
        "overdue": _overdue(session),
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


def test_every_coordinated_adopted_project_appears_exactly_once(session, client, tmp_path, store):
    """One row per project, on the reading and on the page."""

    projects = _mixed(session, tmp_path, store)

    reading = _portfolio(session)
    body = client.get("/portfolio").text

    shown = [row.project_id for row in reading.standings]
    assert sorted(shown) == sorted(project.id for project in projects.values())
    assert len(shown) == len(set(shown)), "a project is listed twice"
    for project in projects.values():
        assert body.count(f'href="/work/{project.slug}?from=portfolio"') == 1


def test_a_prepared_candidate_is_what_makes_a_project_ready(
    session, tmp_path, store
):
    """The whole defect (#636), stated as one pair of projects.

    Both have an accepted Project Record revision, a registered template and
    mapping, a configured issued set and nothing waiting on anybody. One has a
    candidate #533 would authorize and one has not, and before this change the
    portfolio could not tell them apart — it read "an accepted revision exists"
    and called both of them the issue to approve for sharing.
    """

    prepared, candidate = _prepared(session, tmp_path, store, "Prepared")
    nothing_prepared = _ready(session, "Nothing prepared")

    reading = _portfolio(session)
    ready = reading.standing(prepared.project.id)
    unprepared = reading.standing(nothing_prepared.id)

    assert ready.state == ISSUE_READY and ready.candidate_ready
    assert unprepared.accepted_revision_id is not None, (
        "the proxy the old predicate used is still true of this project"
    )
    assert unprepared.readiness_problems == 0, "and nothing blocks its issue"
    assert not unprepared.candidate_ready
    assert unprepared.state == NO_ACTION


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


def test_the_primary_state_follows_the_declared_precedence(session, tmp_path, store):
    """Blocker, then review, then a candidate to authorize, then follow-up."""

    projects = _mixed(session, tmp_path, store)
    reading = _portfolio(session)

    states = {
        name: reading.standing(project.id).state
        for name, project in projects.items()
    }
    assert states == {
        "blocked": ISSUE_BLOCKED,
        "reviewing": REVIEW_WAITING,
        # A candidate #533 would authorize, and nothing above it waiting.
        "prepared": ISSUE_READY,
        # An accepted revision, a clean issue readiness — and nothing prepared.
        # This is the project the old predicate called ready to approve.
        "ready": NO_ACTION,
        # Every change has a Follow-up Plan and every plan's return date is
        # still ahead: somebody was asked and is not late, which is waiting
        # rather than due.
        "planned": NO_ACTION,
        # The same shape after the date those plans named has passed.
        "overdue": FOLLOW_UP_DUE,
        # A review still waiting outranks both issue states below it, and the
        # Follow-up Plan beside it is a count rather than the state.
        "partly_planned": REVIEW_WAITING,
        "quiet": NO_ACTION,
    }
    assert set(states.values()) == set(STATE_PRECEDENCE), (
        "every rung of the precedence is reachable from a database state"
    )
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
        (dict(changes_to_review=3, follow_up_due=2, readiness_problems=1,
              accepted_revision_id=7, candidate_ready=True), ISSUE_BLOCKED),
        (dict(changes_to_review=0, follow_up_due=0, readiness_problems=1,
              accepted_revision_id=7, candidate_ready=True), ISSUE_BLOCKED),
        # Then a review affecting the current issue, over both issue states.
        (dict(changes_to_review=1, follow_up_due=4, readiness_problems=0,
              accepted_revision_id=7, candidate_ready=True), REVIEW_WAITING),
        (dict(changes_to_review=1, follow_up_due=0, readiness_problems=0,
              accepted_revision_id=None, candidate_ready=False), REVIEW_WAITING),
        # Then a candidate ready for authorization, over accepted follow-up.
        # This is the maintainer's decision on #636 as one row: a due chase is
        # a count beside a bounded act Corridor can record and clear, never
        # ahead of it.
        (dict(changes_to_review=0, follow_up_due=2, readiness_problems=0,
              accepted_revision_id=7, candidate_ready=True), ISSUE_READY),
        # Then accepted follow-up. An accepted revision on its own no longer
        # occupies the rung above, which is the whole of the defect: before
        # #636 this row read ISSUE_READY and FOLLOW_UP_DUE was unreachable.
        (dict(changes_to_review=0, follow_up_due=1, readiness_problems=0,
              accepted_revision_id=7, candidate_ready=False), FOLLOW_UP_DUE),
        (dict(changes_to_review=0, follow_up_due=1, readiness_problems=0,
              accepted_revision_id=None, candidate_ready=False), FOLLOW_UP_DUE),
        # A Follow-up Plan somebody is waiting on is not a due one, so a
        # project with plans and no due plan asks for nothing.
        (dict(changes_to_review=0, follow_up_due=0, readiness_problems=0,
              accepted_revision_id=7, candidate_ready=False), NO_ACTION),
        # Coverage cannot block an issue that does not exist yet, which is
        # #536's own rule for its issue section.
        (dict(changes_to_review=0, follow_up_due=0, readiness_problems=2,
              accepted_revision_id=None, candidate_ready=False), NO_ACTION),
        (dict(changes_to_review=0, follow_up_due=0, readiness_problems=0,
              accepted_revision_id=None, candidate_ready=False), NO_ACTION),
    ],
)
def test_the_precedence_is_exactly_the_declared_order(inputs, expected):
    """Every rung of the precedence, including the ties between two rungs."""

    assert primary_state(**inputs) == expected


def test_the_accepted_revision_alone_no_longer_reaches_the_issue_rung():
    """The old predicate, stated as the one input it read, and refused.

    Every other input here says the project needs nothing. Under #537's
    predicate the accepted revision alone returned ``ISSUE_READY``, which is
    what made the rung below it unreachable from any database state.
    """

    assert primary_state(
        changes_to_review=0,
        follow_up_due=0,
        readiness_problems=0,
        accepted_revision_id=7,
        candidate_ready=False,
    ) == NO_ACTION


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


def test_a_due_follow_up_stays_a_count_beside_a_ready_candidate(
    session, tmp_path, store
):
    """The maintainer's decision on #636, proved from records rather than a table.

    One project, one candidate #533 would authorize, and one Follow-up Plan
    whose return date has already passed. The chase is real and it is late; it
    is still a count, because authorizing the prepared candidate is a bounded
    act Corridor can record and clear, and the pilot follow-up bundle sends
    nothing, records no delivery, and establishes nothing about whether the
    external interaction happened.
    """

    prepared, _ = _prepared(
        session, tmp_path, store, "Chased", chase_returning=OVERDUE_RETURN
    )
    planned = len(
        session.scalars(
            select(DeltaFollowUpPlan).where(
                DeltaFollowUpPlan.project_id == prepared.project.id
            )
        ).all()
    )

    standing = _portfolio(session).standing(prepared.project.id)

    assert planned > 0
    assert standing.follow_up_waiting == planned
    assert standing.follow_up_due == planned, "and every one of them is late"
    assert standing.changes_to_review == 0
    assert standing.candidate_ready and standing.state == ISSUE_READY
    assert dict(standing.counts)["Follow-up Plans waiting on an answer"] == planned
    assert dict(standing.counts)["Past the date the plan named"] == planned


# --- what makes a candidate one somebody may authorize ---------------------


def test_an_authorized_candidate_is_not_offered_a_second_time(
    session, tmp_path, store
):
    """The issue went out, so the row that offered it must stop offering it.

    #529's own staleness rule says why: the comparison baseline this candidate
    was prepared against is no longer the current one, because this candidate
    is what moved it.
    """

    prepared, candidate = _prepared(session, tmp_path, store, "Issued")
    assert _portfolio(session).standing(prepared.project.id).state == ISSUE_READY

    _authorize(session, prepared, candidate, store)
    session.expire_all()

    standing = _portfolio(session).standing(prepared.project.id)
    assert not standing.candidate_ready
    assert standing.state == NO_ACTION


def test_a_fresh_candidate_for_an_issue_already_sent_is_not_ready(
    session, tmp_path, store
):
    """The other "already authorized" question, and the one only #533 answers.

    This candidate is not stale by any of #529's three terms: the profile has
    not moved, the accepted revision has not moved, and it names the current
    package as its own baseline. It still describes exactly the issue the
    customer already has, and offering it would ask somebody to send the same
    package twice — which is what ``current_issue_state_is_issued`` is for.
    """

    prepared, first = _prepared(session, tmp_path, store, "Sent already")
    _authorize(session, prepared, first, store)
    session.expire_all()

    _, _, second = prepare_candidate(session, prepared, store)
    session.expire_all()

    assert not candidate_is_stale(session, second, as_of=NOW), (
        "the second candidate is fresh by every one of #529's own terms"
    )
    assert current_issue_state_is_issued(session, prepared.project.id, as_of=NOW)
    standing = _portfolio(session).standing(prepared.project.id)
    assert not standing.candidate_ready
    assert standing.state == NO_ACTION


def test_a_candidate_the_configuration_moved_past_is_not_ready(
    session, tmp_path, store
):
    """#529's profile term: what the project issues changed after preparation."""

    prepared, _ = _prepared(session, tmp_path, store, "Reconfigured")
    assert _portfolio(session).standing(prepared.project.id).candidate_ready

    configure_issued_set(
        session, prepared, effective_from=datetime(2026, 2, 1, tzinfo=timezone.utc)
    )
    session.expire_all()

    standing = _portfolio(session).standing(prepared.project.id)
    assert not standing.candidate_ready
    assert standing.state == NO_ACTION


def test_a_candidate_whose_template_was_replaced_is_not_ready(
    session, tmp_path, store
):
    """#533's own revalidation term, which staleness does not cover.

    The sealed artifacts were rendered through a registration the project no
    longer renders through, so ``authorize_release_package`` refuses. A row
    still saying "ready to authorize" would be promising an act #533 would
    turn down.
    """

    prepared, candidate = _prepared(session, tmp_path, store, "Retemplated")
    assert _portfolio(session).standing(prepared.project.id).candidate_ready

    _replace_output_template(session, prepared)
    session.expire_all()

    assert not _portfolio(session).standing(prepared.project.id).candidate_ready
    with pytest.raises(AuthorizationRefused):
        _authorize(session, prepared, candidate, store)


def test_a_blocked_candidate_is_not_ready(session, tmp_path, store):
    """A complete candidate can still be unauthorizable, and this row says so.

    Every configured artifact was rendered and retained; what blocks it is the
    undecided difference an explicit customer policy waits on, bound into the
    candidate's own identity. Only a newly prepared candidate clears that, so
    the row must not offer this one.
    """

    prepared, candidate = _prepared(
        session,
        tmp_path,
        store,
        "Blocked candidate",
        policies=[RESOLVE_COMMITTED_DATE],
        before_preparing=lambda made: open_delta(session, made),
    )

    assert candidate.readiness == BLOCKED
    assert not _portfolio(session).standing(prepared.project.id).candidate_ready


# --- the follow-up rung, which had no reachable state at all ---------------


def test_a_due_follow_up_reaches_the_state_below_the_issue_rung(
    session, tmp_path, store
):
    """``FOLLOW_UP_DUE`` from records, which was impossible before #636.

    Every proposed change on this project has a recorded Follow-up Plan, so
    nothing waits on the coordinator's own judgement; the record holds an
    accepted revision, which is precisely what used to put it on the issue
    rung; nothing is prepared; and the date every plan named has passed.
    """

    project = _overdue(session, "Late answers")

    standing = _portfolio(session).standing(project.id)

    assert standing.accepted_revision_id is not None
    assert standing.changes_to_review == 0
    assert not standing.candidate_ready
    assert standing.follow_up_due == standing.follow_up_waiting > 0
    assert standing.state == FOLLOW_UP_DUE
    assert not standing.quiet


def test_a_plan_inside_the_window_it_named_is_waiting_and_not_due(session):
    """"Waiting on somebody" and "due for coordinator action" are not one state.

    The same project as above with one thing changed: the date each plan named
    has not arrived. Nobody is late, so there is nothing for the coordinator to
    do, and #425's own band rule is what says so.
    """

    project = _project(session, "Answers not yet due")
    _cross_source(session, project).issued()
    planned = _plan_returning(session, project, return_date=RETURNS_AT)

    standing = _portfolio(session).standing(project.id)

    assert planned > 0 and standing.follow_up_waiting == planned
    assert standing.follow_up_due == 0
    assert standing.state == NO_ACTION
    assert dict(standing.counts)["Follow-up Plans waiting on an answer"] == planned


def test_a_plan_that_named_no_return_date_is_due(session):
    """A plan with no date has no date that will ever make it due.

    #425 puts it in its own band above the awaiting one for exactly that
    reason, and this row follows that ordering rather than inventing a second
    opinion about it.
    """

    project = _project(session, "No date named")
    _cross_source(session, project).issued()
    planned = _plan_returning(session, project, return_date=None)

    standing = _portfolio(session).standing(project.id)

    assert standing.follow_up_waiting == planned > 0
    assert standing.follow_up_overdue == 0, "nothing is past a date it named"
    assert standing.follow_up_due == planned
    assert standing.state == FOLLOW_UP_DUE


# --- the portfolio and the project's own week ------------------------------


def test_the_portfolio_and_the_project_week_never_disagree(session, tmp_path, store):
    """Same records, same functions, so the same state and the same numbers."""

    projects = _mixed(session, tmp_path, store)
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
        if standing.quiet:
            # Two of #536's own sections still count unconditionally, and
            # both belong to its lane rather than this one. Its Issue section
            # counts any accepted revision with no readiness problem as one
            # outstanding thing (`project_workflow._issue_section`, the `else`
            # branch that sets `outstanding = 1`, whose summary still says
            # preparing and approving an issue are not built), and its
            # Follow-up section counts every live plan whether or not the date
            # it named has arrived (`project_workflow._follow_up_section`,
            # `outstanding=len(needs)`). #636 taught this reading to ask
            # whether a candidate exists and whether a plan is actually due;
            # those two sections have not learned either yet. The numbers
            # themselves still agree exactly — the assertions above are
            # unconditional — and it is only which sections make a *state*
            # non-quiet that the two readings now scope differently.
            assert waiting <= {ISSUE, FOLLOW_UP}, (
                "a quiet project has nothing waiting except #536's own two "
                "unconditional section counts"
            )
        else:
            assert waiting, "a project the portfolio calls busy has a busy week"
        if standing.state == REVIEW_WAITING:
            assert REVIEW in waiting
        if standing.state == FOLLOW_UP_DUE:
            assert FOLLOW_UP in waiting
        if standing.state in (ISSUE_BLOCKED, ISSUE_READY):
            assert ISSUE in waiting


def test_selecting_a_project_opens_the_first_incomplete_section(session, client, tmp_path, store):
    """The link goes to #536's own landing section, not to a second opinion."""

    projects = _mixed(session, tmp_path, store)
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
    adopted = Adopted(session, project).accepted(CONFLICT).template().issued()
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


def test_the_page_has_no_completion_action_of_any_kind(session, client, tmp_path, store):
    """No portfolio-work table means nothing to tick, so nothing to tick with."""

    _mixed(session, tmp_path, store)

    body = client.get("/portfolio").text

    assert "<form" not in body
    assert "<button" not in body
    assert "<input" not in body
    for token in ("done", "complete", "dismiss", "snooze"):
        assert f'value="{token}"' not in body


def test_reading_the_portfolio_records_nothing_about_any_project(session, client, tmp_path, store):
    """Looking at a project is not an act on it."""

    projects = _mixed(session, tmp_path, store)
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


def test_the_reading_is_derived_and_repeats_itself(session, tmp_path, store):
    """No stored completion flag, owner, cadence, or deferral of its own."""

    _mixed(session, tmp_path, store)

    assert _portfolio(session).standings == _portfolio(session).standings


def test_a_record_change_moves_the_row_with_no_portfolio_act(
    session, tmp_path, store
):
    """The row changes because the records changed, never because it was told.

    It also proves the readiness rung still stands over a prepared candidate: a
    source Corridor could not read blocks the project whose candidate is
    otherwise ready to authorize, and reading the source is what releases it.
    """

    prepared, _ = _prepared(session, tmp_path, store, "Unreadable source")
    unreadable = Rendition(session, prepared.project, "permit-2026-09.pdf")
    unreadable.document.parse_status = "failed"
    session.flush()

    blocked = _portfolio(session).standing(prepared.project.id)
    assert blocked.state == ISSUE_BLOCKED
    assert blocked.candidate_ready, "the candidate itself is untouched by this"

    for document in session.scalars(
        select(Document).where(
            Document.project_id == prepared.project.id,
            Document.parse_status != "parsed",
        )
    ).all():
        document.parse_status = "parsed"
    session.flush()

    assert _portfolio(session).standing(prepared.project.id).state == ISSUE_READY


# --- one bounded read ------------------------------------------------------


def test_one_bounded_read_produces_the_whole_portfolio(session, tmp_path, store):
    """Statement count is a constant of the reading, not a multiple of projects."""

    _prepared(session, tmp_path, store, "Small prepared")
    _reviewing(session, "Small 0")
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
    # not its size. Twenty statements is what it costs today; the ceiling is
    # deliberately close to it so that an accidental extra query is a failure
    # rather than a slow drift back to one round trip per project. It rose from
    # twelve when Issue readiness learned to state what a project is configured
    # to issue (#641), and from fifteen when the issue rung stopped guessing at
    # a candidate and read one (#636): five more, and five is the whole cost at
    # any portfolio size — every prepared candidate, every authorized package,
    # the effective issue profile of every project and its configured
    # artifacts, and every registered template and mapping. Each is the batched
    # sibling of the single-project reader #529 or #533 already owned, which is
    # what the equality above proves and what a per-candidate `candidate_is_stale`
    # would have broken immediately.
    assert large <= 20, large


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


def test_every_project_is_a_region_bound_to_its_own_heading(session, client, tmp_path, store):
    """Each row is reachable by heading and by landmark."""

    projects = _mixed(session, tmp_path, store)

    body = client.get("/portfolio").text

    for project in projects.values():
        assert f'aria-labelledby="project-{project.slug}-heading"' in body
        assert f'<h2 id="project-{project.slug}-heading">' in body


def test_the_page_reads_as_one_document_with_descending_headings(session, client, tmp_path, store):
    """`docs/accessibility-acceptance-checklist.md` §2, for this screen."""

    _mixed(session, tmp_path, store)

    body = client.get("/portfolio").text

    assert body.count("<main>") == 1
    assert body.count("<h1>") == 1
    levels = [int(level) for level in re.findall(r"<h([1-6])[ >]", body)]
    assert levels[0] == 1
    for previous, level in zip(levels, levels[1:]):
        assert level <= previous + 1, "a heading level is skipped"


def test_every_state_is_printed_in_its_own_words(session, client, tmp_path, store):
    """Colour is redundant reinforcement; the sentence carries the meaning."""

    projects = _mixed(session, tmp_path, store)
    reading = _portfolio(session)

    body = client.get("/portfolio").text

    for project in projects.values():
        assert SENTENCES[reading.standing(project.id).state] in body


def test_no_provisional_customer_label_is_printed(session, client, tmp_path, store):
    """`This Week`, `Review needed`, and `Ready to issue` are not yet approved.

    The repository terminology procedure has not run on any of them, so none
    may be coined here; every state prints the words #536 already prints for
    the same fact. Internal state names stay internal.
    """

    _mixed(session, tmp_path, store)

    body = client.get("/portfolio").text

    for label in PROVISIONAL_LABELS:
        assert label not in body
    for state in STATE_PRECEDENCE:
        assert state not in body
    assert "Review Packet" not in body
    assert "Release Package" not in body


def test_the_portfolio_prints_no_consequence_level(session, client, tmp_path, store):
    """A level belongs to one packet, and this reading shows no packets (#641).

    ADR-0085's three headings are derived now, and the review screen and the
    project's own week both print them. The portfolio is a row per project with
    a count of questions on it; printing one packet's level beside a count of
    several would be a fourth state nobody derived.
    """

    _mixed(session, tmp_path, store)

    body = client.get("/portfolio").text

    for level in VISIBLE_LEVELS:
        assert level not in body


# --- the measurement contract (#558) ---------------------------------------


def test_the_presentation_records_every_project_shown_and_its_state(session, client, tmp_path, store):
    """One event per reading, carrying the projects and the derived states."""

    projects = _mixed(session, tmp_path, store)

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
