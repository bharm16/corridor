"""Synthetic UCM workbooks for the later-revision path, including the burst (#606).

Every workbook here is synthetic. It exercises the importer and the comparison,
never a claim about a real customer form (ADR-0046).

``burst_workbooks`` is the fixture #527 needs to exercise batching: an adopted
baseline of forty-four rows and a later revision that changes one non-date,
non-organization field on every one of them, so the reading has forty-four
compatible changes of one source revision to place in one item.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from openpyxl import Workbook

from corridor.baseline_adoption import adopt_baseline, preview_baseline_adoption
from corridor.connectors.pull_connector import SourceEnvelope
from corridor.field_mapping_manifest import (
    DEMO_EXTERNAL_REFERENCES,
    FieldMappingManifest,
    MappingDeclaration,
)
from corridor.models import Document, Project
from corridor.principals import HumanPrincipal
from corridor.push_intake import (
    PushCredential,
    PushPayload,
    accept_delivery,
    bind_credential,
    register_push_credential,
)
from corridor.source_delivery import require_stored_envelope
from corridor.source_intake import (
    StagedSource,
    confirm_intake,
    preview_intake,
    validate_and_stage,
)
from corridor.source_revision_declaration import (
    COMPLETE_ENUMERATION,
    PARTIAL_EXPORT,
    REPLACES,
    RevisionDeclaration,
    declare_source_revision,
    revision_intake_reading,
)


PRINCIPAL = HumanPrincipal("local:coordinator")
CUSTOMER = "Lone Star Transit Authority"
DEMO = MappingDeclaration(external_references=DEMO_EXTERNAL_REFERENCES)

HEADINGS = [
    "Utility Conflict ID",
    "Utility Owner",
    "Utility Type",
    "Size",
    "Material",
    "Station Origin",
    "Start Station",
    "End Station",
    "Resolution Strategy Selected (from Resolution Alternatives)",
    "Promised For",
    "Action Due Date",
    "Comment",
    "UCM Record ID",
    "Record URL",
]

# The adopted baseline: three ordinary conflicts, at worksheet rows 3, 4 and 5.
BASELINE_ROWS = [
    ["UC-1", "CenterPoint Energy", "Electric", "12 in", "Steel", "SR-BL",
     "1149+00", "1150+00", "Relocate", "2026-03-01", "2026-02-01",
     "pole at station", "UCM-1001", "https://ucm.example/records/1001"],
    ["UC-2", "City of Austin", "Water", "8 in", "PVC", "SR-BL",
     "1160+00", "1161+00", "Adjust", "2026-04-01", "2026-03-01",
     "", "UCM-1002", ""],
    ["UC-3", "Oncor", "Electric", "4 in", "Copper", "SR-BL",
     "1180+00", "1181+00", "Relocate", "2026-05-01", "", "", "UCM-1003", ""],
]


def workbook_bytes(
    path: Path, rows: Sequence[Sequence[object]], *, headings: Sequence[str] | None = None
) -> bytes:
    """One synthetic UCM workbook, in the published form's own column order."""

    book = Workbook()
    sheet = book.active
    sheet.title = "Utility Conflicts"
    sheet.append(["Utility Conflict Management (UCM) — Utility Conflicts"])
    sheet.append(list(HEADINGS if headings is None else headings))
    for row in rows:
        sheet.append(list(row))
    book.save(path)
    return path.read_bytes()


def adopt(
    session,
    project: Project,
    body: bytes,
    tmp_path: Path,
    *,
    source_identity: str = "UCM workbook revision C",
    declaration: MappingDeclaration | None = None,
) -> tuple[int, FieldMappingManifest]:
    """Adopt one baseline and return its revision and its mapping revision.

    The manifest is the declaration the project registered, not something
    rebuilt from a later file: its digest is bound to the populated example it
    was proved against, which is exactly why a later revision must be read
    through the registered one rather than through its own.
    """

    staged = validate_and_stage(body, "ucm.xlsx")
    preview = preview_baseline_adoption(
        session,
        project=project,
        staged=staged,
        customer=CUSTOMER,
        source_identity=source_identity,
        field_mapping=DEMO if declaration is None else declaration,
    )
    result = adopt_baseline(
        session,
        preview=preview,
        principal=PRINCIPAL,
        idempotency_key=f"adopt-{project.slug}",
        images_dir=tmp_path / "images",
    )
    return result.revision_id, preview.field_mapping_manifest


def declared(
    *,
    is_complete_enumerative_source: bool = False,
    row_accounting_sealed: bool = False,
    principal: HumanPrincipal = PRINCIPAL,
) -> RevisionDeclaration:
    """The coordinator's declaration a fixture captures one revision under.

    The two flags are the facts confirmation establishes (#825) and they stay
    separate here for the same reason ``capture_later_revision`` keeps them
    separate: a test that fixes what an unsealed reading may propose has to be
    able to declare one without the other, which is a combination the product
    screen cannot produce but the reader's contract still has to hold.
    """

    return RevisionDeclaration(
        declared_by=principal,
        is_complete_enumerative_source=is_complete_enumerative_source,
        row_accounting_sealed=row_accounting_sealed,
    )


def deliver(
    session,
    project: Project,
    body: bytes,
    *,
    filename: str = "ucm-later.xlsx",
    external_identity: str = "UCM workbook revision D",
    material: str | None = None,
) -> tuple[StagedSource, SourceEnvelope]:
    """Take delivery of one later revision through the manual push ingress (#511)."""

    secret = material or f"secret-{project.slug}"
    register_push_credential(
        session,
        customer=CUSTOMER,
        project=project,
        channel="webhook",
        material=secret,
    )
    binding = bind_credential(session, PushCredential("webhook", secret))
    receipt = accept_delivery(
        session,
        binding,
        PushPayload(
            body=body,
            filename=filename,
            transport_delivery_id=external_identity,
        ),
    )
    return validate_and_stage(body, filename), receipt.envelope


def register_delivered_revision(
    session,
    project: Project,
    body: bytes,
    tmp_path: Path,
    *,
    completeness: str,
    revision_identity: str = "UCM workbook revision D",
    revision_relationship: str = REPLACES,
    related_revision_identity: str = "",
    filename: str = "ucm-later.xlsx",
    external_identity: str = "UCM workbook revision D",
):
    """Deliver, confirm and declare one later revision the way the product does.

    The whole of what a coordinator's confirmation writes, without the HTTP:
    the delivery is taken, the exact bytes are registered as this project's
    Document, and the declaration that says which revision they are is recorded
    beside the delivery. The ordinary processing pass then has everything it
    routes on, which is the point of driving it this way rather than calling
    the reader directly.
    """

    staged, envelope = deliver(
        session,
        project,
        body,
        filename=filename,
        external_identity=external_identity,
        # One credential per delivery: `register_push_credential` refuses to
        # re-point a live credential at anything, so a second delivery in one
        # test presents its own rather than re-registering the first.
        material=f"secret-{project.slug}-{external_identity}",
    )
    delivery = require_stored_envelope(session, envelope)
    reading = revision_intake_reading(session, project, staged, "matrix")
    preview = preview_intake(session, project, staged, "matrix")
    confirmation = confirm_intake(
        session,
        project=project,
        sha256=staged.sha256,
        filename=staged.filename,
        doc_type="matrix",
        binding_fingerprint=preview.binding_fingerprint,
        principal=PRINCIPAL,
        images_dir=tmp_path / "images",
        source_delivery_id=int(delivery.id),
    )
    if reading.applies and reading.held is None:
        declare_source_revision(
            session,
            project=project,
            delivery=delivery,
            reading=reading,
            principal=PRINCIPAL,
            revision_identity=revision_identity,
            completeness=completeness,
            revision_relationship=revision_relationship,
            related_revision_identity=related_revision_identity,
        )
    return session.get_one(Document, confirmation.document_id), reading


# --- the burst -------------------------------------------------------------

# Forty-four rows is deliberately more than the forty #527 must batch, so the
# fixture keeps proving batching if a row is later excluded for another reason.
BURST_ROW_COUNT = 44


def _burst_row(index: int, size: str) -> list[object]:
    station = 1000 + index
    return [
        f"UC-{index:03d}",
        f"Utility Owner {index:03d}",
        "Electric" if index % 2 else "Water",
        size,
        "Steel" if index % 3 else "PVC",
        "SR-BL",
        f"{station}+00",
        f"{station}+50",
        "Relocate" if index % 2 else "Adjust",
        "2026-03-01",
        "2026-02-01",
        "",
        f"UCM-{index:04d}",
        "",
    ]


def burst_workbooks(tmp_path: Path) -> tuple[bytes, bytes, int]:
    """A baseline and a later revision differing by one routine field per row.

    ``Size`` is chosen on purpose: it is neither a date nor an organization
    field, so every one of the changes belongs to the same consequence band and
    none of them is held out of the batch. Each row's size moves by one inch, so
    every row differs and no row differs by accident. The count is returned so a
    test asserts the number the fixture actually carries rather than a literal
    that can drift away from it.
    """

    baseline = [
        _burst_row(index, f"{6 + index % 7} in")
        for index in range(1, BURST_ROW_COUNT + 1)
    ]
    later = [
        _burst_row(index, f"{7 + index % 7} in")
        for index in range(1, BURST_ROW_COUNT + 1)
    ]
    return (
        workbook_bytes(tmp_path / "burst-baseline.xlsx", baseline),
        workbook_bytes(tmp_path / "burst-later.xlsx", later),
        BURST_ROW_COUNT,
    )
