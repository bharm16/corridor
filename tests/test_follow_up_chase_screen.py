"""The chase list, finally on a screen, and the ways a screen could lie (#658).

#425 built contact-ready follow-up bundles and merged with no route: the chase
list was deterministic, digested, and invisible.  This is the surface, rendered
in the Follow-up section of #536's ordered week, and the properties under test
are the ones that make a *second* surface dangerous rather than the ones that
are easy to assert.

The first is that the screen and the reading cannot disagree.  #425 put every
trigger, bundling and ordering rule in one derivation precisely so a second
surface could not hold a different opinion about which asks exist or what order
they come in.  Two tests hold that: one compares the rendered HTML's own bundle
identities, in document order, against the reader's tuple, and one confirms the
view refuses when a sequence is handed to it in any other order.

The second is that no "no response" state can appear while Corridor retains no
outgoing correspondence.  ``read_retained_outgoing_requests`` returns nothing,
so the band is empty in production; the danger is a screen deciding on its own
that a long-quiet bundle reads well as a chase.  The wording exists in exactly
one place, and the view refuses a reading that claims the band with no retained
request behind it.

The third is the amended definition of contact-ready: a bundle with no address
is still rendered, with its responsible role and an explicit unresolved state.
Nothing is suppressed for want of an address.

And the fourth is that no message is generated, sent, or tracked.  The brief is
read-only text, in no form, with no send control anywhere on the page.

Nothing here reads a clock.  Every instant is declared by the test.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from html import unescape
import re
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor.analytics import EventFamily, capture_events
from corridor.follow_up_bundles import (
    APPROACHING_COMMITMENT_BAND,
    ASK_CONFIRM_ACCEPTED_DATE,
    AWAITING_ANSWER_BAND,
    CONTACT_RESOLVED,
    CONTACT_UNRESOLVED,
    ELAPSED,
    OVERDUE_ANSWER_BAND,
    UNANSWERED_REQUEST_BAND,
    RetainedOutgoingRequest,
    SourceReference,
    read_follow_up_bundles,
    read_retained_outgoing_requests,
)
from corridor.models import (
    DeltaDisposition,
    DeltaFollowUpPlan,
    DeltaRecordDecision,
    DeltaReviewPacketReceipt,
    Project,
    ProjectRecordRevision,
)
from corridor.operating_mode import adopt_project_baseline
from corridor.packet_review import (
    FocusedAnswer,
    focused_request,
    read_review_items,
)
from corridor.principals import HumanPrincipal
from corridor.review_packets import NEEDS_COORDINATION, resolve_review_packet
from corridor.web.app import (
    app,
    get_human_principal,
    get_review_clock,
    get_session,
)
from corridor.web.follow_up_view import (
    NO_RESPONSE,
    FollowUpViewRefused,
    _refuse_reordered,
    chase_view,
    copy_ready_brief,
)

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
OUTSIDER = HumanPrincipal("local:outsider")

# Every instant and date below is declared here, never read from a clock.
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
TODAY = NOW.date()
RETURNS_AT = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
OVERDUE_RETURN = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)
# Sooner than the accepted Promised For below, and still in the future.
SOONER_RETURN = datetime(2026, 9, 10, 0, 0, tzinfo=timezone.utc)

SOON = "2026-09-21"
EARLIER_WEEK = "2026-09-10"
ALREADY_PAST = "2026-08-20"

WATER = "City Water"
WATER_CONTACT = "mains@citywater.example"
POWER = "Northline Power"

FIRST = 42
SECOND = 43

_BUNDLE_KEY = re.compile(r'data-bundle-key="([^"]*)"')


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


# --- the fixture -----------------------------------------------------------


class Chase:
    """One adopted project with exactly the accepted values a test needs."""

    def __init__(self, session: Session, project: Project):
        self.session = session
        self.project = project
        self.source = Rendition(session, project, "ucm-2026-08.xlsx")
        self.revision_of: dict[tuple[str, str], int] = {}
        self.baseline = None
        self.rows: set[int] = set()
        self.renditions: dict[str, Rendition] = {}
        self.accepted_dates: dict[int, str] = {}

    def accept(
        self, number: int, field_name: str, value: str, *, is_date: bool = False
    ) -> "Chase":
        fact, _ = self.source.capture(
            fact_type=field_name,
            value=value,
            subject_key=subject(number),
        )
        revision = accept_baseline_fact(self.session, self.project, fact)
        self.revision_of[(subject(number), field_name)] = revision
        if self.baseline is None:
            self.baseline = register_baseline(
                self.session, self.project, self.source.document, revision
            )
        if number not in self.rows:
            register_source_row(
                self.session,
                self.project,
                self.baseline,
                row_number=number,
                business_identity=f"U-{number:03d}",
            )
            self.rows.add(number)
        return self

    def organization(
        self, number: int, name: str, contact: str | None = None
    ) -> "Chase":
        self.accept(number, "external_org", name)
        if contact is not None:
            self.accept(number, "external_org_contact", contact)
        return self

    def promised(self, number: int, value: str) -> "Chase":
        self.accepted_dates[number] = value
        return self.accept(number, "committed_date", value, is_date=True)

    def rendition(self, name: str) -> Rendition:
        if name not in self.renditions:
            self.renditions[name] = Rendition(self.session, self.project, name)
        return self.renditions[name]

    def answer(
        self, *, document: str, family: str, revision: str, value: str, number: int
    ):
        """One arriving source's own answer for an accepted Promised For."""

        rendition = self.rendition(document)
        fact, segment = rendition.capture(
            fact_type="committed_date",
            value=value,
            subject_key=subject(number),
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
                    subject_key=subject(number),
                    field_name="committed_date",
                    accepted_value=self.accepted_dates[number],
                    proposed_value=value,
                    baseline_revision=self.revision_of[
                        (subject(number), "committed_date")
                    ],
                )
            ],
            is_complete_enumerative_source=False,
            row_accounting_sealed=False,
        )

    def adopt(self) -> "Chase":
        adopt_project_baseline(
            self.session,
            project_id=self.project.id,
            adopted_by_principal="local:adopter",
            baseline_source_sha256=self.source.document.sha256,
            importer_identity="follow_up_chase_screen_fixture",
            importer_version="v1",
            idempotency_key=f"adopt:{uuid4().hex[:10]}",
        )
        self.session.expire_all()
        return self


def _addressed(session: Session, project: Project) -> Chase:
    """One organization with a resolved contact, two accepted dates."""

    made = Chase(session, project)
    made.organization(FIRST, WATER, WATER_CONTACT)
    made.promised(FIRST, SOON)
    made.organization(SECOND, WATER, WATER_CONTACT)
    made.promised(SECOND, EARLIER_WEEK)
    return made.adopt()


def _cross_source(session: Session, project: Project) -> Chase:
    """Two retained sources answering one accepted Promised For differently."""

    made = Chase(session, project)
    made.organization(FIRST, WATER, WATER_CONTACT)
    made.promised(FIRST, SOON)
    made.answer(
        document="ucm-2026-09.xlsx",
        family="ucm-workbook",
        revision="2026-09",
        value="2026-12-15",
        number=FIRST,
    )
    made.answer(
        document="minutes-2026-09-02.pdf",
        family="meeting-minutes",
        revision="2026-09-02",
        value="2027-01-20",
        number=FIRST,
    )
    return made.adopt()


def _plan_every_child(
    session: Session, project: Project, *, return_date: datetime | None
) -> None:
    """Answer both sources of the one focused item coordination-needed."""

    reading = read_review_items(session, project_id=project.id, as_of=NOW)
    item = next(item for item in reading.items if item.focused)
    request = focused_request(
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
                return_date=return_date,
            )
            for child in item.children
        ],
    )
    result = resolve_review_packet(session, request)
    assert result.status == "saved", result
    session.expire_all()


def _reading(session: Session, project: Project, **kwargs):
    return read_follow_up_bundles(
        session, project_id=project.id, as_of=NOW, **kwargs
    )


def _rendered_keys(body: str) -> list[str]:
    """The bundle identities the page rendered, in document order."""

    return [unescape(value) for value in _BUNDLE_KEY.findall(body)]


# --- the screen and the reading cannot disagree ----------------------------


def test_the_screen_renders_exactly_the_readers_bundles_in_the_readers_order(
    session, project, client
):
    """One derivation owns which asks exist and what order they come in (#425).

    The page's own bundle identities are compared against the reader's tuple,
    in document order.  A second surface that filtered, re-sorted, or invented
    a bundle would fail here and nowhere else.
    """

    # Two interactions whose band order and date order deliberately disagree:
    # an accepted date inside the horizon (band 4, due later) and a plan whose
    # return date is sooner but not yet late (band 6).  A screen that sorted by
    # the date a coordinator can see would render these the other way round.
    _cross_source(session, project)
    _plan_every_child(session, project, return_date=SOONER_RETURN)

    reading = _reading(session, project)
    body = client.get(f"/work/{project.slug}").text

    assert len(reading.bundles) == 2
    assert [bundle.band for bundle in reading.bundles] == [
        APPROACHING_COMMITMENT_BAND,
        AWAITING_ANSWER_BAND,
    ]
    dates = [bundle.due_date for bundle in reading.bundles]
    assert dates == sorted(dates, reverse=True), (
        "this fixture is only a proof while band order contradicts date order"
    )
    assert _rendered_keys(body) == [
        bundle.bundle_key for bundle in reading.bundles
    ]


def test_the_view_refuses_any_sequence_but_the_readings_own(session, project):
    """The ordering promise is mechanical, not a convention to be remembered."""

    _addressed(session, project)
    reading = _reading(session, project)
    assert len(reading.bundles) == 2

    # What a re-sorted screen looks like: the ordinal the reading assigned no
    # longer matches the position the screen would render the bundle at.
    reversed_reading = replace(reading, bundles=tuple(reversed(reading.bundles)))
    with pytest.raises(FollowUpViewRefused, match="ordering belongs to the"):
        chase_view(reversed_reading)

    # And what a filtered screen looks like — the shape a "hide the ones with
    # no address" would produce.  The guard is exercised directly because no
    # public call can produce it: `chase_view` has no filter to reach through.
    view = chase_view(reading)
    with pytest.raises(FollowUpViewRefused, match="never suppressed"):
        _refuse_reordered(view.bundles[:1], reading)


def test_the_bands_and_their_sentences_are_printed_and_no_score_is(
    session, project, client
):
    """ADR-0010 abolished the severity scalar; #425 printed a sentence instead."""

    made = Chase(session, project)
    made.organization(FIRST, WATER, WATER_CONTACT)
    made.promised(FIRST, ALREADY_PAST)
    made.adopt()

    reading = _reading(session, project)
    body = client.get(f"/work/{project.slug}").text

    band = next(
        band for band in reading.bands if band.name == reading.bundles[0].band
    )
    assert band.sentence in body
    assert band.quantity.replace("_", " ") in body
    for forbidden in ("severity", "score", "priority score", "risk score"):
        assert forbidden not in body.lower()


# --- no "no response" while nothing outgoing is retained -------------------


def test_no_response_wording_cannot_appear_while_nothing_outgoing_is_retained(
    session, project, client
):
    """The band is empty in production, and quiet is not evidence of silence.

    The project below has been waiting since before the cutoff with no movement
    at all — exactly the shape a screen is tempted to caption "no response".
    """

    _cross_source(session, project)
    _plan_every_child(session, project, return_date=OVERDUE_RETURN)

    reading = _reading(session, project)
    body = client.get(f"/work/{project.slug}").text

    assert read_retained_outgoing_requests(
        session, project_id=project.id, as_of=NOW
    ) == ()
    assert reading.retained_outgoing_requests == 0
    assert reading.bundles
    assert all(
        bundle.band != UNANSWERED_REQUEST_BAND for bundle in reading.bundles
    )
    # The bundle *is* overdue and is said to be, which is a different claim.
    assert reading.bundles[0].band == OVERDUE_ANSWER_BAND
    assert "Past the date this bundle names" in body
    assert NO_RESPONSE.text not in body
    assert "no response" not in body.lower()


def test_the_view_refuses_a_no_response_band_with_no_retained_request(
    session, project
):
    """Rendering the state is refused as well as deriving it."""

    _addressed(session, project)
    reading = _reading(session, project)
    forged = replace(
        reading,
        bundles=(
            replace(reading.bundles[0], band=UNANSWERED_REQUEST_BAND, ordinal=1),
        ),
    )

    with pytest.raises(FollowUpViewRefused, match="empty inbox"):
        chase_view(forged)


def test_a_retained_request_past_its_boundary_does_produce_the_state(
    session, project
):
    """The predicate is implemented, not merely absent: the port is exercised."""

    _addressed(session, project)
    request = RetainedOutgoingRequest(
        request_identity="letter-2026-07-02",
        organization=WATER,
        subject_identities=(subject(FIRST),),
        question="Please confirm the date you now hold.",
        sent_on=date(2026, 7, 2),
        expected_response_by=date(2026, 7, 30),
        reference=SourceReference(
            kind="retained_outgoing_request",
            identity="letter-2026-07-02",
            detail="the request itself, as retained",
        ),
    )

    view = chase_view(_reading(session, project, outgoing_requests=[request]))

    unanswered = [
        row for row in view.bundles if row.bundle.band == UNANSWERED_REQUEST_BAND
    ]
    assert len(unanswered) == 1
    assert unanswered[0].no_response is NO_RESPONSE
    assert all(
        row.no_response is None
        for row in view.bundles
        if row.bundle.band != UNANSWERED_REQUEST_BAND
    )


# --- contact-ready, with or without an address ------------------------------


def test_a_bundle_with_no_recorded_contact_is_rendered_with_its_role(
    session, project, client
):
    """#425 as amended: no bundle is suppressed for want of an address."""

    made = Chase(session, project)
    made.organization(FIRST, POWER)  # no contact recorded at all
    made.promised(FIRST, SOON)
    made.adopt()

    reading = _reading(session, project)
    body = client.get(f"/work/{project.slug}").text

    assert len(reading.bundles) == 1
    recipient = reading.bundles[0].recipient
    assert recipient.contact_state == CONTACT_UNRESOLVED
    assert _rendered_keys(body) == [reading.bundles[0].bundle_key]
    assert POWER in body
    assert recipient.responsible_role in body
    assert "no contact is recorded for this role" in body
    assert "Contact not recorded" in body


def test_a_resolved_contact_names_the_individual_and_the_channel(
    session, project, client, tmp_path, monkeypatch
):
    """An explicit imported person and address appear on the actual screen."""
    from corridor.config import settings
    from corridor.models import ExternalOrg
    from corridor.project_contacts import import_contact_csv
    from corridor.push_intake import PushCredential, PushPayload, accept_delivery, bind_credential, register_push_credential

    _addressed(session, project)
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "store"))
    if session.scalar(select(ExternalOrg).where(ExternalOrg.name == WATER)) is None:
        session.add(ExternalOrg(name=WATER))
        session.flush()
    register_push_credential(session, customer="fixture", project=project, channel="webhook", material=project.slug)
    binding = bind_credential(session, PushCredential(channel="webhook", material=project.slug))
    raw = ("source_contact_id,organization_ref,responsible_role,person_name,channel,address\n"
           f"lead,{WATER},the responsible contact,Pat,email,{WATER_CONTACT}\n").encode()
    delivery = accept_delivery(session, binding, PushPayload(body=raw, filename="contacts.csv"))
    receipt = import_contact_csv(session, delivery.envelope, import_identity="screen-contacts")
    # A live import supplies its own recorded instant. Read after that receipt;
    # the fixed historical cutoff used by other screen tests must not see it.
    cutoff = receipt.recorded_at + timedelta(seconds=1)
    app.dependency_overrides[get_review_clock] = lambda: (lambda: cutoff)

    body = client.get(f"/work/{project.slug}").text
    reading = read_follow_up_bundles(session, project_id=project.id, as_of=cutoff)

    assert all(
        bundle.recipient.contact_state == CONTACT_RESOLVED
        for bundle in reading.bundles
    )
    assert WATER_CONTACT in body
    assert "Pat" in body and "by email" in body
    assert "Contact recorded" in body
    assert "Contact not recorded" not in body


def test_every_field_the_ticket_names_is_on_the_screen(
    session, project, client
):
    """Recipient, contact state, subject, ask, position, conflicts, date,
    evidence, and the consequence sentence — the amended contact-ready list."""

    _cross_source(session, project)
    _plan_every_child(session, project, return_date=RETURNS_AT)

    reading = _reading(session, project)
    body = client.get(f"/work/{project.slug}").text
    # The bundle the Follow-up Plan authorized, not the accepted-date ask that
    # sits beside it: this is the one carrying evidence and quoted wording.
    bundle = next(
        row for row in reading.bundles if row.ask != ASK_CONFIRM_ACCEPTED_DATE
    )
    assert bundle.quoted_wording and bundle.references

    assert bundle.recipient.sentence() in body
    assert bundle.subject_line in body
    assert bundle.ask_sentence in body
    for line in bundle.accepted_position:
        assert line.business_identity in body
        assert line.accepted_text in body
    for question in bundle.open_questions:
        assert question in body
    for reference in bundle.references:
        assert f"{reference.kind} {reference.identity}" in body
    assert bundle.due_date is not None and bundle.due_date.isoformat() in body
    # The incoming wording is quoted and attributed, never stated as the record.
    for quote in bundle.quoted_wording:
        assert quote.attribution in body


def test_each_citation_that_names_a_passage_opens_it(session, project, client):
    """A citation the record resolves to a passage is a link to that passage.

    #831 built one exact-source view and every surface opens it; a bundle's
    own citations are one of those surfaces. What is asserted is the address
    the *reading* resolved, so the screen cannot offer a passage the record
    did not name, and a reference the record resolves to nothing prints as it
    always did rather than acquiring a link the data does not support.
    """

    _cross_source(session, project)
    _plan_every_child(session, project, return_date=RETURNS_AT)

    reading = _reading(session, project)
    body = client.get(f"/work/{project.slug}").text

    named = {
        segment_id
        for bundle in reading.bundles
        for reference in bundle.references
        for segment_id in reference.source_segment_ids
    }
    assert named, "the fixture records no assessed passage to link to"
    for segment_id in sorted(named):
        assert f'href="/sources/{project.slug}/passage/{segment_id}"' in body
    unresolved = [
        reference
        for bundle in reading.bundles
        for reference in bundle.references
        if not reference.source_segment_ids
    ]
    assert unresolved, "the fixture records no unresolvable citation to leave alone"
    for reference in unresolved:
        assert f"{reference.kind} {reference.identity}" in body


# --- the brief is text, and nothing is sent --------------------------------


def test_the_brief_is_copyable_read_only_text_and_no_message_is_produced(
    session, project, client
):
    """#425 excludes drafting, sending, delivery tracking and escalation."""

    _addressed(session, project)

    body = client.get(f"/work/{project.slug}").text
    reading = _reading(session, project)

    for bundle in reading.bundles:
        brief = copy_ready_brief(bundle)
        assert brief.startswith("Subject: ")
        assert bundle.subject_line in brief
        assert bundle.ask_sentence in brief
    # Reachable and copyable from the keyboard: a labelled, read-only control.
    tags = re.findall(r"<textarea[^>]*>", body)
    assert len(tags) == len(reading.bundles)
    for tag in tags:
        assert " readonly" in tag
        assert "name=" not in tag
    for ordinal in range(1, len(reading.bundles) + 1):
        assert f'for="follow-up-bundle-{ordinal}-brief"' in body
        assert f'aria-describedby="follow-up-bundle-{ordinal}-brief-hint"' in body
    # And nothing that could send it.
    assert "<form" not in body
    assert "<button" not in body
    for forbidden in ("mailto:", "Send ", "smtp", "Draft email"):
        assert forbidden not in body


# --- accessibility, on the rendered HTML -----------------------------------


def test_each_bundle_is_a_region_bound_to_its_own_heading(
    session, project, client
):
    """`docs/accessibility-acceptance-checklist.md` §2, for this section."""

    _addressed(session, project)

    body = client.get(f"/work/{project.slug}").text
    reading = _reading(session, project)

    for ordinal in range(1, len(reading.bundles) + 1):
        anchor = f"follow-up-bundle-{ordinal}"
        assert f'aria-labelledby="{anchor}-heading"' in body
        assert f'<h4 id="{anchor}-heading">' in body
        assert f'id="{anchor}" tabindex="-1"' in body


def test_the_section_keeps_the_pages_heading_and_focus_rules(
    session, project, client
):
    """One `h1`, no skipped level, one `autofocus`, every table captioned."""

    _addressed(session, project)

    body = client.get(f"/work/{project.slug}").text

    assert body.count("<main>") == 1
    assert body.count("<h1>") == 1
    assert body.count("autofocus") == 1
    levels = [int(level) for level in re.findall(r"<h([1-6])[ >]", body)]
    assert levels[0] == 1
    for previous, level in zip(levels, levels[1:]):
        assert level <= previous + 1, "a heading level is skipped"
    assert body.count('<table class="record"') == body.count("<caption>")


def test_every_state_prints_its_own_words_not_a_colour(
    session, project, client
):
    """`docs/accessibility-acceptance-checklist.md` §1, for this section."""

    made = Chase(session, project)
    made.organization(FIRST, POWER)
    made.promised(FIRST, ALREADY_PAST)
    made.adopt()

    body = client.get(f"/work/{project.slug}").text

    assert "Contact not recorded" in body
    assert "Past the date this bundle names" in body
    # The mark beside the words is decorative and the words carry the meaning.
    assert '<span class="mark" aria-hidden="true">' in body


# --- what the page still writes, and what it does not ----------------------


def test_reading_the_chase_list_writes_no_row_at_all(session, project, client):
    """The list is derived from accepted authority; looking at it is not an act."""

    _cross_source(session, project)
    _plan_every_child(session, project, return_date=RETURNS_AT)
    before = tuple(
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

    for _ in range(3):
        assert client.get(f"/work/{project.slug}").status_code == 200

    session.expire_all()
    assert (
        tuple(
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
        == before
    )


def test_the_reading_is_recorded_through_the_558_event_contract(
    session, project, client
):
    """One presentation event, at the declared cutoff, carrying the digest."""

    _addressed(session, project)
    reading = _reading(session, project)

    with capture_events() as collected:
        assert client.get(f"/work/{project.slug}").status_code == 200

    (event,) = collected.by_family(EventFamily.FOLLOW_UP_READING)
    assert event.occurred_at == NOW
    assert event.payload["reading_identity"] == reading.reading_identity
    assert event.payload["bundle_count"] == len(reading.bundles)
    assert event.payload["retained_outgoing_requests"] == 0
    assert event.payload["project_id"] == project.id
    assert event.payload["principal_subject"] == COORDINATOR.subject
    # Bounded shape only in the labels (#491, #522).
    assert event.metric_labels == {
        "surface": "project_workflow",
        "status": "presented",
    }


def test_the_cutoff_comes_from_the_caller_never_a_clock(
    session, project, client
):
    """Overdue is the bundle's own date against the declared cutoff."""

    made = Chase(session, project)
    made.organization(FIRST, WATER, WATER_CONTACT)
    made.promised(FIRST, ALREADY_PAST)
    made.adopt()

    view = chase_view(_reading(session, project))

    assert view.reading.cutoff == NOW
    assert view.reading.cutoff_date == TODAY
    assert view.bundles[0].overdue is not None
    # And overdue agrees with the band, so the screen states one fact twice
    # rather than two facts that could drift apart.
    for row in view.bundles:
        elapsed = row.band.direction == ELAPSED
        dated = row.bundle.due_date is not None
        assert (row.overdue is not None) == (elapsed and dated)


def test_a_non_member_never_sees_a_bundle(session, project, client):
    """The section is inside the project partition every surface opens (#531)."""

    _addressed(session, project)
    app.dependency_overrides[get_human_principal] = lambda: OUTSIDER

    assert client.get(f"/work/{project.slug}").status_code == 404
