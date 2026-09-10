"""The coordinator reading of one authoritative source revision (#527).

One revised UCM produces dozens of exact, independent, compatible changes and a
handful that fail differently. The properties under test are that the revision
is *one* bounded item with its own accounting, that everything the partition
took out of the batch is still reachable exactly once, and that what the item
shows — accepted value and revision, incoming value, exact source, affected
fields and Utility Conflicts, and the customer artifacts that would change — is
read from stored identities rather than guessed.

#659 adds the other half of what a lone exception needs: a held-out change is
answered on its own, so Needs coordination is offered for it and records a
Follow-up Plan without accepting anything. Those tests are at the foot of this
module.

Nothing here reads a clock. Every cutoff and decision instant is declared.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.analytics import EventFamily, capture_events, default_binding
from corridor.db import engine
from corridor.delta_resolution import live_delta_status
from corridor.follow_up_bundles import (
    ASK_ANSWER_OPEN_QUESTION,
    read_follow_up_bundles,
)
from corridor.models import (
    DeltaDisposition,
    DeltaFollowUpPlan,
    DeltaFollowUpPlanEvidence,
    FactDecision,
    Project,
    ProjectRecordRevision,
)
from corridor.packet_review import (
    ARTIFACT_IMPACT_RULE_VERSION,
    ItemReading,
    BATCH_OUTCOMES,
    CUSTOMER_WORKBOOK,
    FOCUSED_OUTCOMES,
    NOT_A_BATCH,
    NOT_READY_NO_SUPPORT,
    WEEKLY_REPORT,
    FocusedAnswer,
    ReviewScreenRefused,
    emit_packet_opening,
    emit_packet_surfacing,
    focused_request,
    packet_request,
    read_review_items,
)
from corridor.principals import HumanPrincipal
from corridor.project_workflow import FOLLOW_UP, read_project_workflow
from corridor.review_packet_reading import (
    COORDINATION_QUESTION,
    ActionableItem,
    ReviewPacketReadingRefused,
    bind_packet_request,
    read_open_deltas,
    HELD_OUT_APPARENT_REMOVAL,
    HELD_OUT_OWNER_MISMATCH,
    HELD_OUT_POSSIBLE_NEW_CONFLICT,
    HELD_OUT_UNCERTAIN_SCOPE,
    PARTITION_RULE_VERSION,
    SOURCE_REVISION,
)
from corridor.review_packets import (
    APPLY,
    DEFER,
    KEEP_CURRENT,
    PacketChildRequest,
    NEEDS_COORDINATION,
    resolve_review_packet,
    reverse_review_packet,
)

from packet_review_support import (
    Rendition,
    accept_baseline_fact,
    move_accepted_value,
    append_deltas,
    modify,
    new_subject,
    register_baseline,
    register_output_template,
    register_source_row,
    subject,
    support,
)


ALICE = HumanPrincipal("local:alice")
CUTOFF = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
DECIDED_AT = datetime(2026, 9, 3, 11, 0, tzinfo=timezone.utc)
RETURNS_AT = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
TEMPLATE_IDENTITY = "district-ucm-template"
TEMPLATE_VERSION = "v3"


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
def project(session: Session) -> Project:
    row = Project(
        slug=f"packet-review-{uuid4().hex[:8]}",
        name="Packet Review",
        is_synthetic=True,
    )
    session.add(row)
    session.flush()
    return row


class Baseline:
    """The adopted record, and the later revision proposing changes to it."""

    def __init__(
        self,
        session: Session,
        project: Project,
        *,
        rows: int,
        source_urls: dict[int, str] | None = None,
        register_template: bool = True,
    ):
        self.session = session
        self.project = project
        self.adopted = Rendition(session, project, "ucm-2026-08.xlsx")
        self.incoming = Rendition(session, project, "ucm-2026-09.xlsx")
        self.revision_of: dict[tuple[str, str], int] = {}
        self.source_rows: dict[str, object] = {}
        first_revision: int | None = None
        for number in range(1, rows + 1):
            fact, _ = self.adopted.capture(
                fact_type="station_from",
                value=f"{1000 + number}+00",
                subject_key=subject(number),
            )
            revision = accept_baseline_fact(session, project, fact)
            first_revision = first_revision or revision
            self.revision_of[(subject(number), "station_from")] = revision
        assert first_revision is not None
        self.baseline = register_baseline(
            session, project, self.adopted.document, first_revision
        )
        for number in range(1, rows + 1):
            self.source_rows[subject(number)] = register_source_row(
                session,
                project,
                self.baseline,
                row_number=number,
                business_identity=f"UC-{number:03d}",
                external_system_id=f"UCM-{number:05d}",
                source_url=(source_urls or {}).get(
                    number, f"https://records.example.gov/conflict/{number}"
                ),
            )
        if register_template:
            register_output_template(
                session, project, identity=TEMPLATE_IDENTITY, version=TEMPLATE_VERSION
            )

    @property
    def latest_revision(self) -> int:
        return int(
            self.session.scalar(
                select(ProjectRecordRevision.id)
                .where(ProjectRecordRevision.project_id == self.project.id)
                .order_by(ProjectRecordRevision.id.desc())
                .limit(1)
            )
        )

    def restate(self, number: int) -> None:
        """The revision carried this row's value again, unchanged."""

        self.incoming.capture(
            fact_type="station_from",
            value=f"{1000 + number}+00",
            subject_key=subject(number),
        )

    def routine_change(self, number: int, *, supported: bool = True):
        """One exact station change, captured, supported, and proposed."""

        value = f"{2000 + number}+00"
        fact, segment = self.incoming.capture(
            fact_type="station_from", value=value, subject_key=subject(number)
        )
        if supported:
            support(self.session, self.project, fact, segment)
        return modify(
            subject_key=subject(number),
            field_name="station_from",
            accepted_value=f"{1000 + number}+00",
            proposed_value=value,
            baseline_revision=self.revision_of[(subject(number), "station_from")],
        )


def _burst(
    session: Session, project: Project, *, routine: int = 40, unchanged: int = 4
) -> Baseline:
    """One revision carrying many routine changes and one of each exception."""

    baseline = Baseline(session, project, rows=routine + 4 + unchanged)
    values = [baseline.routine_change(number) for number in range(1, routine + 1)]
    for number in range(routine + 5, routine + 5 + unchanged):
        baseline.restate(number)
    # An apparent removal, an organization change, an unresolved scope, and a
    # subject the accepted record does not hold: each fails differently.
    values.append(
        modify(
            subject_key=subject(routine + 1),
            field_name="station_from",
            accepted_value=f"{1000 + routine + 1}+00",
            proposed_value=None,
            baseline_revision=baseline.revision_of[
                (subject(routine + 1), "station_from")
            ],
            change_type="apparent_removal",
        )
    )
    values.append(
        modify(
            subject_key=subject(routine + 2),
            field_name="external_org",
            accepted_value="AT&T Texas",
            proposed_value="AT&T Texas (SWBT)",
            baseline_revision=None,
        )
    )
    values.append(
        modify(
            subject_key=subject(routine + 3),
            field_name="applies_to",
            accepted_value=None,
            proposed_value="UC-041",
            baseline_revision=None,
        )
    )
    values.append(
        new_subject(
            subject_key=subject(routine + 4),
            fields=("station_from",),
            baseline_revision=None,
        )
    )
    append_deltas(
        session,
        project,
        baseline.incoming,
        source_revision="2026-09",
        values=values,
    )
    return baseline


def _batch_item(reading):
    """The one item carrying the revision's routine batch."""

    batches = [
        item
        for item in reading.items
        if item.grouping_key_kind == SOURCE_REVISION
        and item.held_out_reason is None
    ]
    assert len(batches) == 1, [item.item_key for item in reading.items]
    return batches[0]


# --- one bounded item ------------------------------------------------------


def test_a_burst_of_routine_changes_is_one_bounded_item_with_its_counts(
    session: Session, project: Project
):
    """Criterion 1 and the 40-change burst: one item, four countable facts."""

    _burst(session, project, routine=40)

    reading = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    item = _batch_item(reading)

    assert item.child_count == 40
    assert item.ready_count == 40
    assert item.held_out_count == 4
    assert item.unchanged_count == 4
    assert item.grouping_key == "ucm-workbook@2026-09"
    assert item.headline == "ucm-workbook 2026-09"
    assert "Review Packet" not in item.headline


def test_batch_eligibility_is_deterministic_and_versioned(
    session: Session, project: Project
):
    """Criterion 2: two readings of one state rebuild the same items."""

    _burst(session, project, routine=6)

    first = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    second = read_review_items(session, project_id=project.id, as_of=CUTOFF)

    assert [item.item_key for item in first.items] == [
        item.item_key for item in second.items
    ]
    assert [item.offer_identity for item in first.items] == [
        item.offer_identity for item in second.items
    ]
    assert {item.grouping_rule_version for item in first.items} == {
        PARTITION_RULE_VERSION
    }
    assert _batch_item(first).artifact_rule_version == ARTIFACT_IMPACT_RULE_VERSION


def test_a_work_list_item_is_one_type_carrying_its_own_identity(
    session: Session, project: Project
):
    """One item, one type: the partition's fields and the customer's words.

    ADR-0085 gives the reading the partition and this module the project
    language, and that boundary is a rule about *who decides*, not a reason for
    two objects per item. A presentation object that forwarded ordinal, key,
    band and Attention Reasons to a wrapped one told every caller, by its shape,
    that the band belonged somewhere else — so callers reached through the
    wrapper instead, and the seam bought nothing.
    """

    _burst(session, project, routine=3)

    item = _batch_item(read_review_items(session, project_id=project.id, as_of=CUTOFF))

    assert isinstance(item, ActionableItem)
    assert not hasattr(item, "actionable")
    assert {field.name for field in fields(ActionableItem)} <= {
        field.name for field in fields(ItemReading)
    }
    assert item.item_key and item.band and item.grouping_rule_version

    # And the partition still decided every one of them: the reading this was
    # built from says exactly the same, because nothing here re-derives it.
    partition = read_open_deltas(session, project_id=project.id, as_of=CUTOFF)
    decided = next(row for row in partition.items if row.item_key == item.item_key)
    assert tuple(getattr(item, field.name) for field in fields(ActionableItem)) == tuple(
        getattr(decided, field.name) for field in fields(ActionableItem)
    )


def test_a_delta_this_item_does_not_offer_is_refused_by_one_check(
    session: Session, project: Project
):
    """The exactly-once rule is one rule, so it refuses in one way (ADR-0085).

    The screen refused it, the binding refused it again with a second exception
    type, and #526 refuses it in SQL. The SQL half stays — it is the authority —
    but the two Python halves were one rule spelled twice, and a route that
    caught only the screen's name would have turned the binding's refusal into a
    server error.
    """

    _burst(session, project, routine=3)
    reading = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    item = _batch_item(reading)
    outsider = next(
        child.delta_id
        for other in reading.items
        if other.item_key != item.item_key
        for child in other.children
    )

    assert issubclass(ReviewScreenRefused, ReviewPacketReadingRefused)

    with pytest.raises(ReviewPacketReadingRefused) as by_the_screen:
        packet_request(
            reading,
            item,
            outcome=APPLY,
            principal=ALICE,
            decided_at=DECIDED_AT,
            delta_ids=[item.children[0].delta_id, outsider],
        )

    # Reached directly, the binding refuses it in the same words and with the
    # very name the review route catches.
    with pytest.raises(ReviewScreenRefused) as by_the_binding:
        bind_packet_request(
            reading.reading,
            item,
            principal=ALICE,
            idempotency_key="offered-check",
            decided_at=DECIDED_AT,
            children=(
                PacketChildRequest(
                    delta_id=outsider,
                    outcome=KEEP_CURRENT,
                    observed_source_revision="2026-09",
                ),
            ),
        )
    assert "not offered by this item" in str(by_the_screen.value)
    assert isinstance(by_the_binding.value, ReviewPacketReadingRefused)


def test_each_exception_is_its_own_focused_item(session: Session, project: Project):
    """Criterion 3: what fails differently is decided on its own."""

    _burst(session, project, routine=3)

    reading = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    held_out = {
        item.held_out_reason: item
        for item in reading.items
        if item.held_out_reason is not None
    }

    assert set(held_out) == {
        HELD_OUT_APPARENT_REMOVAL,
        HELD_OUT_OWNER_MISMATCH,
        HELD_OUT_UNCERTAIN_SCOPE,
        HELD_OUT_POSSIBLE_NEW_CONFLICT,
    }
    for item in held_out.values():
        assert item.child_count == 1
        assert item.decidable is True
        assert item.headline != "ucm-workbook 2026-09"


def test_conflicting_sources_become_one_coordination_question(
    session: Session, project: Project
):
    """Two retained revisions answering one field differently is not a batch."""

    baseline = Baseline(session, project, rows=1)
    append_deltas(
        session,
        project,
        baseline.incoming,
        source_revision="2026-09",
        values=[baseline.routine_change(1)],
    )
    later = Rendition(session, project, "utility-email-2026-09.xlsx")
    fact, segment = later.capture(
        fact_type="station_from", value="3000+00", subject_key=subject(1)
    )
    support(session, project, fact, segment)
    append_deltas(
        session,
        project,
        later,
        source_revision="2026-09-email",
        values=[
            modify(
                subject_key=subject(1),
                field_name="station_from",
                accepted_value="1001+00",
                proposed_value="3000+00",
                baseline_revision=baseline.revision_of[(subject(1), "station_from")],
            )
        ],
    )

    reading = read_review_items(session, project_id=project.id, as_of=CUTOFF)

    (question,) = [
        item
        for item in reading.items
        if item.grouping_key_kind == COORDINATION_QUESTION
    ]
    assert question.child_count == 2
    # It is decided, but child by child against both sources (#528), never as
    # one batch outcome over the pair.
    assert question.batched is False
    assert question.focused is True
    assert question.decidable is True
    assert "sources disagree" in question.headline


def test_every_open_delta_is_offered_by_exactly_one_item(
    session: Session, project: Project
):
    """Criterion 4: the partition is surfaced, never re-derived, never doubled."""

    _burst(session, project, routine=5)

    reading = read_review_items(session, project_id=project.id, as_of=CUTOFF)

    offered = [
        child.delta_id for item in reading.items for child in item.children
    ]
    assert sorted(offered) == sorted(reading.reading.actionable_delta_ids)
    assert len(offered) == len(set(offered))


def test_a_held_out_sibling_is_named_read_only_and_points_at_its_own_item(
    session: Session, project: Project
):
    """A reference explains the accounting; it never grows a second control."""

    _burst(session, project, routine=5)

    reading = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    item = _batch_item(reading)

    assert {child.delta_id for child in item.held_out_children}.isdisjoint(
        {child.delta_id for child in item.children}
    )
    for sibling in item.held_out_children:
        assert sibling.held_out_reason
        owner = reading.item(sibling.held_out_item_key)
        assert owner is not None
        assert sibling.delta_id in {child.delta_id for child in owner.children}


def test_a_single_delta_revision_still_gets_a_safe_one_delta_item(
    session: Session, project: Project
):
    """The fallback: one exact change is one item, not nothing."""

    baseline = Baseline(session, project, rows=1)
    append_deltas(
        session,
        project,
        baseline.incoming,
        source_revision="2026-09",
        values=[baseline.routine_change(1)],
    )

    reading = read_review_items(session, project_id=project.id, as_of=CUTOFF)

    assert len(reading.items) == 1
    assert reading.items[0].child_count == 1
    assert reading.items[0].decidable is True


# --- what the item shows ---------------------------------------------------


def test_the_item_shows_accepted_value_revision_incoming_value_and_exact_source(
    session: Session, project: Project
):
    """Criterion 5, the part a coordinator reads before deciding anything."""

    baseline = Baseline(session, project, rows=1)
    append_deltas(
        session,
        project,
        baseline.incoming,
        source_revision="2026-09",
        values=[baseline.routine_change(1)],
    )

    reading = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    (child,) = reading.items[0].children

    assert child.accepted_value == "1001+00"
    assert child.accepted_revision_id == baseline.revision_of[
        (subject(1), "station_from")
    ]
    assert child.incoming_value == "2001+00"
    assert child.field_name == "From station"
    assert child.subject_name == "UC-001 (Utility Conflicts row 1)"
    assert child.source is not None
    assert child.source.document_filename == "ucm-2026-09.xlsx"
    assert child.source.location == "sheet Utility Conflicts, cell C1"
    assert child.source.exact_text == "2001+00"


def test_affected_fields_and_utility_conflicts_are_listed_on_the_item(
    session: Session, project: Project
):
    """Children stay inspectable by Utility Conflict and by field."""

    baseline = Baseline(session, project, rows=2)
    append_deltas(
        session,
        project,
        baseline.incoming,
        source_revision="2026-09",
        values=[baseline.routine_change(1), baseline.routine_change(2)],
    )

    item = _batch_item(read_review_items(session, project_id=project.id, as_of=CUTOFF))

    assert item.subject_names == (
        "UC-001 (Utility Conflicts row 1)",
        "UC-002 (Utility Conflicts row 2)",
    )
    assert item.field_names == ("From station",)


def test_customer_artifacts_name_the_registered_template_and_the_weekly_report(
    session: Session, project: Project
):
    """Criterion 5's last clause, answered from what the project registered."""

    baseline = Baseline(session, project, rows=1)
    append_deltas(
        session,
        project,
        baseline.incoming,
        source_revision="2026-09",
        values=[baseline.routine_change(1)],
    )

    item = _batch_item(read_review_items(session, project_id=project.id, as_of=CUTOFF))

    assert item.customer_artifacts == (
        f"{CUSTOMER_WORKBOOK} ({TEMPLATE_IDENTITY} {TEMPLATE_VERSION})",
        WEEKLY_REPORT,
    )


def test_an_external_identifier_and_source_url_are_shown_and_deep_linked(
    session: Session, project: Project
):
    """The 2026-09-02 amendment: read-only deep links, no map required."""

    baseline = Baseline(session, project, rows=1)
    append_deltas(
        session,
        project,
        baseline.incoming,
        source_revision="2026-09",
        values=[baseline.routine_change(1)],
    )

    (child,) = read_review_items(
        session, project_id=project.id, as_of=CUTOFF
    ).items[0].children

    links = {link.role: link for link in child.external_links}
    assert links["external_system_id"].text == "UCM-00001"
    assert links["external_system_id"].href is None
    assert links["source_url"].href == "https://records.example.gov/conflict/1"


def test_an_unfollowable_source_url_is_shown_as_text_rather_than_linked(
    session: Session, project: Project
):
    """A stored value that is not a web address never becomes a link."""

    baseline = Baseline(
        session,
        project,
        rows=1,
        source_urls={1: "file:///N:/utilities/UC-001.pdf"},
    )
    append_deltas(
        session,
        project,
        baseline.incoming,
        source_revision="2026-09",
        values=[baseline.routine_change(1)],
    )

    (child,) = read_review_items(
        session, project_id=project.id, as_of=CUTOFF
    ).items[0].children

    links = {link.role: link for link in child.external_links}
    assert links["source_url"].text == "file:///N:/utilities/UC-001.pdf"
    assert links["source_url"].href is None


def test_a_child_without_recorded_support_is_listed_and_not_ready(
    session: Session, project: Project
):
    """#519 would refuse an Apply with no support, so the item says so first."""

    baseline = Baseline(session, project, rows=2)
    append_deltas(
        session,
        project,
        baseline.incoming,
        source_revision="2026-09",
        values=[
            baseline.routine_change(1),
            baseline.routine_change(2, supported=False),
        ],
    )

    item = _batch_item(read_review_items(session, project_id=project.id, as_of=CUTOFF))

    assert item.child_count == 2
    assert item.ready_count == 1
    unsupported = [child for child in item.children if not child.ready]
    assert [child.not_ready_reason for child in unsupported] == [NOT_READY_NO_SUPPORT]


# --- the act -------------------------------------------------------------


def test_the_saved_act_names_only_the_selected_children(
    session: Session, project: Project
):
    """Criterion 6: an unselected child stays open and is never implied."""

    baseline = Baseline(session, project, rows=3)
    append_deltas(
        session,
        project,
        baseline.incoming,
        source_revision="2026-09",
        values=[baseline.routine_change(number) for number in (1, 2, 3)],
    )
    reading = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    item = _batch_item(reading)
    chosen = [item.children[0].delta_id, item.children[2].delta_id]

    request = packet_request(
        reading,
        item,
        outcome=APPLY,
        principal=ALICE,
        decided_at=DECIDED_AT,
        delta_ids=chosen,
    )

    assert [child.delta_id for child in request.children] == chosen
    assert request.grouping_rule_version == PARTITION_RULE_VERSION
    assert request.observed_accepted_revision_id == reading.accepted_revision_id
    assert all(child.record_effects for child in request.children)
    assert all(child.support_assessment_ids for child in request.children)


def test_a_child_outside_the_item_is_refused_before_any_write(
    session: Session, project: Project
):
    """Exactly-once holds of the act, not only of the reading."""

    _burst(session, project, routine=3)
    reading = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    item = _batch_item(reading)
    outsider = next(
        child.delta_id
        for other in reading.items
        if other.item_key != item.item_key
        for child in other.children
    )

    with pytest.raises(Exception) as refusal:
        packet_request(
            reading,
            item,
            outcome=APPLY,
            principal=ALICE,
            decided_at=DECIDED_AT,
            delta_ids=[item.children[0].delta_id, outsider],
        )

    assert "not offered by this item" in str(refusal.value)


def test_a_coordination_question_offers_no_batch_act_here(
    session: Session, project: Project
):
    """No delta ever carries two decision controls (ADR-0085)."""

    baseline = Baseline(session, project, rows=1)
    append_deltas(
        session,
        project,
        baseline.incoming,
        source_revision="2026-09",
        values=[baseline.routine_change(1)],
    )
    later = Rendition(session, project, "utility-email-2026-09.xlsx")
    fact, segment = later.capture(
        fact_type="station_from", value="3000+00", subject_key=subject(1)
    )
    support(session, project, fact, segment)
    append_deltas(
        session,
        project,
        later,
        source_revision="2026-09-email",
        values=[
            modify(
                subject_key=subject(1),
                field_name="station_from",
                accepted_value="1001+00",
                proposed_value="3000+00",
                baseline_revision=baseline.revision_of[(subject(1), "station_from")],
            )
        ],
    )
    reading = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    (question,) = [
        item
        for item in reading.items
        if item.grouping_key_kind == COORDINATION_QUESTION
    ]

    with pytest.raises(ReviewScreenRefused):
        packet_request(
            reading,
            question,
            outcome=APPLY,
            principal=ALICE,
            decided_at=DECIDED_AT,
            delta_ids=[question.children[0].delta_id],
        )


def test_applying_the_batch_writes_one_revision_through_the_packet_command(
    session: Session, project: Project
):
    """Criterion 8: Save is #526's, and it commits one revision for the batch."""

    baseline = Baseline(session, project, rows=3)
    append_deltas(
        session,
        project,
        baseline.incoming,
        source_revision="2026-09",
        values=[baseline.routine_change(number) for number in (1, 2, 3)],
    )
    reading = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    item = _batch_item(reading)

    result = resolve_review_packet(
        session,
        packet_request(
            reading,
            item,
            outcome=APPLY,
            principal=ALICE,
            decided_at=DECIDED_AT,
            delta_ids=[child.delta_id for child in item.children],
        ),
    )

    assert result.status == "saved", result.refusals
    assert result.revision_id is not None
    assert len(result.children) == 3
    effective = session.scalars(
        select(FactDecision.fact_id).where(
            FactDecision.revision_id == result.revision_id
        )
    ).all()
    assert len(effective) == 3

    after = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    assert after.items == ()


def test_deferring_the_batch_writes_no_project_record_revision(
    session: Session, project: Project
):
    """Criterion 7's secondary act: Defer is scheduling (ADR-0084)."""

    baseline = Baseline(session, project, rows=2)
    append_deltas(
        session,
        project,
        baseline.incoming,
        source_revision="2026-09",
        values=[baseline.routine_change(1), baseline.routine_change(2)],
    )
    reading = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    item = _batch_item(reading)

    result = resolve_review_packet(
        session,
        packet_request(
            reading,
            item,
            outcome=DEFER,
            principal=ALICE,
            decided_at=DECIDED_AT,
            delta_ids=[child.delta_id for child in item.children],
            deferred_until=RETURNS_AT,
        ),
    )

    assert result.status == "saved", result.refusals
    assert result.revision_id is None
    later = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    assert later.items == ()
    returned = read_review_items(
        session, project_id=project.id, as_of=RETURNS_AT + timedelta(days=1)
    )
    assert _batch_item(returned).child_count == 2


def test_a_defer_without_a_date_is_refused_before_anything_is_read(
    session: Session, project: Project
):
    baseline = Baseline(session, project, rows=1)
    append_deltas(
        session,
        project,
        baseline.incoming,
        source_revision="2026-09",
        values=[baseline.routine_change(1)],
    )
    reading = read_review_items(session, project_id=project.id, as_of=CUTOFF)

    with pytest.raises(ReviewScreenRefused):
        packet_request(
            reading,
            reading.items[0],
            outcome=DEFER,
            principal=ALICE,
            decided_at=DECIDED_AT,
            delta_ids=[reading.items[0].children[0].delta_id],
        )


def test_a_stale_child_refuses_the_whole_packet_and_preserves_selections(
    session: Session, project: Project
):
    """Criterion 8's second half: no partial write, nothing the person typed lost."""

    baseline = Baseline(session, project, rows=2)
    append_deltas(
        session,
        project,
        baseline.incoming,
        source_revision="2026-09",
        values=[baseline.routine_change(1), baseline.routine_change(2)],
    )
    reading = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    item = _batch_item(reading)
    request = packet_request(
        reading,
        item,
        outcome=APPLY,
        principal=ALICE,
        decided_at=DECIDED_AT,
        delta_ids=[child.delta_id for child in item.children],
    )
    # The accepted record moves after the reading was taken.
    moved, _ = baseline.adopted.capture(
        fact_type="station_from", value="9999+00", subject_key=subject(1)
    )
    move_accepted_value(session, project, moved)

    result = resolve_review_packet(session, request)

    assert result.status == "refused"
    assert result.revision_id is None
    assert [child.delta_id for child in result.preserved_selections] == [
        child.delta_id for child in request.children
    ]
    assert result.refusals[0].reason == "stale_accepted_revision"
    assert result.refusals[0].current_accepted_revision_id is not None


def test_a_resubmitted_save_never_decides_the_same_delta_twice(
    session: Session, project: Project
):
    """A double-submitted form writes one act; the second is refused, not doubled."""

    baseline = Baseline(session, project, rows=1)
    append_deltas(
        session,
        project,
        baseline.incoming,
        source_revision="2026-09",
        values=[baseline.routine_change(1)],
    )
    reading = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    item = reading.items[0]
    delta_ids = [item.children[0].delta_id]

    first = resolve_review_packet(
        session,
        packet_request(
            reading,
            item,
            outcome=KEEP_CURRENT,
            principal=ALICE,
            decided_at=DECIDED_AT,
            delta_ids=delta_ids,
        ),
    )
    second = resolve_review_packet(
        session,
        packet_request(
            reading,
            item,
            outcome=KEEP_CURRENT,
            principal=ALICE,
            decided_at=DECIDED_AT,
            delta_ids=delta_ids,
        ),
    )

    assert first.status == "saved", first.refusals
    assert second.status == "refused"
    assert second.receipt_id is None
    assert second.revision_id is None
    assert second.refusals[0].reason == "already_resolved"


def test_a_defer_to_a_later_date_is_a_different_act(
    session: Session, project: Project
):
    """The return date is part of the act, so a re-Defer is not a replay."""

    baseline = Baseline(session, project, rows=1)
    append_deltas(
        session,
        project,
        baseline.incoming,
        source_revision="2026-09",
        values=[baseline.routine_change(1)],
    )
    reading = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    item = reading.items[0]

    first = packet_request(
        reading,
        item,
        outcome=DEFER,
        principal=ALICE,
        decided_at=DECIDED_AT,
        delta_ids=[item.children[0].delta_id],
        deferred_until=RETURNS_AT,
    )
    later = packet_request(
        reading,
        item,
        outcome=DEFER,
        principal=ALICE,
        decided_at=DECIDED_AT,
        delta_ids=[item.children[0].delta_id],
        deferred_until=RETURNS_AT + timedelta(days=14),
    )

    assert first.idempotency_key != later.idempotency_key


# --- the versioned event contract (#558) ----------------------------------


def test_surfacing_opening_child_decision_and_save_are_all_emitted(
    session: Session, project: Project
):
    """The amendment's second criterion, with the binding each event requires."""

    baseline = Baseline(session, project, rows=2)
    append_deltas(
        session,
        project,
        baseline.incoming,
        source_revision="2026-09",
        values=[baseline.routine_change(1), baseline.routine_change(2)],
    )
    reading = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    item = _batch_item(reading)
    request = packet_request(
        reading,
        item,
        outcome=APPLY,
        principal=ALICE,
        decided_at=DECIDED_AT,
        delta_ids=[child.delta_id for child in item.children],
    )

    with capture_events() as events:
        emit_packet_surfacing(reading, item)
        emit_packet_opening(reading, item)
        resolve_review_packet(session, request)

    families = [event.family for event in events.events]
    assert families.count(EventFamily.PACKET_SURFACING) == 1
    assert families.count(EventFamily.PACKET_OPENING) == 1
    assert families.count(EventFamily.CHILD_DECISION) == 2
    assert families.count(EventFamily.PACKET_SAVE) == 1
    for event in events.events:
        assert event.binding.packetizer_rules_version == PARTITION_RULE_VERSION
        assert event.binding.code_revision
        assert event.binding.product_revision
        assert event.binding.template_identity
        assert event.binding.mapping_identity
        assert event.version
    surfacing = events.by_family(EventFamily.PACKET_SURFACING)[0]
    assert surfacing.payload["child_count"] == 2
    assert surfacing.payload["unchanged_count"] == 0
    assert surfacing.payload["artifact_rule_version"] == ARTIFACT_IMPACT_RULE_VERSION
    assert "project_id" not in surfacing.metric_labels


def test_the_screen_binding_never_overwrites_a_supplied_deployment_binding(
    session: Session, project: Project
):
    baseline = Baseline(session, project, rows=1)
    append_deltas(
        session,
        project,
        baseline.incoming,
        source_revision="2026-09",
        values=[baseline.routine_change(1)],
    )
    reading = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    binding = default_binding(
        code_revision="git:abc123", enabled_feature_flags=("packet_review",)
    )

    with capture_events() as events:
        emit_packet_surfacing(reading, reading.items[0], binding=binding)

    (event,) = events.events
    assert event.binding.code_revision == "git:abc123"
    assert event.binding.enabled_feature_flags == ("packet_review",)
    assert event.binding.packetizer_rules_version == PARTITION_RULE_VERSION


def test_a_project_with_no_registered_template_is_promised_no_workbook(
    session: Session, project: Project
):
    """The artifact rule reads registrations; it never assumes an artifact."""

    baseline = Baseline(session, project, rows=1, register_template=False)
    append_deltas(
        session,
        project,
        baseline.incoming,
        source_revision="2026-09",
        values=[baseline.routine_change(1)],
    )

    item = read_review_items(session, project_id=project.id, as_of=CUTOFF).items[0]

    assert item.customer_artifacts == (WEEKLY_REPORT,)


# --- Needs coordination on a focused single-source item (#659) -------------
#
# A held-out change is one delta a coordinator has to answer alone, and the
# question it raises is very often not "which value is right" but "who can tell
# me?". Before #659 the only answers offered for one were Apply, Keep current,
# and a dated Defer, so the ask a coordinator actually needed had to be spelled
# as a date and every Follow-up Plan the product could create was a Source
# Discrepancy raised from a cross-source contradiction. These tests fix the four
# single-source shapes the partition already holds out.


HELD_OUT_REASONS = (
    HELD_OUT_APPARENT_REMOVAL,
    HELD_OUT_POSSIBLE_NEW_CONFLICT,
    HELD_OUT_OWNER_MISMATCH,
    HELD_OUT_UNCERTAIN_SCOPE,
)


def _held_out(reading, reason: str):
    """The one item the partition held out of the batch for this reason."""

    (item,) = [row for row in reading.items if row.held_out_reason == reason]
    return item


def _coordination(reading, item, *, question: str, **overrides):
    """The focused act recording one Follow-up Plan on this item's one change."""

    (child,) = item.children
    answer = FocusedAnswer(
        delta_id=child.delta_id,
        outcome=NEEDS_COORDINATION,
        question=question,
        responsible_organization="AT&T Texas",
        return_date=RETURNS_AT,
        **overrides,
    )
    return focused_request(
        reading, item, principal=ALICE, decided_at=DECIDED_AT, answers=(answer,)
    )


@pytest.mark.parametrize("reason", HELD_OUT_REASONS)
def test_a_held_out_change_is_answered_child_by_child_not_as_a_batch(
    session: Session, project: Project, reason: str
):
    """A one-delta hold-out is a focused item, so it carries every outcome."""

    _burst(session, project, routine=3)

    reading = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    item = _held_out(reading, reason)

    assert item.focused is True
    assert item.batched is False
    assert item.child_count == 1
    assert NEEDS_COORDINATION in FOCUSED_OUTCOMES
    # The batch that carries the revision's routine changes is unchanged.
    assert _batch_item(reading).batched is True


def test_needs_coordination_is_never_offered_over_a_batch(
    session: Session, project: Project
):
    """Forty unrelated routine changes can never be planned away in one act."""

    _burst(session, project, routine=40)
    reading = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    batch = _batch_item(reading)

    assert NEEDS_COORDINATION not in BATCH_OUTCOMES
    with pytest.raises(ReviewScreenRefused) as refused:
        packet_request(
            reading,
            batch,
            outcome=NEEDS_COORDINATION,
            principal=ALICE,
            decided_at=DECIDED_AT,
            delta_ids=[child.delta_id for child in batch.children],
        )
    assert "batch decisions" in str(refused.value)


@pytest.mark.parametrize("reason", HELD_OUT_REASONS)
def test_a_held_out_change_is_not_reachable_through_the_batch_act(
    session: Session, project: Project, reason: str
):
    """The one route to a held-out change is the focused act, so there is one."""

    _burst(session, project, routine=3)
    reading = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    item = _held_out(reading, reason)

    with pytest.raises(ReviewScreenRefused) as refused:
        packet_request(
            reading,
            item,
            outcome=KEEP_CURRENT,
            principal=ALICE,
            decided_at=DECIDED_AT,
            delta_ids=[child.delta_id for child in item.children],
        )
    assert str(refused.value) == NOT_A_BATCH


@pytest.mark.parametrize("reason", HELD_OUT_REASONS)
def test_needs_coordination_plans_the_question_and_accepts_nothing(
    session: Session, project: Project, reason: str
):
    """The plan records what the ask needs; the record and the delta do not move."""

    _burst(session, project, routine=3)
    reading = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    item = _held_out(reading, reason)
    (child,) = item.children
    before = session.scalars(
        select(ProjectRecordRevision.id).where(
            ProjectRecordRevision.project_id == project.id
        )
    ).all()
    decisions_before = session.scalars(
        select(FactDecision.id).where(FactDecision.project_id == project.id)
    ).all()

    act = _coordination(reading, item, question=f"Who can confirm {reason}?")
    result = resolve_review_packet(session, act)

    assert result.status == "saved"
    (recorded,) = result.children
    assert recorded.outcome == NEEDS_COORDINATION
    assert recorded.follow_up_plan_id is not None
    assert recorded.decision_id is None

    (plan,) = session.scalars(
        select(DeltaFollowUpPlan).where(DeltaFollowUpPlan.project_id == project.id)
    ).all()
    assert plan.delta_id == child.delta_id
    assert plan.open_question == f"Who can confirm {reason}?"
    assert plan.responsible_organization == "AT&T Texas"
    assert plan.return_date == RETURNS_AT
    assert plan.affected_scope["subject_identity"] == child.subject_identity
    assert plan.affected_scope["field"] == child.field
    assert plan.revision_id == result.revision_id

    # No accepted value: the packet's revision carries the plan and nothing
    # effective, and the delta is still open and still offered.
    assert not session.scalars(
        select(DeltaDisposition).where(
            DeltaDisposition.project_id == project.id,
            DeltaDisposition.delta_id == child.delta_id,
        )
    ).all()
    assert live_delta_status(session, child.delta_id) == "open"
    assert (
        session.scalars(
            select(FactDecision.id).where(FactDecision.project_id == project.id)
        ).all()
        == decisions_before
    )
    assert result.revision_id not in before
    after = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    assert child.delta_id in after.reading.actionable_delta_ids
    current_child = next(value for current_item in after.items for value in current_item.children
                         if value.delta_id == child.delta_id)
    (visible_plan,) = current_child.follow_up_plans
    assert (visible_plan.plan_id, visible_plan.delta_id, visible_plan.revision_id) == (plan.id, child.delta_id, result.revision_id)
    assert visible_plan.open_question == f"Who can confirm {reason}?"
    assert visible_plan.responsible_organization == "AT&T Texas"
    assert visible_plan.responsible_principal is None
    assert visible_plan.return_date == RETURNS_AT
    assert visible_plan.recorded_by == ALICE.subject
    assert visible_plan.recorded_at == DECIDED_AT
    assert current_child.accepted_value == child.accepted_value
    assert after.follow_up_plans == (visible_plan,)


def test_the_plan_cites_its_evidence_and_leaves_that_value_unaccepted(
    session: Session, project: Project
):
    """An owner mismatch has a captured, supported incoming value; the plan cites it.

    This is the shape where "no accepted value" is a real guard rather than a
    vacuous one: there *is* a Source Fact this act could have made effective,
    and Needs coordination must not make it effective. The four-way parametrized
    test above cannot prove that, because the partition's other hold-out shapes
    carry no captured incoming fact at all.
    """

    baseline = Baseline(session, project, rows=1)
    fact, segment = baseline.incoming.capture(
        fact_type="external_org", value="AT&T Texas (SWBT)", subject_key=subject(1)
    )
    assessment = support(session, project, fact, segment)
    append_deltas(
        session,
        project,
        baseline.incoming,
        source_revision="2026-09",
        values=[
            modify(
                subject_key=subject(1),
                field_name="external_org",
                accepted_value="AT&T Texas",
                proposed_value="AT&T Texas (SWBT)",
                baseline_revision=None,
            )
        ],
    )
    reading = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    item = _held_out(reading, HELD_OUT_OWNER_MISMATCH)

    act = _coordination(
        reading,
        item,
        question="Does this correct the name, or did ownership move?",
    )
    resolve_review_packet(session, act)

    (plan,) = session.scalars(
        select(DeltaFollowUpPlan).where(DeltaFollowUpPlan.project_id == project.id)
    ).all()
    cited = session.scalars(
        select(DeltaFollowUpPlanEvidence.support_assessment_id).where(
            DeltaFollowUpPlanEvidence.plan_id == plan.id
        )
    ).all()
    assert cited == [assessment.id]
    reread = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    (visible_plan,) = reread.follow_up_plans
    assert visible_plan.plan_id == plan.id
    assert visible_plan.support_assessment_ids == (assessment.id,)
    assert visible_plan.source_segment_ids == (segment.id,)
    # The supported value stays unaccepted, and the change stays in Review.
    (child,) = item.children
    assert child.incoming_fact_id == fact.id
    assert not session.scalars(
        select(FactDecision.id).where(
            FactDecision.project_id == project.id, FactDecision.fact_id == fact.id
        )
    ).all()
    assert not session.scalars(
        select(DeltaDisposition).where(
            DeltaDisposition.project_id == project.id,
            DeltaDisposition.delta_id == child.delta_id,
        )
    ).all()
    assert live_delta_status(session, child.delta_id) == "open"


@pytest.mark.parametrize("reason", HELD_OUT_REASONS)
def test_a_single_source_plan_reaches_the_chase_list_and_the_week(
    session: Session, project: Project, reason: str
):
    """#425's bundle and #536's Follow-up section both read the plan, not a delta."""

    _burst(session, project, routine=3)
    reading = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    item = _held_out(reading, reason)
    (child,) = item.children

    resolve_review_packet(
        session, _coordination(reading, item, question="Who owes us this answer?")
    )

    chase = read_follow_up_bundles(session, project_id=project.id, as_of=CUTOFF)
    (bundle,) = chase.bundles
    (plan,) = session.scalars(
        select(DeltaFollowUpPlan).where(DeltaFollowUpPlan.project_id == project.id)
    ).all()
    assert bundle.item_identities == (f"follow_up_plan:{plan.id}",)
    assert bundle.open_questions == ("Who owes us this answer?",)
    # One conflict, named by whatever identity the record actually printed for
    # it: the customer's own business identity where a baseline row registered
    # one, and the subject key where the source proposed a conflict the record
    # does not hold yet.
    assert len(bundle.affected_conflicts) == 1
    assert bundle.affected_conflicts[0] in (
        child.subject_identity,
        child.subject_name,
        child.subject_name.split(" (")[0],
    )
    # Nothing contradicts anything here, so the ask is the plain open question
    # rather than #425's Source Discrepancy.
    assert bundle.ask == ASK_ANSWER_OPEN_QUESTION
    assert bundle.recipient.organization == "AT&T Texas"
    assert bundle.due_date == RETURNS_AT.date()

    week = read_project_workflow(session, project_id=project.id, as_of=CUTOFF)
    (need,) = week.follow_up
    assert need.delta_id == child.delta_id
    assert need.open_question == "Who owes us this answer?"
    assert week.section(FOLLOW_UP).outstanding == 1
    # The change is described under Follow-up rather than counted as still
    # waiting on the coordinator's own judgement.
    assert child.delta_id not in week.changes_awaiting_decision


@pytest.mark.parametrize("reason", HELD_OUT_REASONS)
def test_undo_removes_the_plan_and_returns_the_change_to_review(
    session: Session, project: Project, reason: str
):
    """An act that never stood raises no ask, and its change is offered again."""

    _burst(session, project, routine=3)
    reading = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    item = _held_out(reading, reason)
    (child,) = item.children
    saved = resolve_review_packet(
        session, _coordination(reading, item, question="Who owes us this answer?")
    )

    undone = reverse_review_packet(
        session,
        project_id=project.id,
        receipt_id=saved.receipt_id,
        principal=ALICE,
        reversed_at=DECIDED_AT,
        idempotency_key=f"undo:{uuid4().hex[:10]}",
    )

    assert undone.status == "reversed"
    assert read_project_workflow(
        session, project_id=project.id, as_of=CUTOFF
    ).follow_up == ()
    assert (
        read_follow_up_bundles(session, project_id=project.id, as_of=CUTOFF).bundles
        == ()
    )
    # The change is back on its own item, decidable again, with nothing accepted.
    again = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    assert child.delta_id in _held_out(again, reason).delta_ids
    assert again.follow_up_plans == ()
    assert all(not value.follow_up_plans for current_item in again.items for value in current_item.children)
    assert live_delta_status(session, child.delta_id) == "open"


def test_review_follow_up_plan_respects_the_declared_reading_cutoff(session, project):
    from datetime import timedelta

    _burst(session, project, routine=3)
    before = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    item = _held_out(before, HELD_OUT_POSSIBLE_NEW_CONFLICT)
    resolve_review_packet(session, _coordination(before, item, question="Confirm this conflict identity."))
    early = read_review_items(session, project_id=project.id, as_of=DECIDED_AT - timedelta(seconds=1))
    assert early.follow_up_plans == ()
    later = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    (visible,) = later.follow_up_plans
    assert visible.open_question == "Confirm this conflict identity."
    assert visible.recorded_at == DECIDED_AT
    assert visible.target_subject_identity == item.children[0].subject_identity
