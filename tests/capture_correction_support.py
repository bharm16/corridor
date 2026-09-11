"""The misread capture every correction test starts from (#836, #842, ADR-0101).

One accepted value, one revision that read the same cell wrong, and the two
other retained cells of that revision a correction can be pointed at: the one
the capture should have read, and one that says something else again. Two test
modules build the same scenario -- the lifecycle properties and the operator
runbook that performs it -- and a second copy of it would let them drift about
what "the same misread" means.

The accepted record really holds this subject and field, which is what makes a
*no-change* recomparison reachable at all: a corrected capture can only match an
accepted value that exists.

Nothing here reads a clock. Every instant and cutoff is declared.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from corridor.capture_correction import (
    build_correction_request,
    record_correction_request,
)
from corridor.models import Project, SourceSegment
from corridor.packet_review import read_review_items
from corridor.principals import HumanPrincipal

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
OPERATOR = HumanPrincipal("local:operator")
WORKER_IDENTITY = "runtime:recapture-worker"
CUTOFF = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
REPORTED_AT = datetime(2026, 9, 11, 10, 0, tzinfo=timezone.utc)
CORRECTED_AT = datetime(2026, 9, 11, 11, 0, tzinfo=timezone.utc)
DECIDED_AT = datetime(2026, 9, 11, 11, 30, tzinfo=timezone.utc)

ACCEPTED_TEXT = "1001+00"
MISREAD_TEXT = "1002+00"
STILL_DIFFERENT_TEXT = "1003+00"
FIELD = "station_from"
EXPECTED = "the station column on this row reads 1001+00, unchanged"


class Misread:
    """One accepted value, and a revision that misread the same cell.

    The accepted record really holds this subject and field, which is what
    makes a *no-change* recomparison reachable at all: a corrected capture can
    only match an accepted value that exists.

    ``correct`` is the passage the coordinator points at -- a retained cell of
    the same revision whose text is the accepted value. ``divergent`` is a
    retained cell of that same revision saying something else again, for the
    case where the correction still differs.
    """

    def __init__(
        self,
        session: Session,
        project: Project,
        *,
        field: str = FIELD,
        accepted_text: str = ACCEPTED_TEXT,
        misread_text: str = MISREAD_TEXT,
        correct_text: str | None = None,
        divergent_text: str = STILL_DIFFERENT_TEXT,
    ) -> None:
        self.session = session
        self.project = project
        self.field = field
        self.accepted_text = accepted_text
        correct_text = accepted_text if correct_text is None else correct_text
        self.adopted = Rendition(session, project, f"ucm-2026-08-{uuid4().hex[:6]}.xlsx")
        accepted, _ = self.adopted.capture(
            fact_type=field, value=accepted_text, subject_key=subject(1)
        )
        self.revision_id = accept_baseline_fact(session, project, accepted)
        baseline = register_baseline(
            session, project, self.adopted.document, self.revision_id
        )
        register_source_row(
            session, project, baseline, row_number=1, business_identity="U-001"
        )
        register_output_template(
            session, project, identity="district-ucm-template", version="v3"
        )
        self.incoming = Rendition(
            session, project, f"ucm-2026-09-{uuid4().hex[:6]}.xlsx"
        )
        self.fact, self.segment = self.incoming.capture(
            fact_type=field, value=misread_text, subject_key=subject(1), cell="C1"
        )
        self.support = support(session, project, self.fact, self.segment)
        # The two other retained cells of this same revision: the one the
        # capture should have read, and one that says something else again.
        self.correct = self.incoming.segment(correct_text, cell="C2")
        self.divergent = self.incoming.segment(divergent_text, cell="C3")
        (self.delta,) = append_deltas(
            session,
            project,
            self.incoming,
            source_revision="2026-09",
            values=[
                modify(
                    subject_key=subject(1),
                    field_name=field,
                    accepted_value=accepted_text,
                    proposed_value=misread_text,
                    baseline_revision=self.revision_id,
                )
            ],
        )

    def item(self):
        """The Review item that decides this change, and the reading it is in."""

        reading = read_review_items(
            self.session, project_id=self.project.id, as_of=CUTOFF
        )
        item = next(
            row
            for row in reading.items
            if any(child.delta_id == self.delta.id for child in row.children)
        )
        return reading, item

    def report(self, *, passage: SourceSegment | None = None):
        """The retained extraction-error report operations will act on."""

        _, item = self.item()
        child = next(
            row for row in item.children if row.delta_id == self.delta.id
        )
        request = build_correction_request(
            self.session,
            item,
            child,
            principal=ALICE,
            reported_at=REPORTED_AT,
            selected_source_segment_id=(passage or self.correct).id,
            expected_interpretation=EXPECTED,
        )
        return record_correction_request(self.session, request)


