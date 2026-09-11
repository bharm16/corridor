"""What capturing through the commands refuses that the ORM fixture accepted.

``source_capture_support`` exists to stop a fixture building a Project Record
shape the product could not, so the properties worth proving are the ones the
hand-built triple skipped.  Each refusal below is paired, in the same test,
with the ORM write the fifteen modules used to perform: the contrast is the
argument, not the refusal on its own.

Every scope check these commands make turns out to be enforced twice: a
document, run, or segment outside its project is a foreign key as well as a
command check, and a Fact with no digest is a not-null constraint.  That family
was never the gap.  The gap is the digest of the exact text, the value's
reproduction from the cell, and idempotent replay — and one test below states
the doubly-enforced case explicitly so the distinction is not lost again.
"""

from __future__ import annotations

from datetime import date
from hashlib import sha256

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from corridor.db_roles import RECORD_DECISION_ROLE, WORKER_CAPABILITY_LOGIN
from corridor.materializer import (
    FactReplayMismatch,
    FactValidationError,
    materialize_segment_value,
)
from corridor.models import Fact, FactSource, Project, SourceSegment
from corridor.source_append import SegmentValues, append_source_segments

from harness_support import as_record_decision_role
from source_capture_support import Rendition, _as_capability_login


SUBJECT = "Utility Conflicts!3"


def _orm_segment(session, project, document, *, value, cell, digest, ordinal=90):
    """The write the fifteen modules performed: a segment straight through the ORM."""

    row = SourceSegment(
        project_id=project.id,
        document_id=document.id,
        kind="spreadsheet_cell",
        exact_text=value,
        content_sha256=digest,
        ordinal=ordinal,
        sheet_name="Utility Conflicts",
        cell_range=cell,
    )
    session.add(row)
    session.flush()
    return row


def test_a_digest_that_is_not_its_words_is_refused_where_the_orm_took_the_triple(
    session, project
):
    """The ORM fixture stored a cell it could not have read; the command cannot."""

    rendition = Rendition(session, project, "ucm-digest.xlsx")
    forged = sha256(b"words that are not in this cell").hexdigest()

    stored = _orm_segment(
        session, project, rendition.document,
        value="1200+00", cell="Z1", digest=forged,
    )
    stated = Fact(
        project_id=project.id,
        document_id=rendition.document.id,
        extraction_run_id=rendition.run.id,
        fact_type="station_from",
        subject_kind="source_row",
        subject_key=SUBJECT,
        text_value="1200+00",
        transformation="trim_cell_text_v1",
        recorded_by="extractor:orm_fixture_v1",
        content_sha256=sha256(b"orm:station_from:1200+00").hexdigest(),
    )
    session.add(stated)
    session.flush()
    session.add(
        FactSource(
            project_id=project.id,
            document_id=rendition.document.id,
            fact_id=stated.id,
            source_segment_id=stored.id,
            role="value_source",
            ordinal=1,
        )
    )
    session.flush()
    assert stored.content_sha256 != sha256(b"1200+00").hexdigest()

    with pytest.raises(FactReplayMismatch):
        materialize_segment_value(session, "station_from", stored)

    with pytest.raises(DBAPIError, match="digest does not match its exact text"):
        with session.begin_nested():
            with _as_capability_login(session):
                append_source_segments(
                    session,
                    project_id=project.id,
                    document_id=rendition.document.id,
                    recorded_verbal_origin_id=None,
                    segments=(
                        SegmentValues(
                            kind="spreadsheet_cell",
                            exact_text="1200+00",
                            content_sha256=forged,
                            ordinal=91,
                            sheet_name="Utility Conflicts",
                            cell_range="Z2",
                        ),
                    ),
                )


def test_a_fact_value_its_own_cell_does_not_reproduce_cannot_be_expressed(
    session, project
):
    """``append_fact`` has no parameter that takes a value literal (#446)."""

    rendition = Rendition(session, project, "ucm-unreproducible.xlsx")
    _fact, segment = rendition.capture(
        fact_type="station_from", value="1200+00", subject_key=SUBJECT
    )

    stated = Fact(
        project_id=project.id,
        document_id=rendition.document.id,
        extraction_run_id=rendition.run.id,
        fact_type="station_from",
        subject_kind="source_row",
        subject_key=SUBJECT,
        text_value="1300+00",
        transformation="trim_cell_text_v1",
        recorded_by="extractor:orm_fixture_v1",
        content_sha256=sha256(b"stated:1300+00").hexdigest(),
    )
    session.add(stated)
    session.flush()
    session.add(
        FactSource(
            project_id=project.id,
            document_id=rendition.document.id,
            fact_id=stated.id,
            source_segment_id=segment.id,
            role="value_source",
            ordinal=1,
        )
    )
    session.flush()
    assert stated.text_value == "1300+00" != segment.exact_text

    captured, captured_segment = rendition.capture(
        fact_type="station_from", value="1300+00", subject_key=SUBJECT, cell="B1"
    )
    assert captured.text_value == captured_segment.exact_text == "1300+00"
    assert session.scalars(
        select(FactSource.source_segment_id).where(FactSource.fact_id == captured.id)
    ).all() == [captured_segment.id]


def test_capturing_one_cell_twice_replays_where_the_orm_could_only_duplicate(
    session, project
):
    rendition = Rendition(session, project, "ucm-replay.xlsx")
    first, first_segment = rendition.capture(
        fact_type="station_from", value="1200+00", subject_key=SUBJECT, cell="A9"
    )
    again, again_segment = rendition.capture(
        fact_type="station_from", value="1200+00", subject_key=SUBJECT, cell="A9"
    )

    assert (again.id, again_segment.id) == (first.id, first_segment.id)
    assert session.scalar(
        select(func.count())
        .select_from(SourceSegment)
        .where(SourceSegment.document_id == rendition.document.id)
    ) == 1

    with pytest.raises(IntegrityError, match="uq_source_segments_spreadsheet_locator"):
        with session.begin_nested():
            _orm_segment(
                session, project, rendition.document,
                value="1200+00", cell="A9",
                digest=sha256(b"1200+00").hexdigest(),
            )


def test_rebinding_one_cell_to_other_words_is_refused(session, project):
    rendition = Rendition(session, project, "ucm-rebind.xlsx")
    rendition.capture(
        fact_type="station_from", value="1200+00", subject_key=SUBJECT, cell="A9"
    )

    with pytest.raises(DBAPIError, match="locator is already bound to different content"):
        with session.begin_nested():
            rendition.capture(
                fact_type="station_from", value="1300+00", subject_key=SUBJECT,
                cell="A9",
            )


def test_a_cell_of_another_projects_rendition_is_refused(session, project):
    """The command and a foreign key agree here; the ORM path never had this gap."""

    other = Project(slug="capture-other", name="Other", is_synthetic=True)
    session.add(other)
    session.flush()
    elsewhere = Rendition(session, other, "other-ucm.xlsx")

    with pytest.raises(DBAPIError, match="document is outside its project"):
        with session.begin_nested():
            with _as_capability_login(session):
                append_source_segments(
                    session,
                    project_id=project.id,
                    document_id=elsewhere.document.id,
                    recorded_verbal_origin_id=None,
                    segments=(
                        SegmentValues(
                            kind="spreadsheet_cell",
                            exact_text="1200+00",
                            content_sha256=sha256(b"1200+00").hexdigest(),
                            ordinal=1,
                            sheet_name="Utility Conflicts",
                            cell_range="A1",
                        ),
                    ),
                )

    with pytest.raises(IntegrityError, match="fk_source_segments_document_scope"):
        with session.begin_nested():
            _orm_segment(
                session, project, elsewhere.document,
                value="1200+00", cell="A1", digest=sha256(b"1200+00").hexdigest(),
            )


def test_the_capture_login_holds_no_write_on_the_tables_it_captures_into(
    session, project
):
    rendition = Rendition(session, project, "ucm-login.xlsx")

    with pytest.raises(DBAPIError, match="permission denied for table source_segments"):
        with session.begin_nested():
            with _as_capability_login(session):
                session.add(
                    SourceSegment(
                        project_id=project.id,
                        document_id=rendition.document.id,
                        kind="spreadsheet_cell",
                        exact_text="1200+00",
                        content_sha256=sha256(b"1200+00").hexdigest(),
                        ordinal=1,
                        sheet_name="Utility Conflicts",
                        cell_range="A1",
                    )
                )
                session.flush()

    assert session.scalar(text("select current_user")) != WORKER_CAPABILITY_LOGIN


def test_a_capture_supplies_its_own_principal_and_hands_back_the_callers(
    session, project
):
    """The caller's role is neither what appends nor what the append leaves behind."""

    rendition = Rendition(session, project, "ucm-principal.xlsx")

    with as_record_decision_role(session):
        assert session.scalar(text("select current_user")) == RECORD_DECISION_ROLE
        fact, segment = rendition.capture(
            fact_type="station_from", value="1200+00", subject_key=SUBJECT
        )
        assert session.scalar(text("select current_user")) == RECORD_DECISION_ROLE

    assert (fact.text_value, segment.exact_text) == ("1200+00", "1200+00")
    assert session.scalar(text("select current_user")) != RECORD_DECISION_ROLE


def test_the_fact_takes_its_transformation_and_value_from_the_released_contract(
    session, project
):
    rendition = Rendition(session, project, "ucm-contract.xlsx")

    station, station_segment = rendition.capture(
        fact_type="station_from", value="  1200+00  ", subject_key=SUBJECT
    )
    committed, _ = rendition.capture(
        fact_type="committed_date", value="2026-12-15", subject_key=SUBJECT
    )

    assert (station.transformation, station.text_value) == (
        "trim_cell_text_v1", "1200+00",
    )
    assert station_segment.exact_text == "  1200+00  "
    assert (committed.transformation, committed.date_value, committed.text_value) == (
        "iso_date_cell_v1", date(2026, 12, 15), None,
    )
    assert (station.subject_kind, committed.subject_kind) == ("source_row", "source_row")


def test_a_cell_that_does_not_materialize_its_fact_type_is_refused(session, project):
    rendition = Rendition(session, project, "ucm-bad-date.xlsx")

    with pytest.raises(FactValidationError, match="not an ISO calendar date"):
        rendition.capture(
            fact_type="committed_date", value="next spring", subject_key=SUBJECT
        )
