"""Capture a later UCM revision as Source Facts and Proposed Deltas (#606).

Adopt Baseline (`baseline_adoption`, #509) establishes the accepted record from
the customer's **first** workbook and stops there.  The generic delta pass
(`delta_generation`, #488) compares Source Facts some other component has
already captured and cannot itself read a workbook.  So "the customer sent us
their next UCM" had no path at all: no Facts, therefore no Proposed Deltas.
This module is that path, and nothing else.  It reads one later revision of an
already-adopted workbook family, captures what it says as Source Facts, and
appends the typed differences from the accepted record as Proposed Deltas.  It
makes **no accepted-record write**; #519 and #526 decide what it proposes.

**One reader, one identity rule, reused rather than restated.**  The workbook
is read by the same `baseline_workbook.read_baseline_workbook` adoption uses,
and the rows are resolved by the same `baseline_adoption.assign_record_subjects`
adoption uses, so "the same row" means the same thing in both acts.  A second
reader, or a second identity rule, would let a value be adopted under one
reading and compared under another; that is the failure this reuse exists to
prevent.  The matching side is read from the accepted record itself — each
accepted subject's own `utility_id` value — rather than from the adoption
receipt, so a subject accepted from an earlier revision's new-subject delta is
matched on the next revision like any other.

**The mapping revision is read back, not reconstructed by the caller.**  The
declaration is stored beside its registration (#610), so the manifest a later
revision is read through is resolved from that stored state rather than rebuilt
byte-for-byte by whoever hands us the delivery (#622).  A caller that already
holds the declaration may still supply it, and it is still verified against the
registered identity, version and digest; a registration written before that
storage existed names a revision Corridor cannot resolve, and is refused by
name rather than standing in as an empty mapping.

**The Row Identification Rule is never guessed from a printed row number.**
The glossary is explicit that a matrix row is identified by its conflict number
alone or by its owner and conflict number together, and never by where it sits
on the sheet.  Adoption's `business_identity` is the conflict number, so that is
the rule in force, and a row that moved down the sheet between revisions is
still the same row.  It follows that a later row carrying an adopted conflict
number under a *different* utility owner is proposed as an ordinary
`external_org` change, which #528's partition already holds out on its own
focused item as an owner mismatch — the identity question reaches a person
instead of being buried in a batch of routine edits.

**A repeated conflict number is ambiguous, and stays ambiguous.**  Adoption
deliberately keeps two rows sharing one conflict number as two subjects and
asks the coordinator about them.  Pairing this revision's first `UC-2` with the
baseline's first `UC-2` would be exactly the identity guessed from a printed
row number that the glossary forbids, so a conflict number carried by more than
one row on either side is never paired.  Each unmatched row of such a group
becomes its own focused `proposed_subject` item, and a row whose complete set of
values already stands accepted under that conflict number produces nothing at
all — the revision says nothing new about it.  An accepted subject that
disappears out of an ambiguous group is recorded in the accounting and produces
no removal: nobody can say *which* of two identically numbered facilities went.

**Apparent removal needs the seal, and a partial export never reads as one.**
`proposed_deltas.create_proposed_delta_group` refuses an `apparent_removal`
without both `is_complete_enumerative_source` and `row_accounting_sealed`, and
this module additionally produces none unless the caller declared both, so an
unsealed or partial revision proposes nothing about the rows it does not carry.
Both flags are the caller's declaration about the delivery, not something read
off the bytes: no property of a spreadsheet says whether it is the customer's
whole population or a filtered view of it.

**Row accounting is a receipt, not a count.**  Every populated row of the
adopted worksheet reaches an explicit disposition through `row_accounting`,
whose `finish` refuses a reading with an unaccounted row, and the complete
accounting — every row, every unknown column, every untypeable value, and every
accepted subject the revision does not carry — is retained as one append-only
`audit.CAPTURE_LATER_SOURCE_REVISION` entry in the same transaction.  It is not
retained on the Extraction Run: that column's accounting is defined against
legacy Candidate counts, and producing Candidates to satisfy it would be a
legacy write this path is forbidden to make (ADR-0081).

**What was tried and rejected.**  Feeding the later workbook through the
existing native extractor and letting `delta_generation` compare the result was
tried first: it drops retired rows and rows missing a required field, it keys
Facts by its own subject rule rather than adoption's, and it has no notion of a
row that is absent — so the three cases this ticket exists for (identity
mismatch, ambiguity, apparent removal) all disappear before the comparison.
Storing a second row-identity table keyed to this revision was rejected because
the identity is the conflict number, which the accepted record already holds.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corridor import audit
from corridor.analytics import AnalyticsBinding
from corridor.measurement_collection import binding_for_source
from corridor.baseline_adoption import (
    FormatIdentity,
    PreviewRow,
    adopted_baseline_source,
    assign_record_subjects,
    effective_baseline_formats,
    effective_field_mapping_manifest,
    format_identity_of,
)
from corridor.baseline_workbook import (
    IMPORTER_VERSION,
    BaselineWorkbookUnsupported,
    OperationsReading,
    read_baseline_workbook,
)
from corridor.connectors.pull_connector import SourceEnvelope
from corridor.delta_generation import (
    COMPARABLE_FACT_TYPES,
    accepted_values,
    fact_value,
    revision_label,
)
from corridor.extraction_runs import record_extraction_run
from corridor.extractor_lineage import deployed_extractor_config, zero_token_usage
from corridor.field_mapping_manifest import FieldMappingManifest, conformance_refusals
from corridor.materializer import materialize_segment_value
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


# The document kind a UCM workbook is registered under, the same one adoption
# registers the baseline as.
REVISION_DOC_TYPE = "matrix"

# The rule this module compares under, recorded on every delta it appends so a
# later change to the comparison is visible rather than retroactive.  It is
# deliberately not `delta_generation.COMPARISON_RULE_VERSION`: that pass
# compares Facts whose subject identity someone else already resolved, and this
# one resolves the identity itself.
REVISION_COMPARISON_RULE_VERSION = "later-source-revision-row-identity-v1"

# How the reader names itself in the row-accounting receipt.
REVISION_READER_PATH = "spreadsheet_cells"

# The canonical field carrying the conflict number, which is what a matrix row
# is identified by (`baseline_workbook` reads it into `business_identity`).
BUSINESS_IDENTITY_FIELD = "utility_id"

# The `target_field` an apparent removal names.  A removal is not about one
# column, and the whole-subject spelling already in use for one is kept so the
# word never means two things across the seam.
ENTIRE_SUBJECT = "entire_subject"

# What became of one row of the later revision.  Closed, because every row of
# the adopted worksheet reaches exactly one of them and an unaccounted row is a
# defect rather than a new case.
COMPARED = "compared"
NEW_SUBJECT = "new_subject"
AMBIGUOUS_IDENTITY = "ambiguous_identity"
UNCHANGED_UNDER_AMBIGUOUS_IDENTITY = "unchanged_under_ambiguous_identity"
NOT_ADOPTABLE = "not_adoptable"
ROW_DISPOSITIONS = (
    COMPARED,
    NEW_SUBJECT,
    AMBIGUOUS_IDENTITY,
    UNCHANGED_UNDER_AMBIGUOUS_IDENTITY,
    NOT_ADOPTABLE,
)

# Why an accepted subject has no counterpart in this revision, and — separately
# — why a candidate was nevertheless not proposed as an apparent removal.
ABSENT_FROM_REVISION = "absent_from_revision"
NOT_ADOPTABLE_IN_REVISION = "not_adoptable_in_revision"
WITHHELD_UNSEALED = "revision_is_not_a_complete_sealed_enumeration"
WITHHELD_AMBIGUOUS = "conflict_number_is_carried_by_more_than_one_row"
WITHHELD_NO_BUSINESS_IDENTITY = "accepted_subject_states_no_conflict_number"


class LaterRevisionRefused(ValueError):
    """This project cannot capture this file as a later revision of its source."""


class LaterRevisionOperationsUnresolved(LaterRevisionRefused):
    """Corridor operations has not settled this workbook's mechanics.

    Its own type for the same reason `BaselineOperationsUnresolved` is: it
    carries importer diagnostics, and a caller catching the broad refusal must
    still not render one of these to a project person.
    """

    def __init__(self, reading: OperationsReading) -> None:
        super().__init__(
            "Corridor operations has not resolved this workbook: "
            + "; ".join(
                f"{item.code} {item.locator or ''}".strip()
                for item in reading.blocking_diagnostics
            )
            + ("; round trip mismatched" if not reading.round_trip.clean else "")
        )
        self.reading = reading


@dataclass(frozen=True)
class RevisionRow:
    """One row of the later revision, and what the comparison did with it."""

    source_row_key: str
    sheet_name: str
    row_number: int
    business_identity: str | None
    record_subject_key: str | None
    subject_identity: str | None
    disposition: str
    reason: str | None
    external_system_id: str | None
    source_url: str | None
    value_count: int
    unsupported_count: int
    retained_count: int

    def as_payload(self) -> dict[str, Any]:
        return {
            "source_row_key": self.source_row_key,
            "sheet_name": self.sheet_name,
            "row_number": self.row_number,
            "business_identity": self.business_identity,
            "record_subject_key": self.record_subject_key,
            "subject_identity": self.subject_identity,
            "disposition": self.disposition,
            "reason": self.reason,
            "external_system_id": self.external_system_id,
            "source_url": self.source_url,
            "value_count": self.value_count,
            "unsupported_count": self.unsupported_count,
            "retained_count": self.retained_count,
        }


@dataclass(frozen=True)
class RemovalCandidate:
    """One accepted subject this revision does not carry, and what became of it."""

    subject_identity: str
    business_identity: str | None
    reason: str
    proposed: bool
    withheld_reason: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "subject_identity": self.subject_identity,
            "business_identity": self.business_identity,
            "reason": self.reason,
            "proposed": self.proposed,
            "withheld_reason": self.withheld_reason,
        }


@dataclass(frozen=True)
class RetainedColumn:
    """One printed heading no canonical field names, and what it held."""

    heading: str
    column: str
    populated_cells: int

    def as_payload(self) -> dict[str, Any]:
        return {
            "heading": self.heading,
            "column": self.column,
            "populated_cells": self.populated_cells,
        }


@dataclass(frozen=True)
class UnsupportedCell:
    """One populated cell no released transformation can type, kept as text."""

    field: str
    cell_range: str
    exact_text: str
    reason: str
    material: bool

    def as_payload(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "cell_range": self.cell_range,
            "exact_text": self.exact_text,
            "reason": self.reason,
            "material": self.material,
        }


@dataclass(frozen=True)
class RevisionAccounting:
    """Every row, column, and untypeable value of the later revision.

    Complete by construction: `row_accounting.RowAccounting.finish` refuses a
    reading whose rows did not all reach a disposition, and its receipt is what
    the capture's audit entry retains.
    """

    sheet_name: str
    header_row_number: int
    source_row_key_rule: str
    rows: tuple[RevisionRow, ...]
    removals: tuple[RemovalCandidate, ...]
    retained_columns: tuple[RetainedColumn, ...]
    unsupported_values: tuple[UnsupportedCell, ...]
    receipt: dict[str, Any]

    def with_disposition(self, disposition: str) -> tuple[RevisionRow, ...]:
        return tuple(row for row in self.rows if row.disposition == disposition)

    @property
    def retained_cell_count(self) -> int:
        return sum(row.retained_count for row in self.rows)

    def as_payload(self) -> dict[str, Any]:
        return {
            "sheet_name": self.sheet_name,
            "header_row_number": self.header_row_number,
            "source_row_key_rule": self.source_row_key_rule,
            "rows": [row.as_payload() for row in self.rows],
            "removals": [item.as_payload() for item in self.removals],
            "retained_columns": [
                item.as_payload() for item in self.retained_columns
            ],
            "unsupported_values": [
                item.as_payload() for item in self.unsupported_values
            ],
        }


@dataclass(frozen=True)
class LaterRevisionCapture:
    """The receipt of one later revision's capture and comparison."""

    project_id: int
    document_id: int
    extraction_run_id: int
    content_sha256: str
    external_identity: str
    external_version: str
    source_family: str
    source_revision: str
    accepted_baseline_revision: str | None
    field_mapping: FormatIdentity
    accounting: RevisionAccounting
    fact_ids: tuple[int, ...]
    delta_ids: tuple[int, ...]
    values_agreed: int


def capture_later_revision(
    session: Session,
    *,
    project: Project,
    staged: StagedSource,
    envelope: SourceEnvelope,
    manifest: FieldMappingManifest | None = None,
    principal: HumanPrincipal,
    is_complete_enumerative_source: bool = False,
    row_accounting_sealed: bool = False,
    analytics_binding: AnalyticsBinding | None = None,
    images_dir: Path | str | None = None,
) -> LaterRevisionCapture:
    """Capture one later revision of the adopted workbook, and propose its changes.

    Runs in the caller's transaction.  Every refusal happens before the first
    write.  Nothing here writes an accepted value: Facts are appended through
    the source-append commands and differences through
    `create_proposed_delta_group`, which are the only writes the runtime role
    holds at all (#492).

    `envelope` is the ingress record #511 wrote for this delivery; the delivery
    must already stand in the ledger as `stored`, which is what makes the exact
    bytes, their digest, the customer's own identity for the source, and its
    external version retained rather than asserted.  The file is read only
    through the mapping revision the project has registered, which is resolved
    from stored state when `manifest` is omitted (#610, #622); a supplied
    `manifest` is still accepted and still verified against the registered
    identity, version and digest.

    `is_complete_enumerative_source` and `row_accounting_sealed` are the
    caller's declaration about this delivery.  Both must be true before an
    absent row is proposed as an apparent removal; a partial export declares
    neither and proposes nothing about what it does not carry.
    """

    actor = require_human_principal(principal)
    delivery = _refuse_unbound_delivery(session, project, staged, envelope)
    manifest = _registered_mapping(session, project, manifest)

    path = staged_file(staged.sha256)
    if path is None:
        raise LaterRevisionRefused(
            "the staged revision is no longer in the content store; upload it again"
        )
    try:
        reading = read_baseline_workbook(
            path, external_references=manifest.external_reference_headings
        )
    except BaselineWorkbookUnsupported as exc:
        raise LaterRevisionRefused(str(exc)) from exc
    if not reading.resolved:
        raise LaterRevisionOperationsUnresolved(reading)

    refusals = conformance_refusals(manifest, reading)
    if refusals:
        raise LaterRevisionRefused(
            "this revision is not the registered mapping revision "
            f"{manifest.revision}: " + "; ".join(refusals)
        )

    accepted = accepted_values(session, project.id)
    baseline_revision = session.scalar(
        select(func.max(ProjectRecordRevision.id)).where(
            ProjectRecordRevision.project_id == project.id
        )
    )
    plan = _plan(
        reading,
        accepted,
        is_complete_enumerative_source=is_complete_enumerative_source,
        row_accounting_sealed=row_accounting_sealed,
    )

    document_id = _register(session, project, staged, actor, images_dir, delivery)
    run_id, fact_ids, captured = _capture_facts(
        session,
        project=project,
        document_id=document_id,
        reading=reading,
        plan=plan,
        actor=actor,
    )
    # The comparison reads the *captured* value, never the cell text: the
    # accepted projection hands back a typed value and a workbook hands back
    # whatever the customer typed, so comparing the two spellings would call a
    # date written another way a change.
    proposals, agreed, unchanged = _proposals(
        plan, captured, accepted, baseline_revision
    )
    accounting = _settled(plan.accounting, unchanged)

    family = _source_family(manifest)
    appended = create_proposed_delta_group(
        session,
        project_id=project.id,
        source_family=family,
        source_revision=staged.sha256,
        document_id=document_id,
        deltas=proposals,
        is_complete_enumerative_source=is_complete_enumerative_source,
        row_accounting_sealed=row_accounting_sealed,
        analytics_binding=analytics_binding or replace(
            binding_for_source(session, delivery),
            mapping_identity=f"{manifest.identity}:{manifest.version}",
        ),
    )
    audit.record(
        session,
        principal=actor,
        action=audit.CAPTURE_LATER_SOURCE_REVISION,
        entity_type=audit.DOCUMENT,
        entity_id=document_id,
        after={
            "content_sha256": staged.sha256,
            "external_identity": envelope.external_identity,
            "external_version": envelope.external_version,
            "source_family": family,
            "source_revision": staged.sha256,
            "field_mapping": format_identity_of(manifest).as_payload(),
            "accepted_baseline_revision": revision_label(baseline_revision) or "",
            "is_complete_enumerative_source": is_complete_enumerative_source,
            "row_accounting_sealed": row_accounting_sealed,
            "comparison_rule_version": REVISION_COMPARISON_RULE_VERSION,
            "captured_facts": len(fact_ids),
            "values_agreed": agreed,
            "proposed_deltas": [row.id for row in appended],
            "row_accounting": accounting.as_payload(),
        },
    )
    session.flush()
    return LaterRevisionCapture(
        project_id=project.id,
        document_id=document_id,
        extraction_run_id=run_id,
        content_sha256=staged.sha256,
        external_identity=envelope.external_identity,
        external_version=envelope.external_version,
        source_family=family,
        source_revision=staged.sha256,
        accepted_baseline_revision=revision_label(baseline_revision),
        field_mapping=format_identity_of(manifest),
        accounting=accounting,
        fact_ids=fact_ids,
        delta_ids=tuple(row.id for row in appended),
        values_agreed=agreed,
    )


# --- refusals ---------------------------------------------------------------


def _refuse_unbound_delivery(
    session: Session,
    project: Project,
    staged: StagedSource,
    envelope: SourceEnvelope,
) -> SourceDelivery:
    """The exact bytes, digest, source identity, and external version are held.

    Checked rather than trusted: the ledger row is what retains the customer's
    own identity for the source and the external version it arrived at, and a
    capture that cannot name one has no revision identity to compare under.

    The proven row is returned rather than discarded, because it is also the
    delivery this revision's Document came in on (#687). Every check a caller
    would otherwise have to repeat has already happened here: the disposition
    is ``stored``, the digest is the staged revision's own, and the project is
    this one. Re-deriving that link downstream would be a second rule that
    could disagree with this one.
    """

    if adopted_baseline_source(session, project.id) is None:
        raise LaterRevisionRefused(
            f"{project.slug} has adopted no baseline, so there is nothing for a "
            "later revision to be a revision of; Adopt Baseline runs first"
        )
    if envelope.content_digest != staged.sha256:
        raise LaterRevisionRefused(
            "the delivered digest is not the staged revision's digest"
        )
    if not envelope.external_identity.strip() or not envelope.external_version.strip():
        raise LaterRevisionRefused(
            "a later revision names the customer's own identity for the source "
            "and the external version it arrived at"
        )
    delivery = stored_delivery(session, idempotency_key=envelope.idempotency_key)
    if delivery is None or delivery.content_sha256 != staged.sha256:
        raise LaterRevisionRefused(
            "no stored delivery holds these exact bytes; take delivery of the "
            "revision before capturing it"
        )
    if delivery.project_id != project.id:
        raise LaterRevisionRefused(
            "this delivery was taken for another project"
        )
    return delivery


def _registered_mapping(
    session: Session, project: Project, manifest: FieldMappingManifest | None
) -> FieldMappingManifest:
    """The mapping revision in force, resolved from stored state or verified (#622).

    A later revision is read only through the mapping the project registered.
    Where the caller supplies nothing, the registered declaration is read back
    through `effective_field_mapping_manifest` (#610), so a caller no longer has
    to reconstruct the approved bytes before it can process a delivery.

    A supplied manifest is still accepted and still checked against the
    registered identity *and* digest (#597), so a manifest that merely reuses the
    registered name is refused: the declaration is what says whether two columns
    still carry two values, and only its own digest moves when that changes.

    A registration written before #610 stored declarations beside registrations
    names a revision Corridor cannot resolve.  That absence is refused by name,
    in the shape the renderer already refuses it, and is never read as an empty
    mapping or quietly replaced by one rebuilt from the delivered file.
    """

    registered = effective_baseline_formats(session, project.id).get("field_mapping")
    if registered is None:
        raise LaterRevisionRefused(
            f"{project.slug} has no registered field mapping; a later revision "
            "is processed only under one a designated person approved"
        )
    if manifest is None:
        resolved = effective_field_mapping_manifest(session, project.id)
        if resolved is None:
            raise LaterRevisionRefused(
                "this project's effective field-mapping registration "
                f"({registered.format_identity} {registered.format_version}, "
                f"{registered.content_sha256}) stores no declaration, so the "
                "mapping revision it names cannot be resolved from stored "
                "state. Supply the manifest that digests to it."
            )
        return resolved
    identity = format_identity_of(manifest)
    if (
        registered.format_identity != identity.identity
        or registered.format_version != identity.version
        or registered.content_sha256 != identity.content_sha256
    ):
        raise LaterRevisionRefused(
            f"{manifest.revision} is not the mapping revision in force for "
            f"{project.slug}; registering a successor is its own attributable "
            "act (register_baseline_format)"
        )
    return manifest


# --- the comparison plan ----------------------------------------------------


@dataclass(frozen=True)
class _RowPlan:
    """One later-revision row, its resolved subject, and what to do with it."""

    row: PreviewRow
    subject_identity: str
    disposition: str

    @property
    def source(self):
        """The workbook row itself, under the preview that resolved its subject."""

        return self.row.row


@dataclass(frozen=True)
class _Plan:
    """Every row's resolution, the removal candidates, and the accounting."""

    rows: tuple[_RowPlan, ...]
    removals: tuple[RemovalCandidate, ...]
    accounting: RevisionAccounting

    @property
    def captured(self) -> tuple[_RowPlan, ...]:
        """The rows whose values are captured as Source Facts."""

        return tuple(
            item
            for item in self.rows
            if item.disposition in (COMPARED, NEW_SUBJECT, AMBIGUOUS_IDENTITY)
        )


def _plan(
    reading: OperationsReading,
    accepted: dict[tuple[str, str], Any],
    *,
    is_complete_enumerative_source: bool,
    row_accounting_sealed: bool,
) -> _Plan:
    """Resolve every row against the accepted record, and account for all of them."""

    revision_rows = assign_record_subjects(reading.rows)
    accepted_by_identity = _accepted_by_business_identity(accepted)
    revision_by_identity: dict[str, list[PreviewRow]] = {}
    for item in revision_rows:
        if item.row.excluded or not item.row.business_identity:
            continue
        revision_by_identity.setdefault(item.row.business_identity, []).append(item)

    accounting = RowAccounting(
        reader_version=IMPORTER_VERSION, reader_path=REVISION_READER_PATH
    )
    for item in revision_rows:
        accounting.detect(
            item.row.source_row_key, page=1, row_number=item.row.row_number
        )

    plans: list[_RowPlan] = []
    records: list[RevisionRow] = []
    for item in revision_rows:
        identity = item.row.business_identity
        if item.row.excluded or not identity:
            disposition, reason = NOT_ADOPTABLE, item.row.exclusion_reason
            subject = None
        else:
            subjects = accepted_by_identity.get(identity, ())
            siblings = revision_by_identity.get(identity, ())
            if len(subjects) == 1 and len(siblings) == 1:
                disposition, reason = COMPARED, None
                subject = subjects[0]
            elif not subjects and len(siblings) == 1:
                disposition, reason = NEW_SUBJECT, None
                subject = _proposed_identity(item, accepted)
            else:
                disposition, reason = AMBIGUOUS_IDENTITY, WITHHELD_AMBIGUOUS
                subject = _proposed_identity(item, accepted)
        accounting.account(
            item.row.source_row_key,
            disposition=(
                "extracted"
                if disposition in (COMPARED, NEW_SUBJECT, AMBIGUOUS_IDENTITY)
                else "skipped"
            ),
            reason=reason or disposition,
        )
        if subject is not None:
            plans.append(
                _RowPlan(row=item, subject_identity=subject, disposition=disposition)
            )
        records.append(
            RevisionRow(
                source_row_key=item.row.source_row_key,
                sheet_name=item.row.sheet_name,
                row_number=item.row.row_number,
                business_identity=identity,
                record_subject_key=item.record_subject_key,
                subject_identity=subject,
                disposition=disposition,
                reason=reason,
                external_system_id=item.row.external_system_id,
                source_url=item.row.source_url,
                value_count=len(item.row.values),
                unsupported_count=len(item.row.unsupported),
                retained_count=len(item.row.retained),
            )
        )

    removals = _removal_candidates(
        accepted,
        accepted_by_identity,
        revision_by_identity,
        revision_rows,
        is_complete_enumerative_source=is_complete_enumerative_source,
        row_accounting_sealed=row_accounting_sealed,
    )
    receipt = accounting.finish(plans).row_accounting
    return _Plan(
        rows=tuple(plans),
        removals=removals,
        accounting=RevisionAccounting(
            sheet_name=reading.adopted_sheet,
            header_row_number=reading.header_row_number,
            source_row_key_rule=reading.source_row_key_rule,
            rows=tuple(records),
            removals=removals,
            retained_columns=tuple(
                RetainedColumn(
                    heading=column.heading,
                    column=column.column,
                    populated_cells=column.populated_cells,
                )
                for column in reading.unknown_columns
            ),
            unsupported_values=tuple(
                UnsupportedCell(
                    field=value.field,
                    cell_range=value.cell_range,
                    exact_text=value.exact_text,
                    reason=value.reason,
                    material=value.material,
                )
                for value in reading.unsupported_values
            ),
            receipt=receipt,
        ),
    )


def _accepted_by_business_identity(
    accepted: dict[tuple[str, str], Any]
) -> dict[str, tuple[str, ...]]:
    """Accepted subjects grouped by the conflict number each of them states."""

    grouped: dict[str, list[str]] = {}
    for (subject, field_name), value in accepted.items():
        if field_name != BUSINESS_IDENTITY_FIELD or not isinstance(value, str):
            continue
        grouped.setdefault(value, []).append(subject)
    return {identity: tuple(sorted(rows)) for identity, rows in grouped.items()}


def _proposed_identity(
    item: PreviewRow, accepted: dict[tuple[str, str], Any]
) -> str:
    """The identity a subject the record does not hold is proposed under.

    The customer's own conflict number, carrying adoption's repeat suffix, so a
    proposed subject is named the way a person reading the workbook would name
    it.  Accepted subjects are named by their adopted source-row key, so the two
    namespaces do not meet; a collision would silently attach this revision's
    values to an unrelated accepted subject, so it refuses instead.
    """

    identity = item.record_subject_key or item.row.source_row_key
    if any(subject == identity for subject, _field in accepted):
        raise LaterRevisionRefused(
            f"{identity!r} names both a proposed subject in this revision and an "
            "accepted subject of the record; the two identities must not meet"
        )
    return identity


def _stands_accepted(
    stated: dict[str, Any],
    subjects: Sequence[str],
    accepted: dict[tuple[str, str], Any],
) -> bool:
    """Whether this row's complete set of captured values already stands accepted.

    Asked only of an ambiguous conflict number, and asked of the *whole* row:
    the accepted subjects carrying that number cannot be told apart, so the only
    safe reading is that a row reproducing one of them exactly says nothing new,
    and a row that does not is a difference somebody has to place.
    """

    for subject in subjects:
        standing = {
            field_name: value
            for (candidate, field_name), value in accepted.items()
            if candidate == subject
        }
        if standing == stated:
            return True
    return False


def _removal_candidates(
    accepted: dict[tuple[str, str], Any],
    accepted_by_identity: dict[str, tuple[str, ...]],
    revision_by_identity: dict[str, list[PreviewRow]],
    revision_rows: Sequence[PreviewRow],
    *,
    is_complete_enumerative_source: bool,
    row_accounting_sealed: bool,
) -> tuple[RemovalCandidate, ...]:
    """Accepted subjects this revision does not carry, and whether to propose one."""

    sealed = is_complete_enumerative_source and row_accounting_sealed
    not_adoptable = {
        item.row.business_identity
        for item in revision_rows
        if item.row.excluded and item.row.business_identity
    }
    identified = {
        subject
        for subjects in accepted_by_identity.values()
        for subject in subjects
    }

    candidates: list[RemovalCandidate] = []
    for identity, subjects in sorted(accepted_by_identity.items()):
        present = revision_by_identity.get(identity, ())
        if present:
            continue
        reason = (
            NOT_ADOPTABLE_IN_REVISION
            if identity in not_adoptable
            else ABSENT_FROM_REVISION
        )
        ambiguous = len(subjects) > 1
        for subject in subjects:
            withheld = (
                WITHHELD_AMBIGUOUS
                if ambiguous
                else (None if sealed else WITHHELD_UNSEALED)
            )
            candidates.append(
                RemovalCandidate(
                    subject_identity=subject,
                    business_identity=identity,
                    reason=reason,
                    proposed=withheld is None,
                    withheld_reason=withheld,
                )
            )
    for subject in sorted({subject for subject, _field in accepted} - identified):
        candidates.append(
            RemovalCandidate(
                subject_identity=subject,
                business_identity=None,
                reason=ABSENT_FROM_REVISION,
                proposed=False,
                withheld_reason=WITHHELD_NO_BUSINESS_IDENTITY,
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
    """Register the exact revision bytes as this project's own Document.

    The Document carries the delivery it came in on, in this same transaction,
    so a Proposed Delta appended from it can be compared by identity against
    the append-only boundary an issue's coverage was confirmed against (#675,
    #687). Nothing here reads a clock or an arrival order to establish that.
    """

    intake = preview_intake(session, project, staged, REVISION_DOC_TYPE)
    try:
        confirmation = confirm_intake(
            session,
            project=project,
            sha256=staged.sha256,
            filename=staged.filename,
            doc_type=REVISION_DOC_TYPE,
            binding_fingerprint=intake.binding_fingerprint,
            principal=actor,
            images_dir=images_dir,
            source_delivery_id=int(delivery.id),
        )
    except IntakeConflict as exc:
        raise LaterRevisionRefused(str(exc)) from exc
    return confirmation.document_id


def _capture_facts(
    session: Session,
    *,
    project: Project,
    document_id: int,
    reading: OperationsReading,
    plan: _Plan,
    actor: HumanPrincipal,
) -> tuple[int, tuple[int, ...], dict[str, dict[str, Fact]]]:
    """Capture what the revision says, under the resolved subject identity.

    Every comparable value of every adoptable row is captured, changed or not:
    a Source Fact is what the source says, and #519 refuses a decision whose
    Fact carries no effective value support, so the support assessment travels
    with it.  The subject key is the resolved identity rather than this
    revision's own row position, which is what makes a Fact and the accepted
    value it is compared with describe the same row.
    """

    segments = {
        (segment.sheet_name, segment.cell_range): segment
        for segment in session.scalars(
            select(SourceSegment).where(
                SourceSegment.document_id == document_id,
                SourceSegment.kind == "spreadsheet_cell",
            )
        ).all()
    }
    run = record_extraction_run(
        session,
        session.get_one(Document, document_id),
        prompt_version=IMPORTER_VERSION,
        schema_version=IMPORTER_VERSION,
        candidate_count=0,
        page_errors=0,
        outcome="completed",
        model=None,
        extractor_config=deployed_extractor_config("baseline", client=None),
        token_usage=zero_token_usage(document_id),
        row_accounting_json=None,
    )
    assessed_at = _capture_instant(session, document_id)
    fact_ids: list[int] = []
    captured: dict[str, dict[str, Fact]] = {}
    for item in plan.captured:
        for value in item.source.values:
            if value.field not in COMPARABLE_FACT_TYPES:
                continue
            segment = segments.get((value.sheet_name, value.cell_range))
            if segment is None:
                raise LaterRevisionRefused(
                    "the delivered revision has no Source Segment at "
                    f"{value.sheet_name}!{value.cell_range}"
                )
            fact = append_fact(
                session,
                project_id=project.id,
                document_id=document_id,
                extraction_run_id=run.id,
                subject_kind="source_row",
                subject_key=item.subject_identity,
                recorded_by=f"importer:{IMPORTER_VERSION}",
                content_sha256=_revision_fact_digest(
                    reading, document_id, item.subject_identity, value.field, segment
                ),
                value=materialize_segment_value(session, value.field, segment),
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
            captured.setdefault(item.subject_identity, {})[value.field] = fact
    session.flush()
    return run.id, tuple(fact_ids), captured


def _capture_instant(session: Session, document_id: int) -> datetime:
    """When this capture happened, taken from the Document the database timed.

    Not `datetime.now`: the assessment instant has to be the same on a replay
    that re-reads the same registered Document, and it must come from the same
    clock every other row of this transaction was stamped by.
    """

    created = session.scalar(
        select(Document.created_at).where(Document.id == document_id)
    )
    if created is None:
        raise LaterRevisionRefused("the registered revision has no creation time")
    return created if created.tzinfo else created.replace(tzinfo=timezone.utc)


def _revision_fact_digest(
    reading: OperationsReading,
    document_id: int,
    subject_identity: str,
    field: str,
    segment: SourceSegment,
) -> str:
    """One Fact identity per rendition, not one per value.

    The rendition is part of the identity because a Source Fact is *what one
    source says*, and two revisions restating the same value are two sources
    saying it.  Leaving the document out made the digest a function of the text
    alone — ``SourceSegment.content_sha256`` is ``sha256(exact_text)`` and
    carries no document — so the second revision to restate an unchanged value
    produced a digest the first revision already owned.  ``uq_facts_content_sha256``
    is global and ``append_fact`` returns the row that already carries the
    digest, so the capture got back the *earlier* revision's Fact and then tried
    to support it with this revision's segment, which
    ``ck_support_assessment_rendition`` refuses outright.

    That made a third delivery fail on the ordinary case — any workbook whose
    second later revision leaves one value alone, which is nearly all of them.
    """

    return sha256(
        json.dumps(
            {
                "importer_version": reading.importer_version,
                "document_id": int(document_id),
                "subject_identity": subject_identity,
                "field": field,
                "segment": segment.content_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


# --- the proposed differences ----------------------------------------------


def _proposals(
    plan: _Plan,
    captured: dict[str, dict[str, Fact]],
    accepted: dict[tuple[str, str], Any],
    baseline_revision: int | None,
) -> tuple[tuple[ProposedDeltaValues, ...], int, frozenset[str]]:
    """The typed differences, the values the record already held, and the no-ops.

    A stated value equal to the accepted one produces nothing, which is the
    whole point of comparing at all: a revision of five hundred rows that
    changed three of them proposes three changes.
    """

    label = revision_label(baseline_revision)
    by_identity = _accepted_by_business_identity(accepted)
    proposals: list[ProposedDeltaValues] = []
    agreed = 0
    unchanged: set[str] = set()
    for item in plan.rows:
        stated = {
            field_name: fact_value(fact)
            for field_name, fact in captured.get(item.subject_identity, {}).items()
        }
        if item.disposition == COMPARED:
            for field_name, value in sorted(stated.items()):
                key = (item.subject_identity, field_name)
                if key in accepted:
                    if accepted[key] == value:
                        agreed += 1
                        continue
                    change_type = "modify"
                else:
                    change_type = "add"
                proposals.append(
                    ProposedDeltaValues(
                        change_type=change_type,
                        target=ExistingSubjectTarget(
                            subject_identity=item.subject_identity, field=field_name
                        ),
                        accepted_value=accepted.get(key),
                        proposed_value=value,
                        comparison_rule_version=REVISION_COMPARISON_RULE_VERSION,
                        accepted_baseline_revision=label,
                    )
                )
            continue
        source_row = item.source
        if item.disposition == AMBIGUOUS_IDENTITY and _stands_accepted(
            stated, by_identity.get(source_row.business_identity or "", ()), accepted
        ):
            agreed += len(stated)
            unchanged.add(source_row.source_row_key)
            continue
        proposals.append(
            ProposedDeltaValues(
                change_type="add",
                target=ProposedSubjectTarget(
                    subject_identity=item.subject_identity,
                    proposed_fields=tuple(sorted(stated)),
                ),
                proposed_value=dict(sorted(stated.items())),
                comparison_rule_version=REVISION_COMPARISON_RULE_VERSION,
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
                comparison_rule_version=REVISION_COMPARISON_RULE_VERSION,
                accepted_baseline_revision=label,
            )
        )
    return tuple(proposals), agreed, frozenset(unchanged)


def _settled(
    accounting: RevisionAccounting, unchanged: frozenset[str]
) -> RevisionAccounting:
    """Record which ambiguous rows the revision turned out to say nothing new about."""

    if not unchanged:
        return accounting
    return replace(
        accounting,
        rows=tuple(
            replace(row, disposition=UNCHANGED_UNDER_AMBIGUOUS_IDENTITY)
            if row.source_row_key in unchanged
            else row
            for row in accounting.rows
        ),
    )


def _source_family(manifest: FieldMappingManifest) -> str:
    """The lineage this revision and its successors share.

    The workbook family is named by the mapping revision's *identity*, not its
    version: a successor mapping revision of the same family still describes the
    same customer form, and a delta from this revision must be able to supersede
    one from the last (#518).
    """

    return f"ucm_workbook:{manifest.identity}"[:64]
