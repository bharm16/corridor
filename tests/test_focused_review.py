"""One cross-source coordination question, decided as one act (#528).

A revised UCM, the minutes of the meeting that discussed it, and a schedule
export can all bear on one Utility Conflict's Promised For, and they arrive
separately.  #527 puts one source revision in front of a coordinator; this is
the other half of ADR-0085's adaptive keying — the item whose subject is the
*question* rather than the document, and the commitment whose subject is the
promise rather than either.

The properties under test are the ones the ticket names: several sources reach
one focused item with the accepted position and every source passage visible
together; unrelated changes from those same sources stay in their own items;
contradicting sources are never collapsed into one incoming value; a
commitment that states its own Applies To scope stays one decision with that
scope enumerated; the partition never offers a delta twice; and low-confidence
extraction residue never reaches the coordinator at all.

Nothing here reads a clock.  Every cutoff, decision instant, and return date is
declared by the test.
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
from urllib.parse import quote
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.db import engine
from corridor.models import (
    DeltaDisposition,
    DeltaFollowUpPlan,
    Fact,
    Project,
    ProjectRecordRevision,
    ProposedDelta,
    UnreadableCellResolution,
)
from corridor.packet_review import (
    LEAVE_OPEN,
    NEEDS_CHOSEN_SOURCE,
    NEEDS_OWNER,
    NEEDS_QUESTION,
    NEEDS_RETURN_DATE,
    RIVAL_ANSWERS,
    FocusedAnswer,
    ReviewScreenRefused,
    focused_request,
    read_review_items,
)
from corridor.principals import HumanPrincipal
from corridor.proposed_deltas import ExistingSubjectTarget, ProposedDeltaValues
from corridor.review_packet_reading import (
    COORDINATION_QUESTION,
    KEY_CONTRADICTED_IDENTITY,
    KEY_SHARED_COMMITMENT_SCOPE,
    SHARED_COMMITMENT,
    SOURCE_REVISION,
    read_open_deltas,
)
from corridor.review_packets import (
    APPLY,
    DEFER,
    EDIT_AND_APPLY,
    KEEP_CURRENT,
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
from packet_review_support import (
    Rendition,
    accept_baseline_fact,
    append_deltas,
    append_statement_deltas,
    modify,
    record_statement,
    register_baseline,
    register_output_template,
    register_source_row,
    subject,
    support,
)


COORDINATOR = HumanPrincipal("local:coordinator")
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
RETURNS_AT = datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc)
CONFLICT = 42
OTHER = 43
THIRD = 44


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
        slug=f"focused-{uuid4().hex[:8]}",
        name="Focused Review",
        is_synthetic=True,
    )
    session.add(row)
    session.flush()
    seed_membership(session, row, COORDINATOR)
    return row


@pytest.fixture
def client(session):
    """The app shares the test's transaction and the test's declared instant."""

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: COORDINATOR
    app.dependency_overrides[get_review_clock] = lambda: (lambda: NOW)
    with TestClient(app) as made:
        yield made
    app.dependency_overrides.clear()


# --- the fixture the ticket names -----------------------------------------


class Coordination:
    """U-042's Promised For, answered differently by three retained sources.

    The accepted record holds one date.  A revised UCM, the minutes of the
    meeting that discussed it, and the contractor's schedule export each carry
    a different one, and each of those three sources also carries an ordinary
    unrelated change that has nothing to do with U-042.
    """

    ACCEPTED = "2026-11-01"
    FROM_WORKBOOK = "2026-12-15"
    FROM_MINUTES = "2027-01-20"
    FROM_SCHEDULE = "2027-02-03"

    def __init__(self, session: Session, project: Project):
        self.session = session
        self.project = project
        self.adopted = Rendition(session, project, "ucm-2026-08.xlsx")
        self.revision_of: dict[tuple[str, str], int] = {}
        first: int | None = None
        for number in (CONFLICT, OTHER, THIRD):
            for fact_type, value in (
                ("committed_date", self.ACCEPTED),
                ("station_from", f"{1000 + number}+00"),
            ):
                fact, _ = self.adopted.capture(
                    fact_type=fact_type,
                    value=value,
                    subject_key=subject(number),
                    date_value=(
                        datetime.fromisoformat(value).date()
                        if fact_type == "committed_date"
                        else None
                    ),
                )
                revision = accept_baseline_fact(session, project, fact)
                first = first or revision
                self.revision_of[(subject(number), fact_type)] = revision
        assert first is not None
        self.baseline = register_baseline(
            session, project, self.adopted.document, first
        )
        for number in (CONFLICT, OTHER, THIRD):
            register_source_row(
                session,
                project,
                self.baseline,
                row_number=number,
                business_identity=f"U-{number:03d}",
                external_system_id=f"UCM-{number:05d}",
                source_url=f"https://records.example.gov/conflict/{number}",
            )
        register_output_template(
            session, project, identity="district-ucm-template", version="v3"
        )
        self.renditions: dict[str, Rendition] = {}

    def _rendition(self, name: str) -> Rendition:
        if name not in self.renditions:
            self.renditions[name] = Rendition(self.session, self.project, name)
        return self.renditions[name]

    def answer(
        self,
        *,
        document: str,
        family: str,
        revision: str,
        value: str,
        number: int = CONFLICT,
        supported: bool = True,
        unrelated: int | None = None,
    ) -> tuple[ProposedDelta, ...]:
        """One source's own answer for a Promised For, plus its own other work."""

        rendition = self._rendition(document)
        fact, segment = rendition.capture(
            fact_type="committed_date",
            value=value,
            subject_key=subject(number),
            date_value=datetime.fromisoformat(value).date(),
        )
        if supported:
            support(self.session, self.project, fact, segment)
        values = [
            modify(
                subject_key=subject(number),
                field_name="committed_date",
                accepted_value=self.ACCEPTED,
                proposed_value=value,
                baseline_revision=self.revision_of[
                    (subject(number), "committed_date")
                ],
            )
        ]
        if unrelated is not None:
            station = f"{5000 + unrelated}+00"
            other_fact, other_segment = rendition.capture(
                fact_type="station_from",
                value=station,
                subject_key=subject(unrelated),
            )
            support(self.session, self.project, other_fact, other_segment)
            values.append(
                modify(
                    subject_key=subject(unrelated),
                    field_name="station_from",
                    accepted_value=f"{1000 + unrelated}+00",
                    proposed_value=station,
                    baseline_revision=self.revision_of[
                        (subject(unrelated), "station_from")
                    ],
                )
            )
        return append_deltas(
            self.session,
            self.project,
            rendition,
            source_family=family,
            source_revision=revision,
            values=values,
        )


def _three_sources(session: Session, project: Project) -> Coordination:
    """The ticket's own fixture: a revised UCM, minutes, and a schedule export."""

    built = Coordination(session, project)
    built.answer(
        document="ucm-2026-09.xlsx",
        family="ucm-workbook",
        revision="2026-09",
        value=Coordination.FROM_WORKBOOK,
        unrelated=OTHER,
    )
    built.answer(
        document="minutes-2026-09-08.pdf",
        family="meeting-minutes",
        revision="2026-09-08",
        value=Coordination.FROM_MINUTES,
        unrelated=THIRD,
    )
    built.answer(
        document="schedule-2026-09-10.xlsx",
        family="schedule-export",
        revision="2026-09-10",
        value=Coordination.FROM_SCHEDULE,
    )
    return built


def _question(session: Session, project: Project):
    reading = read_review_items(session, project_id=project.id, as_of=NOW)
    (item,) = [
        row for row in reading.items if row.grouping_key_kind == COORDINATION_QUESTION
    ]
    return reading, item


# --- criterion 1, 5: one focused item, sources side by side ----------------


def test_three_sources_on_one_promise_are_one_focused_decision(
    session: Session, project: Project
) -> None:
    """Criterion 1: one item, the accepted position, and every passage together."""

    _three_sources(session, project)

    _, item = _question(session, project)

    assert item.focused is True
    assert item.batched is False
    assert item.child_count == 3
    (position,) = item.accepted_positions
    assert position.subject_name.startswith(f"U-{CONFLICT:03d}")
    assert position.value == Coordination.ACCEPTED
    assert position.revision_id is not None
    # Every source keeps its own row, its own value, and its own passage.
    answers = item.source_answers
    assert {row.value for row in answers} == {
        Coordination.FROM_WORKBOOK,
        Coordination.FROM_MINUTES,
        Coordination.FROM_SCHEDULE,
    }
    assert {row.source for row in answers} == {
        "ucm-workbook 2026-09",
        "meeting-minutes 2026-09-08",
        "schedule-export 2026-09-10",
    }
    assert all(row.document_filename for row in answers)
    assert all(row.quote for row in answers)


def test_unrelated_changes_from_those_sources_stay_in_their_own_items(
    session: Session, project: Project
) -> None:
    """Criterion 1's other half: consolidation never swallows the neighbours."""

    _three_sources(session, project)

    reading, question = _question(session, project)
    others = [item for item in reading.items if item.item_key != question.item_key]

    assert {item.grouping_key for item in others} == {
        "ucm-workbook@2026-09",
        "meeting-minutes@2026-09-08",
    }
    for item in others:
        assert item.grouping_key_kind == SOURCE_REVISION
        assert {child.subject_identity for child in item.children} <= {
            subject(OTHER),
            subject(THIRD),
        }


def test_no_source_value_is_ever_collapsed_into_one_incoming_value(
    session: Session, project: Project
) -> None:
    """Criterion 5: three answers stay three answers."""

    _three_sources(session, project)

    _, item = _question(session, project)

    values = [child.incoming_value for child in item.children]
    assert len(values) == len(set(values)) == 3
    # And the item never advertises a single incoming value of its own.
    assert not hasattr(item, "incoming_value")


def test_every_delta_is_offered_exactly_once_after_consolidation(
    session: Session, project: Project
) -> None:
    """Criterion 8: consolidation moves deltas between items, never doubles them."""

    _three_sources(session, project)

    reading = read_review_items(session, project_id=project.id, as_of=NOW)
    offered = [
        child.delta_id for item in reading.items for child in item.children
    ]
    every = set(
        session.scalars(
            select(ProposedDelta.id).where(ProposedDelta.project_id == project.id)
        ).all()
    )
    assert sorted(offered) == sorted(set(offered))
    assert set(offered) == every


# --- criterion 3: the rule is explainable from the item --------------------


def test_the_item_says_which_key_the_rule_chose_and_why(
    session: Session, project: Project
) -> None:
    """Criterion 3: no opaque judgement decides packet membership."""

    _three_sources(session, project)

    reading, question = _question(session, project)

    assert question.grouping_rule_version == "delta-partition-v2"
    assert "two retained sources answer the same field differently" in (
        question.key_words
    )
    for item in reading.items:
        assert item.key_words
        assert item.grouping_rule_version == reading.rule_version


# --- criterion 4: what must not be one question ---------------------------


def test_an_identity_disagreement_is_named_as_one(
    session: Session, project: Project
) -> None:
    """Criterion 4: uncertain subject identity is its own bounded item."""

    built = Coordination(session, project)
    for family, revision, value in (
        ("ucm-workbook", "2026-09", "U-042A"),
        ("meeting-minutes", "2026-09-08", "U-143"),
    ):
        rendition = built._rendition(f"{family}.xlsx")
        fact, segment = rendition.capture(
            fact_type="utility_id", value=value, subject_key=subject(CONFLICT)
        )
        support(session, project, fact, segment)
        append_deltas(
            session,
            project,
            rendition,
            source_family=family,
            source_revision=revision,
            values=[
                modify(
                    subject_key=subject(CONFLICT),
                    field_name="utility_id",
                    accepted_value="U-042",
                    proposed_value=value,
                    baseline_revision=None,
                )
            ],
        )

    _, item = _question(session, project)

    assert item.key_reason == KEY_CONTRADICTED_IDENTITY
    assert item.identity_question is True
    assert "which utility this is" in item.headline


def test_a_materially_different_action_is_never_answered_with_a_value_change(
    session: Session, project: Project
) -> None:
    """Criterion 4: an edited value and a vanished row are separate items."""

    built = Coordination(session, project)
    built.answer(
        document="ucm-2026-09.xlsx",
        family="ucm-workbook",
        revision="2026-09",
        value=Coordination.FROM_WORKBOOK,
    )
    append_deltas(
        session,
        project,
        built._rendition("ucm-2026-10.xlsx"),
        source_family="ucm-workbook",
        source_revision="2026-10",
        values=[
            modify(
                subject_key=subject(CONFLICT),
                field_name="committed_date",
                accepted_value=Coordination.ACCEPTED,
                proposed_value=None,
                baseline_revision=None,
                change_type="apparent_removal",
            )
        ],
    )

    reading = read_review_items(session, project_id=project.id, as_of=NOW)

    assert not [
        item for item in reading.items if item.grouping_key_kind == COORDINATION_QUESTION
    ]
    assert len(reading.items) == 2
    for item in reading.items:
        assert item.child_count == 1
        assert item.held_out_reason is not None


# --- criterion 2, 11: one commitment over several conflicts ----------------


def _commitment(session: Session, project: Project) -> Coordination:
    """One attributable commitment moving three Utility Conflicts at once."""

    built = Coordination(session, project)
    statement = record_statement(session, project, scope_mode="selected")
    rendition = built._rendition("call-2026-09-11.pdf")
    values = []
    for number in (CONFLICT, OTHER, THIRD):
        fact, segment = rendition.capture(
            fact_type="committed_date",
            value=Coordination.FROM_MINUTES,
            subject_key=subject(number),
            date_value=datetime.fromisoformat(Coordination.FROM_MINUTES).date(),
        )
        support(session, project, fact, segment)
        values.append(
            modify(
                subject_key=subject(number),
                field_name="committed_date",
                accepted_value=Coordination.ACCEPTED,
                proposed_value=Coordination.FROM_MINUTES,
                baseline_revision=built.revision_of[
                    (subject(number), "committed_date")
                ],
            )
        )
    append_statement_deltas(
        session,
        project,
        statement,
        source_revision="stmt-2026-09-11",
        values=values,
    )
    built.statement = statement
    return built


def test_a_split_source_is_still_shown_on_the_question_it_left(
    session: Session, project: Project
) -> None:
    """Criterion 1: every relevant passage stays visible, split or not.

    A source that proposes a materially different action is decided on its own
    item, but it is still evidence about the same value, so the question lists
    it read-only rather than hiding it.
    """

    built = Coordination(session, project)
    built.answer(
        document="ucm-2026-09.xlsx",
        family="ucm-workbook",
        revision="2026-09",
        value=Coordination.FROM_WORKBOOK,
    )
    built.answer(
        document="minutes-2026-09-08.pdf",
        family="meeting-minutes",
        revision="2026-09-08",
        value=Coordination.FROM_MINUTES,
    )
    append_deltas(
        session,
        project,
        built._rendition("ucm-2026-10.xlsx"),
        source_family="ucm-workbook",
        source_revision="2026-10",
        values=[
            modify(
                subject_key=subject(CONFLICT),
                field_name="committed_date",
                accepted_value=Coordination.ACCEPTED,
                proposed_value=None,
                baseline_revision=None,
                change_type="apparent_removal",
            )
        ],
    )

    reading, question = _question(session, project)
    removal = next(
        item
        for item in reading.items
        if item.item_key != question.item_key
        and item.children[0].change_type == "apparent_removal"
    )

    assert question.child_count == 2
    (context,) = question.held_out_children
    assert context.delta_id == removal.children[0].delta_id
    assert context.held_out_item_key == removal.item_key
    assert context.selected is False
    # Read-only context is never a second control on the same delta.
    assert context.delta_id not in {child.delta_id for child in question.children}


def test_one_commitment_over_several_conflicts_is_one_decision(
    session: Session, project: Project
) -> None:
    """Criterion 2 and 11: one item, the whole scope, every child identity."""

    built = _commitment(session, project)

    reading = read_review_items(session, project_id=project.id, as_of=NOW)
    (item,) = reading.items

    assert item.grouping_key_kind == SHARED_COMMITMENT
    assert item.grouping_key == f"statement:{built.statement.id}"
    assert item.key_reason == KEY_SHARED_COMMITMENT_SCOPE
    assert item.focused is True
    assert item.child_count == 3
    scope = {row.subject_name: row for row in item.scope_subjects}
    assert len(scope) == 3
    assert all(row.decided_here for row in scope.values())
    assert all(row.fields for row in scope.values())
    # Each Utility Conflict keeps its own identity on the item.
    assert {child.subject_identity for child in item.children} == {
        subject(CONFLICT),
        subject(OTHER),
        subject(THIRD),
    }


def test_a_contradicted_conflict_leaves_the_commitment_and_says_so(
    session: Session, project: Project
) -> None:
    """The commitment's accounting stays complete where one conflict leaves it."""

    built = _commitment(session, project)
    built.answer(
        document="ucm-2026-09.xlsx",
        family="ucm-workbook",
        revision="2026-09",
        value=Coordination.FROM_WORKBOOK,
        number=OTHER,
    )

    reading = read_review_items(session, project_id=project.id, as_of=NOW)
    commitment = next(
        item for item in reading.items if item.grouping_key_kind == SHARED_COMMITMENT
    )
    question = next(
        item for item in reading.items if item.grouping_key_kind == COORDINATION_QUESTION
    )

    assert commitment.child_count == 2
    assert question.child_count == 2
    left = [
        row for row in commitment.scope_subjects if not row.decided_here
    ]
    assert len(left) == 1
    assert left[0].held_out_item_key == question.item_key


# --- the act: one question, one answer per source -------------------------


def _answer(reading, item, *answers: FocusedAnswer):
    return focused_request(
        reading,
        item,
        principal=COORDINATOR,
        decided_at=NOW,
        answers=answers,
    )


def test_one_source_wins_and_the_others_are_kept_out_in_one_revision(
    session: Session, project: Project
) -> None:
    """Criterion 9: the four decisions apply, and #526 writes all or nothing."""

    _three_sources(session, project)
    reading, item = _question(session, project)
    winner = next(
        child
        for child in item.children
        if child.incoming_value == Coordination.FROM_MINUTES
    )
    losers = [child for child in item.children if child.delta_id != winner.delta_id]

    act = _answer(
        reading,
        item,
        FocusedAnswer(delta_id=winner.delta_id, outcome=APPLY),
        *(
            FocusedAnswer(delta_id=child.delta_id, outcome=KEEP_CURRENT)
            for child in losers
        ),
    )
    result = resolve_review_packet(session, act)

    assert result.status == "saved"
    assert result.revision_id is not None
    # One revision, three separately identified decisions.
    assert len(result.children) == 3
    assert len({child.decision_id for child in result.children}) == 3
    revisions = session.scalars(
        select(ProjectRecordRevision.id).where(
            ProjectRecordRevision.project_id == project.id,
            ProjectRecordRevision.command_type == "resolve_delta",
        )
    ).all()
    assert len(revisions) == 1
    dispositions = {
        row.delta_id: row.disposition
        for row in session.scalars(
            select(DeltaDisposition).where(DeltaDisposition.project_id == project.id)
        ).all()
    }
    assert dispositions[winner.delta_id] == "accept"
    assert all(dispositions[child.delta_id] == "reject" for child in losers)


def test_two_sources_cannot_both_become_the_accepted_value(
    session: Session, project: Project
) -> None:
    """Only one answer can stand, and the refusal happens before any write."""

    _three_sources(session, project)
    reading, item = _question(session, project)
    first, second, _ = item.children

    with pytest.raises(ReviewScreenRefused) as refused:
        _answer(
            reading,
            item,
            FocusedAnswer(delta_id=first.delta_id, outcome=APPLY),
            FocusedAnswer(delta_id=second.delta_id, outcome=APPLY),
        )

    assert str(refused.value) == RIVAL_ANSWERS
    assert refused.value.delta_id == second.delta_id
    assert not session.scalars(
        select(DeltaDisposition).where(DeltaDisposition.project_id == project.id)
    ).all()


def test_the_packet_command_refuses_rival_effects_by_itself(
    session: Session, project: Project
) -> None:
    """The rule lives in #526, so both screens are held to it, not just one.

    Built by hand rather than through the screen: an invariant enforced only
    at the door the screen uses is not enforced.
    """

    from corridor.review_packets import PacketChildRequest, ReviewPacketRequest

    _three_sources(session, project)
    reading, item = _question(session, project)
    first, second, _ = item.children

    result = resolve_review_packet(
        session,
        ReviewPacketRequest(
            project_id=project.id,
            grouping_rule_version=item.grouping_rule_version,
            grouping_key_kind=item.grouping_key_kind,
            grouping_key=item.grouping_key,
            principal=COORDINATOR,
            idempotency_key=f"rival:{uuid4().hex[:10]}",
            decided_at=NOW,
            observed_accepted_revision_id=reading.accepted_revision_id,
            children=(
                PacketChildRequest(
                    delta_id=first.delta_id,
                    outcome=APPLY,
                    observed_source_revision=first.source_revision,
                    record_effects=(),
                    support_assessment_ids=first.support_assessment_ids,
                ),
                PacketChildRequest(
                    delta_id=second.delta_id,
                    outcome=APPLY,
                    observed_source_revision=second.source_revision,
                    record_effects=(),
                    support_assessment_ids=second.support_assessment_ids,
                ),
            ),
        ),
    )

    assert result.status == "refused"
    assert "rival_effects" in {refusal.reason for refusal in result.refusals}
    assert not session.scalars(
        select(DeltaDisposition).where(DeltaDisposition.project_id == project.id)
    ).all()


def test_edit_and_apply_takes_another_sources_captured_value(
    session: Session, project: Project
) -> None:
    """ADR-0084: an edited external fact is another source, never typed words."""

    _three_sources(session, project)
    reading, item = _question(session, project)
    subject_child = next(
        child
        for child in item.children
        if child.incoming_value == Coordination.FROM_WORKBOOK
    )
    chosen = next(
        child
        for child in item.children
        if child.incoming_value == Coordination.FROM_SCHEDULE
    )

    act = _answer(
        reading,
        item,
        FocusedAnswer(
            delta_id=subject_child.delta_id,
            outcome=EDIT_AND_APPLY,
            apply_fact_from_delta_id=chosen.delta_id,
        ),
    )
    result = resolve_review_packet(session, act)

    assert result.status == "saved"
    (child,) = result.children
    assert child.outcome == EDIT_AND_APPLY
    effective = session.scalar(
        select(Fact.date_value).where(Fact.id == chosen.incoming_fact_id)
    )
    assert effective.isoformat() == Coordination.FROM_SCHEDULE


def test_edit_and_apply_refuses_a_source_this_item_does_not_offer(
    session: Session, project: Project
) -> None:
    _three_sources(session, project)
    reading, item = _question(session, project)
    child = item.children[0]

    with pytest.raises(ReviewScreenRefused) as refused:
        _answer(
            reading,
            item,
            FocusedAnswer(
                delta_id=child.delta_id,
                outcome=EDIT_AND_APPLY,
                apply_fact_from_delta_id=None,
            ),
        )

    assert str(refused.value) == NEEDS_CHOSEN_SOURCE
    assert refused.value.control == "source"


def test_needs_coordination_records_the_question_and_who_owes_it(
    session: Session, project: Project
) -> None:
    """Corridor never requires a false resolution (ADR-0085)."""

    _three_sources(session, project)
    reading, item = _question(session, project)
    child = item.children[0]

    act = _answer(
        reading,
        item,
        FocusedAnswer(
            delta_id=child.delta_id,
            outcome=NEEDS_COORDINATION,
            question="Which date did the contractor actually commit to?",
            responsible_organization="Bell South",
            return_date=RETURNS_AT,
        ),
    )
    result = resolve_review_packet(session, act)

    assert result.status == "saved"
    (plan,) = session.scalars(
        select(DeltaFollowUpPlan).where(DeltaFollowUpPlan.project_id == project.id)
    ).all()
    assert "actually commit" in plan.open_question
    assert plan.responsible_organization == "Bell South"
    assert plan.return_date == RETURNS_AT
    assert plan.affected_scope["subject_identity"] == subject(CONFLICT)
    # The delta stays open: a question is not a resolution.
    assert not session.scalars(
        select(DeltaDisposition).where(
            DeltaDisposition.project_id == project.id,
            DeltaDisposition.delta_id == child.delta_id,
        )
    ).all()


@pytest.mark.parametrize(
    ("answer_kwargs", "message", "control"),
    (
        (
            {"outcome": NEEDS_COORDINATION, "responsible_organization": "Bell"},
            NEEDS_QUESTION,
            "question",
        ),
        (
            {"outcome": NEEDS_COORDINATION, "question": "which date?"},
            NEEDS_OWNER,
            "responsible",
        ),
        ({"outcome": DEFER}, NEEDS_RETURN_DATE, "return"),
    ),
)
def test_an_incomplete_answer_names_the_control_that_holds_it(
    session: Session,
    project: Project,
    answer_kwargs: dict,
    message: str,
    control: str,
) -> None:
    """The rule refuses; the screen only renders what it said."""

    _three_sources(session, project)
    reading, item = _question(session, project)
    child = item.children[0]

    with pytest.raises(ReviewScreenRefused) as refused:
        _answer(reading, item, FocusedAnswer(delta_id=child.delta_id, **answer_kwargs))

    assert str(refused.value) == message
    assert refused.value.delta_id == child.delta_id
    assert refused.value.control == control


def test_a_source_left_open_records_nothing_and_stays_offered(
    session: Session, project: Project
) -> None:
    """Leaving a source open is the absence of a decision, not a sixth one."""

    _three_sources(session, project)
    reading, item = _question(session, project)
    answered, *rest = item.children

    act = _answer(
        reading,
        item,
        FocusedAnswer(delta_id=answered.delta_id, outcome=KEEP_CURRENT),
        *(
            FocusedAnswer(delta_id=child.delta_id, outcome=LEAVE_OPEN)
            for child in rest
        ),
    )
    resolve_review_packet(session, act)

    later = read_review_items(session, project_id=project.id, as_of=NOW)
    still_open = {
        child.delta_id
        for later_item in later.items
        for child in later_item.children
    }
    assert answered.delta_id not in still_open
    assert {child.delta_id for child in rest} <= still_open


def test_an_act_with_no_answer_at_all_is_refused(
    session: Session, project: Project
) -> None:
    _three_sources(session, project)
    reading, item = _question(session, project)

    with pytest.raises(ReviewScreenRefused):
        _answer(
            reading,
            item,
            *(
                FocusedAnswer(delta_id=child.delta_id, outcome=LEAVE_OPEN)
                for child in item.children
            ),
        )


def test_a_batch_is_never_answered_child_by_child(
    session: Session, project: Project
) -> None:
    """The two forms stay apart, so one delta never grows a second control."""

    _three_sources(session, project)
    reading = read_review_items(session, project_id=project.id, as_of=NOW)
    batch = next(
        item for item in reading.items if item.grouping_key_kind == SOURCE_REVISION
    )

    with pytest.raises(ReviewScreenRefused):
        _answer(
            reading,
            batch,
            FocusedAnswer(delta_id=batch.children[0].delta_id, outcome=APPLY),
        )


def test_the_same_answers_replay_rather_than_double(
    session: Session, project: Project
) -> None:
    """#457: a resubmitted form writes one act, and a changed answer is another."""

    _three_sources(session, project)
    reading, item = _question(session, project)
    first, second, _ = item.children

    once = _answer(
        reading, item, FocusedAnswer(delta_id=first.delta_id, outcome=KEEP_CURRENT)
    )
    again = _answer(
        reading, item, FocusedAnswer(delta_id=first.delta_id, outcome=KEEP_CURRENT)
    )
    different = _answer(
        reading, item, FocusedAnswer(delta_id=second.delta_id, outcome=KEEP_CURRENT)
    )

    assert once.idempotency_key == again.idempotency_key
    assert once.idempotency_key != different.idempotency_key


# --- criterion 7: what never reaches the coordinator at all ----------------


def test_a_low_confidence_extraction_failure_never_reaches_the_work_list(
    session: Session, project: Project
) -> None:
    """Criterion 7: extraction residue is Corridor operations, not human work.

    A cell no single read can be trusted on is resolved by ADR-0064's harness
    into an ``unconfirmed`` reading — "flagged, never Ready" — and an
    unconfirmed reading is not a Source Fact.  Since a Proposed Delta is only
    ever compared from captured Source Facts, the residue cannot reach this
    reading at all.

    The test builds the real thing rather than asserting the absence of
    something it never created: one unconfirmed resolution for a cell of the
    same document that also carried a readable, supported value.  The readable
    value produces exactly one item; the unreadable one produces none.  Give
    the resolution a Source Fact of its own and the first assertion goes red,
    which is what makes this a proof rather than a restatement.
    """

    built = Coordination(session, project)
    built.answer(
        document="scan-2026-09.pdf",
        family="ucm-workbook",
        revision="2026-09",
        value=Coordination.FROM_WORKBOOK,
    )
    scan = built._rendition("scan-2026-09.pdf")
    session.add(
        UnreadableCellResolution(
            project_id=project.id,
            document_id=scan.document.id,
            page_no=1,
            cell_key=f"{subject(OTHER)}!material",
            state="unconfirmed",
            value="ductile iron",
            origin="harness",
            policy_version="unreadable-cell-v1",
        )
    )
    session.flush()

    reading = read_review_items(session, project_id=project.id, as_of=NOW)

    # The residue exists, and the coordinator's list does not know about it.
    (residue,) = session.scalars(
        select(UnreadableCellResolution).where(
            UnreadableCellResolution.project_id == project.id
        )
    ).all()
    assert residue.state == "unconfirmed"
    offered = {
        (child.subject_identity, child.field)
        for item in reading.items
        for child in item.children
    }
    assert offered == {(subject(CONFLICT), "committed_date")}
    # It became no Source Fact, which is the mechanism the criterion rests on.
    assert not session.scalars(
        select(Fact.id).where(
            Fact.project_id == project.id,
            Fact.subject_key == subject(OTHER),
            Fact.fact_type == "material",
        )
    ).all()


# --- the screen -----------------------------------------------------------


def _open(client, project, key: str):
    return client.get(f"/review/{project.slug}?item={quote(key, safe='')}")


def test_the_focused_screen_shows_the_record_and_every_source_together(
    session: Session, project: Project, client
) -> None:
    _three_sources(session, project)
    _, item = _question(session, project)

    body = _open(client, project, item.item_key).text

    assert "What the record says today" in body
    assert "What each source says" in body
    assert Coordination.ACCEPTED in body
    for value in (
        Coordination.FROM_WORKBOOK,
        Coordination.FROM_MINUTES,
        Coordination.FROM_SCHEDULE,
    ):
        assert value in body
    assert "Review Packet" not in body


def test_the_focused_screen_deep_links_the_external_record_read_only(
    session: Session, project: Project, client
) -> None:
    """The #527 amendment, on this screen too: identity and links, never a map."""

    _three_sources(session, project)
    _, item = _question(session, project)

    body = _open(client, project, item.item_key).text

    assert f"UCM-{CONFLICT:05d}" in body
    import html
    href = html.unescape(re.search(r'href="([^"]+/source\?[^"]+)"', body).group(1))
    response = client.get(href, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"https://records.example.gov/conflict/{CONFLICT}"


def test_the_focused_screen_keeps_the_accessibility_properties(
    session: Session, project: Project, client
) -> None:
    """One h1, one autofocus, and every control labelled and reachable."""

    _three_sources(session, project)
    _, item = _question(session, project)

    body = _open(client, project, item.item_key).text

    assert body.count("<h1") == 1
    assert body.count("autofocus") == 1
    assert "<main>" in body
    for child in item.children:
        for control in ("outcome", "source", "question", "responsible", "return"):
            assert f'for="answer-{control}-{child.delta_id}"' in body
            assert f'id="answer-{control}-{child.delta_id}"' in body
    assert "disabled" not in body


def test_the_screen_saves_one_answer_per_source_through_the_packet_command(
    session: Session, project: Project, client
) -> None:
    _three_sources(session, project)
    _, item = _question(session, project)
    winner, *losers = item.children

    response = client.post(
        f"/review/{project.slug}/answers",
        data={
            "item_key": item.item_key,
            "answer_delta": [str(child.delta_id) for child in item.children],
            "answer_outcome": [APPLY] + [KEEP_CURRENT] * len(losers),
            "answer_source": [""] * item.child_count,
            "answer_question": [""] * item.child_count,
            "answer_person": [""] * item.child_count,
            "answer_organization": [""] * item.child_count,
            "answer_return": [""] * item.child_count,
        },
    )

    assert response.status_code == 200
    assert "Answered 3 changes" in response.text
    assert len(
        session.scalars(
            select(DeltaDisposition).where(DeltaDisposition.project_id == project.id)
        ).all()
    ) == 3


def test_an_incomplete_answer_returns_an_error_summary_linked_to_its_control(
    session: Session, project: Project, client
) -> None:
    _three_sources(session, project)
    _, item = _question(session, project)
    child = item.children[0]

    response = client.post(
        f"/review/{project.slug}/answers",
        data={
            "item_key": item.item_key,
            "answer_delta": [str(row.delta_id) for row in item.children],
            "answer_outcome": [NEEDS_COORDINATION]
            + [LEAVE_OPEN] * (item.child_count - 1),
            "answer_source": [""] * item.child_count,
            "answer_question": [""] * item.child_count,
            "answer_person": [""] * item.child_count,
            "answer_organization": [""] * item.child_count,
            "answer_return": [""] * item.child_count,
        },
    )

    assert response.status_code == 400
    assert f'href="#answer-question-{child.delta_id}"' in response.text
    assert NEEDS_QUESTION in response.text
    assert response.text.count("autofocus") == 1
    assert not session.scalars(
        select(DeltaDisposition).where(DeltaDisposition.project_id == project.id)
    ).all()


def test_a_refused_save_keeps_what_the_coordinator_typed(
    session: Session, project: Project, client
) -> None:
    """A refusal never discards the answers a person already gave."""

    _three_sources(session, project)
    _, item = _question(session, project)
    child = item.children[0]

    response = client.post(
        f"/review/{project.slug}/answers",
        data={
            "item_key": item.item_key,
            "answer_delta": [str(row.delta_id) for row in item.children],
            "answer_outcome": [NEEDS_COORDINATION]
            + [LEAVE_OPEN] * (item.child_count - 1),
            "answer_source": [""] * item.child_count,
            "answer_question": ["Which date was actually committed?"]
            + [""] * (item.child_count - 1),
            "answer_person": [""] * item.child_count,
            "answer_organization": [""] * item.child_count,
            "answer_return": [""] * item.child_count,
        },
    )

    assert response.status_code == 400
    assert "Which date was actually committed?" in response.text
    assert re.search(
        rf'id="answer-outcome-{child.delta_id}".*?'
        rf'<option value="{NEEDS_COORDINATION}"\s*selected',
        response.text,
        re.S,
    )


def test_only_the_opened_item_carries_answer_controls(
    session: Session, project: Project, client
) -> None:
    """ADR-0085's exactly-once rule, kept by the screen as well as the rule."""

    _three_sources(session, project)
    reading, item = _question(session, project)

    body = _open(client, project, item.item_key).text

    assert body.count('name="answer_delta"') == item.child_count
    others = [row for row in reading.items if row.item_key != item.item_key]
    for other in others:
        for child in other.children:
            assert f'value="{child.delta_id}"' not in body.split("</form>")[0]


# ── The same one refusal presentation the source-revision screen uses ───────


def test_the_focused_screens_left_the_reading_refusal_is_the_shared_sentence(
    session: Session, project: Project, client
) -> None:
    """Both Save handlers say this in the same words, written once (#794)."""

    from corridor.web.app import _item_left_the_reading

    _three_sources(session, project)
    _, item = _question(session, project)

    response = client.post(
        f"/review/{project.slug}/answers",
        data={
            "item_key": "coordination_question:nothing-here",
            "answer_delta": [str(row.delta_id) for row in item.children],
            "answer_outcome": [LEAVE_OPEN] * item.child_count,
            "answer_source": [""] * item.child_count,
            "answer_question": [""] * item.child_count,
            "answer_person": [""] * item.child_count,
            "answer_organization": [""] * item.child_count,
            "answer_return": [""] * item.child_count,
        },
    )

    shared = _item_left_the_reading()
    assert response.status_code == 409
    assert shared["heading"] in response.text
    assert shared["detail"] in response.text


def test_a_declared_review_refusal_is_presented_through_the_shared_shape(
    session: Session, project: Project, client
) -> None:
    """`ReviewScreenRefused` wrote the sentence, so the screen quotes it whole."""

    from corridor.web.app import REVIEW_REFUSAL_HEADING, _review_screen_refused

    presented = _review_screen_refused(
        ReviewScreenRefused("this item is answered on its own, not as a batch")
    )
    assert presented == {
        "heading": REVIEW_REFUSAL_HEADING,
        "detail": "this item is answered on its own, not as a batch",
        "rows": (),
    }

    _three_sources(session, project)
    _, item = _question(session, project)
    response = client.post(
        f"/review/{project.slug}/answers",
        data={
            "item_key": item.item_key,
            "answer_delta": [],
            "answer_outcome": [],
            "answer_source": [],
            "answer_question": [],
            "answer_person": [],
            "answer_organization": [],
            "answer_return": [],
        },
    )
    assert response.status_code == 409
    assert REVIEW_REFUSAL_HEADING in response.text
