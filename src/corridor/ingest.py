"""Register a document and extract its pages.

Every page of a PDF gets both text and a rendered image, because evidence
display needs both: a quote is *verified* against the page text and *shown*
against the page image. A citation you cannot see is not much of a citation.

A spreadsheet has neither pages nor a layout (ADR-0005). Each worksheet
becomes a page, its text is generated from its cells, and there is no image
— rendering one would produce a picture of a spreadsheet rather than
evidence, and the generated text already is the rendering a reviewer checks
a quote against. `text_source` says which of the two a page came from,
because a citation against cells verifies exactly and one against a
printout cannot.

Originals are never modified. The file on disk is read and hashed; nothing
is written back to it.

The discarded PDF router treated fewer than fifty native characters as a scan
and swallowed every OCR exception into an empty string. That made short pages,
mixed pages, and failed OCR indistinguishable. PDF ingest now persists the page
inventory and region decision before it uses any recovered text; an engine
exception becomes a scoped Processing Failure and fails the document attempt.

A PDF page's native reading, its Page Inventory and its routing decision all
come from one reader: the paired-rendition reader (ADR-0094, ADR-0095). Each
of the three used to be a setting choosing between that reader and the
incumbent engine, kept apart so a rollback of one could not drag the others
with it. #741 removed the incumbent, so there is nothing left for those
settings to select and they are gone; what a rollback means now is a
version-control revert, recorded in
`docs/operations/pdf-engine-retirement-rollback.md`.

Pages and image regions the inventory routes to OCR read through Amazon
Textract, at the authorized outbound boundary (#732, #739). The ordinary
answer today is that boundary's refusal, with zero outbound requests, because
the customer authorization it requires is #522's and does not exist yet: a
routed OCR region becomes a scoped Processing Failure naming the refusal. That
is the designed behaviour, not a gap left by this removal — an unauthorized
project has never been allowed to send a customer page to a provider, and it
used to be answered locally instead. A routing decision says who a region
*should* be read by; an attempt and its Processing Failure record who read it.
Neither is allowed to borrow the other's name.

A value only Textract supplies is an Unconfirmed reading (ADR-0094, ADR-0064).
Extraction stays pure, so the page loop carries the scanned reading to the
persistence seam, which appends one ``unconfirmed`` resolution per Textract-only
cell beside the token layer and the raw OCR receipt. That row is what
``load_project``'s corroboration upgrade works; the receipt alone gave it
nothing to read.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.models import (
    NUMBERING_SCHEMES,
    DocPage,
    Document,
    DocumentQuarantine,
    PageProcessingFailure,
    ProcessingArtifact,
    SourceDelivery,
)
from corridor.analytics import emit_event, source_capture_event
from corridor.measurement_collection import binding_for_source
from corridor.page_inventory import (
    PageInventory,
    PageRoutingDecision,
    read_page_facts,
    reader_page_inventories,
    route_reader_page,
)
from corridor.render_profiles import (
    RenderDerivative,
    persist_render_derivative,
    render_page_derivatives,
)
from corridor.retention import open_reference, register_processing_artifact
from corridor.scanned_reading import (
    ScannedPageReading,
    ScannedReader,
    open_scanned_reader,
    read_routed_page,
    record_unconfirmed_readings,
    recovered_text,
)
from corridor.source_segments import (
    SPREADSHEET_SUFFIXES,
    append_ingested_source_segments,
)
from corridor.storage import stored_file
from corridor.token_layers import (
    TEXTRACT_ENGINE,
    TokenLayer,
    page_text_projection,
    persist_token_layer,
    read_native_token_layers,
)
from corridor_pdf_reader.execution import MEASURED_DPI, MEASURED_ENGINE, PdfiumExecutor

# Suffixes read as a workbook rather than a page image. `.xlsm` alongside
# `.xlsx` because TxDOT's own form ships macros in some revisions and the
# cells are identical either way; `.xls` is deliberately absent, since
# openpyxl cannot read the old binary format and a file that silently
# failed would look like a document nobody collected.


@dataclass(frozen=True)
class PageFailure:
    engine: str
    configuration: dict
    region_id: str
    scope: dict
    error_type: str
    error_message: str


@dataclass(frozen=True)
class DocumentParseFailure:
    """Why a whole-document read failed: the same shape a page failure records.

    A routed region that fails gets a ``PageProcessingFailure`` row naming the
    engine, the region, the error type and the error message. A read that fails
    for the whole document used to get ``parse_status = "failed"`` and nothing
    else, so the only account of it was "something went wrong": every real
    condition -- a rendition with no pages, a reader that returned no page N,
    source bytes that no longer match the registered Document -- arrived as the
    same silence, and the extraction receipt could say no more than ``ingest
    parse_status is 'failed'``.

    **Why this is a value and not a row.** The relation that should own it is
    ``page_processing_failures``, and it cannot: ``page_number`` is ``not null``
    with a ``page_number > 0`` check, and a whole-document failure has no page.
    Making it nullable is a migration, and this lane holds no migration slot, so
    the reason travels on the returned Document -- ``pipeline.ingest_and_extract``
    reads it here and records it as the Extraction Run receipt's structured
    ``error_detail``, which is the durable copy. A document-level Processing
    Failure relation is still the right home for it.
    """

    stage: str
    error_type: str
    error_message: str

    def as_error_detail(self) -> dict:
        """The structured reason an Extraction Run receipt records."""
        return {
            "stage": self.stage,
            "error_type": self.error_type,
            "error_message": self.error_message,
        }


def document_parse_failure(document: Document) -> DocumentParseFailure | None:
    """Why this document's read failed, when this process is the one that read it.

    Known only to the ingest that attempted the read; a Document loaded in a
    later session answers ``None`` because nothing durable holds the reason yet.
    """

    return getattr(document, "_parse_failure", None)


@dataclass(frozen=True)
class OcrAttempt:
    """One region's raw OCR output, kept as a Class B intermediary (ADR-0072).

    The recovered text folds into the page's Class A text; this receipt keeps
    the exact engine attempt — configuration, outcome, and output or error —
    as a regenerable, TTL-eligible file so a later failure review can read
    what OCR actually produced without the record depending on it.
    """

    region_id: str
    outcome: str
    receipt_path: Path


@dataclass(frozen=True)
class ExtractedPage:
    page_no: int
    text: str
    image_path: Path | None
    text_source: str
    inventory: PageInventory | None = None
    routing: PageRoutingDecision | None = None
    failures: tuple[PageFailure, ...] = ()
    derivatives: tuple[RenderDerivative, ...] = ()
    ocr_attempts: tuple[OcrAttempt, ...] = ()
    token_layers: tuple[TokenLayer, ...] = ()
    native_reading: object | None = None
    # The page's Textract reading when the route sent it there and the read
    # succeeded; the persistence seam records its Textract-only cells.
    scanned_reading: ScannedPageReading | None = None


def ingest_document(
    session: Session,
    *,
    project_id: int,
    path: Path | str,
    doc_type: str,
    images_dir: Path | str,
    filename: str | None = None,
    source_url: str | None = None,
    retrieved_at: str | datetime | None = None,
    doc_date: date | None = None,
    registry_id: str | None = None,
    expected_sha256: str | None = None,
    numbering_scheme: str | None = None,
    source_delivery_id: int | None = None,
    parse: bool = True,
) -> Document:
    # `parse=False` registers the document and stops, leaving `parse_status`
    # at `pending` for `parse_registered_document` to finish (#893). It exists
    # for one caller: the web confirmation, where registering is the person's
    # attributable act and rendering and reading the file is a worker's job
    # rather than something a person waits on. Every other caller is already a
    # batch or a worker, so reading on the way in costs nobody a request.

    # `source_delivery_id` is the ledger row of the delivery that carried these
    # exact bytes in (#687). It is the caller's proven fact, never derived here:
    # only an intake path that already holds a `source_deliveries` row for these
    # bytes may pass one, and every other path leaves the link unknown rather
    # than guessing a delivery from a time, a filename, or an arrival order. The
    # composite foreign key `(source_delivery_id, project_id)` means a delivery
    # taken for another customer's project is refused by the database.

    # The content-addressed store names files by hash, so `path.name` is a
    # 64-character hex string. Callers pass the document's real name — the
    # archive member or manifest title — because this is what a reader sees
    # next to a citation.
    path = Path(path)
    filename = filename or path.name
    sha256, source_file_missing = _registered_identity(
        path,
        expected_sha256=expected_sha256,
        registry_id=registry_id,
        numbering_scheme=numbering_scheme,
    )

    registered = None
    if registry_id is not None:
        registered = session.scalar(
            select(Document).where(
                Document.project_id == project_id,
                Document.registry_id == registry_id,
            )
        )

    existing = session.scalars(
        select(Document).where(
            Document.project_id == project_id, Document.sha256 == sha256
        )
    ).first()
    if existing is not None:
        if registered is not None and registered.id != existing.id:
            raise ValueError(
                f"registry_id {registry_id!r} already names another document"
            )
        if registry_id is not None and existing.registry_id not in (
            None,
            registry_id,
        ):
            raise ValueError(
                "one registered document cannot carry multiple registry ids"
            )
        return _backfill_registered_provenance(
            session,
            existing,
            path=path,
            images_dir=images_dir,
            registry_id=registry_id,
            source_url=source_url,
            retrieved_at=retrieved_at,
            doc_date=doc_date,
            filename=filename,
            source_delivery_id=source_delivery_id,
            numbering_scheme=numbering_scheme,
            parse=parse,
        )

    if registered is not None:
        raise ValueError(
            f"registry_id {registry_id!r} already names different document bytes"
        )

    document = Document(
        project_id=project_id,
        registry_id=registry_id,
        sha256=sha256,
        filename=filename,
        doc_type=doc_type,
        source_url=source_url,
        retrieved_at=_as_datetime(retrieved_at),
        doc_date=doc_date,
        pages=0,
        parse_status="pending",
        numbering_scheme=numbering_scheme or "project-unique",
        source_delivery_id=source_delivery_id,
    )
    session.add(document)
    session.flush()
    _emit_registered_capture(session, document, path, outcome="unavailable" if source_file_missing else "captured")
    _quarantine_unmodeled_semantics(session, document)

    if source_file_missing:
        # Registered and visibly failed rather than silently absent — the
        # same contract as a parse failure. The recovery path for failed
        # documents picks it up once the store file is restored.
        document._parse_failure = DocumentParseFailure(
            stage="locate_source",
            error_type="FileNotFoundError",
            error_message=f"document file {path} is missing from the store",
        )
        document.parse_status = "failed"
        document.pages = 0
        session.flush()
        return document

    if not parse:
        # Registered, and visibly waiting to be read. `pending` is the status
        # the register already prints as "waiting for the processing pass",
        # and that pass now really does take it: `_parse_landed_documents`
        # selects exactly this state (`corridor.project_processing`).
        return document

    token_dir = Path(images_dir) / sha256
    pages = _read_document(document, path, token_dir)
    if pages is None:
        document.parse_status = "failed"
        document.pages = 0
        session.flush()
        return document

    _persist_pages(session, document, pages, token_dir)

    append_ingested_source_segments(
        session, document, path, native_reading=pages[0].native_reading if pages else None
    )

    document.pages = len(pages)
    document.parse_status = (
        "failed" if any(page.failures for page in pages) else "parsed"
    )
    session.flush()
    return document


def _registered_identity(
    path: Path,
    *,
    expected_sha256: str | None,
    registry_id: str | None,
    numbering_scheme: str | None,
) -> tuple[str, bool]:
    """The content digest this document is registered under, and its declarations."""

    source_file_missing = not path.exists()
    if source_file_missing:
        # The lockfile recorded a successful fetch, but the content-addressed
        # store lost the bytes. The recorded hash still identifies the
        # document, so register it visibly failed rather than aborting the
        # whole project's ingest on one hole in the store.
        if expected_sha256 is None:
            raise FileNotFoundError(
                f"document file {path} is missing and no lockfile sha256 "
                "identifies its content"
            )
        sha256 = expected_sha256
    else:
        sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        if expected_sha256 is not None and sha256 != expected_sha256:
            raise ValueError(
                f"document bytes do not match lockfile sha256: expected "
                f"{expected_sha256}, got {sha256}"
            )
    if registry_id is not None and not registry_id.strip():
        raise ValueError("registry_id must be non-empty")
    if numbering_scheme is not None and numbering_scheme not in NUMBERING_SCHEMES:
        raise ValueError(
            f"unknown numbering_scheme {numbering_scheme!r}; expected one "
            f"of {', '.join(NUMBERING_SCHEMES)}"
        )
    return sha256, source_file_missing


def _read_document(
    document: Document, path: Path, token_dir: Path
) -> list[ExtractedPage] | None:
    """Read every page, or keep why the whole-document read failed.

    The narrowest scope a bare ``except`` can have here, and the reason it is
    still bare: `_extract_pages` raises ``ValueError`` for a rendition with no
    pages and for a reader that returned no page N, ``read_native_pdf`` raises
    ``SourceDocumentDigestMismatch`` on byte drift, and the reader assembly and
    render workers raise their own types. One unreadable document must not abort
    a project's ingest, so every one of them registers the document visibly
    failed — but the reason used to be discarded here, which is what made a
    failed document indistinguishable from one nobody collected.
    """

    try:
        return _extract(
            path, token_dir, document.sha256, project=str(document.project_id)
        )
    except Exception as exc:
        document._parse_failure = DocumentParseFailure(
            stage="read_document",
            error_type=type(exc).__name__,
            error_message=str(exc) or repr(exc),
        )
        return None


def _backfill_registered_provenance(
    session: Session,
    existing: Document,
    *,
    path: Path,
    images_dir: Path | str,
    registry_id: str | None,
    source_url: str | None,
    retrieved_at: str | datetime | None,
    doc_date: date | None,
    filename: str,
    source_delivery_id: int | None,
    numbering_scheme: str | None,
    parse: bool = True,
) -> Document:
    """Re-ingest of identical bytes: provenance may be completed, never rewritten."""

    # Re-ingest never re-parses — the bytes are identical by definition.
    # But provenance describes where the file came from, not the file,
    # and a document first ingested without a date would otherwise carry
    # that gap forever. Backfill nulls only; never overwrite. The delivery
    # link follows the same rule for a stronger reason: identical bytes can
    # be delivered twice, and the second delivery must not relabel which one
    # this document came in on (#687).
    for attribute, value in (
        ("registry_id", registry_id),
        ("source_url", source_url),
        ("retrieved_at", _as_datetime(retrieved_at)),
        ("doc_date", doc_date),
        ("filename", filename),
        ("source_delivery_id", source_delivery_id),
    ):
        if value and getattr(existing, attribute) in (None, ""):
            setattr(existing, attribute, value)
    # The numbering scheme is a declaration, not provenance: unlike
    # the backfill-nulls-only facts above it always holds a value
    # (the default), so a manifest that states one re-declares it —
    # re-registration is exactly where a registry fact may change
    # (ADR-0030).
    if numbering_scheme is not None:
        existing.numbering_scheme = numbering_scheme
    if parse:
        if path.suffix.lower() == ".pdf":
            ingest_native_reader(
                session, document=existing, path=path, images_dir=images_dir
            )
        else:
            append_ingested_source_segments(session, existing, path)
    # A caller that is not reading the file leaves the reader alone here too
    # (#893). Nothing is lost by it: these exact bytes are already registered,
    # so they are already read, already `pending` for the standing pass, or
    # already failed and owned by the bounded re-parse -- and re-running the
    # reader in a web request to re-derive segments it has or will have is the
    # work this stopped doing.
    _quarantine_unmodeled_semantics(session, existing)
    session.flush()
    _emit_registered_capture(session, existing, path, outcome="replayed")
    return existing


def _emit_registered_capture(session, document: Document, path: Path, *, outcome: str) -> None:
    """Registration binds native identity; staging or re-ingest cannot relabel it."""
    delivery = session.get(SourceDelivery, document.source_delivery_id) if document.source_delivery_id else None
    binding = binding_for_source(session, delivery)
    emit_event(source_capture_event(
        binding, customer_id=binding.customer_id, project_id=document.project_id,
        channel=delivery.channel if delivery else "upload", document_id=int(document.id),
        source_delivery_id=document.source_delivery_id, content_sha256=document.sha256,
        storage_key=delivery.bytes_reference if delivery else str(path),
        byte_count=path.stat().st_size if path.exists() else None,
        occurred_at=document.created_at if outcome != "replayed" else datetime.now(timezone.utc), outcome=outcome,
    ))


def reparse_document(
    session: Session,
    *,
    document: Document,
    path: Path | str,
    images_dir: Path | str,
) -> bool:
    """Read one registered document whose bytes this database has not read (#350).

    Ordinary re-ingest returns the existing document untouched when the bytes are
    identical, which is correct for provenance but leaves a document that failed to
    parse permanently unread even after the reader is fixed. This is the explicit,
    bounded recovery for exactly that case. It re-extracts from the original file —
    which is never modified — and, only on success, writes the freshly parsed pages
    and flips the status. It refuses to touch a document that already parsed, so a
    successfully parsed history is never rewritten as a retry; the caller is
    responsible for the attributable receipt and for excluding held inputs.

    Since #893 it also reads a document whose parse has not been *attempted* —
    one `ingest_document(parse=False)` registered and handed to the standing
    pass. The two are the same act against this table: the caller holds bytes
    nothing has turned into pages, and it either produces them or records why
    it could not. The page delete below is what makes the retry of either
    safe, so a crash mid-read cannot leave a half-read document behind.

    Returns ``True`` when the document is now parsed, ``False`` when it failed
    again. Either way the prior state is preserved rather than corrupted: a second
    failure leaves the status ``failed`` and no partial pages.
    """

    if document.parse_status == "parsed":
        raise ValueError("a successfully parsed document is never re-parsed as a retry")
    path = Path(path)
    token_dir = Path(images_dir) / document.sha256
    pages = _read_document(document, path, token_dir)
    if pages is None:
        document.parse_status = "failed"
        document.pages = 0
        session.flush()
        return False

    # A failed parse left no pages; guard the invariant rather than assume it, so a
    # partial earlier attempt could never leave duplicated page numbers behind.
    existing = session.scalars(
        select(DocPage).where(DocPage.document_id == document.id)
    ).all()
    for page in existing:
        session.delete(page)
    session.flush()

    _persist_pages(session, document, pages, token_dir)
    append_ingested_source_segments(
        session, document, path, native_reading=pages[0].native_reading if pages else None
    )
    document.pages = len(pages)
    document.parse_status = (
        "failed" if any(page.failures for page in pages) else "parsed"
    )
    session.flush()
    return document.parse_status == "parsed"


def parse_registered_document(
    session: Session, *, document: Document, images_dir: Path | str
) -> bool:
    """Read a document registered without a read, from wherever its bytes are.

    The standing pass's half of `ingest_document(parse=False)` (#893). It adds
    one thing to `reparse_document`: it resolves the registered bytes itself,
    because the pass holds a Document and not a path, and a document whose
    bytes the store has lost has to end up *visibly failed* rather than
    selected again for ever by a pass that cannot read it.
    """

    path = stored_file(document)
    if path is None:
        document._parse_failure = DocumentParseFailure(
            stage="locate_source",
            error_type="FileNotFoundError",
            error_message=(
                f"document {document.sha256} is missing from the store"
            ),
        )
        document.parse_status = "failed"
        document.pages = 0
        session.flush()
        return False
    return reparse_document(
        session, document=document, path=path, images_dir=images_dir
    )


def ingest_native_reader(
    session: Session, *, document: Document, path: Path | str,
    images_dir: Path | str, engine: str = "tagged", dpi: int = 36,
):
    """Append the paired-rendition reader's own segments to a registered PDF.

    This is the production reader (ADR-0094, ADR-0095): the challenger it was
    added as won, and #741 removed the incumbent it was measured against, so
    nothing here is disabled or optional any more. It returns its own page text
    and tokens along with the new segments, and it runs on every re-ingest of
    identical bytes -- ordinary document deduplication cannot suppress it,
    because the segments are what a citation is later replayed against. Existing
    DocPage projections and accepted citations remain historical. Pipeline
    selection is #447's and downstream semantic mapping is #737's.
    """
    from corridor.reader_segments import append_native_segments, read_native_pdf

    reading = read_native_pdf(path, source_sha256=document.sha256, engine=engine, dpi=dpi)
    segments = append_native_segments(session, document, reading)
    for layer in reading.token_layers:
        persist_token_layer(session, document.id, layer, output_dir=Path(images_dir) / document.sha256)
    return reading, segments


def _persist_pages(
    session: Session,
    document: Document,
    pages: list[ExtractedPage],
    token_dir: Path,
) -> None:
    # A render or receipt is terminal the moment ingest finishes producing it;
    # the 30-day Class B clock starts here.
    terminal_at = datetime.now(timezone.utc)
    for extracted in pages:
        page = DocPage(
            document_id=document.id,
            page_no=extracted.page_no,
            text=extracted.text,
            # None for a worksheet, which has no rendering to point at.
            image_path=str(extracted.image_path) if extracted.image_path else None,
            text_source=extracted.text_source,
            inventory_json=(
                extracted.inventory.model_dump(mode="json")
                if extracted.inventory
                else None
            ),
            routing_json=(
                extracted.routing.model_dump(mode="json")
                if extracted.routing
                else None
            ),
        )
        session.add(page)
        session.flush()
        # Every render — review, OCR-layout, table-CV, and any region crop —
        # is Class B by construction: the persistence seam classifies it, so
        # no current or future render path can escape TTL.
        for derivative in extracted.derivatives:
            persist_render_derivative(session, document.id, derivative)
        # Native and OCR token layers persist as Class B artifacts with
        # PostgreSQL manifests (ADR-0073); deleting them later leaves every
        # promoted source segment verifiable.
        for layer in extracted.token_layers:
            persist_token_layer(session, document.id, layer, output_dir=token_dir)
        # A cell only Textract read is an Unconfirmed reading on the record
        # (ADR-0094): flagged, never Ready, upgraded by `load_project` the
        # moment a readable source corroborates it. Idempotent per cell.
        if extracted.scanned_reading is not None:
            record_unconfirmed_readings(
                session,
                project_id=document.project_id,
                document_id=document.id,
                reading=extracted.scanned_reading,
            )
        # Raw OCR output is its own Class B intermediary; register each receipt
        # and key it by region so an open failure can hold it reachable.
        raw_ocr_by_region: dict[str, ProcessingArtifact] = {}
        for attempt in extracted.ocr_attempts:
            raw_ocr_by_region[attempt.region_id] = register_processing_artifact(
                session,
                project_id=document.project_id,
                kind="raw_ocr",
                path=attempt.receipt_path,
                terminal_at=terminal_at,
            )
        ocr_layout_artifact = _ocr_layout_artifact(session, extracted)
        for failure in extracted.failures:
            failure_row = PageProcessingFailure(
                document_id=document.id,
                page_number=extracted.page_no,
                engine=failure.engine,
                configuration_json=failure.configuration,
                region_id=failure.region_id,
                scope_json=failure.scope,
                error_type=failure.error_type,
                error_message=failure.error_message,
            )
            session.add(failure_row)
            session.flush()
            referenced_by = f"page_processing_failure:{failure_row.id}"
            # An open failure keeps its own render and raw-OCR intermediaries
            # reachable (90 days), so the evidence survives while it is unresolved.
            # The scanned route reads the whole page in one call and writes
            # one receipt for it, while its failures are still recorded per
            # routed region, so a region-keyed lookup finds nothing and the
            # receipt an open failure needs would expire under it. Fall back to
            # the page receipt, which is the attempt that failed.
            for artifact in (
                raw_ocr_by_region.get(failure.region_id)
                or raw_ocr_by_region.get("page"),
                ocr_layout_artifact,
            ):
                if artifact is None:
                    continue
                open_reference(
                    session,
                    project_id=document.project_id,
                    family="processing_artifact",
                    source_row_id=artifact.id,
                    kind="processing_failure",
                    referenced_by=referenced_by,
                )


def _ocr_layout_artifact(
    session: Session, extracted: ExtractedPage
) -> ProcessingArtifact | None:
    """The registered OCR-layout render for this page, if any — the artifact an
    open OCR failure holds reachable alongside its raw-OCR receipt."""

    for derivative in extracted.derivatives:
        if derivative.profile_name == "ocr_layout":
            return session.scalar(
                select(ProcessingArtifact).where(
                    ProcessingArtifact.storage_path == str(derivative.artifact_path)
                )
            )
    return None


def _extract(
    path: Path, images_dir: Path, source_sha256: str, *, project: str
) -> list[ExtractedPage]:
    if path.suffix.lower() in SPREADSHEET_SUFFIXES:
        return _extract_sheets(path)
    if path.suffix.lower() == ".eml":
        return _extract_message(path)
    return _extract_pages(path, images_dir, source_sha256, project=project)


def _extract_message(path: Path) -> list[ExtractedPage]:
    """One page holding a stored raw message's plain-text body.

    Email intake (#372, ADR-0058) stores the complete original message
    byte-exact; the body is written evidence that flows through the ordinary
    prose statement path, so it needs a registered page a quote can verify
    against. The exact transmitted text is the page — no image exists, and
    "text_layer" is honest: this is the text the source itself carried.
    """
    from email import policy
    from email.parser import BytesParser

    message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    body = ""
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                body = str(part.get_content() or "")
                break
    elif message.get_content_type() == "text/plain":
        body = str(message.get_content() or "")
    if not body.strip():
        # This page is a display fallback only. #455 cites MIME parts directly,
        # and preserves HTML/attachment-only messages without calling them prose.
        body = "\n".join(f"{name}: {value}" for name, value in message.raw_items())
    if not body.strip():
        raise ValueError(f"{path.name}: no message headers or body")
    return [ExtractedPage(1, body, None, "text_layer")]


def _extract_sheets(path: Path) -> list[ExtractedPage]:
    """One page per worksheet, its text generated from its cells.

    Sheet order is the file's own, and the page number is its position —
    1-based like a PDF's, so `[D12 p.2]` still names something a reader can
    find. A sheet name would be a better citation and cannot be one: the
    citation columns hold a page number, and widening them is a schema
    change this ticket does not need.
    """
    from corridor.sheets import read_workbook, sheet_text

    sheets = read_workbook(path)
    if not sheets:
        raise ValueError(f"{path.name}: no worksheets")
    return [
        ExtractedPage(index, sheet_text(sheet), None, "cells")
        for index, sheet in enumerate(sheets, start=1)
    ]


class _NativeInventoryRead:
    """The native reader's executor hook, retaining its paired inventory facts.

    Both adapters already read the same measured configuration. The existing
    isolated page-facts command returns that exact reader result plus image
    and path facts, so it can supply both projections without a second decode.
    The real executor retains all process, timeout, and memory protections.
    """

    def __init__(self):
        self.facts = None

    def read_document(self, source, *, engine, dpi):
        if engine != MEASURED_ENGINE or dpi != MEASURED_DPI:
            raise ValueError("paired inventory requires the measured native reader configuration")
        self.facts = PdfiumExecutor().run(read_page_facts, Path(source))
        return self.facts


def _extract_pages(
    path: Path, images_dir: Path, source_sha256: str, *, project: str
) -> list[ExtractedPage]:
    images_dir.mkdir(parents=True, exist_ok=True)
    out: list[ExtractedPage] = []
    # The boundary an OCR-routed region reads through (#732, #739). It is
    # opened for every document rather than behind a setting, because the
    # incumbent local engine that the setting used to choose instead is gone
    # (#741). Opening it is not a network call: an unauthorized project gets
    # the boundary's refusal, recorded per region as a Processing Failure.
    scanned = open_scanned_reader(
        project,
        extraction_run=source_sha256,
        cache_root=Path(images_dir) / "textract",
    )
    # One isolated read of the whole document, before the page loop. A failure
    # here fails the document attempt like any other engine failure.
    from corridor.reader_segments import read_native_pdf

    paired_read = _NativeInventoryRead()
    native_reading = read_native_pdf(
        path, source_sha256=source_sha256, executor=paired_read
    )
    reader_layers = {
        layer.page_no: layer for layer in native_reading.token_layers
    }
    # The reader's own pages, glyphs included: what the scanned route's
    # native-glyph assignment is derived over (#810), from the read already
    # made rather than a second decode.
    reader_pages = {
        int(page["number"]): page for page in paired_read.facts["pages"]
    }
    # Native text and inventory are projections of one authenticated reading;
    # the page-facts command adds inventory geometry without decoding again.
    reader_inventories = reader_page_inventories(paired_read.facts)

    if not reader_inventories:
        raise ValueError(f"{path.name}: no pages")
    for page_no in sorted(reader_inventories):
        # 1-based: citations are written for humans, and [D12 p.4] must
        # mean the page a reader sees.
        inventory = reader_inventories[page_no]
        routing = route_reader_page(inventory)
        reader_layer = reader_layers.get(page_no)
        if reader_layer is None:
            raise ValueError(
                f"{path.name}: the reader returned no page {page_no}"
            )
        # `DocPage.text` is a projection over the native token layer, not
        # a second reading beside it (ADR-0073).
        native_text = page_text_projection(reader_layer)
        # The layout/model derivative is purpose-specific even when OCR is
        # not needed; vision consumers never borrow reviewer pixels. All
        # three are asked for at once so the worker's OpenCV import is paid
        # once a page rather than three times (#548).
        derivatives = render_page_derivatives(
            pdf_path=path,
            page_number=page_no,
            profile_names=("review", "ocr_layout", "table_cv"),
            output_dir=images_dir,
        )
        review_derivative, ocr_derivative, table_derivative = derivatives
        image_path = review_derivative.artifact_path
        ocr_text: list[str] = []
        failures: list[PageFailure] = []
        ocr_attempts: list[OcrAttempt] = []
        outcome = _read_scanned_page(
            scanned,
            path=path,
            images_dir=images_dir,
            page_no=page_no,
            source_sha256=source_sha256,
            routing=routing,
            native_layer=reader_layer,
            reader_page=reader_pages.get(page_no),
            render_profile_id=ocr_derivative.profile_id,
        )
        ocr_text.extend(outcome.text)
        failures.extend(outcome.failures)
        ocr_attempts.extend(outcome.attempts)
        scanned_layer: TokenLayer | None = outcome.token_layer
        parts = []
        if routing.page_mode in {"native", "both"} or not ocr_text:
            if native_text:
                parts.append(native_text.rstrip())
        parts.extend(text for text in ocr_text if text not in parts)
        text = "\n".join(parts)
        if text:
            text += "\n"
        # Compatibility readers keep every page that required OCR on the
        # less-trusted OCR path, including failed attempts. Calling an
        # image-only failure `text_layer` would recreate the retired lie.
        source = (
            "ocr" if routing.page_mode in {"ocr", "both"} else "text_layer"
        )

        # Two coordinate-bearing token layers, kept separately (ADR-0073):
        # the native reading always, plus an OCR layer when the page routes
        # through OCR. Nothing picks a page-wide winner here — DocPage.text
        # above is a rebuildable projection, and geometry-consuming
        # extraction reads these tokens, not the page string.
        token_layers: list[TokenLayer] = [reader_layer]
        if scanned_layer is not None:
            token_layers.append(scanned_layer)

        out.append(
            ExtractedPage(
                page_no=page_no,
                text=text,
                image_path=image_path,
                text_source=source,
                inventory=inventory,
                routing=routing,
                failures=tuple(failures),
                derivatives=tuple(derivatives),
                ocr_attempts=tuple(ocr_attempts),
                token_layers=tuple(token_layers),
                native_reading=native_reading,
                scanned_reading=outcome.reading,
            )
        )

    return out


@dataclass(frozen=True)
class _ScannedPageOutcome:
    """What one page's Textract read produced, in the shapes the page loop keeps."""

    text: tuple[str, ...]
    failures: tuple[PageFailure, ...]
    attempts: tuple[OcrAttempt, ...]
    token_layer: TokenLayer | None
    reading: ScannedPageReading | None = None


def _read_scanned_page(
    scanned: ScannedReader,
    *,
    path: Path,
    images_dir: Path,
    page_no: int,
    source_sha256: str,
    routing: PageRoutingDecision,
    native_layer: TokenLayer | None,
    reader_page: dict[str, Any] | None,
    render_profile_id: str,
) -> _ScannedPageOutcome:
    """One page through the authorized adapter, as the page loop records it.

    A page the decision routes nowhere is not read and is not a failure: an
    ordinary blank page and a clean native page are not scanned work. A page
    that is routed and cannot be read is a Processing Failure per routed
    region, naming the provider that failed, the request configuration and the
    page scope — and the incumbent engine is not asked to fill the gap, so the
    record says Textract did not read this page rather than showing a reading
    from an engine nobody selected.
    """

    outcome = read_routed_page(
        scanned,
        path,
        page_no=page_no,
        routing=routing,
        rendition_sha256=source_sha256,
        source_sha256=source_sha256,
        native_layer=native_layer,
        reader_page=reader_page,
        render_profile_id=render_profile_id,
    )
    if outcome is None:
        return _ScannedPageOutcome((), (), (), None)
    scope = {
        "page_number": page_no,
        "region_ids": [region.region_id for region in outcome.regions],
        "router_version": routing.router_version,
    }
    if outcome.failure is not None:
        failure = outcome.failure
        receipt = _write_raw_ocr_receipt(
            images_dir,
            page_no=page_no,
            region_id="page",
            configuration=failure.configuration,
            scope=failure.scope,
            outcome="failed",
            text=None,
            error_type=failure.reason,
            error_message=failure.detail,
            engine=failure.engine,
        )
        return _ScannedPageOutcome(
            (),
            tuple(
                PageFailure(
                    engine=failure.engine,
                    configuration=failure.configuration,
                    region_id=region.region_id,
                    scope=dict(scope, region_id=region.region_id),
                    error_type=failure.reason,
                    error_message=failure.detail,
                )
                for region in outcome.regions
            ),
            (OcrAttempt("page", "failed", receipt),),
            None,
        )
    reading = outcome.reading
    assert reading is not None
    recovered = recovered_text(reading.reading)
    result = "recovered" if recovered.strip() else "empty"
    receipt = _write_raw_ocr_receipt(
        images_dir,
        page_no=page_no,
        region_id="page",
        configuration={"provenance": reading.provenance},
        scope=scope,
        outcome=result,
        text=recovered,
        error_type=None,
        error_message=None,
        engine=reading.token_layer.identity.engine,
        values=[
            {
                "region_id": value.region_id,
                "table": value.table,
                "row": value.row,
                "column": value.column,
                "box": value.box.model_dump(mode="json"),
                "value": value.value,
                "value_source": value.value_source,
                "state": value.state,
                "confidence": value.confidence,
                "locator": value.locator.model_dump(mode="json") if value.locator else None,
                "provenance": value.provenance,
            }
            for value in reading.values
        ],
    )
    return _ScannedPageOutcome(
        (recovered.strip(),) if recovered.strip() else (),
        (),
        (OcrAttempt("page", result, receipt),),
        reading.token_layer,
        reading,
    )


def _write_raw_ocr_receipt(
    images_dir: Path,
    *,
    page_no: int,
    region_id: str,
    configuration: dict,
    scope: dict,
    outcome: str,
    text: str | None,
    error_type: str | None,
    error_message: str | None,
    engine: str = TEXTRACT_ENGINE,
    values: list[dict] | None = None,
) -> Path:
    """Write one OCR attempt's exact output to a content-addressed Class B file.

    Content-addressed so a re-render of the same page reuses the identical
    receipt rather than colliding; the persistence seam registers it for
    retention. No session here: extraction stays pure, persistence classifies.

    `engine` is the engine that actually read, which is not always the one the
    route named (#739): a receipt that said `tesseract` over a Textract reading
    would be the same lie the retired router told about thin text. `values` is
    the scanned route's per-cell classification — which cells were re-mapped
    from the document's own glyphs and which are Unconfirmed readings — kept
    here rather than in the record, because it is evidence about a reading and
    not a value the record depends on.
    """

    payload = {
        "configuration": dict(configuration),
        "engine": engine,
        "error_message": error_message,
        "error_type": error_type,
        "outcome": outcome,
        "page_number": page_no,
        "region_id": region_id,
        "scope": scope,
        "text": text,
        "values": values,
    }
    content = (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    safe_region_id = "".join(
        character if character.isalnum() or character in {"-", "_"} else "_"
        for character in region_id
    )
    destination = (
        images_dir / f"{page_no:04d}-{safe_region_id}-raw-ocr-{digest}.json"
    )
    if destination.exists():
        if destination.read_bytes() != content:
            raise ValueError(f"raw OCR receipt content-address collision at {destination}")
    else:
        destination.write_bytes(content)
    return destination.resolve()


def _as_datetime(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _quarantine_unmodeled_semantics(session, document) -> None:
    """Record the durable outcome for a document Corridor must not read.

    A schedule's rows relate to each other — one work item must complete
    before another may start — and Corridor has no model for that relation
    (#149). The document is registered and visible; this row is the
    project-level fact that it is deliberately unread, surviving process
    exit rather than living in an operator's memory. Idempotent, so
    re-ingest never duplicates it.
    """
    if document.doc_type != "schedule":
        return
    if session.get(DocumentQuarantine, document.id) is None:
        session.add(
            DocumentQuarantine(
                document_id=document.id,
                reason=(
                    "document-asserted work sequencing is not modeled; rows "
                    "would keep their values and lose their relationships "
                    "(#149)"
                ),
            )
        )
