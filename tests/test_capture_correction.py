"""Reporting a wrong extraction, bound to the capture that was on the screen (#836).

ADR-0100 adds one ancillary action to the focused Review form and is explicit
about what it may not become.  The properties under test are the four clauses
that decision turns on:

* the control is offered only where no alternative captured value exists, and
  it is not a fifth primary decision -- it cannot be submitted as an answer,
  and recording one resolves nothing;
* the request names the **exact** capture, proved the way ADR-0100 states it:
  a second capture of the same document and field arrives afterwards, the
  reading moves to it, and opening the request still shows the original
  capture and its original evidence;
* nothing the request records overwrites the disputed Source Fact or its
  Source Segment; and
* a correction that establishes no change has no exit in the declared Proposed
  Delta lifecycle, so the seam refuses and names the missing relationship
  rather than inventing a disposition.

The fixture is an organization change, which the partition holds out of its
source revision's batch.  That makes it a focused item carrying exactly one
captured source value and no alternative to apply instead -- the shape the
correction request exists for.

Nothing here reads a clock.  Every cutoff and instant is declared.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from corridor.capture_correction import (
    CONTROL_INTERPRETATION,
    CONTROL_PASSAGE,
    CORRECTION_CONTROL,
    CORRECTION_SUPPORTING_TEXT,
    NO_CHANGE_EXIT_UNAVAILABLE,
    CaptureCorrectionRefused,
    build_correction_request,
    challenged_capture,
    offers_correction,
    resolve_challenged_capture,
    withdraw_for_no_change,
)
from corridor.delta_resolution import live_delta_status
from corridor.models import (
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


class Misread:
    """One adopted row, and a revision whose organization change was captured.

    ``incoming`` is the revised workbook the capture came from, so a test that
    needs a *second* capture of the same document and field appends it to this
    same rendition.
    """

    def __init__(self, session: Session, project: Project):
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
        append_deltas(
            session,
            project,
            self.incoming,
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


def test_the_control_is_not_offered_where_another_source_already_captured_one(
    session: Session, project: Project
):
    """Edit and apply is the act here, so the correction request is withheld."""

    _, item = _cross_source(session, project)

    for child in item.children:
        assert item.alternatives_for(child.delta_id) != ()
        assert offers_correction(item, child) is False
        with pytest.raises(CaptureCorrectionRefused) as refused:
            challenged_capture(session, item, child)
        assert refused.value.reason == "not_offered"
        assert refused.value.delta_id == child.delta_id


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


# --- the no-change outcome, and the relationship that does not exist -------


def test_a_correction_that_establishes_no_change_has_no_recorded_exit(
    session: Session, project: Project
):
    """ADR-0100 instructs #836 to state the missing relationship, so it is named.

    The refusal is the statement, and the second half is the evidence for it:
    the only exit the lifecycle offers that is not a coordinator decision is
    ``DeltaSupersession``, and PostgreSQL refuses one that names no superseding
    delta, inbound thread reading or minutes capture -- which is every
    correction of the same source version.
    """

    built = Misread(session, project)
    request = built.request()

    with pytest.raises(CaptureCorrectionRefused) as refused:
        withdraw_for_no_change(request)

    assert refused.value.reason == "relationship_not_decided"
    assert str(refused.value) == NO_CHANGE_EXIT_UNAVAILABLE
    assert refused.value.delta_id == request.capture.delta_id

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
