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

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import html
import pathlib
import re
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

import corridor.web.app
from corridor import web_boundary
from corridor.delta_resolution import (
    DEFER,
    ChildDecisionRequest,
    resolve_delta,
)
from corridor.follow_up_plan_lifecycle import cancel_follow_up_plan, closure_for
from corridor.native_follow_up_reading import read_adopted_follow_up_plans
from corridor.models import (
    DeltaDeferral,
    DeltaFollowUpPlan,
    DeltaReviewPacketReceipt,
    OutgoingRequest,
    Project,
    ProjectRecordRevision,
)
from corridor.proposed_deltas import record_delta_deferral
from corridor.review_packets import packet_reversal
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
    OPEN_NOW,
    RESCHEDULE,
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
# What correcting a plan adds: the closure, and the successor plan the closure
# names. A revision is deliberately not in this set.
CLOSING_WRITES = {"delta_follow_up_plan_closures", "delta_follow_up_plans"}


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


def _schedule_form(client, project: Project, delta_id: int) -> dict[str, str]:
    """The hidden fields the week's own reschedule form carries for one change.

    Read off the rendered page rather than composed here (#903).  What makes a
    resent submission one act with the first is that the browser sends back
    exactly what this form emitted, so a test that made up its own values
    would be proving something no browser does.
    """

    markup = client.get(f"/work/{project.slug}").text
    for form in re.findall(
        r'<form[^>]*action="[^"]*/schedule">(.*?)</form>', markup, re.S
    ):
        fields = dict(re.findall(r'name="(\w+)"\s+value="([^"]*)"', form))
        if fields.get("delta_id") == str(delta_id):
            return fields
    raise AssertionError(f"the week renders no scheduling form for {delta_id}")


def _schedule(
    client,
    project: Project,
    delta_id: int,
    *,
    form: dict[str, str],
    scheduling: str = RESCHEDULE,
    returns_on: str | None = None,
    reason: str | None = None,
):
    """Submit the week's scheduling form, as the page it was read from left it."""

    data = {
        "delta_id": str(delta_id),
        "scheduling": scheduling,
        "request_identity": form["request_identity"],
        "in_force_receipt": form["in_force_receipt"],
    }
    if returns_on is not None:
        data["returns_on"] = returns_on
    if reason is not None:
        data["scheduling_reason"] = reason
    return client.post(f"/work/{project.slug}/schedule", data=data)


@dataclass(frozen=True, slots=True)
class _CommittedDeferral:
    """One project, one open change and one schedule, all already committed."""

    project_id: int
    delta_id: int
    deferral_id: int


def _committed_deferral(factory) -> _CommittedDeferral:
    """The week two competing submissions both read, in its own transaction."""

    with factory() as setup:
        project = Project(
            slug=f"deferred-{uuid4().hex[:8]}", name="Deferred", is_synthetic=True
        )
        setup.add(project)
        setup.flush()
        seed_membership(setup, project, COORDINATOR)
        delta_id = _one_change(setup, project)
        receipt = record_delta_deferral(
            setup,
            project_id=project.id,
            delta_id=delta_id,
            deferred_at=FIRST_VISIT,
            scheduled_by_principal=COORDINATOR.subject,
            request_identity="schedule:the-defer-both-tabs-read",
            deferred_until=RETURNS_ON,
        )
        committed = _CommittedDeferral(
            project_id=int(project.id),
            delta_id=int(delta_id),
            deferral_id=int(receipt.id),
        )
        setup.commit()
    return committed


def _receipts(session: Session, delta_id: int) -> list[DeltaDeferral]:
    """Every scheduling receipt this change carries, oldest first."""

    return list(
        session.scalars(
            select(DeltaDeferral)
            .where(DeltaDeferral.delta_id == delta_id)
            .order_by(DeltaDeferral.id)
        ).all()
    )


def _revision_count(session: Session, project: Project) -> int:
    return session.scalar(
        select(func.count()).where(ProjectRecordRevision.project_id == project.id)
    )


def _text(markup: str) -> str:
    """The words a reader sees, with the markup's own line wrapping removed."""

    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", markup)))


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
    form = _schedule_form(client, project, delta_id)
    with nothing_written(session, project.id, apart_from=SCHEDULING_WRITES):
        response = _schedule(
            client,
            project,
            delta_id,
            form=form,
            returns_on="2026-11-15",
            reason="the meeting moved",
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
    form = _schedule_form(client, project, delta_id)
    with nothing_written(session, project.id, apart_from=SCHEDULING_WRITES):
        response = _schedule(
            client, project, delta_id, form=form, scheduling=OPEN_NOW
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

    # The week renders no scheduling form for a change it has not deferred,
    # so this submission is composed by hand. The two fields the form would
    # have carried are present and well formed: what is refused is the act,
    # not the shape of the request.
    with nothing_written(session, project.id, apart_from=A_PAGE_VIEW_WRITES):
        response = _schedule(
            client,
            project,
            delta_id,
            form={"request_identity": "schedule:by-hand", "in_force_receipt": "0"},
            returns_on="2026-11-15",
        )
    assert response.status_code == 409
    assert "not deferred under this reading" in _text(response.text)


def test_a_reschedule_with_no_date_is_refused_and_records_nothing(
    session, project, clock, client
):
    delta_id = _one_change(session, project)
    _defer(session, client, clock, project, delta_id, until="2026-10-01")
    session.expire_all()

    form = _schedule_form(client, project, delta_id)
    with nothing_written(session, project.id, apart_from=A_PAGE_VIEW_WRITES):
        response = _schedule(client, project, delta_id, form=form)
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


def test_the_recorded_wake_condition_is_named_when_it_is_what_woke_it(
    session, project, clock, client
):
    """A deferral held on a person's own condition names it when it fires.

    The review screen always records a date, so this one is scheduled through
    the command the screen uses, with the wake condition and no date at all --
    which ADR-0084 allows and the reading has always honoured.
    """

    adopted = Adopted(session, project).build()
    first = adopted.answer(
        document="utility-letter.pdf",
        family="utility-letter",
        revision="2026-09-01",
        value="2027-02-15",
    )[0]
    session.expire_all()
    outcome = resolve_delta(
        session,
        ChildDecisionRequest(
            project_id=project.id,
            delta_id=first.id,
            action=DEFER,
            principal=COORDINATOR,
            idempotency_key=f"defer:{uuid4().hex[:10]}",
            decided_at=FIRST_VISIT,
            wake_condition="the utility sends its revised schedule",
        ),
    )
    assert outcome.status == "deferred", outcome
    session.expire_all()

    held = read_project_workflow(
        session, project_id=project.id, as_of=FIRST_VISIT
    ).deferred[0]
    assert held.returns_at is None
    assert held.return_sentence == (
        "Comes back when the utility sends its revised schedule"
    )

    # The revised schedule arrives as a newer source version, which is
    # ADR-0084's wake condition, and the change says both halves.
    adopted.answer(
        document="meeting-minutes.pdf",
        family="meeting-minutes",
        revision="2026-09-02",
        value="2027-03-20",
    )
    session.expire_all()

    reading = read_review_items(session, project_id=project.id, as_of=FIRST_VISIT)
    sentence = next(
        row.return_sentence
        for item in reading.items
        for row in item.children
        if row.delta_id == first.id
    )
    assert sentence == (
        "A newer source version arrived for this subject and field, which is "
        "what it was waiting for: the utility sends its revised schedule"
    )


def test_a_change_brought_back_early_says_who_brought_it_back(
    session, project, clock, client
):
    delta_id = _one_change(session, project)
    _defer(session, client, clock, project, delta_id, until="2026-10-01")
    session.expire_all()

    clock.advance_to(SECOND_VISIT)
    response = _schedule(
        client,
        project,
        delta_id,
        form=_schedule_form(client, project, delta_id),
        scheduling=OPEN_NOW,
        reason="the utility called",
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
    moved = _schedule(
        client,
        project,
        delta_id,
        form=_schedule_form(client, project, delta_id),
        returns_on="2026-12-01",
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


# --- what a replayed or overtaken scheduling submission is (#903) -----------
#
# A resent form, a contradicted one, a deliberate second reschedule inside one
# instant, and a submission composed against a schedule that has since moved.
# All four turn on the same choice: what the caller says its *request* is, not
# when the request arrived, is what makes two calls one act.  The instant is
# held still in three of them precisely so it cannot be doing the work.


def test_an_exactly_resent_schedule_returns_the_act_already_recorded(
    session, project, clock, client
):
    """The browser sends the same form twice; the record holds one schedule.

    The second submission carries the identity the page minted for the first,
    asks for the same date, and is answered with the receipt already written
    -- so the page announcing that date is telling the truth, and nothing is
    recorded a second time.  It is the same answer as the first submission
    because it is the same act.
    """

    delta_id = _one_change(session, project)
    _defer(session, client, clock, project, delta_id, until="2026-10-01")
    session.expire_all()

    clock.advance_to(SECOND_VISIT)
    form = _schedule_form(client, project, delta_id)
    first = _schedule(client, project, delta_id, form=form, returns_on="2026-11-15")
    assert first.status_code == 201, first.text
    session.expire_all()
    recorded = _receipts(session, delta_id)

    with nothing_written(session, project.id, apart_from=A_PAGE_VIEW_WRITES):
        resent = _schedule(
            client, project, delta_id, form=form, returns_on="2026-11-15"
        )

    assert resent.status_code == 201, resent.text
    assert "comes back on 2026-11-15" in _text(resent.text)
    session.expire_all()
    assert [receipt.id for receipt in _receipts(session, delta_id)] == [
        receipt.id for receipt in recorded
    ]


def test_a_resent_schedule_asking_for_another_date_is_refused(
    session, project, clock, client
):
    """The case #903 was filed for, answered by refusing rather than lying.

    Same request identity, different return date.  Before, the command
    returned the receipt already written as a success and the requested date
    was dropped on the floor; the route papered over it by comparing receipt
    ids.  Now the contradiction is refused in the command, nothing is
    recorded, and the schedule in force is still the one the first submission
    asked for.
    """

    delta_id = _one_change(session, project)
    _defer(session, client, clock, project, delta_id, until="2026-10-01")
    session.expire_all()

    clock.advance_to(SECOND_VISIT)
    form = _schedule_form(client, project, delta_id)
    first = _schedule(client, project, delta_id, form=form, returns_on="2026-11-15")
    assert first.status_code == 201, first.text
    session.expire_all()

    with nothing_written(session, project.id, apart_from=A_PAGE_VIEW_WRITES):
        contradicted = _schedule(
            client, project, delta_id, form=form, returns_on="2026-12-20"
        )

    assert contradicted.status_code == 409
    words = _text(contradicted.text)
    assert "already recorded asking for something else" in words
    # The machine token the two halves of the rule agree on is not a sentence,
    # and a coordinator never reads one.
    assert "resolve_delta:" not in words
    session.expire_all()
    assert read_project_workflow(
        session, project_id=project.id, as_of=SECOND_VISIT
    ).deferred[0].returns_at.isoformat() == "2026-11-15"
    assert len(_receipts(session, delta_id)) == 2


def test_a_second_reschedule_at_the_very_same_instant_is_a_second_act(
    session, project, clock, client
):
    """Two deliberate reschedules, one declared instant, two receipts.

    The clock does not move between them, which is what #457's identity could
    not survive: it made the instant the thing that told two acts apart, so
    the second was silently answered with the first's receipt.  Each act now
    carries the identity its own page minted, and names the receipt it means
    to replace, so both are recorded and the later one is in force.
    """

    delta_id = _one_change(session, project)
    _defer(session, client, clock, project, delta_id, until="2026-10-01")
    session.expire_all()

    clock.advance_to(SECOND_VISIT)
    first = _schedule(
        client,
        project,
        delta_id,
        form=_schedule_form(client, project, delta_id),
        returns_on="2026-11-15",
    )
    assert first.status_code == 201, first.text
    session.expire_all()

    # The same instant, and the coordinator changes their mind on the page the
    # first act rendered.
    again = _schedule(
        client,
        project,
        delta_id,
        form=_schedule_form(client, project, delta_id),
        returns_on="2026-12-20",
    )
    assert again.status_code == 201, again.text
    session.expire_all()

    receipts = _receipts(session, delta_id)
    assert len(receipts) == 3, "the first Defer and both reschedules"
    assert [receipt.deferred_at for receipt in receipts[1:]] == [
        SECOND_VISIT,
        SECOND_VISIT,
    ]
    assert receipts[-1].supersedes_deferral_id == receipts[-2].id
    assert read_project_workflow(
        session, project_id=project.id, as_of=SECOND_VISIT
    ).deferred[0].returns_at.isoformat() == "2026-12-20"


@pytest.mark.slow
def test_a_submission_composed_against_a_moved_schedule_is_refused(
    runtime_database,
):
    """Two submissions read the same week; only the first one changes it.

    This cannot be proved inside one rollback-scoped transaction, because what
    the second submission must see is a receipt another transaction *committed*
    after its own page was rendered.  So the harness's own isolated database
    runs three real transactions: the two submissions carry their own fresh
    request identities and both name the receipt their shared reading showed,
    and the second is refused because that receipt is no longer the schedule in
    force.  Nothing about it is a retry -- it is a distinct act composed
    against a record that moved under it.
    """

    factory = runtime_database.session_factory
    scenario = _committed_deferral(factory)

    with factory() as first:
        recorded = record_delta_deferral(
            first,
            project_id=scenario.project_id,
            delta_id=scenario.delta_id,
            deferred_at=SECOND_VISIT,
            scheduled_by_principal=COORDINATOR.subject,
            request_identity="schedule:first-tab",
            deferred_until=datetime(2026, 11, 15, tzinfo=timezone.utc),
            supersedes_deferral_id=scenario.deferral_id,
        )
        moved_to = int(recorded.id)
        first.commit()

    with factory() as second:
        with pytest.raises(DBAPIError) as refused:
            record_delta_deferral(
                second,
                project_id=scenario.project_id,
                delta_id=scenario.delta_id,
                deferred_at=SECOND_VISIT,
                scheduled_by_principal=COORDINATOR.subject,
                request_identity="schedule:second-tab",
                deferred_until=datetime(2026, 12, 20, tzinfo=timezone.utc),
                supersedes_deferral_id=scenario.deferral_id,
            )
        second.rollback()

    message = str(refused.value)
    assert "resolve_delta:stale_schedule" in message
    assert "is not the one in force" in message

    with factory() as reading:
        receipts = reading.scalars(
            select(DeltaDeferral)
            .where(DeltaDeferral.delta_id == scenario.delta_id)
            .order_by(DeltaDeferral.id)
        ).all()
        assert [receipt.id for receipt in receipts] == [
            scenario.deferral_id,
            moved_to,
        ]
        assert receipts[-1].deferred_until == datetime(
            2026, 11, 15, tzinfo=timezone.utc
        )


# --- the Needs coordination walk, and what #837 still owes it --------------


def test_needs_coordination_waits_past_its_date_and_is_settled_later(
    session, project, clock, client
):
    """Needs coordination, the reply, the visit after its date, the settlement.

    The half this walk could not take until #837 landed is the reply itself.  A
    reply from an External Organization is Corridor-originated correspondence
    and a *distinct fact* from the record question being answered, and the
    whole reason the two are kept apart is the confusion this walk now proves
    cannot happen: City Water replies, the coordinator records it against the
    request that asked, and the question is **still open**.  The plan is still
    outstanding, the Proposed Delta is still open, the week still says the date
    the plan named has passed, and nothing about the accepted record has moved.

    Only the last step retires the ask, and it is a decision on the review
    screen and not a reply: settling the change is what closes the question.
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

    # #837's half, which this walk was written waiting for. The coordinator
    # sends from their own mail client and records what went out against the
    # plans it advanced; Corridor sends nothing.
    plan_ids = [need.plan_id for need in later.follow_up]
    sent = client.post(
        f"/work/{project.slug}/follow-up/sent",
        data={
            "follow_up_plan_id": [str(plan_id) for plan_id in plan_ids],
            "covered_subject_key": [
                need.subject_name for need in later.follow_up
            ],
            "external_organization": "City Water",
            "question": "Which date does the utility hold to?",
            "sent_content": "Which date do you hold to for U-042?",
            "sent_on": "2026-10-02",
            "sent_by": "local:coordinator",
            "expected_response_by": "2026-10-09",
        },
    )
    assert sent.status_code == 201, sent.text
    session.expire_all()

    # City Water replies, and the coordinator records it with the evidence.
    request_id = session.scalar(
        select(OutgoingRequest.id).where(OutgoingRequest.project_id == project.id)
    )
    replied = client.post(
        f"/work/{project.slug}/follow-up/response",
        data={
            "request_id": str(request_id),
            "received_on": "2026-10-12",
            "completeness": "substantive",
            "evidence_kind": "manual_observation",
            "observation": "City Water said they hold to 15 February.",
            "observed_by": "local:coordinator",
            "source_reference": "telephone call, 11:20",
        },
    )
    assert replied.status_code == 201, replied.text
    session.expire_all()

    # And this is the distinction the two facts are kept apart for: they
    # replied, and the question is still open. The plan is still outstanding,
    # the change is still undecided, and the week still says the date the plan
    # named has passed.
    after_reply = read_project_workflow(
        session, project_id=project.id, as_of=THIRD_VISIT
    )
    assert len(after_reply.follow_up) == len(children)
    assert all(
        need.overdue(today=THIRD_VISIT.date()) for need in after_reply.follow_up
    )
    assert first.id in read_review_items(
        session, project_id=project.id, as_of=THIRD_VISIT
    ).reading.open_delta_ids
    assert "past the date this plan named" in _text(
        client.get(f"/work/{project.slug}").text
    )

    # Visit three: the coordinator settles the change. Settling the record
    # question is what retires the ask -- not the reply, which is a separate
    # fact and has just been shown to settle nothing.
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
    """A plan can be corrected and cancelled here, and nothing sends.

    #835 asks for both halves at once, and they pull in opposite directions: a
    screen that can retire an outside ask is exactly the screen somebody would
    next expect to send the message.  It does not, it says so, and the page's
    own words tell the coordinator where the message still comes from.

    #837 sharpened the point rather than softening it.  The week can now record
    what was sent and what came back, so the screen is one step closer to
    looking like a mail client than it was -- and the count below is what keeps
    that from becoming one.  Recording a send is not sending: the application
    still has exactly one mail sender and exactly one caller of it.
    """

    _two_sources_planned(session, client, project)

    markup = client.get(f"/work/{project.slug}").text
    page = _text(markup)
    assert "nothing on this page sends anything" in page
    assert "a message still goes out from wherever you send mail" in page
    # Every form this week can carry, named. A send would be another one.
    actions = set(re.findall(r'<form[^>]*action="([^"]+)"', markup))
    assert actions <= {
        # #843's navigation shell, on every customer page.
        "/sign-out",
        f"/work/{project.slug}/issue/authorize",
        f"/work/{project.slug}/issue/prepare",
        f"/work/{project.slug}/schedule",
        f"/work/{project.slug}/follow-up/close",
        # #837's two recordings of correspondence that already happened.
        f"/work/{project.slug}/follow-up/sent",
        f"/work/{project.slug}/follow-up/response",
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


# --- the plan lifecycle: correcting one, and cancelling one ---------------


def _one_plan(session: Session, client, project: Project) -> int:
    """One live Follow-up Plan, recorded the way the review screen records it."""

    plans = _two_sources_planned(session, client, project)
    week = read_project_workflow(session, project_id=project.id, as_of=FIRST_VISIT)
    assert len(week.follow_up) == len(plans)
    return week.follow_up[0].plan_id


def test_correcting_a_plan_leaves_one_live_ask_and_not_two(
    session, project, client
):
    """The finding this relation exists for, asserted as a count.

    Appending a corrected plan was always possible: ``delta_follow_up_plans``
    takes more than one row per Proposed Delta and every reader of them returns
    them all. What was missing is the statement that one replaced the other,
    without which the week lists both and the chase list contacts somebody
    twice.
    """

    plan_id = _one_plan(session, client, project)
    before = read_project_workflow(session, project_id=project.id, as_of=FIRST_VISIT)
    asks = len(before.follow_up)

    response = client.post(
        f"/work/{project.slug}/follow-up/close",
        data={
            "plan_id": str(plan_id),
            "plan_action": "update",
            "plan_question": "Which date does City Water hold to for U-042?",
            "plan_organization": "City Water",
            "plan_return": "2026-11-30",
        },
    )
    assert response.status_code == 201, response.text
    session.expire_all()

    after = read_project_workflow(session, project_id=project.id, as_of=FIRST_VISIT)
    assert len(after.follow_up) == asks, "the correction replaced the ask, it did not add one"
    assert plan_id not in {need.plan_id for need in after.follow_up}
    corrected = next(
        need
        for need in after.follow_up
        if need.plan_id not in {row.plan_id for row in before.follow_up}
    )
    assert corrected.open_question == "Which date does City Water hold to for U-042?"
    assert corrected.return_date.isoformat() == "2026-11-30"

    closure = closure_for(session, project_id=project.id, plan_id=plan_id)
    assert closure is not None
    assert closure.closure_kind == "superseded"
    assert closure.successor_plan_id == corrected.plan_id
    assert closure.closed_by_principal == COORDINATOR.subject
    assert closure.closed_at == FIRST_VISIT
    assert closure.cancellation_reason is None


def test_cancelling_a_plan_retires_the_ask_and_leaves_the_change_open(
    session, project, client
):
    """Nobody outside owes an answer; the proposed change is still undecided."""

    plan_id = _one_plan(session, client, project)
    before = read_project_workflow(session, project_id=project.id, as_of=FIRST_VISIT)
    delta_id = next(
        need.delta_id for need in before.follow_up if need.plan_id == plan_id
    )

    response = client.post(
        f"/work/{project.slug}/follow-up/close",
        data={
            "plan_id": str(plan_id),
            "plan_action": "cancel",
            "plan_cancellation_reason": "answered_another_way",
            "plan_note": "the utility called and confirmed it",
        },
    )
    assert response.status_code == 201, response.text
    session.expire_all()

    after = read_project_workflow(session, project_id=project.id, as_of=FIRST_VISIT)
    assert plan_id not in {need.plan_id for need in after.follow_up}
    assert len(after.follow_up) == len(before.follow_up) - 1

    closure = closure_for(session, project_id=project.id, plan_id=plan_id)
    assert closure is not None
    assert closure.closure_kind == "cancelled"
    assert closure.cancellation_reason == "answered_another_way"
    assert closure.successor_plan_id is None
    assert closure.note == "the utility called and confirmed it"
    assert closure.closed_by_principal == COORDINATOR.subject

    # The Proposed Delta is untouched: still open, still offered.
    reading = read_review_items(session, project_id=project.id, as_of=FIRST_VISIT)
    assert delta_id in reading.reading.open_delta_ids
    assert delta_id in reading.reading.actionable_delta_ids


def test_neither_closing_act_writes_a_project_record_revision(
    session, project, client
):
    """Counted the way the scheduling acts are: over the whole project graph."""

    plan_id = _one_plan(session, client, project)
    revisions = _revision_count(session, project)

    with nothing_written(
        session, project.id, apart_from=A_PAGE_VIEW_WRITES | CLOSING_WRITES
    ):
        correction = client.post(
            f"/work/{project.slug}/follow-up/close",
            data={
                "plan_id": str(plan_id),
                "plan_action": "update",
                "plan_question": "Which date does City Water hold to?",
                "plan_organization": "City Water",
            },
        )
    assert correction.status_code == 201, correction.text
    session.expire_all()
    assert _revision_count(session, project) == revisions

    week = read_project_workflow(session, project_id=project.id, as_of=FIRST_VISIT)
    with nothing_written(
        session, project.id, apart_from=A_PAGE_VIEW_WRITES | {"delta_follow_up_plan_closures"}
    ):
        cancellation = client.post(
            f"/work/{project.slug}/follow-up/close",
            data={
                "plan_id": str(week.follow_up[0].plan_id),
                "plan_action": "cancel",
                "plan_cancellation_reason": "no_longer_needed",
            },
        )
    assert cancellation.status_code == 201, cancellation.text
    session.expire_all()
    assert _revision_count(session, project) == revisions


def test_a_plan_closes_once_and_a_second_attempt_is_refused(
    session, project, clock, client
):
    """A second closure is two statements about one plan, not a correction."""

    plan_id = _one_plan(session, client, project)
    first = client.post(
        f"/work/{project.slug}/follow-up/close",
        data={
            "plan_id": str(plan_id),
            "plan_action": "cancel",
            "plan_cancellation_reason": "raised_in_error",
        },
    )
    assert first.status_code == 201, first.text
    session.expire_all()

    clock.advance_to(SECOND_VISIT)
    with nothing_written(session, project.id, apart_from=A_PAGE_VIEW_WRITES):
        again = client.post(
            f"/work/{project.slug}/follow-up/close",
            data={
                "plan_id": str(plan_id),
                "plan_action": "cancel",
                "plan_cancellation_reason": "no_longer_needed",
            },
        )
    assert again.status_code == 409
    assert "not one this project is still waiting on" in _text(again.text)


def test_postgresql_refuses_a_closure_by_a_person_who_may_not_coordinate(
    session, project, client
):
    """#839's proof, on the relation, for exactly the reason #839 gives.

    The route performs no designation check of its own, so this is the rule
    holding where a second caller could not forget it: the trigger re-reads the
    active roster entry as the schema's own owner and refuses.
    """

    plan_id = _one_plan(session, client, project)
    reader = HumanPrincipal("local:reader")
    seed_membership(session, project, reader, designations=())

    outcome = cancel_follow_up_plan(
        session,
        project_id=project.id,
        plan_id=plan_id,
        principal=reader,
        cancellation_reason="no_longer_needed",
        closed_at=FIRST_VISIT,
        idempotency_key=f"close:{uuid4().hex[:10]}",
    )
    assert not outcome.closed
    assert "project-coordination designation" in outcome.refusal.detail
    session.expire_all()
    assert closure_for(session, project_id=project.id, plan_id=plan_id) is None
    assert plan_id in {
        need.plan_id
        for need in read_project_workflow(
            session, project_id=project.id, as_of=FIRST_VISIT
        ).follow_up
    }


def test_a_cancellation_with_no_structured_reason_is_refused(
    session, project, client
):
    """ADR-0038's rule on the spine's relation: prose is not a reason."""

    plan_id = _one_plan(session, client, project)

    with nothing_written(session, project.id, apart_from=A_PAGE_VIEW_WRITES):
        response = client.post(
            f"/work/{project.slug}/follow-up/close",
            data={
                "plan_id": str(plan_id),
                "plan_action": "cancel",
                "plan_note": "we decided we do not need it",
            },
        )
    assert response.status_code == 400
    assert "Choose why this is no longer an outside ask" in _text(response.text)


def test_a_correction_with_no_question_or_no_party_is_refused(
    session, project, client
):
    plan_id = _one_plan(session, client, project)

    with nothing_written(session, project.id, apart_from=A_PAGE_VIEW_WRITES):
        no_question = client.post(
            f"/work/{project.slug}/follow-up/close",
            data={
                "plan_id": str(plan_id),
                "plan_action": "update",
                "plan_question": "   ",
                "plan_organization": "City Water",
            },
        )
    assert no_question.status_code == 400
    assert "still records the exact question" in _text(no_question.text)

    with nothing_written(session, project.id, apart_from=A_PAGE_VIEW_WRITES):
        no_party = client.post(
            f"/work/{project.slug}/follow-up/close",
            data={
                "plan_id": str(plan_id),
                "plan_action": "update",
                "plan_question": "Which date?",
                "plan_organization": "",
            },
        )
    assert no_party.status_code == 400
    assert "still names who owes the answer" in _text(no_party.text)


def test_both_readers_drop_a_closed_plan_and_cannot_disagree(
    session, project, client
):
    """The week and the publication reader answer from the one rule.

    Two renderings of one plan came apart once before, over the reversal join
    written twice; the closure rule is written once in
    ``native_follow_up_reading`` and both readers call it.
    """

    plan_id = _one_plan(session, client, project)
    revision_id = read_project_workflow(
        session, project_id=project.id, as_of=FIRST_VISIT
    ).accepted_revision_id
    before = read_adopted_follow_up_plans(
        session, project.id, revision_id, current=True
    )
    assert plan_id in {plan.plan_id for plan in before}

    response = client.post(
        f"/work/{project.slug}/follow-up/close",
        data={
            "plan_id": str(plan_id),
            "plan_action": "cancel",
            "plan_cancellation_reason": "no_longer_needed",
        },
    )
    assert response.status_code == 201, response.text
    session.expire_all()

    after = read_adopted_follow_up_plans(
        session, project.id, revision_id, current=True
    )
    assert plan_id not in {plan.plan_id for plan in after}
    assert plan_id not in {
        need.plan_id
        for need in read_project_workflow(
            session, project_id=project.id, as_of=FIRST_VISIT
        ).follow_up
    }


def test_the_closure_is_not_an_undo_and_leaves_the_packet_act_standing(
    session, project, client
):
    """The boundary #834 owns, asserted rather than described.

    Undo says the recorded act never stood. A closure says the ask was real and
    is finished, so the packet receipt, its children and the revision it wrote
    are all exactly where they were.
    """

    plan_id = _one_plan(session, client, project)
    receipt = session.scalars(
        select(DeltaReviewPacketReceipt).where(
            DeltaReviewPacketReceipt.project_id == project.id
        )
    ).one()
    revision_id = receipt.revision_id

    response = client.post(
        f"/work/{project.slug}/follow-up/close",
        data={
            "plan_id": str(plan_id),
            "plan_action": "cancel",
            "plan_cancellation_reason": "no_longer_needed",
        },
    )
    assert response.status_code == 201, response.text
    session.expire_all()

    still = session.scalars(
        select(DeltaReviewPacketReceipt).where(
            DeltaReviewPacketReceipt.project_id == project.id
        )
    ).one()
    assert still.id == receipt.id
    assert still.revision_id == revision_id
    assert packet_reversal(session, receipt.id) is None
    # And the plan itself: never edited, never removed.
    plan = session.get(DeltaFollowUpPlan, plan_id)
    assert plan is not None
    assert plan.recorded_by_principal == COORDINATOR.subject


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
