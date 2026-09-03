"""The coordinator reading of one authoritative source revision (#527).

One revised UCM produces dozens of exact, independent, compatible changes and a
handful that fail differently. The properties under test are that the revision
is *one* bounded item with its own accounting, that everything the partition
took out of the batch is still reachable exactly once, and that what the item
shows — accepted value and revision, incoming value, exact source, affected
fields and Utility Conflicts, and the customer artifacts that would change — is
read from stored identities rather than guessed.

Nothing here reads a clock. Every cutoff and decision instant is declared.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.analytics import EventFamily, capture_events, default_binding
from corridor.db import engine
from corridor.models import FactDecision, Project, ProjectRecordRevision
from corridor.packet_review import (
    ARTIFACT_IMPACT_RULE_VERSION,
    CUSTOMER_WORKBOOK,
    NOT_READY_NO_SUPPORT,
    WEEKLY_REPORT,
    ReviewScreenRefused,
    emit_packet_opening,
    emit_packet_surfacing,
    packet_request,
    read_review_items,
)
from corridor.principals import HumanPrincipal
from corridor.review_packet_reading import (
    COORDINATION_QUESTION,
    HELD_OUT_APPARENT_REMOVAL,
    HELD_OUT_OWNER_MISMATCH,
    HELD_OUT_POSSIBLE_NEW_CONFLICT,
    HELD_OUT_UNCERTAIN_SCOPE,
    PARTITION_RULE_VERSION,
    SOURCE_REVISION,
)
from corridor.review_packets import APPLY, DEFER, KEEP_CURRENT, resolve_review_packet

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
    assert [item.actionable for item in first.items] == [
        item.actionable for item in second.items
    ]
    assert {item.grouping_rule_version for item in first.items} == {
        PARTITION_RULE_VERSION
    }
    assert _batch_item(first).artifact_rule_version == ARTIFACT_IMPACT_RULE_VERSION


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
    assert question.decidable is False
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
    assert sorted(offered) == sorted(reading.actionable_delta_ids)
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
