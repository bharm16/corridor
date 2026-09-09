"""Render the accepted Project Record into the approved native UCM workbook (#495).

The customer's Utility Conflict Matrix is a workbook they already use, and the
deliverable is *their* file with the accepted values in it — not a Corridor
layout carrying the same numbers. `export.to_xlsx` builds a Corridor workbook
from scratch and keeps its place; this module never builds a workbook at all.
It takes the approved output template's exact bytes, writes only the mapped
cells the accepted record changes, and copies every other package part through
byte for byte.

**Three identities, held apart.** Adopt Baseline (#509) records the accepted
data-baseline identity, the approved output-template identity, and the approved
field-mapping identity separately, because a later template or mapping is
registered on its own act and changes no accepted value. So the renderer is
handed template bytes *and* the mapping revision they are to be read through,
and reads the registrations; it refuses bytes that are not the approved
template, and a manifest that is not the approved mapping revision, on the same
terms — the caller supplies the artifact and the registration supplies the
digest a person approved.

**The template does not get to say what it means (#597).** A digest over the
template's own columns cannot see a form that stopped carrying `Start Station`
and `End Station` as two values and started carrying one combined range in the
same two columns: every heading and every drop-down is unchanged. So the
registered `field_mapping_manifest` is the authority, and the template is
checked *against* it — its printed columns, its declared vocabularies, and its
own populated values, which must split and combine the way the mapping revision
declares. A form that changed carries a different mapping revision, registered
by a person holding the project-coordination designation. Rendering is never a
second Adopt Baseline and never writes anything.

**Source-row identity is not record-subject identity.** A cell is addressed by
the source row's own coordinate in the customer's file; the value in it belongs
to a Project Record subject. Both travel on every reported cell, and the renderer
refuses a template row whose printed conflict id no longer matches the source row
it is supposed to be, rather than writing an accepted value into somebody else's
line.

**Why byte surgery rather than openpyxl.** Loading a workbook with openpyxl and
saving it rewrites the whole package: unknown parts vanish, styles are
regenerated, and `save()` stamps the current wall clock into `docProps/core.xml`
(#579). Both failures are fatal here — the first silently damages the customer's
workbook, and the second means the same record renders as different bytes every
second. So the package is opened as a zip, exactly one worksheet part is edited
by replacing the byte span of each written cell, and the archive is re-written
through `spreadsheet_conversion.canonical_package`. Nothing in this module reads
a clock.

**What fails closed.** A macro project, an external link, a form control, a
digital signature, a pivot cache, workbook or sheet protection, or any package
part outside the supported inventory refuses the render. So does writing over a
formula cell, adding a subject with no approved row-insertion template, and
retiring a row with no customer-declared retirement mapping — a row is never
deleted and a removal is never silent.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from hashlib import sha256
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.baseline_adoption import (
    adopted_baseline_source,
    adopted_source_rows,
    effective_baseline_formats,
    effective_field_mapping_manifest,
)
from corridor.baseline_workbook import (
    BaselineWorkbookUnsupported,
    OperationsReading,
    read_baseline_workbook,
)
from corridor.current_record import (
    CurrentRecordValue,
    read_project_record_as_of_revision,
)
from corridor.field_mapping_manifest import (
    CARRIES_RETIREMENT_WORDING,
    FieldMappingManifest,
    MappingManifestRefused,
    MaterialMapping,
    composition_rule,
    conformance_refusals,
    exact_text,
)
from corridor.models import BaselineSourceRow, ProjectRecordRevision
from corridor.presentation import label
from corridor.spreadsheet_conversion import canonical_package

# What produced these bytes. Recorded on the receipt beside the identities it
# was bound to, for the same reason `IMPORTER_VERSION` is recorded on an
# adoption: a workbook written by a later renderer is a different rendering of
# the same record, and the two are never assumed interchangeable.
RENDERER_VERSION = "workbook_render_v1"

# The one optional workbook variant this renderer offers, under the name the
# rest of the product already prints for the same thing (`presentation.label`).
# It is configured on the render profile and never chosen per issue, because it
# changes the customer's workbook.
PROVENANCE_SHEET_NAME = label("provenance")

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_RELS_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_WORKSHEET_REL_TYPE = _RELS_NS + "/worksheet"
_WORKSHEET_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
)
_PROVENANCE_REL_ID = "rIdCorridorSourceTraceability"

# Package parts this renderer can carry through unchanged and still stand behind
# the result. Everything outside it refuses the render: a part nobody enumerated
# is a feature nobody proved survives, and a subtly damaged workbook is worse
# than a refusal a person can act on.
#
# `xl/drawings/` and `xl/media/` are here deliberately. A picture, a logo, or a
# chart holds no cell state, and copying the part byte for byte leaves it exactly
# as the customer drew it. A *control* is what would break, and a control never
# arrives alone: `xl/ctrlProps/`, `xl/activeX/`, `<controls>`, `<oleObjects>`, and
# `<legacyDrawing>` all refuse below, so a drawing that reaches this list is one
# with nothing behind it.
_SUPPORTED_PARTS = (
    "[Content_Types].xml",
    "_rels/.rels",
    "docProps/",
    "xl/workbook.xml",
    "xl/_rels/workbook.xml.rels",
    "xl/worksheets/",
    "xl/styles.xml",
    "xl/sharedStrings.xml",
    "xl/theme/",
    "xl/tables/",
    "xl/drawings/",
    "xl/media/",
    "xl/printerSettings/",
    "xl/calcChain.xml",
    "xl/comments",
    "xl/threadedComments/",
    "xl/persons/",
)

# Parts that name a feature this renderer will not silently carry. Each is a
# workbook capability whose behaviour depends on cells it cannot see.
_UNSUPPORTED_PARTS = {
    "xl/vbaProject.bin": "a macro project",
    "xl/externalLinks/": "an external workbook link",
    "xl/pivotCache/": "a pivot cache",
    "xl/pivotTables/": "a pivot table",
    "xl/ctrlProps/": "a form control",
    "xl/activeX/": "an ActiveX control",
    "_xmlsignatures/": "a digital signature",
    "customXml/": "a custom XML data store",
}

# Markup that names the same kind of feature from inside a part.
#
# Each pattern insists the element actually carries something. An empty
# `<workbookProtection/>` is written by openpyxl into every workbook it saves
# and protects nothing; refusing on the tag alone would refuse every workbook
# the product itself produced, which is a false alarm rather than a fail-closed.
_UNSUPPORTED_MARKUP = (
    (re.compile(rb"<sheetProtection\b[^>]*\s\w+="), "worksheet protection"),
    (re.compile(rb"<workbookProtection\b[^>]*\s\w+="), "workbook protection"),
    (re.compile(rb"<fileSharing\b[^>]*\s\w+="), "workbook file sharing"),
    (re.compile(rb"<externalReferences>"), "an external workbook reference"),
    (re.compile(rb"<oleObjects\b(?![^>]*/>)"), "an embedded OLE object"),
    (re.compile(rb"<controls\b(?![^>]*/>)"), "a form control"),
    (re.compile(rb"<legacyDrawing\b"), "a legacy drawing or control layer"),
)


class WorkbookRenderRefused(ValueError):
    """This accepted record cannot be rendered into this workbook."""


class UnapprovedOutputTemplate(WorkbookRenderRefused):
    """These bytes are not the output template this project approved."""


class UnapprovedFieldMapping(WorkbookRenderRefused):
    """This template's columns are not the field mapping this project approved."""


class UnsupportedWorkbookFeature(WorkbookRenderRefused):
    """The workbook holds a feature this renderer cannot preserve."""


class UndeclaredRenderBehaviour(WorkbookRenderRefused):
    """The render needs a behaviour nobody approved outside this issue."""


@dataclass(frozen=True)
class RetirementMapping:
    """How this customer's own form says a conflict left the active record.

    Declared by the customer, never inferred: the form's retirement wording is
    the form's, and inventing one would put a phrase in their matrix that their
    own reviewers do not use.
    """

    field: str
    wording: str


@dataclass(frozen=True)
class RowInsertionTemplate:
    """The one row a new subject may add, and the table metadata that follows it.

    Deliberately small. A new subject adds one worksheet row; whether the sheet's
    table range and filter follow it, and whose formatting the new cells take,
    are the only associated metadata this renderer will touch, and each is stated
    rather than assumed.
    """

    style_source_row: int | None = None
    extend_table_ref: bool = False
    extend_auto_filter: bool = False


@dataclass(frozen=True)
class RenderProfile:
    """Everything about this customer's output settled outside the weekly issue.

    A provenance worksheet changes the customer's workbook, so it is configured
    here and carried by digest onto every receipt — the coordinator is never
    asked to choose it while preparing an issue.
    """

    retirement: RetirementMapping | None = None
    row_insertion: RowInsertionTemplate | None = None
    provenance_worksheet: bool = False

    @property
    def content_sha256(self) -> str:
        return sha256(
            json.dumps(
                {
                    "retirement": (
                        None
                        if self.retirement is None
                        else [self.retirement.field, self.retirement.wording]
                    ),
                    "row_insertion": (
                        None
                        if self.row_insertion is None
                        else [
                            self.row_insertion.style_source_row,
                            self.row_insertion.extend_table_ref,
                            self.row_insertion.extend_auto_filter,
                        ]
                    ),
                    "provenance_worksheet": self.provenance_worksheet,
                    "renderer_version": RENDERER_VERSION,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class RenderedCell:
    """One mapped cell this render wrote, and both identities behind it.

    ``fields`` is plural because one cell need not carry one canonical field: a
    mapping revision may declare a combined range, and then the cell carries
    both ends of it (and the mapping's other column carries none).
    """

    source_row_key: str
    record_subject_key: str
    fields: tuple[str, ...]
    sheet_name: str
    cell_range: str
    before: str
    after: str

    @property
    def field(self) -> str:
        """The canonical fields this cell carries, as one printable name."""

        return "+".join(self.fields)


@dataclass(frozen=True)
class RenderedRow:
    """One declared row a new Project Record subject added."""

    record_subject_key: str
    sheet_name: str
    row_number: int
    fields: tuple[str, ...]


@dataclass(frozen=True)
class RetiredRow:
    """One source row the record no longer carries, retired in the form's words."""

    source_row_key: str
    record_subject_key: str
    sheet_name: str
    cell_range: str
    wording: str


@dataclass(frozen=True)
class MappedWorkbookInput:
    """One consumed mapping input, including cells whose bytes stayed unchanged."""

    source_row_key: str
    record_subject_key: str
    fields: tuple[str, ...]
    sheet_name: str
    cell_range: str
    rendered_value: str
    accepted_fields: tuple[CurrentRecordValue, ...]


@dataclass(frozen=True)
class RenderedWorkbook:
    """The rendered artifact and every identity it is bound to.

    #529 consumes this without reopening the workbook: the revision, the three
    approved identities, the render profile, the renderer, the output digest,
    and the complete bounded change reading are all here.
    """

    content: bytes
    output_sha256: str
    renderer_version: str
    project_id: int
    revision_id: int
    baseline_content_sha256: str
    output_template_identity: str
    output_template_version: str
    output_template_sha256: str
    field_mapping_identity: str
    field_mapping_version: str
    field_mapping_sha256: str
    field_mapping_schema_version: str
    render_profile_sha256: str
    sheet_name: str
    changed_cells: tuple[RenderedCell, ...] = ()
    added_rows: tuple[RenderedRow, ...] = ()
    retired_rows: tuple[RetiredRow, ...] = ()
    changed_parts: tuple[str, ...] = ()
    unchanged_parts: tuple[str, ...] = ()
    package_differences: tuple[str, ...] = ()
    external_references: tuple[tuple[str, str, str], ...] = ()
    mapped_inputs: tuple[MappedWorkbookInput, ...] = ()

    @property
    def field_mapping_revision(self) -> str:
        """The mapping revision this render was performed under."""

        return f"{self.field_mapping_identity} {self.field_mapping_version}"


def render_project_record_workbook(
    session: Session,
    *,
    project_id: int,
    revision_id: int,
    template_bytes: bytes,
    field_mapping: FieldMappingManifest | None = None,
    profile: RenderProfile = RenderProfile(),
) -> RenderedWorkbook:
    """Render one accepted revision into the approved template, or refuse.

    Every input is named rather than discovered: the exact revision, the exact
    template bytes, the mapping revision they are read through, and the profile
    settled at onboarding. What the renderer looks up is only what a person
    already approved — the accepted data-baseline identity, the effective
    output-template identity, and the effective field-mapping identity.

    ``field_mapping`` may be omitted, and then the mapping revision comes from
    what the registration itself stores (#610). A past render is reproducible
    that way without the caller holding the declaration: before #610 the
    registration recorded a digest and nothing that resolved it, so a render
    could be proved to have used a revision but not to have used *this* one.
    A registration that stores no declaration refuses by name rather than
    rendering through an empty mapping.
    """

    baseline = adopted_baseline_source(session, project_id)
    if baseline is None:
        raise WorkbookRenderRefused(
            "this project has not adopted a baseline, so it has no accepted "
            "record to render and no approved template to render it into"
        )
    revision = session.get(ProjectRecordRevision, revision_id)
    if revision is None or revision.project_id != project_id:
        raise WorkbookRenderRefused(
            f"revision {revision_id} is not a Project Record revision of this project"
        )

    formats = effective_baseline_formats(session, project_id)
    template_format = formats.get("output_template")
    mapping_format = formats.get("field_mapping")
    if template_format is None or mapping_format is None:
        raise WorkbookRenderRefused(
            "this project has no effective output-template and field-mapping "
            "registration to render through"
        )

    template_sha256 = sha256(template_bytes).hexdigest()
    if template_sha256 != template_format.content_sha256:
        raise UnapprovedOutputTemplate(
            "these bytes are not the approved output template: "
            f"{template_sha256} was offered and "
            f"{template_format.content_sha256} is registered"
        )

    if field_mapping is None:
        field_mapping = effective_field_mapping_manifest(session, project_id)
        if field_mapping is None:
            raise WorkbookRenderRefused(
                "this project's effective field-mapping registration "
                f"({mapping_format.format_identity} "
                f"{mapping_format.format_version}, "
                f"{mapping_format.content_sha256}) stores no declaration, so "
                "the mapping revision it names cannot be resolved from stored "
                "state. Supply the manifest that digests to it."
            )

    if field_mapping.content_sha256 != mapping_format.content_sha256:
        raise UnapprovedFieldMapping(
            f"{field_mapping.revision} is not the approved mapping revision: "
            f"{field_mapping.content_sha256} was offered and "
            f"{mapping_format.content_sha256} is registered"
        )

    parts = _package_parts(template_bytes)
    _refuse_unsupported_package(parts)
    reading = _template_reading(template_bytes, field_mapping)
    refusals = conformance_refusals(field_mapping, reading)
    if refusals:
        raise UnapprovedFieldMapping(
            "this template is not the approved mapping revision "
            f"{field_mapping.revision}. A changed material field, field "
            "meaning, split or combined value, or controlled vocabulary needs "
            "an explicitly approved mapping revision before the record can be "
            "written through it: " + "; ".join(refusals)
        )

    plan = _plan(
        session,
        project_id=project_id,
        revision_id=revision_id,
        reading=reading,
        manifest=field_mapping,
        profile=profile,
    )
    provenance = (
        ("Project Record revision", str(revision_id)),
        ("Accepted baseline digest", baseline.content_sha256),
        (
            "Approved output template",
            f"{template_format.format_identity} {template_format.format_version} "
            f"{template_format.content_sha256}",
        ),
        (
            "Approved field mapping",
            f"{field_mapping.revision} {mapping_format.content_sha256} "
            f"({field_mapping.schema_version})",
        ),
        ("Render profile", profile.content_sha256),
        ("Renderer", RENDERER_VERSION),
        ("Changed cells", str(len(plan.changed_cells))),
        ("Added rows", str(len(plan.added_rows))),
        ("Retired rows", str(len(plan.retired_rows))),
    )
    rendered_parts, changed, differences = _apply(
        parts, reading, plan, profile, provenance
    )
    content = canonical_package(rendered_parts)
    return RenderedWorkbook(
        content=content,
        output_sha256=sha256(content).hexdigest(),
        renderer_version=RENDERER_VERSION,
        project_id=project_id,
        revision_id=revision_id,
        baseline_content_sha256=baseline.content_sha256,
        output_template_identity=template_format.format_identity,
        output_template_version=template_format.format_version,
        output_template_sha256=template_format.content_sha256,
        field_mapping_identity=mapping_format.format_identity,
        field_mapping_version=mapping_format.format_version,
        field_mapping_sha256=mapping_format.content_sha256,
        field_mapping_schema_version=field_mapping.schema_version,
        render_profile_sha256=profile.content_sha256,
        sheet_name=reading.adopted_sheet,
        changed_cells=plan.changed_cells,
        added_rows=plan.added_rows,
        retired_rows=plan.retired_rows,
        changed_parts=tuple(sorted(changed)),
        unchanged_parts=tuple(sorted(set(parts) - changed)),
        package_differences=differences,
        external_references=plan.external_references,
        mapped_inputs=plan.mapped_inputs,
    )


def workbook_reader_input_manifest(rendered: RenderedWorkbook) -> dict:
    """Read the exact render's complete mapped inputs without rereading state.

    Cells that already matched the accepted value remain represented. This
    carries no claim that unrendered native classes have coverage.
    """
    return {"project_id": rendered.project_id, "revision_id": rendered.revision_id,
        "output_sha256": rendered.output_sha256,
        "field_mapping_sha256": rendered.field_mapping_sha256,
        "mapped_cells": [{"source_row_key": cell.source_row_key,
            "record_subject_key": cell.record_subject_key, "fields": list(cell.fields),
            "sheet_name": cell.sheet_name, "cell_range": cell.cell_range,
            "rendered_value": cell.rendered_value,
            "accepted_fields": [{"fact_type": value.fact_type, "value": _printed(value),
                "fact_id": value.fact_id, "decision_kind": "fact_decision", "decision_id": value.decision_id,
                "revision_id": value.revision_id, "fact_subject_key": value.fact_subject_key or value.subject_key} for value in cell.accepted_fields]}
            for cell in rendered.mapped_inputs],
        "retired_rows": [{"source_row_key": row.source_row_key, "record_subject_key": row.record_subject_key,
            "sheet_name": row.sheet_name, "cell_range": row.cell_range, "wording": row.wording}
            for row in rendered.retired_rows]}


# --- Reading what is to be written -----------------------------------------


@dataclass
class _Subject:
    """One Project Record subject, as this revision leaves it."""

    values: dict[str, str] = dataclass_field(default_factory=dict)
    closure_kind: str | None = None

    @property
    def open_conflict(self) -> bool:
        """Whether the customer's matrix should still show this as a live row."""

        return self.closure_kind != "constraint_closed" and bool(self.values)


@dataclass(frozen=True)
class _Plan:
    changed_cells: tuple[RenderedCell, ...]
    added_rows: tuple[RenderedRow, ...]
    retired_rows: tuple[RetiredRow, ...]
    writes: dict[int, dict[str, str]]
    appended: tuple[tuple[int, dict[str, str]], ...]
    external_references: tuple[tuple[str, str, str], ...]
    mapped_inputs: tuple[MappedWorkbookInput, ...]


def _template_reading(
    template_bytes: bytes, manifest: FieldMappingManifest
) -> OperationsReading:
    """The #509 capability inventory of the template, read from its own bytes.

    Read *through* the mapping revision: which headings carry a reference out
    of the workbook is the manifest's declaration, never a guess at a spelling
    (#597), and a heading nobody declared stays a retained unknown column.
    """

    with TemporaryDirectory() as directory:
        path = Path(directory) / "output-template.xlsx"
        path.write_bytes(template_bytes)
        try:
            reading = read_baseline_workbook(
                path, external_references=manifest.external_reference_headings
            )
        except BaselineWorkbookUnsupported as exc:
            raise UnsupportedWorkbookFeature(
                f"the approved output template cannot be read: {exc}"
            ) from exc
    blocking = [
        item for item in reading.blocking_diagnostics if item.code != "no_source_rows"
    ]
    if blocking:
        raise UnsupportedWorkbookFeature(
            "the approved output template is not renderable: "
            + "; ".join(f"{item.code} {item.locator or ''}".strip() for item in blocking)
        )
    return reading


def _plan(
    session: Session,
    *,
    project_id: int,
    revision_id: int,
    reading: OperationsReading,
    manifest: FieldMappingManifest,
    profile: RenderProfile,
) -> _Plan:
    """Decide every cell before a byte is written, and refuse before that.

    Every write goes through the registered mapping revision rather than
    through a field-to-column dictionary: the mapping decides how many cells a
    canonical value occupies and which of them it lands in, so a project whose
    approved revision declares a combined range writes one cell where a project
    declaring one value per column writes two.
    """

    letters = {column.heading: column.column for column in reading.column_mapping}
    template_rows = {row.source_row_key: row for row in reading.rows}
    formula_cells = set(reading.formula_cells)
    source_rows = {
        row.source_row_key: row
        for row in adopted_source_rows(session, project_id)
        if not row.excluded
    }
    accepted_values = read_project_record_as_of_revision(session, project_id, revision_id)
    record = _record_by_subject(accepted_values)
    mapped_inputs = []

    def retain_input(source_key, record_key, fields, row_number, column, rendered_value):
        mapped_inputs.append(MappedWorkbookInput(source_key, record_key, fields, reading.adopted_sheet,
            f"{column}{row_number}", rendered_value, tuple(value for value in accepted_values
                if value.subject_key == source_key and value.fact_type in fields)))

    writes: dict[int, dict[str, str]] = {}
    changed: list[RenderedCell] = []
    added: list[RenderedRow] = []
    retired: list[RetiredRow] = []
    appended: list[tuple[int, dict[str, str]]] = []
    next_row = max(
        (row.row_number for row in reading.rows),
        default=reading.header_row_number,
    )

    for subject_key, subject in sorted(record.items()):
        values = subject.values
        source_row = source_rows.get(subject_key)
        if source_row is None:
            next_row += 1
            cells = _composed_cells(manifest, values, letters)
            added.append(
                _new_row(
                    reading,
                    profile,
                    manifest,
                    subject_key=subject_key,
                    row_number=next_row,
                    values=values,
                )
            )
            appended.append((next_row, cells))
            for mapping in manifest.mappings:
                rule = composition_rule(mapping.composition)
                for index, heading in enumerate(mapping.source_columns):
                    column = letters[heading]
                    if column in cells:
                        retain_input(subject_key, subject_key, rule.fields_at(mapping, index), next_row, column, cells[column])
            continue
        template_row = template_rows.get(subject_key)
        if template_row is None:
            raise WorkbookRenderRefused(
                f"the approved output template carries no row at "
                f"{subject_key!r}, which the accepted record still holds"
            )
        if template_row.business_identity != source_row.business_identity:
            raise WorkbookRenderRefused(
                f"the template row at {subject_key!r} prints "
                f"{template_row.business_identity!r} where the accepted source "
                f"row is {source_row.business_identity!r}; the rows have moved "
                "and an accepted value would be written into another line"
            )
        for mapping in manifest.mappings:
            accepted = tuple(values.get(field) for field in mapping.target_fields)
            if all(value is None for value in accepted):
                continue
            rule = composition_rule(mapping.composition)
            try:
                composed = rule.compose(mapping, accepted)
            except MappingManifestRefused as exc:
                raise UndeclaredRenderBehaviour(
                    f"{subject_key!r} cannot be written through the approved "
                    f"mapping revision {manifest.revision}: {exc}"
                ) from exc
            for index, heading in enumerate(mapping.source_columns):
                before = exact_text(template_row, mapping.target_fields[index])
                after = composed[index]
                column = letters[heading]
                retain_input(source_row.source_row_key, source_row.record_subject_key or subject_key,
                    rule.fields_at(mapping, index), source_row.row_number, column, after)
                if after == before:
                    continue
                cell_range = f"{column}{source_row.row_number}"
                locator = f"{reading.adopted_sheet}!{cell_range}"
                if locator in formula_cells:
                    raise UnsupportedWorkbookFeature(
                        f"{locator} is a formula cell the approved mapping "
                        "writes to; overwriting it would drop the customer's "
                        "own calculation"
                    )
                writes.setdefault(source_row.row_number, {})[column] = after
                changed.append(
                    RenderedCell(
                        source_row_key=source_row.source_row_key,
                        record_subject_key=(
                            source_row.record_subject_key or subject_key
                        ),
                        fields=rule.fields_at(mapping, index),
                        sheet_name=reading.adopted_sheet,
                        cell_range=cell_range,
                        before=before,
                        after=after,
                    )
                )

    for key, source_row in sorted(source_rows.items()):
        subject = record.get(key)
        if subject is not None and subject.open_conflict:
            continue
        retired.append(
            _retire(
                reading, profile, manifest, source_row, letters, formula_cells, writes
            )
        )

    return _Plan(
        mapped_inputs=tuple(mapped_inputs),
        changed_cells=tuple(changed),
        added_rows=tuple(added),
        retired_rows=tuple(retired),
        writes=writes,
        appended=tuple(appended),
        external_references=tuple(
            (row.source_row_key, row.external_system_id or "", row.source_url or "")
            for row in sorted(source_rows.values(), key=lambda row: row.row_number)
            if row.external_system_id or row.source_url
        ),
    )


def _composed_cells(
    manifest: FieldMappingManifest,
    values: dict[str, str],
    letters: dict[str, str],
) -> dict[str, str]:
    """Every column one subject's accepted values occupy, by column letter."""

    cells: dict[str, str] = {}
    for mapping in manifest.mappings:
        accepted = tuple(values.get(field) for field in mapping.target_fields)
        if all(value is None for value in accepted):
            continue
        rule = composition_rule(mapping.composition)
        try:
            composed = rule.compose(mapping, accepted)
        except MappingManifestRefused as exc:
            raise UndeclaredRenderBehaviour(
                f"these accepted values cannot be written through "
                f"{manifest.revision}: {exc}"
            ) from exc
        for heading, text in zip(mapping.source_columns, composed):
            if text:
                cells[letters[heading]] = text
    return dict(sorted(cells.items()))


def _new_row(
    reading: OperationsReading,
    profile: RenderProfile,
    manifest: FieldMappingManifest,
    *,
    subject_key: str,
    row_number: int,
    values: dict[str, str],
) -> RenderedRow:
    if profile.row_insertion is None:
        raise UndeclaredRenderBehaviour(
            f"{subject_key!r} is a Project Record subject the adopted baseline "
            "has no source row for, and this project has approved no "
            "row-insertion template to add one with"
        )
    return RenderedRow(
        record_subject_key=subject_key,
        sheet_name=reading.adopted_sheet,
        row_number=row_number,
        fields=tuple(
            sorted(name for name in values if manifest.mapping_for(name) is not None)
        ),
    )


def _retire(
    reading: OperationsReading,
    profile: RenderProfile,
    manifest: FieldMappingManifest,
    source_row: BaselineSourceRow,
    letters: dict[str, str],
    formula_cells: set[str],
    writes: dict[int, dict[str, str]],
) -> RetiredRow:
    """Write the customer's own retirement wording; never remove the row."""

    if profile.retirement is None:
        raise UndeclaredRenderBehaviour(
            f"the accepted record no longer carries {source_row.source_row_key!r} "
            "as an open conflict, and this customer has declared no retirement "
            "or status mapping to say so in their own form's words"
        )
    mapping = manifest.mapping_for(profile.retirement.field)
    if mapping is None:
        raise UndeclaredRenderBehaviour(
            f"the declared retirement mapping writes {profile.retirement.field!r}, "
            "which the approved template heads no column for"
        )
    _refuse_undeclared_retirement(mapping, manifest, profile.retirement.field)
    cell_range = f"{letters[mapping.source_columns[0]]}{source_row.row_number}"
    locator = f"{reading.adopted_sheet}!{cell_range}"
    if locator in formula_cells:
        raise UnsupportedWorkbookFeature(
            f"{locator} is a formula cell the declared retirement mapping writes to"
        )
    writes.setdefault(source_row.row_number, {})[
        letters[mapping.source_columns[0]]
    ] = profile.retirement.wording
    return RetiredRow(
        source_row_key=source_row.source_row_key,
        record_subject_key=source_row.record_subject_key or source_row.source_row_key,
        sheet_name=reading.adopted_sheet,
        cell_range=cell_range,
        wording=profile.retirement.wording,
    )


def _refuse_undeclared_retirement(
    mapping: MaterialMapping, manifest: FieldMappingManifest, field: str
) -> None:
    """A retirement wording goes only where the mapping revision says it may.

    Not a formality: writing the customer's own `Not Used` into one end of a
    combined range, or into a column their mapping says carries something else,
    is the same silent damage as writing a split value into a combined column.
    """

    if mapping.retirement != CARRIES_RETIREMENT_WORDING:
        raise UndeclaredRenderBehaviour(
            f"the approved mapping revision {manifest.revision} declares that "
            f"{field!r} does not carry a retirement wording ({mapping.retirement})"
        )
    if len(mapping.source_columns) != 1:
        raise UndeclaredRenderBehaviour(
            f"the approved mapping revision {manifest.revision} carries {field!r} "
            f"across {len(mapping.source_columns)} columns, and a retirement "
            "wording is one value in one column"
        )


def _record_by_subject(
    values: tuple[CurrentRecordValue, ...]
) -> dict[str, "_Subject"]:
    """The accepted revision as printable text, by Project Record subject.

    A satellite-valued Fact prints nothing: `applies_to` and `closure_result`
    are references into the record rather than words the customer's form prints,
    and writing an internal id into their matrix would be inventing a value. The
    closure is still read, as the one thing it does say about the customer's own
    row — that the conflict is no longer open.
    """

    subjects: dict[str, _Subject] = {}
    for value in values:
        held = subjects.setdefault(value.subject_key, _Subject())
        text_value = _printed(value)
        if text_value is not None:
            held.values[value.fact_type] = text_value
        if value.fact_type == "closure_result":
            held.closure_kind = value.closure_kind
    return subjects


def _printed(value: CurrentRecordValue) -> str | None:
    if value.text_value is not None:
        return value.text_value
    if value.date_value is not None:
        return value.date_value.isoformat()
    return None


# --- The package ------------------------------------------------------------


def _package_parts(data: bytes) -> dict[str, bytes]:
    from io import BytesIO
    from zipfile import BadZipFile, ZipFile

    try:
        with ZipFile(BytesIO(data)) as archive:
            return {name: archive.read(name) for name in archive.namelist()}
    except BadZipFile as exc:
        raise UnsupportedWorkbookFeature(
            f"the approved output template is not an OOXML package: {exc}"
        ) from exc


def _refuse_unsupported_package(parts: dict[str, bytes]) -> None:
    """Fail closed on any feature or part this renderer cannot stand behind."""

    refusals: list[str] = []
    for name in sorted(parts):
        for prefix, description in _UNSUPPORTED_PARTS.items():
            if name.startswith(prefix):
                refusals.append(f"{name} carries {description}")
                break
        else:
            if not any(name.startswith(prefix) for prefix in _SUPPORTED_PARTS):
                refusals.append(f"{name} is a package part this renderer cannot read")
    for name in sorted(parts):
        if not name.endswith(".xml") and not name.endswith(".rels"):
            continue
        for markup, description in _UNSUPPORTED_MARKUP:
            if markup.search(parts[name]):
                refusals.append(f"{name} declares {description}")
    if refusals:
        raise UnsupportedWorkbookFeature(
            "the approved output template holds features this renderer will not "
            "silently damage: " + "; ".join(refusals)
        )


def _apply(
    parts: dict[str, bytes],
    reading: OperationsReading,
    plan: _Plan,
    profile: RenderProfile,
    provenance: tuple[tuple[str, str], ...],
) -> tuple[dict[str, bytes], set[str], tuple[str, ...]]:
    """Write the planned cells, and say which parts changed and why."""

    rendered = dict(parts)
    changed: set[str] = set()
    differences: list[str] = []
    sheet_part = _sheet_part_name(parts, reading.adopted_sheet)
    part = parts[sheet_part]

    if plan.writes:
        part = _write_cells(part, plan.writes, sheet_part)
        differences.append(
            f"{sheet_part}: {sum(len(row) for row in plan.writes.values())} mapped "
            "cells rewritten as inline strings; every other byte of the part is "
            "unchanged"
        )
    if plan.appended:
        styles = _row_styles(part, profile)
        part = _append_rows(part, plan.appended, styles)
        last = max(number for number, _ in plan.appended)
        part = _extend_ref(part, b"dimension", last)
        differences.append(
            f"{sheet_part}: {len(plan.appended)} declared rows appended and the "
            "sheet dimension extended to row " + str(last)
        )
        insertion = profile.row_insertion
        if insertion is not None and insertion.extend_auto_filter:
            part = _extend_ref(part, b"autoFilter", last)
            differences.append(f"{sheet_part}: the sheet filter range follows the rows")
        if insertion is not None and insertion.extend_table_ref:
            for name in sorted(parts):
                if name.startswith("xl/tables/"):
                    rendered[name] = _extend_ref(parts[name], b"table", last)
                    if rendered[name] != parts[name]:
                        changed.add(name)
                        differences.append(f"{name}: the table range follows the rows")

    if part != parts[sheet_part]:
        rendered[sheet_part] = part
        changed.add(sheet_part)

    if profile.provenance_worksheet:
        rendered, added = _add_provenance_worksheet(rendered, provenance)
        changed.update(added)
        differences.append(
            f"a {PROVENANCE_SHEET_NAME!r} worksheet is added, with the workbook "
            "part, its relationships, and the content types that declare it"
        )

    differences.append(
        "the archive is rewritten with pinned entry metadata so identical inputs "
        "produce identical bytes; every part's content is byte-identical unless "
        "named above"
    )
    return rendered, changed, tuple(differences)


def _sheet_part_name(parts: dict[str, bytes], sheet_name: str) -> str:
    """The worksheet part the adopted sheet's name resolves to."""

    workbook = parts["xl/workbook.xml"].decode("utf-8")
    prefix = _relationship_prefix(workbook)
    identifiers = {}
    for element in re.finditer(r"<sheet\b[^>]*?/?>", workbook):
        tag = element.group(0)
        name = _tag_attribute(tag, "name")
        relationship_id = _tag_attribute(tag, f"{prefix}:id")
        if name is not None and relationship_id is not None:
            identifiers[_unescape(name)] = relationship_id
    relationship = identifiers.get(sheet_name)
    if relationship is None:
        raise UnsupportedWorkbookFeature(
            f"the approved output template's workbook part names no sheet "
            f"{sheet_name!r} with a relationship"
        )
    rels = parts["xl/_rels/workbook.xml.rels"].decode("utf-8")
    targets = {}
    for element in re.finditer(r"<Relationship\b[^>]*?/?>", rels):
        tag = element.group(0)
        identifier = _tag_attribute(tag, "Id")
        target_path = _tag_attribute(tag, "Target")
        if identifier is not None and target_path is not None:
            targets[identifier] = target_path
    target = targets.get(relationship)
    if target is None:
        raise UnsupportedWorkbookFeature(
            f"the approved output template has no part for sheet {sheet_name!r}"
        )
    return "xl/" + target.lstrip("/").removeprefix("xl/")


def _tag_attribute(tag: str, name: str) -> str | None:
    """One attribute of an XML open tag, whatever order the writer used."""

    match = re.search(re.escape(name) + r'="([^"]*)"', tag)
    return match.group(1) if match else None


def _relationship_prefix(workbook: str) -> str:
    match = re.search(r'xmlns:([A-Za-z0-9_.-]+)="' + re.escape(_RELS_NS) + '"', workbook)
    if match is None:
        raise UnsupportedWorkbookFeature(
            "the approved output template's workbook part binds no relationship "
            "namespace, so its sheets cannot be resolved to parts"
        )
    return match.group(1)


# --- Worksheet XML surgery --------------------------------------------------
#
# Every edit is a byte-span replacement inside one part. Re-serializing the
# worksheet through an XML library was tried first and rejected: it renames
# namespace prefixes, which breaks `mc:Ignorable`, and it rewrites markup the
# render did not touch, which destroys the one property #529 needs — that an
# unchanged part, and an unchanged cell inside a changed part, is byte-identical
# to the customer's own template.


def _elements(data: bytes, tag: bytes, start: int, end: int):
    """Every element of one tag in a span, as (start, end, open tag)."""

    opening = re.compile(rb"<" + tag + rb"\b[^>]*?(/?)>")
    closing = b"</" + tag + b">"
    position = start
    while True:
        match = opening.search(data, position, end)
        if match is None:
            return
        if match.group(1) == b"/":
            finish = match.end()
        else:
            found = data.find(closing, match.end(), end)
            if found == -1:
                raise UnsupportedWorkbookFeature(
                    f"the worksheet part has an unclosed <{tag.decode()}> element"
                )
            finish = found + len(closing)
        yield match.start(), finish, match.group(0)
        position = finish


def _attribute(open_tag: bytes, name: bytes) -> str | None:
    match = re.search(rb"\b" + name + rb'="([^"]*)"', open_tag)
    return match.group(1).decode("utf-8") if match else None


def _sheet_data_span(part: bytes) -> tuple[int, int, bool]:
    match = re.search(rb"<sheetData\b[^>]*?(/?)>", part)
    if match is None:
        raise UnsupportedWorkbookFeature("the worksheet part has no <sheetData>")
    if match.group(1) == b"/":
        return match.start(), match.end(), True
    finish = part.find(b"</sheetData>", match.end())
    if finish == -1:
        raise UnsupportedWorkbookFeature("the worksheet part has no </sheetData>")
    return match.end(), finish, False


def _column_index(column: str) -> int:
    index = 0
    for char in column:
        index = index * 26 + (ord(char.upper()) - ord("A") + 1)
    return index


def _escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _unescape(value: str) -> str:
    return value.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _cell_markup(reference: str, style: str | None, text: str) -> bytes:
    attribute = f' s="{style}"' if style is not None else ""
    return (
        f'<c r="{reference}"{attribute} t="inlineStr"><is><t>'
        f"{_escape(text)}</t></is></c>"
    ).encode("utf-8")


def _rewrite_row(row: bytes, number: int, values: dict[str, str]) -> bytes:
    """One worksheet row with the named columns written and nothing else moved."""

    match = re.match(rb"<row\b[^>]*?(/?)>", row)
    if match is None:
        raise UnsupportedWorkbookFeature("a worksheet row has no opening tag")
    self_closed = match.group(1) == b"/"
    opening = match.group(0)[:-2] + b">" if self_closed else match.group(0)
    inner = b"" if self_closed else row[match.end() : -len(b"</row>")]

    existing = [
        (start, finish, _attribute(open_tag, b"r") or "", open_tag)
        for start, finish, open_tag in _elements(inner, b"c", 0, len(inner))
    ]
    edits: list[tuple[int, int, bytes]] = []
    for column, text in values.items():
        reference = f"{column}{number}"
        replaced = next(
            (item for item in existing if item[2] == reference),
            None,
        )
        if replaced is not None:
            edits.append(
                (
                    replaced[0],
                    replaced[1],
                    _cell_markup(reference, _attribute(replaced[3], b"s"), text),
                )
            )
            continue
        position = len(inner)
        for start, _finish, existing_reference, _open_tag in existing:
            letters = "".join(char for char in existing_reference if char.isalpha())
            if letters and _column_index(letters) > _column_index(column):
                position = start
                break
        edits.append((position, position, _cell_markup(reference, None, text)))

    for start, finish, markup in sorted(edits, key=lambda item: item[0], reverse=True):
        inner = inner[:start] + markup + inner[finish:]
    return opening + inner + b"</row>"


def _write_cells(
    part: bytes, writes: dict[int, dict[str, str]], sheet_part: str
) -> bytes:
    start, finish, self_closed = _sheet_data_span(part)
    if self_closed:
        raise UnsupportedWorkbookFeature(
            f"{sheet_part} holds no rows to write the accepted record into"
        )
    edits: list[tuple[int, int, bytes]] = []
    seen: set[int] = set()
    for row_start, row_end, open_tag in _elements(part, b"row", start, finish):
        number = _attribute(open_tag, b"r")
        if number is None or int(number) not in writes:
            continue
        seen.add(int(number))
        edits.append(
            (
                row_start,
                row_end,
                _rewrite_row(part[row_start:row_end], int(number), writes[int(number)]),
            )
        )
    missing = sorted(set(writes) - seen)
    if missing:
        raise UnsupportedWorkbookFeature(
            f"{sheet_part} has no row " + ", ".join(map(str, missing))
        )
    for row_start, row_end, markup in sorted(edits, reverse=True):
        part = part[:row_start] + markup + part[row_end:]
    return part


def _row_styles(part: bytes, profile: RenderProfile) -> dict[str, str]:
    """Each mapped column's style on the row a new row takes its formatting from."""

    insertion = profile.row_insertion
    if insertion is None or insertion.style_source_row is None:
        return {}
    start, finish, self_closed = _sheet_data_span(part)
    if self_closed:
        return {}
    for row_start, row_end, open_tag in _elements(part, b"row", start, finish):
        if _attribute(open_tag, b"r") != str(insertion.style_source_row):
            continue
        row = part[row_start:row_end]
        styles: dict[str, str] = {}
        for cell_start, cell_end, cell_tag in _elements(row, b"c", 0, len(row)):
            reference = _attribute(cell_tag, b"r") or ""
            style = _attribute(cell_tag, b"s")
            letters = "".join(char for char in reference if char.isalpha())
            if letters and style is not None:
                styles[letters] = style
        return styles
    return {}


def _append_rows(
    part: bytes,
    rows: tuple[tuple[int, dict[str, str]], ...],
    styles: dict[str, str],
) -> bytes:
    start, finish, self_closed = _sheet_data_span(part)
    markup = b"".join(
        b'<row r="%d">' % number
        + b"".join(
            _cell_markup(f"{column}{number}", styles.get(column), text)
            for column, text in sorted(
                values.items(), key=lambda item: _column_index(item[0])
            )
        )
        + b"</row>"
        for number, values in rows
    )
    if self_closed:
        return part[:start] + b"<sheetData>" + markup + b"</sheetData>" + part[finish:]
    return part[:finish] + markup + part[finish:]


def _extend_ref(part: bytes, tag: bytes, last_row: int) -> bytes:
    """Extend one `ref="A1:O9"` range down to a row, and never shrink it."""

    match = re.search(rb"<" + tag + rb'\b[^>]*?\bref="([^"]*)"', part)
    if match is None:
        return part
    reference = match.group(1).decode("utf-8")
    if ":" not in reference:
        return part
    first, second = reference.split(":", 1)
    letters = "".join(char for char in second if char.isalpha())
    digits = "".join(char for char in second if char.isdigit())
    if not digits or int(digits) >= last_row:
        return part
    replacement = f"{first}:{letters}{last_row}".encode("utf-8")
    return part[: match.start(1)] + replacement + part[match.end(1) :]


# --- The one optional workbook variant --------------------------------------


def _add_provenance_worksheet(
    parts: dict[str, bytes], rows: tuple[tuple[str, str], ...]
) -> tuple[dict[str, bytes], set[str]]:
    """Add the configured provenance worksheet, and everything that declares it.

    Four package parts change and each is named on the receipt, because adding a
    worksheet is the one intentional structural difference this renderer makes to
    the customer's own package.
    """

    workbook = parts["xl/workbook.xml"].decode("utf-8")
    if f'name="{_escape(PROVENANCE_SHEET_NAME)}"' in workbook:
        raise UnsupportedWorkbookFeature(
            f"the approved output template already heads a {PROVENANCE_SHEET_NAME!r} "
            "worksheet, so the configured provenance variant would overwrite it"
        )
    rels = parts["xl/_rels/workbook.xml.rels"].decode("utf-8")
    if _PROVENANCE_REL_ID in rels:
        raise UnsupportedWorkbookFeature(
            "the approved output template already binds this renderer's "
            "provenance relationship"
        )
    prefix = _relationship_prefix(workbook)
    used = {
        int(match.group(1))
        for match in re.finditer(r"xl/worksheets/sheet(\d+)\.xml", " ".join(parts))
    }
    number = max(used, default=0) + 1
    part_name = f"xl/worksheets/sheet{number}.xml"
    sheet_ids = [
        int(match.group(1))
        for match in re.finditer(r'<sheet\b[^>]*?sheetId="(\d+)"', workbook)
    ]

    added = dict(parts)
    added[part_name] = _provenance_part(rows)
    added["xl/workbook.xml"] = workbook.replace(
        "</sheets>",
        f'<sheet name="{_escape(PROVENANCE_SHEET_NAME)}" '
        f'sheetId="{max(sheet_ids, default=0) + 1}" '
        f'{prefix}:id="{_PROVENANCE_REL_ID}"/></sheets>',
        1,
    ).encode("utf-8")
    added["xl/_rels/workbook.xml.rels"] = rels.replace(
        "</Relationships>",
        f'<Relationship Id="{_PROVENANCE_REL_ID}" Type="{_WORKSHEET_REL_TYPE}" '
        f'Target="worksheets/sheet{number}.xml"/></Relationships>',
        1,
    ).encode("utf-8")
    content_types = parts["[Content_Types].xml"].decode("utf-8")
    added["[Content_Types].xml"] = content_types.replace(
        "</Types>",
        f'<Override PartName="/{part_name}" ContentType="{_WORKSHEET_TYPE}"/></Types>',
        1,
    ).encode("utf-8")
    return added, {
        part_name,
        "xl/workbook.xml",
        "xl/_rels/workbook.xml.rels",
        "[Content_Types].xml",
    }


def _provenance_part(rows: tuple[tuple[str, str], ...]) -> bytes:
    """The provenance worksheet, with no wall clock anywhere in it."""

    markup = []
    for number, (name, value) in enumerate(rows, start=1):
        markup.append(
            b'<row r="%d">' % number
            + _cell_markup(f"A{number}", None, name)
            + _cell_markup(f"B{number}", None, value)
            + b"</row>"
        )
    return (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<worksheet xmlns="' + _MAIN_NS.encode("utf-8") + b'"><sheetData>'
        + b"".join(markup)
        + b"</sheetData></worksheet>"
    )
