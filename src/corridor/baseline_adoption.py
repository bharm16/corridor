"""Adopt Baseline: the coordinator's reading, and the one atomic Save (#509).

Corridor operations proves the workbook mechanics first
(``baseline_workbook``).  This module is the other half: it turns that reading
into the small set of **material project questions** a coordinator is entitled
to decide, binds the exact preview they saw, and performs the adoption as one
attributable, atomic act.

Why the split is enforced here rather than described in a comment.  The failure
this ticket exists to prevent is a coordinator being asked to adjudicate a
spreadsheet — "column J has no canonical field", "row 41 is hidden", "this cell
is a formula".  None of that is a project decision, and a person who answers a
hundred of them has not decided anything about their utilities.  So
``preview_baseline_adoption`` refuses to produce a coordinator reading at all
while operations has an unresolved diagnostic, and the coordinator reading is a
closed set of typed question kinds that mechanically cannot carry an importer
diagnostic.  A technical failure reaches the coordinator only where it *is* a
material project fact: a populated **material** value that no released
transformation can type is a question, and the identical failure on a
non-material column is not.

What the act writes, and why it is one act.  ADR-0076 makes Adopt Baseline one
bulk project decision, never hundreds of row-level clicks, and atomic.  So a
single ``SECURITY DEFINER`` command writes one Project Record revision, one
separately identified ``fact_decisions`` row per adopted Source Fact, the
accepted data-baseline identity, the source-row identities, and the
output-template and field-mapping identities — or none of them.  The same
transaction then invokes #520's one-way operating-mode transition with the
revision it just wrote, so from the moment the adoption commits no legacy
automatic-admission path can replace an accepted value.

What was tried before, and rejected.  Adopting through the existing native
extractor (``extract_sheet`` -> Candidates -> automatic Record Inclusion) would
have reached the accepted record without any human act at all, which is exactly
the ADR-0029 behaviour ADR-0076 supersedes; it also drops retired rows and rows
missing a required field, and an adoption that silently drops rows is not an
adoption of the customer's record.  Storing the coordinator preview as a
row-by-row acceptance queue was rejected for the reason ADR-0076 gives: a
500-row UCM would need 500 attributable clicks to establish a record the
customer already accepts.  The preview is therefore bound by one digest and
adopted whole; a preview that no longer matches the bytes or the reading is
refused rather than partially applied.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import BigInteger, bindparam, cast, func, select
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Session

from corridor import audit
from corridor.access import COORDINATION, resolve_membership
from corridor.baseline_workbook import (
    IMPORTER_IDENTITY,
    IMPORTER_VERSION,
    BaselineRow,
    BaselineWorkbookUnsupported,
    OperationsReading,
    read_baseline_workbook,
)
from corridor.extraction_runs import record_extraction_run
from corridor.extractor_lineage import deployed_extractor_config, zero_token_usage
from corridor.field_mapping_manifest import (
    FieldMappingManifest,
    MappingDeclaration,
    MappingManifestRefused,
    declared_field_mapping,
    manifest_from_declaration,
    prove_manifest,
)
from corridor.materializer import materialize_segment_value
from corridor.models import (
    BaselineAdoption,
    BaselineFormat,
    BaselineFormatManifest,
    BaselineFormatObject,
    BaselineSource,
    BaselineSourceRow,
    Dependency,
    Document,
    FactDecision,
    Project,
    SourceSegment,
)
from corridor.object_storage import (
    ObjectStore,
    StorageError,
    content_key,
    content_store,
    digest_bytes,
)
from corridor.operating_mode import adopt_project_baseline
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.source_append import append_fact
from corridor.source_intake import (
    IntakeConflict,
    StagedSource,
    confirm_intake,
    preview_intake,
)
from corridor.storage import staged_file
from corridor.support_assessments import FactProposition, record_support_assessment


BASELINE_DOC_TYPE = "matrix"
BASELINE_COMMAND_TYPE = "adopt_baseline"

# The field mapping this importer applies is a declared manifest and its
# identity is that manifest's (`field_mapping_manifest`), not a digest of
# anything read off the template: a successor form that combines two mapped
# values while printing the same headings is a different mapping revision, and
# only a declaration can say so (#597).

# The closed set of questions a coordinator may be asked while adopting.  A
# question kind outside this set is a bug, not a new feature: the whole point
# is that workbook mechanics have nowhere to go.
COORDINATOR_QUESTION_KINDS = (
    "adopted_scope",
    "duplicate_business_identity",
    "likely_distinct_facilities",
    "unmappable_material_value",
    "proposed_exclusion",
    "conflicting_plan_basis",
)

# The material descriptors two rows sharing one business identity must agree on
# before they are the same facility twice rather than two facilities under one
# number.
_FACILITY_DESCRIPTORS = (
    "external_org",
    "utility_type",
    "station_from",
    "station_to",
)


class BaselineAdoptionRefused(ValueError):
    """This project cannot adopt this source as its initial accepted record."""


class BaselineOperationsUnresolved(BaselineAdoptionRefused):
    """Corridor operations has not settled the workbook mechanics yet.

    Deliberately its own type: it is the boundary that keeps importer
    diagnostics away from the coordinator, and a caller that catches
    ``BaselineAdoptionRefused`` broadly still must not render one of these to a
    project person.
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


class StaleBaselinePreview(BaselineAdoptionRefused):
    """The preview being adopted no longer describes the source or the reading."""


@dataclass(frozen=True)
class FormatIdentity:
    """One registered output-template or field-mapping identity."""

    kind: str
    identity: str
    version: str
    content_sha256: str

    def as_payload(self) -> dict[str, str]:
        return {
            "format_kind": self.kind,
            "format_identity": self.identity,
            "format_version": self.version,
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True)
class CoordinatorQuestion:
    """One material project question, in the coordinator's own terms."""

    kind: str
    subject: str
    detail: str
    source_rows: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "subject": self.subject,
            "detail": self.detail,
            "source_rows": list(self.source_rows),
        }


@dataclass(frozen=True)
class PreviewRow:
    """One source row and the Project Record subject it would resolve to."""

    row: BaselineRow
    record_subject_key: str | None

    def as_payload(self) -> dict[str, Any]:
        return {
            "source_row_key": self.row.source_row_key,
            "sheet_name": self.row.sheet_name,
            "row_number": self.row.row_number,
            "business_identity": self.row.business_identity,
            "record_subject_key": self.record_subject_key,
            "external_system_id": self.row.external_system_id,
            "source_url": self.row.source_url,
            "excluded": self.row.excluded,
            "exclusion_reason": self.row.exclusion_reason,
        }


@dataclass(frozen=True)
class BaselinePreview:
    """Exactly what a named person adopts in one Save.

    ``operations`` travels with the preview so an operator can read it; it is
    never part of the coordinator reading and never part of what the
    coordinator is asked to decide.
    """

    project_id: int
    project_slug: str
    customer: str
    source_identity: str
    source_kind: str
    filename: str
    content_sha256: str
    byte_size: int
    operations: OperationsReading
    questions: tuple[CoordinatorQuestion, ...]
    rows: tuple[PreviewRow, ...]
    output_template: FormatIdentity
    field_mapping: FormatIdentity
    field_mapping_manifest: FieldMappingManifest
    field_mapping_declaration: MappingDeclaration
    intake_binding_fingerprint: str
    binding_fingerprint: str
    already_adopted: bool

    @property
    def adopted_rows(self) -> tuple[PreviewRow, ...]:
        return tuple(item for item in self.rows if not item.row.excluded)

    @property
    def excluded_rows(self) -> tuple[PreviewRow, ...]:
        return tuple(item for item in self.rows if item.row.excluded)

    @property
    def adopted_value_count(self) -> int:
        return sum(len(item.row.values) for item in self.adopted_rows)

    def questions_of(self, kind: str) -> tuple[CoordinatorQuestion, ...]:
        return tuple(item for item in self.questions if item.kind == kind)


@dataclass(frozen=True)
class BaselineAdoptionResult:
    """The receipt of one adoption, replayable to the same answer."""

    revision_id: int
    baseline_source_id: int
    document_id: int
    adoption_id: int
    fact_ids: tuple[int, ...]
    created: bool


def preview_baseline_adoption(
    session: Session,
    *,
    project: Project,
    staged: StagedSource,
    customer: str,
    source_identity: str,
    source_kind: str = "ucm_workbook",
    output_template: FormatIdentity | None = None,
    field_mapping: MappingDeclaration | None = None,
) -> BaselinePreview:
    """Read the staged source and report only the material project questions.

    ``field_mapping`` is the mapping revision Corridor operations declares for
    this form: which columns carry which canonical fields, how their values
    split or combine, and which headings carry a reference out of the workbook.
    It defaults to one value per column under the published headings and no
    external references at all, because nothing in a file may be given a
    meaning it did not declare (#597).

    Writes nothing.  Refuses before producing a coordinator reading when
    Corridor operations has an unresolved diagnostic, and when the project
    already holds an accepted Project Record that adopting would overwrite.
    """

    if not customer.strip() or not source_identity.strip():
        raise BaselineAdoptionRefused(
            "Adopt Baseline names the customer and their own name for the source"
        )
    if source_kind not in ("ucm_workbook", "system_export"):
        raise BaselineAdoptionRefused(f"unknown baseline source kind {source_kind!r}")

    path = staged_file(staged.sha256)
    if path is None:
        raise BaselineAdoptionRefused(
            "the staged source is no longer in the content store; upload it again"
        )
    declaration = field_mapping or MappingDeclaration()
    try:
        operations = read_baseline_workbook(
            path, external_references=declaration.external_reference_headings
        )
    except BaselineWorkbookUnsupported as exc:
        raise BaselineAdoptionRefused(str(exc)) from exc
    if not operations.resolved:
        raise BaselineOperationsUnresolved(operations)

    already = session.scalar(
        select(BaselineSource).where(BaselineSource.project_id == project.id)
    )
    if already is None:
        _refuse_nonempty_project_record(session, project)

    rows = assign_record_subjects(operations.rows)
    questions = _coordinator_questions(
        operations, rows, customer=customer, source_identity=source_identity
    )
    template = output_template or FormatIdentity(
        kind="output_template",
        identity=source_identity,
        version="as-adopted",
        content_sha256=staged.sha256,
    )
    if template.kind != "output_template":
        raise BaselineAdoptionRefused(
            "the output template identity must be an output_template"
        )
    try:
        manifest = declared_field_mapping(operations, declaration)
    except MappingManifestRefused as exc:
        raise BaselineAdoptionRefused(
            f"this form is not the declared mapping revision: {exc}"
        ) from exc
    mapping = format_identity_of(manifest)
    intake = preview_intake(session, project, staged, BASELINE_DOC_TYPE)

    preview = BaselinePreview(
        project_id=project.id,
        project_slug=project.slug,
        customer=customer.strip(),
        source_identity=source_identity.strip(),
        source_kind=source_kind,
        filename=staged.filename,
        content_sha256=staged.sha256,
        byte_size=staged.size_bytes,
        operations=operations,
        questions=questions,
        rows=rows,
        output_template=template,
        field_mapping=mapping,
        field_mapping_manifest=manifest,
        field_mapping_declaration=declaration,
        intake_binding_fingerprint=intake.binding_fingerprint,
        binding_fingerprint="",
        already_adopted=already is not None,
    )
    return _with_fingerprint(preview)


def adopt_baseline(
    session: Session,
    *,
    preview: BaselinePreview,
    principal: HumanPrincipal,
    idempotency_key: str,
    images_dir: Path | str | None = None,
) -> BaselineAdoptionResult:
    """Run the existing named-human baseline command under its owner bootstrap."""
    from corridor.activation_runtime import owner_source_bootstrap
    # Human validation precedes entering the separate source bootstrap context.
    require_human_principal(principal)
    with owner_source_bootstrap(session):
        return _adopt_baseline(session, preview=preview, principal=principal,
            idempotency_key=idempotency_key, images_dir=images_dir)


def _adopt_baseline(
    session: Session,
    *,
    preview: BaselinePreview,
    principal: HumanPrincipal,
    idempotency_key: str,
    images_dir: Path | str | None = None,
) -> BaselineAdoptionResult:
    """Adopt the complete previewed baseline in one Save, or write nothing.

    Runs in the caller's transaction.  Every refusal happens before the first
    write, and the accepted-authority half is one command, so a partial
    adoption is not a state a caller can reach.  A replay with the same key and
    the same bytes returns the receipt already written.
    """

    actor = require_human_principal(principal)
    if not idempotency_key.strip():
        raise BaselineAdoptionRefused("Adopt Baseline needs an idempotency key")

    project = session.get_one(Project, preview.project_id)
    current = _current_preview(session, preview)
    if current.binding_fingerprint != preview.binding_fingerprint:
        raise StaleBaselinePreview(
            "This adoption no longer matches the source you previewed. Preview "
            "the baseline again before adopting it."
        )

    existing = session.scalar(
        select(BaselineSource).where(BaselineSource.project_id == project.id)
    )
    if existing is not None:
        if existing.idempotency_key != idempotency_key:
            raise BaselineAdoptionRefused(
                f"{project.slug} already adopted a baseline; replacing it is a "
                "later record change, not another initial adoption"
            )
        # A replay of the same adoption writes nothing at all — not a second
        # Document registration, and not a second attributable confirmation.
        return BaselineAdoptionResult(
            revision_id=existing.revision_id,
            baseline_source_id=existing.id,
            document_id=existing.document_id,
            adoption_id=_replayed_adoption_id(session, project.id),
            fact_ids=_adopted_fact_ids(session, existing.revision_id),
            created=False,
        )

    try:
        confirmation = confirm_intake(
            session,
            project=project,
            sha256=preview.content_sha256,
            filename=preview.filename,
            doc_type=BASELINE_DOC_TYPE,
            binding_fingerprint=preview.intake_binding_fingerprint,
            principal=actor,
            images_dir=images_dir,
        )
    except IntakeConflict as exc:
        raise StaleBaselinePreview(str(exc)) from exc
    document_id = confirmation.document_id

    fact_ids = _capture_baseline_facts(
        session, project=project, document_id=document_id, preview=preview, actor=actor
    )
    outcome = session.scalar(
        select(
            func.adopt_project_record_baseline(
                project.id,
                actor.subject,
                idempotency_key,
                _jsonb(_baseline_payload(preview, document_id)),
                _jsonb([item.as_payload() for item in preview.rows]),
                _jsonb(
                    [
                        preview.output_template.as_payload(),
                        preview.field_mapping.as_payload(),
                    ]
                ),
                cast(bindparam(None, list(fact_ids)), ARRAY(BigInteger)),
            )
        )
    )
    revision_id = int(outcome["revision_id"])
    registered_mapping = effective_baseline_formats(session, project.id)[
        "field_mapping"
    ]
    _store_declaration(
        session,
        project_id=project.id,
        format_id=registered_mapping.id,
        manifest=preview.field_mapping_manifest,
    )
    adoption = adopt_project_baseline(
        session,
        project_id=project.id,
        adopted_by_principal=actor.subject,
        baseline_source_sha256=preview.content_sha256,
        importer_identity=IMPORTER_IDENTITY,
        importer_version=IMPORTER_VERSION,
        idempotency_key=idempotency_key,
        revision_id=revision_id,
    )
    audit.record(
        session,
        principal=actor,
        action=audit.ADOPT_BASELINE,
        entity_type=audit.PROJECT,
        entity_id=project.id,
        after={
            "revision_id": revision_id,
            "baseline_source_id": int(outcome["baseline_source_id"]),
            "content_sha256": preview.content_sha256,
            "preview_fingerprint": preview.binding_fingerprint,
            "adopted_rows": len(preview.adopted_rows),
            "excluded_rows": len(preview.excluded_rows),
            "adopted_values": len(fact_ids),
        },
    )
    session.flush()
    return BaselineAdoptionResult(
        revision_id=revision_id,
        baseline_source_id=int(outcome["baseline_source_id"]),
        document_id=document_id,
        adoption_id=adoption.id,
        fact_ids=fact_ids,
        created=bool(outcome["created"]),
    )


def register_baseline_format(
    session: Session,
    *,
    project_id: int,
    identity: FormatIdentity,
    principal: HumanPrincipal,
    idempotency_key: str,
    manifest: FieldMappingManifest | None = None,
    template_bytes: bytes | None = None,
    template_suffix: str = ".xlsx",
    retained_at: datetime | None = None,
    store: ObjectStore | None = None,
) -> BaselineFormat:
    """Register a replacement output template or mapping revision, on its own act.

    It supersedes the effective registration of the same kind, writes no
    accepted value, and is never a second Adopt Baseline.

    A field mapping is registered as a **declared manifest** and nothing else
    (#597): Corridor operations may construct and technically validate one, and
    where it changes what a mapped column means, a person holding the
    project-coordination designation must approve it.  So a mapping whose
    digest differs from the one in force is refused unless this actor holds
    that designation on this project — the registration is the approval, and an
    approval nobody was designated for is not one.
    """

    actor = require_human_principal(principal)
    if identity.kind not in ("output_template", "field_mapping"):
        raise BaselineAdoptionRefused(f"unknown format kind {identity.kind!r}")
    if not idempotency_key.strip():
        raise BaselineAdoptionRefused("a format registration needs an idempotency key")
    if identity.kind == "field_mapping":
        _refuse_unapproved_mapping_revision(
            session,
            project_id=project_id,
            identity=identity,
            manifest=manifest,
            actor=actor,
        )
    retained: tuple[str, int] | None = None
    if identity.kind == "output_template":
        # Before the registration, never after it, and never on trust: this is
        # what makes an output template's bytes something a later preparation
        # can retrieve rather than a digest nothing kept (#690).
        retained = _retain_output_template(
            identity,
            template_bytes,
            suffix=template_suffix,
            store=store,
        )
    outcome = session.scalar(
        select(
            func.register_baseline_format(
                project_id,
                identity.kind,
                identity.identity,
                identity.version,
                identity.content_sha256,
                actor.subject,
                idempotency_key,
            )
        )
    )
    session.expire_all()
    registered = session.get_one(BaselineFormat, int(outcome["format_id"]))
    if retained is not None:
        _bind_output_template_object(
            session,
            registration=registered,
            storage_key=retained[0],
            byte_count=retained[1],
            suffix=template_suffix,
            actor=actor,
            retained_at=retained_at,
        )
    if identity.kind == "field_mapping":
        # `manifest` is not None here: `_refuse_unapproved_mapping_revision`
        # refuses a field mapping registered without one.
        _store_declaration(
            session,
            project_id=project_id,
            format_id=registered.id,
            manifest=manifest,
        )
    return registered


def _retain_output_template(
    identity: FormatIdentity,
    template_bytes: bytes | None,
    *,
    suffix: str,
    store: ObjectStore | None,
) -> tuple[str, int]:
    """Retain the exact bytes an output template is registered over.

    **An output template is not registered until its exact bytes have first
    been retained and verified against the registration digest** (#690). The
    gap this closes is narrow and was real: initial adoption happens to work
    only because the adopted workbook is staged and registered as a Document
    before it becomes the as-adopted template, so a replacement registered
    here could name a digest whose bytes nobody kept. Preparation would then
    have no template to render the one mandatory artifact through, and storage
    reconciliation -- which derives its expected objects from Documents,
    Processing Artifacts, page renders and token layers -- would not have
    noticed either.

    No local path, filename, "newest workbook", customer-system refetch or
    arbitrary caller bytes may substitute, which is why the only thing
    accepted here is bytes that hash to the digest being registered.
    """

    if template_bytes is None:
        raise BaselineAdoptionRefused(
            "an output template is registered over its exact bytes; supply "
            "them so they can be retained and verified against the "
            "registration digest"
        )
    digest = digest_bytes(template_bytes)
    if digest != identity.content_sha256:
        raise BaselineAdoptionRefused(
            "the template bytes offered hash to "
            f"{digest} and the registration names {identity.content_sha256}; "
            "these are not the same content"
        )
    key = content_key(digest, suffix)
    try:
        (store or content_store()).put(key, template_bytes, sha256=digest)
    except StorageError as exc:
        raise BaselineAdoptionRefused(
            f"the output template bytes could not be retained: {exc}"
        ) from exc
    return (key, len(template_bytes))


def _bind_output_template_object(
    session: Session,
    *,
    registration: BaselineFormat,
    storage_key: str,
    byte_count: int,
    suffix: str,
    actor: HumanPrincipal,
    retained_at: datetime | None,
) -> None:
    """Record the storage binding the registration owns.

    Keyed by the registration, and carrying its identity, version and digest
    through a composite foreign key, so a binding that names different content
    from what was registered is unrepresentable rather than merely unlikely.
    """

    if session.get(BaselineFormatObject, int(registration.id)) is not None:
        return
    session.add(
        BaselineFormatObject(
            format_id=int(registration.id),
            project_id=int(registration.project_id),
            format_identity=registration.format_identity,
            format_version=registration.format_version,
            content_sha256=registration.content_sha256,
            storage_key=storage_key,
            byte_count=int(byte_count),
            file_suffix=suffix,
            retained_by_principal=actor.subject,
            retained_at=retained_at or registration.registered_at,
        )
    )
    session.flush()


def _store_declaration(
    session: Session,
    *,
    project_id: int,
    format_id: int,
    manifest: FieldMappingManifest,
) -> None:
    """Record what this mapping revision declares, not only that it exists.

    The command re-derives the digest over these exact bytes and refuses them
    unless it is the digest the registration already records, so a receipt can
    never name a revision whose stored declaration is a different one (#610).
    """

    session.scalar(
        select(
            func.attach_baseline_format_manifest(
                project_id, format_id, manifest.declaration_json
            )
        )
    )
    session.expire_all()


def _refuse_unapproved_mapping_revision(
    session: Session,
    *,
    project_id: int,
    identity: FormatIdentity,
    manifest: FieldMappingManifest | None,
    actor: HumanPrincipal,
) -> None:
    """A mapping revision is a proved declaration a designated person approves."""

    if manifest is None:
        raise BaselineAdoptionRefused(
            "a field mapping is registered as a declared mapping revision; "
            "a digest with no manifest behind it records nothing about what "
            "the customer's columns mean"
        )
    try:
        prove_manifest(manifest)
    except MappingManifestRefused as exc:
        raise BaselineAdoptionRefused(
            f"this mapping revision does not prove out: {exc}"
        ) from exc
    if (
        manifest.content_sha256 != identity.content_sha256
        or manifest.identity != identity.identity
        or manifest.version != identity.version
    ):
        raise BaselineAdoptionRefused(
            "the registered identity is not this manifest's; a mapping "
            "revision is named and digested by the declaration it records, so "
            "a receipt can never name a revision the manifest does not"
        )
    effective = effective_baseline_formats(session, project_id).get("field_mapping")
    if effective is None or effective.content_sha256 == identity.content_sha256:
        # Nothing in force to change, or the same declaration registered again
        # under a new key: an idempotent act, not a semantic one.
        return
    membership = resolve_membership(session, actor.subject, project_id)
    if membership is None or not membership.has(COORDINATION):
        raise BaselineAdoptionRefused(
            f"{manifest.revision} changes what this project's mapped columns "
            "mean, which is a material semantic change. A person holding the "
            "project-coordination designation approves it; Corridor operations "
            "constructs and validates it and cannot approve it."
        )


def adopted_baseline_source(
    session: Session, project_id: int
) -> BaselineSource | None:
    """The accepted data-baseline identity, or ``None`` for a legacy project."""

    return session.scalar(
        select(BaselineSource).where(BaselineSource.project_id == project_id)
    )


def adopted_source_rows(
    session: Session, project_id: int
) -> tuple[BaselineSourceRow, ...]:
    """Every adopted source row's identity, in worksheet order."""

    return tuple(
        session.scalars(
            select(BaselineSourceRow)
            .where(BaselineSourceRow.project_id == project_id)
            .order_by(BaselineSourceRow.row_number, BaselineSourceRow.id)
        ).all()
    )


def effective_baseline_formats(
    session: Session, project_id: int
) -> dict[str, BaselineFormat]:
    """The output-template and field-mapping identities in force right now."""

    return effective_baseline_formats_by_project(session, (project_id,)).get(
        project_id, {}
    )


def effective_baseline_formats_by_project(
    session: Session, project_ids: Sequence[int]
) -> dict[int, dict[str, BaselineFormat]]:
    """The same reading for several projects in one statement (#537).

    A cross-project reading may not ask this once per project, and it may not
    answer it by a second rule either, so the single-project reader above is
    this function over one project.
    """

    ids = tuple(dict.fromkeys(int(value) for value in project_ids))
    found: dict[int, dict[str, BaselineFormat]] = {
        project_id: {} for project_id in ids
    }
    if not ids:
        return found
    for row in session.scalars(
        select(BaselineFormat).where(
            BaselineFormat.project_id.in_(ids),
            BaselineFormat.superseded_by.is_(None),
        )
    ).all():
        found[int(row.project_id)][row.format_kind] = row
    return found


def stored_mapping_revision(
    session: Session, *, identity: str, version: str, content_sha256: str
) -> FieldMappingManifest | None:
    """The full declaration one registered mapping revision records (#610).

    ``None`` where nothing stored it — a registration written before this
    storage existed names a revision Corridor cannot itself resolve, and
    saying so is the point. It is never read as an empty manifest.
    """

    stored = session.scalar(
        select(BaselineFormatManifest)
        .where(
            BaselineFormatManifest.format_identity == identity,
            BaselineFormatManifest.format_version == version,
            BaselineFormatManifest.content_sha256 == content_sha256,
        )
        .order_by(BaselineFormatManifest.format_id)
        .limit(1)
    )
    if stored is None:
        return None
    return manifest_from_declaration(stored.declaration)


def effective_field_mapping_manifest(
    session: Session, project_id: int
) -> FieldMappingManifest | None:
    """The mapping revision in force, read back from stored state alone.

    ``None`` both where no field mapping is registered and where the one that
    is stores no declaration; the caller states which, because the two are
    different facts about the same project.
    """

    registration = effective_baseline_formats(session, project_id).get("field_mapping")
    if registration is None:
        return None
    stored = session.get(BaselineFormatManifest, registration.id)
    if stored is None:
        return None
    return manifest_from_declaration(stored.declaration)


# --- The coordinator reading ------------------------------------------------


def _coordinator_questions(
    operations: OperationsReading,
    rows: tuple[PreviewRow, ...],
    *,
    customer: str,
    source_identity: str,
) -> tuple[CoordinatorQuestion, ...]:
    """Only what a project person can decide, never how the file is shaped."""

    adopted = [item for item in rows if not item.row.excluded]
    questions = [
        CoordinatorQuestion(
            kind="adopted_scope",
            subject=f"{customer}: {source_identity}",
            detail=(
                f"{len(adopted)} rows of {operations.adopted_sheet!r} become the "
                f"accepted record; {len(rows) - len(adopted)} are proposed for "
                "exclusion."
            ),
        )
    ]

    by_identity: dict[str, list[PreviewRow]] = {}
    for item in adopted:
        if item.row.business_identity:
            by_identity.setdefault(item.row.business_identity, []).append(item)
    for identity, members in sorted(by_identity.items()):
        if len(members) < 2:
            continue
        descriptors = {
            tuple(item.row.value(field) for field in _FACILITY_DESCRIPTORS)
            for item in members
        }
        keys = tuple(item.row.source_row_key for item in members)
        if len(descriptors) > 1:
            questions.append(
                CoordinatorQuestion(
                    kind="likely_distinct_facilities",
                    subject=identity,
                    detail=(
                        f"{len(members)} rows share {identity!r} but describe "
                        "different owners, types, or locations. They are kept as "
                        "separate subjects; confirm whether they really are "
                        "distinct facilities."
                    ),
                    source_rows=keys,
                )
            )
        else:
            questions.append(
                CoordinatorQuestion(
                    kind="duplicate_business_identity",
                    subject=identity,
                    detail=(
                        f"{len(members)} rows repeat {identity!r} with the same "
                        "owner, type, and location. They are kept as separate "
                        "subjects rather than merged."
                    ),
                    source_rows=keys,
                )
            )

    for item in adopted:
        for value in item.row.unsupported:
            if not value.material:
                continue
            questions.append(
                CoordinatorQuestion(
                    kind="unmappable_material_value",
                    subject=f"{value.field} {value.exact_text!r}",
                    detail=(
                        f"{item.row.source_row_key} states {value.exact_text!r} for "
                        f"{value.field}, which no released reading can record as a "
                        "value. It is retained as source text and left out of the "
                        "accepted value."
                    ),
                    source_rows=(item.row.source_row_key,),
                )
            )

    for item in rows:
        if item.row.excluded:
            questions.append(
                CoordinatorQuestion(
                    kind="proposed_exclusion",
                    subject=item.row.source_row_key,
                    detail=_exclusion_detail(item.row),
                    source_rows=(item.row.source_row_key,),
                )
            )

    bases = sorted(
        {
            item.row.value("baseline")
            for item in adopted
            if item.row.value("baseline")
        }
    )
    if len(bases) > 1:
        questions.append(
            CoordinatorQuestion(
                kind="conflicting_plan_basis",
                subject=", ".join(bases),
                detail=(
                    "The adopted rows measure stationing from more than one "
                    "station origin, so distances are not comparable across them "
                    "until one basis is confirmed."
                ),
                source_rows=tuple(
                    item.row.source_row_key for item in adopted if item.row.value("baseline")
                ),
            )
        )
    return tuple(questions)


_EXCLUSION_SENTENCES = {
    "retired_row": (
        "The form itself retires this number: the row carries a retirement "
        "phrase and no other content."
    ),
    "missing_required_fields": (
        "The row names neither a conflict identifier nor a utility owner that "
        "the record could be kept under."
    ),
    "insufficient_mapped_fields": (
        "The row populates too few recognized columns to describe a conflict."
    ),
}


def _exclusion_detail(row: BaselineRow) -> str:
    return _EXCLUSION_SENTENCES.get(
        row.exclusion_reason or "", "The row is proposed for exclusion."
    )


def assign_record_subjects(rows: tuple[BaselineRow, ...]) -> tuple[PreviewRow, ...]:
    """Give each adopted row its own Project Record subject identity.

    Public because a later revision of the same workbook resolves its rows by
    the same rule (`later_revision`, #606); two identity rules over one file
    would let a value be adopted under one reading and compared under another.

    Source-row identity and record-subject identity stay distinct, and a
    repeated business identity never collapses two rows into one subject: the
    second occurrence takes a suffixed subject and the coordinator is asked
    about it.  The alternative — one subject per business identity — is the
    exact silent merge ADR-0030 and this ticket both forbid.
    """

    seen: dict[str, int] = {}
    assigned: list[PreviewRow] = []
    for row in rows:
        if row.excluded:
            assigned.append(PreviewRow(row=row, record_subject_key=None))
            continue
        base = row.business_identity or row.source_row_key
        seen[base] = seen.get(base, 0) + 1
        suffix = "" if seen[base] == 1 else f"#{seen[base]}"
        assigned.append(PreviewRow(row=row, record_subject_key=f"{base}{suffix}"))
    return tuple(assigned)


# --- Binding, capture, and payloads ----------------------------------------


def _with_fingerprint(preview: BaselinePreview) -> BaselinePreview:
    payload = {
        "project_id": preview.project_id,
        "customer": preview.customer,
        "source_identity": preview.source_identity,
        "source_kind": preview.source_kind,
        "filename": preview.filename,
        "content_sha256": preview.content_sha256,
        "byte_size": preview.byte_size,
        "importer_identity": IMPORTER_IDENTITY,
        "importer_version": IMPORTER_VERSION,
        "adopted_sheet": preview.operations.adopted_sheet,
        "header_row_number": preview.operations.header_row_number,
        "column_mapping": [
            [column.column, column.heading, column.field]
            for column in preview.operations.column_mapping
        ],
        "unknown_columns": [
            [column.column, column.heading, column.populated_cells]
            for column in preview.operations.unknown_columns
        ],
        "output_template": preview.output_template.as_payload(),
        "field_mapping": preview.field_mapping.as_payload(),
        "questions": [item.as_payload() for item in preview.questions],
        "rows": [
            {
                **item.as_payload(),
                "values": [
                    [value.field, value.cell_range, value.exact_text]
                    for value in item.row.values
                ],
                "unsupported": [
                    [value.field, value.cell_range, value.exact_text]
                    for value in item.row.unsupported
                ],
                "retained": [
                    [value.heading, value.cell_range, value.exact_text]
                    for value in item.row.retained
                ],
            }
            for item in preview.rows
        ],
    }
    fingerprint = sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return _replace(preview, binding_fingerprint=fingerprint)


def _replace(preview: BaselinePreview, **changes: Any) -> BaselinePreview:
    values = {
        name: getattr(preview, name)
        for name in BaselinePreview.__dataclass_fields__
    }
    values.update(changes)
    return BaselinePreview(**values)


def _current_preview(session: Session, preview: BaselinePreview) -> BaselinePreview:
    """Recompute the preview from the bytes as they are now."""

    project = session.get_one(Project, preview.project_id)
    staged_path = staged_file(preview.content_sha256)
    if staged_path is None:
        raise StaleBaselinePreview(
            "The previewed bytes are no longer staged. Upload the source again."
        )
    if sha256(Path(staged_path).read_bytes()).hexdigest() != preview.content_sha256:
        raise StaleBaselinePreview(
            "The staged bytes changed since the preview. Upload the source again."
        )
    return preview_baseline_adoption(
        session,
        project=project,
        staged=StagedSource(
            sha256=preview.content_sha256,
            size_bytes=preview.byte_size,
            suffix=Path(preview.filename).suffix.lower(),
            filename=preview.filename,
            stored_path=Path(staged_path),
        ),
        customer=preview.customer,
        source_identity=preview.source_identity,
        source_kind=preview.source_kind,
        output_template=preview.output_template,
        field_mapping=preview.field_mapping_declaration,
    )


def format_identity_of(manifest: FieldMappingManifest) -> FormatIdentity:
    """The registerable identity one declared mapping revision amounts to.

    Public because it is also the check a later render makes: #495 renders the
    accepted record *through* a registered field mapping, and the way it
    refuses a successor template is by finding that the manifest offered with
    the template is not the one a person approved.

    The digest is taken over the **declaration**, never over the template.
    That is the whole correction #597 makes: a digest computed from a
    template's own columns cannot see a form that stopped carrying `Start
    Station` and `End Station` as two values and started carrying one combined
    range in the same two columns, because nothing about that change is
    printed.  A declaration says it, so the digest moves when the meaning does.
    """

    return FormatIdentity(
        kind="field_mapping",
        identity=manifest.identity,
        version=manifest.version,
        content_sha256=manifest.content_sha256,
    )


def _baseline_payload(preview: BaselinePreview, document_id: int) -> dict[str, Any]:
    operations = preview.operations
    return {
        "document_id": document_id,
        "content_sha256": preview.content_sha256,
        "byte_size": preview.byte_size,
        "filename": preview.filename,
        "source_identity": preview.source_identity,
        "customer": preview.customer,
        "source_kind": preview.source_kind,
        "preview_fingerprint": preview.binding_fingerprint,
        "importer_identity": IMPORTER_IDENTITY,
        "importer_version": IMPORTER_VERSION,
        "worksheet_scope": {
            "adopted_sheet": operations.adopted_sheet,
            "header_row_number": operations.header_row_number,
            "source_row_key_rule": operations.source_row_key_rule,
            "worksheets": [
                {
                    "name": sheet.name,
                    "position": sheet.position,
                    "state": sheet.state,
                    "disposition": sheet.disposition,
                    "reason": sheet.reason,
                }
                for sheet in operations.worksheets
            ],
        },
        "unknown_columns": [
            {
                "column": column.column,
                "heading": column.heading,
                "populated_cells": column.populated_cells,
            }
            for column in operations.unknown_columns
        ],
        "coordinator_questions": [item.as_payload() for item in preview.questions],
        "operations_summary": {
            "parser": operations.parser,
            "formula_cells": list(operations.formula_cells),
            "hidden_content": list(operations.hidden_content),
            "controlled_vocabularies": [
                {
                    "column": item.column,
                    "heading": item.heading,
                    "allowed_values": list(item.allowed_values),
                    "checked": item.checked,
                    "out_of_vocabulary": list(item.out_of_vocabulary),
                    "reference": item.reference,
                }
                for item in operations.controlled_vocabularies
            ],
            "unsupported_values": [
                {
                    "field": item.field,
                    "cell_range": item.cell_range,
                    "reason": item.reason,
                    "material": item.material,
                }
                for item in operations.unsupported_values
            ],
            "diagnostics": [
                {"code": item.code, "detail": item.detail, "locator": item.locator}
                for item in operations.diagnostics
            ],
            "round_trip": {
                "rows_checked": operations.round_trip.rows_checked,
                "values_checked": operations.round_trip.values_checked,
                "mismatches": list(operations.round_trip.mismatches),
            },
        },
    }


def _capture_baseline_facts(
    session: Session,
    *,
    project: Project,
    document_id: int,
    preview: BaselinePreview,
    actor: HumanPrincipal,
) -> tuple[int, ...]:
    """Capture one Source Fact per adopted value, with its Support Assessment.

    Capture and decision stay two acts even inside one Save (ADR-0076): these
    Facts are statements about the workbook, and only the adoption command that
    follows makes them the accepted record.
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
    assessed_at = datetime.now(timezone.utc)
    fact_ids: list[int] = []
    for item in preview.adopted_rows:
        for value in item.row.values:
            segment = segments.get((value.sheet_name, value.cell_range))
            if segment is None:
                raise BaselineAdoptionRefused(
                    f"the adopted source has no Source Segment at "
                    f"{value.sheet_name}!{value.cell_range}"
                )
            materialized = materialize_segment_value(session, value.field, segment)
            fact = append_fact(
                session,
                project_id=project.id,
                document_id=document_id,
                extraction_run_id=run.id,
                subject_kind="source_row",
                subject_key=item.row.source_row_key,
                recorded_by=f"importer:{IMPORTER_VERSION}",
                content_sha256=_baseline_fact_digest(
                    preview, item.row.source_row_key, value.field, segment
                ),
                value=materialized,
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
    session.flush()
    return tuple(fact_ids)


def _baseline_fact_digest(
    preview: BaselinePreview, source_row_key: str, field: str, segment: SourceSegment
) -> str:
    return sha256(
        json.dumps(
            {
                "baseline": preview.content_sha256,
                "importer_version": IMPORTER_VERSION,
                "source_row_key": source_row_key,
                "field": field,
                "segment": segment.content_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _refuse_nonempty_project_record(session: Session, project: Project) -> None:
    """Refuse a project whose accepted record adoption would overwrite.

    Both halves of the accepted record are counted, and the same two the
    adoption command checks: the spine's effective decisions, and the legacy
    Constraint Records the frozen admission paths still write. Checking only
    one of them would let the other be silently adopted over.
    """

    decided = session.scalar(
        select(func.count())
        .select_from(FactDecision)
        .where(
            FactDecision.project_id == project.id,
            FactDecision.superseded_by.is_(None),
        )
    ) + session.scalar(
        select(func.count())
        .select_from(Dependency)
        .where(Dependency.project_id == project.id)
    )
    if decided:
        raise BaselineAdoptionRefused(
            f"{project.slug} already holds {decided} accepted record decisions. "
            "Adopting a baseline over an existing Project Record needs an "
            "explicit migration or reconciliation, or a fresh environment."
        )


def _replayed_adoption_id(session: Session, project_id: int) -> int:
    return int(
        session.scalar(
            select(BaselineAdoption.id).where(
                BaselineAdoption.project_id == project_id
            )
        )
    )


def _adopted_fact_ids(session: Session, revision_id: int) -> tuple[int, ...]:
    return tuple(
        session.scalars(
            select(FactDecision.fact_id)
            .where(FactDecision.revision_id == revision_id)
            .order_by(FactDecision.id)
        ).all()
    )


def _jsonb(value: object):
    return cast(bindparam(None, json.dumps(value)), JSONB)
