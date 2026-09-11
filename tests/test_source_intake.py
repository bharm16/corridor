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
from corridor import audit
from corridor.due_work import (
    HANDLER_PROJECT_PROCESSING,
    HandlerContract,
    ProjectProcessingDeclaration,
    configure_due_work,
    enqueue_due_work,
    run_due_work_once,
)
from corridor import processing_holds
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
    SourceSegment,
)
from corridor.pipeline import EXTRACTED_PROPOSALS, ExtractionRoute
from corridor.principals import HumanPrincipal
import corridor.project_processing as project_processing
from corridor.project_processing import process_project, summarize_pass
from corridor.record_inclusion import record_inclusion_pending
import corridor.source_register as source_register
import corridor.source_intake as source_intake
from corridor.source_intake import (
    IntakeConflict,
    IntakeRefused,
    confirm_intake,
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


def test_confirm_can_register_without_reading_the_file(session, project, store):
    """#893: the act a person waits on registers; the standing pass reads.

    ``pending`` is not a new state -- it is the one a Document has had between
    registration and its read all along, and the one the source register
    already prints as waiting for the processing pass. What is new is that it
    survives the commit, so this asserts what a confirmed-but-unread source
    looks like on disk: no pages, no rendered derivatives, and no segments a
    read would have appended.
    """

    staged = validate_and_stage(_matrix_pdf(), "deferred.pdf")
    preview = preview_intake(session, project, staged, "matrix")

    result = confirm_intake(
        session,
        project=project,
        sha256=preview.sha256,
        filename=preview.filename,
        doc_type=preview.doc_type,
        binding_fingerprint=preview.binding_fingerprint,
        principal=PRINCIPAL,
        parse=False,
    )

    document = session.get(Document, result.document_id)
    assert document.parse_status == "pending"
    assert document.pages == 0
    assert (
        session.scalar(
            select(func.count())
            .select_from(DocPage)
            .where(DocPage.document_id == document.id)
        )
        == 0
    )
    assert (
        session.scalar(
            select(func.count())
            .select_from(SourceSegment)
            .where(SourceSegment.document_id == document.id)
        )
        == 0
    )
    # The attributable confirmation is written either way: what was deferred is
    # the reading, never the act.
    entry = session.get(AuditLog, result.audit_id)
    assert entry.action == audit.CONFIRM_SOURCE_INTAKE
    assert entry.entity_id == document.id


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
        configure_processing = configure_due_work(setup, declaration, now=now)
        schedule_id = configure_processing.id
        setup.commit()
    return schedule_id


def _project_with_org(factory, prefix: str) -> int:
    with factory() as setup:
        project = Project(
            slug=f"{prefix}-{uuid4().hex[:8]}", name=prefix.title(), is_synthetic=True
        )
        setup.add(project)
        setup.flush([project])
        project_id = project.id
        setup.add(ExternalOrg(name="Tejas Pipeline Co", aliases=[]))
        setup.commit()
    return project_id


def _confirm_upload(
    session,
    project_id: int,
    filename: str,
    body: bytes | None = None,
    doc_type: str = "matrix",
):
    """The web confirmation's own act: register, do not read (#893)."""

    project = session.get(Project, project_id)
    staged = validate_and_stage(_matrix_pdf() if body is None else body, filename)
    preview = preview_intake(session, project, staged, doc_type)
    return confirm_intake(
        session,
        project=project,
        sha256=preview.sha256,
        filename=preview.filename,
        doc_type=preview.doc_type,
        binding_fingerprint=preview.binding_fingerprint,
        principal=PRINCIPAL,
        parse=False,
    )


def _run_one_pass(factory, project_id, select_route, now):
    with factory() as ticking:
        enqueue_due_work(ticking, now=now)
        ticking.commit()
    return run_due_work_once(
        factory,
        clock=_Clock(now),
        owner="runtime:processing-worker",
        registry=_registry(select_route),
    )


def _page_and_segment_counts(session, document_id) -> tuple[int, int]:
    """The rows a read writes, asked of one document rather than of a project."""

    return (
        int(
            session.scalar(
                select(func.count())
                .select_from(DocPage)
                .where(DocPage.document_id == document_id)
            )
        ),
        int(
            session.scalar(
                select(func.count())
                .select_from(SourceSegment)
                .where(SourceSegment.document_id == document_id)
            )
        ),
    )


def _document_state(factory, project_id) -> tuple[str, int, int, int]:
    """Parse status, page count, ``doc_pages`` rows and ``source_segments`` rows."""

    with factory() as verify:
        document = verify.scalars(
            select(Document).where(Document.project_id == project_id)
        ).one()
        pages, segments = _page_and_segment_counts(verify, document.id)
        return document.parse_status, document.pages, pages, segments


def test_committed_upload_is_processed_by_the_standing_pass(
    runtime_database, tmp_path, monkeypatch
):
    """The whole handoff, across a process exit: confirm here, read and extract there.

    This is also the crash-between-confirmation-and-parse case (#893). The
    confirming process commits a Document nothing has read and then ends; the
    durable state it left is what the next worker selects on, so the read is
    not lost with the process that would have done it.
    """

    _stage_store(tmp_path, monkeypatch)
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 29, 7, 0, tzinfo=timezone.utc)
    project_id = _project_with_org(factory, "durable")

    # One process: an ordinary person uploads and confirms a matrix. It commits.
    with factory() as uploading:
        _confirm_upload(uploading, project_id, "committed.pdf")
        uploading.commit()

    # What the person's request left behind: registered, unread, and nothing
    # the reader would have written.
    assert _document_state(factory, project_id) == ("pending", 0, 0, 0)

    _declare_processing(factory, project_id, now)
    select_route, calls = _scripted_route({"committed.pdf": "FOC1-1"})

    # A later, separate process (the standing worker) reads it and extracts it.
    result = _run_one_pass(factory, project_id, select_route, now)

    assert result is not None
    assert result.execution_outcome == "completed"
    assert result.handler_result["parsed"] == 1
    assert calls["count"] == 1
    status, pages, doc_pages, segments = _document_state(factory, project_id)
    assert status == "parsed"
    assert pages == 1 and doc_pages == 1 and segments > 0
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


def test_a_second_pass_re_reads_nothing_and_captures_nothing_twice(
    runtime_database, tmp_path, monkeypatch
):
    """At-least-once delivery, exactly-once capture (#893).

    The runtime's contract is that an occurrence may run more than once, so the
    read has to be safe to repeat. It is selected on a status the first read
    committed away from, so the second pass finds nothing to read and the
    pages, the segments and the Extraction Run are the same rows, not another
    set of them.
    """

    _stage_store(tmp_path, monkeypatch)
    factory = runtime_database.session_factory
    first_at = datetime(2026, 8, 29, 7, 0, tzinfo=timezone.utc)
    second_at = datetime(2026, 8, 29, 8, 0, tzinfo=timezone.utc)
    project_id = _project_with_org(factory, "retried")

    with factory() as uploading:
        _confirm_upload(uploading, project_id, "retried.pdf")
        uploading.commit()
    _declare_processing(factory, project_id, first_at)
    select_route, calls = _scripted_route({"retried.pdf": "FOC1-1"})

    first = _run_one_pass(factory, project_id, select_route, first_at)
    after_first = _document_state(factory, project_id)
    second = _run_one_pass(factory, project_id, select_route, second_at)

    assert first.execution_outcome == "completed"
    assert second.execution_outcome == "completed"
    assert first.handler_result["parsed"] == 1
    assert second.handler_result["parsed"] == 0
    # One reading, one extraction, whatever the runtime delivered.
    assert calls["count"] == 1
    assert _document_state(factory, project_id) == after_first
    with factory() as verify:
        assert (
            verify.scalar(
                select(func.count())
                .select_from(ExtractionRun)
                .join(Document, Document.id == ExtractionRun.document_id)
                .where(Document.project_id == project_id)
            )
            == 1
        )
        assert (
            verify.scalar(
                select(func.count())
                .select_from(Dependency)
                .where(Dependency.project_id == project_id)
            )
            == 1
        )


def test_a_crash_during_the_read_leaves_the_document_for_the_next_pass(
    runtime_database, tmp_path, monkeypatch
):
    """A read that does not commit is a read that did not happen (#893).

    The worker is killed mid-read — modelled by the read raising after its
    transaction has begun. Nothing half-read may survive, and the document must
    still be selected by the next pass rather than sitting `pending` for ever
    or being written twice when the read finally succeeds.
    """

    _stage_store(tmp_path, monkeypatch)
    factory = runtime_database.session_factory
    first_at = datetime(2026, 8, 29, 7, 0, tzinfo=timezone.utc)
    second_at = datetime(2026, 8, 29, 8, 0, tzinfo=timezone.utc)
    project_id = _project_with_org(factory, "crashed")

    with factory() as uploading:
        _confirm_upload(uploading, project_id, "crashed.pdf")
        uploading.commit()
    _declare_processing(factory, project_id, first_at)
    select_route, calls = _scripted_route({"crashed.pdf": "FOC1-1"})

    honest = project_processing.parse_registered_document

    def crash_after_writing(session, *, document, images_dir):
        # Write the pages the real read would have written, then die: a rollback
        # that only undoes a no-op proves nothing.
        honest(session, document=document, images_dir=images_dir)
        raise RuntimeError("worker killed mid-read")

    monkeypatch.setattr(
        project_processing, "parse_registered_document", crash_after_writing
    )
    crashed = _run_one_pass(factory, project_id, select_route, first_at)

    # Nothing of the lost read survived, and the document is still the one the
    # next pass selects.
    assert _document_state(factory, project_id) == ("pending", 0, 0, 0)
    assert calls["count"] == 0
    assert crashed.handler_result["health"] == "processing_attention_required"

    monkeypatch.setattr(project_processing, "parse_registered_document", honest)
    recovered = _run_one_pass(factory, project_id, select_route, second_at)

    assert recovered.execution_outcome == "completed"
    assert recovered.handler_result["parsed"] == 1
    status, pages, doc_pages, segments = _document_state(factory, project_id)
    assert status == "parsed"
    assert pages == 1 and doc_pages == 1 and segments > 0
    assert calls["count"] == 1


def test_an_unreadable_source_fails_visibly_instead_of_waiting_for_ever(
    runtime_database, tmp_path, monkeypatch
):
    """The pass must not select the same unreadable source every hour (#893).

    A read the reader cannot complete ends `failed`, which is the state the
    bounded attributable re-parse owns and this selection does not. The register
    prints it as a read failure rather than as waiting, so a source nobody can
    read is somebody's to act on rather than a row that never changes.
    """

    _stage_store(tmp_path, monkeypatch)
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 29, 7, 0, tzinfo=timezone.utc)
    project_id = _project_with_org(factory, "unreadable")

    with factory() as uploading:
        confirmation = _confirm_upload(uploading, project_id, "unreadable.pdf")
        document_id = confirmation.document_id
        sha256 = confirmation.sha256
        uploading.commit()

    # The store loses the bytes between the confirmation and the read.
    staged = source_intake._resolve_staged(sha256)
    staged.unlink()

    _declare_processing(factory, project_id, now)
    select_route, calls = _scripted_route({"unreadable.pdf": "FOC1-1"})
    result = _run_one_pass(factory, project_id, select_route, now)

    assert result.handler_result["parsed"] == 0
    assert result.handler_result["health"] == "processing_attention_required"
    assert calls["count"] == 0
    with factory() as verify:
        assert verify.get(Document, document_id).parse_status == "failed"
        register = source_register.read_source_register(
            verify, project_id=project_id
        )
        assert [row.state for row in register.rows] == ["parse_failed"]
        assert register.rows[0].state_words == (
            "Failed to parse — the file could not be read"
        )


def _hold_reading(session, document_id):
    """One recorded restriction that prohibits reading this source (#919)."""

    return processing_holds.impose_hold(
        session,
        document_id=document_id,
        prohibited_stage=processing_holds.DOCUMENT_READING,
        reason_code=processing_holds.INTAKE_SECURITY_FINDING,
        reason="an intake check refused these bytes for rich reading",
        authority=processing_holds.INTAKE_SECURITY,
        imposed_by="tests.test_source_intake",
        evidence=f"documents.id={document_id}",
    )


def test_a_held_upload_is_skipped_by_the_pass_and_reported_rather_than_lost(
    runtime_database, tmp_path, monkeypatch
):
    """A restriction on *reading* stops the read act opening the bytes (#919).

    The read act selects on `pending`, so a document registered and restricted
    in the same request is selected by the very next pass. It asks which stage
    the restriction prohibits before it opens anything, and a restriction on
    document reading is where it stops.

    Three things have to be true at once, which is why they are one test: the
    restricted file is not opened, an ordinary sibling in the same pass still
    is, and the restricted one is reported rather than silently missing from
    every count.
    """

    _stage_store(tmp_path, monkeypatch)
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 29, 7, 0, tzinfo=timezone.utc)
    project_id = _project_with_org(factory, "held")

    with factory() as uploading:
        held = _confirm_upload(
            uploading,
            project_id,
            "sequencing.pdf",
            body=_matrix_pdf("Sequencing"),
        )
        ordinary = _confirm_upload(
            uploading, project_id, "ordinary.pdf", body=_matrix_pdf("Ordinary")
        )
        _hold_reading(uploading, held.document_id)
        uploading.commit()

    # The state the person's request left behind: both registered and unread,
    # and one of them already restricted.
    with factory() as verify:
        assert verify.get(Document, held.document_id).parse_status == "pending"
        standing = processing_holds.permission(verify, held.document_id)
        assert standing.may_read_document is False

    _declare_processing(factory, project_id, now)
    select_route, calls = _scripted_route({"ordinary.pdf": "FOC1-1"})
    result = _run_one_pass(factory, project_id, select_route, now)

    assert result.execution_outcome == "completed", result.error_code
    # One read, one restriction, and the restriction is not this pass's failure.
    assert result.handler_result["parsed"] == 1
    assert result.handler_result["held_unread"] == 1
    assert result.handler_result["health"] == "healthy"
    assert calls["count"] == 1

    with factory() as verify:
        # Nothing opened the restricted file: no status flip, no pages, no
        # segments.
        still_held = verify.get(Document, held.document_id)
        assert still_held.parse_status == "pending"
        assert still_held.pages == 0
        assert _page_and_segment_counts(verify, held.document_id) == (0, 0)
        # The ordinary sibling was read in the same pass.
        read = verify.get(Document, ordinary.document_id)
        assert read.parse_status == "parsed"
        pages, segments = _page_and_segment_counts(verify, ordinary.document_id)
        assert pages == 1 and segments > 0
        # And the register does not tell a coordinator to wait for a pass that
        # will skip it. The recorded reason is the record's own words; nothing
        # here says the file is dangerous.
        register = source_register.read_source_register(
            verify, project_id=project_id
        )
        rows = {row.document_id: row for row in register.rows}
        assert rows[held.document_id].state == source_register.READING_HELD
        assert rows[held.document_id].state_words == (
            "On hold — document reading is not permitted"
        )
        assert "refused these bytes" in rows[held.document_id].recorded_reason
        # Its sibling went the whole way in the same pass.
        assert rows[ordinary.document_id].state == "processed"


def test_a_held_document_stays_held_over_a_second_pass(
    runtime_database, tmp_path, monkeypatch
):
    """Skipping is not a retry, and repeating it never becomes a read (#919).

    The document the pass skipped is still `pending`, so the next pass selects
    it again. What must not happen is the skip quietly wearing off — and what
    must not happen either is the second pass losing it, which is how a
    restricted source becomes an invisible permanent pending state.
    """

    _stage_store(tmp_path, monkeypatch)
    factory = runtime_database.session_factory
    first_at = datetime(2026, 8, 29, 7, 0, tzinfo=timezone.utc)
    second_at = datetime(2026, 8, 29, 8, 0, tzinfo=timezone.utc)
    project_id = _project_with_org(factory, "stillheld")

    with factory() as uploading:
        held = _confirm_upload(uploading, project_id, "sequencing.pdf")
        _hold_reading(uploading, held.document_id)
        uploading.commit()

    _declare_processing(factory, project_id, first_at)
    select_route, calls = _scripted_route({})

    first = _run_one_pass(factory, project_id, select_route, first_at)
    second = _run_one_pass(factory, project_id, select_route, second_at)

    assert first.handler_result["held_unread"] == 1
    assert second.handler_result["held_unread"] == 1
    assert (first.handler_result["parsed"], second.handler_result["parsed"]) == (0, 0)
    assert calls["count"] == 0
    with factory() as verify:
        document = verify.get(Document, held.document_id)
        assert document.parse_status == "pending"
        assert _page_and_segment_counts(verify, held.document_id) == (0, 0)


def test_a_schedule_held_against_extraction_is_read_by_the_pass(
    runtime_database, tmp_path, monkeypatch
):
    """The reading the maintainer's ruling permits, on the worker path (#919).

    `schedule` is an accepted kind on the confirmation route, and registration
    records the unmodeled-sequencing restriction in the same transaction. That
    restriction is on *interpreting* the document, not on opening it, so the
    standing pass reads the file into pages and segments like any other source
    — and the register says both halves: the document was read, and extraction
    is on hold. The predecessor skipped it and left it permanently unread.
    """

    _stage_store(tmp_path, monkeypatch)
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 29, 7, 0, tzinfo=timezone.utc)
    project_id = _project_with_org(factory, "schedule")

    with factory() as uploading:
        schedule = _confirm_upload(
            uploading,
            project_id,
            "sequencing.pdf",
            body=_matrix_pdf("Sequencing"),
            doc_type="schedule",
        )
        uploading.commit()

    _declare_processing(factory, project_id, now)
    select_route, calls = _scripted_route({})
    result = _run_one_pass(factory, project_id, select_route, now)

    assert result.execution_outcome == "completed", result.error_code
    assert result.handler_result["parsed"] == 1
    assert result.handler_result["held_unread"] == 0
    assert result.handler_result["health"] == "healthy"
    # Read, and never handed to an extractor.
    assert calls["count"] == 0

    with factory() as verify:
        document = verify.get(Document, schedule.document_id)
        assert document.parse_status == "parsed"
        pages, segments = _page_and_segment_counts(verify, schedule.document_id)
        assert pages == 1 and segments > 0
        standing = processing_holds.permission(verify, schedule.document_id)
        assert (standing.may_read_document, standing.may_extract_semantics) == (
            True,
            False,
        )
        register = source_register.read_source_register(
            verify, project_id=project_id
        )
        rows = {row.document_id: row for row in register.rows}
        assert rows[schedule.document_id].state == (
            source_register.READ_EXTRACTION_HELD
        )
        assert rows[schedule.document_id].state_words == (
            "Document read. Extraction is on hold."
        )


def test_nothing_is_visible_to_another_connection_before_the_confirm_commits(
    runtime_database, tmp_path, monkeypatch
):
    """Work becomes visible when the confirmation commits, and not before (#893).

    Two real connections, because that is the only way to ask the question: one
    holds an uncommitted confirmation, the other is the worker's own selection
    query. Before the commit the worker sees no document to read; after it, it
    sees exactly one.
    """

    _stage_store(tmp_path, monkeypatch)
    factory = runtime_database.session_factory
    project_id = _project_with_org(factory, "uncommitted")

    def pending_documents() -> list[int]:
        with factory() as worker:
            return list(
                worker.scalars(
                    select(Document.id).where(
                        Document.project_id == project_id,
                        Document.parse_status == "pending",
                    )
                )
            )

    with factory() as uploading:
        _confirm_upload(uploading, project_id, "uncommitted.pdf")
        # Written, flushed, and invisible to anybody else.
        assert pending_documents() == []
        uploading.commit()

    assert len(pending_documents()) == 1


def test_a_rolled_back_upload_hands_off_no_work(
    runtime_database, tmp_path, monkeypatch
):
    _stage_store(tmp_path, monkeypatch)
    factory = runtime_database.session_factory
    now = datetime(2026, 8, 29, 9, 0, tzinfo=timezone.utc)
    project_id = _project_with_org(factory, "rollback")

    # Stage bytes (content-addressed, harmless) then confirm and ROLL BACK.
    with factory() as aborting:
        _confirm_upload(aborting, project_id, "aborted.pdf")
        aborting.rollback()

    _declare_processing(factory, project_id, now)
    select_route, calls = _scripted_route({"aborted.pdf": "FOC1-1"})
    result = _run_one_pass(factory, project_id, select_route, now)

    assert calls["count"] == 0
    # Nothing to read either: a rolled-back confirmation hands off no read and
    # no extraction (#893).
    assert result.handler_result["parsed"] == 0
    assert result.handler_result["health"] == "healthy"
    with factory() as verify:
        assert (
            verify.scalar(
                select(func.count())
                .select_from(Document)
                .where(Document.project_id == project_id)
            )
            == 0
        )
