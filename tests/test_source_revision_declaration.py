"""Confirmation declares what the bytes cannot, and the pass routes on it (#825).

The dedicated later-revision tests (``test_later_revision``) fix what the
reader proposes for a retired row, a missing field, an ambiguous conflict
number and an absent row. They call the reader directly, so passing them proved
the reader and nothing about the path a coordinator's upload actually takes.

These tests drive that path: a delivery is taken, a person confirms it and
declares which revision it is, and the ordinary processing pass chooses the
reader. The assertions about what is proposed are deliberately the same ones
the dedicated tests make, named beside each, so the two cannot drift into
agreeing on the reader while disagreeing about what production runs.

Every workbook is synthetic (``later_revision_support``). No test reads a
clock.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from corridor import processing_holds
from corridor import audit
from corridor.config import settings
from corridor.extract_project import extract_project
from corridor.later_revision import (
    ABSENT_FROM_REVISION,
    REVISION_CAPTURE_SERVICE_IDENTITY,
    WITHHELD_UNSEALED,
)
from corridor.models import (
    AuditLog,
    Document,
    ExtractionRun,
    Fact,
    ProposedDelta,
    SourceRevisionDeclaration,
    SourceSegment,
)
from corridor.pipeline import CAPTURED_READING, SOURCE_FACTS, extract_any, extraction_route
from corridor.source_delivery import DeliveryBinding, DeliveryObservation, take_delivery
from corridor.source_revision_declaration import (
    ADDITIONAL_RENDITION,
    COMPLETE_ENUMERATION,
    COMPLETENESS,
    CORRIDOR_OPERATIONS,
    FROM_DECLARATION,
    FROM_REGISTRATION,
    FROM_SOURCE_METADATA,
    PARTIAL_EXPORT,
    REVISION_IDENTITY,
    SOURCE_FAMILY,
    SourceRevisionHeld,
    revision_intake_reading,
)

from later_revision_support import (
    BASELINE_ROWS,
    CUSTOMER,
    HEADINGS,
    PRINCIPAL,
    adopt,
    register_delivered_revision,
    workbook_bytes,
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    return tmp_path / "files"


def _changed(rows, index, column, value):
    copied = [list(row) for row in rows]
    copied[index][HEADINGS.index(column)] = value
    return copied


def _deltas(session, project):
    return tuple(
        session.scalars(
            select(ProposedDelta)
            .where(ProposedDelta.project_id == project.id)
            .order_by(ProposedDelta.id)
        ).all()
    )


def _question(reading, field):
    return next(item for item in reading.questions if item.field == field)


# --- what confirmation establishes -----------------------------------------


def test_the_family_and_the_mapping_come_from_what_the_project_registered(
    session, project, tmp_path, store
):
    adopt(session, project, workbook_bytes(tmp_path / "a.xlsx", BASELINE_ROWS), tmp_path)
    body = workbook_bytes(
        tmp_path / "b.xlsx", _changed(BASELINE_ROWS, 0, "Size", "18 in")
    )
    document, reading = register_delivered_revision(
        session, project, body, tmp_path, completeness=COMPLETE_ENUMERATION
    )

    assert reading.applies and reading.held is None
    family = _question(reading, SOURCE_FAMILY)
    assert family.answered and family.source == FROM_REGISTRATION
    assert reading.source_family.startswith("ucm_workbook:")
    declaration = session.scalars(select(SourceRevisionDeclaration)).one()
    assert declaration.source_family == reading.source_family
    assert declaration.uses_registered_mapping is True
    assert declaration.answer_sources_json[SOURCE_FAMILY] == FROM_REGISTRATION
    assert declaration.declared_by_principal == PRINCIPAL.subject
    assert document.source_delivery_id == declaration.delivery_id


def test_completeness_is_asked_and_is_never_defaulted(
    session, project, tmp_path, store
):
    """Nothing registration or the transport holds says whether a file is whole."""

    adopt(session, project, workbook_bytes(tmp_path / "a.xlsx", BASELINE_ROWS), tmp_path)
    body = workbook_bytes(
        tmp_path / "b.xlsx", _changed(BASELINE_ROWS, 0, "Size", "18 in")
    )
    _document, reading = register_delivered_revision(
        session, project, body, tmp_path, completeness=PARTIAL_EXPORT
    )

    completeness = _question(reading, COMPLETENESS)
    assert not completeness.answered and completeness.source == ""
    assert completeness.field in {item.field for item in reading.unanswered}
    declaration = session.scalars(select(SourceRevisionDeclaration)).one()
    assert declaration.completeness == PARTIAL_EXPORT
    assert declaration.answer_sources_json[COMPLETENESS] == FROM_DECLARATION


def test_a_complete_predecessor_does_not_make_the_next_delivery_complete(
    session, project, tmp_path, store
):
    """The one inference the ticket forbids, and a similar filename with it."""

    adopt(session, project, workbook_bytes(tmp_path / "a.xlsx", BASELINE_ROWS), tmp_path)
    register_delivered_revision(
        session,
        project,
        workbook_bytes(tmp_path / "b.xlsx", _changed(BASELINE_ROWS, 0, "Size", "18 in")),
        tmp_path,
        completeness=COMPLETE_ENUMERATION,
        revision_identity="UCM workbook revision D",
        external_identity="UCM workbook revision D",
    )

    # Same project, same filename, same shape: a filtered export of two rows.
    _document, reading = register_delivered_revision(
        session,
        project,
        workbook_bytes(tmp_path / "c.xlsx", BASELINE_ROWS[:2]),
        tmp_path,
        completeness=PARTIAL_EXPORT,
        revision_identity="UCM workbook revision E",
        external_identity="UCM workbook revision E",
    )

    assert not _question(reading, COMPLETENESS).answered
    assert _question(reading, COMPLETENESS).choices


def test_the_revision_identity_defaults_only_from_the_transports_own_version(
    session, project, tmp_path, store
):
    """A pushed digest is Corridor's fallback, not the source's own version."""

    adopt(session, project, workbook_bytes(tmp_path / "a.xlsx", BASELINE_ROWS), tmp_path)
    body = workbook_bytes(
        tmp_path / "b.xlsx", _changed(BASELINE_ROWS, 0, "Size", "18 in")
    )
    _document, pushed = register_delivered_revision(
        session, project, body, tmp_path, completeness=COMPLETE_ENUMERATION
    )
    assert not _question(pushed, REVISION_IDENTITY).answered

    # The same bytes arriving on a pull, where the customer's own system names
    # the version.
    other = workbook_bytes(
        tmp_path / "c.xlsx", _changed(BASELINE_ROWS, 1, "Size", "9 in")
    )
    from corridor.source_intake import validate_and_stage

    staged = validate_and_stage(other, "pulled.xlsx")
    take_delivery(
        session,
        DeliveryBinding(
            customer=CUSTOMER,
            project_id=project.id,
            project_slug=project.slug,
            transport="pull",
            channel="box",
            configuration_identity="fixture-connector",
        ),
        DeliveryObservation(
            external_identity="ucm.xlsx",
            external_version="Revision E",
            content_digest=staged.sha256,
            bytes_reference=str(staged.stored_path),
        ),
        service_identity="fixture-connector",
        run_identity="first-pass",
    )

    reading = revision_intake_reading(session, project, staged, "matrix")

    identity = _question(reading, REVISION_IDENTITY)
    assert identity.answered and identity.answer == "Revision E"
    assert identity.source == FROM_SOURCE_METADATA


def test_a_legacy_project_has_no_later_revision_to_declare(
    session, project, tmp_path, store
):
    """Nothing to be a revision of, so the ordinary confirmation is the whole act."""

    from corridor.source_intake import validate_and_stage

    staged = validate_and_stage(
        workbook_bytes(tmp_path / "b.xlsx", BASELINE_ROWS), "ucm.xlsx"
    )

    reading = revision_intake_reading(session, project, staged, "matrix")

    assert reading.applies is False
    assert reading.questions == () and reading.held is None


def test_a_file_that_is_not_the_registered_mapping_is_held_with_its_owner(
    session, project, tmp_path, store
):
    adopt(session, project, workbook_bytes(tmp_path / "a.xlsx", BASELINE_ROWS), tmp_path)
    renamed = [*HEADINGS]
    renamed[HEADINGS.index("Size")] = "Diameter"
    from corridor.source_intake import validate_and_stage

    staged = validate_and_stage(
        workbook_bytes(tmp_path / "b.xlsx", BASELINE_ROWS, headings=renamed),
        "ucm-later.xlsx",
    )

    reading = revision_intake_reading(session, project, staged, "matrix")

    assert reading.applies and reading.held is not None
    assert reading.held.responsible_party == CORRIDOR_OPERATIONS
    assert "registered" in reading.held.reason
    assert reading.held.next_action
    assert reading.unanswered == ()


# --- what the ordinary processing pass then does ---------------------------


def test_a_declared_later_revision_reaches_the_later_revision_reader(
    session, project, tmp_path, store
):
    """The same proposal `test_only_a_changed_value_becomes_a_proposed_delta` fixes."""

    adopt(session, project, workbook_bytes(tmp_path / "a.xlsx", BASELINE_ROWS), tmp_path)
    document, _reading = register_delivered_revision(
        session,
        project,
        workbook_bytes(tmp_path / "b.xlsx", _changed(BASELINE_ROWS, 0, "Size", "18 in")),
        tmp_path,
        completeness=COMPLETE_ENUMERATION,
    )

    assert extraction_route(document).output == CAPTURED_READING
    reading = extract_any(session, document)

    # Exactly what `test_only_a_changed_value_becomes_a_proposed_delta` fixes
    # for the same change, reached through the pass instead of the reader.
    (delta,) = _deltas(session, project)
    assert (delta.target_subject_identity, delta.target_field, delta.change_type) == (
        "Utility Conflicts!3",
        "size",
        "modify",
    )
    assert delta.accepted_value == "12 in"
    assert delta.proposed_value == "18 in"
    run = session.get_one(ExtractionRun, reading.run.id)
    assert run.outcome == "completed"


def test_a_complete_declaration_proposes_the_absent_row_as_a_removal(
    session, project, tmp_path, store
):
    """`test_a_complete_sealed_revision_proposes_the_absent_row_as_a_removal`."""

    adopt(session, project, workbook_bytes(tmp_path / "a.xlsx", BASELINE_ROWS), tmp_path)
    document, _reading = register_delivered_revision(
        session,
        project,
        workbook_bytes(tmp_path / "b.xlsx", BASELINE_ROWS[:2]),
        tmp_path,
        completeness=COMPLETE_ENUMERATION,
    )

    extract_any(session, document)

    (removal,) = _deltas(session, project)
    assert removal.change_type == "apparent_removal"
    assert removal.target_subject_identity == "Utility Conflicts!5"
    assert removal.accepted_value["utility_id"] == "UC-3"
    assert removal.proposed_value is None


def test_a_partial_declaration_never_proposes_a_removal(
    session, project, tmp_path, store
):
    """`test_a_partial_revision_never_reads_as_a_deletion`, through the pass."""

    adopt(session, project, workbook_bytes(tmp_path / "a.xlsx", BASELINE_ROWS), tmp_path)
    document, _reading = register_delivered_revision(
        session,
        project,
        workbook_bytes(tmp_path / "b.xlsx", BASELINE_ROWS[:2]),
        tmp_path,
        completeness=PARTIAL_EXPORT,
    )

    extract_any(session, document)

    assert _deltas(session, project) == ()
    entry = session.scalars(
        select(AuditLog).where(
            AuditLog.action == audit.CAPTURE_LATER_SOURCE_REVISION,
            AuditLog.entity_id == document.id,
        )
    ).one()
    (withheld,) = entry.after_json["row_accounting"]["removals"]
    assert withheld["reason"] == ABSENT_FROM_REVISION
    assert withheld["proposed"] is False
    assert withheld["withheld_reason"] == WITHHELD_UNSEALED


def test_a_retired_row_reaches_the_same_disposition_through_the_pass(
    session, project, tmp_path, store
):
    """`test_every_row_of_the_revision_reaches_a_disposition`, through the pass."""

    adopt(session, project, workbook_bytes(tmp_path / "a.xlsx", BASELINE_ROWS), tmp_path)
    later = [
        *BASELINE_ROWS,
        ["UC-9", "Not Used", "", "", "", "", "", "", "", "", "", "", "", ""],
    ]
    document, _reading = register_delivered_revision(
        session,
        project,
        workbook_bytes(tmp_path / "b.xlsx", later),
        tmp_path,
        completeness=COMPLETE_ENUMERATION,
    )

    extract_any(session, document)

    entry = session.scalars(
        select(AuditLog).where(
            AuditLog.action == audit.CAPTURE_LATER_SOURCE_REVISION,
            AuditLog.entity_id == document.id,
        )
    ).one()
    accounting = entry.after_json["row_accounting"]
    assert len(accounting["rows"]) == len(later)
    assert accounting["rows"][-1]["disposition"] == "not_adoptable"
    assert accounting["rows"][-1]["reason"] == "retired_row"


def test_a_declared_additional_rendition_proposes_nothing_and_keeps_its_locators(
    session, project, tmp_path, store
):
    """ADR-0069: another rendition of one revision is not a second revision."""

    adopt(session, project, workbook_bytes(tmp_path / "a.xlsx", BASELINE_ROWS), tmp_path)
    changed = _changed(BASELINE_ROWS, 0, "Size", "18 in")
    first, _reading = register_delivered_revision(
        session,
        project,
        workbook_bytes(tmp_path / "b.xlsx", changed),
        tmp_path,
        completeness=COMPLETE_ENUMERATION,
        revision_identity="UCM workbook revision D",
    )
    extract_any(session, first)
    after_first = _deltas(session, project)

    # The same revision, exported again: one changed cell of whitespace is
    # enough for different bytes, and the coordinator says what it is.
    rendition = [list(row) for row in changed]
    rendition[1][HEADINGS.index("Comment")] = "re-exported"
    second, _second_reading = register_delivered_revision(
        session,
        project,
        workbook_bytes(tmp_path / "c.xlsx", rendition),
        tmp_path,
        completeness=COMPLETE_ENUMERATION,
        revision_identity="UCM workbook revision D",
        revision_relationship=ADDITIONAL_RENDITION,
        related_revision_identity="UCM workbook revision D",
        filename="ucm-later-again.xlsx",
        external_identity="UCM workbook revision D rendition 2",
    )

    assert extraction_route(second).output == SOURCE_FACTS
    assert extract_any(session, second) == []

    assert _deltas(session, project) == after_first
    assert not session.scalars(
        select(Fact).where(Fact.document_id == second.id)
    ).all()
    # Its own evidence and locators are retained, which is what a rendition is
    # kept for.
    assert session.scalars(
        select(SourceSegment).where(
            SourceSegment.document_id == second.id,
            SourceSegment.kind == "spreadsheet_cell",
        )
    ).first() is not None


def test_a_delivery_nobody_declared_is_held_with_a_named_owner(
    session, project, tmp_path, store
):
    from corridor.source_intake import confirm_intake, preview_intake
    from corridor.source_delivery import require_stored_envelope
    from later_revision_support import deliver

    adopt(session, project, workbook_bytes(tmp_path / "a.xlsx", BASELINE_ROWS), tmp_path)
    body = workbook_bytes(
        tmp_path / "b.xlsx", _changed(BASELINE_ROWS, 0, "Size", "18 in")
    )
    staged, envelope = deliver(session, project, body)
    delivery = require_stored_envelope(session, envelope)
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
    document = session.get_one(Document, confirmation.document_id)

    with pytest.raises(SourceRevisionHeld) as refused:
        extract_any(session, document)

    held = refused.value.held
    assert held.responsible_party
    assert held.next_action
    assert _deltas(session, project) == ()
    assert not session.scalars(
        select(Fact).where(Fact.document_id == document.id)
    ).all()
    run = session.scalars(
        select(ExtractionRun).where(ExtractionRun.document_id == document.id)
    ).one()
    assert run.outcome == "quarantined"


def test_the_pass_records_a_held_delivery_as_a_held_document(
    session, project, tmp_path, store
):
    """`extract_project` keeps the held fact where a register can read it."""

    from corridor.source_intake import confirm_intake, preview_intake
    from corridor.source_delivery import require_stored_envelope
    from later_revision_support import deliver

    adopt(session, project, workbook_bytes(tmp_path / "a.xlsx", BASELINE_ROWS), tmp_path)
    staged, envelope = deliver(
        session,
        project,
        workbook_bytes(tmp_path / "b.xlsx", _changed(BASELINE_ROWS, 0, "Size", "18 in")),
    )
    delivery = require_stored_envelope(session, envelope)
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

    outcomes = extract_project(
        session,
        project,
        select_route=extraction_route,
        commit=False,
        document_sha256=staged.sha256,
    )

    assert [outcome.status for outcome in outcomes] == ["quarantined"]
    [hold] = processing_holds.open_holds(session, confirmation.document_id)
    assert hold.prohibited_stage == processing_holds.SEMANTIC_EXTRACTION
    assert hold.reason_code == processing_holds.UNDECLARED_SOURCE_REVISION
    assert "acts next" in hold.reason
    assert _deltas(session, project) == ()


def test_the_receipt_names_the_declaration_and_the_service_identity_apart(
    session, project, tmp_path, store
):
    adopt(session, project, workbook_bytes(tmp_path / "a.xlsx", BASELINE_ROWS), tmp_path)
    document, _reading = register_delivered_revision(
        session,
        project,
        workbook_bytes(tmp_path / "b.xlsx", _changed(BASELINE_ROWS, 0, "Size", "18 in")),
        tmp_path,
        completeness=COMPLETE_ENUMERATION,
    )

    extract_any(session, document)

    entry = session.scalars(
        select(AuditLog).where(
            AuditLog.action == audit.CAPTURE_LATER_SOURCE_REVISION,
            AuditLog.entity_id == document.id,
        )
    ).one()
    declared = entry.after_json["declaration"]
    assert declared["declared_by_principal"] == PRINCIPAL.subject
    assert declared["is_complete_enumerative_source"] is True
    assert declared["revision_identity"] == "UCM workbook revision D"
    assert declared["declaration_id"] is not None
    assert (
        entry.after_json["executed_by_service_identity"]
        == REVISION_CAPTURE_SERVICE_IDENTITY
    )
    assert declared["declared_by_principal"] != (
        entry.after_json["executed_by_service_identity"]
    )
