"""The misread capture every correction test starts from (#836, #842, ADR-0101).

One accepted value, and one revision that read the challenged conflict's own
row through the wrong column. Two test modules build the same scenario -- the
lifecycle properties and the operator runbook that performs it -- and a second
copy of it would let them drift about what "the same misread" means.

The accepted record really holds this subject and field, which is what makes a
*no-change* recomparison reachable at all: a corrected capture can only match an
accepted value that exists.

**The workbook is laid out the way a UCM workbook is laid out (#945).** A row
is a Utility Conflict and a column is a field, which is not this fixture's
convention: ``facts.append_structured_cell_facts`` files every structured
capture under ``sheet_name!worksheet_row_number`` and takes its field from the
sheet's own header row. So row 1 is the header, row 2 is the one conflict, and
the challenged capture reads that conflict's **Required By** cell as its
``field`` -- a wrong-column misreading of the right row, which is the defect
the correction reports and the one whose fix is a single unambiguous cell.

There is exactly one passage that can support a value for one subject and one
field, and it is ``correct``: column C of row 2. Two cells cannot both be it,
so a scenario that needs the corrected value to *differ* from the accepted one
builds the fixture with a different ``correct_text`` rather than pointing at a
second cell. Under the old layout those alternatives sat at C2 and C3 -- rows
2 and 3 of the sheet, which is to say two other Utility Conflicts.

Nothing here reads a clock. Every instant and cutoff is declared.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from corridor.capture_correction import (
    build_correction_request,
    record_correction_request,
)
from corridor.correction_applicability import (
    BUSINESS_IDENTITY_FIELD,
    PassageApplicability,
    assess_passage,
)
from corridor.models import Project, ProposedDelta, SourceSegment
from corridor.packet_review import read_review_items
from corridor.principals import HumanPrincipal

from source_capture_support import append_header_row

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

#: The one conflict this scenario is about, as a worksheet row under the header.
CONFLICT_ROW = 2
#: The conflict below it, which the same revision also proposes a change to and
#: which no correction here ever names. See ``neighbouring_open_proposal``.
NEIGHBOUR_ROW = CONFLICT_ROW + 1
NEIGHBOUR_ACCEPTED_TEXT = "1004+00"
#: The column the challenged field is published in, and the column the capture
#: wrongly read it from. Both are named by the rendition's own header row.
FIELD_COLUMN = "C"
WRONG_COLUMN = "D"
#: The column carrying each row's conflict number, which is what a row's subject
#: is resolved through -- its business identity, not its position on the sheet
#: (#945 B). Row identity follows the number the row states about itself, so the
#: same conflict at a different worksheet row still resolves to its own subject.
IDENTITY_COLUMN = "A"
#: The conflict numbers the two rows state, distinct so neither row is ambiguous.
BUSINESS_IDENTITY = "U-001"
NEIGHBOUR_BUSINESS_IDENTITY = "U-002"
#: What ``WRONG_COLUMN`` actually carries. Two date fields in adjacent columns
#: is the maintainer's own example of a substitution a correction may not make.
WRONG_FIELD = "need_date"
ALTERNATE_WRONG_FIELD = "committed_date"


class Misread:
    """One accepted value, and a revision that misread the same cell.

    The accepted record really holds this subject and field, which is what
    makes a *no-change* recomparison reachable at all: a corrected capture can
    only match an accepted value that exists.

    ``correct`` is the passage the coordinator points at: the challenged
    conflict's own cell in the challenged field's own column, which is the one
    passage the retained structure holds to this subject and this field.
    ``wrong_field_cell`` is the same conflict's cell in the neighbouring
    column -- the right row under the wrong field, which a correction may not
    substitute (#945).

    ``correct_text`` is what that cell says, so a scenario needing the
    corrected value to differ from the accepted one asks for a different text
    rather than a different cell.

    ``header=False`` retains the same cells under a sheet that names none of
    its columns, which is the source that settles nothing either way: the
    report is still built and retained, and the investigation is the half that
    must refuse to conclude anything from it.
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
        header: bool = True,
    ) -> None:
        self.session = session
        self.project = project
        self.field = field
        self.accepted_text = accepted_text
        correct_text = accepted_text if correct_text is None else correct_text
        self.subject_key = subject(CONFLICT_ROW)
        neighbouring = (
            ALTERNATE_WRONG_FIELD if field == WRONG_FIELD else WRONG_FIELD
        )
        self.adopted = Rendition(session, project, f"ucm-2026-08-{uuid4().hex[:6]}.xlsx")
        accepted, _ = self.adopted.capture(
            fact_type=field, value=accepted_text, subject_key=self.subject_key
        )
        # The accepted record states each conflict's number, so a passage's row
        # resolves to its subject by the number it carries -- the way
        # ``later_revision`` resolves a row -- rather than by its sheet position
        # (#945 B).
        challenged_identity, _ = self.adopted.capture(
            fact_type=BUSINESS_IDENTITY_FIELD,
            value=BUSINESS_IDENTITY,
            subject_key=self.subject_key,
        )
        neighbour_identity, _ = self.adopted.capture(
            fact_type=BUSINESS_IDENTITY_FIELD,
            value=NEIGHBOUR_BUSINESS_IDENTITY,
            subject_key=subject(NEIGHBOUR_ROW),
        )
        self.revision_id = accept_baseline_fact(
            session, project, accepted, challenged_identity, neighbour_identity
        )
        baseline = register_baseline(
            session, project, self.adopted.document, self.revision_id
        )
        # Both rows this scenario names are registered by their own conflict
        # number, so a passage of either resolves through its business identity
        # rather than its sheet position (#945 B).
        for row_number, identity in (
            (CONFLICT_ROW, BUSINESS_IDENTITY),
            (NEIGHBOUR_ROW, NEIGHBOUR_BUSINESS_IDENTITY),
        ):
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
        self.incoming = Rendition(
            session, project, f"ucm-2026-09-{uuid4().hex[:6]}.xlsx"
        )
        # What this revision's columns carry, in the workbook's own words. The
        # header row is what makes "column C is this field" -- and "column A is
        # the conflict number" -- a retained fact about the source rather than a
        # fixture's private convention.
        self.headings = (
            append_header_row(
                self.incoming,
                {
                    IDENTITY_COLUMN: BUSINESS_IDENTITY_FIELD,
                    FIELD_COLUMN: field,
                    WRONG_COLUMN: neighbouring,
                },
            )
            if header
            else {}
        )
        # Each conflict's own row states its conflict number in the identity
        # column, which is what its subject is resolved through.
        self.incoming.segment(
            BUSINESS_IDENTITY, cell=f"{IDENTITY_COLUMN}{CONFLICT_ROW}"
        )
        self.incoming.segment(
            NEIGHBOUR_BUSINESS_IDENTITY, cell=f"{IDENTITY_COLUMN}{NEIGHBOUR_ROW}"
        )
        # The misreading: the challenged conflict's own row, read through the
        # neighbouring column.
        self.fact, self.segment = self.incoming.capture(
            fact_type=field,
            value=misread_text,
            subject_key=self.subject_key,
            cell=f"{WRONG_COLUMN}{CONFLICT_ROW}",
        )
        self.support = support(session, project, self.fact, self.segment)
        # The one passage that carries this field for this conflict, and the
        # neighbouring cell that carries a different field for the same one.
        self.correct = self.incoming.segment(
            correct_text, cell=f"{FIELD_COLUMN}{CONFLICT_ROW}"
        )
        # The cited cell is the neighbouring column's, which is exactly what
        # a wrong-field selection looks like when a test needs one.
        self.wrong_field_cell = self.segment
        # And the next conflict down, in this field's own column: a cell that
        # reads perfectly well and describes somebody else (#945).
        self.other_conflicts_cell = self.incoming.segment(
            STILL_DIFFERENT_TEXT, cell=f"{FIELD_COLUMN}{CONFLICT_ROW + 1}"
        )
        (self.delta,) = append_deltas(
            session,
            project,
            self.incoming,
            source_revision="2026-09",
            values=[
                modify(
                    subject_key=self.subject_key,
                    field_name=field,
                    accepted_value=accepted_text,
                    proposed_value=misread_text,
                    baseline_revision=self.revision_id,
                )
            ],
        )
        #: The neighbour's own accepted revision, once one is asked for.
        self.neighbour_revision_id: int | None = None

    def neighbouring_open_proposal(self) -> ProposedDelta:
        """One more proposal from this revision, about the conflict below (#952).

        A reader that must not name the retired proposal has to be caught
        naming something else, or an accidentally empty reading passes for a
        correct exclusion. So this appends the control: the next Utility
        Conflict down, read from this field's own column -- the cell
        ``other_conflicts_cell`` already stands for -- against an accepted
        value of its own, so it is a genuine difference from the accepted
        record and reads as open rather than as raised against a value that
        moved.

        No correction ever names it. It is a different subject from the
        challenged one, so ``correct_captured_reading`` recomputes nothing
        about it and the retirement cannot reach it.

        Opt-in, because the modules that exercise the correction itself count
        what this project has open and must keep counting what they always
        did.
        """

        accepted, _ = self.adopted.capture(
            fact_type=self.field,
            value=NEIGHBOUR_ACCEPTED_TEXT,
            subject_key=subject(NEIGHBOUR_ROW),
        )
        self.neighbour_revision_id = accept_baseline_fact(
            self.session, self.project, accepted
        )
        (delta,) = append_deltas(
            self.session,
            self.project,
            self.incoming,
            source_revision="2026-09",
            values=[
                modify(
                    subject_key=subject(NEIGHBOUR_ROW),
                    field_name=self.field,
                    accepted_value=NEIGHBOUR_ACCEPTED_TEXT,
                    proposed_value=STILL_DIFFERENT_TEXT,
                    baseline_revision=self.neighbour_revision_id,
                )
            ],
        )
        return delta

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

    def applicability(
        self, *, passage: SourceSegment | None = None
    ) -> PassageApplicability:
        """What the retained source structure says about one of these passages.

        Computed from the same rows the product computes it from, so a test
        that hands this to ``record_correction_result`` is handing it the real
        verdict rather than a convenient one -- which is the whole of what that
        command refuses to take on trust (#945).
        """

        return assess_passage(
            self.session,
            project_id=int(self.project.id),
            subject_identity=self.subject_key,
            field=self.field,
            selected=passage if passage is not None else self.correct,
        )

    def report_bypassing_the_picker(self, passage: SourceSegment):
        """One retained report over a passage the screen would have refused.

        ``build_correction_request`` refuses a selection the retained structure
        contradicts, so a report naming one exists only where somebody went
        round the screen -- and that caller is exactly who the authoritative
        path has to answer for itself (#945). Everything else about the report
        is what the screen would have written.
        """

        request = replace(
            self._request(), selected_source_segment_id=int(passage.id)
        )
        return record_correction_request(self.session, request)

    def report(self, *, passage: SourceSegment | None = None):
        """The retained extraction-error report operations will act on."""

        return record_correction_request(self.session, self._request(passage=passage))

    def _request(self, *, passage: SourceSegment | None = None):
        """The request the screen builds, refusals and all."""

        _, item = self.item()
        child = next(
            row for row in item.children if row.delta_id == self.delta.id
        )
        return build_correction_request(
            self.session,
            item,
            child,
            principal=ALICE,
            reported_at=REPORTED_AT,
            selected_source_segment_id=(passage or self.correct).id,
            expected_interpretation=EXPECTED,
        )


