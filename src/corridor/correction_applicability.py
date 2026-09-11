"""Whether one retained passage can support a value for the challenged subject (#945).

#836 grounded a capture correction in *this document's* retained bytes, and
#945 found that necessary condition standing alone as the whole rule.  With
conflict 1's size capture challenged and **conflict 2's own size cell**
selected as the supporting passage, ``build_correction_request`` raised
nothing and ``correct_captured_reading`` wrote a Source Fact under conflict 1's
subject carrying conflict 2's value, held it there with a ``supported``
``value_support`` assessment naming only conflict 2's cell, retired conflict
1's proposal and proposed 6 in onto the record.  "A cell in the same document"
and "a cell that describes this Utility Conflict" are different claims, and
only the first was enforced.

**The rule, as the maintainer stated it.**

    A capture correction may assert a value only when the retained evidence
    establishes that the value applies to the challenged subject and field
    under the declared interpretation.  The selected passage need not already
    have produced a Fact, but the correction may not establish its own
    applicability merely by assigning the challenged subject to a newly
    created Fact.

The second sentence names the trap this module is written to avoid:

    Create a Fact saying this cell belongs to UC-1
    -> cite that new Fact as proof that the cell belongs to UC-1

**So the verdict is computed from source structure only, and never from a
capture.**  Three retained inputs decide it, and a correction can write none of
them: the selected passage's own typed locator, the customer's adopted
source-row registration, and the document's own header row read through the
released heading vocabulary.  Facts, Support Assessments, Proposed Deltas and
correction results are not consulted at all -- not the challenged capture, not
a neighbouring one, and above all not the corrected capture the act is about to
append.  A newly created Fact therefore cannot move this answer, which is what
makes the circular case unreachable rather than merely discouraged.

**Applicability comes from source structure, never from proximity.**  Neither
"within two rows", nor a matching value type, nor the filename, nor the
coordinator's stated interpretation is consulted here.  ADR-0082 already
settles why: support concerns *one proposition and its relationship to
evidence*, and a passage can support one proposition and be irrelevant to
another.  Locating a valid number in the same workbook is not proof that it
describes the challenged conflict.

**The structured UCM path, in the terms that path already uses.**  A workbook
cell's subject is its worksheet row and its field is its column, which is not
this module's invention: ``facts.append_structured_cell_facts`` files every
structured capture under ``sheet_name!worksheet_row_number`` -- the same
``source_row_key_rule`` Adopt Baseline records -- and takes the field from the
sheet's own header row through ``sheets.column_mapping``.  So the two halves
are read exactly where the capture path reads them:

* **subject** -- the adopted ``project_baseline_source_rows`` registration for
  that sheet and row, which is the customer's own resolution of a source row to
  a Project Record subject; and where a revision carries a row nobody has
  registered, the worksheet-row identity that path would itself assign.
* **field** -- the column the document's own retained header row names, read
  through the released heading vocabulary.  An unextracted cell is answered as
  readily as an extracted one, because a header row says what a column carries
  whether or not anything was ever captured from it.

One consequence is worth saying out loud: on a one-value-per-column form there
is exactly one cell that can support a value for one subject and field, so for
a structured source this is a check on the coordinator's selection rather than
a choice between several admissible passages.  That is the geometry of the
form, not a narrowing of the act -- the passage a misreading *should* have been
read from is a single cell, whether the capture misread its text or cited the
wrong cell entirely.

**What is deliberately not decided here.**  A passage that is not a structured
cell -- a PDF span, an email span, a recorded verbal statement -- is answered
``UNCLEAR`` rather than guessed at.  So is a structured cell whose sheet
retained no readable header, and one whose row the registration excludes.
``UNCLEAR`` is not a refusal to act: the report is retained and the
investigation records that it could not be substantiated, which is exactly what
ADR-0101 asks for when evidence is missing or ambiguous.  Reusing a retained
``scope`` or ``attribution`` assessment to answer a prose passage, and the note
that explicitly applies one value to several named conflicts, are the next
increment and are not built here; the maintainer's own framing is that the
first fix need not solve arbitrary prose attribution, and an unsupported case
staying an explicitly unsubstantiated investigation is a correct outcome.

**Two doors ask, and the database asks again.**  The picker explains what it is
offering, ``capture_correction.build_correction_request`` refuses a contradicted
selection before any report is recorded, ``operations_repair`` re-asks before it
writes anything, and ``record_capture_correction_result`` re-derives the whole
verdict from these same retained rows inside the writing transaction.  The last
of those is the one a caller cannot go round: a substantiated outcome whose
passage is not applicable is refused by the command and by the relation's own
CHECK.

Nothing here reads a clock, and nothing here writes.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from openpyxl.utils import column_index_from_string
from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.models import BaselineSourceRow, SourceSegment
from corridor.sheets import MIN_HEADER_FIELDS, column_mapping


__all__ = [
    "APPLICABLE",
    "APPLICABILITY_VERDICTS",
    "CROSS_SUBJECT_PASSAGE",
    "NOT_ESTABLISHED",
    "NOT_ESTABLISHED_REPORT_RETAINED",
    "OTHER_FIELD",
    "OTHER_SUBJECT",
    "PassageApplicability",
    "UNCLEAR",
    "assess_passage",
    "worksheet_cell",
]


#: The selected passage establishes that the value applies to the challenged
#: subject and field, so a correction may assert it.
APPLICABLE = "applicable"
#: The retained structure says this passage describes a different subject.
OTHER_SUBJECT = "other_subject"
#: It describes the right subject under a different field.  Two fields sharing
#: a value class is not a relationship: a Required By date selected as a
#: Promised For date is a semantic substitution, refused as one.
OTHER_FIELD = "other_field"
#: Nothing retained settles it either way.  The report stands and the
#: investigation says it could not be substantiated.
UNCLEAR = "unclear"
APPLICABILITY_VERDICTS = (APPLICABLE, OTHER_SUBJECT, OTHER_FIELD, UNCLEAR)


# The maintainer approved these two sentences on 2026-09-11 and they are used
# as written.  Each describes what happened in plain words rather than naming a
# customer-facing type, which is the treatment ADR-0100 gave "Report an
# extraction error" and ADR-0101 gave "Corridor corrected its reading of this
# source"; a defined type for either would need the terminology research first.
CROSS_SUBJECT_PASSAGE = (
    "This passage describes a different Utility Conflict. Choose evidence for "
    "this conflict, or ask Corridor operations to review the source mapping. "
    "No correction was applied."
)
#: The first sentence alone, for the door where nothing has been retained yet.
#: Promising that a report remains available where no report was written would
#: be false, so the retention clause is only ever added by the door that has
#: actually retained one.
NOT_ESTABLISHED = (
    "Corridor could not establish that this passage supports the value for "
    "this Utility Conflict."
)
#: ...and with it, for the investigation door, where the report really stands.
NOT_ESTABLISHED_REPORT_RETAINED = (
    f"{NOT_ESTABLISHED} Your report remains available for investigation; no "
    "correction was applied."
)


#: How a verdict was reached, as a stable machine token on the retained proof.
SOURCE_ROW_REGISTRATION = "source_row_registration"
WORKSHEET_ROW_IDENTITY = "worksheet_row_identity"
EXCLUDED_SOURCE_ROW = "excluded_source_row"
UNSTRUCTURED_PASSAGE = "unstructured_passage"
RETAINED_HEADER_ROW = "retained_header_row"
NO_HEADER_ROW = "no_header_row"
UNMAPPED_COLUMN = "unmapped_column"


_CELL = re.compile(r"^([A-Z]+)([1-9][0-9]*)$")


@dataclass(frozen=True, slots=True)
class PassageApplicability:
    """What the retained source structure says about one selected passage.

    Everything here is derived from rows a correction cannot write, so two
    readings of the same records reach the same verdict and appending the
    corrected capture does not change it.  ``subject_basis`` and
    ``field_heading_segment_id`` are the evidence itself rather than a summary
    of it: the registration or rule the subject came from, and the exact
    retained header cell the field claim rests on.
    """

    verdict: str
    subject_identity: str | None
    subject_basis: str
    field: str | None
    field_basis: str
    field_heading_segment_id: int | None
    field_heading_text: str | None

    @property
    def applies(self) -> bool:
        return self.verdict == APPLICABLE

    @property
    def offered_sentence(self) -> str:
        """What the picker and the request door say, having retained nothing."""

        if self.verdict == APPLICABLE:
            return ""
        if self.verdict == OTHER_SUBJECT:
            return CROSS_SUBJECT_PASSAGE
        return NOT_ESTABLISHED

    @property
    def investigation_sentence(self) -> str:
        """What an investigation says, the report having really been retained."""

        if self.verdict == APPLICABLE:
            return ""
        if self.verdict == OTHER_SUBJECT:
            return CROSS_SUBJECT_PASSAGE
        return NOT_ESTABLISHED_REPORT_RETAINED


def worksheet_cell(segment: SourceSegment) -> tuple[str, str, int] | None:
    """One structured cell as sheet, column letters and worksheet row.

    ``None`` for every other passage, which is how a PDF span or a recorded
    verbal statement reaches ``UNCLEAR`` rather than being parsed at.
    """

    if segment.kind != "spreadsheet_cell":
        return None
    if segment.sheet_name is None or segment.cell_range is None:
        return None
    found = _CELL.fullmatch(segment.cell_range)
    if found is None:
        return None
    return segment.sheet_name, found.group(1), int(found.group(2))


def assess_passage(
    session: Session,
    *,
    project_id: int,
    subject_identity: str,
    field: str,
    selected: SourceSegment,
) -> PassageApplicability:
    """Whether this passage establishes a value for this subject and this field.

    ``subject_identity`` and ``field`` are the challenged capture's own, read
    off retained rows by the callers rather than typed by anyone.  Nothing the
    coordinator wrote is an input, and neither is any Fact: see the module
    docstring for why the second of those is the whole point.
    """

    cell = worksheet_cell(selected)
    if cell is None:
        return PassageApplicability(
            verdict=UNCLEAR,
            subject_identity=None,
            subject_basis=UNSTRUCTURED_PASSAGE,
            field=None,
            field_basis=UNSTRUCTURED_PASSAGE,
            field_heading_segment_id=None,
            field_heading_text=None,
        )
    sheet_name, column, row_number = cell
    passage_subject, subject_basis = _passage_subject(
        session,
        project_id=project_id,
        sheet_name=sheet_name,
        row_number=row_number,
        subject_identity=subject_identity,
    )
    heading, passage_field = _passage_field(
        session, selected=selected, sheet_name=sheet_name, column=column,
        row_number=row_number,
    )
    if passage_subject is None or passage_field is None:
        verdict = UNCLEAR
    elif passage_subject != subject_identity:
        verdict = OTHER_SUBJECT
    elif passage_field != field:
        verdict = OTHER_FIELD
    else:
        verdict = APPLICABLE
    return PassageApplicability(
        verdict=verdict,
        subject_identity=passage_subject,
        subject_basis=subject_basis,
        field=passage_field,
        field_basis=_field_basis(heading, passage_field),
        # The cell the field claim rests on, and only where there is a claim:
        # a heading the vocabulary does not name establishes nothing, so it is
        # not recorded as the evidence for anything.
        field_heading_segment_id=(
            None if heading is None or passage_field is None else int(heading.id)
        ),
        field_heading_text=(
            None if heading is None or passage_field is None else heading.exact_text
        ),
    )


def _field_basis(heading: SourceSegment | None, field: str | None) -> str:
    """Which of the three things the header row said, as a stable token."""

    if field is not None:
        return RETAINED_HEADER_ROW
    return NO_HEADER_ROW if heading is None else UNMAPPED_COLUMN


def _passage_subject(
    session: Session,
    *,
    project_id: int,
    sheet_name: str,
    row_number: int,
    subject_identity: str,
) -> tuple[str | None, str]:
    """Which Project Record subject this worksheet row resolves to.

    One row resolves under two retained rules, and the product uses both. A
    structured capture is filed under ``sheet_name!worksheet_row_number``, the
    ``source_row_key_rule`` Adopt Baseline records; the adopted row's own
    ``record_subject_key`` is the customer's resolution of that row to a
    record subject, which is the row's business identity where the form prints
    one. Neither is this module's invention and neither is safe to drop: a
    project whose deltas are stated in one space would be refused wholesale if
    only the other were consulted.

    So a row resolves to *either* of its retained identities, and what the
    verdict asks is whether the challenged subject is one of them. That keeps
    the containment exactly where #945 put it -- a neighbouring row resolves to
    neither identity of this conflict, whatever space the delta is stated in.

    A row the adoption excluded resolves to nothing at all: it is not in the
    record, so no cell of it carries a value for any subject, and the answer is
    the unsettled one rather than a refusal about somebody else's conflict.

    The newest registration wins where a project has registered more than one
    baseline source, which is also what makes a re-registered mapping felt: a
    correction compared against the old resolution is refused by the command
    rather than committed against a mapping that has moved.
    """

    rule_identity = f"{sheet_name}!{row_number}"
    registered = session.scalars(
        select(BaselineSourceRow)
        .where(
            BaselineSourceRow.project_id == project_id,
            BaselineSourceRow.sheet_name == sheet_name,
            BaselineSourceRow.row_number == row_number,
        )
        .order_by(
            BaselineSourceRow.baseline_source_id.desc(), BaselineSourceRow.id.desc()
        )
        .limit(1)
    ).first()
    if registered is None:
        identities, canonical = {rule_identity}, rule_identity
        basis = WORKSHEET_ROW_IDENTITY
    elif registered.excluded or not registered.record_subject_key:
        return None, EXCLUDED_SOURCE_ROW
    else:
        identities = {rule_identity, registered.record_subject_key}
        canonical = registered.record_subject_key
        basis = SOURCE_ROW_REGISTRATION
    return (
        subject_identity if subject_identity in identities else canonical,
        basis,
    )


def _passage_field(
    session: Session,
    *,
    selected: SourceSegment,
    sheet_name: str,
    column: str,
    row_number: int,
) -> tuple[SourceSegment | None, str | None]:
    """Which canonical field this column carries, and the header cell saying so.

    The sheet's own retained header row is read the way
    ``facts.append_structured_cell_facts`` reads it: cells are laid out by
    column number so a blank column cannot shift every heading one place left,
    and the header is the first row naming at least ``MIN_HEADER_FIELDS``
    canonical fields through the released heading vocabulary.  A sheet with no
    such row answers nothing rather than falling back to a guess, and a header
    at or below the selected cell is not a header for it.
    """

    rows: dict[int, dict[int, SourceSegment]] = {}
    for segment in session.scalars(
        select(SourceSegment)
        .where(
            SourceSegment.project_id == selected.project_id,
            SourceSegment.document_id == selected.document_id,
            SourceSegment.kind == "spreadsheet_cell",
            SourceSegment.sheet_name == sheet_name,
        )
        .order_by(SourceSegment.ordinal, SourceSegment.id)
    ).all():
        cell = worksheet_cell(segment)
        if cell is None:
            continue
        rows.setdefault(cell[2], {})[column_index_from_string(cell[1])] = segment
    wanted = column_index_from_string(column)
    for header_row in sorted(rows):
        if header_row >= row_number:
            return None, None
        cells = rows[header_row]
        headings = [
            "" if cells.get(index) is None else cells[index].exact_text
            for index in range(1, max(cells) + 1)
        ]
        mapping = column_mapping(headings)
        if len(mapping) < MIN_HEADER_FIELDS:
            # ``sheets.header_row``'s own threshold, for its own reason: a
            # merged title band populates one cell of the row it spans, and a
            # data cell whose words happen to match a published heading would
            # otherwise turn its row into a header for everything below it.
            continue
        heading = cells.get(wanted)
        return heading, mapping.get(wanted - 1)
    return None, None
