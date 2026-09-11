"""Deferred work comes back, says why, and can be brought back early (#835).

Creating a deferral was already proved.  What happened afterwards was not.  The
week printed a bare count of deferred changes, the return instant
``review_packet_reading`` computed was never rendered anywhere a coordinator
could see it, nothing let them correct a wrong date or open one deliberately,
and a change that came back said nothing at all about why it was back.

So the properties under test are the ones the ticket names, and they are about
*scheduling* rather than about the record.  ADR-0084 settles that a deferral is
Work List scheduling: the Proposed Delta stays open, the accepted record is
untouched, and no Project Record revision is written.  That is asserted here
over the whole project graph rather than over a table this file picked, because
a hand-picked table set is how a write lands unseen (``record_counts``).

Two walks run across separate visits with the clock advanced, because "it comes
back" is a claim about a later visit and cannot be observed inside one.  The
clock is the declared seam ``get_review_clock``, never a sleep.

What is deliberately *not* here: nothing in this file reverses a decision.
#834's Undo compensates for a recorded semantic act; every act below leaves the
earlier scheduling receipt exactly where it is and appends a newer one beside
it, which is why a deferral can be rescheduled without an Undo and an Undo is
still the only thing that can take an acceptance back.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import html
import pathlib
import re
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

import corridor.web.app
from corridor import web_boundary
from corridor.models import DeltaDeferral, Project, ProjectRecordRevision
from corridor.operating_mode import adopt_project_baseline
from corridor.packet_review import read_review_items
from corridor.principals import HumanPrincipal
from corridor.project_workflow import read_project_workflow
from corridor.review_packet_reading import (
    RETURNED_DATE_REACHED,
    RETURNED_OPENED_EARLY,
    RETURNED_WAKE_CONDITION,
)
from corridor.web.app import (
    app,
    get_human_principal,
    get_review_clock,
    get_session,
)

from access_support import seed_membership
from record_counts import nothing_written
from packet_review_support import (
    Rendition,
    accept_baseline_fact,
    append_deltas,
    configure_issue,
    field_mapping,
    modify,
    register_baseline,
    register_field_mapping,
    register_output_template,
    register_source_row,
    subject,
    support,
)


COORDINATOR = HumanPrincipal("local:coordinator")
CONFIGURED_FROM = datetime(2026, 1, 5, tzinfo=timezone.utc)
# The three visits one walk makes.  The gaps are days, so a date-valued return
# instant lies strictly between two of them and nothing depends on an hour.
FIRST_VISIT = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
RETURNS_ON = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
SECOND_VISIT = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
THIRD_VISIT = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
ACCEPTED = "2026-11-01"
CONFLICT = 42
COMMITTED = "committed_date"

# The one write a page view makes that is not a change to the record: the
# Product Proving receipt for the request itself.
A_PAGE_VIEW_WRITES = {"product_proving_frontend_requests", "audit_log"}
# What a scheduling act adds on top of that, and the whole of what it adds.
SCHEDULING_WRITES = A_PAGE_VIEW_WRITES | {"delta_deferrals"}


class Clock:
    """The declared review seam, moved by the test rather than by time."""

    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def advance_to(self, moment: datetime) -> None:
        self.moment = moment


@pytest.fixture
def clock() -> Clock:
    return Clock(FIRST_VISIT)


@pytest.fixture
def project(session: Session) -> Project:
    row = Project(
        slug=f"deferred-{uuid4().hex[:8]}", name="Deferred", is_synthetic=True
    )
    session.add(row)
    session.flush()
    seed_membership(session, row, COORDINATOR)
    return row


@pytest.fixture
def client(session, clock):
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: COORDINATOR
    app.dependency_overrides[get_review_clock] = lambda: clock
    with TestClient(app) as made:
        yield made
    app.dependency_overrides.clear()


# --- one adopted project with one proposed change --------------------------


class Adopted:
    """The smallest adopted project that can hold a deferrable change."""

    def __init__(self, session: Session, project: Project) -> None:
        self.session = session
        self.project = project
        self.source = Rendition(session, project, "ucm-2026-08.xlsx")
        self.revision_of: dict[tuple[str, str], int] = {}
        self.renditions: dict[str, Rendition] = {}

    def build(self) -> "Adopted":
        fact, _ = self.source.capture(
            fact_type=COMMITTED, value=ACCEPTED, subject_key=subject(CONFLICT)
        )
        revision = accept_baseline_fact(self.session, self.project, fact)
        self.revision_of[(subject(CONFLICT), COMMITTED)] = revision
        baseline = register_baseline(
            self.session, self.project, self.source.document, revision
        )
        register_source_row(
            self.session,
            self.project,
            baseline,
            row_number=CONFLICT,
            business_identity=f"U-{CONFLICT:03d}",
        )
        register_output_template(
            self.session, self.project, identity="district-ucm-template", version="v3"
        )
        register_field_mapping(
            self.session, self.project, field_mapping(COMMITTED)
        )
        configure_issue(
            self.session,
            self.project,
            principal=COORDINATOR,
            effective_from=CONFIGURED_FROM,
        )
        adopt_project_baseline(
            self.session,
            project_id=self.project.id,
            adopted_by_principal="local:adopter",
            baseline_source_sha256=self.source.document.sha256,
            importer_identity="deferred_return_fixture",
            importer_version="v1",
            idempotency_key=f"adopt:{uuid4().hex[:10]}",
        )
        self.session.expire_all()
        return self

    def answer(self, *, document: str, family: str, revision: str, value: str):
        """One source's own Promised For, captured, supported and proposed."""

        rendition = self.renditions.setdefault(
            document, Rendition(self.session, self.project, document)
        )
        fact, segment = rendition.capture(
            fact_type=COMMITTED, value=value, subject_key=subject(CONFLICT)
        )
        support(self.session, self.project, fact, segment)
        return append_deltas(
            self.session,
            self.project,
            rendition,
            source_revision=revision,
            source_family=family,
            values=[
                modify(
                    subject_key=subject(CONFLICT),
                    field_name=COMMITTED,
                    accepted_value=ACCEPTED,
                    proposed_value=value,
                    baseline_revision=self.revision_of[(subject(CONFLICT), COMMITTED)],
                )
            ],
            is_complete_enumerative_source=False,
            row_accounting_sealed=False,
        )


def _one_change(session: Session, project: Project) -> int:
    """One adopted project holding exactly one open Proposed Delta."""

    adopted = Adopted(session, project).build()
    deltas = adopted.answer(
        document="utility-letter.pdf",
        family="utility-letter",
        revision="2026-09-01",
        value="2027-02-15",
    )
    return deltas[0].id


def _defer(
    session: Session, client, clock: Clock, project: Project, delta_id: int, *, until: str
) -> None:
    """Defer the one open change through the review screen that owns Defer."""

    reading = read_review_items(session, project_id=project.id, as_of=clock())
    item = reading.reading.item_for(delta_id)
    assert item is not None
    response = client.post(
        f"/review/{project.slug}",
        data={
            "item_key": item.item_key,
            "outcome": "defer",
            "child": str(delta_id),
            "defer_until": until,
        },
    )
    assert response.status_code == 200, response.text


def _revision_count(session: Session, project: Project) -> int:
    return session.scalar(
        select(func.count()).where(ProjectRecordRevision.project_id == project.id)
    )


def _text(markup: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", markup))


# --- the coordinator can see what is deferred, and when it returns ---------


def test_the_week_names_every_deferred_change_and_the_date_it_comes_back(
    session, project, clock, client
):
    """The date the coordinator recorded is on the page, not only in history."""

    delta_id = _one_change(session, project)
    _defer(session, client, clock, project, delta_id, until="2026-10-01")
    session.expire_all()

    workflow = read_project_workflow(session, project_id=project.id, as_of=FIRST_VISIT)
    assert [held.delta_id for held in workflow.deferred] == [delta_id]
    held = workflow.deferred[0]
    assert held.subject_name == f"U-{CONFLICT:03d}"
    assert held.returns_at == RETURNS_ON.date()
    assert held.return_sentence == "Comes back on 2026-10-01"

    page = _text(client.get(f"/work/{project.slug}").text)
    assert "Deferred until later" in page
    assert f"U-{CONFLICT:03d}" in page
    assert "Comes back on 2026-10-01" in page


def test_a_project_with_nothing_deferred_prints_no_deferred_section(
    session, project, client
):
    """The section is the deferred work, not a heading that is always there."""

    _one_change(session, project)

    assert read_project_workflow(
        session, project_id=project.id, as_of=FIRST_VISIT
    ).deferred == ()
    assert "Deferred until later" not in _text(
        client.get(f"/work/{project.slug}").text
    )


# --- scheduling is attributable, and writes no Project Record revision -----


def test_changing_the_return_date_is_attributable_and_writes_no_revision(
    session, project, clock, client
):
    """A wrong date is corrected by a second receipt, not by an edit."""

    delta_id = _one_change(session, project)
    _defer(session, client, clock, project, delta_id, until="2026-10-01")
    session.expire_all()
    revisions = _revision_count(session, project)

    clock.advance_to(SECOND_VISIT)
    with nothing_written(session, project.id, apart_from=SCHEDULING_WRITES):
        response = client.post(
            f"/work/{project.slug}/schedule",
            data={
                "delta_id": str(delta_id),
                "scheduling": "reschedule",
                "returns_on": "2026-11-15",
                "scheduling_reason": "the meeting moved",
            },
        )
    assert response.status_code == 201, response.text
    session.expire_all()

    assert _revision_count(session, project) == revisions
    receipts = session.scalars(
        select(DeltaDeferral)
        .where(DeltaDeferral.delta_id == delta_id)
        .order_by(DeltaDeferral.id)
    ).all()
    assert len(receipts) == 2, "the first schedule is kept, not edited"
    latest = receipts[-1]
    assert latest.scheduled_by_principal == COORDINATOR.subject
    assert latest.deferred_at == SECOND_VISIT
    assert latest.deferred_until == datetime(2026, 11, 15, tzinfo=timezone.utc)
    assert latest.reason == "the meeting moved"

    workflow = read_project_workflow(session, project_id=project.id, as_of=FIRST_VISIT)
    assert workflow.deferred[0].returns_at.isoformat() == "2026-11-15"


def test_opening_a_deferred_change_early_writes_no_revision_either(
    session, project, clock, client
):
    """"Bring this back now" is scheduling; the change is open before and after."""

    delta_id = _one_change(session, project)
    _defer(session, client, clock, project, delta_id, until="2026-10-01")
    session.expire_all()
    revisions = _revision_count(session, project)

    clock.advance_to(SECOND_VISIT)
    with nothing_written(session, project.id, apart_from=SCHEDULING_WRITES):
        response = client.post(
            f"/work/{project.slug}/schedule",
            data={"delta_id": str(delta_id), "scheduling": "open_now"},
        )
    assert response.status_code == 201, response.text
    session.expire_all()

    assert _revision_count(session, project) == revisions
    reading = read_review_items(session, project_id=project.id, as_of=SECOND_VISIT)
    assert reading.reading.actionable_delta_ids == (delta_id,)
    assert reading.reading.deferred == ()
    assert read_project_workflow(
        session, project_id=project.id, as_of=SECOND_VISIT
    ).deferred == ()


def test_the_week_cannot_start_a_deferral_it_only_reschedules_one(
    session, project, client
):
    """A change with no live schedule is refused here and decided on Review.

    This is what keeps the page from growing a second control over the same
    change (ADR-0085): the only surface that can *begin* a deferral is still
    the one that offers the change exactly once.
    """

    delta_id = _one_change(session, project)

    with nothing_written(session, project.id, apart_from=A_PAGE_VIEW_WRITES):
        response = client.post(
            f"/work/{project.slug}/schedule",
            data={
                "delta_id": str(delta_id),
                "scheduling": "reschedule",
                "returns_on": "2026-11-15",
            },
        )
    assert response.status_code == 409
    assert "not deferred under this reading" in _text(response.text)


def test_a_reschedule_with_no_date_is_refused_and_records_nothing(
    session, project, clock, client
):
    delta_id = _one_change(session, project)
    _defer(session, client, clock, project, delta_id, until="2026-10-01")
    session.expire_all()

    with nothing_written(session, project.id, apart_from=A_PAGE_VIEW_WRITES):
        response = client.post(
            f"/work/{project.slug}/schedule",
            data={"delta_id": str(delta_id), "scheduling": "reschedule"},
        )
    assert response.status_code == 400
    assert "Give the date this change should come back on" in _text(response.text)


# --- a change that came back says why --------------------------------------


def test_a_change_back_on_its_own_date_says_the_date_was_reached(
    session, project, clock, client
):
    delta_id = _one_change(session, project)
    _defer(session, client, clock, project, delta_id, until="2026-10-01")
    session.expire_all()

    clock.advance_to(THIRD_VISIT)
    reading = read_review_items(session, project_id=project.id, as_of=THIRD_VISIT)
    standing = next(
        row for row in reading.reading.standings if row.delta_id == delta_id
    )
    assert standing.returned is not None
    assert standing.returned.reason == RETURNED_DATE_REACHED
    child = reading.reading.item_for(delta_id)
    assert child is not None
    sentence = next(
        row.return_sentence
        for item in reading.items
        for row in item.children
        if row.delta_id == delta_id
    )
    assert sentence == (
        "The date it was deferred until, 2026-10-01, has been reached"
    )


def test_a_change_woken_by_a_newer_source_says_which_condition_woke_it(
    session, project, clock, client
):
    """ADR-0084's wake condition, named beside the words that were recorded."""

    adopted = Adopted(session, project).build()
    first = adopted.answer(
        document="utility-letter.pdf",
        family="utility-letter",
        revision="2026-09-01",
        value="2027-02-15",
    )[0]
    _defer(session, client, clock, project, first.id, until="2026-12-01")
    session.expire_all()

    adopted.answer(
        document="meeting-minutes.pdf",
        family="meeting-minutes",
        revision="2026-09-02",
        value="2027-03-20",
    )
    session.expire_all()

    reading = read_review_items(session, project_id=project.id, as_of=FIRST_VISIT)
    standing = next(
        row for row in reading.reading.standings if row.delta_id == first.id
    )
    assert standing.returned is not None
    assert standing.returned.reason == RETURNED_WAKE_CONDITION
    sentence = next(
        row.return_sentence
        for item in reading.items
        for row in item.children
        if row.delta_id == first.id
    )
    assert sentence == (
        "A newer source version arrived for this subject and field"
    )


def test_a_change_brought_back_early_says_who_brought_it_back(
    session, project, clock, client
):
    delta_id = _one_change(session, project)
    _defer(session, client, clock, project, delta_id, until="2026-10-01")
    session.expire_all()

    clock.advance_to(SECOND_VISIT)
    response = client.post(
        f"/work/{project.slug}/schedule",
        data={
            "delta_id": str(delta_id),
            "scheduling": "open_now",
            "scheduling_reason": "the utility called",
        },
    )
    assert response.status_code == 201, response.text
    session.expire_all()

    reading = read_review_items(session, project_id=project.id, as_of=SECOND_VISIT)
    standing = next(
        row for row in reading.reading.standings if row.delta_id == delta_id
    )
    assert standing.returned is not None
    assert standing.returned.reason == RETURNED_OPENED_EARLY
    sentence = next(
        row.return_sentence
        for item in reading.items
        for row in item.children
        if row.delta_id == delta_id
    )
    assert sentence == (
        "local:coordinator brought this back on 2026-09-20, before the date "
        "it was waiting for: the utility called"
    )
    item = reading.reading.item_for(delta_id)
    assert item is not None
    page = _text(
        client.get(
            f"/review/{project.slug}",
            params={"item": item.item_key},
        ).text
    )
    assert "Back from a deferral" in page
    assert "before the date it was waiting for" in page


# --- the walk: defer, return, settle, across separate visits ---------------


def test_defer_then_return_then_settle_across_three_visits(
    session, project, clock, client
):
    """One coordinator, three visits, one clock the test moves itself.

    Visit one defers the change to a date.  Visit two is before that date: the
    change is not offered, and the week says when it comes back.  Visit three
    is after it: the change is offered again, says the date was reached, and is
    settled.  Nothing between the visits writes a Project Record revision.
    """

    delta_id = _one_change(session, project)

    # Visit one: defer it.
    _defer(session, client, clock, project, delta_id, until="2026-10-01")
    session.expire_all()
    after_defer = _revision_count(session, project)

    # Visit two, before the date.  Still open, still not offered, and the week
    # says when it comes back.
    clock.advance_to(SECOND_VISIT)
    second = read_review_items(session, project_id=project.id, as_of=SECOND_VISIT)
    assert second.reading.actionable_delta_ids == ()
    assert [row.delta_id for row in second.reading.deferred] == [delta_id]
    assert delta_id in second.reading.open_delta_ids
    page = _text(client.get(f"/work/{project.slug}").text)
    assert "Comes back on 2026-10-01" in page
    assert _revision_count(session, project) == after_defer

    # Visit three, after the date.  Offered again, and it says why.
    clock.advance_to(THIRD_VISIT)
    third = read_review_items(session, project_id=project.id, as_of=THIRD_VISIT)
    assert third.reading.actionable_delta_ids == (delta_id,)
    item = third.reading.item_for(delta_id)
    assert item is not None
    settle = client.post(
        f"/review/{project.slug}",
        data={
            "item_key": item.item_key,
            "outcome": "keep_current",
            "child": str(delta_id),
        },
    )
    assert settle.status_code == 200, settle.text
    session.expire_all()

    settled = read_review_items(session, project_id=project.id, as_of=THIRD_VISIT)
    assert delta_id not in settled.reading.open_delta_ids
    assert _revision_count(session, project) == after_defer + 1, (
        "exactly one revision, written by the semantic decision and by "
        "nothing the scheduling did"
    )


def test_a_reschedule_moves_the_visit_the_change_comes_back_on(
    session, project, clock, client
):
    """The corrected date is the one the reading obeys, not the first one."""

    delta_id = _one_change(session, project)
    _defer(session, client, clock, project, delta_id, until="2026-10-01")
    session.expire_all()

    clock.advance_to(SECOND_VISIT)
    moved = client.post(
        f"/work/{project.slug}/schedule",
        data={
            "delta_id": str(delta_id),
            "scheduling": "reschedule",
            "returns_on": "2026-12-01",
        },
    )
    assert moved.status_code == 201, moved.text
    session.expire_all()

    clock.advance_to(THIRD_VISIT)
    assert read_review_items(
        session, project_id=project.id, as_of=THIRD_VISIT
    ).reading.actionable_delta_ids == ()

    later = THIRD_VISIT + timedelta(days=70)
    clock.advance_to(later)
    assert read_review_items(
        session, project_id=project.id, as_of=later
    ).reading.actionable_delta_ids == (delta_id,)


def test_a_retried_schedule_says_so_rather_than_announcing_a_date_twice(
    session, project, clock, client
):
    """The command is idempotent on the act's identity, and the page says so.

    ``defer_proposed_delta`` returns the receipt already written when the same
    person schedules the same delta at the same recorded instant (#457), and
    records nothing.  A page that announced the date it asked for would then
    state a schedule the record does not hold.
    """

    delta_id = _one_change(session, project)
    _defer(session, client, clock, project, delta_id, until="2026-10-01")
    session.expire_all()

    with nothing_written(session, project.id, apart_from=A_PAGE_VIEW_WRITES):
        response = client.post(
            f"/work/{project.slug}/schedule",
            data={
                "delta_id": str(delta_id),
                "scheduling": "reschedule",
                "returns_on": "2026-11-15",
            },
        )
    assert response.status_code == 200
    assert "already the schedule this change is under" in _text(response.text)
    session.expire_all()
    assert read_project_workflow(
        session, project_id=project.id, as_of=FIRST_VISIT
    ).deferred[0].returns_at.isoformat() == "2026-10-01"


# --- the Needs coordination walk, and what #837 still owes it --------------


def test_needs_coordination_waits_past_its_date_and_is_settled_later(
    session, project, clock, client
):
    """Needs coordination, the visit after its date, then the settlement.

    The half this walk cannot yet take is the reply itself.  A reply from an
    External Organization is Corridor-originated correspondence and a distinct
    fact from the record question being answered: ``outgoing_requests`` holds
    the relation and ``record_outgoing_request_response`` the operation, and
    #837 owns giving a coordinator a way to record one.  Until it does, the
    week can prove that the question is still waiting, that it is past the date
    the plan named, and that answering the change settles it -- and it must not
    pretend a recorded reply settled anything, which is exactly the confusion
    the two facts are kept apart to prevent.
    """

    adopted = Adopted(session, project).build()
    first = adopted.answer(
        document="utility-letter.pdf",
        family="utility-letter",
        revision="2026-09-01",
        value="2027-02-15",
    )[0]
    adopted.answer(
        document="meeting-minutes.pdf",
        family="meeting-minutes",
        revision="2026-09-02",
        value="2027-03-20",
    )
    session.expire_all()

    # Visit one: the two sources disagree, so this is one coordination
    # question, and the coordinator records a plan for every source on it.
    reading = read_review_items(session, project_id=project.id, as_of=FIRST_VISIT)
    item = next(row for row in reading.items if row.focused)
    children = [child.delta_id for child in item.children]
    planned = client.post(
        f"/review/{project.slug}/answers",
        data={
            "item_key": item.item_key,
            "answer_delta": [str(value) for value in children],
            "answer_outcome": ["needs_coordination"] * len(children),
            "answer_source": [""] * len(children),
            "answer_question": ["Which date does the utility hold to?"] * len(children),
            "answer_person": [""] * len(children),
            "answer_organization": ["City Water"] * len(children),
            "answer_return": ["2026-10-01"] * len(children),
        },
    )
    assert planned.status_code == 200, planned.text
    session.expire_all()

    week = read_project_workflow(session, project_id=project.id, as_of=FIRST_VISIT)
    assert len(week.follow_up) == len(children)
    assert not any(need.overdue(today=FIRST_VISIT.date()) for need in week.follow_up)

    # Visit two, past the date the plan named. Still open, still an outside
    # ask, and the week says the date has passed.
    clock.advance_to(THIRD_VISIT)
    later = read_project_workflow(session, project_id=project.id, as_of=THIRD_VISIT)
    assert all(need.overdue(today=THIRD_VISIT.date()) for need in later.follow_up)
    page = _text(client.get(f"/work/{project.slug}").text)
    assert "past the date this plan named" in page
    assert first.id in read_review_items(
        session, project_id=project.id, as_of=THIRD_VISIT
    ).reading.open_delta_ids

    # What #837 owes: nothing here records that anyone replied. The relation
    # exists and no production module writes it, so the week cannot say a
    # reply arrived and does not.
    assert "replied" not in page

    # Visit three: the answer arrives by whatever means, and the coordinator
    # settles the change. Settling the record question is what retires the
    # ask -- not a reply, which is a separate fact.
    settle = read_review_items(session, project_id=project.id, as_of=THIRD_VISIT)
    settle_item = next(row for row in settle.items if row.focused)
    answered = client.post(
        f"/review/{project.slug}/answers",
        data={
            "item_key": settle_item.item_key,
            "answer_delta": [str(child.delta_id) for child in settle_item.children],
            "answer_outcome": ["keep_current"] * len(settle_item.children),
            "answer_source": [""] * len(settle_item.children),
            "answer_question": [""] * len(settle_item.children),
            "answer_person": [""] * len(settle_item.children),
            "answer_organization": [""] * len(settle_item.children),
            "answer_return": [""] * len(settle_item.children),
        },
    )
    assert answered.status_code == 200, answered.text
    session.expire_all()

    assert read_project_workflow(
        session, project_id=project.id, as_of=THIRD_VISIT
    ).follow_up == ()


def _two_sources_planned(session: Session, client, project: Project) -> list[int]:
    """Two disagreeing sources, answered Needs coordination on one item."""

    adopted = Adopted(session, project).build()
    adopted.answer(
        document="utility-letter.pdf",
        family="utility-letter",
        revision="2026-09-01",
        value="2027-02-15",
    )
    adopted.answer(
        document="meeting-minutes.pdf",
        family="meeting-minutes",
        revision="2026-09-02",
        value="2027-03-20",
    )
    session.expire_all()
    reading = read_review_items(session, project_id=project.id, as_of=FIRST_VISIT)
    item = next(row for row in reading.items if row.focused)
    children = [child.delta_id for child in item.children]
    response = client.post(
        f"/review/{project.slug}/answers",
        data={
            "item_key": item.item_key,
            "answer_delta": [str(value) for value in children],
            "answer_outcome": ["needs_coordination"] * len(children),
            "answer_source": [""] * len(children),
            "answer_question": ["Which date does the utility hold to?"] * len(children),
            "answer_person": [""] * len(children),
            "answer_organization": ["City Water"] * len(children),
            "answer_return": ["2026-10-01"] * len(children),
        },
    )
    assert response.status_code == 200, response.text
    session.expire_all()
    return children


def test_no_follow_up_plan_control_claims_to_send_anything(session, project, client):
    """Nothing on the week sends, and the page says which of the two it is.

    #835 asks for a plan that can be updated or cancelled with attribution and
    for nothing to send email.  The second half holds today and is asserted
    here so it cannot quietly stop holding; the first half has no home in the
    record yet and the page still says so rather than offering a control that
    would have nowhere to write.
    """

    _two_sources_planned(session, client, project)

    markup = client.get(f"/work/{project.slug}").text
    page = _text(markup)
    assert "nothing on this page sends, closes, or reassigns one" in page
    # Every form this week can carry, named. A send, a plan update or a plan
    # cancellation would be a fourth, and none exists to render.
    actions = set(re.findall(r'<form[^>]*action="([^"]+)"', markup))
    assert actions <= {
        f"/work/{project.slug}/issue/authorize",
        f"/work/{project.slug}/issue/prepare",
        f"/work/{project.slug}/schedule",
    }, actions
    assert not any(
        word in page.lower()
        for word in ("send this email", "email the utility", "cancel this plan")
    )

    # And the stronger half, from the source rather than from one page: the
    # application has exactly one mail sender and exactly one caller of it,
    # the sign-in link. A follow-up control that mailed anybody would have to
    # add a second, and would fail here.
    application = (
        pathlib.Path(corridor.web.app.__file__).read_text(encoding="utf-8")
    )
    assert application.count("sender.send_sign_in_link") == 1
    assert re.findall(r"sender\.send_\w+", application) == ["sender.send_sign_in_link"]


# --- the boundary and the form ---------------------------------------------


def test_the_scheduling_route_is_admitted_and_reads_only_protected_relations():
    key = ("POST", "/work/{slug}/schedule")

    assert key in web_boundary.PILOT_ROUTES
    assert (
        web_boundary.PILOT_ROUTES[key].relations
        <= web_boundary.PROTECTED_RELATIONS
    )
    assert "delta_deferrals" in web_boundary.PILOT_ROUTES[key].relations
    assert web_boundary.unprotected_route_relations() == ()


def test_the_scheduling_form_carries_the_request_forgery_field(
    session, project, clock, client
):
    """#821's rule, read off the page this route actually renders."""

    delta_id = _one_change(session, project)
    _defer(session, client, clock, project, delta_id, until="2026-10-01")
    session.expire_all()

    markup = client.get(f"/work/{project.slug}").text
    opening = markup.index(f'action="/work/{project.slug}/schedule"')
    closing = markup.index("</form>", opening)
    assert 'name="csrf_token"' in markup[opening:closing]
