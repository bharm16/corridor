"""Which Documents name the delivery they came in on, and which stay unknown (#687).

#675 built `documents.source_delivery_id`, the walk from a Proposed Delta to it,
and ADR-0085's "Can wait" limb that reads it — and no ingress path wrote the
column, so the limb fired for nothing. These tests fix the rule at the seam that
creates Documents: an intake path holding a proven `source_deliveries` row writes
the link in the same transaction, and every other path leaves it null, because a
null link is a supported answer and a guessed one is not.

No test here reads a clock. The delivery ordering these rules turn on is the
append-only identifier, never an arrival instant, which is exactly why #675 keyed
the boundary by identity in the first place.
"""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from corridor import audit, email_intake, push_intake
from corridor.config import settings
from corridor.db import Session, engine
from corridor.document_delivery_backfill import (
    CAPTURE_RECEIPT_RULE,
    INBOUND_MESSAGE_RULE,
    link_documents_to_deliveries,
)
from corridor.ingest import ingest_document
from corridor.key_date_table import capture_key_date_table
from corridor.later_revision import capture_later_revision
from corridor.models import (
    AuditLog,
    Document,
    InboundMessage,
    Project,
    SourceDelivery,
)
from corridor.source_delivery import (
    DeliveryBinding,
    DeliveryObservation,
    take_delivery,
)
from corridor.source_intake import confirm_intake, preview_intake, validate_and_stage

from key_date_table_support import (
    KEY_DATE_ROWS,
    UCM_HEADINGS,
    UCM_ROWS,
    deliver_export,
    key_date_workbook,
)
from later_revision_support import (
    BASELINE_ROWS,
    HEADINGS,
    PRINCIPAL,
    adopt,
    deliver,
    workbook_bytes,
)
from pdf_fixture_support import PdfFixture


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    scoped = Session(bind=connection)
    yield scoped
    scoped.close()
    if transaction.is_active:
        transaction.rollback()
    connection.close()


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "store"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    return tmp_path / "store"


@pytest.fixture
def project(session):
    row = Project(
        slug=f"delivery-link-{uuid4().hex[:8]}",
        name="Delivery Link Test",
        is_synthetic=True,
    )
    session.add(row)
    session.flush()
    return row


def _delivery(session, envelope) -> SourceDelivery:
    return session.scalars(
        select(SourceDelivery).where(
            SourceDelivery.idempotency_key == envelope.idempotency_key
        )
    ).one()


def _revised_ucm(tmp_path, promised_for: str = "2026-06-01") -> bytes:
    rows = [list(row) for row in BASELINE_ROWS]
    rows[0][HEADINGS.index("Promised For")] = promised_for
    return workbook_bytes(tmp_path / f"ucm-{promised_for}.xlsx", rows)


def _push(session, project, payload: bytes, *, alias: str | None = None):
    material = alias or f"alias-{uuid4().hex[:8]}@corridor.test"
    push_intake.register_push_credential(
        session,
        customer="Lone Star Transit Authority",
        project=project,
        channel="project_alias",
        material=material,
    )
    return email_intake.receive_pushed_message(
        session,
        credential=push_intake.PushCredential(
            channel="project_alias", material=material
        ),
        raw_bytes=payload,
    )


def _message(
    *,
    body: str,
    attachment: bytes | None = None,
    to: str = "someone@example.test",
) -> bytes:
    message = EmailMessage()
    message["From"] = "utility@example.test"
    message["To"] = to
    message["Message-ID"] = f"<{uuid4().hex}@example.test>"
    message["Subject"] = "Relocation update"
    message.set_content(body)
    if attachment is not None:
        message.add_attachment(
            attachment,
            maintype="application",
            subtype="pdf",
            filename="attachment.pdf",
        )
    return message.as_bytes()


def _pdf(text: str) -> bytes:
    fixture = PdfFixture()
    fixture.add_page().text((72, 72), text)
    return fixture.tobytes()


# --- the paths that hold a proven delivery ----------------------------------


def test_a_later_ucm_revision_names_the_delivery_it_arrived_on(
    session, project, tmp_path
):
    """The row `capture_later_revision` already refuses to run without.

    That refusal has checked the disposition, the digest and the project, so
    the link is the same fact the capture was allowed to happen on rather than
    a second derivation that could disagree with it.
    """

    adopt(session, project, workbook_bytes(tmp_path / "ucm.xlsx", BASELINE_ROWS), tmp_path)
    staged, envelope = deliver(session, project, _revised_ucm(tmp_path))
    delivery = _delivery(session, envelope)

    capture = capture_later_revision(
        session,
        project=project,
        staged=staged,
        envelope=envelope,
        principal=PRINCIPAL,
        images_dir=tmp_path / "images",
    )

    assert session.get(Document, capture.document_id).source_delivery_id == delivery.id


def test_a_key_date_export_names_the_delivery_it_arrived_on(
    session, project, tmp_path
):
    adopt(
        session,
        project,
        workbook_bytes(tmp_path / "ucm.xlsx", UCM_ROWS, headings=UCM_HEADINGS),
        tmp_path,
    )
    staged, envelope = deliver_export(
        session,
        project,
        key_date_workbook(tmp_path / "key-dates.xlsx", KEY_DATE_ROWS),
        external_identity="schedule export 1",
    )
    delivery = _delivery(session, envelope)

    capture = capture_key_date_table(
        session,
        project=project,
        staged=staged,
        envelope=envelope,
        principal=PRINCIPAL,
        images_dir=tmp_path / "images",
    )

    assert session.get(Document, capture.document_id).source_delivery_id == delivery.id


def test_a_pushed_message_names_its_delivery_and_its_attachment_does_not(
    session, project
):
    """The message document is the delivery's own bytes; an attachment is a part.

    Linking the part too would fold it into the delivery's single coverage
    line, and an attachment that failed to parse would then hide behind a
    message that read cleanly. It stays its own line, and its own honest
    unknown.
    """

    received = _push(
        session,
        project,
        _message(body="Relocation is delayed.", attachment=_pdf("attachment text")),
    )
    inbound = session.get(InboundMessage, received.message_id)

    assert inbound.push_delivery_id is not None
    body = session.get(Document, inbound.document_id)
    assert body.source_delivery_id == inbound.push_delivery_id

    registered = [
        receipt["document_id"]
        for receipt in inbound.attachments_json
        if receipt.get("document_id")
    ]
    assert registered, "the fixture registered no attachment to judge"
    for document_id in registered:
        assert session.get(Document, document_id).source_delivery_id is None


# --- the paths that hold none -----------------------------------------------


def test_the_frozen_global_address_route_writes_no_link(session, project):
    """ADR-0059's route infers the project from content and takes no delivery."""

    email_intake.register_project_identifier(
        session, project=project, kind="csj", value="0912-31-999"
    )
    received = email_intake.receive_message(
        session,
        raw_bytes=_message(
            body="CSJ 0912-31-999 is delayed.", to="intake@corridor.test"
        ),
        service_address="intake@corridor.test",
    )
    inbound = session.get(InboundMessage, received.message_id)

    assert inbound.push_delivery_id is None
    assert session.get(Document, inbound.document_id).source_delivery_id is None


def test_an_upload_with_no_delivery_leaves_the_link_unknown(
    session, project, tmp_path
):
    """Paper handed over at a meeting arrived through no transport (ADR-0058)."""

    staged = validate_and_stage(_pdf("handed over at the meeting"), "notes.pdf")
    preview = preview_intake(session, project, staged, "other")
    confirmation = confirm_intake(
        session,
        project=project,
        sha256=staged.sha256,
        filename=staged.filename,
        doc_type="other",
        binding_fingerprint=preview.binding_fingerprint,
        principal=PRINCIPAL,
        images_dir=tmp_path / "images",
    )

    assert session.get(Document, confirmation.document_id).source_delivery_id is None


def test_the_adopted_baseline_workbook_leaves_the_link_unknown(
    session, project, tmp_path
):
    """Adopt Baseline reads the customer's own workbook, not a delivery."""

    body = workbook_bytes(tmp_path / "ucm.xlsx", BASELINE_ROWS)
    adopt(session, project, body, tmp_path)

    adopted = session.scalars(
        select(Document).where(Document.project_id == project.id)
    ).all()
    assert adopted, "the fixture adopted no baseline document"
    assert all(document.source_delivery_id is None for document in adopted)


def test_a_second_delivery_of_identical_bytes_never_relabels_the_first(
    session, project, tmp_path
):
    """Re-ingest backfills a null link and overwrites nothing.

    Identical bytes can be delivered twice. The document came in on the first
    of them, and the second is a duplicate observation of the same source, not
    a correction of where it came from.
    """

    adopt(session, project, workbook_bytes(tmp_path / "ucm.xlsx", BASELINE_ROWS), tmp_path)
    body = _revised_ucm(tmp_path)
    staged, first_envelope = deliver(session, project, body)
    first = _delivery(session, first_envelope)
    capture = capture_later_revision(
        session,
        project=project,
        staged=staged,
        envelope=first_envelope,
        principal=PRINCIPAL,
        images_dir=tmp_path / "images",
    )
    document = session.get(Document, capture.document_id)
    assert document.source_delivery_id == first.id

    _staged, second_envelope = deliver(
        session,
        project,
        body,
        filename="ucm-again.xlsx",
        external_identity="UCM workbook revision D, sent again",
        material=f"secret-again-{project.slug}",
    )
    second = _delivery(session, second_envelope)
    assert second.id != first.id

    ingest_document(
        session,
        project_id=project.id,
        path=Path(staged.stored_path),
        doc_type="matrix",
        images_dir=tmp_path / "images",
        filename="ucm-again.xlsx",
        expected_sha256=staged.sha256,
        source_delivery_id=int(second.id),
    )

    session.refresh(document)
    assert document.source_delivery_id == first.id


# --- the backfill for the history -------------------------------------------


def test_the_backfill_links_what_an_inbound_message_already_names(session, project):
    """Rule 1: a foreign key somebody already wrote, read back."""

    received = _push(session, project, _message(body="Relocation is delayed."))
    inbound = session.get(InboundMessage, received.message_id)
    document = session.get(Document, inbound.document_id)
    document.source_delivery_id = None
    session.flush()

    report = link_documents_to_deliveries(session, project_id=project.id)

    assert report.linked == {INBOUND_MESSAGE_RULE: 1}
    session.refresh(document)
    assert document.source_delivery_id == inbound.push_delivery_id


def test_the_backfill_links_what_a_capture_receipt_proves(session, project, tmp_path):
    """Rule 2: the receipt of a capture that had already proven the delivery."""

    adopt(session, project, workbook_bytes(tmp_path / "ucm.xlsx", BASELINE_ROWS), tmp_path)
    staged, envelope = deliver(session, project, _revised_ucm(tmp_path))
    delivery = _delivery(session, envelope)
    capture = capture_later_revision(
        session,
        project=project,
        staged=staged,
        envelope=envelope,
        principal=PRINCIPAL,
        images_dir=tmp_path / "images",
    )
    document = session.get(Document, capture.document_id)
    document.source_delivery_id = None
    session.flush()

    report = link_documents_to_deliveries(session, project_id=project.id)

    assert report.linked == {CAPTURE_RECEIPT_RULE: 1}
    session.refresh(document)
    assert document.source_delivery_id == delivery.id


def test_the_backfill_leaves_a_receipt_naming_other_bytes_unknown(
    session, project, tmp_path
):
    """A receipt describing another source may not link this document.

    The receipt is repointed at a delivery that really is in this project, is
    really `stored`, and really is the only one answering to those three facts
    — so the ledger lookup alone would happily return it. What refuses is the
    one question the lookup cannot ask: whether the receipt is describing this
    document's own bytes. Without that, "a receipt proves the delivery" decays
    into "some delivery of this project", which is the guess the ticket
    forbids.
    """

    adopt(session, project, workbook_bytes(tmp_path / "ucm.xlsx", BASELINE_ROWS), tmp_path)
    staged, envelope = deliver(session, project, _revised_ucm(tmp_path))
    capture = capture_later_revision(
        session,
        project=project,
        staged=staged,
        envelope=envelope,
        principal=PRINCIPAL,
        images_dir=tmp_path / "images",
    )
    _other_staged, other = deliver(
        session,
        project,
        _revised_ucm(tmp_path, promised_for="2026-07-01"),
        filename="ucm-other.xlsx",
        external_identity="UCM workbook revision E",
        material=f"secret-other-{project.slug}",
    )
    other_delivery = _delivery(session, other)

    document = session.get(Document, capture.document_id)
    document.source_delivery_id = None
    receipt = session.scalars(
        select(AuditLog)
        .where(
            AuditLog.action == audit.CAPTURE_LATER_SOURCE_REVISION,
            AuditLog.entity_id == document.id,
        )
        .order_by(AuditLog.id)
    ).one()
    receipt.after_json = {
        **receipt.after_json,
        "content_sha256": other_delivery.content_sha256,
        "external_identity": other_delivery.external_identity,
        "external_version": other_delivery.external_version,
    }
    session.flush()
    assert other_delivery.content_sha256 != document.sha256
    assert (
        session.scalars(
            select(SourceDelivery.id).where(
                SourceDelivery.project_id == project.id,
                SourceDelivery.content_sha256 == other_delivery.content_sha256,
                SourceDelivery.external_identity == other_delivery.external_identity,
                SourceDelivery.external_version == other_delivery.external_version,
                SourceDelivery.disposition == "stored",
            )
        ).all()
        == [other_delivery.id]
    ), "the repointed receipt must resolve to exactly one delivery, or the guard is untested"

    report = link_documents_to_deliveries(session, project_id=project.id)

    assert report.linked == {}
    assert report.left_unknown == 1
    session.refresh(document)
    assert document.source_delivery_id is None


def test_the_backfill_leaves_an_ambiguous_receipt_unknown(session, project, tmp_path):
    """Two stored deliveries answering to the same four facts prove neither.

    The ledger's identity includes the channel, so the same bytes under the
    same customer identity and version can stand twice — once per channel. The
    receipt cannot say which of them this document came in on, so the pass says
    nothing rather than picking the first.
    """

    adopt(session, project, workbook_bytes(tmp_path / "ucm.xlsx", BASELINE_ROWS), tmp_path)
    staged, envelope = deliver(session, project, _revised_ucm(tmp_path))
    capture = capture_later_revision(
        session,
        project=project,
        staged=staged,
        envelope=envelope,
        principal=PRINCIPAL,
        images_dir=tmp_path / "images",
    )
    take_delivery(
        session,
        DeliveryBinding(
            customer="Lone Star Transit Authority",
            project_id=project.id,
            project_slug=project.slug,
            transport="pull",
            channel="shared-files",
            configuration_identity="shared-files-v1",
            configuration_version="1",
        ),
        DeliveryObservation(
            external_identity=envelope.external_identity,
            external_version=envelope.external_version,
            content_digest=staged.sha256,
            bytes_reference="objects/second",
        ),
        service_identity="test",
        run_identity=uuid4().hex,
    )
    document = session.get(Document, capture.document_id)
    document.source_delivery_id = None
    session.flush()
    twice = session.scalars(
        select(SourceDelivery.id).where(
            SourceDelivery.project_id == project.id,
            SourceDelivery.content_sha256 == staged.sha256,
            SourceDelivery.disposition == "stored",
        )
    ).all()
    assert len(twice) == 2, "the fixture holds no ambiguity to leave unknown"

    report = link_documents_to_deliveries(session, project_id=project.id)

    assert report.linked == {}
    assert report.left_unknown == 1
    session.refresh(document)
    assert document.source_delivery_id is None


def test_the_backfill_never_guesses_a_delivery_for_an_unproven_document(
    session, project, tmp_path
):
    """A manual upload beside a real delivery of other bytes stays unknown.

    Nothing sweeps the ledger for a plausible neighbour: with no receipt naming
    this document, there is no proof, and no proof is left as unknown.
    """

    adopt(session, project, workbook_bytes(tmp_path / "ucm.xlsx", BASELINE_ROWS), tmp_path)
    deliver(session, project, _revised_ucm(tmp_path))
    staged = validate_and_stage(_pdf("handed over at the meeting"), "notes.pdf")
    preview = preview_intake(session, project, staged, "other")
    confirmation = confirm_intake(
        session,
        project=project,
        sha256=staged.sha256,
        filename=staged.filename,
        doc_type="other",
        binding_fingerprint=preview.binding_fingerprint,
        principal=PRINCIPAL,
        images_dir=tmp_path / "images",
    )

    link_documents_to_deliveries(session, project_id=project.id)

    assert session.get(Document, confirmation.document_id).source_delivery_id is None


def test_the_backfill_is_idempotent_and_never_overwrites_a_link(session, project):
    """A second pass finds its own work done and changes nothing."""

    received = _push(session, project, _message(body="Relocation is delayed."))
    inbound = session.get(InboundMessage, received.message_id)
    document = session.get(Document, inbound.document_id)

    # Intake already linked it, so the first pass has nothing to do at all.
    assert document.source_delivery_id == inbound.push_delivery_id
    assert link_documents_to_deliveries(session, project_id=project.id).total == 0

    document.source_delivery_id = None
    session.flush()
    assert link_documents_to_deliveries(session, project_id=project.id).total == 1
    assert link_documents_to_deliveries(session, project_id=project.id).total == 0
    session.refresh(document)
    assert document.source_delivery_id == inbound.push_delivery_id


def test_the_backfill_stays_inside_the_project_it_was_given(session, tmp_path):
    """One customer's pass never reaches another customer's documents."""

    first = Project(
        slug=f"delivery-link-a-{uuid4().hex[:8]}", name="A", is_synthetic=True
    )
    second = Project(
        slug=f"delivery-link-b-{uuid4().hex[:8]}", name="B", is_synthetic=True
    )
    session.add_all([first, second])
    session.flush()
    for row in (first, second):
        received = _push(session, row, _message(body="Relocation is delayed."))
        inbound = session.get(InboundMessage, received.message_id)
        session.get(Document, inbound.document_id).source_delivery_id = None
    session.flush()

    report = link_documents_to_deliveries(session, project_id=first.id)

    assert report.linked == {INBOUND_MESSAGE_RULE: 1}
    unlinked = session.scalars(
        select(Document).where(
            Document.project_id == second.id, Document.source_delivery_id.is_(None)
        )
    ).all()
    assert unlinked


def test_the_backfill_is_reachable_as_the_operator_command_it_is():
    """A one-shot historical pass is still an operator entry point, not dead code.

    This module lives in the application package with no caller in `src`, which
    is what an operator command looks like here: a `main` and a `make` target,
    the same shape as every `*_cli`. It stays reachable that way rather than
    moving under `scripts/`, because the pass reads customer relations through
    the ordinary session factory and an operator runs it once per deployment
    that registered Documents before #687 — after which the intake paths write
    the link themselves and this has nothing left to find.
    """

    from corridor import document_delivery_backfill

    recipe = (
        (Path(__file__).parents[1] / "Makefile")
        .read_text(encoding="utf-8")
        .split("\nlink-deliveries:\n", 1)[1]
        .split("\n\n", 1)[0]
    )
    assert "python -m corridor.document_delivery_backfill" in recipe
    assert callable(document_delivery_backfill.main)
    # Anything but the one documented invocation is a usage error, not a sweep.
    assert document_delivery_backfill.main(["sweep-everything"]) == 2
    assert document_delivery_backfill.main([]) == 2
