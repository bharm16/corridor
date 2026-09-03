"""The derived reading that partitions open Proposed Deltas (#494).

The guarantee under test is exactly-once: every Proposed Delta of a project
holds exactly one standing, and every actionable one is offered by exactly one
item.  Nothing disappears and nothing is offered twice, so no two saves can
race for the same delta.  The property test asserts that over generated delta
sets rather than one worked example.

Every time in these tests is supplied by the caller.  Nothing reads the wall
clock, nothing compares a PostgreSQL-assigned ``created_at`` against a logical
time, and the deferral cases are judged against a declared cutoff.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import random
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from corridor.db import engine
from corridor.delta_resolution import (
    REJECT,
    ChildDecisionRequest,
    live_delta_status,
    resolve_delta,
)
from corridor.models import (
    ActiveExtractionRun,
    Document,
    ExtractionRun,
    Fact,
    FactSource,
    Project,
    ProposedDelta,
    SourceSegment,
)
from corridor.principals import HumanPrincipal
from corridor.proposed_deltas import (
    ExistingSubjectTarget,
    ProposedDeltaValues,
    ProposedSubjectTarget,
    create_proposed_delta_group,
    record_delta_deferral,
    record_delta_supersession,
)
from corridor.review_packet_reading import (
    ACTIONABLE,
    APPARENT_REMOVAL_BAND,
    CONSEQUENCE_BANDS,
    COORDINATION_QUESTION,
    DEFERRED,
    HELD_OUT_APPARENT_REMOVAL,
    HELD_OUT_OWNER_MISMATCH,
    HELD_OUT_POSSIBLE_NEW_CONFLICT,
    HELD_OUT_UNCERTAIN_SCOPE,
    PARTITION_RULE_VERSION,
    PAST_DUE_COMMITMENT_BAND,
    PROMISED_TIMING_CHANGE_BAND,
    RECORD_CLEANUP_BAND,
    RESOLVED,
    SOURCE_CONTRADICTION_BAND,
    SOURCE_REVISION,
    STALE,
    SUPERSEDED,
    UNPLACED_SUBJECT_BAND,
    ReviewPacketReadingRefused,
    bind_child_decision,
    bind_packet_request,
    read_open_deltas,
)
from corridor.review_packets import APPLY, KEEP_CURRENT, PacketChildRequest


ALICE = HumanPrincipal("local:alice")
CUTOFF = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
DECIDED_AT = datetime(2026, 9, 3, 11, 0, tzinfo=timezone.utc)
SUBJECT = "Utility Conflicts!7"


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
        slug=f"packet-reading-{uuid4().hex[:8]}",
        name="Packet Reading",
        is_synthetic=True,
    )
    session.add(row)
    session.flush()
    return row


# --- fixture helpers ------------------------------------------------------


def _delta(
    session: Session,
    project: Project,
    *,
    field: str | None = "station_from",
    subject: str = SUBJECT,
    change_type: str = "modify",
    accepted_value: object = "1149+00",
    proposed_value: object = "1200+00",
    source_family: str = "ucm-workbook",
    source_revision: str = "rev-1",
    baseline_revision: int | None = None,
) -> ProposedDelta:
    if change_type == "add" and field is None:
        target = ProposedSubjectTarget(
            subject_identity=subject, proposed_fields=("station_from",)
        )
    else:
        target = ExistingSubjectTarget(subject_identity=subject, field=field or "notes")
    (row,) = create_proposed_delta_group(
        session,
        project_id=project.id,
        source_family=source_family,
        source_revision=source_revision,
        is_complete_enumerative_source=True,
        row_accounting_sealed=True,
        deltas=[
            ProposedDeltaValues(
                change_type=change_type,
                target=target,
                accepted_value=accepted_value,
                proposed_value=proposed_value,
                accepted_baseline_revision=(
                    f"revision:{baseline_revision}"
                    if baseline_revision is not None
                    else None
                ),
            )
        ],
    )
    return row


class _Capture:
    """One document and the Source Facts captured from it."""

    def __init__(self, session: Session, project: Project, name: str):
        self.session = session
        self.project = project
        self.document = Document(
            project_id=project.id,
            sha256=sha256(f"{project.slug}:{name}".encode()).hexdigest(),
            filename=name,
            doc_type="matrix",
            numbering_scheme="project-unique",
            pages=1,
            parse_status="parsed",
        )
        session.add(self.document)
        session.flush()
        self.run = ExtractionRun(
            document_id=self.document.id,
            prompt_version="packet_reading_fixture_v1",
            outcome="completed",
            candidate_count=0,
            page_errors=0,
        )
        session.add(self.run)
        session.flush()
        session.add(
            ActiveExtractionRun(
                document_id=self.document.id, extraction_run_id=self.run.id
            )
        )
        self._ordinal = 0

    def fact(self, *, fact_type: str, value: str, subject_key: str) -> Fact:
        dated = fact_type in ("committed_date", "action_due_date", "need_date")
        self._ordinal += 1
        segment = SourceSegment(
            project_id=self.project.id,
            document_id=self.document.id,
            kind="spreadsheet_cell",
            exact_text=value,
            content_sha256=sha256(f"{uuid4().hex}:{value}".encode()).hexdigest(),
            ordinal=self._ordinal,
            sheet_name="Utility Conflicts",
            cell_range=f"A{self._ordinal}",
        )
        self.session.add(segment)
        self.session.flush()
        row = Fact(
            project_id=self.project.id,
            document_id=self.document.id,
            extraction_run_id=self.run.id,
            fact_type=fact_type,
            subject_kind="source_row",
            subject_key=subject_key,
            text_value=None if dated else value,
            date_value=date.fromisoformat(value) if dated else None,
            transformation="iso_date_cell_v1" if dated else "trim_cell_text_v1",
            recorded_by="extractor:packet_reading_fixture_v1",
            content_sha256=sha256(
                f"{uuid4().hex}:{fact_type}:{value}".encode()
            ).hexdigest(),
        )
        self.session.add(row)
        self.session.flush()
        self.session.add(
            FactSource(
                project_id=self.project.id,
                document_id=self.document.id,
                fact_id=row.id,
                source_segment_id=segment.id,
                role="value_source",
                ordinal=1,
            )
        )
        self.session.flush()
        return row


def _accept(session: Session, project: Project, fact: Fact) -> int:
    """One accepted decision for a subject and field, at its own revision."""

    session.execute(text("set local role corridor_fact_decision_writer"))
    revision_id = session.scalar(
        text(
            "insert into project_record_revisions ("
            "project_id, command_type, human_principal, idempotency_key"
            ") values (:project_id, 'adopt_baseline', 'local:adopter', :key)"
            " returning id"
        ),
        {"project_id": project.id, "key": f"accept:{uuid4().hex[:12]}"},
    )
    session.execute(
        text(
            "insert into fact_decisions ("
            "project_id, fact_id, subject_key, fact_type, revision_id, disposition"
            ") values (:project_id, :fact_id, :subject_key, :fact_type,"
            " :revision_id, 'include')"
        ),
        {
            "project_id": project.id,
            "fact_id": fact.id,
            "subject_key": fact.subject_key,
            "fact_type": fact.fact_type,
            "revision_id": revision_id,
        },
    )
    session.execute(text("reset role"))
    session.expire_all()
    return int(revision_id)


def _reject(session: Session, project: Project, delta: ProposedDelta) -> None:
    """Resolve one delta through #519's own command, with no record effect."""

    outcome = resolve_delta(
        session,
        ChildDecisionRequest(
            project_id=project.id,
            delta_id=delta.id,
            action=REJECT,
            principal=ALICE,
            idempotency_key=f"reject:{delta.id}:{uuid4().hex[:8]}",
            decided_at=DECIDED_AT,
        ),
    )
    assert outcome.status == "resolved", outcome.refusal


def _defer(
    session: Session,
    project: Project,
    delta: ProposedDelta,
    *,
    until: datetime | None,
    wake_condition: str | None = None,
) -> None:
    record_delta_deferral(
        session,
        project_id=project.id,
        delta_id=delta.id,
        deferred_at=DECIDED_AT,
        scheduled_by_principal=ALICE.subject,
        deferred_until=until,
        wake_condition=wake_condition,
    )


# --- the central guarantee ------------------------------------------------


def _generate(session: Session, project: Project, rng: random.Random) -> None:
    """One pseudo-random but reproducible delta set and its lifecycle."""

    subjects = ("Utility Conflicts!7", "Utility Conflicts!8", "Utility Conflicts!9")
    fields = (
        "station_from",
        "committed_date",
        "need_date",
        "external_org",
        "applies_to",
        "notes",
    )
    lineages = (
        ("ucm-workbook", "rev-1"),
        ("ucm-workbook", "rev-2"),
        ("meeting-minutes", "m-1"),
        ("schedule-export", "s-1"),
    )

    # A few accepted values, so some deltas can be stale against a moved record.
    capture = _Capture(session, project, f"baseline-{uuid4().hex[:6]}.xlsx")
    standing: dict[tuple[str, str], int] = {}
    for subject in subjects[: rng.randint(0, 2)]:
        field = rng.choice(("station_from", "committed_date"))
        value = "2026-10-01" if field == "committed_date" else "1149+00"
        fact = capture.fact(fact_type=field, value=value, subject_key=subject)
        standing[(subject, field)] = _accept(session, project, fact)

    deltas: list[ProposedDelta] = []
    for index in range(rng.randint(8, 18)):
        subject = rng.choice(subjects)
        change_type = rng.choices(
            ("modify", "add", "apparent_removal"), weights=(7, 2, 2)
        )[0]
        field = None if change_type == "add" and rng.random() < 0.5 else rng.choice(fields)
        family, revision = rng.choice(lineages)
        moved = standing.get((subject, field or ""))
        baseline = None
        if rng.random() < 0.5:
            # Half the deltas name the revision they were compared against; a
            # value that has since moved makes exactly those stale.
            baseline = (moved - 1) if moved else rng.randint(1, 3)
        deltas.append(
            _delta(
                session,
                project,
                field=field,
                subject=subject,
                change_type=change_type,
                proposed_value=f"value-{index}-{uuid4().hex[:6]}",
                source_family=family,
                source_revision=revision,
                baseline_revision=baseline,
            )
        )

    # Supersessions first: a superseded delta can no longer be resolved or
    # deferred, which is the order the commands themselves enforce.
    spent: set[int] = set()
    for delta in deltas:
        if rng.random() >= 0.2:
            continue
        candidates = [
            other
            for other in deltas
            if other.id != delta.id
            and other.id not in spent
            and delta.id not in spent
            and other.source_family == delta.source_family
        ]
        if not candidates:
            continue
        superseding = rng.choice(candidates)
        if superseding.id in spent or delta.id in spent:
            continue
        record_delta_supersession(
            session,
            project_id=project.id,
            prior_delta_id=delta.id,
            superseding_delta_id=superseding.id,
        )
        spent.add(delta.id)

    for delta in deltas:
        if delta.id in spent:
            continue
        roll = rng.random()
        organization = delta.target_field in ("external_org", "external_org_contact")
        if roll < 0.15 and not organization:
            _reject(session, project, delta)
            spent.add(delta.id)
        elif roll < 0.4:
            until = rng.choice(
                (
                    None,
                    CUTOFF - timedelta(days=3),
                    CUTOFF + timedelta(days=7),
                )
            )
            _defer(
                session,
                project,
                delta,
                until=until,
                wake_condition="newer_source_version" if until is None else None,
            )


@pytest.mark.parametrize("seed", range(24))
def test_every_delta_holds_one_standing_and_every_actionable_one_is_offered_once(
    session: Session, project: Project, seed: int
) -> None:
    """Nothing disappears from the reading and nothing is offered twice."""

    _generate(session, project, random.Random(seed))

    reading = read_open_deltas(session, project_id=project.id, as_of=CUTOFF)

    every_delta = {
        row.id
        for row in session.scalars(
            select(ProposedDelta).where(ProposedDelta.project_id == project.id)
        ).all()
    }
    assert every_delta, "the generator produced no deltas to partition"

    # One standing each, over every delta the project holds.
    standings = Counter(standing.delta_id for standing in reading.standings)
    assert set(standings) == every_delta
    assert max(standings.values()) == 1

    # Exactly one actionable item offers each actionable delta.
    offered = Counter(
        delta_id for item in reading.items for delta_id in item.delta_ids
    )
    assert set(offered) == set(reading.actionable_delta_ids)
    assert not offered or max(offered.values()) == 1

    # An item is never empty and never offers a delta it should not.
    for item in reading.items:
        assert item.delta_ids
        assert set(item.delta_ids) <= set(reading.actionable_delta_ids)

    # The four non-actionable standings and the actionable set are disjoint and
    # together cover the project, so nothing silently vanishes.
    buckets = (
        set(reading.actionable_delta_ids),
        {standing.delta_id for standing in reading.deferred},
        {standing.delta_id for standing in reading.stale},
        {standing.delta_id for standing in reading.superseded},
        {standing.delta_id for standing in reading.resolved},
    )
    union: set[int] = set()
    for bucket in buckets:
        assert not (union & bucket)
        union |= bucket
    assert union == every_delta


@pytest.mark.parametrize("seed", (3, 11, 19))
def test_the_reading_agrees_with_the_delta_lifecycle_command(
    session: Session, project: Project, seed: int
) -> None:
    """No delta the commands call resolved or superseded is ever offered."""

    _generate(session, project, random.Random(seed))
    reading = read_open_deltas(session, project_id=project.id, as_of=CUTOFF)

    for standing in reading.standings:
        status = live_delta_status(session, standing.delta_id)
        if standing.standing == RESOLVED:
            assert status == "resolved"
        elif standing.standing == SUPERSEDED:
            assert status == "superseded"
        else:
            # Actionable, deferred, and stale are all still open acts.
            assert status in ("open", "deferred")


def test_the_reading_is_deterministic(session: Session, project: Project) -> None:
    _generate(session, project, random.Random(7))

    first = read_open_deltas(session, project_id=project.id, as_of=CUTOFF)
    second = read_open_deltas(session, project_id=project.id, as_of=CUTOFF)

    assert first.items == second.items
    assert first.standings == second.standings


# --- the partition rule ---------------------------------------------------


def test_one_source_revision_batches_its_compatible_children_as_one_item(
    session: Session, project: Project
) -> None:
    first = _delta(session, project, field="station_from")
    second = _delta(session, project, field="station_to", proposed_value="1300+00")
    third = _delta(
        session,
        project,
        field="notes",
        subject="Utility Conflicts!8",
        proposed_value="revised note",
    )

    reading = read_open_deltas(session, project_id=project.id, as_of=CUTOFF)

    assert len(reading.items) == 1
    (item,) = reading.items
    assert item.grouping_key_kind == SOURCE_REVISION
    assert item.grouping_key == "ucm-workbook@rev-1"
    assert item.grouping_rule_version == PARTITION_RULE_VERSION
    assert item.held_out_reason is None
    assert item.delta_ids == (first.id, second.id, third.id)


def test_an_apparent_removal_is_its_own_band_and_its_own_item(
    session: Session, project: Project
) -> None:
    routine = _delta(session, project, field="station_from")
    removal = _delta(
        session,
        project,
        field="station_to",
        change_type="apparent_removal",
        proposed_value=None,
    )

    reading = read_open_deltas(session, project_id=project.id, as_of=CUTOFF)

    removal_items = [item for item in reading.items if removal.id in item.delta_ids]
    assert len(removal_items) == 1
    (held,) = removal_items
    assert held.delta_ids == (removal.id,)
    assert held.held_out_reason == HELD_OUT_APPARENT_REMOVAL
    assert held.band == APPARENT_REMOVAL_BAND
    # The routine change still batches, and never with the removal.
    (batch,) = [item for item in reading.items if routine.id in item.delta_ids]
    assert batch.delta_ids == (routine.id,)
    assert batch.band == RECORD_CLEANUP_BAND


@pytest.mark.parametrize(
    "field,change_type,expected",
    (
        ("external_org", "modify", HELD_OUT_OWNER_MISMATCH),
        ("applies_to", "modify", HELD_OUT_UNCERTAIN_SCOPE),
        (None, "add", HELD_OUT_POSSIBLE_NEW_CONFLICT),
    ),
)
def test_a_child_that_cannot_join_a_batch_falls_back_to_one_delta(
    session: Session,
    project: Project,
    field: str | None,
    change_type: str,
    expected: str,
) -> None:
    routine = _delta(session, project, field="station_from")
    held_out = _delta(
        session,
        project,
        field=field,
        change_type=change_type,
        subject="Utility Conflicts!8",
        proposed_value="held out",
    )

    reading = read_open_deltas(session, project_id=project.id, as_of=CUTOFF)

    (held,) = [item for item in reading.items if held_out.id in item.delta_ids]
    assert held.delta_ids == (held_out.id,)
    assert held.held_out_reason == expected
    (batch,) = [item for item in reading.items if routine.id in item.delta_ids]
    assert held_out.id not in batch.delta_ids


def test_contradicting_sources_become_one_coordination_question(
    session: Session, project: Project
) -> None:
    from_workbook = _delta(
        session,
        project,
        field="committed_date",
        accepted_value="2026-11-01",
        proposed_value="2026-12-15",
    )
    from_minutes = _delta(
        session,
        project,
        field="committed_date",
        accepted_value="2026-11-01",
        proposed_value="2027-01-20",
        source_family="meeting-minutes",
        source_revision="m-1",
    )

    reading = read_open_deltas(session, project_id=project.id, as_of=CUTOFF)

    assert len(reading.items) == 1
    (item,) = reading.items
    assert item.grouping_key_kind == COORDINATION_QUESTION
    assert item.grouping_key == f"{SUBJECT}#committed_date"
    assert item.delta_ids == (from_workbook.id, from_minutes.id)
    # A contradicted Promised For carries both reasons, and ADR-0035 puts the
    # item under the highest-consequence one rather than hiding the other.
    assert item.attention_reasons == (
        PROMISED_TIMING_CHANGE_BAND,
        SOURCE_CONTRADICTION_BAND,
    )
    assert item.band == PROMISED_TIMING_CHANGE_BAND


def test_two_revisions_of_one_source_family_still_contradict_each_other(
    session: Session, project: Project
) -> None:
    """Two revisions disagreeing on one field is still a contradiction."""

    first = _delta(session, project, field="station_from", proposed_value="1200+00")
    second = _delta(
        session,
        project,
        field="station_from",
        proposed_value="1300+00",
        source_revision="rev-2",
    )

    reading = read_open_deltas(session, project_id=project.id, as_of=CUTOFF)

    (item,) = reading.items
    assert item.grouping_key_kind == COORDINATION_QUESTION
    assert item.band == SOURCE_CONTRADICTION_BAND
    assert item.delta_ids == (first.id, second.id)


# --- the deterministic exits ----------------------------------------------


def test_a_superseded_delta_leaves_the_reading_by_rule(
    session: Session, project: Project
) -> None:
    prior = _delta(session, project, field="station_from")
    newer = _delta(session, project, field="station_from", proposed_value="1400+00")
    record_delta_supersession(
        session,
        project_id=project.id,
        prior_delta_id=prior.id,
        superseding_delta_id=newer.id,
    )

    reading = read_open_deltas(session, project_id=project.id, as_of=CUTOFF)

    assert reading.actionable_delta_ids == (newer.id,)
    (exited,) = reading.superseded
    assert exited.delta_id == prior.id
    assert exited.superseded_by_delta_id == newer.id


def test_a_stale_delta_leaves_the_actionable_set_when_the_record_moved(
    session: Session, project: Project
) -> None:
    capture = _Capture(session, project, "baseline.xlsx")
    fact = capture.fact(fact_type="station_from", value="1149+00", subject_key=SUBJECT)
    revision = _accept(session, project, fact)

    fresh = _delta(session, project, field="station_from", baseline_revision=revision)
    stale = _delta(
        session,
        project,
        field="station_from",
        proposed_value="1500+00",
        baseline_revision=revision - 1,
    )

    reading = read_open_deltas(session, project_id=project.id, as_of=CUTOFF)

    assert reading.actionable_delta_ids == (fresh.id,)
    (exited,) = reading.stale
    assert exited.delta_id == stale.id
    assert exited.current_accepted_revision_id == revision
    assert exited.baseline_revision == revision - 1


def test_a_delta_with_no_recorded_baseline_is_never_called_stale(
    session: Session, project: Project
) -> None:
    capture = _Capture(session, project, "baseline.xlsx")
    fact = capture.fact(fact_type="station_from", value="1149+00", subject_key=SUBJECT)
    _accept(session, project, fact)
    unknown = _delta(session, project, field="station_from", baseline_revision=None)

    reading = read_open_deltas(session, project_id=project.id, as_of=CUTOFF)

    assert reading.stale == ()
    assert reading.actionable_delta_ids == (unknown.id,)


# --- deferral is scheduling, and is never a disappearance -------------------


def test_a_dated_deferral_hides_the_delta_until_its_declared_return(
    session: Session, project: Project
) -> None:
    delta = _delta(session, project, field="station_from")
    returns_at = CUTOFF + timedelta(days=7)
    _defer(session, project, delta, until=returns_at)

    before = read_open_deltas(session, project_id=project.id, as_of=CUTOFF)
    assert before.actionable_delta_ids == ()
    (held,) = before.deferred
    assert held.delta_id == delta.id
    assert held.returns_at == returns_at

    after = read_open_deltas(
        session, project_id=project.id, as_of=returns_at + timedelta(seconds=1)
    )
    assert after.deferred == ()
    assert after.actionable_delta_ids == (delta.id,)


def test_an_open_ended_deferral_waits_for_its_wake_condition(
    session: Session, project: Project
) -> None:
    delta = _delta(session, project, field="station_from")
    _defer(session, project, delta, until=None, wake_condition="newer_source_version")

    reading = read_open_deltas(
        session, project_id=project.id, as_of=CUTOFF + timedelta(days=3650)
    )

    assert reading.actionable_delta_ids == ()
    (held,) = reading.deferred
    assert held.delta_id == delta.id
    assert held.returns_at is None
    assert held.wake_condition == "newer_source_version"


def test_a_newer_independent_source_wakes_a_deferred_delta(
    session: Session, project: Project
) -> None:
    deferred = _delta(session, project, field="committed_date", proposed_value="2026-12-15")
    _defer(session, project, deferred, until=None, wake_condition="newer_source_version")
    newer = _delta(
        session,
        project,
        field="committed_date",
        proposed_value="2027-01-20",
        source_family="meeting-minutes",
        source_revision="m-1",
    )

    reading = read_open_deltas(session, project_id=project.id, as_of=CUTOFF)

    assert reading.deferred == ()
    (item,) = reading.items
    assert item.grouping_key_kind == COORDINATION_QUESTION
    assert item.delta_ids == (deferred.id, newer.id)


def test_a_deferred_delta_stays_in_the_accounting(
    session: Session, project: Project
) -> None:
    delta = _delta(session, project, field="station_from")
    _defer(session, project, delta, until=CUTOFF + timedelta(days=7))

    reading = read_open_deltas(session, project_id=project.id, as_of=CUTOFF)

    assert [standing.delta_id for standing in reading.standings] == [delta.id]
    assert reading.standings[0].standing == DEFERRED


# --- consequence bands ----------------------------------------------------


def test_bands_order_items_by_project_consequence(
    session: Session, project: Project
) -> None:
    cleanup = _delta(
        session, project, field="notes", subject="A", proposed_value="tidy"
    )
    timing = _delta(
        session,
        project,
        field="committed_date",
        subject="B",
        accepted_value="2026-12-01",
        proposed_value="2026-12-15",
    )
    past_due = _delta(
        session,
        project,
        field="committed_date",
        subject="C",
        accepted_value="2026-08-01",
        proposed_value="2026-08-20",
    )
    removal = _delta(
        session,
        project,
        field="station_to",
        subject="D",
        change_type="apparent_removal",
        proposed_value=None,
    )

    reading = read_open_deltas(session, project_id=project.id, as_of=CUTOFF)

    bands = [item.band for item in reading.items]
    assert bands == [
        PAST_DUE_COMMITMENT_BAND,
        PROMISED_TIMING_CHANGE_BAND,
        APPARENT_REMOVAL_BAND,
        RECORD_CLEANUP_BAND,
    ]
    assert [item.ordinal for item in reading.items] == [1, 2, 3, 4]
    by_delta = {
        delta_id: item for item in reading.items for delta_id in item.delta_ids
    }
    assert by_delta[past_due.id].attention_reasons == (
        PAST_DUE_COMMITMENT_BAND,
        PROMISED_TIMING_CHANGE_BAND,
    )
    assert by_delta[timing.id].band == PROMISED_TIMING_CHANGE_BAND
    assert by_delta[removal.id].band == APPARENT_REMOVAL_BAND
    assert by_delta[cleanup.id].band == RECORD_CLEANUP_BAND


def test_a_proposed_subject_joins_the_unplaced_band(
    session: Session, project: Project
) -> None:
    proposed = _delta(session, project, field=None, change_type="add")

    reading = read_open_deltas(session, project_id=project.id, as_of=CUTOFF)

    (item,) = reading.items
    assert item.band == UNPLACED_SUBJECT_BAND
    assert item.held_out_reason == HELD_OUT_POSSIBLE_NEW_CONFLICT
    assert item.delta_ids == (proposed.id,)


def test_past_due_is_judged_against_the_declared_cutoff_not_the_clock(
    session: Session, project: Project
) -> None:
    delta = _delta(
        session,
        project,
        field="committed_date",
        accepted_value="2026-10-01",
        proposed_value="2026-10-15",
    )

    early = read_open_deltas(
        session, project_id=project.id, as_of=datetime(2026, 9, 1, tzinfo=timezone.utc)
    )
    late = read_open_deltas(
        session, project_id=project.id, as_of=datetime(2026, 11, 1, tzinfo=timezone.utc)
    )

    assert early.items[0].band == PROMISED_TIMING_CHANGE_BAND
    assert late.items[0].band == PAST_DUE_COMMITMENT_BAND
    assert delta.id in late.items[0].delta_ids


def test_a_reading_refuses_a_cutoff_with_no_time_zone(
    session: Session, project: Project
) -> None:
    with pytest.raises(ReviewPacketReadingRefused):
        read_open_deltas(session, project_id=project.id, as_of=datetime(2026, 9, 3))


# --- the invocation seam #527 and #528 use --------------------------------


def test_a_bound_child_decision_resolves_through_the_shared_command(
    session: Session, project: Project
) -> None:
    delta = _delta(session, project, field="station_from")
    reading = read_open_deltas(session, project_id=project.id, as_of=CUTOFF)
    (item,) = reading.items

    bound = bind_child_decision(
        reading,
        item,
        ChildDecisionRequest(
            project_id=project.id,
            delta_id=delta.id,
            action=REJECT,
            principal=ALICE,
            idempotency_key=f"keep:{uuid4().hex[:8]}",
            decided_at=DECIDED_AT,
        ),
    )
    assert bound.observed_accepted_revision_id == reading.accepted_revision_id

    outcome = resolve_delta(session, bound)
    assert outcome.status == "resolved"

    after = read_open_deltas(session, project_id=project.id, as_of=CUTOFF)
    assert after.items == ()
    assert [standing.standing for standing in after.resolved] == [RESOLVED]


def test_binding_refuses_a_delta_the_item_does_not_offer(
    session: Session, project: Project
) -> None:
    inside = _delta(session, project, field="station_from")
    outside = _delta(
        session,
        project,
        field="external_org",
        subject="Utility Conflicts!8",
        proposed_value="Other Utility",
    )
    reading = read_open_deltas(session, project_id=project.id, as_of=CUTOFF)
    (item,) = [entry for entry in reading.items if inside.id in entry.delta_ids]

    with pytest.raises(ReviewPacketReadingRefused):
        bind_child_decision(
            reading,
            item,
            ChildDecisionRequest(
                project_id=project.id,
                delta_id=outside.id,
                action=REJECT,
                principal=ALICE,
                idempotency_key="wrong-item",
                decided_at=DECIDED_AT,
            ),
        )


def test_a_bound_packet_request_carries_the_items_grouping_key(
    session: Session, project: Project
) -> None:
    first = _delta(session, project, field="station_from")
    second = _delta(session, project, field="station_to", proposed_value="1300+00")
    reading = read_open_deltas(session, project_id=project.id, as_of=CUTOFF)
    (item,) = reading.items

    request = bind_packet_request(
        reading,
        item,
        principal=ALICE,
        idempotency_key="packet-1",
        decided_at=DECIDED_AT,
        children=(
            PacketChildRequest(
                delta_id=first.id,
                outcome=KEEP_CURRENT,
                observed_source_revision=first.source_revision,
            ),
        ),
    )

    assert request.project_id == project.id
    assert request.grouping_rule_version == PARTITION_RULE_VERSION
    assert request.grouping_key_kind == SOURCE_REVISION
    assert request.grouping_key == item.grouping_key
    assert request.observed_accepted_revision_id == reading.accepted_revision_id
    # A selection is the coordinator's; the second child simply stays open.
    assert [child.delta_id for child in request.children] == [first.id]
    assert second.id in item.delta_ids


def test_a_bound_packet_refuses_a_child_from_another_item(
    session: Session, project: Project
) -> None:
    inside = _delta(session, project, field="station_from")
    outside = _delta(
        session,
        project,
        field="applies_to",
        subject="Utility Conflicts!8",
        proposed_value="U-042",
    )
    reading = read_open_deltas(session, project_id=project.id, as_of=CUTOFF)
    (item,) = [entry for entry in reading.items if inside.id in entry.delta_ids]

    with pytest.raises(ReviewPacketReadingRefused):
        bind_packet_request(
            reading,
            item,
            principal=ALICE,
            idempotency_key="packet-2",
            decided_at=DECIDED_AT,
            children=(
                PacketChildRequest(
                    delta_id=outside.id,
                    outcome=APPLY,
                    observed_source_revision=outside.source_revision,
                ),
            ),
        )


def test_an_empty_project_reads_as_an_empty_partition(
    session: Session, project: Project
) -> None:
    reading = read_open_deltas(session, project_id=project.id, as_of=CUTOFF)

    assert reading.items == ()
    assert reading.standings == ()
    assert reading.actionable_delta_ids == ()
    assert reading.rule_version == PARTITION_RULE_VERSION
    assert reading.as_of == CUTOFF


def test_the_reading_ignores_another_projects_deltas(
    session: Session, project: Project
) -> None:
    other = Project(
        slug=f"other-{uuid4().hex[:8]}", name="Other", is_synthetic=True
    )
    session.add(other)
    session.flush()
    mine = _delta(session, project, field="station_from")
    _delta(session, other, field="station_from")

    reading = read_open_deltas(session, project_id=project.id, as_of=CUTOFF)

    assert reading.actionable_delta_ids == (mine.id,)


def test_actionable_standings_name_their_item(
    session: Session, project: Project
) -> None:
    delta = _delta(session, project, field="station_from")

    reading = read_open_deltas(session, project_id=project.id, as_of=CUTOFF)

    (standing,) = reading.standings
    assert standing.standing == ACTIONABLE
    assert standing.band == RECORD_CLEANUP_BAND
    assert standing.item_key == reading.items[0].item_key
    assert reading.item_for(delta.id) is reading.items[0]


def test_a_dated_deferral_that_has_returned_is_actionable_again(
    session: Session, project: Project
) -> None:
    delta = _delta(session, project, field="station_from")
    _defer(session, project, delta, until=CUTOFF - timedelta(days=1))

    reading = read_open_deltas(session, project_id=project.id, as_of=CUTOFF)

    assert reading.deferred == ()
    assert reading.actionable_delta_ids == (delta.id,)


def test_stale_takes_precedence_over_a_running_deferral(
    session: Session, project: Project
) -> None:
    """A changed accepted value is an ADR-0084 wake condition, and it is stale."""

    capture = _Capture(session, project, "baseline.xlsx")
    fact = capture.fact(fact_type="station_from", value="1149+00", subject_key=SUBJECT)
    revision = _accept(session, project, fact)
    delta = _delta(
        session, project, field="station_from", baseline_revision=revision - 1
    )
    _defer(session, project, delta, until=CUTOFF + timedelta(days=30))

    reading = read_open_deltas(session, project_id=project.id, as_of=CUTOFF)

    assert reading.deferred == ()
    assert [standing.delta_id for standing in reading.stale] == [delta.id]


def test_todays_promise_is_not_yet_past_due(
    session: Session, project: Project
) -> None:
    _delta(
        session,
        project,
        field="committed_date",
        accepted_value=date(2026, 9, 3).isoformat(),
        proposed_value=date(2026, 9, 20).isoformat(),
    )

    reading = read_open_deltas(session, project_id=project.id, as_of=CUTOFF)

    assert reading.items[0].band == PROMISED_TIMING_CHANGE_BAND


def test_the_generated_scenarios_exercise_every_standing_and_key(
    session: Session,
) -> None:
    """The property above is only worth its name if the input space is real."""

    standings: set[str] = set()
    kinds: set[str] = set()
    held_out: set[str] = set()
    batched = False
    for seed in range(24):
        project = Project(
            slug=f"coverage-{seed}-{uuid4().hex[:8]}",
            name="Coverage",
            is_synthetic=True,
        )
        session.add(project)
        session.flush()
        _generate(session, project, random.Random(seed))
        reading = read_open_deltas(session, project_id=project.id, as_of=CUTOFF)
        standings.update(standing.standing for standing in reading.standings)
        for item in reading.items:
            kinds.add(item.grouping_key_kind)
            if item.held_out_reason is not None:
                held_out.add(item.held_out_reason)
            batched = batched or len(item.delta_ids) > 1

    assert standings == {ACTIONABLE, DEFERRED, STALE, SUPERSEDED, RESOLVED}
    assert kinds == {SOURCE_REVISION, COORDINATION_QUESTION}
    assert held_out == {
        HELD_OUT_APPARENT_REMOVAL,
        HELD_OUT_POSSIBLE_NEW_CONFLICT,
        HELD_OUT_OWNER_MISMATCH,
        HELD_OUT_UNCERTAIN_SCOPE,
    }
    assert batched


def test_a_recurring_value_returns_as_a_new_item_beside_its_history(
    session: Session, project: Project
) -> None:
    """A rejected value recurring from a newer source is offered again (#518)."""

    rejected = _delta(session, project, field="station_from")
    _reject(session, project, rejected)
    recurrence = _delta(
        session,
        project,
        field="station_from",
        proposed_value="1200+00",
        source_family="meeting-minutes",
        source_revision="m-1",
    )

    reading = read_open_deltas(session, project_id=project.id, as_of=CUTOFF)

    assert reading.actionable_delta_ids == (recurrence.id,)
    # The earlier decision is not erased: it keeps its own standing in the
    # accounting, so the reading can show what was decided before.
    assert [standing.delta_id for standing in reading.resolved] == [rejected.id]
    (item,) = reading.items
    assert item.delta_ids == (recurrence.id,)


def test_the_band_table_is_a_total_order_with_no_gaps() -> None:
    """Every band an item can carry is orderable, and no two share a place."""

    ordinals = [band.ordinal for band in CONSEQUENCE_BANDS]
    assert ordinals == list(range(1, len(CONSEQUENCE_BANDS) + 1))
    assert len({band.name for band in CONSEQUENCE_BANDS}) == len(CONSEQUENCE_BANDS)
    assert all(band.reason for band in CONSEQUENCE_BANDS)
    # ADR-0083 keeps apparent removal out of every other band.
    apparent = next(
        band for band in CONSEQUENCE_BANDS if band.name == APPARENT_REMOVAL_BAND
    )
    assert apparent.work_list_group is None
