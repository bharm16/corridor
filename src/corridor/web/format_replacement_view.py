"""The template-and-mapping page's reading, and the act it carries (#829).

The customer-journey audit found ``register_baseline_format`` with a domain
implementation, a migration granting it to the web capability, and no
production caller: a customer who changed their workbook layout was stranded
behind an incompatible template with no visible repair
(``docs/research/customer-journey-audit-2026-09-10.md``, "Template and mapping
replacement has the same gap"). This module is the reading that page renders,
the counterpart of ``corridor.web.issue_profile_view`` for the two
registrations a project renders through rather than for what it issues.

**It reads; it decides nothing.** ``baseline_adoption.effective_baseline_formats``
says what is registered, ``format_replacement.propose_format_replacement``
validates an offered replacement and composes what registering it would do, and
``release_candidate.candidate_is_stale`` says what a prepared candidate no
longer matches. Every sentence about state on this page comes from one of
those. Re-deriving any of them here would put a second opinion about a
customer's form on the screen that replaces it.

**Operations and the coordinator read the same page, in two panels.** #509
draws that line: the importer mechanics — worksheets, formulas, hidden content,
unknown columns, diagnostics, the round trip — are operations' and never a
project decision, and the field-by-field comparison of what the mapped columns
mean is the coordinator's. They are separate panels for that reason, not for
layout.

**It shows who may approve, and hides nothing on the strength of it.** The
audit's own finding is that a derived capability display is not a competing
security authority: reading the same roster designation the registration proves
against, and saying so beside the control, prevents a predictable failed click.
The control is still rendered and the refusal still comes from the command.

Terminology: *output template* and *field mapping* are the glossary's own names
for the two identities ADR-0076 records separately from the adopted data
baseline, and ``presentation.field_label`` names every canonical field. No
customer-facing label is coined here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from corridor.access import COORDINATION, MembershipAccess
from corridor.baseline_adoption import effective_baseline_formats
from corridor.format_replacement import (
    FIELD_MAPPING,
    OUTPUT_TEMPLATE,
    FieldComparison,
    ProposedFormat,
    RangeChoice,
    ValidationFinding,
    propose_format_replacement,
    range_choices,
    registered_formats,
)
from corridor.release_candidate import candidate_is_stale, current_release_candidate


#: What each registration is called on the screen. The glossary's own words for
#: the two identities ADR-0076 records apart from the adopted data baseline.
KIND_WORDS = {
    OUTPUT_TEMPLATE: "output template",
    FIELD_MAPPING: "field mapping",
}


@dataclass(frozen=True, slots=True)
class RegistrationLine:
    """One registration this project made, and whether it is the one in force."""

    format_id: int
    kind: str
    words: str
    identity: str
    version: str
    content_sha256: str
    registered_by_principal: str
    registered_at: datetime
    in_force: bool


@dataclass(frozen=True, slots=True)
class OperationsPanel:
    """The importer mechanics of the offered file, as operations owns them.

    Every number is ``baseline_workbook``'s own reading. Nothing is re-counted
    here, and nothing on this panel is a project question: #509 keeps importer
    diagnostics away from the person deciding what the customer's columns mean.
    """

    worksheet: str
    header_row_number: int
    importer: str
    mapped_columns: tuple[tuple[str, str], ...]
    unknown_columns: tuple[str, ...]
    controlled_vocabularies: tuple[str, ...]
    formula_cells: tuple[str, ...]
    hidden_content: tuple[str, ...]
    unsupported_values: tuple[str, ...]
    diagnostics: tuple[str, ...]
    rows_checked: int
    values_checked: int
    round_trip_clean: bool


@dataclass(frozen=True, slots=True)
class ProposalPanel:
    """The registration an approval would make, bound to what it would supersede."""

    kind: str
    words: str
    identity: str
    version: str
    content_sha256: str
    staged_sha256: str
    supersedes_format_id: int | None
    supersedes_identity: str | None
    supersedes_version: str | None
    findings: tuple[ValidationFinding, ...]
    comparison: tuple[FieldComparison, ...]
    material_changes: tuple[FieldComparison, ...]
    operations: OperationsPanel | None
    unchanged: bool

    @property
    def valid(self) -> bool:
        return not self.findings


@dataclass(frozen=True, slots=True)
class PreparedCandidateLine:
    """The prepared issue a replacement would leave behind, if any."""

    candidate_identity: str
    stale_reasons: tuple[str, ...]

    @property
    def stale(self) -> bool:
        return bool(self.stale_reasons)


@dataclass(frozen=True, slots=True)
class FormatReplacementView:
    """Everything the template-and-mapping page prints, read once."""

    registrations: tuple[RegistrationLine, ...]
    effective: tuple[RegistrationLine, ...]
    ranges: tuple[RangeChoice, ...]
    proposal: ProposalPanel | None
    candidate: PreparedCandidateLine | None
    may_approve: bool


def format_replacement_view(
    session: Session,
    *,
    project_id: int,
    membership: MembershipAccess,
    as_of: datetime,
    proposal: ProposedFormat | None = None,
    ranges: tuple[RangeChoice, ...] | None = None,
) -> FormatReplacementView:
    """What is registered, what a replacement would register, and what it strands.

    ``proposal`` is ``None`` where nobody has offered a file — the page is then
    a reading with an upload form — and a validated proposal where one arrived.
    """

    formats = effective_baseline_formats(session, project_id)
    in_force = {int(row.id) for row in formats.values()}
    lines = tuple(
        _line(row, in_force) for row in registered_formats(session, project_id)
    )
    return FormatReplacementView(
        registrations=lines,
        effective=tuple(line for line in lines if line.in_force),
        ranges=(
            ranges if ranges is not None else range_choices(session, project_id)
        ),
        proposal=None if proposal is None else _panel(proposal),
        candidate=_candidate(session, project_id, as_of),
        may_approve=membership.has(COORDINATION),
    )


def validated_replacement(
    session: Session,
    *,
    project_id: int,
    kind: str,
    identity: str,
    version: str,
    staged_sha256: str,
    ranges: tuple[RangeChoice, ...],
    scratch: Path | None = None,
) -> ProposedFormat:
    """Validate one offered replacement. The rule is the domain module's alone."""

    return propose_format_replacement(
        session,
        project_id=project_id,
        kind=kind,
        identity=identity,
        version=version,
        staged_sha256=staged_sha256,
        ranges=ranges,
        scratch=scratch,
    )


def _line(row, in_force: set[int]) -> RegistrationLine:
    return RegistrationLine(
        format_id=int(row.id),
        kind=row.format_kind,
        words=KIND_WORDS.get(row.format_kind, row.format_kind),
        identity=row.format_identity,
        version=row.format_version,
        content_sha256=row.content_sha256,
        registered_by_principal=row.registered_by_principal,
        registered_at=row.registered_at,
        in_force=int(row.id) in in_force,
    )


def _panel(proposal: ProposedFormat) -> ProposalPanel:
    return ProposalPanel(
        kind=proposal.kind,
        words=KIND_WORDS.get(proposal.kind, proposal.kind),
        identity=proposal.identity,
        version=proposal.version,
        content_sha256=proposal.content_sha256,
        staged_sha256=proposal.staged_sha256,
        supersedes_format_id=proposal.supersedes_format_id,
        supersedes_identity=proposal.supersedes_identity,
        supersedes_version=proposal.supersedes_version,
        findings=proposal.findings,
        comparison=proposal.comparison,
        material_changes=proposal.material_changes,
        operations=_operations(proposal.reading),
        unchanged=proposal.unchanged,
    )


def _operations(reading) -> OperationsPanel | None:
    """The importer's reading, flattened into the strings the panel prints."""

    if reading is None:
        return None
    return OperationsPanel(
        worksheet=reading.adopted_sheet,
        header_row_number=reading.header_row_number,
        importer=f"{reading.importer_identity} {reading.importer_version}",
        mapped_columns=tuple(
            (column.heading, column.field) for column in reading.column_mapping
        ),
        unknown_columns=tuple(item.heading for item in reading.unknown_columns),
        controlled_vocabularies=tuple(
            item.heading for item in reading.controlled_vocabularies
        ),
        formula_cells=reading.formula_cells,
        hidden_content=reading.hidden_content,
        unsupported_values=tuple(
            f"{item.sheet_name}!{item.cell_range} {item.field}: {item.reason}"
            for item in reading.unsupported_values
        ),
        diagnostics=tuple(item.detail for item in reading.diagnostics),
        rows_checked=reading.round_trip.rows_checked,
        values_checked=reading.round_trip.values_checked,
        round_trip_clean=reading.round_trip.clean,
    )


def _candidate(
    session: Session, project_id: int, as_of: datetime
) -> PreparedCandidateLine | None:
    """The prepared issue a replacement would leave behind.

    ``candidate_is_stale`` is #529's own rule and the only one consulted: a
    replaced registration makes a candidate stale by that comparison and by
    nothing this module adds.
    """

    candidate = current_release_candidate(session, project_id)
    if candidate is None:
        return None
    return PreparedCandidateLine(
        candidate_identity=candidate.candidate_identity,
        stale_reasons=candidate_is_stale(session, candidate, as_of=as_of),
    )
