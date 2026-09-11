"""The pilot chase list, and the ways a chase list lies (#425).

The properties under test are the ones the ticket is about, not the ones that
are easy to assert.

The first is the boundary the whole ticket exists to draw: an unresolved
Proposed Delta creates **no** chase item.  Not when it is open, not when a
person deferred it to a date, not when it is the most alarming difference on
the project.  Only a recorded Follow-up Plan — the coordination-needed outcome
of #526, with its question, its responsible party, its return date, its scope
and its evidence — authorizes an external ask.  Two tests hold that line from
both sides, and each of them was confirmed to fail when the guard it protects
is removed.

The second is that a bundle never states an unaccepted value as the record's.
A bundle *may* quote what an arriving source says, because "your minutes say
20 January, our record says 15 December, which holds?" is the actual question;
the quotation is attributed and the accepted position beside it is composed
from the frozen projection.

The third is that "no response" is a fact, not an absence.  It needs a retained
outgoing request and the response boundary that request declared.  Corridor
retains no outgoing correspondence today, so the band is empty in production
and the tests exercise the predicate through the same input port the future
seam will arrive on.

The fourth is ordering.  Bands, each with one declared quantity, and nothing
that could be called a score: a bundle in a higher-consequence band precedes
every bundle below it however small its day count is.

Nothing here reads a clock.  Every date in this module is declared by the test.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from corridor.follow_up_bundles import (
    APPROACHING_COMMITMENT_BAND,
    ASK_ANSWER_OPEN_QUESTION,
    ASK_ANSWER_RETAINED_REQUEST,
    ASK_CONFIRM_ACCEPTED_DATE,
    ASK_RESOLVE_SOURCE_DISCREPANCY,
    AWAITING_ANSWER_BAND,
    BAND_ORDINALS,
    CONSEQUENCE_BANDS,
    CONTACT_RESOLVED,
    CONTACT_UNRESOLVED,
    EXCLUDED_INTERNAL_DATE_FIELDS,
    FOLLOW_UP_RULE_SET,
    OVERDUE_ANSWER_BAND,
    PAST_DUE_COMMITMENT_BAND,
    UNANSWERED_REQUEST_BAND,
    UNBOUNDED_ANSWER_BAND,
    FollowUpBundleRefused,
    RetainedOutgoingRequest,
    SourceReference,
    bundle_reading_payload,
    read_follow_up_bundles,
    read_retained_outgoing_requests,
)
from corridor.models import (
    DeltaDeferral,
    DeltaFollowUpPlan,
    OutgoingRequest,
    Project,
    SourceSegment,
)
from corridor.operating_mode import adopt_project_baseline
from corridor.outgoing_requests import (
    OutgoingRequestRefused,
    record_outgoing_request_response,
    retain_outgoing_request,
)
from corridor.packet_review import (
    FocusedAnswer,
    focused_request,
    packet_request,
    read_review_items,
)
from corridor.principals import HumanPrincipal
from corridor.review_packets import (
    DEFER,
    NEEDS_COORDINATION,
    resolve_review_packet,
)

from access_support import seed_membership
from harness_support import move_accepted_value
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
# Every instant and date below is declared here, never read from a clock.
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
TODAY = NOW.date()
RETURNS_AT = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
OVERDUE_RETURN = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)

# Two dates inside one ISO week (39) and inside the declared 21-day horizon,
# one in a different week (37), and one past the horizon altogether.
SOON = "2026-09-21"
ALSO_SOON = "2026-09-23"
EARLIER_WEEK = "2026-09-10"
BEYOND_HORIZON = "2026-09-28"
ALREADY_PAST = "2026-08-20"

WATER = "City Water"
WATER_CONTACT = "mains@citywater.example"
POWER = "Northline Power"

FIRST = 42
SECOND = 43
THIRD = 44


@pytest.fixture
def project(session: Session) -> Project:
    row = Project(slug=f"chase-{uuid4().hex[:8]}", name="Ridge Road", is_synthetic=True)
    session.add(row)
    session.flush()
    seed_membership(session, row, COORDINATOR)
    return row


class Chase:
    """One adopted project with exactly the accepted values a test needs.

    ``accept`` writes one accepted typed value for one subject and field, each
    at its own revision, exactly as Adopt Baseline does. Nothing here writes a
    Proposed Delta unless a test asks for one, so the default project is a
    quiet accepted record with no incoming source at all.
    """

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
                    accepted_value=self.accepted_value(number),
                    proposed_value=value,
                    baseline_revision=self.revision_of[
                        (subject(number), "committed_date")
                    ],
                )
            ],
            is_complete_enumerative_source=False,
            row_accounting_sealed=False,
        )

    def accepted_value(self, number: int) -> str:
        return self.accepted_dates[number]

    def adopt(self) -> "Chase":
        adopt_project_baseline(
            self.session,
            project_id=self.project.id,
            adopted_by_principal="local:adopter",
            baseline_source_sha256=self.source.document.sha256,
            importer_identity="follow_up_bundle_fixture",
            importer_version="v1",
            idempotency_key=f"adopt:{uuid4().hex[:10]}",
        )
        self.session.expire_all()
        return self


def _chase(session: Session, project: Project) -> Chase:
    """One organization, one contact, two conflicts with accepted dates."""

    made = Chase(session, project)
    made.organization(FIRST, WATER, WATER_CONTACT)
    made.organization(SECOND, WATER, WATER_CONTACT)
    made.promised(FIRST, SOON)
    made.promised(SECOND, ALSO_SOON)
    made.accepted_dates = {FIRST: SOON, SECOND: ALSO_SOON}
    return made


def _cross_source(session: Session, project: Project) -> Chase:
    """Two retained sources answering one accepted Promised For differently.

    A cross-source coordination question is the only shape whose children can
    each be answered coordination-needed, which is how a Follow-up Plan is
    recorded at all today (#528, #526).
    """

    made = _chase(session, project)
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
) -> tuple[int, ...]:
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
    return tuple(child.delta_id for child in item.children)


def _read(session: Session, project: Project, **kwargs):
    return read_follow_up_bundles(
        session, project_id=project.id, as_of=NOW, **kwargs
    )


# --- accepted authority produces the list ----------------------------------


def test_an_accepted_date_that_has_passed_is_one_past_due_chase_item(
    session, project
):
    """The list starts from what the record holds, with no delta anywhere."""

    made = Chase(session, project)
    made.organization(FIRST, WATER, WATER_CONTACT)
    made.promised(FIRST, ALREADY_PAST)
    made.adopt()

    reading = _read(session, project)

    assert len(reading.bundles) == 1
    bundle = reading.bundles[0]
    assert bundle.band == PAST_DUE_COMMITMENT_BAND
    assert bundle.quantity_name == "days_past_accepted_date"
    assert bundle.quantity == (TODAY - date.fromisoformat(ALREADY_PAST)).days
    assert bundle.ask == ASK_CONFIRM_ACCEPTED_DATE
    line = bundle.accepted_position[0]
    assert line.accepted_text == ALREADY_PAST
    assert line.field_name == "Promised for"
    assert line.business_identity == f"U-{FIRST:03d}"
    assert bundle.rule_set == FOLLOW_UP_RULE_SET


def test_several_conflicts_owed_the_same_answer_in_one_week_are_one_bundle(
    session, project
):
    """One recipient, one ask, one compatible due window is one interaction."""

    _chase(session, project).adopt()

    reading = _read(session, project)

    assert len(reading.bundles) == 1
    bundle = reading.bundles[0]
    assert bundle.band == APPROACHING_COMMITMENT_BAND
    assert bundle.due_window == "2026-W39"
    assert bundle.affected_conflicts == (f"U-{FIRST:03d}", f"U-{SECOND:03d}")
    assert {line.accepted_text for line in bundle.accepted_position} == {
        SOON,
        ALSO_SOON,
    }
    # The band's one declared quantity, taken from the item that has least
    # time left, and nothing else.
    assert bundle.quantity_name == "days_until_accepted_date"
    assert bundle.quantity == (date.fromisoformat(SOON) - TODAY).days


def test_one_organization_produces_several_bundles_for_different_windows(
    session, project
):
    """Different deadlines are different interactions, same organization."""

    made = _chase(session, project)
    made.organization(THIRD, WATER, WATER_CONTACT)
    made.promised(THIRD, EARLIER_WEEK)
    made.adopt()

    reading = _read(session, project)

    windows = [bundle.due_window for bundle in reading.bundles]
    assert windows == ["2026-W37", "2026-W39"]
    assert all(
        bundle.recipient.contact_state == CONTACT_UNRESOLVED
        for bundle in reading.bundles
    )


def test_a_date_beyond_the_declared_horizon_raises_nothing(session, project):
    """The horizon is a declared number, not a feeling about what is soon."""

    made = Chase(session, project)
    made.organization(FIRST, WATER, WATER_CONTACT)
    made.promised(FIRST, BEYOND_HORIZON)
    made.adopt()

    assert _read(session, project).bundles == ()
    assert (
        date.fromisoformat(BEYOND_HORIZON) - TODAY
    ).days > _read(session, project).horizon_days


def test_an_internal_action_due_date_is_never_an_external_ask(session, project):
    """ADR-0090 retired the alerts that made an empty internal field a defect."""

    made = Chase(session, project)
    made.organization(FIRST, WATER, WATER_CONTACT)
    made.accept(FIRST, "action_due_date", ALREADY_PAST, is_date=True)
    made.adopt()

    assert "action_due_date" in EXCLUDED_INTERNAL_DATE_FIELDS
    assert _read(session, project).bundles == ()


def test_a_bundle_is_never_suppressed_for_want_of_an_address(session, project):
    """The amended contract: name the role and say the contact is unresolved."""

    made = Chase(session, project)
    made.organization(FIRST, POWER)
    made.promised(FIRST, SOON)
    made.adopt()

    bundle = _read(session, project).bundles[0]

    assert bundle.recipient.contact_state == CONTACT_UNRESOLVED
    assert bundle.recipient.contact_name is None
    assert bundle.recipient.channel is None
    assert bundle.recipient.organization == POWER
    assert "no contact is recorded" in bundle.recipient.sentence()


def test_legacy_v1_preserves_its_prior_recipient_rendering(
    session, project
):
    """Channel participates only where a contact actually resolved to one."""

    made = Chase(session, project)
    made.organization(FIRST, WATER, WATER_CONTACT)
    made.organization(SECOND, WATER)
    made.promised(FIRST, SOON)
    made.promised(SECOND, SOON)
    made.adopt()

    reading = _read(session, project, rule_version="v1")

    states = {bundle.recipient.contact_state for bundle in reading.bundles}
    assert states == {CONTACT_RESOLVED, CONTACT_UNRESOLVED}
    resolved = next(
        bundle
        for bundle in reading.bundles
        if bundle.recipient.contact_state == CONTACT_RESOLVED
    )
    assert resolved.recipient.channel == "email"


# --- the boundary the ticket exists to draw --------------------------------


def test_an_open_proposed_delta_with_no_follow_up_plan_creates_no_chase_item(
    session, project
):
    """The rule, from the side that matters most.

    Two sources disagree about an accepted Promised For, both differences are
    open, and nobody has decided anything. That is the most alarming thing on
    this project and it is still not an external obligation: no person has
    recorded who owes what answer by when.
    """

    _cross_source(session, project)

    reading = _read(session, project)

    assert session.scalar(
        select(func.count()).select_from(DeltaFollowUpPlan).where(
            DeltaFollowUpPlan.project_id == project.id
        )
    ) == 0
    # The accepted dates are still chased; the open differences are not.
    assert all(
        bundle.ask == ASK_CONFIRM_ACCEPTED_DATE for bundle in reading.bundles
    )
    assert all(bundle.quoted_wording == () for bundle in reading.bundles)


def test_a_dated_internal_defer_creates_no_follow_up_plan_and_no_chase_item(
    session, project
):
    """"Not this week" is scheduling. It is not a promise to anybody outside."""

    made = _chase(session, project)
    made.answer(
        document="ucm-2026-09.xlsx",
        family="ucm-workbook",
        revision="2026-09",
        value="2026-12-15",
        number=SECOND,
    )
    made.adopt()
    reading = read_review_items(session, project_id=project.id, as_of=NOW)
    item = next(item for item in reading.items if item.batched)
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
            deferral_reason="waiting on the district review",
        ),
    )
    assert result.status == "saved", result
    session.expire_all()

    assert session.scalar(
        select(func.count()).select_from(DeltaDeferral).where(
            DeltaDeferral.project_id == project.id
        )
    ) == 1
    assert session.scalar(
        select(func.count()).select_from(DeltaFollowUpPlan).where(
            DeltaFollowUpPlan.project_id == project.id
        )
    ) == 0

    bundles = _read(session, project).bundles

    assert [bundle.ask for bundle in bundles] == [ASK_CONFIRM_ACCEPTED_DATE]
    assert all(
        "waiting on the district review" not in bundle.ask_sentence
        for bundle in bundles
    )


# --- a Follow-up Plan is what authorizes an ask ----------------------------


def test_a_recorded_follow_up_plan_authorizes_a_source_discrepancy_bundle(
    session, project
):
    """#526's coordination-needed outcome is the whole authority for the ask."""

    _cross_source(session, project)
    _plan_every_child(session, project, return_date=RETURNS_AT)

    reading = _read(session, project)
    discrepancy = next(
        bundle
        for bundle in reading.bundles
        if bundle.ask == ASK_RESOLVE_SOURCE_DISCREPANCY
    )

    assert discrepancy.band == AWAITING_ANSWER_BAND
    assert discrepancy.due_date == RETURNS_AT.date()
    assert discrepancy.open_questions == (
        "Which date does the utility actually hold to?",
    )
    assert "Source Discrepancy" in discrepancy.ask_sentence
    assert discrepancy.recipient.organization == WATER


def test_a_clarification_bundle_never_states_an_unaccepted_value_as_the_record(
    session, project
):
    """Quote the source; state the record. Never the other way round."""

    _cross_source(session, project)
    _plan_every_child(session, project, return_date=RETURNS_AT)

    bundle = next(
        bundle
        for bundle in _read(session, project).bundles
        if bundle.ask == ASK_RESOLVE_SOURCE_DISCREPANCY
    )

    accepted = {line.accepted_text for line in bundle.accepted_position}
    assert accepted == {SOON}
    quoted = {quote.text for quote in bundle.quoted_wording}
    assert quoted == {"2026-12-15", "2027-01-20"}
    assert not (quoted & accepted)
    for quote in bundle.quoted_wording:
        assert "not a value the Project Record holds" in quote.attribution
        assert quote.reference.identity


def test_a_bundle_citation_opens_the_exact_passage_the_assessment_recorded(
    session, project
):
    """A citation carries the address of its passage, not only an id (#831).

    A Support Assessment names the Source Segments it weighed, in order, so
    the bundle can offer the exact-source view of each of them rather than
    printing an assessment id a coordinator has to chase. The check that it is
    the *right* passage is the one below: the words behind each citation are
    the incoming values this ask exists to reconcile.
    """

    _cross_source(session, project)
    _plan_every_child(session, project, return_date=RETURNS_AT)

    bundle = next(
        bundle
        for bundle in _read(session, project).bundles
        if bundle.ask == ASK_RESOLVE_SOURCE_DISCREPANCY
    )

    assessments = [
        reference
        for reference in bundle.references
        if reference.kind == "support_assessment"
    ]
    assert assessments, bundle.references
    assert {
        session.get(SourceSegment, segment_id).exact_text
        for reference in assessments
        for segment_id in reference.source_segment_ids
    } == {"2026-12-15", "2027-01-20"}


def test_a_proposed_delta_citation_claims_no_passage_because_none_is_recorded(
    session, project
):
    """Nothing joins `proposed_deltas` to a Source Segment, so nothing links.

    The Review screen shows a delta beside a passage by matching on the source
    revision's document, subject and field; that is a correspondence it
    derives, not one the record states. A bundle citation that opened the
    wrong passage would be worse than one that does not open, so the quoted
    wording's reference carries no address at all until a recorded edge exists
    to carry (#831).
    """

    _cross_source(session, project)
    _plan_every_child(session, project, return_date=RETURNS_AT)

    bundle = next(
        bundle
        for bundle in _read(session, project).bundles
        if bundle.ask == ASK_RESOLVE_SOURCE_DISCREPANCY
    )

    quoted = [quote.reference for quote in bundle.quoted_wording]
    assert quoted
    assert all(reference.kind == "proposed_delta" for reference in quoted)
    assert all(reference.source_segment_ids == () for reference in quoted)


def test_the_released_reading_payload_carries_no_passage_address(session, project):
    """The digest proves the rules are deterministic; a screen address is not one.

    `bundle_reading_payload` is a released contract whose `content_sha256` is
    compared across replays. A consumer that wants the passages behind a
    citation resolves them from `support_assessment_sources` itself, which is
    where the assessment recorded them, so nothing here changes a digest that
    every retained reading was measured under.
    """

    _cross_source(session, project)
    _plan_every_child(session, project, return_date=RETURNS_AT)

    payload = bundle_reading_payload(_read(session, project))

    references = [
        reference
        for bundle in payload["bundles"]
        for reference in bundle["references"]
    ]
    assert references
    assert all("source_segment_ids" not in reference for reference in references)


def test_a_bundle_whose_accepted_position_is_not_the_projection_is_refused(
    session, project
):
    """The guard itself, exercised directly on a tampered bundle."""

    from corridor.follow_up_bundles import _validate_bundle
    from corridor.record_projection import read_project_record_as_of_revision
    from corridor.follow_up_bundles import _index_projection

    _chase(session, project).adopt()
    reading = _read(session, project)
    bundle = reading.bundles[0]
    projection = _index_projection(
        read_project_record_as_of_revision(
            session, project.id, reading.accepted_revision_id
        )
    )
    _validate_bundle(bundle, projection)

    tampered = replace(
        bundle,
        accepted_position=(
            replace(bundle.accepted_position[0], accepted_text="2026-12-15"),
        ),
    )

    with pytest.raises(FollowUpBundleRefused, match="never present it as the record"):
        _validate_bundle(tampered, projection)


def test_a_plan_on_an_uncontradicted_difference_asks_the_plain_question(
    session, project
):
    """The ask names what it is: not every plan is a Source Discrepancy.

    One of the two disagreeing differences is made stale by moving the accepted
    value under it, so the surviving difference no longer contradicts anything
    while both plans stay live.
    """

    made = _cross_source(session, project)
    _plan_every_child(session, project, return_date=RETURNS_AT)
    later, _ = made.source.capture(
        fact_type="committed_date",
        value=ALSO_SOON,
        subject_key=subject(FIRST),
    )
    move_accepted_value(session, project, later)
    session.expire_all()

    asks = {bundle.ask for bundle in _read(session, project).bundles}

    assert ASK_ANSWER_OPEN_QUESTION in asks
    assert ASK_RESOLVE_SOURCE_DISCREPANCY not in asks


def test_an_overdue_return_date_outranks_an_answer_that_is_not_late(
    session, project
):
    """The plan's own date, and the exact number of days it has been past."""

    _cross_source(session, project)
    _plan_every_child(session, project, return_date=OVERDUE_RETURN)

    bundle = next(
        bundle
        for bundle in _read(session, project).bundles
        if bundle.ask == ASK_RESOLVE_SOURCE_DISCREPANCY
    )

    assert bundle.band == OVERDUE_ANSWER_BAND
    assert bundle.quantity_name == "days_past_return_date"
    assert bundle.quantity == (TODAY - OVERDUE_RETURN.date()).days


def test_no_movement_is_an_exact_elapsed_time_predicate(session, project):
    """A plan with no return date is measured, never described as stale."""

    _cross_source(session, project)
    _plan_every_child(session, project, return_date=None)

    reading = _read(session, project)
    bundle = next(
        bundle
        for bundle in reading.bundles
        if bundle.ask == ASK_RESOLVE_SOURCE_DISCREPANCY
    )

    assert bundle.band == UNBOUNDED_ANSWER_BAND
    assert bundle.quantity_name == "days_since_plan_recorded"
    # The plans were recorded at the declared instant, so the elapsed count is
    # exactly zero — a number, not an adjective.
    assert bundle.quantity == 0
    assert reading.no_movement_rule == "days_since_plan_recorded"


# --- "no response" is a fact, not an empty inbox ---------------------------


def _first_plan_id(session: Session, project: Project) -> int:
    """The id of a Follow-up Plan a retained outgoing request can advance."""

    return session.scalar(
        select(DeltaFollowUpPlan.id)
        .where(DeltaFollowUpPlan.project_id == project.id)
        .order_by(DeltaFollowUpPlan.id)
    )


def _retain(
    session: Session,
    project: Project,
    *,
    plan_id: int,
    sent_on: date,
    expected_response_by: date,
    subject_keys: tuple[str, ...] = (subject(FIRST),),
    question: str = "Please confirm the relocation date for U-042.",
    idempotency_key: str | None = None,
    sent_bytes: bytes | None = None,
    content_sha256: str | None = None,
) -> OutgoingRequest:
    return retain_outgoing_request(
        session,
        project_id=project.id,
        follow_up_plan_id=plan_id,
        external_organization=WATER,
        question=question,
        covered_subject_keys=subject_keys,
        sent_on=sent_on,
        sent_by_principal="local:coordinator",
        expected_response_by=expected_response_by,
        idempotency_key=idempotency_key or f"req:{uuid4().hex[:12]}",
        sent_bytes=sent_bytes,
        content_sha256=content_sha256,
    )


def test_a_project_that_retained_no_request_reads_empty_and_fires_no_band(
    session, project
):
    """Since #652 Corridor can retain a request, but this project retained none.

    ADR-0090 retired STALE because silence is not evidence, and the honest state
    for a project with nothing on record is still empty: the port returns
    nothing, the count is zero, and no no-response band appears.
    """

    _cross_source(session, project)
    _plan_every_child(session, project, return_date=OVERDUE_RETURN)

    assert (
        read_retained_outgoing_requests(
            session, project_id=project.id, as_of=NOW
        )
        == ()
    )
    reading = _read(session, project)
    assert reading.retained_outgoing_requests == 0
    assert all(
        bundle.band != UNANSWERED_REQUEST_BAND for bundle in reading.bundles
    )


def test_no_production_module_retains_an_outgoing_request_yet():
    """The writer seam has no producer until #652's sending side lands.

    ``outgoing_requests`` is a deliberate seam awaiting the sending side; the
    only callers of its two commands are this module and the chase-screen
    tests. This pin holds that absence exactly, the way the architecture
    ratchets do: the first production caller deletes this test and the
    matching paragraph of ``outgoing_requests``'s docstring in the same change,
    so the seam gains a producer on purpose rather than by accident.
    """

    from pathlib import Path

    from source_scan_support import callers_of

    writers = {"retain_outgoing_request", "record_outgoing_request_response"}
    source_root = Path(__file__).parents[1] / "src" / "corridor"
    callers = sorted(
        f"{path.name}:{lineno} names {writer}"
        for writer, sites in callers_of(writers, (source_root,)).items()
        for path, lines in sites.items()
        if path.name != "outgoing_requests.py" and "migrations" not in path.parts
        for lineno in lines
    )

    assert callers == [], (
        "outgoing_requests has a production producer now; retire this pin and "
        "the docstring paragraph that announces the absence"
    )


def test_a_retained_request_is_read_back_through_the_port_in_its_shape(
    session, project
):
    """The retained record reads back as the port's RetainedOutgoingRequest."""

    _cross_source(session, project)
    _plan_every_child(session, project, return_date=RETURNS_AT)
    plan_id = _first_plan_id(session, project)
    row = _retain(
        session,
        project,
        plan_id=plan_id,
        sent_on=date(2026, 8, 1),
        expected_response_by=date(2026, 8, 15),
        sent_bytes=b"Dear City Water, please confirm the relocation date.",
    )
    # Exact bytes were kept, so the digest still verifies against them.
    assert row.digest_is_valid

    (retained,) = read_retained_outgoing_requests(
        session, project_id=project.id, as_of=NOW
    )

    assert retained.request_identity == str(row.id)
    assert retained.organization == WATER
    assert retained.subject_identities == (subject(FIRST),)
    assert retained.question == "Please confirm the relocation date for U-042."
    assert retained.sent_on == date(2026, 8, 1)
    assert retained.expected_response_by == date(2026, 8, 15)
    assert retained.reference.kind == "outgoing_request"
    assert retained.reference.identity == str(row.id)


def test_a_digest_only_request_needs_no_retained_bytes(session, project):
    """"Whoever sent it, by whatever means" may keep only the digest."""

    _cross_source(session, project)
    _plan_every_child(session, project, return_date=RETURNS_AT)
    plan_id = _first_plan_id(session, project)
    digest = sha256(b"a letter Corridor never kept the bytes of").hexdigest()

    row = _retain(
        session,
        project,
        plan_id=plan_id,
        sent_on=date(2026, 8, 1),
        expected_response_by=date(2026, 8, 15),
        content_sha256=digest,
    )

    assert row.sent_bytes is None
    assert row.content_sha256 == digest
    # With no bytes to compare, the digest stands on its own footing.
    assert row.digest_is_valid
    assert len(
        read_retained_outgoing_requests(session, project_id=project.id, as_of=NOW)
    ) == 1


def test_a_received_response_stops_the_silence_clock(session, project):
    """A recorded response excludes the request as of any cutoff on or after it."""

    _cross_source(session, project)
    _plan_every_child(session, project, return_date=RETURNS_AT)
    plan_id = _first_plan_id(session, project)
    row = _retain(
        session,
        project,
        plan_id=plan_id,
        sent_on=date(2026, 8, 1),
        expected_response_by=date(2026, 8, 15),
        sent_bytes=b"the request",
    )

    # Before any response, the request is returned.
    assert len(
        read_retained_outgoing_requests(session, project_id=project.id, as_of=NOW)
    ) == 1

    record_outgoing_request_response(
        session,
        project_id=project.id,
        request_id=row.id,
        received_on=date(2026, 8, 20),
        recorded_by_principal="local:coordinator",
    )

    # As of a cutoff after the reply, the clock is stopped and it is gone.
    assert (
        read_retained_outgoing_requests(session, project_id=project.id, as_of=NOW)
        == ()
    )
    # As of a cutoff before the reply arrived, it is still outstanding: the
    # exclusion is bound to the reading's own cutoff, never a clock.
    before_reply = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
    assert len(
        read_retained_outgoing_requests(
            session, project_id=project.id, as_of=before_reply
        )
    ) == 1


def test_a_retained_request_past_its_boundary_drives_the_band_through_the_port(
    session, project
):
    """End to end through the real port, not the injection kwarg: the band fires."""

    _cross_source(session, project)
    _plan_every_child(session, project, return_date=RETURNS_AT)
    plan_id = _first_plan_id(session, project)
    _retain(
        session,
        project,
        plan_id=plan_id,
        sent_on=date(2026, 8, 1),
        expected_response_by=date(2026, 8, 15),
        sent_bytes=b"the request",
    )

    reading = _read(session, project)

    assert reading.retained_outgoing_requests == 1
    unanswered = next(
        bundle
        for bundle in reading.bundles
        if bundle.band == UNANSWERED_REQUEST_BAND
    )
    assert unanswered.ask == ASK_ANSWER_RETAINED_REQUEST
    assert unanswered.quantity_name == "days_past_expected_response"
    assert unanswered.quantity == (TODAY - date(2026, 8, 15)).days
    assert unanswered.recipient.organization == WATER


def test_a_recorded_response_removes_the_band_through_the_port(session, project):
    """The same end-to-end path goes quiet once the answer is on record."""

    _cross_source(session, project)
    _plan_every_child(session, project, return_date=RETURNS_AT)
    plan_id = _first_plan_id(session, project)
    row = _retain(
        session,
        project,
        plan_id=plan_id,
        sent_on=date(2026, 8, 1),
        expected_response_by=date(2026, 8, 15),
        sent_bytes=b"the request",
    )
    record_outgoing_request_response(
        session,
        project_id=project.id,
        request_id=row.id,
        received_on=date(2026, 8, 20),
        recorded_by_principal="local:coordinator",
    )

    reading = _read(session, project)

    assert reading.retained_outgoing_requests == 0
    assert all(
        bundle.band != UNANSWERED_REQUEST_BAND for bundle in reading.bundles
    )


def test_a_retained_request_is_written_only_through_the_command(session, project):
    """The write-role boundary is the database's, not a Python convention.

    A direct insert as any role but the record-decision role is refused by the
    guard trigger even though every value is otherwise valid; only the
    SECURITY DEFINER command, which runs as that role, may write the row.
    """

    _cross_source(session, project)
    _plan_every_child(session, project, return_date=RETURNS_AT)
    plan_id = _first_plan_id(session, project)

    with pytest.raises(DBAPIError) as refused:
        with session.begin_nested():
            session.execute(
                text(
                    "insert into outgoing_requests ("
                    "project_id, follow_up_plan_id, external_organization, "
                    "question, covered_subject_keys, content_sha256, sent_on, "
                    "sent_by_principal, expected_response_by, idempotency_key"
                    ") values ("
                    ":project_id, :plan_id, :org, :question, "
                    "cast(:subjects as jsonb), :digest, :sent_on, :principal, "
                    ":boundary, :key)"
                ),
                {
                    "project_id": project.id,
                    "plan_id": plan_id,
                    "org": WATER,
                    "question": "A request nobody may write raw.",
                    "subjects": '["' + subject(FIRST) + '"]',
                    "digest": sha256(b"raw").hexdigest(),
                    "sent_on": date(2026, 8, 1),
                    "principal": "local:coordinator",
                    "boundary": date(2026, 8, 15),
                    "key": f"raw:{uuid4().hex[:10]}",
                },
            )
    assert "written only through its append command" in str(refused.value)

    # The command, running as the record-decision role, writes it.
    row = _retain(
        session,
        project,
        plan_id=plan_id,
        sent_on=date(2026, 8, 1),
        expected_response_by=date(2026, 8, 15),
        sent_bytes=b"the request",
    )
    assert session.get(OutgoingRequest, row.id) is not None


def test_a_replayed_retention_returns_the_row_it_already_wrote(session, project):
    """The command is idempotent under one key, like its sibling receipts."""

    _cross_source(session, project)
    _plan_every_child(session, project, return_date=RETURNS_AT)
    plan_id = _first_plan_id(session, project)

    first = _retain(
        session,
        project,
        plan_id=plan_id,
        sent_on=date(2026, 8, 1),
        expected_response_by=date(2026, 8, 15),
        sent_bytes=b"the request",
        idempotency_key="letter-2026-08-01",
    )
    again = _retain(
        session,
        project,
        plan_id=plan_id,
        sent_on=date(2026, 8, 1),
        expected_response_by=date(2026, 8, 15),
        sent_bytes=b"the request",
        idempotency_key="letter-2026-08-01",
    )

    assert again.id == first.id
    assert len(
        read_retained_outgoing_requests(session, project_id=project.id, as_of=NOW)
    ) == 1


def test_a_boundary_before_the_send_day_is_refused(session, project):
    """Silence before the boundary is not a finding, and the boundary follows the send."""

    _cross_source(session, project)
    _plan_every_child(session, project, return_date=RETURNS_AT)
    plan_id = _first_plan_id(session, project)

    with pytest.raises(DBAPIError):
        with session.begin_nested():
            _retain(
                session,
                project,
                plan_id=plan_id,
                sent_on=date(2026, 8, 15),
                expected_response_by=date(2026, 8, 1),
                sent_bytes=b"the request",
            )


def test_a_retained_request_past_its_declared_boundary_is_a_no_response(
    session, project
):
    """With the request and the boundary both retained, the fact exists."""

    _chase(session, project).adopt()
    request = RetainedOutgoingRequest(
        request_identity="letter-2026-08-01",
        organization=WATER,
        subject_identities=(subject(FIRST),),
        question="Please confirm the relocation date for U-042.",
        sent_on=date(2026, 8, 1),
        expected_response_by=date(2026, 8, 15),
        reference=SourceReference(
            kind="outgoing_request",
            identity="letter-2026-08-01",
            detail="retained outgoing request",
        ),
    )

    reading = _read(session, project, outgoing_requests=[request])
    unanswered = next(
        bundle
        for bundle in reading.bundles
        if bundle.band == UNANSWERED_REQUEST_BAND
    )

    assert unanswered.ask == ASK_ANSWER_RETAINED_REQUEST
    assert unanswered.quantity_name == "days_past_expected_response"
    assert unanswered.quantity == (TODAY - date(2026, 8, 15)).days
    assert unanswered.references[0].identity == "letter-2026-08-01"

    not_yet = replace(request, expected_response_by=date(2026, 12, 1))
    assert all(
        bundle.band != UNANSWERED_REQUEST_BAND
        for bundle in _read(session, project, outgoing_requests=[not_yet]).bundles
    )


def test_a_request_with_no_retained_record_behind_it_is_refused(session, project):
    """An outgoing request without the request is not an input at all."""

    _chase(session, project).adopt()
    hollow = RetainedOutgoingRequest(
        request_identity="",
        organization=WATER,
        subject_identities=(subject(FIRST),),
        question="Did anyone ever ask?",
        sent_on=date(2026, 8, 1),
        expected_response_by=date(2026, 8, 15),
        reference=SourceReference(kind="outgoing_request", identity="", detail=""),
    )

    with pytest.raises(FollowUpBundleRefused, match="not an absence of incoming mail"):
        _read(session, project, outgoing_requests=[hollow])


# --- ordering ---------------------------------------------------------------


def test_a_higher_consequence_band_always_precedes_a_lower_one(session, project):
    """The anti-score property, stated as the case a score would reorder.

    One conflict is one day past its accepted date; another is nineteen days
    from its own. Any scheme that multiplied a band by a day count could put
    the second first. The band ordinal is compared before any number is.
    """

    made = Chase(session, project)
    made.organization(FIRST, WATER, WATER_CONTACT)
    made.promised(FIRST, "2026-09-02")
    made.organization(THIRD, POWER)
    made.promised(THIRD, "2026-09-22")
    made.adopt()

    reading = _read(session, project)

    assert [bundle.band for bundle in reading.bundles] == [
        PAST_DUE_COMMITMENT_BAND,
        APPROACHING_COMMITMENT_BAND,
    ]
    assert reading.bundles[0].quantity == 1
    assert reading.bundles[1].quantity == 19
    ordinals = [BAND_ORDINALS[bundle.band] for bundle in reading.bundles]
    assert ordinals == sorted(ordinals)
    assert [bundle.ordinal for bundle in reading.bundles] == [1, 2]


def test_every_band_declares_exactly_one_quantity_and_why_it_is_a_band():
    """No band may exist without the sentence that justifies its position."""

    assert len({band.ordinal for band in CONSEQUENCE_BANDS}) == len(
        CONSEQUENCE_BANDS
    )
    for band in CONSEQUENCE_BANDS:
        assert band.quantity
        assert band.direction in ("elapsed", "remaining")
        assert len(band.sentence.split()) >= 8


def test_no_bundle_carries_an_opaque_numeric_severity_score(session, project):
    """The thing ADR-0010 abolished must not grow back under another name."""

    _chase(session, project).adopt()
    payload = bundle_reading_payload(_read(session, project))

    banned = ("score", "severity", "priority", "weight", "rank", "points")
    text = repr(payload).casefold()
    assert not [word for word in banned if word in text]
    for bundle in payload["bundles"]:
        # The one number a bundle carries is its own band's declared quantity.
        numbers = [
            key
            for key, value in bundle.items()
            if isinstance(value, int) and key not in ("ordinal", "quantity")
        ]
        assert numbers == []


# --- versioned, replayable, consumable -------------------------------------


def test_the_same_state_and_cutoff_replay_to_the_same_reading(session, project):
    """Trigger, bundling and ordering are one versioned, replayable rule set."""

    _cross_source(session, project)
    _plan_every_child(session, project, return_date=RETURNS_AT)

    first = _read(session, project)
    second = _read(session, project)

    assert first.reading_identity == second.reading_identity
    assert bundle_reading_payload(first) == bundle_reading_payload(second)
    assert first.rule_set == FOLLOW_UP_RULE_SET
    assert first.accepted_revision_id is not None


def test_an_unreleased_rule_version_is_refused_rather_than_guessed(
    session, project
):
    _chase(session, project).adopt()

    with pytest.raises(FollowUpBundleRefused, match="not a follow-up rule version"):
        _read(session, project, rule_version="v99")


def test_a_naive_cutoff_is_refused(session, project):
    _chase(session, project).adopt()

    with pytest.raises(FollowUpBundleRefused, match="aware cutoff"):
        read_follow_up_bundles(
            session, project_id=project.id, as_of=datetime(2026, 9, 3, 12, 0)
        )


def test_the_payload_carries_everything_contact_ready_means(session, project):
    """The amendment's list, checked item by item on one bundle."""

    _cross_source(session, project)
    _plan_every_child(session, project, return_date=RETURNS_AT)
    payload = bundle_reading_payload(_read(session, project))

    bundle = next(
        entry
        for entry in payload["bundles"]
        if entry["ask"] == ASK_RESOLVE_SOURCE_DISCREPANCY
    )
    assert bundle["subject_line"].startswith("Ridge Road: ")
    assert bundle["ask_sentence"]
    assert bundle["accepted_position"]
    assert bundle["affected_conflicts"]
    assert bundle["due_date"] == RETURNS_AT.date().isoformat()
    assert bundle["references"]
    assert bundle["recipient"]["organization"] == WATER
    assert bundle["recipient"]["contact_state"] in (
        CONTACT_RESOLVED,
        CONTACT_UNRESOLVED,
    )
    assert payload["rule_set"] == FOLLOW_UP_RULE_SET
    assert payload["content_sha256"]
    # A structured brief, deliberately not a generated email.
    assert "Dear" not in bundle["ask_sentence"]
