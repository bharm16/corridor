"""Capture one design-partner Key Date table as Source Facts and Proposed Deltas (#450).

**The format is the partner's Key Date table, and only that one.**  Its three
columns are the ones the corpus already holds in `corpus/sh99-milestones.csv` —
`code`, `name`, `need_date` — and they arrive in coordination vocabulary, so
this path has no mapping surface, model or human, at all.  The clearance-date
table and the design-build Utility Tracking Report are each a later issue; a
generic three-format adapter framework is exactly what this ticket's amendment
withdrew, and building one anyway would be building the withdrawn ticket.

**The rendition captured is the workbook.**  `source_intake` accepts a PDF or
an Excel workbook and nothing else, and `source_segments` can replay exactly
two locator shapes — a workbook cell and a PDF prose span.  A Source Fact that
cannot be replayed from its registered bytes is not a Source Fact (ADR-0076,
#446), so the same three columns are read from the workbook the partner's
scheduler exports rather than from a delimited file Corridor can neither stage
nor dereference.  Reading a delimited rendition means extending the replay
contract, which is its own issue.

**Corridor does not calculate CPM.**  The adapter consumes the export's stated
dates and nothing else (ADR-0078, and the rule carried from closed #427).  It
reads no sequencing, no float, no duration and no relationship, and it derives
no date from another date.  A key date's date is whatever the row says.

**It never moves Promised For or an accepted Required By relationship.**  That
is the defect this module exists to replace, not an incidental property of it.
`milestones._apply_import` writes the new date straight onto the `Milestone`,
and `schedule_linking.flow_through_revisions` then carries it onto every linked
Constraint's Required By basis, so today a schedule export silently rewrites
accepted authority.  Here the same file produces Source Facts and Proposed
Deltas and stops; `delta_resolution` (#519) and the Review Packet transaction
(#526) are the only things that make anything effective.  Promised For
(`committed_date`) is not a column of this format and is never written on any
path through here.

**A Key Date is identified by its code, never by where it sits in the file.**
That is the same Row Identification Rule argument `later_revision` makes about
a conflict number: a key date that moved down the sheet between revisions is
still the same key date, and pairing by printed row number would call an
insertion a change to every row below it.  A code carried by more than one row
is not resolved by preferring one of them — both rows become visible Processing
Failures, because a key date table that names `DESIGN` twice does not say which
`DESIGN` is which and no rule here may guess.

**The scheduled date is captured as a `need_date` Source Fact.**  Required By
is "calculated from the exact Key Date Version that the Constraint serves"
(glossary), and `milestones.link_dependency` carries `milestone.need_date` onto
the Constraint unchanged, so `need_date` already names one date read through
two subjects: the key date the project needs met, and the Constraint that
serves it.  A distinct fact type would be the more precise spelling and is not
available here — `facts.fact_type` and `ck_facts_subject` are CHECK-constrained
and this change may not carry a migration.  The subject namespace keeps the two
apart instead: a key date subject is `key_date:<code>` and an adopted conflict
subject is `<sheet>!<row>`, so the two never meet in one projection key.

**Impact is a Derivation, not a fact.**  `proposed_deltas.ImpactDerivation`
already says so (ADR-0082): a computed consequence carries its rule, its
inputs, and when it was evaluated, so a later rule change is visible rather
than retroactive.  The affected Constraints of a moved key date are read from
the accepted record — the subjects whose accepted Required By is the key date's
currently accepted date — and never from a similarity guess.  `proposed_deltas`
has no column for one, so the derivations are retained beside the delta ids in
the single append-only audit entry this act writes and returned on its receipt.

**What was tried and rejected.**  Feeding the table through
`milestones.preview_import` and diffing its `RowPreview` classifications was
tried first: that reader's output is a decision about the `Milestone` table, it
drops a blank code silently into `skipped_blank`, and every path out of it
writes accepted authority — the three things this ticket forbids.  Registering
the key date rows as adopted baseline subjects was rejected because Adopt
Baseline establishes the accepted record from the customer's own workbook and
a schedule export is a source that disagrees with the record, not the record.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Sequence

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor import audit
from corridor.analytics import AnalyticsBinding
from corridor.measurement_collection import binding_for_source
from corridor.connectors.pull_connector import SourceEnvelope
from corridor.delta_generation import accepted_values, fact_value, revision_label
from corridor.extraction_runs import record_extraction_run
from corridor.extractor_lineage import deployed_extractor_config, zero_token_usage
from corridor.fact_types import FACT_TYPE_CONTRACTS
from corridor.materializer import (
    FactValidationError,
    materialize_segment_value,
    validated_scalar_value,
)
from corridor.models import (
    Document,
    Fact,
    Project,
    ProjectRecordRevision,
    SourceDelivery,
    SourceSegment,
)
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.proposed_deltas import (
    ExistingSubjectTarget,
    ImpactDerivation,
    ProposedDeltaValues,
    ProposedSubjectTarget,
    create_proposed_delta_group,
)
from corridor.row_accounting import RowAccounting
from corridor.source_append import append_fact
from corridor.source_delivery import stored_delivery
from corridor.source_intake import (
    IntakeConflict,
    StagedSource,
    confirm_intake,
    preview_intake,
)
from corridor.storage import staged_file
from corridor.support_assessments import FactProposition, record_support_assessment


# What produced this reading.  Recorded on the Extraction Run and the row
# accounting receipt, so a table read by a later reader is a different reading
# of the same file and the two are never pooled.
READER_VERSION = "key_date_table_v1"
READER_PATH = "spreadsheet_cells"

# The document kind a schedule export is registered under.
KEY_DATE_TABLE_DOC_TYPE = "schedule"

# The printed headings of the one format this adapter reads, in the partner's
# own spelling.  Matched case-insensitively and trimmed, because a heading's
# capitalisation is not what makes it that column, but never inferred: a sheet
# whose first populated row is not exactly these three headings is not this
# format and the file refuses rather than guessing a mapping.
CODE_COLUMN = "code"
NAME_COLUMN = "name"
DATE_COLUMN = "need_date"
DECLARED_COLUMNS = (CODE_COLUMN, NAME_COLUMN, DATE_COLUMN)

# The lineage every revision of this format shares, so a delta from the next
# export can supersede one from this export (#518).  It is the format, not the
# file: a second export of the same table is a newer revision of one lineage.
KEY_DATE_TABLE_FAMILY = "key_date_table:v1"

# The rule this module compares under, recorded on every delta it appends so a
# later change to the comparison is visible rather than retroactive.
KEY_DATE_COMPARISON_RULE_VERSION = "key-date-table-code-identity-v1"

# How a key date row is named in the Project Record's subject namespace.  A
# conflict subject adopted from a UCM workbook is `<sheet>!<row>`, so the two
# namespaces cannot collide.
KEY_DATE_SUBJECT_PREFIX = "key_date:"

# The Fact type carrying a key date's scheduled date.
SCHEDULED_DATE_FIELD = "need_date"

# The `target_field` an apparent removal names.  A removal is not about one
# column, and the whole-subject spelling `later_revision` already uses is kept
# so the word never means two things across the seam.
ENTIRE_SUBJECT = "entire_subject"

# The impact rule this module evaluates, and what it reads.  Named and
# versioned because it is a Derivation: a later rule is a later version, never
# a silent re-reading of an old delta.
IMPACT_RULE = "key_date_change_impact_v1"

# What became of one row of the table.  Closed: every populated row reaches
# exactly one of these and an unaccounted row is a defect, not a new case.
COMPARED = "compared"
NEW_KEY_DATE = "new_key_date"
PROCESSING_FAILURE = "processing_failure"
ROW_DISPOSITIONS = (COMPARED, NEW_KEY_DATE, PROCESSING_FAILURE)

# Why one row did not complete the reading contract.  Every one of these is a
# retained Processing Failure carrying the row's own identity, never a skip.
BLANK_CODE = "row_states_no_key_date_code"
REPEATED_CODE = "key_date_code_is_carried_by_more_than_one_row"
MISSING_DATE = "row_states_no_scheduled_date"
UNTYPEABLE_DATE = "scheduled_date_is_not_an_iso_calendar_date"

# Why an accepted key date has no counterpart in this export, and — separately
# — why a candidate was nevertheless not proposed as an apparent removal.
ABSENT_FROM_TABLE = "absent_from_table"
FAILED_IN_TABLE = "carried_by_a_row_that_did_not_complete_processing"
WITHHELD_UNSEALED = "export_is_not_a_complete_sealed_enumeration"


class KeyDateTableRefused(ValueError):
    """This project cannot capture this file as a Key Date table."""


@dataclass(frozen=True)
class KeyDateRow:
    """One row of the export, and what the comparison did with it."""

    source_row_key: str
    sheet_name: str
    row_number: int
    code: str | None
    name: str | None
    stated_date: str | None
    date_cell_range: str | None
    subject_identity: str | None
    disposition: str
    reason: str | None

    def as_payload(self) -> dict[str, Any]:
        return {
            "source_row_key": self.source_row_key,
            "sheet_name": self.sheet_name,
            "row_number": self.row_number,
            "code": self.code,
            "name": self.name,
            "stated_date": self.stated_date,
            "date_cell_range": self.date_cell_range,
            "subject_identity": self.subject_identity,
            "disposition": self.disposition,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RemovalCandidate:
    """One accepted key date this export does not carry, and what became of it."""

    subject_identity: str
    code: str
    reason: str
    proposed: bool
    withheld_reason: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "subject_identity": self.subject_identity,
            "code": self.code,
            "reason": self.reason,
            "proposed": self.proposed,
            "withheld_reason": self.withheld_reason,
        }


@dataclass(frozen=True)
class KeyDateTableAccounting:
    """Every row of the export, every retained column, and every absent key date.

    Complete by construction: `row_accounting.RowAccounting.finish` refuses a
    reading whose rows did not all reach a disposition.
    """

    sheet_name: str
    header_row_number: int
    source_row_key_rule: str
    rows: tuple[KeyDateRow, ...]
    removals: tuple[RemovalCandidate, ...]
    retained_columns: tuple[str, ...]
    receipt: dict[str, Any]

    def with_disposition(self, disposition: str) -> tuple[KeyDateRow, ...]:
        return tuple(row for row in self.rows if row.disposition == disposition)

    @property
    def processing_failures(self) -> tuple[KeyDateRow, ...]:
        return self.with_disposition(PROCESSING_FAILURE)

    def as_payload(self) -> dict[str, Any]:
        return {
            "sheet_name": self.sheet_name,
            "header_row_number": self.header_row_number,
            "source_row_key_rule": self.source_row_key_rule,
            "rows": [row.as_payload() for row in self.rows],
            "removals": [item.as_payload() for item in self.removals],
            "retained_columns": list(self.retained_columns),
        }


@dataclass(frozen=True)
class KeyDateImpact:
    """One appended delta and the impact derived from it."""

    delta_id: int
    derivation: ImpactDerivation

    def as_payload(self) -> dict[str, Any]:
        return {
            "delta_id": self.delta_id,
            "rule": self.derivation.rule,
            "inputs": self.derivation.inputs,
            "evaluated_at": self.derivation.evaluated_at.isoformat(),
            "affected_constraint_ids": list(
                self.derivation.affected_constraint_ids
            ),
            "affected_key_dates": list(self.derivation.affected_key_dates),
        }


@dataclass(frozen=True)
class KeyDateTableCapture:
    """The receipt of one Key Date table's capture and comparison."""

    project_id: int
    document_id: int
    extraction_run_id: int
    content_sha256: str
    external_identity: str
    external_version: str
    source_family: str
    source_revision: str
    accepted_baseline_revision: str | None
    accounting: KeyDateTableAccounting
    fact_ids: tuple[int, ...]
    delta_ids: tuple[int, ...]
    impacts: tuple[KeyDateImpact, ...]
    values_agreed: int

    @property
    def processing_failures(self) -> tuple[KeyDateRow, ...]:
        return self.accounting.processing_failures


def capture_key_date_table(
    session: Session,
    *,
    project: Project,
    staged: StagedSource,
    envelope: SourceEnvelope,
    principal: HumanPrincipal,
    is_complete_enumerative_source: bool = False,
    row_accounting_sealed: bool = False,
    analytics_binding: AnalyticsBinding | None = None,
    images_dir: Path | str | None = None,
    impact_evaluated_at: datetime | None = None,
) -> KeyDateTableCapture:
    """Capture one Key Date table, and propose its differences from the record.

    Runs in the caller's transaction.  Every refusal about the *file* happens
    before the first write; a row that does not complete the reading contract is
    a retained Processing Failure rather than a refusal of the whole export, so
    one unreadable date never hides the twenty readable ones.

    Nothing here writes an accepted value.  Facts are appended through the
    source-append commands and differences through `create_proposed_delta_group`,
    which are the only writes the runtime role holds at all (#492), so on an
    adopted-baseline project (#520) this act captures and proposes and can do
    nothing else.

    `envelope` is the ingress record #511 wrote for this delivery; the delivery
    must already stand in the ledger as `stored`, which is what makes the exact
    bytes, their digest, the customer's own identity for the export, and its
    external version retained rather than asserted.

    `is_complete_enumerative_source` and `row_accounting_sealed` are the
    caller's declaration about this delivery.  Both must be true before an
    accepted key date the export does not carry is proposed as an apparent
    removal; a filtered export declares neither and proposes nothing about what
    it leaves out.  No property of a spreadsheet says which one it is.
    """

    actor = require_human_principal(principal)
    impact_instant = impact_evaluated_at if impact_evaluated_at is not None else datetime.now(timezone.utc)
    if impact_instant.tzinfo is None:
        raise KeyDateTableRefused("impact evaluation needs an explicit timezone")
    delivery = _refuse_unbound_delivery(session, project, staged, envelope)

    path = staged_file(staged.sha256)
    if path is None:
        raise KeyDateTableRefused(
            "the staged export is no longer in the content store; upload it again"
        )
    reading = _read_key_date_table(path)

    accepted = accepted_values(session, project.id)
    baseline_revision = session.scalar(
        select(func.max(ProjectRecordRevision.id)).where(
            ProjectRecordRevision.project_id == project.id
        )
    )

    document_id = _register(session, project, staged, actor, images_dir, delivery)
    segments = _segments(session, document_id)
    plan = _plan(
        reading,
        segments,
        accepted,
        is_complete_enumerative_source=is_complete_enumerative_source,
        row_accounting_sealed=row_accounting_sealed,
    )

    run_id, fact_ids, captured = _capture_facts(
        session,
        project=project,
        document_id=document_id,
        segments=segments,
        plan=plan,
        actor=actor,
    )
    proposals, agreed = _proposals(plan, captured, accepted, baseline_revision)

    appended = create_proposed_delta_group(
        session,
        project_id=project.id,
        source_family=KEY_DATE_TABLE_FAMILY,
        source_revision=staged.sha256,
        document_id=document_id,
        deltas=proposals,
        is_complete_enumerative_source=is_complete_enumerative_source,
        row_accounting_sealed=row_accounting_sealed,
        analytics_binding=analytics_binding or binding_for_source(session, delivery),
    )
    impacts = _impacts(
        appended,
        accepted,
        evaluated_at=impact_instant,
        baseline_revision=baseline_revision,
    )
    from corridor.impact_derivations import append_impact_derivation

    for impact in impacts:
        append_impact_derivation(session, project_id=project.id,
            delta_id=impact.delta_id, derivation=impact.derivation)
    audit.record(
        session,
        principal=actor,
        action=audit.CAPTURE_KEY_DATE_TABLE,
        entity_type=audit.DOCUMENT,
        entity_id=document_id,
        after={
            "content_sha256": staged.sha256,
            "external_identity": envelope.external_identity,
            "external_version": envelope.external_version,
            "source_family": KEY_DATE_TABLE_FAMILY,
            "source_revision": staged.sha256,
            "reader_version": READER_VERSION,
            "accepted_baseline_revision": revision_label(baseline_revision) or "",
            "is_complete_enumerative_source": is_complete_enumerative_source,
            "row_accounting_sealed": row_accounting_sealed,
            "comparison_rule_version": KEY_DATE_COMPARISON_RULE_VERSION,
            "captured_facts": len(fact_ids),
            "values_agreed": agreed,
            "proposed_deltas": [row.id for row in appended],
            "impact_derivations": [item.as_payload() for item in impacts],
            "row_accounting": plan.accounting.as_payload(),
        },
    )
    session.flush()
    return KeyDateTableCapture(
        project_id=project.id,
        document_id=document_id,
        extraction_run_id=run_id,
        content_sha256=staged.sha256,
        external_identity=envelope.external_identity,
        external_version=envelope.external_version,
        source_family=KEY_DATE_TABLE_FAMILY,
        source_revision=staged.sha256,
        accepted_baseline_revision=revision_label(baseline_revision),
        accounting=plan.accounting,
        fact_ids=fact_ids,
        delta_ids=tuple(row.id for row in appended),
        impacts=impacts,
        values_agreed=agreed,
    )


def key_date_subject(code: str) -> str:
    """The Project Record subject identity one key date code is captured under."""

    return f"{KEY_DATE_SUBJECT_PREFIX}{code}"


def key_date_code(subject_identity: str) -> str | None:
    """The key date code a subject identity names, or ``None`` if it names none."""

    if not subject_identity.startswith(KEY_DATE_SUBJECT_PREFIX):
        return None
    return subject_identity[len(KEY_DATE_SUBJECT_PREFIX) :]


# --- refusals ---------------------------------------------------------------


def _refuse_unbound_delivery(
    session: Session,
    project: Project,
    staged: StagedSource,
    envelope: SourceEnvelope,
) -> SourceDelivery:
    """The exact bytes, digest, source identity, and external version are held.

    Checked rather than trusted: the ledger row is what retains the customer's
    own identity for the export and the external version it arrived at, and a
    capture that cannot name one has no revision identity to compare under.

    The proven row is returned rather than discarded: it is also the delivery
    this export's Document came in on (#687), already checked here to be
    ``stored``, to hold these exact bytes, and to belong to this project.
    """

    if envelope.content_digest != staged.sha256:
        raise KeyDateTableRefused(
            "the delivered digest is not the staged export's digest"
        )
    if not envelope.external_identity.strip() or not envelope.external_version.strip():
        raise KeyDateTableRefused(
            "a Key Date table names the customer's own identity for the export "
            "and the external version it arrived at"
        )
    delivery = stored_delivery(session, idempotency_key=envelope.idempotency_key)
    if delivery is None or delivery.content_sha256 != staged.sha256:
        raise KeyDateTableRefused(
            "no stored delivery holds these exact bytes; take delivery of the "
            "export before capturing it"
        )
    if delivery.project_id != project.id:
        raise KeyDateTableRefused("this delivery was taken for another project")
    return delivery


# --- the reading ------------------------------------------------------------


@dataclass(frozen=True)
class _RawRow:
    """One populated row of the export, before anything is resolved."""

    source_row_key: str
    sheet_name: str
    row_number: int
    code_cell_range: str
    name_cell_range: str
    date_cell_range: str


@dataclass(frozen=True)
class _Reading:
    """The one worksheet this format's headings name, and its populated rows."""

    sheet_name: str
    header_row_number: int
    rows: tuple[_RawRow, ...]
    retained_columns: tuple[str, ...]


def _read_key_date_table(path: Path) -> _Reading:
    """Locate the declared headings and every populated row beneath them.

    Deterministic and total: the sheet is found by its printed headings, the
    row key is the worksheet name and the row number the customer sees in
    Excel, and every populated row is returned.  Nothing is filtered here —
    filtering is what makes a silent skip possible, so a row's disposition is
    decided once, in `_plan`, where it is accounted for.
    """

    workbook = load_workbook(path, data_only=True)
    try:
        for sheet in workbook.worksheets:
            located = _locate(sheet)
            if located is None:
                continue
            header_row_number, columns, retained = located
            rows = tuple(
                _RawRow(
                    source_row_key=f"{sheet.title}!{number}",
                    sheet_name=sheet.title,
                    row_number=number,
                    code_cell_range=f"{columns[CODE_COLUMN]}{number}",
                    name_cell_range=f"{columns[NAME_COLUMN]}{number}",
                    date_cell_range=f"{columns[DATE_COLUMN]}{number}",
                )
                for number in _populated_rows(sheet, header_row_number)
            )
            return _Reading(
                sheet_name=sheet.title,
                header_row_number=header_row_number,
                rows=rows,
                retained_columns=retained,
            )
    finally:
        workbook.close()
    raise KeyDateTableRefused(
        "no worksheet of this file carries the Key Date table's headings "
        f"({', '.join(DECLARED_COLUMNS)}); this is not the format this adapter reads"
    )


def _locate(sheet) -> tuple[int, dict[str, str], tuple[str, ...]] | None:
    """The header row, the declared columns' letters, and any retained heading."""

    for row in sheet.iter_rows():
        headings = {
            str(cell.value).strip().lower(): get_column_letter(cell.column)
            for cell in row
            if cell.value is not None and str(cell.value).strip()
        }
        if not headings:
            continue
        if not all(name in headings for name in DECLARED_COLUMNS):
            # The first populated row of a worksheet is its header row. A sheet
            # whose header is not this format's is not this format, and reading
            # further down for a second candidate header is how a mapping tier
            # gets in through the back door.
            return None
        return (
            row[0].row,
            {name: headings[name] for name in DECLARED_COLUMNS},
            tuple(sorted(set(headings) - set(DECLARED_COLUMNS))),
        )
    return None


def _populated_rows(sheet, header_row_number: int) -> tuple[int, ...]:
    """Every worksheet row below the header carrying at least one value."""

    numbers: list[int] = []
    for row in sheet.iter_rows(min_row=header_row_number + 1):
        if not row:
            continue
        if any(cell.value is not None and str(cell.value).strip() for cell in row):
            numbers.append(row[0].row)
    return tuple(numbers)


# --- the comparison plan ----------------------------------------------------


@dataclass(frozen=True)
class _RowPlan:
    """One export row, its resolved subject, and what to do with it."""

    row: _RawRow
    code: str
    subject_identity: str
    disposition: str


@dataclass(frozen=True)
class _Plan:
    """Every row's resolution, the removal candidates, and the accounting."""

    rows: tuple[_RowPlan, ...]
    removals: tuple[RemovalCandidate, ...]
    accounting: KeyDateTableAccounting


def _plan(
    reading: _Reading,
    segments: dict[tuple[str, str], SourceSegment],
    accepted: dict[tuple[str, str], Any],
    *,
    is_complete_enumerative_source: bool,
    row_accounting_sealed: bool,
) -> _Plan:
    """Resolve every row against the accepted record, and account for all of them.

    The date is typed here, from the registered segment's own exact text and
    through the Fact type's released transformation, so a value no released
    transformation can read becomes a visible Processing Failure with the exact
    text it failed on — never a row that quietly produced no Fact.
    """

    accepted_codes = _accepted_key_dates(accepted)
    counts: dict[str, int] = {}
    for row in reading.rows:
        code = _cell_text(segments, reading.sheet_name, row.code_cell_range)
        if code:
            counts[code] = counts.get(code, 0) + 1

    accounting = RowAccounting(reader_version=READER_VERSION, reader_path=READER_PATH)
    for row in reading.rows:
        accounting.detect(row.source_row_key, page=1, row_number=row.row_number)

    plans: list[_RowPlan] = []
    records: list[KeyDateRow] = []
    failed_codes: set[str] = set()
    for row in reading.rows:
        code = _cell_text(segments, reading.sheet_name, row.code_cell_range)
        name = _cell_text(segments, reading.sheet_name, row.name_cell_range)
        stated = _cell_text(segments, reading.sheet_name, row.date_cell_range)
        subject = key_date_subject(code) if code else None
        reason = _row_failure(code, stated, counts, segments, reading, row)
        if reason is not None:
            disposition = PROCESSING_FAILURE
            if code:
                failed_codes.add(code)
        elif code in accepted_codes:
            disposition = COMPARED
        else:
            disposition = NEW_KEY_DATE
        accounting.account(
            row.source_row_key,
            disposition="skipped" if reason is not None else "extracted",
            reason=reason or disposition,
        )
        if reason is None and code and subject is not None:
            plans.append(
                _RowPlan(
                    row=row,
                    code=code,
                    subject_identity=subject,
                    disposition=disposition,
                )
            )
        records.append(
            KeyDateRow(
                source_row_key=row.source_row_key,
                sheet_name=reading.sheet_name,
                row_number=row.row_number,
                code=code or None,
                name=name or None,
                stated_date=stated or None,
                date_cell_range=row.date_cell_range,
                subject_identity=None if reason is not None else subject,
                disposition=disposition,
                reason=reason,
            )
        )

    removals = _removal_candidates(
        accepted_codes,
        carried={item.code for item in plans},
        failed=failed_codes,
        is_complete_enumerative_source=is_complete_enumerative_source,
        row_accounting_sealed=row_accounting_sealed,
    )
    receipt = accounting.finish(plans).row_accounting
    return _Plan(
        rows=tuple(plans),
        removals=removals,
        accounting=KeyDateTableAccounting(
            sheet_name=reading.sheet_name,
            header_row_number=reading.header_row_number,
            source_row_key_rule="sheet_name!worksheet_row_number",
            rows=tuple(records),
            removals=removals,
            retained_columns=reading.retained_columns,
            receipt=receipt,
        ),
    )


def _row_failure(
    code: str,
    stated: str,
    counts: dict[str, int],
    segments: dict[tuple[str, str], SourceSegment],
    reading: _Reading,
    row: _RawRow,
) -> str | None:
    """Why this row did not complete the reading contract, or ``None``."""

    if not code:
        return BLANK_CODE
    if counts.get(code, 0) > 1:
        return REPEATED_CODE
    if not stated:
        return MISSING_DATE
    segment = segments.get((reading.sheet_name, row.date_cell_range))
    if segment is None:
        return MISSING_DATE
    try:
        validated_scalar_value(
            FACT_TYPE_CONTRACTS[SCHEDULED_DATE_FIELD], segment.exact_text
        )
    except FactValidationError:
        return UNTYPEABLE_DATE
    return None


def _accepted_key_dates(accepted: dict[tuple[str, str], Any]) -> dict[str, str]:
    """The accepted key date subjects, keyed by the code each one names.

    Only subjects in the key date namespace are read.  An adopted Utility
    Conflict also carries a `need_date`, and treating one as a key date this
    export failed to mention would propose removing the customer's conflicts
    from their own record.
    """

    codes: dict[str, str] = {}
    for subject, _field in accepted:
        code = key_date_code(subject)
        if code:
            codes[code] = subject
    return codes


def _removal_candidates(
    accepted_codes: dict[str, str],
    *,
    carried: set[str],
    failed: set[str],
    is_complete_enumerative_source: bool,
    row_accounting_sealed: bool,
) -> tuple[RemovalCandidate, ...]:
    """Accepted key dates this export does not carry, and whether to propose one."""

    sealed = is_complete_enumerative_source and row_accounting_sealed
    candidates: list[RemovalCandidate] = []
    for code, subject in sorted(accepted_codes.items()):
        if code in carried:
            continue
        # A key date the export *does* name, on a row that did not complete
        # processing, is not an absent key date. Proposing its removal would
        # turn one unreadable cell into a proposal to drop the whole key date.
        reason = FAILED_IN_TABLE if code in failed else ABSENT_FROM_TABLE
        withheld = (
            WITHHELD_UNSEALED
            if not sealed
            else (FAILED_IN_TABLE if code in failed else None)
        )
        candidates.append(
            RemovalCandidate(
                subject_identity=subject,
                code=code,
                reason=reason,
                proposed=withheld is None,
                withheld_reason=withheld,
            )
        )
    return tuple(candidates)


# --- capture ----------------------------------------------------------------


def _register(
    session: Session,
    project: Project,
    staged: StagedSource,
    actor: HumanPrincipal,
    images_dir: Path | str | None,
    delivery: SourceDelivery,
) -> int:
    """Register the exact export bytes as this project's own Document.

    The Document carries the delivery it came in on, in this same transaction,
    so a Proposed Delta appended from it reaches the append-only boundary its
    issue's coverage was confirmed against (#675, #687).
    """

    intake = preview_intake(session, project, staged, KEY_DATE_TABLE_DOC_TYPE)
    try:
        confirmation = confirm_intake(
            session,
            project=project,
            sha256=staged.sha256,
            filename=staged.filename,
            doc_type=KEY_DATE_TABLE_DOC_TYPE,
            binding_fingerprint=intake.binding_fingerprint,
            principal=actor,
            images_dir=images_dir,
            source_delivery_id=int(delivery.id),
        )
    except IntakeConflict as exc:
        raise KeyDateTableRefused(str(exc)) from exc
    return confirmation.document_id


def _segments(
    session: Session, document_id: int
) -> dict[tuple[str, str], SourceSegment]:
    """This export's registered cells, addressed the way the reader names them."""

    return {
        (segment.sheet_name, segment.cell_range): segment
        for segment in session.scalars(
            select(SourceSegment).where(
                SourceSegment.document_id == document_id,
                SourceSegment.kind == "spreadsheet_cell",
            )
        ).all()
    }


def _cell_text(
    segments: dict[tuple[str, str], SourceSegment], sheet_name: str, cell_range: str
) -> str:
    """The exact registered text of one cell, or empty when the cell is blank."""

    segment = segments.get((sheet_name, cell_range))
    return segment.exact_text.strip() if segment is not None else ""


def _capture_facts(
    session: Session,
    *,
    project: Project,
    document_id: int,
    segments: dict[tuple[str, str], SourceSegment],
    plan: _Plan,
    actor: HumanPrincipal,
) -> tuple[int, tuple[int, ...], dict[str, Fact]]:
    """Capture what the export says about every readable key date, changed or not.

    A Source Fact is what the source says, so a key date the export restates
    unchanged is still captured; #519 refuses a decision whose Fact carries no
    effective value support, so the Support Assessment travels with it.
    """

    run = record_extraction_run(
        session,
        session.get_one(Document, document_id),
        prompt_version=READER_VERSION,
        schema_version=READER_VERSION,
        candidate_count=0,
        page_errors=0,
        outcome="completed",
        model=None,
        extractor_config=deployed_extractor_config("key_date_table", client=None),
        token_usage=zero_token_usage(document_id),
        row_accounting_json=None,
    )
    assessed_at = _capture_instant(session, document_id)
    fact_ids: list[int] = []
    captured: dict[str, Fact] = {}
    for item in plan.rows:
        segment = segments[(item.row.sheet_name, item.row.date_cell_range)]
        fact = append_fact(
            session,
            project_id=project.id,
            document_id=document_id,
            extraction_run_id=run.id,
            subject_kind="source_row",
            subject_key=item.subject_identity,
            recorded_by=f"importer:{READER_VERSION}",
            content_sha256=_fact_digest(document_id, item.subject_identity, segment),
            value=materialize_segment_value(session, SCHEDULED_DATE_FIELD, segment),
        )
        record_support_assessment(
            session,
            project_id=project.id,
            proposition=FactProposition(fact_id=fact.id),
            source_segment_ids=(segment.id,),
            evidence_role="value_support",
            assessment="supported",
            authority=actor,
            assessed_at=assessed_at,
        )
        fact_ids.append(fact.id)
        captured[item.subject_identity] = fact
    session.flush()
    return run.id, tuple(fact_ids), captured


def _capture_instant(session: Session, document_id: int) -> datetime:
    """When this capture happened, taken from the Document the database timed.

    Not `datetime.now`: the assessment instant and the impact evaluation instant
    have to be the same on a replay that re-reads the same registered Document,
    and both must come from the same clock every other row of this transaction
    was stamped by.
    """

    created = session.scalar(
        select(Document.created_at).where(Document.id == document_id)
    )
    if created is None:
        raise KeyDateTableRefused("the registered export has no creation time")
    return created if created.tzinfo else created.replace(tzinfo=timezone.utc)


def _fact_digest(
    document_id: int, subject_identity: str, segment: SourceSegment
) -> str:
    """The Fact's identity, which includes the rendition that stated it.

    A Fact digest is globally unique and `append_fact` returns the row that
    already carries one, so leaving the rendition out would make the second
    export's unchanged key date *the first export's Fact* — a Fact bound to one
    document with a Support Assessment citing another document's cell, which
    the append command refuses outright. Two renditions stating the same date
    are two Source Facts: each says what its own source says.  The registered
    Document is content-addressed, so a replay of the same bytes reaches the
    same document and the same digest.
    """

    return sha256(
        json.dumps(
            {
                "reader_version": READER_VERSION,
                "document_id": document_id,
                "subject_identity": subject_identity,
                "field": SCHEDULED_DATE_FIELD,
                "segment": segment.content_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


# --- the proposed differences ----------------------------------------------


def _proposals(
    plan: _Plan,
    captured: dict[str, Fact],
    accepted: dict[tuple[str, str], Any],
    baseline_revision: int | None,
) -> tuple[tuple[ProposedDeltaValues, ...], int]:
    """The typed differences and the count of dates the record already held.

    A stated date equal to the accepted one produces nothing, which is the whole
    point of comparing: an export of two hundred key dates that moved three of
    them proposes three changes.
    """

    label = revision_label(baseline_revision)
    proposals: list[ProposedDeltaValues] = []
    agreed = 0
    for item in plan.rows:
        fact = captured.get(item.subject_identity)
        if fact is None:
            continue
        stated = fact_value(fact)
        key = (item.subject_identity, SCHEDULED_DATE_FIELD)
        if item.disposition == COMPARED:
            if key in accepted:
                if accepted[key] == stated:
                    agreed += 1
                    continue
                change_type = "modify"
            else:
                change_type = "add"
            proposals.append(
                ProposedDeltaValues(
                    change_type=change_type,
                    target=ExistingSubjectTarget(
                        subject_identity=item.subject_identity,
                        field=SCHEDULED_DATE_FIELD,
                    ),
                    accepted_value=accepted.get(key),
                    proposed_value=stated,
                    comparison_rule_version=KEY_DATE_COMPARISON_RULE_VERSION,
                    accepted_baseline_revision=label,
                )
            )
            continue
        proposals.append(
            ProposedDeltaValues(
                change_type="add",
                target=ProposedSubjectTarget(
                    subject_identity=item.subject_identity,
                    proposed_fields=(SCHEDULED_DATE_FIELD,),
                ),
                proposed_value={SCHEDULED_DATE_FIELD: stated},
                comparison_rule_version=KEY_DATE_COMPARISON_RULE_VERSION,
                accepted_baseline_revision=label,
            )
        )
    for candidate in plan.removals:
        if not candidate.proposed:
            continue
        proposals.append(
            ProposedDeltaValues(
                change_type="apparent_removal",
                target=ExistingSubjectTarget(
                    subject_identity=candidate.subject_identity,
                    field=ENTIRE_SUBJECT,
                ),
                accepted_value={
                    field_name: value
                    for (subject, field_name), value in sorted(accepted.items())
                    if subject == candidate.subject_identity
                },
                proposed_value=None,
                comparison_rule_version=KEY_DATE_COMPARISON_RULE_VERSION,
                accepted_baseline_revision=label,
            )
        )
    return tuple(proposals), agreed


# --- the derived impact -----------------------------------------------------


def _stated_date(delta) -> Any:
    """The date one delta states, whichever target shape carries it.

    A change to an accepted key date carries the date itself; a key date the
    record does not hold carries its initial fields as one object, because
    ADR-0082 makes a new subject one atomic change rather than one delta per
    field.  The derivation names the same thing in both shapes.
    """

    value = delta.proposed_value
    if isinstance(value, dict):
        return value.get(SCHEDULED_DATE_FIELD)
    return value


def _impacts(
    appended: Sequence[Any],
    accepted: dict[tuple[str, str], Any],
    *,
    evaluated_at: datetime,
    baseline_revision: int | None,
) -> tuple[KeyDateImpact, ...]:
    """What each proposed key date change would reach, if it were resolved.

    Read from the accepted record and nowhere else.  A Constraint's Required By
    is calculated from the exact Key Date Version it serves, so the Constraints
    a moved key date reaches are the accepted subjects whose accepted Required By
    is that key date's currently accepted date.  A key date the record does not
    yet hold reaches nothing, and the derivation says so in its inputs rather
    than reporting an empty set that looks like a computed answer.
    """

    impacts: list[KeyDateImpact] = []
    for delta in appended:
        code = key_date_code(delta.target_subject_identity)
        if code is None:
            continue
        accepted_date = accepted.get(
            (delta.target_subject_identity, SCHEDULED_DATE_FIELD)
        )
        affected = (
            tuple(
                sorted(
                    subject
                    for (subject, field_name), value in accepted.items()
                    if field_name == SCHEDULED_DATE_FIELD
                    and key_date_code(subject) is None
                    and value == accepted_date
                )
            )
            if accepted_date is not None
            else ()
        )
        impacts.append(
            KeyDateImpact(
                delta_id=delta.id,
                derivation=ImpactDerivation(
                    rule=IMPACT_RULE,
                    inputs={
                        "key_date_code": code,
                        "change_type": delta.change_type,
                        "accepted_key_date": accepted_date,
                        "accepted_record_count": sum(
                            field_name == SCHEDULED_DATE_FIELD
                            for _, field_name in accepted
                        ),
                        "proposed_key_date": _stated_date(delta),
                        "accepted_baseline_revision": revision_label(
                            baseline_revision
                        ),
                        "reads": (
                            "current_project_record.need_date"
                            if accepted_date is not None
                            else "the record holds no accepted date for this key date"
                        ),
                    },
                    evaluated_at=evaluated_at,
                    affected_constraint_ids=affected,
                    affected_key_dates=(code,),
                ),
            )
        )
    return tuple(impacts)
