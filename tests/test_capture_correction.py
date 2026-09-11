"""Reporting a wrong extraction, bound to the capture that was on the screen (#836).

ADR-0100 adds one ancillary action to the focused Review form and is explicit
about what it may not become.  The properties under test are the four clauses
that decision turns on:

* the control reaches every change a Review item decides against a capture --
  a batch's own change included, and whether or not another source captured a
  usable value -- while one delta is still offered it by exactly one item; and
  it is not a fifth primary decision: it cannot be submitted as an answer, and
  recording one resolves nothing;
* the request names the **exact** capture, proved the way ADR-0100 states it:
  a second capture of the same document and field arrives afterwards, the
  reading moves to it, and opening the request still shows the original
  capture and its original evidence;
* nothing the request records overwrites the disputed Source Fact or its
  Source Segment; and
* a correction that establishes no change cannot leave through either exit
  the declared Proposed Delta lifecycle offered, which is why ADR-0101 gave it
  a relationship of its own; the lifecycle half is proved in
  ``tests/test_capture_correction_retirement.py``.

The fixture is an organization change, which the partition holds out of its
source revision's batch.  That makes it a focused item carrying exactly one
captured source value and no alternative to apply instead -- ADR-0100's own
worked case.  ``ordinary_change`` adds a second change of the same revision
beside it, which is the other half of the availability rule: that one is
decided inside the revision's batch, and the organization change is listed
there read-only by the item that does not decide it.

Nothing here reads a clock.  Every cutoff and instant is declared.
"""

from __future__ import annotations

from dataclasses import replace
import html
import re
from datetime import datetime, timezone
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from corridor.capture_correction import (
    CONTROL_INTERPRETATION,
    CONTROL_PASSAGE,
    CORRECTION_CONTROL,
    CORRECTION_SUPPORTING_TEXT,
    PASSAGE_MATCH_LIMIT,
    CaptureCorrectionRefused,
    build_correction_request,
    challenged_capture,
    correction_idempotency_key,
    offers_correction,
    passage_choices,
    record_correction_request,
    reported_corrections,
    resolve_challenged_capture,
)
from corridor.delta_resolution import live_delta_status
from corridor.models import (
    CaptureCorrectionRequest,
    DeltaDeferral,
    DeltaDisposition,
    DeltaRecordDecision,
    DeltaSupersession,
    Fact,
    Project,
    SourceSegment,
)
from corridor.packet_review import (
    FOCUSED_OUTCOMES,
    FocusedAnswer,
    ReviewScreenRefused,
    focused_request,
    read_review_items,
)
from corridor.principals import HumanPrincipal
from corridor.review_packet_reading import HELD_OUT_OWNER_MISMATCH
from corridor.web.app import (
    app,
    get_human_principal,
    get_review_clock,
    get_session,
)

from browser_session_support import form_fields, submit_form

from packet_review_support import (
    Rendition,
    accept_baseline_fact,
    append_deltas,
    modify,
    register_baseline,
    register_output_template,
    register_source_row,
    subject,
    support,
)


ALICE = HumanPrincipal("local:alice")
CUTOFF = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
REPORTED_AT = datetime(2026, 9, 3, 11, 30, tzinfo=timezone.utc)
DECIDED_AT = datetime(2026, 9, 3, 11, 0, tzinfo=timezone.utc)
EXPECTED = "the owner column on this row reads AT&T Texas, unchanged"


@pytest.fixture
def project(member_project) -> Project:
    """The roster membership the #331 access gate wants before a screen answers."""

    return member_project(ALICE)


class Misread:
    """One adopted row, and a revision whose organization change was captured.

    ``incoming`` is the revised workbook the capture came from, so a test that
    needs a *second* capture of the same document and field appends it to this
    same rendition.

    ``ordinary_change`` appends a second change of that same revision, whose
    shape the partition does not hold out.  The revision then has both an
    item that batches its ordinary change and an item that decides the
    organization change on its own, which is the reading the availability
    rule and ADR-0085's exactly-once rule are both read against.
    """

    def __init__(
        self, session: Session, project: Project, *, ordinary_change: bool = False
    ):
        self.session = session
        self.project = project
        self.adopted = Rendition(session, project, "ucm-2026-08.xlsx")
        accepted, _ = self.adopted.capture(
            fact_type="station_from", value="1001+00", subject_key=subject(1)
        )
        revision = accept_baseline_fact(session, project, accepted)
        baseline = register_baseline(
            session, project, self.adopted.document, revision
        )
        register_source_row(
            session, project, baseline, row_number=1, business_identity="U-001"
        )
        register_output_template(
            session, project, identity="district-ucm-template", version="v3"
        )
        self.incoming = Rendition(session, project, "ucm-2026-09.xlsx")
        self.fact, self.segment = self.incoming.capture(
            fact_type="external_org",
            value="AT&T Texas (SWBT)",
            subject_key=subject(1),
        )
        support(session, project, self.fact, self.segment)
        values = [
            modify(
                subject_key=subject(1),
                field_name="external_org",
                accepted_value="AT&T Texas",
                proposed_value="AT&T Texas (SWBT)",
                baseline_revision=None,
            )
        ]
        self.batched_fact: Fact | None = None
        if ordinary_change:
            self.batched_fact, batched_segment = self.incoming.capture(
                fact_type="station_from", value="1002+00", subject_key=subject(1)
            )
            support(session, project, self.batched_fact, batched_segment)
            values.append(
                modify(
                    subject_key=subject(1),
                    field_name="station_from",
                    accepted_value="1001+00",
                    proposed_value="1002+00",
                    baseline_revision=revision,
                )
            )
        append_deltas(
            session,
            project,
            self.incoming,
            source_revision="2026-09",
            values=values,
        )

    def batch(self):
        """The revision's own batch, which decides the ordinary change."""

        reading = read_review_items(
            self.session, project_id=self.project.id, as_of=CUTOFF
        )
        (item,) = [row for row in reading.items if row.batched]
        return reading, item

    def item(self):
        """The held-out organization change, and the reading that carries it."""

        reading = read_review_items(
            self.session, project_id=self.project.id, as_of=CUTOFF
        )
        (item,) = [
            row
            for row in reading.items
            if row.held_out_reason == HELD_OUT_OWNER_MISMATCH
        ]
        return reading, item

    def request(self, *, passage: SourceSegment | None = None):
        """The correction request this coordinator would submit, as built."""

        _, item = self.item()
        (child,) = item.children
        return build_correction_request(
            self.session,
            item,
            child,
            principal=ALICE,
            reported_at=REPORTED_AT,
            selected_source_segment_id=(passage or self.segment).id,
            expected_interpretation=EXPECTED,
        )


def _cross_source(session: Session, project: Project):
    """Two retained sources answering one field, so an alternative capture exists."""

    adopted = Rendition(session, project, "ucm-2026-08.xlsx")
    accepted, _ = adopted.capture(
        fact_type="committed_date", value="2026-11-01", subject_key=subject(1)
    )
    revision = accept_baseline_fact(session, project, accepted)
    baseline = register_baseline(session, project, adopted.document, revision)
    register_source_row(
        session, project, baseline, row_number=1, business_identity="U-001"
    )
    register_output_template(
        session, project, identity="district-ucm-template", version="v3"
    )
    for name, family, source_revision, value in (
        ("ucm-2026-09.xlsx", "ucm-workbook", "2026-09", "2026-12-15"),
        ("minutes-2026-09-08.pdf", "meeting-minutes", "2026-09-08", "2027-01-20"),
    ):
        rendition = Rendition(session, project, name)
        fact, segment = rendition.capture(
            fact_type="committed_date", value=value, subject_key=subject(1)
        )
        support(session, project, fact, segment)
        append_deltas(
            session,
            project,
            rendition,
            source_family=family,
            source_revision=source_revision,
            values=[
                modify(
                    subject_key=subject(1),
                    field_name="committed_date",
                    accepted_value="2026-11-01",
                    proposed_value=value,
                    baseline_revision=revision,
                )
            ],
        )
    reading = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    (item,) = [row for row in reading.items if len(row.children) > 1]
    return reading, item


# --- offered only where there is nothing else to apply ---------------------


def test_the_control_is_offered_where_no_other_captured_value_exists(
    session: Session, project: Project
):
    """ADR-0100's case: the source is clear and there is nothing to choose."""

    built = Misread(session, project)
    _, item = built.item()
    (child,) = item.children

    assert item.focused is True
    assert item.alternatives_for(child.delta_id) == ()
    assert offers_correction(item, child) is True
    assert CORRECTION_CONTROL == "Report an extraction error"
    assert "does not change the record" in CORRECTION_SUPPORTING_TEXT


def test_the_control_is_offered_although_another_source_captured_a_value(
    session: Session, project: Project
):
    """Another usable value does not make a misread capture right (2026-09-11).

    Edit and apply is available here and is still the act for "another source
    already carries the right value". It answers what the record should show;
    it does not report that Corridor read this document wrong, and applying it
    would leave the misreading captured exactly as it is.
    """

    _, item = _cross_source(session, project)

    for child in item.children:
        assert item.alternatives_for(child.delta_id) != ()
        assert offers_correction(item, child) is True
        assert challenged_capture(session, item, child).fact_id is not None


def test_a_change_this_item_does_not_decide_carries_no_control_here(
    session: Session, project: Project
):
    """ADR-0085's exactly-once rule, kept by the item that decides the change.

    The batch lists the held-out organization change read-only and names the
    item that decides it. Neither the read-only row nor the deciding item's
    own row is offered the control by the batch, so widening the control to a
    batch's changes gives one delta one control on one page rather than two.
    """

    built = Misread(session, project, ordinary_change=True)
    _, batch = built.batch()
    _, held_out = built.item()
    (decided_elsewhere,) = held_out.children
    (listed_read_only,) = [
        row
        for row in batch.held_out_children
        if row.delta_id == decided_elsewhere.delta_id
    ]

    assert offers_correction(held_out, decided_elsewhere) is True
    assert offers_correction(batch, listed_read_only) is False
    # The same change, by the row the item that decides it carries: still not
    # this item's to report, because this item does not decide it.
    assert offers_correction(batch, decided_elsewhere) is False
    with pytest.raises(CaptureCorrectionRefused) as refused:
        challenged_capture(session, batch, decided_elsewhere)
    assert refused.value.reason == "not_offered"


def test_a_batched_change_is_reportable_on_the_batch_that_decides_it(
    session: Session, project: Project
):
    """An individual change inside a source revision's batch (2026-09-11)."""

    built = Misread(session, project, ordinary_change=True)
    _, batch = built.batch()
    (child,) = batch.children

    assert batch.batched is True and batch.focused is False
    assert offers_correction(batch, child) is True
    capture = challenged_capture(session, batch, child)
    assert capture.fact_id == built.batched_fact.id
    assert capture.field == "station_from"


# --- an ancillary action, never a fifth primary decision -------------------


def test_the_correction_is_not_an_answer_the_focused_form_accepts(
    session: Session, project: Project
):
    """ADR-0085's four primary decisions and secondary Defer are not extended."""

    built = Misread(session, project)
    reading, item = built.item()
    (child,) = item.children

    assert len(FOCUSED_OUTCOMES) == 5
    assert "report_extraction_error" not in FOCUSED_OUTCOMES
    with pytest.raises(ReviewScreenRefused) as refused:
        focused_request(
            reading,
            item,
            principal=ALICE,
            decided_at=DECIDED_AT,
            answers=(
                FocusedAnswer(
                    delta_id=child.delta_id, outcome="report_extraction_error"
                ),
            ),
        )
    assert "is not one of this screen's decisions" in str(refused.value)


def test_the_request_resolves_nothing_and_overwrites_nothing(
    session: Session, project: Project
):
    """It changes no accepted value and leaves the Proposed Delta open.

    The disputed capture and its Source Segment are compared against the bytes
    they held before the request, re-read rather than trusted from the objects
    already in the identity map.
    """

    built = Misread(session, project)
    _, item = built.item()
    (child,) = item.children
    fact_digest = built.fact.content_sha256
    exact_text, segment_digest = built.segment.exact_text, built.segment.content_sha256

    request = built.request()

    assert request.reported_by_principal == ALICE.subject
    assert request.reported_at == REPORTED_AT
    assert request.expected_interpretation == EXPECTED
    for model, identifier in (
        (DeltaDisposition, DeltaDisposition.delta_id),
        (DeltaDeferral, DeltaDeferral.delta_id),
        (DeltaRecordDecision, DeltaRecordDecision.delta_id),
    ):
        assert not session.scalars(
            select(model.id).where(identifier == child.delta_id)
        ).all()
    assert live_delta_status(session, child.delta_id) == "open"
    session.expire_all()
    assert session.get(Fact, request.capture.fact_id).content_sha256 == fact_digest
    retained = session.get(SourceSegment, request.capture.source_segment_id)
    assert (retained.exact_text, retained.content_sha256) == (
        exact_text,
        segment_digest,
    )


# --- the request names the exact capture ----------------------------------


def test_the_request_names_the_capture_and_the_passage_that_was_shown(
    session: Session, project: Project
):
    """Identity and evidence, both read from retained rows rather than strings."""

    built = Misread(session, project)
    request = built.request()

    assert request.capture.fact_id == built.fact.id
    assert request.capture.fact_content_sha256 == built.fact.content_sha256
    assert request.capture.source_segment_id == built.segment.id
    assert request.capture.document_id == built.incoming.document.id
    assert request.capture.document_filename == "ucm-2026-09.xlsx"
    assert request.capture.subject_identity == subject(1)
    assert request.capture.field == "external_org"
    assert request.capture.exact_text == "AT&T Texas (SWBT)"
    assert request.capture.locator is not None


def test_a_second_capture_of_the_same_document_and_field_does_not_move_the_request(
    session: Session, project: Project
):
    """ADR-0100's stated test for the exact-capture binding.

    The reading reconstructs a delta's capture by subject and field, so it
    moves to the newer Fact; the request recorded an immutable identity and
    still opens against the capture the coordinator challenged, with the exact
    text that capture cited.
    """

    built = Misread(session, project)
    request = built.request()

    later, later_segment = built.incoming.capture(
        fact_type="external_org",
        value="AT&T Texas (SBC)",
        subject_key=subject(1),
    )
    support(session, project, later, later_segment)

    _, item = built.item()
    (child,) = item.children
    assert later.id != built.fact.id
    assert child.incoming_fact_id == later.id
    assert child.source.source_segment_id == later_segment.id

    opened = resolve_challenged_capture(session, request)

    assert opened.fact_id == built.fact.id
    assert opened.fact_content_sha256 == built.fact.content_sha256
    assert opened.source_segment_id == built.segment.id
    assert opened.exact_text == "AT&T Texas (SWBT)"
    assert opened == request.capture


# --- the request the screen refuses to build -------------------------------


def test_a_passage_from_another_source_is_refused(
    session: Session, project: Project
):
    """Operations corrects a capture against bytes this source already retained."""

    built = Misread(session, project)
    elsewhere = built.adopted.segment("AT&T Texas")

    with pytest.raises(CaptureCorrectionRefused) as refused:
        built.request(passage=elsewhere)

    assert refused.value.reason == "passage_not_in_this_source"
    assert refused.value.control == CONTROL_PASSAGE


def _a_neighbouring_conflicts_own_cell(session: Session, project: Project):
    """One misread size, and the *next* Utility Conflict's own size cell.

    Two adopted rows of one sheet.  The revision misread the first row's size,
    and the second row's size is captured as *that row's* own Source Fact, so
    the cell a coordinator could point at is not merely another cell that
    happens to read plausibly: the record itself already says it describes a
    different Utility Conflict.
    """

    adopted = Rendition(session, project, "ucm-2026-08.xlsx")
    accepted, _ = adopted.capture(
        fact_type="size", value="12 in", subject_key=subject(1), cell="C1"
    )
    revision = accept_baseline_fact(session, project, accepted)
    baseline = register_baseline(session, project, adopted.document, revision)
    for row_number, identity in ((1, "U-001"), (2, "U-002")):
        register_source_row(
            session,
            project,
            baseline,
            row_number=row_number,
            business_identity=identity,
        )
    register_output_template(
        session, project, identity="district-ucm-template", version="v3"
    )
    incoming = Rendition(session, project, "ucm-2026-09.xlsx")
    misread, misread_cell = incoming.capture(
        fact_type="size", value="16 in", subject_key=subject(1), cell="C1"
    )
    support(session, project, misread, misread_cell)
    neighbour, neighbours_cell = incoming.capture(
        fact_type="size", value="6 in", subject_key=subject(2), cell="C2"
    )
    support(session, project, neighbour, neighbours_cell)
    append_deltas(
        session,
        project,
        incoming,
        source_revision="2026-09",
        values=[
            modify(
                subject_key=subject(1),
                field_name="size",
                accepted_value="12 in",
                proposed_value="16 in",
                baseline_revision=revision,
            )
        ],
    )
    reading = read_review_items(session, project_id=project.id, as_of=CUTOFF)
    (item,) = reading.items
    (child,) = item.children
    return item, child, neighbours_cell


@pytest.mark.xfail(
    strict=True,
    reason="the product accepts it: `build_correction_request` checks only "
    "that the passage belongs to this document and this project "
    "(capture_correction.py:393-406), so a correction from another Utility "
    "Conflict's own cell is built, `correct_captured_reading` writes a Source "
    "Fact under the challenged capture's subject with that cell's words, holds "
    "it there with a supported value_support assessment naming only that cell, "
    "retires the proposal and raises a replacement proposing the neighbour's "
    "value onto this conflict. Reported, not worked around; the marking is "
    "strict so it fails the moment a refusal exists.",
)
def test_another_conflicts_valid_cell_cannot_support_this_conflict(
    session: Session, project: Project
):
    """A plausible number somewhere else in the file is not evidence here.

    ``PASSAGE_NOT_IN_THIS_SOURCE`` establishes that a correction is grounded in
    *this document's* retained bytes.  Being in the same document is necessary
    and is not sufficient: a correction re-reads the passage a coordinator
    names and records the result as a Source Fact about the challenged
    capture's own subject, held to that passage by a ``value_support``
    assessment.  So a passage the record already attributes to a different
    Utility Conflict establishes support for a conflict it says nothing about,
    and nothing in the request declares a relationship under which it could.

    What that means for the fixture the core journey walks is the reason this
    test exists: "another cell in the same document contains a plausible
    value" cannot stand in for a misread capture, because it does not
    establish that the cell describes the same Utility Conflict.  The journey's
    correction scenarios now inject a declared extraction fault and point the
    correction at each conflict's *own* cell, which is why they no longer
    depend on the answer to this.

    What a permitted evidentiary relationship would be is a maintainer's
    decision, not this test's: the refusal's words, and whether one row may
    ever be evidence about another, belong with ADR-0100 and ADR-0101.
    """

    item, child, neighbours_cell = _a_neighbouring_conflicts_own_cell(
        session, project
    )

    with pytest.raises(CaptureCorrectionRefused) as refused:
        build_correction_request(
            session,
            item,
            child,
            principal=ALICE,
            reported_at=REPORTED_AT,
            selected_source_segment_id=neighbours_cell.id,
            expected_interpretation="this conflict's size is 6 in",
        )

    assert refused.value.control == CONTROL_PASSAGE


@pytest.mark.parametrize(
    "passage_id, interpretation, reason, control",
    [
        (None, EXPECTED, "passage_required", CONTROL_PASSAGE),
        (0, "   ", "interpretation_required", CONTROL_INTERPRETATION),
    ],
)
def test_an_incomplete_request_names_the_control_that_holds_it(
    session: Session,
    project: Project,
    passage_id,
    interpretation,
    reason,
    control,
):
    """Each refusal is a shape the coordinator can correct without losing the rest."""

    built = Misread(session, project)
    _, item = built.item()
    (child,) = item.children

    with pytest.raises(CaptureCorrectionRefused) as refused:
        build_correction_request(
            session,
            item,
            child,
            principal=ALICE,
            reported_at=REPORTED_AT,
            selected_source_segment_id=(
                built.segment.id if passage_id == 0 else passage_id
            ),
            expected_interpretation=interpretation,
        )

    assert (refused.value.reason, refused.value.control) == (reason, control)
    assert refused.value.delta_id == child.delta_id


# --- the no-change outcome, and why its exit is a relationship of its own ---


def test_neither_lifecycle_exit_can_carry_a_no_change_correction(
    session: Session, project: Project
):
    """Why ADR-0101 added a relationship instead of reusing one of the two.

    This module's own refusal is gone, because the thing it was holding open
    now exists (``capture_correction_retirement``, and the properties ADR-0101
    requires are proved in ``tests/test_capture_correction_retirement.py``).
    What stays here is the evidence that asked for it, because it is evidence
    about *this* seam: the only exit the lifecycle offered that is not a
    coordinator decision is ``DeltaSupersession``, and PostgreSQL refuses one
    that names no superseding delta, inbound thread reading or minutes
    capture -- which is every correction of the same source version. A later
    change that thinks it can reuse supersession fails here first.
    """

    built = Misread(session, project)
    request = built.request()

    with pytest.raises(IntegrityError, match="ck_delta_supersessions_successor"):
        with session.begin_nested():
            session.add(
                DeltaSupersession(
                    project_id=project.id,
                    prior_delta_id=request.capture.delta_id,
                    reason="capture_corrected",
                )
            )
            session.flush()


# --- the report, retained --------------------------------------------------


def test_a_recorded_report_retains_the_capture_the_passage_and_the_reason(
    session: Session, project: Project
):
    """Everything ADR-0100 says the process preserves, on the row it writes."""

    built = Misread(session, project)
    elsewhere = built.incoming.segment("AT&T Texas", cell="D1")
    request = built.request(passage=elsewhere)

    recorded = record_correction_request(session, request)

    assert recorded.project_id == project.id
    assert recorded.fact_id == built.fact.id
    assert recorded.fact_content_sha256 == built.fact.content_sha256
    assert recorded.document_id == built.incoming.document.id
    # The passage the capture cited and the passage the coordinator chose are
    # different columns, because reading the wrong cell is the defect reported.
    assert recorded.source_segment_id == built.segment.id
    assert recorded.selected_source_segment_id == elsewhere.id
    assert recorded.expected_interpretation == EXPECTED
    assert recorded.reported_by_principal == ALICE.subject
    assert recorded.reported_at == REPORTED_AT
    assert recorded.idempotency_key == correction_idempotency_key(request)


def test_the_same_report_submitted_twice_replays(
    session: Session, project: Project
):
    """A resubmitted form records the report already written, never a second."""

    built = Misread(session, project)

    first = record_correction_request(session, built.request())
    again = record_correction_request(session, built.request())

    assert again.id == first.id
    assert session.scalar(
        select(func.count())
        .select_from(CaptureCorrectionRequest)
        .where(CaptureCorrectionRequest.project_id == project.id)
    ) == 1


def test_the_runtime_cannot_write_a_report_except_through_the_command(
    session: Session, project: Project
):
    """The append boundary, not a convention this module asks callers to keep."""

    built = Misread(session, project)
    request = built.request()

    with pytest.raises(DBAPIError):
        with session.begin_nested():
            session.add(
                CaptureCorrectionRequest(
                    project_id=project.id,
                    delta_id=request.capture.delta_id,
                    document_id=built.incoming.document.id,
                    fact_id=built.fact.id,
                    fact_content_sha256=built.fact.content_sha256,
                    source_segment_id=built.segment.id,
                    selected_source_segment_id=built.segment.id,
                    expected_interpretation=EXPECTED,
                    reported_by_principal=ALICE.subject,
                    reported_at=REPORTED_AT,
                    idempotency_key="raw-insert",
                )
            )
            session.flush()


def test_the_command_refuses_a_passage_from_another_source(
    session: Session, project: Project
):
    """The same rule the Python refuses, held where a caller cannot go round it."""

    built = Misread(session, project)
    request = built.request()
    elsewhere = built.adopted.segment("AT&T Texas")
    foreign = replace(request, selected_source_segment_id=int(elsewhere.id))

    with pytest.raises(CaptureCorrectionRefused) as refused:
        with session.begin_nested():
            record_correction_request(session, foreign)

    assert refused.value.reason == "passage_not_in_this_source"
    assert "capture_correction:" not in str(refused.value)


def test_a_second_capture_does_not_move_the_retained_report(
    session: Session, project: Project
):
    """The binding survives the round trip through the store, not only in memory."""

    built = Misread(session, project)
    recorded = record_correction_request(session, built.request())

    later, later_segment = built.incoming.capture(
        fact_type="external_org", value="AT&T Texas (SBC)", subject_key=subject(1)
    )
    support(session, project, later, later_segment)
    _, item = built.item()
    (child,) = item.children

    assert child.incoming_fact_id == later.id
    session.expire_all()
    stored = session.get(CaptureCorrectionRequest, recorded.id)
    assert stored.fact_id == built.fact.id
    assert stored.source_segment_id == built.segment.id
    assert stored.fact_content_sha256 == built.fact.content_sha256
    (standing,) = reported_corrections(
        session, project_id=project.id, delta_ids=[child.delta_id]
    )[child.delta_id]
    assert (standing.fact_id, standing.reported_by_principal) == (
        built.fact.id,
        ALICE.subject,
    )


def test_the_passages_offered_are_this_sources_own_with_the_cited_one_marked(
    session: Session, project: Project
):
    """The picker is bounded to the capture's own retained source, in source order."""

    built = Misread(session, project)
    built.incoming.segment("AT&T Texas", cell="D1")
    built.adopted.segment("somewhere else entirely")
    _, item = built.item()
    (child,) = item.children

    offered = passage_choices(session, challenged_capture(session, item, child))

    assert [choice.exact_text for choice in offered.choices] == [
        "AT&T Texas (SWBT)",
        "AT&T Texas",
    ]
    assert [choice.cited for choice in offered.choices] == [True, False]
    assert (offered.searched, offered.not_shown) == ("", 0)


def test_a_search_reaches_a_passage_the_opening_window_does_not(
    session: Session, project: Project
):
    """A misread usually lands beside the right cell; when it does not, this.

    The window the picker opens on is bounded on purpose, so a legitimate cell
    thirty rows away is not in it.  Searching this document's own retained
    passages reaches it, by the words it holds or by the place the picker
    prints it under, which is what stops a coordinator having to name a
    passage by its identifier to select it.
    """

    built = Misread(session, project)
    for number in range(2, 32):
        built.incoming.segment(f"row {number} of this sheet", cell=f"D{number}")
    distant = built.incoming.segment("AT&T Texas, per the owner column", cell="D40")
    _, item = built.item()
    (child,) = item.children
    capture = challenged_capture(session, item, child)

    opening = passage_choices(session, capture)
    by_words = passage_choices(session, capture, matching="per the owner column")
    by_place = passage_choices(session, capture, matching="cell D40")

    assert distant.id not in [choice.source_segment_id for choice in opening.choices]
    assert [choice.source_segment_id for choice in by_words.choices] == [distant.id]
    assert [choice.source_segment_id for choice in by_place.choices] == [distant.id]
    assert (by_words.searched, by_words.not_shown) == ("per the owner column", 0)

    missing = passage_choices(session, capture, matching="a column nobody wrote")
    assert missing.choices == ()
    assert "No passage this source retained mentions" in missing.offered_words


def test_a_search_that_matches_more_than_it_offers_says_how_many_it_left_out(
    session: Session, project: Project
):
    """A cut list is said to be cut, rather than shown as the whole answer."""

    built = Misread(session, project)
    for number in range(2, PASSAGE_MATCH_LIMIT + 12):
        built.incoming.segment(f"owner column row {number}", cell=f"D{number}")
    _, item = built.item()
    (child,) = item.children

    found = passage_choices(
        session, challenged_capture(session, item, child), matching="owner column"
    )

    assert len(found.choices) == PASSAGE_MATCH_LIMIT
    assert found.not_shown == 10
    assert "10 further matches are not listed" in found.offered_words


# --- the screen ------------------------------------------------------------


@pytest.fixture
def web(session):
    """The app shares the test's transaction and the test's declared instant."""

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: ALICE
    app.dependency_overrides[get_review_clock] = lambda: (lambda: CUTOFF)
    with TestClient(app) as made:
        yield made
    app.dependency_overrides.clear()


def _open(web, project, key: str):
    return web.get(f"/review/{project.slug}?item={quote(key, safe='')}")


def test_the_control_renders_outside_the_answers_form_and_says_what_it_is_not(
    session: Session, project: Project, web
):
    """ADR-0100's words, its own form, and no option inside the outcome control."""

    built = Misread(session, project)
    _, item = built.item()

    body = _open(web, project, item.item_key).text

    assert CORRECTION_CONTROL in body
    assert "does not change the record" in body
    assert f'action="/review/{project.slug}/correction"' in body
    # Its own form, opened after the answers form closes: a nested form would
    # make one Save mean two acts, and the browser would not send it anyway.
    answers = body.index(f'action="/review/{project.slug}/answers"')
    closed = body.index("</form>", answers)
    assert body.index(f'action="/review/{project.slug}/correction"') > closed
    # And it is not one of the outcome control's options.
    outcome = body.index('name="answer_outcome"')
    assert CORRECTION_CONTROL not in body[outcome : body.index("</select>", outcome)]


def test_the_control_renders_where_another_source_captured_a_value(
    session: Session, project: Project, web
):
    """Both sources' captures are reportable, each on its own change."""

    _, item = _cross_source(session, project)

    body = _open(web, project, item.item_key).text

    assert CORRECTION_CONTROL in body
    for child in item.children:
        assert f'name="correction_delta" value="{child.delta_id}"' in body


def test_a_batch_offers_its_changes_and_reports_the_one_that_was_asked_for(
    session: Session, project: Project, web
):
    """One control for one capture, on a screen that decides many at once."""

    built = Misread(session, project, ordinary_change=True)
    _, batch = built.batch()
    (child,) = batch.children
    (held_out,) = built.item()[1].children
    action = f'action="/review/{project.slug}/correction"'

    listed = _open(web, project, batch.item_key).text
    # Nothing was asked about yet: the changes are offered, and no capture is
    # reported against until one of them is chosen.
    assert CORRECTION_CONTROL in listed
    assert f'<option value="{child.delta_id}"' in listed
    # And it does not offer the change this batch only lists: that one is
    # decided, and reported, on the item that holds it.
    assert f'<option value="{held_out.delta_id}"' not in listed
    assert action not in listed

    asked = web.get(
        f"/review/{project.slug}"
        f"?item={quote(batch.item_key, safe='')}&correction={child.delta_id}"
    ).text

    assert asked.count(action) == 1
    assert f'name="correction_delta" value="{child.delta_id}"' in asked
    assert f'name="correction_delta" value="{held_out.delta_id}"' not in asked


def test_the_screen_records_a_report_and_leaves_every_change_open(
    session: Session, project: Project, web
):
    """The act the ticket names, and the thing it must not do."""

    built = Misread(session, project)
    _, item = built.item()
    (child,) = item.children

    saved = web.post(
        f"/review/{project.slug}/correction",
        data={
            "item_key": item.item_key,
            "correction_delta": str(child.delta_id),
            "correction_passage": str(built.segment.id),
            "correction_interpretation": EXPECTED,
        },
    )

    assert saved.status_code == 200
    assert "Reported an extraction error" in saved.text
    (recorded,) = session.scalars(
        select(CaptureCorrectionRequest).where(
            CaptureCorrectionRequest.project_id == project.id
        )
    ).all()
    assert recorded.fact_id == built.fact.id
    assert recorded.reported_by_principal == ALICE.subject
    assert live_delta_status(session, child.delta_id) == "open"
    for model, identifier in (
        (DeltaDisposition, DeltaDisposition.delta_id),
        (DeltaDeferral, DeltaDeferral.delta_id),
        (DeltaRecordDecision, DeltaRecordDecision.delta_id),
    ):
        assert not session.scalars(
            select(model.id).where(identifier == child.delta_id)
        ).all()
    # The change is still offered, and still carries every answer it did.
    _, again = built.item()
    assert [row.delta_id for row in again.children] == [child.delta_id]


def test_an_incomplete_report_names_the_control_and_keeps_what_was_typed(
    session: Session, project: Project, web
):
    """A refusal a coordinator can correct without retyping the rest."""

    built = Misread(session, project)
    _, item = built.item()
    (child,) = item.children

    refused = web.post(
        f"/review/{project.slug}/correction",
        data={
            "item_key": item.item_key,
            "correction_delta": str(child.delta_id),
            "correction_passage": "",
            "correction_interpretation": EXPECTED,
        },
    )

    assert refused.status_code == 400
    assert f"{CONTROL_PASSAGE}-{child.delta_id}" in refused.text
    # Unescaped, because the reason a coordinator typed carries an ampersand
    # and the page is right to escape it.
    assert EXPECTED in html.unescape(refused.text)
    assert not session.scalars(select(CaptureCorrectionRequest.id)).all()


def test_the_screen_finds_a_distant_passage_and_reports_the_capture_against_it(
    session: Session, project: Project, web
):
    """The act end to end, with the passage reached by reading rather than by id."""

    built = Misread(session, project)
    for number in range(2, 32):
        built.incoming.segment(f"row {number} of this sheet", cell=f"D{number}")
    distant = built.incoming.segment("AT&T Texas, per the owner column", cell="D40")
    _, item = built.item()
    (child,) = item.children
    opened = quote(item.item_key, safe="")

    before = _open(web, project, item.item_key).text
    found = web.get(
        f"/review/{project.slug}?item={opened}"
        f"&correction={child.delta_id}&search=per+the+owner+column"
    ).text

    assert f'<option value="{distant.id}"' not in before
    assert f'<option value="{distant.id}"' in found
    # And the picker says which passages it is offering, so a shortened list
    # is never presented as everything this source holds.
    assert 'that mention "per the owner column"' in html.unescape(found)

    fields = form_fields(found, f"/review/{project.slug}/correction")
    fields["correction_passage"] = str(distant.id)
    fields["correction_interpretation"] = EXPECTED
    saved = submit_form(web, f"/review/{project.slug}/correction", fields)

    assert saved.status_code == 200
    (recorded,) = session.scalars(select(CaptureCorrectionRequest)).all()
    assert recorded.selected_source_segment_id == distant.id
    # The passages the coordinator was reading are still the ones on the page,
    # because the search came back with the report rather than resetting to
    # the window the chosen passage is not in.
    assert "per the owner column" in saved.text


def test_the_form_the_page_renders_is_the_one_the_route_accepts(
    session: Session, project: Project, web
):
    """Submitted as the page emitted it, forgery field and all (#821, #880)."""

    built = Misread(session, project)
    _, item = built.item()

    body = _open(web, project, item.item_key).text
    fields = form_fields(body, f"/review/{project.slug}/correction")
    assert fields is not None and "csrf_token" in fields
    # `form_fields` reads the hidden inputs; the two visible controls are what
    # a browser adds, so the passage comes from the option the page itself
    # marked selected rather than from a value this test chose.
    fields["correction_passage"] = re.search(
        r'<option value="(\d+)"\s+selected', body
    ).group(1)
    fields["correction_interpretation"] = EXPECTED

    saved = submit_form(web, f"/review/{project.slug}/correction", fields)

    assert saved.status_code == 200
    (recorded,) = session.scalars(select(CaptureCorrectionRequest)).all()
    # The passage the form preselected is the one the capture cited.
    assert recorded.selected_source_segment_id == built.segment.id
