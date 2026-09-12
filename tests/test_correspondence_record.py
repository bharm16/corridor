"""What was sent, what came back, and what neither of them settles (#837).

#652 built the retention and shipped it with no caller: a coordinator had no
way to say "we wrote to City Water on the 1st" and no way to say "they replied
on the 20th, here is the evidence", so the no-response band ADR-0090 allows was
implemented and unreachable.  This file is the surface that closes that, and
the properties under test are the ones that make correspondence dangerous
rather than the ones that are easy to assert.

**One request, the plans it advances.**  A follow-up bundle is one interaction
covering several questions, so one message advances one *or more* Follow-up
Plans.  A request that covered some of a bundle's plans and not others has to
say which, because the alternative is a page implying that one email answered
questions it never mentioned.

**A reply is not an answer to the record question.**  Recording that somebody
replied stops the literal no-response condition and changes nothing else: the
Follow-up Plan stays open, the Proposed Delta stays open, and the accepted
record is untouched.  #835's walk proves the other half -- that *settling* the
change is what retires the ask -- and the two together are the distinction the
accepted #652 contract exists to keep.

**Nothing here sends anything.**  A person sends from their own mail client and
records it, which is why the form asks for the message rather than composing
one, and why the application still has exactly one mail sender with exactly one
caller.

**Correction is an append.**  A mistaken request or reply is corrected by
recording a corrected one that names the original and says why; the original
stays on the page, marked as corrected.

Nothing here reads a clock.  Every instant and date is declared by the test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from html import unescape
import pathlib
import re
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

import corridor.web.app
from corridor.follow_up_bundles import (
    UNANSWERED_REQUEST_BAND,
    RetainedOutgoingRequest,
    SourceReference,
    read_follow_up_bundles,
    read_retained_outgoing_requests,
)
from corridor.follow_up_plan_lifecycle import (
    cancel_follow_up_plan,
    update_follow_up_plan,
)
from corridor.models import (
    DeltaDisposition,
    DeltaFollowUpPlan,
    Document,
    Project,
    ProjectRecordRevision,
    SourceSegment,
)
from corridor.operating_mode import adopt_project_baseline
from corridor.outgoing_requests import (
    OutgoingRequestRefused,
    read_correspondence,
    read_sent_content,
    record_outgoing_request_response,
    retain_outgoing_request,
)
from corridor.packet_review import (
    FocusedAnswer,
    focused_request,
    read_review_items,
)
from corridor.principals import HumanPrincipal
from corridor.record_history import read_record_history
from corridor.review_packets import NEEDS_COORDINATION, resolve_review_packet
from corridor import web_boundary
from corridor.web.app import (
    app,
    get_human_principal,
    get_review_clock,
    get_session,
)
from corridor.web.follow_up_view import chase_view

import journey_matrix
from access_support import seed_membership
from browser_session_support import page_without_shell
from journey_harness import Step, run_scenario
from packet_review_support import (
    Rendition,
    accept_baseline_fact,
    append_deltas,
    modify,
    register_baseline,
    register_source_row,
    subject,
    support,
)


COORDINATOR = HumanPrincipal("local:coordinator")

# Every instant below is declared, never read from a clock.
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
RETURNS_AT = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)

SENT_ON = date(2026, 9, 1)
EXPECTED_BY = date(2026, 9, 20)
REPLIED_ON = date(2026, 9, 25)

WATER = "City Water"
SOON = "2026-09-21"
FIRST = 42

MESSAGE = (
    "Dear City Water,\n\nOur record holds 21 September for U-042. Your "
    "September workbook says 15 December. Which date do you hold to?\n"
)


@pytest.fixture
def project(member_project) -> Project:
    return member_project(COORDINATOR)


@pytest.fixture
def client(session):
    """The app shares the test's transaction and the test's declared instant."""

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: COORDINATOR
    app.dependency_overrides[get_review_clock] = lambda: (lambda: NOW)
    with TestClient(app) as made:
        yield made
    app.dependency_overrides.clear()


# --- one adopted project with two disagreeing sources on one question -------


def _planned(session: Session, project: Project) -> tuple[int, ...]:
    """Two sources disagreeing on one accepted date, both answered planned.

    One focused item, two children, so one bundle carries two Follow-up Plans.
    That is the smallest shape in which "this message covered some of them"
    is a different statement from "this message covered the follow-up".
    """

    source = Rendition(session, project, "ucm-2026-08.xlsx")
    org, _ = source.capture(
        fact_type="external_org", value=WATER, subject_key=subject(FIRST)
    )
    accept_baseline_fact(session, project, org)
    promised, _ = source.capture(
        fact_type="committed_date", value=SOON, subject_key=subject(FIRST)
    )
    revision = accept_baseline_fact(session, project, promised)
    baseline = register_baseline(session, project, source.document, revision)
    register_source_row(
        session,
        project,
        baseline,
        row_number=FIRST,
        business_identity=f"U-{FIRST:03d}",
    )
    for name, family, source_revision, value in (
        ("ucm-2026-09.xlsx", "ucm-workbook", "2026-09", "2026-12-15"),
        ("minutes-2026-09-02.pdf", "meeting-minutes", "2026-09-02", "2027-01-20"),
    ):
        rendition = Rendition(session, project, name)
        fact, segment = rendition.capture(
            fact_type="committed_date", value=value, subject_key=subject(FIRST)
        )
        support(session, project, fact, segment)
        append_deltas(
            session,
            project,
            rendition,
            source_revision=source_revision,
            source_family=family,
            values=[
                modify(
                    subject_key=subject(FIRST),
                    field_name="committed_date",
                    accepted_value=SOON,
                    proposed_value=value,
                    baseline_revision=revision,
                )
            ],
            is_complete_enumerative_source=False,
            row_accounting_sealed=False,
        )
    adopt_project_baseline(
        session,
        project_id=project.id,
        adopted_by_principal="local:adopter",
        baseline_source_sha256=source.document.sha256,
        importer_identity="correspondence_record_fixture",
        importer_version="v1",
        idempotency_key=f"adopt:{uuid4().hex[:10]}",
    )
    session.expire_all()

    reading = read_review_items(session, project_id=project.id, as_of=NOW)
    item = next(row for row in reading.items if row.focused)
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
                    responsible_organization=WATER,
                    return_date=RETURNS_AT,
                )
                for child in item.children
            ],
        ),
    )
    assert result.status == "saved", result
    session.expire_all()
    return tuple(
        session.scalars(
            select(DeltaFollowUpPlan.id)
            .where(DeltaFollowUpPlan.project_id == project.id)
            .order_by(DeltaFollowUpPlan.id)
        )
    )


def _retain(session, project, *, plans, **kwargs):
    settings = {
        "external_organization": WATER,
        "question": "Which date do you hold to?",
        "covered_subject_keys": (subject(FIRST),),
        "sent_content": MESSAGE.encode("utf-8"),
        "sent_on": SENT_ON,
        "sent_by_principal": "local:coordinator",
        "recorded_by_principal": "local:coordinator",
        "expected_response_by": EXPECTED_BY,
        "idempotency_key": f"sent:{uuid4().hex[:10]}",
    }
    settings.update(kwargs)
    return retain_outgoing_request(
        session, project_id=project.id, follow_up_plan_ids=plans, **settings
    )


def _bundle_with_plans(session, project, *, as_of=NOW, correspondence=()):
    reading = read_follow_up_bundles(
        session, project_id=project.id, as_of=as_of
    )
    view = chase_view(reading, correspondence)
    return next(
        one for one in view.bundles if one.correspondence.plan_ids
    )


# --- recording a send makes the band reachable ------------------------------


def test_a_recorded_send_populates_the_band_once_the_boundary_passes(
    session, project
):
    """Before the date nobody is owed an answer; after it, the fact exists.

    Both halves matter. ADR-0090 retired STALE because silence before a
    boundary is not evidence of anything, and the band exists because silence
    after a declared one is.
    """

    plans = _planned(session, project)
    _retain(session, project, plans=plans)

    # NOW is before the boundary this request declared.
    early = read_follow_up_bundles(session, project_id=project.id, as_of=NOW)
    assert early.retained_outgoing_requests == 1
    assert all(
        bundle.band != UNANSWERED_REQUEST_BAND for bundle in early.bundles
    )

    late = read_follow_up_bundles(session, project_id=project.id, as_of=LATER)
    unanswered = next(
        bundle
        for bundle in late.bundles
        if bundle.band == UNANSWERED_REQUEST_BAND
    )
    assert unanswered.quantity == (LATER.date() - EXPECTED_BY).days


def test_one_request_advances_several_plans_and_says_which(session, project):
    """The relation, and the sentence a bundle prints when it covered some."""

    plans = _planned(session, project)
    assert len(plans) == 2
    _retain(session, project, plans=plans[:1])

    bundle = _bundle_with_plans(
        session,
        project,
        correspondence=read_correspondence(
            session, project_id=project.id, as_of=NOW
        ),
    )
    (recorded,) = bundle.correspondence.requests

    assert recorded.covered == plans[:1]
    assert recorded.uncovered == plans[1:]
    assert recorded.coverage_sentence == (
        "Covers 1 of the 2 Follow-up Plans in this follow-up. Not covered by "
        f"this request: {plans[1]}."
    )

    # And when it covered the whole follow-up, it says so rather than listing.
    _retain(session, project, plans=plans)
    whole = _bundle_with_plans(
        session,
        project,
        correspondence=read_correspondence(
            session, project_id=project.id, as_of=NOW
        ),
    )
    assert any(
        view.coverage_sentence
        == "Covers 2 of the 2 Follow-up Plans in this follow-up: all of them."
        for view in whole.correspondence.requests
    )


def test_the_exact_sent_content_comes_back_not_its_digest(session, project):
    """A digest proves two things are the same and shows nobody anything."""

    plans = _planned(session, project)
    row = _retain(session, project, plans=plans)

    assert read_sent_content(row).decode("utf-8") == MESSAGE
    # Retained through the storage interface under the digest's own key, so
    # the two cannot disagree without the read failing.
    assert row.sent_content_key.endswith(f"{row.content_sha256}.txt")


def test_the_sender_and_the_recorder_are_kept_apart(session, project):
    """Who sent it and who says it was sent are two claims, not one."""

    plans = _planned(session, project)
    delegated = _retain(
        session,
        project,
        plans=plans,
        sent_by_principal="rosa.mehta@example.test",
        recorded_by_principal="local:coordinator",
    )
    assert not delegated.recorded_by_the_sender

    firsthand = _retain(
        session,
        project,
        plans=plans,
        sent_by_principal="local:coordinator",
        recorded_by_principal="local:coordinator",
    )
    assert firsthand.recorded_by_the_sender

    bundle = _bundle_with_plans(
        session,
        project,
        correspondence=read_correspondence(
            session, project_id=project.id, as_of=NOW
        ),
    )
    sentences = {view.sender_sentence for view in bundle.correspondence.requests}
    assert (
        "Sent by rosa.mehta@example.test; recorded here by local:coordinator."
        in sentences
    )
    assert "Sent by local:coordinator, who recorded it." in sentences


def test_a_request_with_no_plan_is_refused(session, project):
    """A request that advances nothing is not correspondence about anything."""

    _planned(session, project)
    with pytest.raises(OutgoingRequestRefused):
        _retain(session, project, plans=())


# --- a reply stops the clock and settles nothing ----------------------------


def _reply(session, project, *, request_id, **kwargs):
    settings = {
        "received_on": REPLIED_ON,
        "recorded_by_principal": "local:coordinator",
        "completeness": "substantive",
        "source_reference": "letter of 25 September, page 2",
        "observation": "City Water confirmed 15 December on the telephone.",
        "observed_by_principal": "local:coordinator",
        "idempotency_key": f"reply:{uuid4().hex[:10]}",
    }
    settings.update(kwargs)
    return record_outgoing_request_response(
        session, project_id=project.id, request_id=request_id, **settings
    )


def test_a_reply_stops_the_clock_and_changes_nothing_else(session, project):
    """The one distinction the accepted #652 contract exists to keep.

    Three things are asserted after the reply, and the second and third are the
    point: the no-response finding is gone, the Follow-up Plans are still
    outstanding, and the accepted record is at the revision it was already at.
    """

    plans = _planned(session, project)
    row = _retain(session, project, plans=plans)
    before_revisions = session.scalars(
        select(ProjectRecordRevision.id).where(
            ProjectRecordRevision.project_id == project.id
        )
    ).all()
    before_dispositions = session.scalars(
        select(DeltaDisposition.id).where(
            DeltaDisposition.project_id == project.id
        )
    ).all()

    # Past the boundary, and unanswered: the finding exists.
    assert len(
        read_retained_outgoing_requests(
            session, project_id=project.id, as_of=LATER
        )
    ) == 1

    _reply(session, project, request_id=row.id)
    session.expire_all()

    # The clock stops.
    assert (
        read_retained_outgoing_requests(
            session, project_id=project.id, as_of=LATER
        )
        == ()
    )
    # The plans do not.
    assert tuple(
        session.scalars(
            select(DeltaFollowUpPlan.id)
            .where(DeltaFollowUpPlan.project_id == project.id)
            .order_by(DeltaFollowUpPlan.id)
        )
    ) == plans
    # And nothing about the accepted record moved: no new revision, and no
    # delta disposed of.
    assert session.scalars(
        select(ProjectRecordRevision.id).where(
            ProjectRecordRevision.project_id == project.id
        )
    ).all() == before_revisions
    assert session.scalars(
        select(DeltaDisposition.id).where(
            DeltaDisposition.project_id == project.id
        )
    ).all() == before_dispositions


def test_an_acknowledgement_and_the_substance_are_two_observations(
    session, project
):
    """Monday's acknowledgement and Friday's answer are two things that happened."""

    plans = _planned(session, project)
    row = _retain(session, project, plans=plans)
    _reply(
        session,
        project,
        request_id=row.id,
        received_on=date(2026, 9, 22),
        completeness="acknowledgement",
        source_reference="automatic acknowledgement, 09:04",
    )
    _reply(
        session,
        project,
        request_id=row.id,
        received_on=REPLIED_ON,
        completeness="substantive",
    )

    (recorded,) = read_correspondence(
        session, project_id=project.id, as_of=LATER
    )
    assert [one.completeness for one in recorded.standing_responses] == [
        "acknowledgement",
        "substantive",
    ]
    # An acknowledgement alone already stopped the clock: "they have not
    # replied" stops being true the moment they reply.
    assert recorded.answered


def test_a_reply_links_to_the_incoming_document(session, project):
    """One of the four kinds, resolved against a real row of this project."""

    plans = _planned(session, project)
    row = _retain(session, project, plans=plans)
    document_id = session.scalars(
        select(Document.id)
        .where(Document.project_id == project.id)
        .order_by(Document.id)
    ).first()

    recorded = _reply(
        session,
        project,
        request_id=row.id,
        document_id=document_id,
        observation=None,
        observed_by_principal=None,
        source_reference="the letter they attached, page 2",
    )

    assert recorded.evidence_kind == "document"
    assert recorded.document_id == document_id
    assert recorded.observation is None


def test_a_reply_links_to_an_exact_source_segment(session, project):
    """The narrowest of the four: the passage the answer is actually in."""

    plans = _planned(session, project)
    row = _retain(session, project, plans=plans)
    segment_id = session.scalars(
        select(SourceSegment.id)
        .where(SourceSegment.project_id == project.id)
        .order_by(SourceSegment.id)
    ).first()
    assert segment_id is not None, "this scenario retains cited passages"

    recorded = _reply(
        session,
        project,
        request_id=row.id,
        source_segment_id=segment_id,
        observation=None,
        observed_by_principal=None,
        source_reference="the cell they pointed at",
    )

    assert recorded.evidence_kind == "source_segment"
    assert recorded.source_segment_id == segment_id


def test_a_reply_naming_a_delivery_that_is_not_this_projects_is_refused(
    session, project
):
    """The delivery link is a real reference, proved where the row is written.

    The fixture takes no delivery, so what is asserted is the half that matters
    for every kind: the identifier has to resolve inside this project, and a
    number that resolves to nothing is refused by PostgreSQL rather than stored
    as evidence.
    """

    plans = _planned(session, project)
    row = _retain(session, project, plans=plans)

    with pytest.raises(OutgoingRequestRefused):
        _reply(
            session,
            project,
            request_id=row.id,
            source_delivery_id=9_000_001,
            observation=None,
            observed_by_principal=None,
        )


def test_a_reply_with_no_evidence_or_with_two_kinds_is_refused(
    session, project
):
    """Exactly one of the four, derived from what arrived rather than declared."""

    plans = _planned(session, project)
    row = _retain(session, project, plans=plans)
    document_id = session.scalars(
        select(Document.id)
        .where(Document.project_id == project.id)
        .order_by(Document.id)
    ).first()

    with pytest.raises(OutgoingRequestRefused) as none_given:
        _reply(
            session,
            project,
            request_id=row.id,
            observation=None,
            observed_by_principal=None,
        )
    assert "exactly one" in str(none_given.value)

    with pytest.raises(OutgoingRequestRefused) as two_given:
        _reply(session, project, request_id=row.id, document_id=document_id)
    assert "exactly one" in str(two_given.value)


def test_a_reply_with_no_reference_is_refused(session, project):
    """"They replied" with nothing to go back to is the STALE assumption."""

    plans = _planned(session, project)
    row = _retain(session, project, plans=plans)

    with pytest.raises(OutgoingRequestRefused):
        _reply(session, project, request_id=row.id, source_reference="   ")


# --- a retired plan retires the ask, not the record (#835 x #837) -----------


def _cancel(session, project, *, plan_id):
    """Retire one Follow-up Plan through #835's own lifecycle command."""

    outcome = cancel_follow_up_plan(
        session,
        project_id=project.id,
        plan_id=plan_id,
        principal=COORDINATOR,
        cancellation_reason="no_longer_needed",
        closed_at=LATER,
        idempotency_key=f"close:{uuid4().hex[:10]}",
    )
    assert outcome.closed, outcome
    session.expire_all()
    return outcome


def test_a_cancelled_plan_stops_the_ask_and_keeps_the_record(session, project):
    """Cancelling the question ends the chase; it does not unsend the message.

    Both halves are the point. A chase list that kept telling a coordinator to
    telephone City Water about a question they explicitly cancelled is the
    failure ADR-0090 retired ``STALE`` for, rebuilt from the other end. And a
    record that vanished with the plan would be asserting that nobody was ever
    asked, which is false and unrecoverable.
    """

    plans = _planned(session, project)
    row = _retain(session, project, plans=plans)

    # Past the boundary with nothing back: the finding exists.
    before = read_follow_up_bundles(
        session, project_id=project.id, as_of=LATER
    )
    assert any(
        bundle.band == UNANSWERED_REQUEST_BAND for bundle in before.bundles
    )

    for plan_id in plans:
        _cancel(session, project, plan_id=plan_id)

    after = read_follow_up_bundles(session, project_id=project.id, as_of=LATER)
    assert all(
        bundle.band != UNANSWERED_REQUEST_BAND for bundle in after.bundles
    ), "a request whose every plan was retired is still raising an ask"

    # The record is untouched: still retained, still unanswered, still
    # readable, with the exact content it was sent with.
    assert after.retained_outgoing_requests == 1
    (recorded,) = read_correspondence(
        session, project_id=project.id, as_of=LATER
    )
    assert recorded.request_id == row.id
    assert not recorded.answered
    assert read_sent_content(recorded).decode("utf-8") == MESSAGE


def test_a_request_covering_a_retired_plan_and_a_live_one_names_both(
    session, project
):
    """The mixed case, where the request-to-plans relation earns its keep.

    One message advanced two questions and one of them has since been retired.
    The ask survives for the other, and the follow-up says plainly that the
    message also covered something nobody is waiting on any more -- which a
    single ``follow_up_plan_id`` could not have said at all.
    """

    plans = _planned(session, project)
    assert len(plans) == 2
    _retain(session, project, plans=plans)
    _cancel(session, project, plan_id=plans[0])

    reading = read_follow_up_bundles(
        session, project_id=project.id, as_of=LATER
    )
    assert any(
        bundle.band == UNANSWERED_REQUEST_BAND for bundle in reading.bundles
    ), "one live plan still owes an answer, so the finding stands"

    view = chase_view(
        reading,
        read_correspondence(session, project_id=project.id, as_of=LATER),
        {plans[1]: "U-042 — Promised for: which date does the utility hold to?"},
    )
    bundle = next(one for one in view.bundles if one.correspondence.plan_ids)
    (recorded,) = bundle.correspondence.requests

    assert recorded.covered == (plans[1],)
    assert recorded.uncovered == ()
    assert recorded.retired == (plans[0],)
    assert recorded.coverage_sentence == (
        "Covers 1 of the 1 Follow-up Plans in this follow-up: all of them. It "
        f"also named 1 Follow-up Plan since retired: {plans[0]}."
    )


def test_a_hand_built_request_with_no_plans_is_left_alone(session, project):
    """The injection port keeps working, and the reason is written down.

    ``append_outgoing_request`` refuses a request that advances nothing, so a
    request with no plans can only have come from the reading's own injection
    kwarg. It has no plan liveness to inherit and is not filtered on one.
    """

    _planned(session, project)
    injected = RetainedOutgoingRequest(
        request_identity="letter-2026-09-01",
        organization=WATER,
        subject_identities=(subject(FIRST),),
        question="Which date do you hold to?",
        sent_on=SENT_ON,
        expected_response_by=EXPECTED_BY,
        reference=SourceReference(
            kind="outgoing_request",
            identity="letter-2026-09-01",
            detail="a request handed to the reading",
        ),
    )

    reading = read_follow_up_bundles(
        session,
        project_id=project.id,
        as_of=LATER,
        outgoing_requests=[injected],
    )

    assert injected.follow_up_plan_ids == ()
    assert any(
        bundle.band == UNANSWERED_REQUEST_BAND for bundle in reading.bundles
    )


# --- correction is an append -------------------------------------------------


def test_a_mistaken_request_is_corrected_without_losing_the_original(
    session, project
):
    """The original stays, the correction stands, and the band reads one."""

    plans = _planned(session, project)
    wrong = _retain(session, project, plans=plans, sent_on=date(2026, 9, 1))
    right = _retain(
        session,
        project,
        plans=plans,
        sent_on=date(2026, 9, 2),
        supersedes_request_id=wrong.id,
        correction_reason="It went out on the 2nd, not the 1st.",
    )

    recorded = read_correspondence(session, project_id=project.id, as_of=LATER)
    by_id = {one.request_id: one for one in recorded}
    # Both are readable; history is not rewritten.
    assert set(by_id) == {wrong.id, right.id}
    assert by_id[wrong.id].sent_on == date(2026, 9, 1)
    assert not by_id[wrong.id].stands
    assert by_id[right.id].stands
    assert by_id[right.id].corrects_request_id == wrong.id

    # And the no-response reading counts the correction once, not both.
    outstanding = read_retained_outgoing_requests(
        session, project_id=project.id, as_of=LATER
    )
    assert [one.request_identity for one in outstanding] == [str(right.id)]


def test_a_mistaken_reply_is_corrected_without_losing_the_original(
    session, project
):
    """Same discipline one layer down, and the original is still on the page."""

    plans = _planned(session, project)
    row = _retain(session, project, plans=plans)
    wrong = _reply(
        session, project, request_id=row.id, received_on=date(2026, 9, 25)
    )
    right = _reply(
        session,
        project,
        request_id=row.id,
        received_on=date(2026, 9, 26),
        supersedes_response_id=wrong.id,
        correction_reason="The letter is dated the 26th.",
    )

    (recorded,) = read_correspondence(
        session, project_id=project.id, as_of=LATER
    )
    by_id = {one.response_id: one for one in recorded.responses}
    assert set(by_id) == {wrong.id, right.id}
    assert not by_id[wrong.id].stands
    assert by_id[right.id].stands
    assert [one.response_id for one in recorded.standing_responses] == [right.id]


def test_a_correction_without_a_reason_is_refused(session, project):
    """A correction that does not say why is an overwrite with extra steps."""

    plans = _planned(session, project)
    wrong = _retain(session, project, plans=plans)

    with pytest.raises(OutgoingRequestRefused):
        _retain(
            session,
            project,
            plans=plans,
            supersedes_request_id=wrong.id,
            correction_reason="",
        )


# --- the screen --------------------------------------------------------------


def test_the_week_offers_both_recordings_and_says_what_neither_does(
    session, project, client
):
    """Record sent, Record response, and the link back to the question."""

    plans = _planned(session, project)
    row = _retain(session, project, plans=plans[:1])
    session.expire_all()

    markup = client.get(f"/work/{project.slug}").text
    page = unescape(page_without_shell(markup))

    assert "What has been sent, and what came back" in page
    assert "Record sent" in page
    assert "Record response" in page
    assert "Return to the coordination question" in page
    assert (
        "Covers 1 of the 2 Follow-up Plans in this follow-up. Not covered by "
        f"this request: {plans[1]}." in page
    )
    assert f"/work/{project.slug}/follow-up/sent/{row.id}" in markup
    # The digest is shown beside the link, never instead of the message.
    assert row.content_sha256 in page


def test_the_retained_message_is_served_back_to_a_member(
    session, project, client
):
    """The point of retaining the content: a coordinator can read it."""

    plans = _planned(session, project)
    row = _retain(session, project, plans=plans)
    session.expire_all()

    served = client.get(f"/work/{project.slug}/follow-up/sent/{row.id}")

    assert served.status_code == 200
    assert served.text == MESSAGE


def test_recording_a_send_from_the_page_writes_the_relation(
    session, project, client
):
    """The deliberate first caller, driven through the form the page rendered."""

    plans = _planned(session, project)
    session.expire_all()

    posted = client.post(
        f"/work/{project.slug}/follow-up/sent",
        data={
            "follow_up_plan_id": [str(plan) for plan in plans],
            "covered_subject_key": [subject(FIRST)],
            "external_organization": WATER,
            "question": "Which date do you hold to?",
            "sent_content": MESSAGE,
            "sent_on": SENT_ON.isoformat(),
            "sent_by": "rosa.mehta@example.test",
            "expected_response_by": EXPECTED_BY.isoformat(),
        },
    )

    assert posted.status_code == 201, posted.text
    session.expire_all()
    (recorded,) = read_correspondence(
        session, project_id=project.id, as_of=NOW
    )
    assert recorded.covered_plan_ids == plans
    assert recorded.sent_by_principal == "rosa.mehta@example.test"
    assert recorded.recorded_by_principal == COORDINATOR.subject
    assert read_sent_content(recorded).decode("utf-8") == MESSAGE


def test_a_send_with_no_usable_expected_date_records_nothing(
    session, project, client
):
    """The date is explicit or there is no request; nothing is derived quietly.

    Two shapes, because the form is not the only way a POST arrives. A field
    the request never carried is refused by the route signature before the
    handler runs, and a field carrying something that is not a date is refused
    by the seam, on the week, in the coordinator's own words. Neither retains
    anything, which is the half that matters: a request whose boundary nobody
    set could later be used to say somebody failed to answer.
    """

    plans = _planned(session, project)
    session.expire_all()
    submission = {
        "follow_up_plan_id": [str(plan) for plan in plans],
        "covered_subject_key": [subject(FIRST)],
        "external_organization": WATER,
        "question": "Which date do you hold to?",
        "sent_content": MESSAGE,
        "sent_on": SENT_ON.isoformat(),
        "sent_by": "local:coordinator",
    }

    omitted = client.post(f"/work/{project.slug}/follow-up/sent", data=submission)
    assert omitted.status_code == 422, omitted.text

    malformed = client.post(
        f"/work/{project.slug}/follow-up/sent",
        data={**submission, "expected_response_by": "whenever they get to it"},
    )
    assert malformed.status_code == 409, malformed.text
    assert "is a date" in unescape(page_without_shell(malformed.text))

    assert read_correspondence(session, project_id=project.id, as_of=NOW) == ()


def test_recording_a_reply_from_the_page_leaves_the_question_open(
    session, project, client
):
    """The page says the reply landed and says the question is still open."""

    plans = _planned(session, project)
    row = _retain(session, project, plans=plans)
    session.expire_all()

    posted = client.post(
        f"/work/{project.slug}/follow-up/response",
        data={
            "request_id": str(row.id),
            "received_on": REPLIED_ON.isoformat(),
            "completeness": "substantive",
            "evidence_kind": "manual_observation",
            "observation": "They confirmed 15 December on the telephone.",
            "observed_by": "local:coordinator",
            "source_reference": "telephone call, 11:20",
        },
    )

    assert posted.status_code == 201, posted.text
    page = unescape(page_without_shell(posted.text))
    assert "still open until somebody settles it" in page
    session.expire_all()
    (recorded,) = read_correspondence(
        session, project_id=project.id, as_of=LATER
    )
    assert recorded.answered
    assert tuple(
        session.scalars(
            select(DeltaFollowUpPlan.id)
            .where(DeltaFollowUpPlan.project_id == project.id)
            .order_by(DeltaFollowUpPlan.id)
        )
    ) == plans


# --- the boundary, the form, and the thing that must not exist ---------------


def test_the_recording_routes_are_admitted_and_read_protected_relations():
    """#824's rule, asked of the three routes #837 adds."""

    for key in (
        ("POST", "/work/{slug}/follow-up/sent"),
        ("POST", "/work/{slug}/follow-up/response"),
        ("GET", "/work/{slug}/follow-up/sent/{request_id}"),
    ):
        assert key in web_boundary.PILOT_ROUTES, key
        assert (
            web_boundary.PILOT_ROUTES[key].relations
            <= web_boundary.PROTECTED_RELATIONS
        ), key
    assert web_boundary.unprotected_route_relations() == ()
    reached = web_boundary.PILOT_ROUTES[
        ("POST", "/work/{slug}/follow-up/sent")
    ].relations
    for relation in (
        "outgoing_requests",
        "outgoing_request_plans",
        "outgoing_request_responses",
    ):
        assert relation in reached, relation


def test_both_recording_forms_carry_the_request_forgery_field(
    session, project, client
):
    """#821's rule, read off the page these routes actually render."""

    plans = _planned(session, project)
    _retain(session, project, plans=plans)
    session.expire_all()

    markup = client.get(f"/work/{project.slug}").text
    for action in (
        f"/work/{project.slug}/follow-up/sent",
        f"/work/{project.slug}/follow-up/response",
    ):
        opening = markup.index(f'action="{action}"')
        closing = markup.index("</form>", opening)
        assert 'name="csrf_token"' in markup[opening:closing], action


def test_nothing_on_the_follow_up_section_sends_anything(
    session, project, client
):
    """Corridor sends nothing, and this is the proof that survives a rewrite.

    The page half is asserted from the rendered markup, and the stronger half
    from the source: the application has exactly one mail sender and exactly
    one caller of it, the sign-in link. A follow-up control that mailed
    anybody would have to add a second, and would fail here.
    """

    plans = _planned(session, project)
    _retain(session, project, plans=plans)
    session.expire_all()

    markup = client.get(f"/work/{project.slug}").text
    page = unescape(page_without_shell(markup)).lower()
    assert "corridor sends nothing" in page
    assert not any(
        phrase in page
        for phrase in ("send this email", "send the message", "email the utility")
    )
    actions = set(
        re.findall(r'<form[^>]*action="([^"]+)"', page_without_shell(markup))
    )
    # Every form the week can carry, named: the Issue section's two, #835's
    # plan lifecycle and scheduling, and #837's two recordings. A send would be
    # a seventh, and none exists to render.
    assert actions <= {
        f"/work/{project.slug}/issue/authorize",
        f"/work/{project.slug}/issue/prepare",
        f"/work/{project.slug}/schedule",
        f"/work/{project.slug}/follow-up/close",
        f"/work/{project.slug}/follow-up/sent",
        f"/work/{project.slug}/follow-up/response",
    }, actions

    application = pathlib.Path(
        corridor.web.app.__file__
    ).read_text(encoding="utf-8")
    assert application.count("sender.send_sign_in_link") == 1
    assert re.findall(r"sender\.send_\w+", application) == [
        "sender.send_sign_in_link"
    ]


# --- the history stays reachable after the plans close (#837 x #642) ---------
#
# #910 reported this honestly rather than quietly building it: a follow-up
# bundle is built from the Follow-up Plans a week is *still* asking about, so
# once every plan one message advanced has closed, that message has no bundle
# to hang on and the week renders it nowhere.  The record survived in
# ``read_correspondence`` and in the database and reached nobody.
#
# The maintainer ruled that history must remain reachable after plans close,
# on the existing per-plan or Record history view, with the sent content and
# the evidence links.  The per-plan section is on the week, and the week's
# follow-up reading is exactly what drops a closed plan -- extending it could
# not reach a closed ask at all -- so this is the Record history view, which is
# read-only, needs no designation, and exists to answer "what happened, and who
# did it".


def _cancel_every(session, project, plans):
    for plan_id in plans:
        _cancel(session, project, plan_id=plan_id)


def _readable(markup: str) -> str:
    """The page as one run of text, so a sentence wrapped in markup matches."""

    return re.sub(r"\s+", " ", unescape(page_without_shell(markup)))


def test_the_week_drops_a_closed_asks_message_and_the_record_page_keeps_it(
    session, project, client
):
    """The gap #910 reported, and the surface that closes it, in one walk.

    Both halves are asserted against rendered pages rather than readers,
    because the defect was never in the reader: the record was there the whole
    time and no screen printed it.
    """

    plans = _planned(session, project)
    row = _retain(session, project, plans=plans)
    session.expire_all()

    week = _readable(client.get(f"/work/{project.slug}").text)
    assert f"Recorded as sent on {SENT_ON} to {WATER}" in week

    _cancel_every(session, project, plans)
    session.expire_all()

    closed_week = _readable(client.get(f"/work/{project.slug}").text)
    assert f"Recorded as sent on {SENT_ON} to {WATER}" not in closed_week

    markup = client.get(f"/record/{project.slug}").text
    page = _readable(markup)
    assert "What has been sent, and what came back" in page
    assert f"Recorded as sent on {SENT_ON} to {WATER}" in page
    assert "Which date do you hold to?" in page
    # The conflict is named the way every other section of this page names it,
    # not by the retained subject key.
    assert (
        "<dt>Utility Conflicts named in it</dt>"
        f"<dd>U-{FIRST:03d} (Utility Conflicts row {FIRST})</dd>" in page
    )
    # The sent content, reachable, with the digest beside it and not instead.
    assert f"/work/{project.slug}/follow-up/sent/{row.id}" in markup
    assert row.content_sha256 in page
    assert client.get(
        f"/work/{project.slug}/follow-up/sent/{row.id}"
    ).text == MESSAGE


def test_the_record_page_says_how_each_closed_ask_ended(session, project, client):
    """A retired ask reads as finished, in the words the closure act used."""

    plans = _planned(session, project)
    _retain(session, project, plans=plans)
    _cancel_every(session, project, plans)
    session.expire_all()

    page = _readable(client.get(f"/record/{project.slug}").text)

    for plan_id in plans:
        assert f"<th scope=\"row\">{plan_id}</th>" in page
    assert page.count("No longer an outside ask") == len(plans)
    assert (
        f"Closed on {LATER.date()} by {COORDINATOR.subject}: "
        "this no longer needs an outside answer." in page
    )
    assert "No closure is recorded for this Follow-up Plan" not in page
    # The proposed change the ask was raised on, so the question behind it is
    # findable in the section above rather than only nameable.
    assert page.count("Raised on proposed change") == len(plans)


def test_a_retired_asks_history_carries_no_control_at_all(
    session, project, client
):
    """History, not a chase: nothing here can be acted on, including by post.

    The record page carries exactly one form, the GET search, and this is the
    section most likely to grow a second one -- a "chase them again" beside a
    message nobody answered is an easy control to add and would resurrect an
    ask the coordinator explicitly retired.
    """

    plans = _planned(session, project)
    _retain(session, project, plans=plans)
    _cancel_every(session, project, plans)
    session.expire_all()

    markup = client.get(f"/record/{project.slug}").text
    body = page_without_shell(markup)

    assert 'method="post"' not in body.lower()
    assert body.lower().count("<form") == 1
    assert "csrf" not in body.lower()
    for control in ("Record sent", "Record response", "Send", "Chase"):
        assert f">{control}<" not in body
    # And the section says what it is before anything else in it.
    assert (
        "This is the record of what was asked, not a list of what is still "
        "outstanding." in _readable(markup)
    )


def test_the_record_page_links_each_reply_to_the_evidence_behind_it(
    session, project, client
):
    """The maintainer's second requirement: the evidence, reachable.

    Two of the four kinds are records Corridor holds at their own route, and
    the reply names which row, so the link is to that row and not to a list.
    """

    plans = _planned(session, project)
    row = _retain(session, project, plans=plans)
    segment_id = session.scalars(
        select(SourceSegment.id)
        .where(SourceSegment.project_id == project.id)
        .order_by(SourceSegment.id)
    ).first()
    document_id = session.scalars(
        select(Document.id)
        .where(Document.project_id == project.id)
        .order_by(Document.id)
    ).first()
    _reply(
        session,
        project,
        request_id=row.id,
        completeness="partial",
        source_segment_id=segment_id,
        observation=None,
        observed_by_principal=None,
        source_reference="the cell they pointed at",
    )
    _reply(
        session,
        project,
        request_id=row.id,
        document_id=document_id,
        observation=None,
        observed_by_principal=None,
        source_reference="the letter they attached, page 2",
    )
    _cancel_every(session, project, plans)
    session.expire_all()

    markup = client.get(f"/record/{project.slug}").text

    assert f"/sources/{project.slug}/passage/{segment_id}" in markup
    assert f"/sources/{project.slug}/document/{document_id}/original" in markup
    page = _readable(markup)
    assert "They answered part of the ask" in page
    assert "They answered the ask" in page


def test_the_four_facts_an_acknowledgement_leaves_true_stay_separate(
    session, project, client
):
    """Acknowledged, still open, answer outstanding, and nothing settled.

    The maintainer kept these four independently true, and a history page is
    where they are most likely to collapse into one: by the time somebody reads
    it the follow-up is usually over, and "they replied" reads as "it was
    answered" unless the page says otherwise in as many words.
    """

    plans = _planned(session, project)
    row = _retain(session, project, plans=plans)
    _reply(
        session,
        project,
        request_id=row.id,
        completeness="acknowledgement",
        observation="They confirmed they have the question.",
        source_reference="their reply of 25 September",
    )
    session.expire_all()

    page = _readable(client.get(f"/record/{project.slug}").text)

    # One: a reply arrived, and it was an acknowledgement.
    assert "They acknowledged, without answering yet" in page
    # Two: the question it asked about has not been closed.
    assert page.count("No closure is recorded for this Follow-up Plan") == len(plans)
    # Three: the answer itself is still outstanding.
    assert (
        "An acknowledgement is recorded and nothing more; the answer itself "
        "is still outstanding." in page
    )
    # Four: recording it settled nothing.
    assert (
        "A recorded reply stops the no-response finding. It does not settle "
        "the question, resolve the proposed change, or change an accepted "
        "value." in page
    )


def test_a_superseded_plan_does_not_inherit_the_predecessors_correspondence(
    session, project, client
):
    """The predecessor's message stays readable and stays the predecessor's.

    A replacement plan does not inherit the old request or its response
    deadline; the retention decided for the first slice is that the
    predecessor's correspondence is kept as history and the successor starts
    with no assumed sent request. Both halves are asserted, because keeping
    the history and attaching it to the successor are different acts and only
    one of them is right.
    """

    plans = _planned(session, project)
    row = _retain(session, project, plans=plans[:1])
    outcome = update_follow_up_plan(
        session,
        project_id=project.id,
        plan_id=plans[0],
        principal=COORDINATOR,
        open_question="Which date does City Water hold to, in writing?",
        responsible_organization=WATER,
        closed_at=LATER,
        idempotency_key=f"correct:{uuid4().hex[:10]}",
    )
    assert outcome.closed, outcome
    successor = outcome.successor_plan_id
    session.expire_all()

    (recorded,) = read_correspondence(session, project_id=project.id, as_of=None)
    assert recorded.covered_plan_ids == plans[:1]
    assert successor not in recorded.covered_plan_ids

    page = _readable(client.get(f"/record/{project.slug}").text)
    assert f"Recorded as sent on {SENT_ON} to {WATER}" in page
    assert f"Replaced by Follow-up Plan {successor}" in page
    assert (
        f"Closed on {LATER.date()} by {COORDINATOR.subject}, replaced by "
        f"Follow-up Plan {successor}. The question was corrected, not "
        "abandoned." in page
    )


def test_the_record_page_keeps_a_corrected_request_beside_its_correction(
    session, project, client
):
    """A page that hid the original would be rewriting history (#837)."""

    plans = _planned(session, project)
    row = _retain(session, project, plans=plans)
    corrected = _retain(
        session,
        project,
        plans=plans,
        question="Which date do you hold to, for U-042 only?",
        supersedes_request_id=row.id,
        correction_reason="the first message named the wrong conflict",
    )
    _cancel_every(session, project, plans)
    session.expire_all()

    page = _readable(client.get(f"/record/{project.slug}").text)

    assert "Corrected by a later record" in page
    assert (
        f"Corrects the request recorded as {row.id}: the first message named "
        "the wrong conflict" in page
    )
    assert "Which date do you hold to?" in page
    assert "Which date do you hold to, for U-042 only?" in page
    assert corrected.id != row.id


def test_reading_the_correspondence_history_needs_no_designation(
    session, project, client
):
    """A read-only member may inspect the correspondence they may read.

    The Record history view's boundary is the project's plain read boundary
    and not the coordination designation, and this section must not be the one
    thing on the page that quietly needs more than reading.
    """

    plans = _planned(session, project)
    _retain(session, project, plans=plans)
    _cancel_every(session, project, plans)
    session.expire_all()
    reader = HumanPrincipal("local:correspondence-reader")
    seed_membership(session, project, reader, designations=[])
    corridor.web.app.app.dependency_overrides[get_human_principal] = lambda: reader

    answered = client.get(f"/record/{project.slug}")

    assert answered.status_code == 200
    page = _readable(answered.text)
    assert f"Recorded as sent on {SENT_ON} to {WATER}" in page


def test_the_record_page_reads_its_correspondence_without_a_cutoff(
    session, project
):
    """A history reading declares no moment, so it clips nothing at one.

    The record page reads no clock at all, and the one failure this would
    reintroduce is the one the section exists to fix: the most recent message
    silently missing from the history of what was sent.
    """

    plans = _planned(session, project)
    early = _retain(session, project, plans=plans, sent_on=date(2026, 9, 1))
    late = _retain(
        session,
        project,
        plans=plans,
        sent_on=date(2027, 3, 4),
        expected_response_by=date(2027, 3, 20),
    )
    session.expire_all()

    history = read_record_history(session, project_id=project.id)

    assert [entry.request.request_id for entry in history.correspondence] == [
        early.id,
        late.id,
    ]
    assert not any(
        plan.closed
        for entry in history.correspondence
        for plan in entry.plans
    )


# --- the scenario, run over #848's harness ----------------------------------
#
# Correspondence recording is a *selected* capability: #652's maintainer
# decision makes it required when a partner's configured workflow includes
# response tracking and explicitly not a blocker otherwise. So it is not a step
# of the core journey every customer walks, and it is not left unexercised
# either. It is a declared extension in `journey_matrix.SELECTED_CAPABILITIES`,
# walked here by the same runner, reported in the same sentences, and held to
# the same questions the core walk is held to.


@dataclass
class Correspondence:
    """What one walk of the correspondence capability carries between steps."""

    session: Session
    project: Project
    client: TestClient
    plans: tuple[int, ...]
    carried: dict[str, object] = field(default_factory=dict)


def _step_record_what_was_sent(walk: Correspondence) -> None:
    posted = walk.client.post(
        f"/work/{walk.project.slug}/follow-up/sent",
        data={
            "follow_up_plan_id": [str(plan) for plan in walk.plans],
            "covered_subject_key": [subject(FIRST)],
            "external_organization": WATER,
            "question": "Which date do you hold to?",
            "sent_content": MESSAGE,
            "sent_on": SENT_ON.isoformat(),
            "sent_by": "rosa.mehta@example.test",
            "expected_response_by": EXPECTED_BY.isoformat(),
        },
    )
    assert posted.status_code == 201, (
        "a coordinator cannot record a message they sent: " + posted.text[:200]
    )
    walk.session.expire_all()
    (recorded,) = read_correspondence(
        walk.session, project_id=walk.project.id, as_of=NOW
    )
    assert recorded.covered_plan_ids == walk.plans, (
        "the request did not advance every plan it named"
    )
    served = walk.client.get(
        f"/work/{walk.project.slug}/follow-up/sent/{recorded.request_id}"
    )
    assert served.status_code == 200 and served.text == MESSAGE, (
        "the exact message that was sent cannot be read back"
    )
    walk.carried["request_id"] = recorded.request_id


def _step_the_boundary_passes(walk: Correspondence) -> None:
    reading = read_follow_up_bundles(
        walk.session, project_id=walk.project.id, as_of=LATER
    )
    assert any(
        bundle.band == UNANSWERED_REQUEST_BAND for bundle in reading.bundles
    ), "past the declared boundary with nothing recorded back, and no finding"


def _step_record_what_came_back(walk: Correspondence) -> None:
    before = walk.session.scalars(
        select(ProjectRecordRevision.id).where(
            ProjectRecordRevision.project_id == walk.project.id
        )
    ).all()
    posted = walk.client.post(
        f"/work/{walk.project.slug}/follow-up/response",
        data={
            "request_id": str(walk.carried["request_id"]),
            "received_on": REPLIED_ON.isoformat(),
            "completeness": "substantive",
            "evidence_kind": "manual_observation",
            "observation": "They confirmed 15 December on the telephone.",
            "observed_by": "local:coordinator",
            "source_reference": "telephone call, 11:20",
        },
    )
    assert posted.status_code == 201, (
        "a coordinator cannot record a reply: " + posted.text[:200]
    )
    walk.session.expire_all()
    assert (
        read_retained_outgoing_requests(
            walk.session, project_id=walk.project.id, as_of=LATER
        )
        == ()
    ), "a recorded reply did not stop the no-response finding"
    assert tuple(
        walk.session.scalars(
            select(DeltaFollowUpPlan.id)
            .where(DeltaFollowUpPlan.project_id == walk.project.id)
            .order_by(DeltaFollowUpPlan.id)
        )
    ) == walk.plans, "a recorded reply resolved a Follow-up Plan"
    assert walk.session.scalars(
        select(ProjectRecordRevision.id).where(
            ProjectRecordRevision.project_id == walk.project.id
        )
    ).all() == before, "a recorded reply moved the accepted record"


def _step_correct_a_mistaken_record(walk: Correspondence) -> None:
    posted = walk.client.post(
        f"/work/{walk.project.slug}/follow-up/sent",
        data={
            "follow_up_plan_id": [str(plan) for plan in walk.plans],
            "covered_subject_key": [subject(FIRST)],
            "external_organization": WATER,
            "question": "Which date do you hold to?",
            "sent_content": MESSAGE,
            "sent_on": date(2026, 9, 2).isoformat(),
            "sent_by": "rosa.mehta@example.test",
            "expected_response_by": EXPECTED_BY.isoformat(),
            "corrects_request_id": str(walk.carried["request_id"]),
            "correction_reason": "It went out on the 2nd, not the 1st.",
        },
    )
    assert posted.status_code == 201, (
        "a mistaken request cannot be corrected: " + posted.text[:200]
    )
    walk.session.expire_all()
    recorded = read_correspondence(
        walk.session, project_id=walk.project.id, as_of=LATER
    )
    standing = [one for one in recorded if one.stands]
    assert len(recorded) == 2 and len(standing) == 1, (
        "a correction did not leave the original readable beside it"
    )
    assert standing[0].corrects_request_id == walk.carried["request_id"]


CORRESPONDENCE_STEPS: tuple[Step, ...] = (
    Step(
        name="record_what_was_sent",
        sentence="the coordinator records the message they sent, against the "
        "Follow-up Plans it advanced, and can read it back",
        owner="#837",
        run=_step_record_what_was_sent,
    ),
    Step(
        name="the_boundary_passes",
        sentence="the date they said they expected a reply by passes with "
        "nothing recorded back, and the follow-up says so",
        owner="#837",
        run=_step_the_boundary_passes,
    ),
    Step(
        name="record_what_came_back",
        sentence="the coordinator records what came back and what it was read "
        "from, which stops the finding and settles nothing",
        owner="#837",
        run=_step_record_what_came_back,
    ),
    Step(
        name="correct_a_mistaken_record",
        sentence="a request recorded wrongly is corrected by an append, and "
        "the original is still there",
        owner="#837",
        run=_step_correct_a_mistaken_record,
    ),
)


def test_the_correspondence_capability_runs_as_a_declared_scenario(
    session, project, client
):
    """Walk the selected capability, and report every step against its ticket.

    Run with ``-rP`` to read the report on a passing run; a failing run carries
    the whole report in its message.
    """

    walk = Correspondence(
        session=session,
        project=project,
        client=client,
        plans=_planned(session, project),
    )
    report = run_scenario(
        "Correspondence recording (#837, a selected capability)",
        CORRESPONDENCE_STEPS,
        walk,
    )
    print("\n" + report.render(), flush=True)

    assert not report.failures, (
        "a step nothing said could fail did:\n" + report.render()
    )
    assert len(report.passed) == len(CORRESPONDENCE_STEPS), (
        "this capability is built, so every step of it passes:\n"
        + report.render()
    )


def test_every_selected_capability_row_names_a_step_of_this_scenario():
    """A row nothing exercises is a claim, not an inventory entry (#848).

    The selected-capability inventory spans four scenarios, each walked by its
    own module: correspondence recording (#837) here, contact correction (#838)
    in ``test_contact_correction_form``, source-question disposition (#833) in
    ``test_source_question_disposition_journey``, and machine intake (#847) in
    ``test_machine_intake_journey``. A row is exercised when *some* scenario has
    a step of that name, so the declared set is their union.
    """

    from test_contact_correction_form import CONTACT_CORRECTION_STEPS
    from test_machine_intake_journey import MACHINE_INTAKE_STEPS
    from test_source_question_disposition_journey import SOURCE_QUESTION_STEPS

    declared = {step.name for step in CORRESPONDENCE_STEPS} | {
        step.name for step in CONTACT_CORRECTION_STEPS
    } | {
        step.name for step in SOURCE_QUESTION_STEPS
    } | {
        step.name for step in MACHINE_INTAKE_STEPS
    }
    unknown = sorted(
        {row.scenario for row in journey_matrix.SELECTED_CAPABILITIES}
        - declared
    )
    assert not unknown, (
        "these selected-capability rows name a step that does not exist: "
        + ", ".join(unknown)
    )
    unknown_workflows = sorted(
        {row.scenario for row in journey_matrix.SELECTED_CAPABILITY_WORKFLOWS}
        - declared
    )
    assert not unknown_workflows, (
        "these claimed workflows name a step that does not exist: "
        + ", ".join(unknown_workflows)
    )


def test_every_selected_capability_row_names_a_route_the_product_serves():
    """The empty route cell is what #848 counts as work still to do.

    There is none here, because #837 is what this inventory was waiting for.
    """

    served = {
        f"{method} {route.path}"
        for route in app.routes
        for method in getattr(route, "methods", ())
    }
    missing = sorted(
        row.route
        for row in journey_matrix.SELECTED_CAPABILITIES
        if row.route not in served
    )
    assert not missing, (
        "these selected-capability rows name a route nothing serves: "
        + ", ".join(missing)
    )
    assert journey_matrix.rows_without_an_act(
        journey_matrix.SELECTED_CAPABILITIES
    ) == ()
    assert journey_matrix.half_built_workflows(
        journey_matrix.SELECTED_CAPABILITY_WORKFLOWS
    ) == ()
    assert journey_matrix.unretrieved_outputs(
        journey_matrix.SELECTED_CAPABILITIES
    ) == ()
