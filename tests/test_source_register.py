"""One register over every delivery, whatever became of it (#841).

The page this reader feeds used to list documents carrying a product-intake
confirmation receipt and nothing else, so a refused delivery, a delivery that
never completed, a connector's pull, a corpus file and an upload somebody
staged and walked away from were all the same absence. These tests hold the
replacement: the ledger is the spine, every disposition is a row with the
reason the record holds, and "processed" is never allowed to read as "nothing
else needs attention".

Every row here is produced by the real intake path rather than by inserting
ledger rows by hand, because what is being tested is that the register agrees
with what the product actually records.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from corridor.config import settings
from corridor.extraction_runs import record_extraction_run
from corridor.intake_hardening import quarantine_document
from corridor.models import Document, SourceDelivery
from corridor.principals import HumanPrincipal
from corridor.proposed_deltas import (
    ExistingSubjectTarget,
    ProposedDeltaValues,
    create_proposed_delta_group,
)
from corridor.source_intake import (
    UploadNotTaken,
    UploadRefused,
    confirm_intake,
    preview_intake,
    receive_upload,
    validate_and_stage,
)
from corridor.source_register import (
    AWAITING_CONFIRMATION,
    DELIVERY_INCOMPLETE,
    OWNER_PROJECT_TEAM,
    OWNER_TECHNICAL_OPERATIONS,
    REFUSED_AT_INTAKE,
    RegisterFilters,
    read_source_register,
)

from pdf_fixture_support import PdfFixture

UPLOADER = HumanPrincipal("local:dana-fields")
PROMPT_VERSION = "matrix_v1"
SCHEMA_VERSION = "matrix_candidate_shape_v1"
MODEL = "gpt-test"


def _matrix_pdf(marker: str = "segment 3C2") -> bytes:
    fixture = PdfFixture()
    page = fixture.add_page(height=180)
    page.text((72, 100), f"Utility Conflict Matrix — {marker}")
    page.text((72, 130), "FOC1-1  AT&T Texas  Telecom  STA 1149+00 to 1153+17")
    return fixture.tobytes()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    return tmp_path / "files"


def _receive(session, project, body, *, filename="matrix.pdf", revision=""):
    return receive_upload(
        session,
        project=project,
        body=body,
        filename=filename,
        principal=UPLOADER,
        customer=settings.customer_id,
        source_revision=revision,
    )


def _upload_and_confirm(session, project, marker="Owner", doc_type="matrix"):
    """The whole product path: take delivery, preview it, then admit it."""

    body = _matrix_pdf(marker)
    received = _receive(session, project, body, filename=f"{marker}.pdf")
    staged = validate_and_stage(body, f"{marker}.pdf")
    preview = preview_intake(session, project, staged, doc_type)
    return received, confirm_intake(
        session,
        project=project,
        sha256=preview.sha256,
        filename=preview.filename,
        doc_type=preview.doc_type,
        binding_fingerprint=preview.binding_fingerprint,
        principal=UPLOADER,
        source_delivery_id=received.delivery_id,
    )


def _rows(session, project, **kwargs):
    register = read_source_register(session, project_id=project.id, **kwargs)
    return {row.key: row for row in register.rows}, register


def test_every_delivery_appears_once_whatever_became_of_it(
    session, project, store, monkeypatch
):
    """A refusal, a failure, an unadmitted upload and an admitted one: four rows.

    The old reader returned one of these four, and the other three were an
    absence indistinguishable from never having sent the file at all.
    """

    _received, confirmed = _upload_and_confirm(session, project, marker="Admitted")

    abandoned = _receive(
        session, project, _matrix_pdf("Abandoned"), filename="abandoned.pdf"
    )

    with pytest.raises(UploadRefused):
        _receive(session, project, b"not a pdf at all", filename="hostile.pdf")

    def _fails(*args, **kwargs):
        raise RuntimeError("the object store rejected the write")

    monkeypatch.setattr("corridor.source_intake.validate_and_stage", _fails)
    with pytest.raises(UploadNotTaken):
        _receive(session, project, _matrix_pdf("Failed"), filename="failed.pdf")
    monkeypatch.undo()

    deliveries = session.scalars(
        select(SourceDelivery).where(SourceDelivery.project_id == project.id)
    ).all()
    rows, register = _rows(session, project)

    assert len(deliveries) == 4
    assert len(rows) == 4 == register.total
    assert {row.state for row in rows.values()} == {
        "pending",
        AWAITING_CONFIRMATION,
        REFUSED_AT_INTAKE,
        DELIVERY_INCOMPLETE,
    }
    # Each row that is not stored carries the reason the ledger recorded, not
    # a sentence composed here.
    refused = next(row for row in rows.values() if row.state == REFUSED_AT_INTAKE)
    assert "hostile.pdf" in refused.recorded_reason
    failed = next(row for row in rows.values() if row.state == DELIVERY_INCOMPLETE)
    assert "object store rejected the write" in failed.recorded_reason
    # The admitted one names the person who admitted it and when.
    admitted = next(
        row for row in rows.values() if row.document_id == confirmed.document_id
    )
    assert admitted.confirmed_by == UPLOADER.subject
    assert admitted.confirmed_at is not None
    assert abandoned.delivery_id in {
        row.delivery_id for row in rows.values() if row.state == AWAITING_CONFIRMATION
    }


def test_a_source_that_names_no_delivery_is_still_in_the_register(
    session, project, store
):
    """A corpus file arrived through no transport, and the register says so.

    The predecessor deliberately excluded it, which made a project's source
    list look complete while the file the record was built from was missing.
    """

    corpus = Document(
        project_id=project.id,
        sha256="c" * 64,
        filename="from-corpus.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(corpus)
    session.flush()

    rows, _register = _rows(session, project)

    row = rows[f"document:{corpus.id}"]
    assert row.delivery_id is None
    assert (row.transport, row.channel) == ("", "")
    assert row.document_id == corpus.id


def test_the_processing_state_of_each_source_is_derived_from_its_own_receipts(
    session, project, store
):
    """Pending, processed, a failed parse and a held file, none of them stored."""

    _, pending = _upload_and_confirm(session, project, marker="Pending")
    _, processed = _upload_and_confirm(session, project, marker="Processed")
    _, failed = _upload_and_confirm(session, project, marker="Failed")
    _, held = _upload_and_confirm(session, project, marker="Held")

    record_extraction_run(
        session,
        session.get(Document, processed.document_id),
        prompt_version=PROMPT_VERSION,
        candidate_count=0,
        page_errors=0,
        outcome="completed",
        model=MODEL,
        schema_version=SCHEMA_VERSION,
        allow_unsealed_legacy=True,
    )
    session.get(Document, failed.document_id).parse_status = "failed"
    quarantine_document(
        session,
        held.document_id,
        "document-asserted work sequencing is not modeled (#149)",
    )
    session.flush()

    rows, _register = _rows(session, project)
    by_document = {row.document_id: row for row in rows.values()}

    assert by_document[pending.document_id].state == "pending"
    assert by_document[processed.document_id].state == "processed"
    assert by_document[failed.document_id].state == "parse_failed"
    assert by_document[held.document_id].state == "held_unmodeled"


def test_processed_never_reads_as_nothing_else_needing_attention(
    session, project, store
):
    """A source read in full can still owe a decision, and the row says so."""

    _, confirmed = _upload_and_confirm(session, project, marker="Read")
    document = session.get(Document, confirmed.document_id)
    record_extraction_run(
        session,
        document,
        prompt_version=PROMPT_VERSION,
        candidate_count=0,
        page_errors=0,
        outcome="completed",
        model=MODEL,
        schema_version=SCHEMA_VERSION,
        allow_unsealed_legacy=True,
    )
    session.flush()

    settled, _register = _rows(session, project)
    read_in_full = next(iter(settled.values()))
    assert read_in_full.state_words == "Processed"
    assert read_in_full.tone == "settled"

    create_proposed_delta_group(
        session,
        project_id=project.id,
        source_family="ucm",
        source_revision="rev-1",
        document_id=document.id,
        deltas=(
            ProposedDeltaValues(
                change_type="modify",
                target=ExistingSubjectTarget(
                    subject_identity="UTL-001", field="clearance_date"
                ),
                accepted_value={"date": "2026-10-01"},
                proposed_value={"date": "2026-11-15"},
            ),
        ),
    )
    session.flush()

    rows, _register = _rows(session, project)
    row = next(iter(rows.values()))
    assert row.state == "processed"
    assert row.tone == "attention"
    assert row.state_words == "Processed — 1 proposed change still needs a decision"
    assert row.output is not None
    assert (row.output.proposed_changes, row.output.open_questions) == (1, 1)
    # The family it was recorded under, read from the group rather than guessed.
    assert row.source_family == "ucm"


def test_a_blocked_row_names_one_owner_and_the_next_action(
    session, project, store
):
    """Two owners, and a held file says what would change it rather than who."""

    _, held = _upload_and_confirm(session, project, marker="Held")
    quarantine_document(
        session,
        held.document_id,
        "document-asserted work sequencing is not modeled (#149)",
    )
    _receive(session, project, _matrix_pdf("Staged"), filename="staged.pdf")
    session.flush()

    rows, _register = _rows(session, project)
    waiting = next(
        row for row in rows.values() if row.state == AWAITING_CONFIRMATION
    )
    quarantined = next(
        row for row in rows.values() if row.state == "held_unmodeled"
    )

    assert waiting.owner == OWNER_PROJECT_TEAM
    assert "Confirm it for processing" in waiting.next_action
    assert quarantined.owner == OWNER_TECHNICAL_OPERATIONS
    assert "models the relationship" in quarantined.next_action
    assert "work sequencing is not modeled" in quarantined.recorded_reason
    # A row nobody is waiting on names nobody.
    _, read = _upload_and_confirm(session, project, marker="Ordinary")
    ordinary = next(
        row
        for row in _rows(session, project)[0].values()
        if row.document_id == read.document_id
    )
    assert (ordinary.owner, ordinary.next_action, ordinary.is_blocked) == ("", "", False)


def test_the_register_filters_by_state_family_and_time(session, project, store):
    """Three bounds, each answered from the rows' own recorded values."""

    _, confirmed = _upload_and_confirm(session, project, marker="Admitted")
    staged = _receive(session, project, _matrix_pdf("Staged"), filename="staged.pdf")
    session.flush()

    by_state = read_source_register(
        session,
        project_id=project.id,
        filters=RegisterFilters(state=AWAITING_CONFIRMATION),
    )
    assert [row.delivery_id for row in by_state.rows] == [staged.delivery_id]
    assert (by_state.matching, by_state.total) == (1, 2)

    by_family = read_source_register(
        session, project_id=project.id, filters=RegisterFilters(family="Admitted")
    )
    assert [row.document_id for row in by_family.rows] == [confirmed.document_id]

    received = session.scalars(
        select(SourceDelivery.received_at)
        .where(SourceDelivery.project_id == project.id)
        .order_by(SourceDelivery.id)
    ).all()
    tomorrow = max(received) + timedelta(days=1)
    assert (
        read_source_register(
            session,
            project_id=project.id,
            filters=RegisterFilters(received_from=tomorrow),
        ).rows
        == ()
    )
    assert (
        len(
            read_source_register(
                session,
                project_id=project.id,
                filters=RegisterFilters(
                    received_from=datetime(2000, 1, 1, tzinfo=timezone.utc),
                    received_to=tomorrow,
                ),
            ).rows
        )
        == 2
    )


def test_the_register_caps_a_page_and_offers_the_deliveries_before_it(
    session, project, store
):
    """The cap is stated, and the page before it is reachable with its filters."""

    for index in range(3):
        _receive(
            session,
            project,
            _matrix_pdf(f"Delivery {index}"),
            filename=f"delivery-{index}.pdf",
        )
    session.flush()

    first = read_source_register(session, project_id=project.id, limit=2)
    assert len(first.rows) == 2
    assert first.matching == 3
    assert first.has_older is True

    second = read_source_register(
        session, project_id=project.id, limit=2, before=first.oldest_key
    )
    assert len(second.rows) == 1
    assert second.has_older is False
    assert {row.key for row in first.rows}.isdisjoint({row.key for row in second.rows})
