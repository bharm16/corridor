"""One design-partner Key Date table becomes Source Facts and Proposed Deltas (#450).

The accepted record is what the project has decided; a schedule export is a
source that disagrees with it about dates. These tests fix what "disagrees"
means: which row is the same key date across exports, which rows never reach a
comparison at all, what a filtered export may never say, what a moved key date
reaches, and — the point of the ticket — what this act is forbidden to write.

Every workbook is synthetic (``key_date_table_support``). No test reads a
clock: every time is a value the test declared or a value the database stamped
and the test read back, and every "did the record move" question is answered
from append-only identifiers.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from corridor import audit
from corridor.config import settings
from corridor.db import Session, engine
from corridor.delta_resolution import (
    ACCEPT,
    RESOLVED,
    ChildDecisionRequest,
    RecordEffect,
    resolve_delta,
)
from corridor.key_date_table import (
    BLANK_CODE,
    COMPARED,
    IMPACT_RULE,
    KEY_DATE_COMPARISON_RULE_VERSION,
    KEY_DATE_TABLE_FAMILY,
    MISSING_DATE,
    NEW_KEY_DATE,
    PROCESSING_FAILURE,
    REPEATED_CODE,
    SCHEDULED_DATE_FIELD,
    UNTYPEABLE_DATE,
    WITHHELD_UNSEALED,
    KeyDateTableRefused,
    capture_key_date_table,
    key_date_subject,
)
from corridor.models import (
    AuditLog,
    Document,
    Fact,
    FactDecision,
    Project,
    ProjectRecordRevision,
    ProposedDelta,
    SupportAssessment,
)

from key_date_table_support import (
    KEY_DATE_ROWS,
    UCM_HEADINGS,
    UCM_ROWS,
    deliver_export,
    key_date_workbook,
    moved,
)
from later_revision_support import PRINCIPAL, adopt, workbook_bytes


DECIDED_AT = datetime(2026, 9, 3, 15, 0, tzinfo=timezone.utc)

# The adopted subject identities of the three UCM rows, which is what the
# accepted record keys the project's Utility Conflicts by.
UC1 = "Utility Conflicts!3"
UC2 = "Utility Conflicts!4"
UC3 = "Utility Conflicts!5"

DESIGN = key_date_subject("DESIGN")
ROW_UTIL = key_date_subject("ROW-UTIL-EXEC")
RELO = key_date_subject("RELO-CONSTR")


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
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    return tmp_path / "files"


@pytest.fixture
def project(session):
    row = Project(
        slug=f"key-date-table-{uuid4().hex[:8]}",
        name="Key Date Table Test",
        is_synthetic=True,
    )
    session.add(row)
    session.flush()
    return row


@pytest.fixture
def adopted(session, project, tmp_path, store):
    """A project whose accepted record is the three-conflict UCM baseline."""

    body = workbook_bytes(tmp_path / "ucm.xlsx", UCM_ROWS, headings=UCM_HEADINGS)
    adopt(session, project, body, tmp_path)
    return project


def _capture(session, project, rows, tmp_path, *, name, **overrides):
    body = key_date_workbook(tmp_path / f"{name}.xlsx", rows)
    staged, envelope = deliver_export(
        session,
        project,
        body,
        filename=f"{name}.xlsx",
        external_identity=f"schedule export {name}",
    )
    return capture_key_date_table(
        session,
        project=project,
        staged=staged,
        envelope=envelope,
        principal=PRINCIPAL,
        images_dir=tmp_path / "images",
        **overrides,
    )


def _deltas(session, project):
    return tuple(
        session.scalars(
            select(ProposedDelta)
            .where(ProposedDelta.project_id == project.id)
            .order_by(ProposedDelta.id)
        ).all()
    )


def _key_date_deltas(session, project):
    return tuple(
        row
        for row in _deltas(session, project)
        if row.source_family == KEY_DATE_TABLE_FAMILY
    )


def _revisions(session, project) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(ProjectRecordRevision)
            .where(ProjectRecordRevision.project_id == project.id)
        )
    )


def _decisions(session, project) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(FactDecision)
            .where(FactDecision.project_id == project.id)
        )
    )


def _accept_every_key_date(session, project, capture):
    """Resolve each proposed key date into the accepted record, as #519 does.

    The adapter proposes and stops, so the only way a key date reaches the
    accepted record is a decision. These tests make that decision explicitly
    rather than writing an accepted value behind the primitive's back, which is
    also what makes the second export's comparison a real one.
    """

    for delta in _key_date_deltas(session, project):
        if delta.id not in capture.delta_ids:
            continue
        fact = session.scalars(
            select(Fact).where(
                Fact.project_id == project.id,
                Fact.document_id == capture.document_id,
                Fact.subject_key == delta.target_subject_identity,
            )
        ).one()
        assessment = session.scalars(
            select(SupportAssessment).where(SupportAssessment.fact_id == fact.id)
        ).one()
        outcome = resolve_delta(
            session,
            ChildDecisionRequest(
                project_id=project.id,
                delta_id=delta.id,
                action=ACCEPT,
                principal=PRINCIPAL,
                idempotency_key=f"accept:{delta.id}",
                observed_accepted_revision_id=session.scalar(select(func.max(ProjectRecordRevision.id)).where(ProjectRecordRevision.project_id == project.id)),
                decided_at=DECIDED_AT,
                record_effects=(RecordEffect(fact_id=fact.id),),
                support_assessment_ids=(assessment.id,),
            ),
        )
        assert outcome.status == RESOLVED, outcome.refusal


# --- what one arrival captures and proposes --------------------------------


def test_the_first_export_captures_every_key_date_and_proposes_each_as_new(
    session, adopted, tmp_path, store
):
    capture = _capture(session, adopted, KEY_DATE_ROWS, tmp_path, name="a")

    facts = session.scalars(
        select(Fact).where(Fact.document_id == capture.document_id)
    ).all()
    assert {fact.subject_key for fact in facts} == {DESIGN, ROW_UTIL, RELO}
    assert {fact.fact_type for fact in facts} == {SCHEDULED_DATE_FIELD}
    assert {fact.date_value.isoformat() for fact in facts} == {
        "2025-01-31",
        "2025-07-31",
        "2026-03-31",
    }
    proposed = {
        (row.target_type, row.target_subject_identity, row.change_type)
        for row in _key_date_deltas(session, adopted)
    }
    assert proposed == {
        ("proposed_subject", DESIGN, "add"),
        ("proposed_subject", ROW_UTIL, "add"),
        ("proposed_subject", RELO, "add"),
    }
    assert capture.values_agreed == 0
    assert [row.disposition for row in capture.accounting.rows] == [NEW_KEY_DATE] * 3
    assert capture.accounting.receipt["detected_row_count"] == 3
    assert capture.accounting.receipt["accounted_row_count"] == 3


def test_capturing_a_key_date_table_writes_no_accepted_value(
    session, adopted, tmp_path, store
):
    """The whole ticket: this act proposes, and #519 and #526 decide.

    The counts are taken on a project whose accepted record already holds three
    Utility Conflicts and their Required By dates, so an assertion of "nothing
    moved" is an assertion about a populated projection, not an empty one.
    """

    before_revisions = _revisions(session, adopted)
    before_decisions = _decisions(session, adopted)
    assert before_revisions > 0 and before_decisions > 0

    capture = _capture(session, adopted, KEY_DATE_ROWS, tmp_path, name="a")

    assert _revisions(session, adopted) == before_revisions
    assert _decisions(session, adopted) == before_decisions
    # And it did do its own work, so the counts above are not unchanged because
    # nothing happened at all.
    assert len(capture.fact_ids) == 3
    assert len(capture.delta_ids) == 3


def test_no_promised_for_or_action_date_is_captured_from_a_key_date_table(
    session, adopted, tmp_path, store
):
    """A column this format does not declare never becomes a Fact or a delta.

    The export carries a Promised For column here on purpose: the accepted
    record holds a Promised For for every conflict, and an adapter that read an
    undeclared heading would be proposing to move one.
    """

    rows = [[*row, "2027-01-01"] for row in KEY_DATE_ROWS]
    body = key_date_workbook(
        tmp_path / "with-promise.xlsx",
        rows,
        headings=["code", "name", "need_date", "Promised For"],
    )
    staged, envelope = deliver_export(
        session, adopted, body, external_identity="schedule export with promise"
    )

    capture = capture_key_date_table(
        session,
        project=adopted,
        staged=staged,
        envelope=envelope,
        principal=PRINCIPAL,
        images_dir=tmp_path / "images",
    )

    assert capture.accounting.retained_columns == ("promised for",)
    facts = session.scalars(
        select(Fact).where(Fact.document_id == capture.document_id)
    ).all()
    assert {fact.fact_type for fact in facts} == {SCHEDULED_DATE_FIELD}
    assert {row.target_field for row in _key_date_deltas(session, adopted)} == {None}


# --- what a second arrival proposes ----------------------------------------


def test_only_a_moved_key_date_becomes_a_proposed_delta(
    session, adopted, tmp_path, store
):
    first = _capture(session, adopted, KEY_DATE_ROWS, tmp_path, name="a")
    _accept_every_key_date(session, adopted, first)
    before = {row.id for row in _key_date_deltas(session, adopted)}

    capture = _capture(
        session,
        adopted,
        moved(KEY_DATE_ROWS, "RELO-CONSTR", "2026-06-30"),
        tmp_path,
        name="b",
    )

    fresh = [
        row for row in _key_date_deltas(session, adopted) if row.id not in before
    ]
    assert [
        (row.target_subject_identity, row.target_field, row.change_type)
        for row in fresh
    ] == [(RELO, SCHEDULED_DATE_FIELD, "modify")]
    assert fresh[0].accepted_value == "2026-03-31"
    assert fresh[0].proposed_value == "2026-06-30"
    assert fresh[0].comparison_rule_version == KEY_DATE_COMPARISON_RULE_VERSION
    assert fresh[0].source_revision == capture.content_sha256
    # The two key dates the export restated are captured and agreed, not
    # proposed: three Facts, two agreements, one difference.
    assert len(capture.fact_ids) == 3
    assert capture.values_agreed == 2
    assert [row.disposition for row in capture.accounting.rows] == [COMPARED] * 3


def test_a_key_date_that_moved_down_the_file_is_still_the_same_key_date(
    session, adopted, tmp_path, store
):
    """Identity is the key date code, never the printed row number."""

    first = _capture(session, adopted, KEY_DATE_ROWS, tmp_path, name="a")
    _accept_every_key_date(session, adopted, first)
    before = {row.id for row in _key_date_deltas(session, adopted)}
    inserted = [
        ["LETTING", "Letting", "2024-11-01"],
        *moved(KEY_DATE_ROWS, "DESIGN", "2025-02-28"),
    ]

    capture = _capture(session, adopted, inserted, tmp_path, name="b")

    assert [
        (row.row_number, row.subject_identity, row.disposition)
        for row in capture.accounting.rows
    ] == [
        (2, key_date_subject("LETTING"), NEW_KEY_DATE),
        (3, DESIGN, COMPARED),
        (4, ROW_UTIL, COMPARED),
        (5, RELO, COMPARED),
    ]
    fresh = {
        (row.target_type, row.target_subject_identity, row.change_type)
        for row in _key_date_deltas(session, adopted)
        if row.id not in before
    }
    assert fresh == {
        ("existing_subject", DESIGN, "modify"),
        ("proposed_subject", key_date_subject("LETTING"), "add"),
    }


# --- rows that never reach a comparison ------------------------------------


def test_a_row_that_cannot_be_read_is_a_visible_processing_failure(
    session, adopted, tmp_path, store
):
    """Never a silent skip: every populated row reaches a retained disposition."""

    rows = [
        ["DESIGN", "Design completion", "2025-01-31"],
        ["", "Nameless", "2025-02-01"],
        ["ROW-UTIL-EXEC", "Agreement execution", "next spring"],
        ["RELO-CONSTR", "Relocation", ""],
        ["DUP", "First", "2026-01-01"],
        ["DUP", "Second", "2026-02-01"],
    ]

    capture = _capture(session, adopted, rows, tmp_path, name="a")

    assert [
        (row.row_number, row.disposition, row.reason)
        for row in capture.accounting.rows
    ] == [
        (2, NEW_KEY_DATE, None),
        (3, PROCESSING_FAILURE, BLANK_CODE),
        (4, PROCESSING_FAILURE, UNTYPEABLE_DATE),
        (5, PROCESSING_FAILURE, MISSING_DATE),
        (6, PROCESSING_FAILURE, REPEATED_CODE),
        (7, PROCESSING_FAILURE, REPEATED_CODE),
    ]
    # The failure keeps the exact text it failed on, so a coordinator sees what
    # the file actually said rather than "row 4 was skipped".
    unreadable = capture.accounting.rows[2]
    assert unreadable.stated_date == "next spring"
    assert unreadable.code == "ROW-UTIL-EXEC"
    # Complete accounting, and only the readable row produced anything.
    receipt = capture.accounting.receipt
    assert receipt["detected_row_count"] == 6
    assert receipt["accounted_row_count"] == 6
    assert receipt["unaccounted_rows"] == []
    assert len(capture.fact_ids) == 1
    assert len(capture.delta_ids) == 1


def test_a_file_that_is_not_this_format_is_refused_before_anything_is_written(
    session, adopted, tmp_path, store
):
    body = key_date_workbook(
        tmp_path / "wrong.xlsx",
        [["UC-1", "CenterPoint"]],
        headings=["Utility Conflict ID", "Utility Owner"],
    )
    staged, envelope = deliver_export(
        session, adopted, body, external_identity="not a key date table"
    )
    before = int(
        session.scalar(
            select(func.count())
            .select_from(Document)
            .where(Document.project_id == adopted.id)
        )
    )

    with pytest.raises(KeyDateTableRefused, match="not the format"):
        capture_key_date_table(
            session,
            project=adopted,
            staged=staged,
            envelope=envelope,
            principal=PRINCIPAL,
            images_dir=tmp_path / "images",
        )

    assert (
        int(
            session.scalar(
                select(func.count())
                .select_from(Document)
                .where(Document.project_id == adopted.id)
            )
        )
        == before
    )


# --- what a partial export may never say -----------------------------------


def test_an_unsealed_export_proposes_no_apparent_removal(
    session, adopted, tmp_path, store
):
    first = _capture(session, adopted, KEY_DATE_ROWS, tmp_path, name="a")
    _accept_every_key_date(session, adopted, first)
    before = {row.id for row in _key_date_deltas(session, adopted)}

    capture = _capture(session, adopted, KEY_DATE_ROWS[:2], tmp_path, name="b")

    assert [
        (item.code, item.proposed, item.withheld_reason)
        for item in capture.accounting.removals
    ] == [("RELO-CONSTR", False, WITHHELD_UNSEALED)]
    assert [row for row in _key_date_deltas(session, adopted) if row.id not in before] == []


def test_a_complete_sealed_export_proposes_the_absent_key_date_for_removal(
    session, adopted, tmp_path, store
):
    first = _capture(session, adopted, KEY_DATE_ROWS, tmp_path, name="a")
    _accept_every_key_date(session, adopted, first)
    before = {row.id for row in _key_date_deltas(session, adopted)}

    _capture(
        session,
        adopted,
        KEY_DATE_ROWS[:2],
        tmp_path,
        name="b",
        is_complete_enumerative_source=True,
        row_accounting_sealed=True,
    )

    removals = [
        row
        for row in _key_date_deltas(session, adopted)
        if row.id not in before and row.change_type == "apparent_removal"
    ]
    assert [row.target_subject_identity for row in removals] == [RELO]
    assert removals[0].accepted_value == {SCHEDULED_DATE_FIELD: "2026-03-31"}


def test_an_apparent_removal_never_names_a_utility_conflict(
    session, adopted, tmp_path, store
):
    """A key date table says nothing about the customer's conflicts.

    A Utility Conflict also carries a `need_date`. An adapter that read the
    accepted record's subjects without asking which namespace they are in would
    declare every conflict absent from a schedule export and propose removing
    the whole record.
    """

    _capture(
        session,
        adopted,
        KEY_DATE_ROWS,
        tmp_path,
        name="a",
        is_complete_enumerative_source=True,
        row_accounting_sealed=True,
    )

    assert [
        row.target_subject_identity
        for row in _key_date_deltas(session, adopted)
        if row.change_type == "apparent_removal"
    ] == []
    assert {UC1, UC2, UC3}.isdisjoint(
        {row.target_subject_identity for row in _key_date_deltas(session, adopted)}
    )


# --- the derived impact -----------------------------------------------------


def test_impact_readback_is_idempotent_versioned_and_visibly_stale(
    session, adopted, tmp_path, store
):
    from dataclasses import replace
    from corridor.impact_derivations import append_impact_derivation, read_impact_derivations

    first = _capture(session, adopted, KEY_DATE_ROWS, tmp_path, name="a")
    _accept_every_key_date(session, adopted, first)
    capture = _capture(session, adopted, moved(KEY_DATE_ROWS, "RELO-CONSTR", "2026-06-30"), tmp_path, name="b")
    (impact,) = capture.impacts
    (reading,) = read_impact_derivations(session, project_id=adopted.id, delta_ids=capture.delta_ids)
    assert reading.affected_constraint_ids == (UC1, UC2)
    assert reading.affected_key_dates == ("RELO-CONSTR",)
    assert not reading.stale
    from corridor.packet_review import read_review_items
    review = read_review_items(session, project_id=adopted.id, as_of=DECIDED_AT)
    assert [item.id for packet in review.items for child in packet.children
            if child.delta_id == impact.delta_id for item in child.impacts] == [reading.id]
    assert len(reading.derivation_sha256) == 64
    replay_id = append_impact_derivation(session, project_id=adopted.id, delta_id=impact.delta_id,
        derivation=replace(impact.derivation, evaluated_at=datetime(2026, 9, 9, tzinfo=timezone.utc)))
    assert replay_id == reading.id
    changed_id = append_impact_derivation(session, project_id=adopted.id, delta_id=impact.delta_id,
        derivation=replace(impact.derivation, rule_version="v2"))
    assert changed_id != reading.id
    assert len(read_impact_derivations(session, project_id=adopted.id, delta_ids=capture.delta_ids)) == 2
    assert read_impact_derivations(session, project_id=-1, delta_ids=capture.delta_ids) == ()
    _accept_every_key_date(session, adopted, capture)
    assert all(item.stale for item in read_impact_derivations(session, project_id=adopted.id, delta_ids=capture.delta_ids))


def test_impact_names_the_constraints_whose_required_by_is_the_moved_key_date(
    session, adopted, tmp_path, store
):
    first = _capture(session, adopted, KEY_DATE_ROWS, tmp_path, name="a")
    _accept_every_key_date(session, adopted, first)

    capture = _capture(
        session,
        adopted,
        moved(KEY_DATE_ROWS, "RELO-CONSTR", "2026-06-30"),
        tmp_path,
        name="b",
    )

    (impact,) = capture.impacts
    assert impact.derivation.rule == IMPACT_RULE
    assert impact.derivation.affected_key_dates == ("RELO-CONSTR",)
    # Two of the three adopted conflicts are required by 2026-03-31; the third
    # serves a different key date and is not reached.
    assert impact.derivation.affected_constraint_ids == (UC1, UC2)
    assert impact.derivation.inputs["accepted_key_date"] == "2026-03-31"
    assert impact.derivation.inputs["proposed_key_date"] == "2026-06-30"
    # The evaluation instant is the one the database stamped on the registered
    # export, not a clock this process read.
    document = session.get(Document, capture.document_id)
    assert impact.derivation.evaluated_at == document.created_at


def test_impact_refuses_cross_project_conflicting_results_and_raw_mutation(
    session, adopted, tmp_path, store
):
    from dataclasses import replace
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from corridor.impact_derivations import append_impact_derivation

    capture = _capture(session, adopted, KEY_DATE_ROWS, tmp_path, name="a")
    impact = capture.impacts[0]
    with pytest.raises(DBAPIError, match="outside project"), session.begin_nested():
        append_impact_derivation(session, project_id=-1, delta_id=impact.delta_id, derivation=impact.derivation)
    with pytest.raises(DBAPIError, match="different consequences"), session.begin_nested():
        append_impact_derivation(session, project_id=adopted.id, delta_id=impact.delta_id,
            derivation=replace(impact.derivation, affected_constraint_ids=("invented",)))
    with pytest.raises(DBAPIError, match="immutable source append"), session.begin_nested():
        session.execute(text("update proposed_delta_impact_derivations set rule='rewritten' where project_id=:p"), {"p": adopted.id})
    with pytest.raises(DBAPIError, match="permission denied"), session.begin_nested():
        session.execute(text("set local role corridor_worker"))
        session.execute(text("delete from proposed_delta_impact_derivations where project_id=:p"), {"p": adopted.id})


def test_the_impact_derivation_is_retained_with_the_act_that_produced_it(
    session, adopted, tmp_path, store
):
    first = _capture(session, adopted, KEY_DATE_ROWS, tmp_path, name="a")
    _accept_every_key_date(session, adopted, first)

    capture = _capture(
        session,
        adopted,
        moved(KEY_DATE_ROWS, "RELO-CONSTR", "2026-06-30"),
        tmp_path,
        name="b",
    )

    entry = session.scalars(
        select(AuditLog)
        .where(
            AuditLog.action == audit.CAPTURE_KEY_DATE_TABLE,
            AuditLog.entity_id == capture.document_id,
        )
        .order_by(AuditLog.id.desc())
    ).first()
    assert entry is not None
    (retained,) = entry.after_json["impact_derivations"]
    assert retained["delta_id"] == capture.delta_ids[0]
    assert retained["rule"] == IMPACT_RULE
    assert retained["affected_constraint_ids"] == [UC1, UC2]
    assert retained["affected_key_dates"] == ["RELO-CONSTR"]
    assert entry.after_json["comparison_rule_version"] == KEY_DATE_COMPARISON_RULE_VERSION
    assert entry.after_json["values_agreed"] == 2


def test_a_key_date_the_record_does_not_hold_reaches_nothing_and_says_so(
    session, adopted, tmp_path, store
):
    capture = _capture(session, adopted, KEY_DATE_ROWS, tmp_path, name="a")

    assert len(capture.impacts) == 3
    for impact in capture.impacts:
        assert impact.derivation.affected_constraint_ids == ()
        assert impact.derivation.inputs["accepted_key_date"] is None
        assert "no accepted date" in impact.derivation.inputs["reads"]


# --- the delivery boundary --------------------------------------------------


def test_an_export_delivered_for_another_project_is_refused(
    session, adopted, tmp_path, store
):
    other = Project(
        slug=f"key-date-other-{uuid4().hex[:8]}",
        name="Other",
        is_synthetic=True,
    )
    session.add(other)
    session.flush()
    body = key_date_workbook(tmp_path / "a.xlsx", KEY_DATE_ROWS)
    staged, envelope = deliver_export(
        session, other, body, external_identity="schedule export a"
    )

    with pytest.raises(KeyDateTableRefused, match="another project"):
        capture_key_date_table(
            session,
            project=adopted,
            staged=staged,
            envelope=envelope,
            principal=PRINCIPAL,
            images_dir=tmp_path / "images",
        )
