"""Replacing the output template or the field mapping, in force, on one act (#829).

ADR-0076 records the accepted data-baseline identity, the output-template
identity and the field-mapping identity **separately**, precisely so a customer
who changes their workbook layout does not re-adopt their record to keep using
it. ``baseline_adoption.register_baseline_format`` is the act that separation
exists for, and the customer-journey audit found it with a domain
implementation, a migration granting it to the web capability, and **no
production caller at all** (``docs/research/customer-journey-audit-2026-09-10.md``,
"Template and mapping replacement has the same gap"). A customer whose form
changed was therefore stranded behind an incompatible template with no visible
repair. This module is the reading and the act that page performs.

**Operations validation reports; it does not raise.** The same composition
renders the page a person reads *before* approving, so a template that no
longer means what the registered mapping says has to arrive as a finding on a
screen rather than as an exception nobody can see. The findings are not
composed here either: ``baseline_workbook`` owns the importer mechanics and
``field_mapping_manifest.conformance_refusals`` owns what makes a file a
conforming reading of the registered revision. Restating either would put a
second opinion about a customer's form on the screen that replaces it.

**The split-or-combine case is the reason the validation exists** (#597). A
successor form that stops carrying *Start Station* and *End Station* as two
values and starts carrying one combined range in the same two columns prints
identical headings and identical drop-downs, so no digest over its bytes can
tell. ``conformance_refusals`` reads the offered file's own populated rows
through the composition the registered revision declares, and a row that cannot
be what the manifest says is reported by worksheet and row number. The converse
is proved the same way: a declared mapping revision is refused by
``prove_manifest`` unless the customer's own populated example survives a full
round trip through the composition the declaration claims.

**Nothing here concludes that a form changed meaning.** That conclusion is a
person's, recorded as a registration — the rule ``field_mapping_manifest``'s
docstring states and the reason a mapping revision exists at all. So a
replacement mapping is submitted *with* its declaration: which released
composition rule each range pair is carried under. Corridor proves the
declaration against the customer's populated example and refuses one that
cannot reproduce it; it never reads a separator and decides.

**Approval binds the version it was shown.** The predecessor registration the
page read and the digest of the exact bytes an approval would register both
travel back, and either having moved refuses the approval whole. The order of
those checks is the contract and is #828's: **unchanged is decided before
stale**, because a resubmitted approval is the case where the predecessor has
legitimately moved — the registration the first submission made is now the one
in force, and recomposing this submission against it digests to exactly what it
registered. Checking staleness first would answer a replayed browser Post with
"somebody else changed this", which is untrue and unrecoverable from the page.

**A format registration changes no accepted value, and cannot.** It writes one
``project_baseline_formats`` row, its stored declaration and, for a template,
its retained bytes. It writes no ``fact_decisions`` row and opens no Project
Record revision, and the record-decision role's commands are the only way
either is written (#492). ``tests/test_format_replacement.py`` proves it by
counting both across the act rather than by asserting one value.

**Staleness stays derived.** Nothing here reaches into a prepared release
candidate. ``release_candidate.candidate_staleness_reasons`` compares what a
candidate was rendered through with what is registered now, and a replacement
makes that comparison differ.

**No clock.** A registration's instant is PostgreSQL's own ``registered_at``;
nothing here reads the day.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tempfile
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.baseline_adoption import (
    FormatIdentity,
    effective_baseline_formats,
    register_baseline_format,
    stored_mapping_revision,
)
from corridor.baseline_workbook import (
    BaselineWorkbookUnsupported,
    OperationsReading,
    read_baseline_workbook,
)
from corridor.field_mapping_manifest import (
    COMBINED_RANGE,
    ONE_VALUE_PER_COLUMN,
    RANGE_ENDPOINTS,
    FieldMappingManifest,
    MappingDeclaration,
    MappingManifestRefused,
    MaterialMapping,
    combined_range_mapping,
    composition_rule,
    conformance_refusals,
    declared_field_mapping,
)
from corridor.models import BaselineFormat
from corridor.object_storage import (
    StorageError,
    content_key,
    content_store,
    digest_bytes,
)
from corridor.presentation import field_label
from corridor.principals import HumanPrincipal


#: The two kinds a project registers, in ADR-0076's own spelling. Anything else
#: is a kind ``register_baseline_format`` refuses, so it is refused here first
#: rather than composed into a proposal nobody could approve.
OUTPUT_TEMPLATE = "output_template"
FIELD_MAPPING = "field_mapping"
REPLACEMENT_KINDS = (OUTPUT_TEMPLATE, FIELD_MAPPING)

#: What a replacement file is offered as. The retention
#: ``register_baseline_format`` performs keys an output template's bytes by
#: this suffix, so a proposal staged under another one would register a
#: template whose retained object the preparation cannot find.
REPLACEMENT_SUFFIX = ".xlsx"

# --- Why a replacement is not registrable -----------------------------------
#
# Codes rather than sentences at the catch site: the sentence a person reads is
# the owning module's, and the code says which reading produced it.

UNREADABLE_WORKBOOK = "unreadable_workbook"
OPERATIONS_UNRESOLVED = "operations_unresolved"
NOT_THE_REGISTERED_MAPPING = "not_the_registered_mapping"
MAPPING_NOT_PROVED = "mapping_not_proved"

# --- What differs between two mappings, field by field ----------------------

ADDED = "added"
REMOVED = "removed"
CHANGED = "changed"
UNCHANGED = "unchanged"


class FormatReplacementRefused(ValueError):
    """This replacement is not the one that was shown, or cannot be registered."""


@dataclass(frozen=True, slots=True)
class ValidationFinding:
    """One reason this file is not registrable, in the words its reader used."""

    code: str
    sentence: str


@dataclass(frozen=True, slots=True)
class MappingLine:
    """How one canonical field is carried, as one mapping revision declares it."""

    field: str
    headings: tuple[str, ...]
    composition: str
    cardinality: str
    delimiter: str | None
    controlled_vocabulary: tuple[str, ...]
    material: bool


@dataclass(frozen=True, slots=True)
class FieldComparison:
    """One canonical field, as the registered and the proposed revision carry it.

    ``differences`` is empty exactly when the two declare the same thing about
    this field, so a screen never has to decide what "the same" means.
    """

    field: str
    label: str
    before: MappingLine | None
    after: MappingLine | None
    differences: tuple[str, ...]

    @property
    def state(self) -> str:
        if self.before is None:
            return ADDED
        if self.after is None:
            return REMOVED
        return CHANGED if self.differences else UNCHANGED


@dataclass(frozen=True, slots=True)
class ProposedFormat:
    """One replacement prepared for approval, and everything that binds it.

    It is a value and not a row: no schema holds a proposal, and #825 holds the
    schema slot. What makes an approval bind *this* one is that the predecessor
    registration and the digest of the exact bytes it would register travel
    back with it, and are compared against a proposal recomposed from the
    staged file.
    """

    project_id: int
    kind: str
    identity: str
    version: str
    content_sha256: str
    staged_sha256: str
    supersedes_format_id: int | None
    supersedes_identity: str | None
    supersedes_version: str | None
    findings: tuple[ValidationFinding, ...]
    comparison: tuple[FieldComparison, ...]
    reading: OperationsReading | None
    unchanged: bool

    @property
    def valid(self) -> bool:
        """Whether the validation found nothing standing in the way."""

        return not self.findings

    @property
    def material_changes(self) -> tuple[FieldComparison, ...]:
        """The fields whose declared meaning is not what is registered now."""

        return tuple(item for item in self.comparison if item.state != UNCHANGED)


@dataclass(frozen=True, slots=True)
class ReplacementOutcome:
    """What one approval did: a registration, or nothing because nothing moved."""

    project_id: int
    kind: str
    format_id: int | None
    identity: str
    version: str
    content_sha256: str
    registered: bool


@dataclass(frozen=True, slots=True)
class RangeChoice:
    """One released range pair, and how the customer's form carries it.

    ``RANGE_ENDPOINTS`` is declared vocabulary knowledge and not a reading of
    any file, so these are the pairs a form may be declared to combine and
    there are no others. The two compositions are the two rules the renderer
    implements; a person chooses between them, and nothing infers one.
    """

    fields: tuple[str, str]
    labels: tuple[str, str]
    composition: str

    @property
    def name(self) -> str:
        """How one choice is named on the form and read back off it."""

        return "-".join(self.fields)

    @property
    def combined(self) -> bool:
        return self.composition == COMBINED_RANGE


def stage_replacement(body: bytes, *, store=None) -> str:
    """Retain the exact offered bytes and answer with their digest.

    A prepared proposal has no row to live in, so the file a person uploaded to
    the validation step has to be reachable again from the approval step by
    something the form can carry. The digest is that something, and the bytes
    go to the content-addressed store under exactly the key
    ``register_baseline_format`` will retain an output template under, so a
    later registration re-writes identical bytes rather than storing a second
    copy under a second key.
    """

    digest = digest_bytes(body)
    key = content_key(digest, REPLACEMENT_SUFFIX)
    (store or content_store()).put(key, body, sha256=digest)
    return digest


def staged_replacement(staged_sha256: str, *, store=None) -> bytes:
    """The exact bytes one staged digest names, verified on the way back out."""

    try:
        return (store or content_store()).get(
            content_key(staged_sha256, REPLACEMENT_SUFFIX), sha256=staged_sha256
        )
    except (StorageError, KeyError, FileNotFoundError) as exc:
        raise FormatReplacementRefused(
            "the file this approval names is no longer staged, so there is "
            "nothing to register. Offer it again."
        ) from exc


def registered_formats(
    session: Session, project_id: int
) -> tuple[BaselineFormat, ...]:
    """Every registration this project has made, newest first.

    The superseded ones as well as the two in force: a replacement's whole
    point is that the previous registration stays readable, because a package
    already issued was rendered through it.
    """

    return tuple(
        session.scalars(
            select(BaselineFormat)
            .where(BaselineFormat.project_id == project_id)
            .order_by(BaselineFormat.id.desc())
        ).all()
    )


def range_choices(
    session: Session,
    project_id: int,
    *,
    combined: Sequence[str] = (),
    declared: bool = False,
) -> tuple[RangeChoice, ...]:
    """How each released range pair is carried, to open the form on.

    ``declared`` says whether ``combined`` is a person's submitted choice. With
    no submission the form opens on what the registered revision declares, so
    preparing a replacement without touching a control proposes the composition
    already in force rather than silently proposing to split every range.
    """

    manifest = _registered_manifest(session, project_id)
    chosen = set(combined)
    return tuple(
        RangeChoice(
            fields=pair,
            labels=(field_label(pair[0]), field_label(pair[1])),
            composition=(
                COMBINED_RANGE
                if (
                    "-".join(pair) in chosen
                    if declared
                    else _is_combined(manifest, pair)
                )
                else ONE_VALUE_PER_COLUMN
            ),
        )
        for pair in RANGE_ENDPOINTS
    )


def propose_format_replacement(
    session: Session,
    *,
    project_id: int,
    kind: str,
    identity: str,
    version: str,
    staged_sha256: str,
    ranges: Sequence[RangeChoice] = (),
    store=None,
    scratch: Path | None = None,
) -> ProposedFormat:
    """Validate one staged replacement and compose what registering it would do.

    Every problem is reported rather than raised, for the reason #828 gives:
    the same composition renders the page a person reads before approving, and
    raising would be a validation screen that cannot show why a file is
    refused.

    ``scratch`` is where the staged bytes are written for the importer, which
    reads a path rather than a buffer. It is a temporary directory of the
    caller's, never a retained location.
    """

    if kind not in REPLACEMENT_KINDS:
        raise FormatReplacementRefused(
            f"{kind!r} is not a registration a project holds; ADR-0076 records "
            "an output template and a field mapping, each on its own act."
        )
    if not identity.strip() or not version.strip():
        raise FormatReplacementRefused(
            "a replacement is named by an identity and a version, so a later "
            "receipt can say which one a render was performed under."
        )
    effective = effective_baseline_formats(session, project_id)
    current = effective.get(kind)
    registered = _registered_manifest(session, project_id)
    if current is None or registered is None:
        raise FormatReplacementRefused(
            "this project has registered no output template and field mapping, "
            "so there is nothing a replacement would supersede. The customer's "
            "baseline is adopted first."
        )

    body = staged_replacement(staged_sha256, store=store)
    findings: list[ValidationFinding] = []
    reading = _reading(body, registered, scratch=scratch, findings=findings)
    proposed: FieldMappingManifest | None = None
    if reading is not None:
        if kind == OUTPUT_TEMPLATE:
            findings.extend(
                ValidationFinding(NOT_THE_REGISTERED_MAPPING, sentence)
                for sentence in conformance_refusals(registered, reading)
            )
        else:
            proposed = _declared(
                reading,
                identity=identity,
                version=version,
                ranges=ranges,
                findings=findings,
            )

    content_sha256 = (
        staged_sha256 if kind == OUTPUT_TEMPLATE else _digest(proposed)
    )
    return ProposedFormat(
        project_id=int(project_id),
        kind=kind,
        identity=identity.strip(),
        version=version.strip(),
        content_sha256=content_sha256,
        staged_sha256=staged_sha256,
        supersedes_format_id=int(current.id),
        supersedes_identity=current.format_identity,
        supersedes_version=current.format_version,
        findings=tuple(findings),
        comparison=(
            compare_mappings(registered, proposed) if kind == FIELD_MAPPING else ()
        ),
        reading=reading,
        unchanged=(
            content_sha256 == current.content_sha256
            and identity.strip() == current.format_identity
            and version.strip() == current.format_version
        ),
    )


def approve_format_replacement(
    session: Session,
    *,
    project_id: int,
    kind: str,
    identity: str,
    version: str,
    staged_sha256: str,
    ranges: Sequence[RangeChoice] = (),
    supersedes_format_id: int | None,
    content_sha256: str,
    principal: HumanPrincipal,
    store=None,
    scratch: Path | None = None,
) -> ReplacementOutcome:
    """Register one validated replacement, as the person performing the act.

    The checks below refuse whole and write nothing. Their order is the
    contract: unchanged first, so a resubmitted approval converges on the
    registration the first one made instead of being told somebody else changed
    it; then the predecessor, then the digest, then the validation.

    The designation this act requires is proved where it is enforced.
    ``register_baseline_format`` refuses a mapping revision that changes what a
    project's mapped columns mean unless the actor holds the project-
    coordination designation on the project, and nothing here restates that
    rule: a second Python gate could drift from the roster the command reads.
    """

    proposal = propose_format_replacement(
        session,
        project_id=project_id,
        kind=kind,
        identity=identity,
        version=version,
        staged_sha256=staged_sha256,
        ranges=ranges,
        store=store,
        scratch=scratch,
    )
    submitted = (content_sha256 or "").strip()
    if proposal.unchanged and submitted == proposal.content_sha256:
        # Already in force, whether because this approval was submitted twice
        # or because it proposed no change. Either way there is no next
        # registration to attribute to anybody.
        return ReplacementOutcome(
            project_id=int(project_id),
            kind=kind,
            format_id=proposal.supersedes_format_id,
            identity=proposal.identity,
            version=proposal.version,
            content_sha256=proposal.content_sha256,
            registered=False,
        )
    if proposal.supersedes_format_id != supersedes_format_id:
        raise FormatReplacementRefused(
            "the registration this project renders through was replaced by "
            "someone else after this page was read, so the version you "
            "approved is not the one it would supersede. Read the "
            "registrations again."
        )
    if submitted != proposal.content_sha256:
        raise FormatReplacementRefused(
            "this is not the replacement the page showed you: what was "
            "validated now digests to something else. Read the validation "
            "again and approve what it prints."
        )
    if proposal.findings:
        raise FormatReplacementRefused(
            " ".join(finding.sentence for finding in proposal.findings)
        )

    manifest = None
    template_bytes = None
    if kind == OUTPUT_TEMPLATE:
        template_bytes = staged_replacement(staged_sha256, store=store)
    else:
        manifest = _declared(
            _require_reading(
                staged_replacement(staged_sha256, store=store),
                _registered_manifest(session, project_id),
                scratch=scratch,
            ),
            identity=proposal.identity,
            version=proposal.version,
            ranges=ranges,
            findings=[],
        )
    registered = register_baseline_format(
        session,
        project_id=project_id,
        identity=FormatIdentity(
            kind=kind,
            identity=proposal.identity,
            version=proposal.version,
            content_sha256=proposal.content_sha256,
        ),
        principal=principal,
        # Derived from the approved bytes rather than generated, so the command
        # converges on the registration already made if this ever arrives
        # twice, instead of opening a second one.
        idempotency_key=f"format-replacement:{kind}:{proposal.content_sha256}",
        manifest=manifest,
        template_bytes=template_bytes,
        template_suffix=REPLACEMENT_SUFFIX,
        store=store,
    )
    return ReplacementOutcome(
        project_id=int(project_id),
        kind=kind,
        format_id=int(registered.id),
        identity=registered.format_identity,
        version=registered.format_version,
        content_sha256=registered.content_sha256,
        registered=True,
    )


def compare_mappings(
    before: FieldMappingManifest, after: FieldMappingManifest | None
) -> tuple[FieldComparison, ...]:
    """What the proposed revision declares about each field, against the one in force.

    Truthful means it answers for every field either revision carries, and
    names what differs rather than scoring how much. A field the proposed
    revision does not carry is reported as carried by neither a heading nor a
    rule, which is what "removed" means for a mapping: nothing about the
    accepted value it named changes, and no column carries it any more.
    """

    if after is None:
        return ()
    registered = {
        field: _line(field, mapping)
        for mapping in before.mappings
        for field in mapping.target_fields
    }
    proposed = {
        field: _line(field, mapping)
        for mapping in after.mappings
        for field in mapping.target_fields
    }
    return tuple(
        FieldComparison(
            field=field,
            label=field_label(field),
            before=registered.get(field),
            after=proposed.get(field),
            differences=_differences(registered.get(field), proposed.get(field)),
        )
        for field in sorted(set(registered) | set(proposed))
    )


# --- Reading the offered file ----------------------------------------------


def _reading(
    body: bytes,
    registered: FieldMappingManifest,
    *,
    scratch: Path | None,
    findings: list[ValidationFinding],
) -> OperationsReading | None:
    """The importer's own reading of the offered file, or why there is none."""

    try:
        reading = _require_reading(body, registered, scratch=scratch)
    except BaselineWorkbookUnsupported as exc:
        findings.append(ValidationFinding(UNREADABLE_WORKBOOK, str(exc)))
        return None
    findings.extend(
        ValidationFinding(OPERATIONS_UNRESOLVED, item.detail)
        for item in reading.blocking_diagnostics
    )
    findings.extend(
        ValidationFinding(OPERATIONS_UNRESOLVED, mismatch)
        for mismatch in reading.round_trip.mismatches
    )
    return reading


def _require_reading(
    body: bytes, registered: FieldMappingManifest, *, scratch: Path | None
) -> OperationsReading:
    """Read the offered file through the declared headings already in force.

    The external-reference headings are the registered revision's, because an
    undeclared heading carries no meaning whatever it is spelled (#597): a
    replacement that moves a reference column declares that too.
    """

    directory = Path(
        scratch if scratch is not None else tempfile.mkdtemp(prefix="corridor-fmt-")
    )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"replacement{REPLACEMENT_SUFFIX}"
    path.write_bytes(body)
    return read_baseline_workbook(
        path, external_references=registered.external_reference_headings
    )


def _declared(
    reading: OperationsReading,
    *,
    identity: str,
    version: str,
    ranges: Sequence[RangeChoice],
    findings: list[ValidationFinding],
) -> FieldMappingManifest | None:
    """The mapping revision this declaration amounts to, proved or reported.

    The declaration is only what a person decided: for each released range
    pair, which composition rule the customer's form carries it under.
    Everything else is what the file itself heads, which
    ``declared_field_mapping`` derives and proves against the file's own
    populated example.
    """

    combined: list[MaterialMapping] = []
    for choice in ranges:
        if not choice.combined:
            continue
        try:
            combined.append(combined_range_mapping(reading, choice.fields))
        except MappingManifestRefused as exc:
            findings.append(ValidationFinding(MAPPING_NOT_PROVED, str(exc)))
            return None
    try:
        return declared_field_mapping(
            reading,
            MappingDeclaration(
                identity=identity.strip(),
                version=version.strip(),
                mappings=tuple(combined),
            ),
        )
    except MappingManifestRefused as exc:
        findings.append(ValidationFinding(MAPPING_NOT_PROVED, str(exc)))
        return None


def _registered_manifest(
    session: Session, project_id: int
) -> FieldMappingManifest | None:
    """The mapping revision in force, read back from its stored declaration."""

    current = effective_baseline_formats(session, project_id).get(FIELD_MAPPING)
    if current is None:
        return None
    return stored_mapping_revision(
        session,
        identity=current.format_identity,
        version=current.format_version,
        content_sha256=current.content_sha256,
    )


def _is_combined(manifest: FieldMappingManifest | None, pair: tuple[str, str]) -> bool:
    if manifest is None:
        return False
    mapping = manifest.mapping_for(pair[0])
    return mapping is not None and mapping.composition == COMBINED_RANGE


def _digest(manifest: FieldMappingManifest | None) -> str:
    return "" if manifest is None else manifest.content_sha256


def _line(field: str, mapping: MaterialMapping) -> MappingLine:
    return MappingLine(
        field=field,
        headings=mapping.source_columns,
        composition=mapping.composition,
        cardinality=composition_rule(mapping.composition).cardinality(mapping),
        delimiter=mapping.delimiter,
        controlled_vocabulary=mapping.controlled_vocabulary,
        material=mapping.material,
    )


def _differences(
    before: MappingLine | None, after: MappingLine | None
) -> tuple[str, ...]:
    """Exactly what the two revisions say differently about one field."""

    if before is None or after is None:
        return ()
    differences: list[str] = []
    if before.headings != after.headings:
        differences.append(
            f"read from {_headings(before)} now and {_headings(after)} under "
            "the proposed revision"
        )
    if (before.composition, before.delimiter) != (
        after.composition,
        after.delimiter,
    ):
        differences.append(
            f"carried as {before.cardinality} under {before.composition} now "
            f"and as {after.cardinality} under {after.composition} under the "
            "proposed revision"
        )
    if before.controlled_vocabulary != after.controlled_vocabulary:
        differences.append(
            f"offers {list(before.controlled_vocabulary)} now and "
            f"{list(after.controlled_vocabulary)} under the proposed revision"
        )
    if before.material != after.material:
        differences.append(
            f"{'material' if before.material else 'not material'} now and "
            f"{'material' if after.material else 'not material'} under the "
            "proposed revision"
        )
    return tuple(differences)


def _headings(line: MappingLine) -> str:
    return ", ".join(repr(heading) for heading in line.headings)


__all__ = [
    "ADDED",
    "CHANGED",
    "FIELD_MAPPING",
    "MAPPING_NOT_PROVED",
    "NOT_THE_REGISTERED_MAPPING",
    "OPERATIONS_UNRESOLVED",
    "OUTPUT_TEMPLATE",
    "REMOVED",
    "REPLACEMENT_KINDS",
    "REPLACEMENT_SUFFIX",
    "UNCHANGED",
    "UNREADABLE_WORKBOOK",
    "FieldComparison",
    "FormatReplacementRefused",
    "MappingLine",
    "ProposedFormat",
    "RangeChoice",
    "ReplacementOutcome",
    "ValidationFinding",
    "approve_format_replacement",
    "compare_mappings",
    "propose_format_replacement",
    "range_choices",
    "registered_formats",
    "stage_replacement",
    "staged_replacement",
]
