"""A product upload is a delivery, with its whole lifecycle (#823, ADR-0089).

ADR-0089 put both transports in one ledger and an upload was in neither, because
a pushed delivery had to name a machine credential and a signed-in person holds
none. The arrival was an analytics observation and nothing else: an upload that
was refused, one that failed to store, and one somebody staged and walked away
from all left exactly the same trace, which is no trace at all.

These tests hold the replacement. Every outcome of an upload is a row in the
shared family, the person who handed the bytes over is what authenticated the
push, and admitting the delivery to processing is a separate act recorded once.
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import select

from corridor.config import settings
from corridor.models import Document, SourceDelivery, SourceDeliveryConfirmation
from corridor.principals import HumanPrincipal
import corridor.source_intake as source_intake
from corridor.source_delivery import (
    DeliveryBinding,
    DeliveryObservation,
    delivery_identity_for,
)
from corridor.source_intake import (
    PRODUCT_UPLOAD_CHANNEL,
    IntakeConflict,
    UploadConflict,
    UploadNotTaken,
    UploadRefused,
    confirm_intake,
    preview_intake,
    receive_upload,
)
from corridor.source_register import read_source_register
from corridor.web.onboarding_view import _supplied

from pdf_fixture_support import PdfFixture

UPLOADER = HumanPrincipal("local:dana-fields")
OTHER_UPLOADER = HumanPrincipal("local:sam-rivers")


def _matrix_pdf(marker: str = "segment 3C2") -> bytes:
    fixture = PdfFixture()
    page = fixture.add_page()
    page.text((72, 100), f"Utility Conflict Matrix — {marker}")
    page.text((72, 130), "FOC1-1  AT&T Texas  Telecom  STA 1149+00 to 1153+17")
    return fixture.tobytes()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    return tmp_path / "files"


def _receive(
    session,
    project,
    body,
    *,
    filename="matrix.pdf",
    source_revision="",
    principal=UPLOADER,
    submission_id="",
):
    return receive_upload(
        session,
        project=project,
        body=body,
        filename=filename,
        principal=principal,
        customer=settings.customer_id,
        source_revision=source_revision,
        submission_id=submission_id,
    )


def _deliveries(session, project):
    return list(
        session.scalars(
            select(SourceDelivery)
            .where(SourceDelivery.project_id == project.id)
            .order_by(SourceDelivery.id)
        ).all()
    )


def test_an_authorized_upload_is_one_stored_delivery_the_person_authenticated(
    session, project, store
):
    """Everything ADR-0089 says a delivery carries, on the upload channel."""

    body = _matrix_pdf()
    received = _receive(session, project, body)

    (row,) = _deliveries(session, project)
    assert row.id == received.delivery_id
    assert row.disposition == "stored"
    assert (row.transport, row.channel) == ("push", PRODUCT_UPLOAD_CHANNEL)
    # The person, not a credential minted for them.
    assert row.delivered_by_principal == UPLOADER.subject
    assert row.credential_id is None
    assert row.customer == settings.customer_id
    assert row.project_id == project.id
    assert row.content_sha256 == hashlib.sha256(body).hexdigest()
    assert row.external_identity == "matrix.pdf"
    assert row.bytes_reference.endswith(f"{row.content_sha256}.pdf")
    assert row.received_at is not None
    # The exact bytes are in the store under their own digest.
    assert received.staged.stored_path.read_bytes() == body


def test_a_refused_upload_is_recorded_with_the_rule_that_refused_it(
    session, project, store
):
    """Nothing is lost when the gate refuses: the refusal is the record.

    The digest is taken before the gate runs, so the row names the exact bytes
    that arrived rather than describing them.
    """

    with pytest.raises(UploadRefused) as refusal:
        _receive(session, project, b"just some notes", filename="notes.txt")

    (row,) = _deliveries(session, project)
    assert row.id == refusal.value.delivery_id
    assert row.disposition == "terminally_refused"
    assert row.refusal_reason.startswith("unsupported_type:")
    assert row.content_sha256 == hashlib.sha256(b"just some notes").hexdigest()
    assert row.delivered_by_principal == UPLOADER.subject
    # Refused bytes are not held, so the row points at nothing.
    assert row.bytes_reference == ""


def test_a_storage_failure_is_transient_and_says_nothing_about_the_bytes(
    session, project, store, monkeypatch
):
    """The opposite fact from a refusal, and the checkpoint rule turns on it."""

    def unavailable(*_args, **_kwargs):
        raise OSError("the object store rejected the write")

    monkeypatch.setattr(source_intake, "store_bytes", unavailable)

    with pytest.raises(UploadNotTaken) as failure:
        _receive(session, project, _matrix_pdf())

    (row,) = _deliveries(session, project)
    assert row.id == failure.value.delivery_id
    assert row.disposition == "transient_failure"
    assert "OSError" in row.refusal_reason
    assert "rejected the write" in row.refusal_reason


def test_an_upload_nobody_confirmed_is_a_delivery_and_no_document(
    session, project, store
):
    """An abandoned staged upload is a record rather than an absence."""

    received = _receive(session, project, _matrix_pdf())

    (row,) = _deliveries(session, project)
    assert row.disposition == "stored"
    assert row.id == received.delivery_id
    assert session.scalars(
        select(SourceDeliveryConfirmation).where(
            SourceDeliveryConfirmation.delivery_id == row.id
        )
    ).first() is None
    assert session.scalars(
        select(Document).where(Document.project_id == project.id)
    ).first() is None


def test_confirmation_is_attributable_bound_to_the_delivery_and_idempotent(
    session, project, store
):
    """The person's admission, recorded once however many times it is replayed."""

    body = _matrix_pdf()
    received = _receive(session, project, body)
    preview = preview_intake(session, project, received.staged, "matrix")

    arguments = dict(
        project=project,
        sha256=received.staged.sha256,
        filename=received.staged.filename,
        doc_type="matrix",
        binding_fingerprint=preview.binding_fingerprint,
        principal=UPLOADER,
        source_delivery_id=received.delivery_id,
    )
    first = confirm_intake(session, **arguments)
    second = confirm_intake(session, **arguments)

    assert first.delivery_confirmation_id == second.delivery_confirmation_id
    confirmations = list(
        session.scalars(
            select(SourceDeliveryConfirmation).where(
                SourceDeliveryConfirmation.project_id == project.id
            )
        ).all()
    )
    assert len(confirmations) == 1
    assert confirmations[0].delivery_id == received.delivery_id
    assert confirmations[0].confirmed_by_principal == UPLOADER.subject
    # The receipt carries the delivery identity this module had reserved for it.
    document = session.get(Document, first.document_id)
    assert document.source_delivery_id == received.delivery_id


def test_a_confirmation_naming_another_delivery_is_refused_before_any_write(
    session, project, store
):
    """The posted identity is re-proved against these bytes, not trusted."""

    other = _receive(session, project, _matrix_pdf("segment 4A1"), filename="other.pdf")
    body = _matrix_pdf()
    received = _receive(session, project, body)
    preview = preview_intake(session, project, received.staged, "matrix")

    with pytest.raises(IntakeConflict) as conflict:
        confirm_intake(
            session,
            project=project,
            sha256=received.staged.sha256,
            filename=received.staged.filename,
            doc_type="matrix",
            binding_fingerprint=preview.binding_fingerprint,
            principal=UPLOADER,
            source_delivery_id=other.delivery_id,
        )

    assert conflict.value.reason == "delivery_mismatch"
    assert session.scalars(
        select(Document).where(Document.project_id == project.id)
    ).first() is None
    assert session.scalars(select(SourceDeliveryConfirmation)).first() is None


def test_identical_bytes_declared_a_new_revision_are_a_second_delivery(
    session, project, store
):
    """Storage deduplication and delivery identity are different questions.

    The same workbook can be handed over again as a genuinely new revision of
    the source, and that is not the same delivery — but it is the same bytes,
    so nothing is written to the store twice.
    """

    body = _matrix_pdf()
    first = _receive(session, project, body)
    again = _receive(session, project, body, source_revision="revision D")

    stored = [row for row in _deliveries(session, project) if row.disposition == "stored"]
    assert [row.id for row in stored] == [first.delivery_id, again.delivery_id]
    assert first.delivery_identity != again.delivery_identity
    assert len({row.content_sha256 for row in stored}) == 1
    assert len({row.bytes_reference for row in stored}) == 1
    assert not first.replayed and not again.replayed


def test_the_same_undeclared_upload_converges_on_the_delivery_already_taken(
    session, project, store
):
    """A re-posted form is one delivery, recorded once and found again."""

    body = _matrix_pdf()
    first = _receive(session, project, body)
    again = _receive(session, project, body)

    assert again.delivery_id == first.delivery_id
    assert again.replayed is True
    dispositions = [row.disposition for row in _deliveries(session, project)]
    assert dispositions == ["stored", "duplicate"]


# --- Submission identity (#957) ---------------------------------------------
# The defect: the delivery identity did not include the submitting principal, so
# the same bytes handed over by two people converged on one row attributed to
# whoever got there first. The identity now names the authenticated principal
# and an opaque submission id, so each independently initiated human submission
# is its own delivery while the stored bytes stay deduplicated.


def test_two_people_uploading_identical_bytes_are_two_attributed_deliveries(
    session, project, store
):
    """The #957 defect: the same bytes from two people were one delivery.

    Each person's act is now its own row, correctly attributed, and the stored
    byte object is still written once.
    """

    body = _matrix_pdf()
    first = _receive(session, project, body, principal=UPLOADER)
    second = _receive(session, project, body, principal=OTHER_UPLOADER)

    stored = [r for r in _deliveries(session, project) if r.disposition == "stored"]
    assert len(stored) == 2
    assert first.delivery_id != second.delivery_id
    assert first.delivery_identity != second.delivery_identity
    assert {r.delivered_by_principal for r in stored} == {
        UPLOADER.subject,
        OTHER_UPLOADER.subject,
    }
    # Storage stays deduplicated: one set of bytes, one stored object.
    assert len({r.content_sha256 for r in stored}) == 1
    assert len({r.bytes_reference for r in stored}) == 1
    assert not second.replayed


def test_an_honest_retry_of_one_submission_converges_on_one_delivery(
    session, project, store
):
    """Same actor, same submission id, same payload: one delivery, found again."""

    body = _matrix_pdf()
    first = _receive(session, project, body, submission_id="submission-alpha")
    again = _receive(session, project, body, submission_id="submission-alpha")

    assert again.delivery_id == first.delivery_id
    assert again.replayed is True
    stored = [r for r in _deliveries(session, project) if r.disposition == "stored"]
    assert len(stored) == 1


def test_a_new_submission_of_identical_bytes_is_a_second_delivery(
    session, project, store
):
    """Same actor, a new submission id, identical bytes: a second delivery."""

    body = _matrix_pdf()
    first = _receive(session, project, body, submission_id="submission-alpha")
    second = _receive(session, project, body, submission_id="submission-beta")

    assert first.delivery_id != second.delivery_id
    assert first.delivery_identity != second.delivery_identity
    stored = [r for r in _deliveries(session, project) if r.disposition == "stored"]
    assert len(stored) == 2
    assert len({r.content_sha256 for r in stored}) == 1


def test_reusing_a_submission_id_with_different_bytes_is_refused(
    session, project, store
):
    """Same actor, same submission id, a different payload: conflicting reuse."""

    first = _receive(
        session, project, _matrix_pdf("first"), submission_id="submission-alpha"
    )
    with pytest.raises(UploadConflict):
        _receive(
            session,
            project,
            _matrix_pdf("second and different"),
            submission_id="submission-alpha",
        )
    # The conflicting reuse stored nothing: the first delivery still stands alone.
    stored = [r for r in _deliveries(session, project) if r.disposition == "stored"]
    assert [r.id for r in stored] == [first.delivery_id]


def test_a_connector_and_a_human_delivering_identical_bytes_are_separate():
    """Already distinct by channel, and the principal keeps them apart too."""

    digest = hashlib.sha256(b"a shared workbook").hexdigest()
    observation = DeliveryObservation(
        external_identity="budget.xlsx", external_version=digest, content_digest=digest
    )
    human = DeliveryBinding(
        customer="acme",
        project_id=1,
        project_slug="acme-north",
        transport="push",
        channel=PRODUCT_UPLOAD_CHANNEL,
        configuration_identity="product-upload",
        delivered_by_principal=UPLOADER.subject,
        submission_id="submission-alpha",
    )
    connector = DeliveryBinding(
        customer="acme",
        project_id=1,
        project_slug="acme-north",
        transport="pull",
        channel="connector",
        configuration_identity="location:sharepoint-1",
    )
    human_identity, _ = delivery_identity_for(human, observation)
    connector_identity, _ = delivery_identity_for(connector, observation)
    assert human_identity != connector_identity


def test_both_peoples_deliveries_show_in_the_source_register(
    session, project, store
):
    """The register shows what happened as content, not only a row count."""

    body = _matrix_pdf()
    first = _receive(session, project, body, principal=UPLOADER)
    second = _receive(session, project, body, principal=OTHER_UPLOADER)

    register = read_source_register(session, project_id=project.id)
    delivery_ids = {row.delivery_id for row in register.rows}
    assert {first.delivery_id, second.delivery_id} <= delivery_ids
    upload_rows = [row for row in register.rows if row.delivery_id in delivery_ids]
    assert all(row.filename == "matrix.pdf" for row in upload_rows)
    assert len([row for row in register.rows if row.channel == PRODUCT_UPLOAD_CHANNEL]) == 2


def test_both_people_are_named_in_the_onboarding_reading(session, project, store):
    """Onboarding groups the identical bytes without merging the two acts."""

    body = _matrix_pdf()
    _receive(session, project, body, principal=UPLOADER)
    _receive(session, project, body, principal=OTHER_UPLOADER)

    groups = _supplied(session, project.id)
    assert len(groups) == 1
    group = groups[0]
    assert group.identical_contents is True
    assert len(group.deliveries) == 2
    assert {source.submitted_by for source in group.deliveries} == {
        f"Submitted by {UPLOADER.subject}",
        f"Submitted by {OTHER_UPLOADER.subject}",
    }
