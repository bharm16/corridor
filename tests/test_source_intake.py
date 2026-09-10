"""Product source intake: bounded upload, read-only preview, attributable confirm.

Two kinds of test live here. The pure validation, preview, confirmation, and
refusal behavior runs on an ordinary rollback-scoped ``session`` against a
temporary content-addressed store. The durable handoff — that a committed upload
is processed by the standing project-processing pass across a process exit and a
rolled-back one hands off no work — needs real committed transactions, so it uses
the harness-owned ``runtime_database`` and drives the shared Due Work runtime with
a controlled extraction route (no live model, ADR-0046).
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from corridor.config import settings
from corridor.db import Session, engine
from corridor import audit
from corridor.due_work import (
    HANDLER_PROJECT_PROCESSING,
    HandlerContract,
    ProjectProcessingDeclaration,
    configure_project_processing,
    enqueue_due_work,
    run_due_work_once,
)
from corridor.extraction_runs import record_extraction_run
from corridor.intake_hardening import HostileContentRefused, inspect_byte_gate
from corridor.models import (
    AuditLog,
    Candidate,
    Dependency,
    DocPage,
    Document,
    ExternalOrg,
    ExtractionRun,
    Project,
)
from corridor.pipeline import EXTRACTED_PROPOSALS, ExtractionRoute
from corridor.principals import HumanPrincipal
from corridor.project_processing import process_project, summarize_pass
from corridor.record_inclusion import record_inclusion_pending
import corridor.source_intake as source_intake
from corridor.source_intake import (
    IntakeConflict,
    IntakeRefused,
    confirm_intake,
    list_confirmed_uploads,
    preview_intake,
    validate_and_stage,
)

from pdf_fixture_support import PdfFixture

PRINCIPAL = HumanPrincipal("local:uploader")
PROMPT_VERSION = "matrix_v1"
SCHEMA_VERSION = "matrix_candidate_shape_v1"
MODEL = "gpt-test"
EXTRACTOR_IDENTITY = "deployed-matrix-v1"


def _matrix_pdf(marker: str = "AT&T Texas (SWBT)") -> bytes:
    """A synthetic matrix PDF. Exercises the pipeline only, never a quality claim."""

    fixture = PdfFixture()
    # The intake proof needs the authored content, not a full sheet of blank raster.
    page = fixture.add_page(height=180)
    page.text((72, 100), "Utility Conflict Matrix — segment 3C2")
    page.text(
        (72, 130),
        f"FOC1-1  {marker}  Telecom  underground fiber  STA 1149+00 to 1153+17",
    )
    return fixture.tobytes()


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the content-addressed store and page renders at a temp directory."""

    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))
    return tmp_path / "files"


@pytest.fixture
def session():
    connection = engine.connect()
    trans = connection.begin()
    s = Session(bind=connection)
    yield s
    s.close()
    trans.rollback()
    connection.close()


@pytest.fixture
def project(session):
    p = Project(slug=f"intake-{uuid4().hex[:8]}", name="Intake Test", is_synthetic=True)
    session.add(p)
    session.flush()
    return p


def _stored_files(store_dir) -> list:
    return list(store_dir.rglob("*")) if store_dir.exists() else []


# --- Bounded limits: refuse foreign/oversized/unsupported before any registration


def test_stage_accepts_a_pdf_and_stores_its_exact_bytes(store):
    body = _matrix_pdf()
    staged = validate_and_stage(body, "conflict-matrix.pdf")

    assert staged.sha256 == hashlib.sha256(body).hexdigest()
    assert staged.size_bytes == len(body)
    assert staged.filename == "conflict-matrix.pdf"
    assert staged.stored_path.read_bytes() == body


def test_stage_is_content_addressed_and_never_duplicates(store):
    body = _matrix_pdf()
    first = validate_and_stage(body, "a.pdf")
    second = validate_and_stage(body, "b.pdf")

    assert first.stored_path == second.stored_path
    assert len([p for p in _stored_files(store) if p.is_file()]) == 1


def test_stage_refuses_an_unsupported_type_with_no_side_effects(store):
    with pytest.raises(IntakeRefused) as excinfo:
        validate_and_stage(b"just some notes", "notes.txt")

    assert excinfo.value.reason == "unsupported_type"
    assert _stored_files(store) == []


def test_stage_refuses_foreign_content_that_lies_about_its_suffix(store):
    with pytest.raises(IntakeRefused) as excinfo:
        validate_and_stage(b"this is not really a pdf", "report.pdf")

    assert excinfo.value.reason == "magic_mismatch"
    assert "do not match" in str(excinfo.value)
    assert _stored_files(store) == []


def test_stage_refuses_an_oversized_file_with_no_side_effects(store, monkeypatch):
    monkeypatch.setattr(source_intake, "MAX_UPLOAD_BYTES", 1024)
    body = _matrix_pdf()
    assert len(body) > 1024

    with pytest.raises(IntakeRefused) as excinfo:
        validate_and_stage(body, "big.pdf")

    assert excinfo.value.reason == "size_limit_exceeded"
    assert "the limit is" in str(excinfo.value)
    assert _stored_files(store) == []


def test_the_upload_channel_refuses_in_the_shared_byte_gate_vocabulary(store):
    """One gate, one vocabulary, whatever transport carried the bytes.

    The upload path enforced its own size limit and its own magic-byte check
    beside the shared byte gate that already enforces both, so identical
    hostile bytes were refused as ``too_large`` here and
    ``size_limit_exceeded`` on the push and pull channels — the same refusal
    under two names, in a record whose whole purpose is saying what was refused
    and why.  The rule is now the gate's on every channel; the sentence a
    person reads stays the upload's own.
    """

    cases = [
        (b"", "empty.pdf", None),
        (b"this is not really a pdf", "report.pdf", None),
        (b"%PDF-" + b"x" * 4096, "big.pdf", 1024),
    ]
    for body, filename, limit in cases:
        with pytest.raises(HostileContentRefused) as gate:
            inspect_byte_gate(body, filename, **({} if limit is None else {"max_bytes": limit}))
        with pytest.raises(IntakeRefused) as upload:
            validate_and_stage(body, filename, max_bytes=limit)

        assert upload.value.reason == gate.value.rule
    assert _stored_files(store) == []


def test_the_upload_keeps_its_own_extra_structural_stage(store):
    """The gate composition is shared; the upload's extra stage is not dropped.

    ``inspect_sandboxed_structure`` is stage 2 of #490 and the upload runs it
    because an upload is the one channel that hands the bytes straight to a
    rich parser on confirmation.  Collapsing the duplicated stage-1 checks must
    not quietly collapse this one too.
    """

    bomb = _zip_with_xml_entity_bomb()
    with pytest.raises(IntakeRefused) as excinfo:
        validate_and_stage(bomb, "workbook.xlsx")

    assert excinfo.value.reason == "xml_entity_bomb"
    assert _stored_files(store) == []


def _zip_with_xml_entity_bomb() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            '<!DOCTYPE x [<!ENTITY a "aaaa">]><workbook/>',
        )
    return buffer.getvalue()


def test_stage_reduces_a_path_like_name_to_a_safe_basename(store):
    staged = validate_and_stage(_matrix_pdf(), "../../etc/matrix.pdf")

    assert staged.filename == "matrix.pdf"


def test_stage_refuses_an_empty_filename(store):
    with pytest.raises(IntakeRefused) as excinfo:
        validate_and_stage(_matrix_pdf(), "   ")

    assert excinfo.value.reason == "unsafe_filename"


# --- Preview: known facts read-only, unresolved metadata explicit


def test_preview_shows_known_facts_and_leaves_metadata_unresolved(session, project, store):
    staged = validate_and_stage(_matrix_pdf(), "matrix.pdf")
    preview = preview_intake(session, project, staged, "matrix")

    assert preview.sha256 == staged.sha256
    assert preview.doc_type == "matrix"
    assert preview.format_label == "PDF"
    assert preview.already_registered is False
    assert preview.binding_fingerprint
    fields = {item.field for item in preview.unresolved}
    assert {"registry_id", "doc_date", "supersession", "rendition_of"} <= fields


def test_preview_of_an_unknown_doc_type_is_refused(session, project, store):
    staged = validate_and_stage(_matrix_pdf(), "matrix.pdf")
    with pytest.raises(IntakeRefused) as excinfo:
        preview_intake(session, project, staged, "manifest")

    assert excinfo.value.reason == "unknown_doc_type"


# --- Confirm: attributable, registers, hands off through the standing pass only


def test_confirm_registers_the_document_and_binds_the_actor(session, project, store):
    staged = validate_and_stage(_matrix_pdf(), "matrix.pdf")
    preview = preview_intake(session, project, staged, "matrix")

    result = confirm_intake(
        session,
        project=project,
        sha256=preview.sha256,
        filename=preview.filename,
        doc_type=preview.doc_type,
        binding_fingerprint=preview.binding_fingerprint,
        principal=PRINCIPAL,
    )

    document = session.get(Document, result.document_id)
    assert result.created is True
    assert document.project_id == project.id
    assert document.sha256 == staged.sha256
    assert document.doc_type == "matrix"
    assert document.parse_status == "parsed"
    assert document.pages and document.pages >= 1
    # Provenance stays exact: nothing about upload implies a replacement.
    assert document.superseded_by is None
    assert document.supersession_source_document_id is None

    entry = session.get(AuditLog, result.audit_id)
    assert entry.action == audit.CONFIRM_SOURCE_INTAKE
    assert entry.entity_type == audit.DOCUMENT
    assert entry.entity_id == document.id
    assert entry.human_principal == PRINCIPAL.subject


def test_confirm_does_not_bump_the_record_inclusion_watermark(session, project, store):
    # The only producer of the Record Inclusion watermark is a completed
    # Extraction Run (#342). Registering an unextracted upload must not mark the
    # project pending, or an idle load would append Policy Runs for nothing.
    staged = validate_and_stage(_matrix_pdf(), "matrix.pdf")
    preview = preview_intake(session, project, staged, "matrix")
    confirm_intake(
        session,
        project=project,
        sha256=preview.sha256,
        filename=preview.filename,
        doc_type=preview.doc_type,
        binding_fingerprint=preview.binding_fingerprint,
        principal=PRINCIPAL,
    )

    assert record_inclusion_pending(session, project.id) is False


def test_confirm_is_idempotent_on_identical_bytes(session, project, store):
    staged = validate_and_stage(_matrix_pdf(), "matrix.pdf")
    preview = preview_intake(session, project, staged, "matrix")
    kwargs = dict(
        project=project,
        sha256=preview.sha256,
        filename=preview.filename,
        doc_type=preview.doc_type,
        binding_fingerprint=preview.binding_fingerprint,
        principal=PRINCIPAL,
    )

    first = confirm_intake(session, **kwargs)
    second = confirm_intake(session, **kwargs)

    assert first.document_id == second.document_id
    assert first.created is True
    assert second.created is False
    count = session.scalar(
        select(func.count())
        .select_from(Document)
        .where(Document.project_id == project.id, Document.sha256 == staged.sha256)
    )
    assert count == 1


def test_different_bytes_are_kept_as_distinct_documents(session, project, store):
    one = validate_and_stage(_matrix_pdf("Owner One"), "one.pdf")
    two = validate_and_stage(_matrix_pdf("Owner Two"), "two.pdf")
    assert one.sha256 != two.sha256

    for staged in (one, two):
        preview = preview_intake(session, project, staged, "matrix")
        confirm_intake(
            session,
            project=project,
            sha256=preview.sha256,
            filename=preview.filename,
            doc_type=preview.doc_type,
            binding_fingerprint=preview.binding_fingerprint,
            principal=PRINCIPAL,
        )

    shas = set(
        session.scalars(
            select(Document.sha256).where(Document.project_id == project.id)
        )
    )
    assert shas == {one.sha256, two.sha256}


def test_confirm_refuses_a_tampered_binding(session, project, store):
    staged = validate_and_stage(_matrix_pdf(), "matrix.pdf")
    preview = preview_intake(session, project, staged, "matrix")

    with pytest.raises(IntakeConflict) as excinfo:
        confirm_intake(
            session,
            project=project,
            sha256=preview.sha256,
            filename=preview.filename,
            doc_type="minutes",  # tampered away from the previewed kind
            binding_fingerprint=preview.binding_fingerprint,
            principal=PRINCIPAL,
        )

    assert excinfo.value.reason == "binding_mismatch"
    assert session.scalar(select(func.count()).select_from(Document)) == 0


def test_confirm_refuses_tampered_staged_bytes(session, project, store):
    staged = validate_and_stage(_matrix_pdf(), "matrix.pdf")
    preview = preview_intake(session, project, staged, "matrix")
    staged.stored_path.write_bytes(_matrix_pdf("Different Owner"))

    with pytest.raises(IntakeConflict) as excinfo:
        confirm_intake(
            session,
            project=project,
            sha256=preview.sha256,
            filename=preview.filename,
            doc_type=preview.doc_type,
            binding_fingerprint=preview.binding_fingerprint,
            principal=PRINCIPAL,
        )

    assert excinfo.value.reason == "bytes_tampered"
    assert session.scalar(select(func.count()).select_from(Document)) == 0


def test_confirm_refuses_a_concurrent_type_conflict(session, project, store):
    staged = validate_and_stage(_matrix_pdf(), "matrix.pdf")
    as_minutes = preview_intake(session, project, staged, "minutes")
    confirm_intake(
        session,
        project=project,
        sha256=as_minutes.sha256,
        filename=as_minutes.filename,
        doc_type="minutes",
        binding_fingerprint=as_minutes.binding_fingerprint,
        principal=PRINCIPAL,
    )

    as_matrix = preview_intake(session, project, staged, "matrix")
    assert as_matrix.type_conflict is True
    with pytest.raises(IntakeConflict) as excinfo:
        confirm_intake(
            session,
            project=project,
            sha256=as_matrix.sha256,
            filename=as_matrix.filename,
            doc_type="matrix",
            binding_fingerprint=as_matrix.binding_fingerprint,
            principal=PRINCIPAL,
        )

    assert excinfo.value.reason == "type_conflict"
    documents = session.scalars(
        select(Document).where(Document.project_id == project.id)
    ).all()
    assert len(documents) == 1
    assert documents[0].doc_type == "minutes"


def test_confirm_refuses_a_cross_project_binding(session, store):
    project_a = Project(slug=f"a-{uuid4().hex[:8]}", name="A", is_synthetic=True)
    project_b = Project(slug=f"b-{uuid4().hex[:8]}", name="B", is_synthetic=True)
    session.add_all([project_a, project_b])
    session.flush()

    staged = validate_and_stage(_matrix_pdf(), "matrix.pdf")
    preview_a = preview_intake(session, project_a, staged, "matrix")

    # The same previewed source and fingerprint, aimed at another project.
    with pytest.raises(IntakeConflict) as excinfo:
        confirm_intake(
            session,
            project=project_b,
            sha256=preview_a.sha256,
            filename=preview_a.filename,
            doc_type=preview_a.doc_type,
            binding_fingerprint=preview_a.binding_fingerprint,
            principal=PRINCIPAL,
        )

    assert excinfo.value.reason == "binding_mismatch"
    assert (
        session.scalar(
            select(func.count())
            .select_from(Document)
            .where(Document.project_id == project_b.id)
        )
        == 0
    )


# --- The uploads list reports honest, derived processing status


def _confirm(session, project, doc_type="matrix", marker="Owner"):
    staged = validate_and_stage(_matrix_pdf(marker), f"{marker}.pdf")
    preview = preview_intake(session, project, staged, doc_type)
    return confirm_intake(
        session,
        project=project,
        sha256=preview.sha256,
        filename=preview.filename,
        doc_type=preview.doc_type,
        binding_fingerprint=preview.binding_fingerprint,
        principal=PRINCIPAL,
    )


def test_uploads_list_reports_pending_processed_and_failed(session, project, store):
    pending = _confirm(session, project, marker="Pending")
    processed = _confirm(session, project, marker="Processed")
    failed = _confirm(session, project, marker="Failed")

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
    session.flush()

    rows = {row.document_id: row for row in list_confirmed_uploads(session, project.id)}
    assert rows[pending.document_id].processing_status == "pending"
    assert rows[processed.document_id].processing_status == "processed"
    assert rows[failed.document_id].processing_status == "parse_failed"
    assert rows[pending.document_id].confirmed_by == PRINCIPAL.subject


def test_uploads_list_is_scoped_to_confirmed_uploads(session, project, store):
    # A document registered another way (no confirmation receipt) is not listed.
    other = Document(
        project_id=project.id,
        sha256=hashlib.sha256(b"corpus-fetched").hexdigest(),
        filename="from-corpus.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(other)
    session.flush()
    confirmed = _confirm(session, project, marker="Uploaded")

    listed = {row.document_id for row in list_confirmed_uploads(session, project.id)}
    assert listed == {confirmed.document_id}


# --- Durable handoff: the standing pass processes a committed upload; a
# --- rolled-back one hands off no work (real committed transactions).


def _scripted_route(script):
    calls = {"count": 0}

    def select_route(document):
        def extract(session, target):
            calls["count"] += 1
            fields = {
                "utility_id": script[target.filename],
                "external_org": "Tejas Pipeline Co",
                "station_from": "1149+00",
            }
            candidate = Candidate(
                project_id=target.project_id,
                kind="dependency",
                payload_json={
                    "kind": "dependency",
                    "fields": fields,
                    "citations": [
                        {
                            "document_id": target.id,
                            "page": 1,
                            "quote": " ".join(fields.values()),
                            "verified": True,
                            "whole_row": True,
                        }
                    ],
                    "confidence": 1.0,
                    "unverified_fields": [],
                    "low_confidence_tokens": [],
                    "dedupe_hint": script[target.filename],
                },
                source_document_id=target.id,
                source_pages=[1],
                confidence=1.0,
                prompt_version=PROMPT_VERSION,
                citations_verified=True,
                model=MODEL,
            )
            session.add(candidate)
            session.flush([candidate])
            return [candidate]

        return ExtractionRoute(
            output=EXTRACTED_PROPOSALS,
            effective_prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
            extract=extract,
            model=MODEL,
            allow_unsealed_legacy=True,
        )

    return select_route, calls


def _registry(select_route):
    def run_effectful(context):
        result = process_project(
            context.session_factory,
            project_id=context.schedule.project_id,
            select_route=select_route,
            clock=context.clock,
        )
        return summarize_pass(
            result,
            configuration_version=context.schedule.configuration_version,
            observed_at=context.clock.now(),
        )

    return {
        HANDLER_PROJECT_PROCESSING: HandlerContract(
            key=HANDLER_PROJECT_PROCESSING,
            scope_kind="one_registered_project_extraction",
            idempotency_contract="at_least_once_reconcilable",
            max_result_bytes=8192,
            model_token_budget=100_000_000,
            notification_budget=0,
            run_effectful=run_effectful,
        )
    }


class _Clock:
    def __init__(self, value):
        self.value = value

    def now(self):
        return self.value


def _stage_store(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "corpus_store", str(tmp_path / "files"))
    monkeypatch.setattr(settings, "corpus_images", str(tmp_path / "images"))


def _declare_processing(factory, project_id, now):
    with factory() as setup:
        declaration = ProjectProcessingDeclaration.released_hourly(
            project_id=project_id,
            configuration_version="project-processing-v1",
            extractor_identity=EXTRACTOR_IDENTITY,
            starts_at=now.replace(minute=0, second=0, microsecond=0),
        )
        configure_processing = configure_project_processing(setup, declaration, now=now)
        schedule_id = configure_processing.id
        setup.commit()
    return schedule_id


def test_committed_upload_is_processed_by_the_standing_pass(
    runtime_database, tmp_path, monkeypatch
):
    _stage_store(tmp_path, monkeypatch)
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 29, 7, 0, tzinfo=timezone.utc)

    # One process: an ordinary person uploads and confirms a matrix. It commits.
    with factory() as uploading:
        project = Project(
            slug=f"durable-{uuid4().hex[:8]}", name="Durable", is_synthetic=True
        )
        uploading.add(project)
        uploading.flush([project])
        project_id = project.id
        uploading.add(ExternalOrg(name="Tejas Pipeline Co", aliases=[]))
        uploading.flush()
        staged = validate_and_stage(_matrix_pdf(), "committed.pdf")
        preview = preview_intake(uploading, project, staged, "matrix")
        confirm_intake(
            uploading,
            project=project,
            sha256=preview.sha256,
            filename=preview.filename,
            doc_type=preview.doc_type,
            binding_fingerprint=preview.binding_fingerprint,
            principal=PRINCIPAL,
        )
        uploading.commit()

    _declare_processing(factory, project_id, now)
    select_route, calls = _scripted_route({"committed.pdf": "FOC1-1"})

    # A later, separate process (the standing worker) picks it up and extracts it.
    with factory() as ticking:
        enqueue_due_work(ticking, now=now)
        ticking.commit()
    result = run_due_work_once(
        factory,
        clock=_Clock(now),
        owner="runtime:processing-worker",
        registry=_registry(select_route),
    )

    assert result is not None
    assert result.execution_outcome == "completed"
    assert calls["count"] == 1
    with factory() as verify:
        outcome = verify.scalar(
            select(ExtractionRun.outcome)
            .join(Document, Document.id == ExtractionRun.document_id)
            .where(Document.project_id == project_id)
        )
        assert outcome == "completed"
        refs = set(
            verify.scalars(
                select(Dependency.source_ref).where(
                    Dependency.project_id == project_id
                )
            )
        )
        assert "FOC1-1" in refs


def test_a_rolled_back_upload_hands_off_no_work(
    runtime_database, tmp_path, monkeypatch
):
    _stage_store(tmp_path, monkeypatch)
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc)

    with factory() as setup:
        project = Project(
            slug=f"rollback-{uuid4().hex[:8]}", name="Rollback", is_synthetic=True
        )
        setup.add(project)
        setup.flush([project])
        project_id = project.id
        setup.commit()

    # Stage bytes (content-addressed, harmless) then confirm and ROLL BACK.
    with factory() as aborting:
        project = aborting.get(Project, project_id)
        staged = validate_and_stage(_matrix_pdf(), "aborted.pdf")
        preview = preview_intake(aborting, project, staged, "matrix")
        confirm_intake(
            aborting,
            project=project,
            sha256=preview.sha256,
            filename=preview.filename,
            doc_type=preview.doc_type,
            binding_fingerprint=preview.binding_fingerprint,
            principal=PRINCIPAL,
        )
        aborting.rollback()

    _declare_processing(factory, project_id, now)
    select_route, calls = _scripted_route({"aborted.pdf": "FOC1-1"})

    with factory() as ticking:
        enqueue_due_work(ticking, now=now)
        ticking.commit()
    run_due_work_once(
        factory,
        clock=_Clock(now),
        owner="runtime:processing-worker",
        registry=_registry(select_route),
    )

    assert calls["count"] == 0
    with factory() as verify:
        assert (
            verify.scalar(
                select(func.count())
                .select_from(Document)
                .where(Document.project_id == project_id)
            )
            == 0
        )
