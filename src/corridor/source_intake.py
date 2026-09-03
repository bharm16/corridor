"""Receive one source Document through the product and confirm its registration.

Upload is the explicit rare fallback for information that never arrived any other
way — paper handed over at a meeting (ADR-0058). The primary front door is the
per-project email address (#372), which reuses everything here: the same bounded
limits, the same read-only preview, and the same durable processing handoff. So
the reusable primitives live in this non-web module and the web routes are thin
adapters over them; an email attachment is the same intake as an uploaded file
with a different envelope.

What did not exist before was a boundary that let an ordinary person hand Corridor
a document without an internal filesystem path or a command, *see what registering
it would do*, and only then commit that registration attributably (ADR-0035,
ADR-0039). Composing the pieces that already exist is the whole design:

- ``ingest_document`` registers and parses one file; it is idempotent on identical
  bytes and never overwrites an earlier file (``corridor.ingest``).
- Extraction of a registered document is the standing gate-7 project-processing
  pass's job (``process_project`` driven by the shared Due Work runtime, #342). A
  committed, parsed, eligible Document is therefore *already* handed off: the next
  scheduled pass extracts it, a crash between commit and that pass cannot lose it,
  and a rolled-back confirm leaves nothing for the pass to find. This module does
  **not** bump the Record Inclusion watermark — that producer is a completed
  Extraction Run (``corridor.record_inclusion``), and marking an unextracted upload
  pending would append idle Policy Runs the shared runtime is built to avoid.

What this module refuses to do is as load-bearing as what it does. Identical bytes,
a filename, an upload time, or model opinion never establish that two files are the
same Document, one supersedes another, or one is a rendition of another (ADR-0015).
Upload registers exactly one Document with exact provenance; a replacement remains a
separate cited human confirmation through the supersession path, never inferred
here.

Two acts, each honest on its own:

1. ``validate_and_stage`` enforces the bounded limits (one file, accepted type,
   size) and writes the exact bytes to the content-addressed store *before any
   model work*. An oversized, unsupported, or foreign file is refused here with an
   actionable reason and leaves nothing registered.
2. ``preview_intake`` reads the current registry state and reports, read-only, what
   confirming would create or change — including that a genuinely unresolved fact
   (registry id, date, any relationship) stays unresolved rather than silently
   becoming authoritative configuration. ``confirm_intake`` binds that exact
   previewed source and the acting person, refusing a stale, tampered, concurrent,
   or cross-project request without any partial authoritative change, and otherwise
   registers the Document in the caller's transaction so a rolled-back caller hands
   off no work.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor import audit
from corridor.analytics import (
    default_binding,
    emit_event,
    source_arrival_event,
    source_capture_event,
)
from corridor.config import settings
from corridor.ingest import SPREADSHEET_SUFFIXES, ingest_document
from corridor.models import (
    DOC_TYPES,
    AuditLog,
    DocumentQuarantine,
    Document,
    ExtractionRun,
    Project,
)
from corridor.object_storage import store_bytes
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.storage import staged_file

# One uploaded file per request is the count bound; a batch caller (email) loops
# this module per attachment. 64 MiB holds a large utility-conflict matrix PDF or
# a workbook with room to spare while refusing an unbounded upload outright.
MAX_UPLOAD_BYTES = 64 * 1024 * 1024

# Exactly the formats the ingest pipeline can read (``corridor.ingest``): a PDF, or
# a workbook that is the structured original (ADR-0005). Everything else is refused
# at the door rather than registered as a document nobody can parse.
ACCEPTED_SUFFIXES = frozenset({".pdf"}) | SPREADSHEET_SUFFIXES

# The declared semantic kind of the source, chosen by the person handing it over —
# a question the bytes cannot answer and the system must not guess (ADR-0007,
# ADR-0030). The full document vocabulary is offered; the model never classifies it.
ACCEPTED_DOC_TYPES = frozenset(DOC_TYPES)

# The first bytes each accepted format actually begins with, so a file whose name
# claims one format but whose content is another is refused as foreign before it is
# ever handed to a parser.
_PDF_MAGIC = b"%PDF-"
_ZIP_MAGIC = b"PK\x03\x04"  # xlsx / xlsm are Zip containers.


class IntakeRefused(ValueError):
    """A bounded, actionable refusal raised before any registration.

    ``reason`` is a stable machine code (``too_large``, ``unsupported_type``,
    ``content_mismatch``, ``empty_file``, ``unsafe_filename``, ``unknown_doc_type``)
    so an adapter can branch on it; ``str(exc)`` is the human sentence.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class IntakeConflict(ValueError):
    """A confirm that no longer matches the source or registry it previewed.

    Raised for a stale, tampered, concurrent, or cross-project confirmation before
    any authoritative write. ``reason`` is one of ``binding_mismatch``,
    ``bytes_missing``, ``bytes_tampered``, ``type_conflict``.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class StagedSource:
    """Exact bytes at rest in the content-addressed store, ready to preview."""

    sha256: str
    size_bytes: int
    suffix: str
    filename: str
    stored_path: Path


@dataclass(frozen=True)
class UnresolvedFact:
    """One registration fact this upload deliberately leaves unresolved."""

    field: str
    note: str


@dataclass(frozen=True)
class IntakePreview:
    """What confirming this staged source would create or change, read-only.

    ``known`` facts are exact and shown as-is. ``unresolved`` names the metadata that
    stays unknown rather than becoming authoritative. ``already_registered`` is the
    idempotent case: these exact bytes are already a Document here, so confirming
    creates nothing. ``type_conflict`` flags the concurrent case a confirm refuses.
    ``binding_fingerprint`` travels to ``confirm_intake`` and pins the exact source
    and declared kind the person saw.
    """

    project_slug: str
    project_id: int
    filename: str
    doc_type: str
    format_label: str
    sha256: str
    size_bytes: int
    already_registered: bool
    existing_document_id: int | None
    existing_doc_type: str | None
    existing_parse_status: str | None
    type_conflict: bool
    unresolved: tuple[UnresolvedFact, ...]
    binding_fingerprint: str


@dataclass(frozen=True)
class IntakeConfirmation:
    """The attributable outcome of one confirmed intake."""

    document_id: int
    sha256: str
    doc_type: str
    created: bool
    audit_id: int


@dataclass(frozen=True)
class UploadedSourceRow:
    """One confirmed upload with its source facts and derived processing status."""

    document_id: int
    filename: str
    doc_type: str
    sha256: str
    parse_status: str
    pages: int | None
    processing_status: str
    confirmed_by: str
    confirmed_at: datetime


def validate_and_stage(
    body: bytes,
    filename: str,
    *,
    max_bytes: int | None = None,
    customer_id: str | None = None,
    project_id: int | str | None = None,
    channel: str = "upload",
) -> StagedSource:
    """Enforce the bounded limits and stage exact bytes; refuse before model work.

    Refuses an empty, oversized, wrong-suffix, or content-mismatched file with an
    ``IntakeRefused`` and writes nothing. Upload and email callers use the 64 MiB
    default; a connected location supplies its separately validated Gate-7 byte
    bound. On acceptance the exact bytes land in the content-addressed store keyed
    by their own hash — writing identical bytes twice is a no-op, so a re-uploaded
    file is never duplicated and an earlier file is never overwritten.
    """

    limit = MAX_UPLOAD_BYTES if max_bytes is None else max_bytes
    if limit < 1:
        raise ValueError("source intake byte limit must be positive")
    safe = _safe_filename(filename)
    suffix = PurePosixPath(safe).suffix.lower()
    if suffix not in ACCEPTED_SUFFIXES:
        raise IntakeRefused(
            "unsupported_type",
            f"{safe!r} is not an accepted source file. Upload a PDF or an Excel "
            f"workbook (.xlsx/.xlsm).",
        )
    if not body:
        raise IntakeRefused("empty_file", f"{safe!r} is empty.")
    if len(body) > limit:
        raise IntakeRefused(
            "too_large",
            f"{safe!r} is {_mib(len(body))} MiB; the limit is "
            f"{_mib(limit)} MiB.",
        )
    if not _content_matches_suffix(suffix, body):
        raise IntakeRefused(
            "content_mismatch",
            f"{safe!r} does not contain {suffix} data. Its contents do not match "
            f"its name, so it cannot be read as that kind of file.",
        )

    sha256 = hashlib.sha256(body).hexdigest()
    binding = default_binding()
    emit_event(
        source_arrival_event(
            binding,
            customer_id=customer_id,
            project_id=project_id,
            channel=channel,
            filename=safe,
            content_sha256=sha256,
            byte_count=len(body),
        )
    )
    stored_path = store_bytes(body, sha256=sha256, suffix=suffix)
    emit_event(
        source_capture_event(
            binding,
            customer_id=customer_id,
            project_id=project_id,
            channel=channel,
            storage_key=f"{sha256[:2]}/{sha256}{suffix}",
            content_sha256=sha256,
            byte_count=len(body),
        )
    )
    return StagedSource(
        sha256=sha256,
        size_bytes=len(body),
        suffix=suffix,
        filename=safe,
        stored_path=stored_path,
    )


def preview_intake(
    session: Session, project: Project, staged: StagedSource, doc_type: str
) -> IntakePreview:
    """Report, read-only, what confirming this staged source would create or change.

    Writes nothing. Reads the current registry so the person sees the idempotent
    case (these exact bytes are already registered here) and the concurrent case
    (already registered under a different kind) before deciding.
    """

    if doc_type not in ACCEPTED_DOC_TYPES:
        raise IntakeRefused(
            "unknown_doc_type",
            f"{doc_type!r} is not a document kind Corridor records.",
        )

    existing = session.scalars(
        select(Document).where(
            Document.project_id == project.id,
            Document.sha256 == staged.sha256,
        )
    ).first()

    unresolved = (
        UnresolvedFact(
            "registry_id",
            "Not assigned. A registry id is declared for a curated corpus, not "
            "minted from an upload (ADR-0030).",
        ),
        UnresolvedFact(
            "doc_date",
            "Unknown unless the document itself states one; upload time is not the "
            "document's date.",
        ),
        UnresolvedFact(
            "supersession",
            "None. A replacement is a separate cited confirmation and is never "
            "inferred from a filename, date, or upload (ADR-0015).",
        ),
        UnresolvedFact(
            "rendition_of",
            "None. Identical or similar bytes do not make this a rendition of "
            "another document.",
        ),
    )

    return IntakePreview(
        project_slug=project.slug,
        project_id=project.id,
        filename=staged.filename,
        doc_type=doc_type,
        format_label=_format_label(staged.suffix),
        sha256=staged.sha256,
        size_bytes=staged.size_bytes,
        already_registered=existing is not None,
        existing_document_id=existing.id if existing else None,
        existing_doc_type=existing.doc_type if existing else None,
        existing_parse_status=existing.parse_status if existing else None,
        type_conflict=existing is not None and existing.doc_type != doc_type,
        unresolved=unresolved,
        binding_fingerprint=_binding_fingerprint(
            project.id, staged.sha256, doc_type, staged.filename
        ),
    )


def confirm_intake(
    session: Session,
    *,
    project: Project,
    sha256: str,
    filename: str,
    doc_type: str,
    binding_fingerprint: str,
    principal: HumanPrincipal,
    images_dir: Path | str | None = None,
) -> IntakeConfirmation:
    """Bind the exact previewed source to the acting person and register it.

    Runs in the caller's transaction so the caller's commit is the durable handoff
    and the caller's rollback hands off no work. Refuses a stale, tampered,
    concurrent, or cross-project request before any write:

    * a ``binding_fingerprint`` that does not match the (project, bytes, kind, name)
      being confirmed — the preview was tampered with or came from another project;
    * staged bytes that are missing or no longer hash to ``sha256`` — tampered;
    * an existing Document with these bytes under a *different* kind — a concurrent
      registration; identical bytes never silently become a second document or a
      rendition.

    Otherwise registers and parses the one bounded file through ``ingest_document``
    (idempotent on identical bytes, never overwriting an earlier file) and records
    one attributable confirmation in the append-only audit log. It never touches
    supersession, organization identity, sequencing, or release.
    """

    principal = require_human_principal(principal)
    if doc_type not in ACCEPTED_DOC_TYPES:
        raise IntakeRefused(
            "unknown_doc_type",
            f"{doc_type!r} is not a document kind Corridor records.",
        )

    expected = _binding_fingerprint(project.id, sha256, doc_type, filename)
    if binding_fingerprint != expected:
        raise IntakeConflict(
            "binding_mismatch",
            "This confirmation no longer matches the source you previewed. Re-upload "
            "and preview it again.",
        )

    staged_path = _resolve_staged(sha256)
    if staged_path is None:
        raise IntakeConflict(
            "bytes_missing",
            "The uploaded bytes are no longer staged. Re-upload the file.",
        )
    if hashlib.sha256(staged_path.read_bytes()).hexdigest() != sha256:
        raise IntakeConflict(
            "bytes_tampered",
            "The staged bytes changed since preview. Re-upload the file.",
        )

    existing = session.scalars(
        select(Document).where(
            Document.project_id == project.id,
            Document.sha256 == sha256,
        )
    ).first()
    if existing is not None and existing.doc_type != doc_type:
        raise IntakeConflict(
            "type_conflict",
            f"These exact bytes are already registered in this project as "
            f"{existing.doc_type!r}. Identical content does not create a second "
            f"document or change an existing one's kind.",
        )

    document = ingest_document(
        session,
        project_id=project.id,
        path=staged_path,
        doc_type=doc_type,
        images_dir=Path(images_dir) if images_dir is not None else _images_dir(),
        filename=filename,
        expected_sha256=sha256,
    )
    created = existing is None

    entry = audit.record(
        session,
        principal=principal,
        action=audit.CONFIRM_SOURCE_INTAKE,
        entity_type=audit.DOCUMENT,
        entity_id=document.id,
        after={
            "sha256": sha256,
            "doc_type": doc_type,
            "filename": filename,
            "created": created,
            "binding_fingerprint": binding_fingerprint,
        },
    )
    return IntakeConfirmation(
        document_id=document.id,
        sha256=sha256,
        doc_type=doc_type,
        created=created,
        audit_id=entry.id,
    )


def list_confirmed_uploads(
    session: Session, project_id: int
) -> list[UploadedSourceRow]:
    """Every document confirmed through product intake, with processing status.

    Scoped to intake by the append-only confirmation receipt rather than by
    guessing provenance from a null source url. Each row's ``processing_status``
    is derived, not stored: a failed parse reads as failed, an unreadable
    extraction as unreadable, and a document the standing pass has not reached yet
    as pending — none is ever relabelled as success.
    """

    confirmations = session.scalars(
        select(AuditLog)
        .join(Document, Document.id == AuditLog.entity_id)
        .where(
            AuditLog.action == audit.CONFIRM_SOURCE_INTAKE,
            AuditLog.entity_type == audit.DOCUMENT,
            Document.project_id == project_id,
        )
        .order_by(AuditLog.ts, AuditLog.id)
    ).all()

    first_confirm: dict[int, AuditLog] = {}
    for entry in confirmations:
        first_confirm.setdefault(entry.entity_id, entry)
    if not first_confirm:
        return []

    documents = {
        document.id: document
        for document in session.scalars(
            select(Document).where(Document.id.in_(first_confirm.keys()))
        ).all()
    }
    quarantined = set(
        session.scalars(
            select(DocumentQuarantine.document_id).where(
                DocumentQuarantine.document_id.in_(first_confirm.keys())
            )
        ).all()
    )

    rows: list[UploadedSourceRow] = []
    for document_id, entry in first_confirm.items():
        document = documents.get(document_id)
        if document is None:
            continue
        rows.append(
            UploadedSourceRow(
                document_id=document.id,
                filename=document.filename,
                doc_type=document.doc_type,
                sha256=document.sha256,
                parse_status=document.parse_status,
                pages=document.pages,
                processing_status=_processing_status(
                    session, document, document_id in quarantined
                ),
                confirmed_by=entry.human_principal or entry.actor,
                confirmed_at=entry.ts,
            )
        )
    rows.sort(key=lambda row: row.confirmed_at, reverse=True)
    return rows


def _processing_status(
    session: Session, document: Document, quarantined: bool
) -> str:
    """Derive the honest processing state of one confirmed document."""

    if document.parse_status == "failed":
        return "parse_failed"
    if document.parse_status != "parsed":
        return "pending"
    if quarantined:
        return "held_unmodeled"
    outcome = session.scalar(
        select(ExtractionRun.outcome)
        .where(ExtractionRun.document_id == document.id)
        .order_by(ExtractionRun.id.desc())
        .limit(1)
    )
    if outcome is None:
        return "pending"
    if outcome == "completed":
        return "processed"
    if outcome in ("unreadable", "no_matrix"):
        return "unreadable"
    return "processing_failed"


def _binding_fingerprint(
    project_id: int, sha256: str, doc_type: str, filename: str
) -> str:
    """A deterministic seal over the exact source and declared kind previewed.

    It binds the confirmation to one project, one byte-identity, one declared kind,
    and one filename. A confirm whose echoed values disagree with it — a tampered
    field, or a preview taken against another project — fails to match and is
    refused. It is an integrity and cross-project check, not an authentication
    secret; production identity is #331.
    """

    payload = json.dumps(
        {
            "project_id": project_id,
            "sha256": sha256,
            "doc_type": doc_type,
            "filename": filename,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _safe_filename(filename: str) -> str:
    """The display name, stripped to a bare basename.

    The store is content-addressed, so a filename never becomes a filesystem path;
    this still refuses a path or control characters so nothing downstream shows or
    trusts a crafted name.
    """

    if not filename or not filename.strip():
        raise IntakeRefused("unsafe_filename", "The upload has no filename.")
    name = PurePosixPath(filename.strip()).name
    name = PurePosixPath(name.replace("\\", "/")).name
    if not name or name in (".", ".."):
        raise IntakeRefused("unsafe_filename", f"{filename!r} is not a usable name.")
    if "\x00" in name or any(ord(character) < 32 for character in name):
        raise IntakeRefused(
            "unsafe_filename", "The filename contains control characters."
        )
    if len(name) > 255:
        raise IntakeRefused("unsafe_filename", "The filename is too long.")
    return name


def _content_matches_suffix(suffix: str, body: bytes) -> bool:
    if suffix == ".pdf":
        return body[: len(_PDF_MAGIC)] == _PDF_MAGIC
    if suffix in SPREADSHEET_SUFFIXES:
        return body[: len(_ZIP_MAGIC)] == _ZIP_MAGIC
    return False


def _format_label(suffix: str) -> str:
    if suffix == ".pdf":
        return "PDF"
    if suffix in SPREADSHEET_SUFFIXES:
        return "Excel workbook"
    return suffix


def _resolve_staged(sha256: str) -> Path | None:
    return staged_file(sha256)


def _images_dir() -> Path:
    return Path(settings.corpus_images)


def _mib(value: int) -> int:
    return value // (1024 * 1024)
