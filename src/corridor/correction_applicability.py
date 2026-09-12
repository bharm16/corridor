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

**So the verdict is computed from source structure and the accepted record,
never from a capture.**  What decides it, and a correction can write none of it:
the selected passage's own typed locator, the passage row's own conflict number
in the selected document, the document's own header row read through the
released heading vocabulary, and the accepted record's own conflict numbers.
Facts, Support Assessments, Proposed Deltas and correction results are not
consulted at all -- not the challenged capture, not a neighbouring one, and
above all not the corrected capture the act is about to append.  A newly created
Fact therefore cannot move this answer, which is what makes the circular case
unreachable rather than merely discouraged.

**Applicability comes from source structure, never from proximity.**  Neither
"within two rows", nor a matching value type, nor the filename, nor the
coordinator's stated interpretation is consulted here.  ADR-0082 already
settles why: support concerns *one proposition and its relationship to
evidence*, and a passage can support one proposition and be irrelevant to
another.  Locating a valid number in the same workbook is not proof that it
describes the challenged conflict.

**The structured UCM path, in the terms that path already uses (#945 B).**  A
workbook row is a Utility Conflict and a column is a field, and ``later_revision``
already resolves a row's subject by the conflict number the row states about
itself -- its business identity, ``utility_id`` -- matched against the accepted
record, *not* by where the row sits on the sheet, because a row moves between
revisions.  This resolver follows that one contract rather than a second, so the
two halves are read the way the capture that assigned the subject read them:

* **subject** -- the conflict number the passage's own row states in the
  selected document's ``utility_id`` column, matched against the accepted
  record's own conflict numbers to the subject that states it.  Read from the
  selected document and the accepted record, so a row that moved is still its
  own subject and a baseline row at the same sheet position is never mistaken
  for it; the ``sheet!row`` locator is meaningful only inside its own source and
  is never an interchangeable alias for a business identity.  A conflict number
  more than one row of the document, or more than one accepted subject, states
  is ambiguous and resolves to none of them.
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
retained no readable header, one whose row states no conflict number, and one
whose conflict number the accepted record does not hold or holds ambiguously.
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
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from corridor.models import SourceSegment
from corridor.sheets import MIN_HEADER_FIELDS, column_mapping


__all__ = [
    "APPLICABLE",
    "APPLICABILITY_VERDICTS",
    "BUSINESS_IDENTITY_FIELD",
    "CROSS_SUBJECT_PASSAGE",
    "DocumentReading",
    "NOT_ESTABLISHED",
    "NOT_ESTABLISHED_REPORT_RETAINED",
    "OTHER_FIELD",
    "OTHER_FIELD_PASSAGE",
    "OTHER_SUBJECT",
    "PassageApplicability",
    "UNCLEAR",
    "assess_passage",
    "assess_passage_against",
    "read_document",
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
#: The right conflict under the wrong field (#945 C). A known contradiction, so
#: it gets its own sentence rather than sharing the uncertainty one below: a
#: Required By column is not a failure to establish the Start Station -- it is a
#: different field, and telling the coordinator that is telling them what to
#: choose instead. Approved 2026-09-11, used as written.
OTHER_FIELD_PASSAGE = (
    "This passage describes a different field for this Utility Conflict. Choose "
    "evidence for the field being corrected. No correction was applied."
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


#: The canonical field a matrix row is identified by -- its conflict number --
#: which is what ``later_revision`` resolves a row's subject through (#945 B).
#: ``tests`` proves this stays the same string ``later_revision`` uses, so the
#: applicability resolver and the capture that assigned the subject cannot state
#: two definitions of identity.
BUSINESS_IDENTITY_FIELD = "utility_id"


#: How a verdict was reached, as a stable machine token on the retained proof.
SOURCE_ROW_REGISTRATION = "source_row_registration"
#: The passage's row carries no conflict number this document could resolve --
#: no utility_id column, or no value in it for this row.
NO_ROW_IDENTITY = "no_row_identity"
#: A conflict number carried by more than one row of this document, or resolving
#: to more than one adopted subject: ambiguous correspondence stays ambiguous.
AMBIGUOUS_ROW_IDENTITY = "ambiguous_row_identity"
#: A conflict number no adopted source row registers, so it names no subject.
UNREGISTERED_ROW_IDENTITY = "unregistered_row_identity"
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
    of it: how the subject was resolved, and the exact retained header cell the
    field claim rests on.  ``subject_row_identity_segment_id`` and
    ``subject_row_identity_heading_segment_id`` are the subject's own evidence,
    for the writing boundary to re-derive against: the passage row's own cell in
    the document's utility_id column, and that column's retained header (#945 B).
    """

    verdict: str
    subject_identity: str | None
    subject_basis: str
    subject_row_identity_segment_id: int | None
    subject_row_identity_heading_segment_id: int | None
    field: str | None
    field_basis: str
    field_heading_segment_id: int | None
    field_heading_text: str | None

    @property
    def applies(self) -> bool:
        return self.verdict == APPLICABLE

    @property
    def offered_sentence(self) -> str:
        """What the picker and the request door say, having retained nothing.

        A known contradiction -- another conflict, or another field -- names
        what to choose instead; only genuine uncertainty gets the "could not
        establish" sentence, because the two must not share one (#945 C).
        """

        if self.verdict == APPLICABLE:
            return ""
        if self.verdict == OTHER_SUBJECT:
            return CROSS_SUBJECT_PASSAGE
        if self.verdict == OTHER_FIELD:
            return OTHER_FIELD_PASSAGE
        return NOT_ESTABLISHED

    @property
    def investigation_sentence(self) -> str:
        """What an investigation says, the report having really been retained.

        A contradicted passage says the same thing at both doors: the report is
        retained either way, but "another field" is not "we could not tell", so
        the retention clause is only added to the uncertainty sentence (#945 C).
        """

        if self.verdict == APPLICABLE:
            return ""
        if self.verdict == OTHER_SUBJECT:
            return CROSS_SUBJECT_PASSAGE
        if self.verdict == OTHER_FIELD:
            return OTHER_FIELD_PASSAGE
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


@dataclass(frozen=True, slots=True)
class SheetStructure:
    """One document sheet read once: what each column carries, and the exact
    retained header cell that says so.

    Everything a passage of this sheet needs to have its field derived, computed
    a single time however many passages of it the picker offers (#945 D). The
    header row is read the way ``facts.append_structured_cell_facts`` reads it:
    cells are laid out by column number so a blank column cannot shift every
    heading one place left, and the header is the first row naming at least
    ``MIN_HEADER_FIELDS`` canonical fields through the released heading
    vocabulary. A sheet with no such row answers nothing rather than guessing,
    and a header at or below the selected cell is not a header for it.
    """

    header_row: int | None
    #: 0-based header list index -> canonical field (``column_mapping``'s shape).
    field_by_index: dict[int, str]
    #: 1-based column index -> the header cell that named the column.
    header_cell_by_column: dict[int, SourceSegment]
    #: 1-based column index of the utility_id column this sheet's header names,
    #: which is the column a matrix row's business identity is read from (#945 B).
    identity_column: int | None
    #: worksheet row -> that row's own cell in the utility_id column, for every
    #: data row below the header. The conflict number a row states about itself.
    identity_cell_by_row: dict[int, SourceSegment]

    def field_of(
        self, column: str, row_number: int
    ) -> tuple[SourceSegment | None, str | None]:
        """The field the passage's column carries, and the header cell, or None.

        The topmost header-qualifying row of the sheet is the header for every
        cell below it, and none for a cell at or above it -- the same answer the
        per-passage scan gave, now read off structure computed once.
        """

        if self.header_row is None or self.header_row >= row_number:
            return None, None
        wanted = column_index_from_string(column)
        return (
            self.header_cell_by_column.get(wanted),
            self.field_by_index.get(wanted - 1),
        )

    @property
    def identity_heading_cell(self) -> SourceSegment | None:
        """The retained header cell that named the utility_id column, or None."""

        if self.identity_column is None:
            return None
        return self.header_cell_by_column.get(self.identity_column)


@dataclass(frozen=True, slots=True)
class DocumentReading:
    """One selected document's structure and row registrations, read once.

    The picker offers many passages of one document and used to reload the sheet
    and re-query the row registration for each of them; this is that reading
    taken a single time and assessed against, then discarded with the reading
    (#945 D). It is a bounded performance repair, not a cache service: nothing
    outside one reading holds it, and the writing boundary re-derives its own.
    """

    structures: dict[str, SheetStructure]
    #: conflict number -> the accepted Project Record subjects stating it. Read
    #: from the accepted record itself -- each accepted subject's own utility_id
    #: value -- exactly as ``later_revision`` resolves a row, so the
    #: applicability check and the capture that assigned the subject share one
    #: definition of identity rather than two (#945 B). More than one subject is
    #: a repeated conflict number the customer kept apart, so it resolves to none
    #: of them: ambiguous correspondence stays ambiguous.
    subjects_by_business_identity: dict[str, tuple[str, ...]]


def read_document(
    session: Session, *, project_id: int, document_id: int | None
) -> DocumentReading:
    """Read one document's sheet structure and the row registrations once.

    Two statements, whatever the document holds: every structured cell of the
    document (grouped into a per-sheet header reading that also finds each row's
    conflict number), and the accepted record's conflict numbers (grouped to the
    subjects that state them). A passage is then assessed against this in memory
    rather than re-reading the sheet and re-querying the record per option
    (#945 D), and always through the document's own row correspondence rather
    than a baseline row at the same sheet position (#945 B).
    """

    by_sheet: dict[str, dict[int, dict[int, SourceSegment]]] = {}
    if document_id is not None:
        for segment in session.scalars(
            select(SourceSegment)
            .where(
                SourceSegment.project_id == project_id,
                SourceSegment.document_id == document_id,
                SourceSegment.kind == "spreadsheet_cell",
            )
            .order_by(SourceSegment.ordinal, SourceSegment.id)
        ).all():
            cell = worksheet_cell(segment)
            if cell is None:
                continue
            sheet_name, column, row_number = cell
            by_sheet.setdefault(sheet_name, {}).setdefault(row_number, {})[
                column_index_from_string(column)
            ] = segment
    return DocumentReading(
        structures={
            sheet_name: _sheet_structure(rows) for sheet_name, rows in by_sheet.items()
        },
        subjects_by_business_identity=_subjects_by_business_identity(
            session, project_id
        ),
    )


def _sheet_structure(rows: dict[int, dict[int, SourceSegment]]) -> SheetStructure:
    """The topmost header-qualifying row of one sheet, and what it named.

    Once the header is found, the utility_id column it names is fixed, and every
    data row below it contributes its own cell in that column -- the conflict
    number the row states about itself, which is what a passage's subject is
    resolved through (#945 B).
    """

    header_row: int | None = None
    field_by_index: dict[int, str] = {}
    header_cells: dict[int, SourceSegment] = {}
    for candidate in sorted(rows):
        cells = rows[candidate]
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
        header_row = candidate
        field_by_index = mapping
        header_cells = dict(cells)
        break

    identity_column: int | None = None
    for index, mapped_field in field_by_index.items():
        if mapped_field == BUSINESS_IDENTITY_FIELD:
            identity_column = index + 1  # 0-based header index -> 1-based column
            break

    identity_cell_by_row: dict[int, SourceSegment] = {}
    if identity_column is not None and header_row is not None:
        for row_number, cells in rows.items():
            if row_number <= header_row:
                continue
            cell = cells.get(identity_column)
            if cell is not None:
                identity_cell_by_row[row_number] = cell

    return SheetStructure(
        header_row=header_row,
        field_by_index=field_by_index,
        header_cell_by_column=header_cells,
        identity_column=identity_column,
        identity_cell_by_row=identity_cell_by_row,
    )


def _subjects_by_business_identity(
    session: Session, project_id: int
) -> dict[str, tuple[str, ...]]:
    """The accepted record's conflict numbers, grouped to the subjects stating them.

    Read from the accepted record itself -- each accepted subject's own
    ``utility_id`` value in ``current_project_record`` -- which is exactly the
    matching side ``later_revision`` resolves a row through (#945 B). Reading it
    here rather than from the adoption receipt keeps the applicability check and
    the capture that assigned the subject on one definition of identity: a
    subject accepted from an earlier revision's new-subject delta is matched the
    same way, and a re-registered mapping is felt at once.

    A conflict number more than one accepted subject states is ambiguous and
    resolves to none of them; the ``current_project_record`` projection already
    excludes superseded and do-not-add decisions, so a value that left the
    record is not matched.
    """

    grouped: dict[str, list[str]] = {}
    for subject_key, value in session.execute(
        text(
            "select subject_key, text_value from current_project_record "
            "where project_id = :project_id and fact_type = :field"
        ),
        {"project_id": project_id, "field": BUSINESS_IDENTITY_FIELD},
    ).all():
        identity = (value or "").strip()
        if identity:
            grouped.setdefault(identity, []).append(subject_key)
    return {identity: tuple(subjects) for identity, subjects in grouped.items()}


def assess_passage(
    session: Session | None,
    *,
    project_id: int,
    subject_identity: str,
    field: str,
    selected: SourceSegment,
    reading: DocumentReading | None = None,
) -> PassageApplicability:
    """Whether this passage establishes a value for this subject and this field.

    ``subject_identity`` and ``field`` are the challenged capture's own, read
    off retained rows by the callers rather than typed by anyone.  Nothing the
    coordinator wrote is an input, and neither is any Fact: see the module
    docstring for why the second of those is the whole point.

    ``reading`` is the document read once (#945 D). The single-passage callers
    -- the request door and operations -- omit it and one is read for the one
    passage; the picker reads it once and hands the same one to every option. A
    ``session`` is needed only to read one, so a caller that already holds a
    reading passes ``None`` for it.
    """

    if reading is None:
        assert session is not None, "assess_passage needs a session or a reading"
        reading = read_document(
            session,
            project_id=project_id,
            document_id=None if selected.document_id is None else int(selected.document_id),
        )
    return assess_passage_against(
        reading, subject_identity=subject_identity, field=field, selected=selected
    )


def assess_passage_against(
    reading: DocumentReading,
    *,
    subject_identity: str,
    field: str,
    selected: SourceSegment,
) -> PassageApplicability:
    """The verdict for one passage against a document read once (#945 D).

    Pure: it reads nothing, so the picker computes the reading a single time and
    assesses every option against it, and the writing boundary re-derives its
    own from the same retained rows.
    """

    cell = worksheet_cell(selected)
    if cell is None:
        return PassageApplicability(
            verdict=UNCLEAR,
            subject_identity=None,
            subject_basis=UNSTRUCTURED_PASSAGE,
            subject_row_identity_segment_id=None,
            subject_row_identity_heading_segment_id=None,
            field=None,
            field_basis=UNSTRUCTURED_PASSAGE,
            field_heading_segment_id=None,
            field_heading_text=None,
        )
    sheet_name, column, row_number = cell
    structure = reading.structures.get(sheet_name)
    passage_subject, subject_basis = _passage_subject(
        reading, structure, row_number=row_number
    )
    if structure is None:
        heading, passage_field = None, None
        identity_cell = None
    else:
        heading, passage_field = structure.field_of(column, row_number)
        identity_cell = structure.identity_cell_by_row.get(row_number)
    identity_heading = None if structure is None else structure.identity_heading_cell
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
        # The passage row's own cell in the utility_id column, and that column's
        # header, so the writing boundary can re-derive the subject against the
        # same retained bytes rather than trust the resolved string (#945 B).
        subject_row_identity_segment_id=(
            None if identity_cell is None else int(identity_cell.id)
        ),
        subject_row_identity_heading_segment_id=(
            None if identity_heading is None else int(identity_heading.id)
        ),
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
    reading: DocumentReading,
    structure: SheetStructure | None,
    *,
    row_number: int,
) -> tuple[str | None, str]:
    """Which Project Record subject this worksheet row resolves to (#945 B).

    Row identity follows the conflict's business identity, not its position on
    the sheet, which is the later-revision reader's own contract: a row may move
    between revisions, so a correction citing row 10 of the *later* revision may
    not use row 10 of the baseline to say what it is. So the row is resolved
    through *its own document's* utility_id cell -- the conflict number the row
    states about itself -- matched against the customer's adopted registration of
    that conflict number to a subject. The positional ``sheet!row`` locator is
    meaningful only inside its own source and is never an interchangeable alias
    for a business identity.

    Ambiguity stays ambiguity, on either side: a conflict number carried by more
    than one row of this document, or registered to more than one adopted
    subject, names none of them, and the row that carries it is unsettled rather
    than resolved to a guess. A row whose conflict number no registration knows
    is unsettled too -- it is not in the record, so no cell of it carries a value
    for any subject.
    """

    if structure is None:
        return None, NO_ROW_IDENTITY
    identity_cell = structure.identity_cell_by_row.get(row_number)
    if identity_cell is None:
        return None, NO_ROW_IDENTITY
    business_identity = identity_cell.exact_text.strip()
    if not business_identity:
        return None, NO_ROW_IDENTITY
    # Document-side ambiguity: this same conflict number on another row of this
    # document. Nobody can say which of two identically numbered rows a passage
    # in one of them describes.
    carriers = [
        row
        for row, other in structure.identity_cell_by_row.items()
        if other.exact_text.strip() == business_identity
    ]
    if len(carriers) != 1:
        return None, AMBIGUOUS_ROW_IDENTITY
    subjects = reading.subjects_by_business_identity.get(business_identity, ())
    if len(subjects) > 1:
        return None, AMBIGUOUS_ROW_IDENTITY
    if not subjects:
        return None, UNREGISTERED_ROW_IDENTITY
    return subjects[0], SOURCE_ROW_REGISTRATION
