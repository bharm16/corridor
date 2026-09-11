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

An upload *is* a member of the delivery family (#823). ADR-0078 lists manual
upload among the connector kinds that enter under one contract, and ADR-0089 made
every delivery one persisted row whatever transport carried it; an upload belongs
there because somebody hands Corridor bytes it never asked for, which is what push
means. What kept it out was three database constraints rather than a preference,
and the load-bearing one was ``ck_source_delivery_push_credential``: it made a
pushed delivery name a ``push_intake_credentials`` row that an authenticated
*person* does not hold. It is now ``ck_source_delivery_authentication``, which
asks how the transport authenticated instead of assuming — a machine push names
its credential, a human push names its principal, and a push naming neither is
refused. Nothing is minted for a person: ``ck_push_intake_credential_channel``
still admits no upload channel, because a credential issued to an uploader would
be a live push secret and therefore a real door into the project. No third
transport is added either, since recording the upload as a pull would state that
a connector configuration fetched it on a cursor — the second definition of one
identity ADR-0089 exists to remove.

``receive_upload`` is that seam, and it records the whole lifecycle rather than
only its happy end. A refusal at the byte or structure gate is a
``terminally_refused`` delivery carrying the rule that refused it; a storage or
scanner failure is a ``transient_failure``, which says nothing about the bytes
and is why the checkpoint rule treats the two differently; and an authorized
upload is ``stored`` before the person has decided anything, so an upload staged
and then abandoned is a record instead of an absence. Confirmation stays the
separate attributable act it always was, now also bound to the delivery through
``source_delivery.confirm_delivery`` and idempotent on a replay — and the
``source_delivery_id`` ``confirm_intake`` had reserved for it is at last the one
an ordinary upload carries.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor import refusals
from corridor import audit
from corridor.analytics import (
    default_binding,
    emit_event,
    source_arrival_event,
    source_capture_event,
)
from corridor.config import settings
from corridor.ingest import SPREADSHEET_SUFFIXES, ingest_document
from corridor.intake_hardening import (
    HostileContentRefused,
    inspect_byte_gate,
    inspect_sandboxed_structure,
)
from corridor.models import (
    DOC_TYPES,
    AuditLog,
    DocumentQuarantine,
    Document,
    ExtractionRun,
    Project,
    SourceDelivery,
)
from corridor.object_storage import content_key, store_bytes
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.storage import staged_file

# ``corridor.source_delivery`` reaches this module back through the connector
# package, so the delivery ledger is imported where it is used rather than at
# the top — the same local import ``source_delivery`` itself makes for the
# activation gate.
if TYPE_CHECKING:
    from corridor.source_delivery import DeliveryObservation

# One uploaded file per request is the count bound; a batch caller (email) loops
# this module per attachment. 64 MiB holds a large utility-conflict matrix PDF or
# a workbook with room to spare while refusing an unbounded upload outright.
MAX_UPLOAD_BYTES = 64 * 1024 * 1024

# Exactly the formats the ingest pipeline can read (``corridor.ingest``): a PDF, or
# a workbook that is the structured original (ADR-0005). Everything else is refused
# at the door rather than registered as a document nobody can parse. Bound
# mail intake can opt into the separately bounded MIME reader for .eml parts;
# this does not expand the upload surface's default format contract.
ACCEPTED_SUFFIXES = frozenset({".pdf"}) | SPREADSHEET_SUFFIXES

# The delivery family's three server-owned facts about a product upload (#823).
# The channel is what the person used, not what they said; the configuration is
# the upload surface itself, which is what a pull delivery names with a connector
# identity and a machine push names with its credential; the service identity is
# this module, because this is what took delivery.
PRODUCT_UPLOAD_CHANNEL = "product_upload"
PRODUCT_UPLOAD_CONFIGURATION = "product-upload"
UPLOAD_SERVICE_IDENTITY = "corridor.source_intake"

# The declared semantic kind of the source, chosen by the person handing it over —
# a question the bytes cannot answer and the system must not guess (ADR-0007,
# ADR-0030). The full document vocabulary is offered; the model never classifies it.
ACCEPTED_DOC_TYPES = frozenset(DOC_TYPES)

# A file whose name claims one format but whose content is another is refused as
# foreign before it is ever handed to a parser, and the size bound is enforced
# before that. Both checks were written here as well as in
# ``intake_hardening.inspect_byte_gate``, which every other channel uses, so the
# same hostile bytes were refused as ``too_large``/``content_mismatch`` on an
# upload and ``size_limit_exceeded``/``magic_mismatch`` on a push or a pull. The
# gate owns the rule now; ``_upload_sentence`` owns the words a person reads.


class IntakeRefused(refusals.Refusal, ValueError):
    """A bounded, actionable refusal raised before any registration.

    ``reason`` is a stable machine code so an adapter can branch on it, and
    ``str(exc)`` is the human sentence. The codes an upload adds of its own are
    ``unsupported_type``, ``unsafe_filename``, ``unknown_doc_type``, and
    ``content_mismatch`` for an attached message that is not readable MIME; every
    other refusal carries the shared byte-gate or structural rule that refused
    it (``empty_file``, ``size_limit_exceeded``, ``magic_mismatch``,
    ``malware_detected``, ``xml_entity_bomb``, and the rest of #490's rules), so
    a refusal reads the same here as it does on the push and pull channels.
    """

    refusal_kind = refusals.MALFORMED_INPUT

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class UploadRefused(IntakeRefused):
    """The gate refused an upload, and the ledger recorded the refusal.

    Carries the ledger identity so the caller can commit the record it just
    made while still refusing the request: a refusal that rolls back with the
    response is exactly the loss ADR-0089 set out to remove, and it is the
    same shape ``push_intake.PushDeliveryRefused`` uses for the same reason.
    """

    def __init__(self, reason: str, message: str, *, delivery_id: int) -> None:
        super().__init__(reason, message)
        self.delivery_id = delivery_id


class UploadNotTaken(refusals.Refusal, RuntimeError):
    """Storage or the scanner failed, so nothing can be said about the bytes.

    Distinct from ``UploadRefused`` because the two mean opposite things to
    anybody reading the ledger afterwards (ADR-0089): a refusal is a fact about
    the delivery and will never be admitted, and this is a fact about Corridor
    on one attempt. The person is told to try again, and the recorded
    ``transient_failure`` is what stops that attempt from vanishing.
    """

    refusal_kind = refusals.CONFLICT

    def __init__(self, message: str, *, delivery_id: int) -> None:
        super().__init__(message)
        self.delivery_id = delivery_id


class IntakeConflict(refusals.Refusal, ValueError):
    """A confirm that no longer matches the source or registry it previewed.

    Raised for a stale, tampered, concurrent, or cross-project confirmation before
    any authoritative write. ``reason`` is one of ``binding_mismatch``,
    ``bytes_missing``, ``bytes_tampered``, ``type_conflict``.
    """

    refusal_kind = refusals.CONFLICT

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
class ReceivedUpload:
    """One taken upload: its staged bytes, and the delivery they arrived on."""

    staged: StagedSource
    delivery_id: int
    delivery_identity: str
    replayed: bool


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
    # The delivery this person admitted to processing, where the confirmation
    # named one; a source that arrived through no transport names none (#823).
    delivery_confirmation_id: int | None = None


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
    allow_email: bool = False,
) -> StagedSource:
    """Enforce the bounded limits and stage exact bytes; refuse before model work.

    Refuses an empty, oversized, wrong-suffix, or foreign file with an
    ``IntakeRefused`` and writes nothing. Upload and email callers use the 64 MiB
    default; a connected location supplies its separately validated Gate-7 byte
    bound. On acceptance the exact bytes land in the content-addressed store keyed
    by their own hash — writing identical bytes twice is a no-op, so a re-uploaded
    file is never duplicated and an earlier file is never overwritten.

    The gate composition is the accepted-format question, which only this
    channel asks, and then #490's stages: ``inspect_byte_gate`` for the size
    bound, the magic bytes and the malware seam — the same call the push and
    pull channels make, so a refusal carries the same rule on every channel —
    and ``inspect_sandboxed_structure``, which the upload adds because it is the
    channel that hands accepted bytes straight to a rich parser on confirmation.
    """

    limit = MAX_UPLOAD_BYTES if max_bytes is None else max_bytes
    if limit < 1:
        raise ValueError("source intake byte limit must be positive")
    safe = _safe_filename(filename)
    suffix = PurePosixPath(safe).suffix.lower()
    if suffix not in ACCEPTED_SUFFIXES and not (allow_email and suffix == ".eml"):
        raise IntakeRefused(
            "unsupported_type",
            f"{safe!r} is not an accepted source file. Upload a PDF or an Excel "
            f"workbook (.xlsx/.xlsm).",
        )

    try:
        inspect_byte_gate(body, safe, max_bytes=limit)
        inspect_sandboxed_structure(body, safe)
    except HostileContentRefused as exc:
        raise IntakeRefused(
            exc.rule, _upload_sentence(exc, safe, suffix, len(body), limit)
        ) from exc

    if suffix == ".eml":
        from corridor.email_segments import read_mime_segments

        try:
            has_sender = any(span.header_name == "from" and span.part_path == ()
                             for span in read_mime_segments(body))
        except (ValueError, UnicodeError, LookupError) as exc:
            raise IntakeRefused("content_mismatch", "attachment is not readable MIME") from exc
        if not has_sender:
            raise IntakeRefused("content_mismatch", "attached email has no From header")

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


def receive_upload(
    session: Session,
    *,
    project: Project,
    body: bytes,
    filename: str,
    principal: HumanPrincipal,
    customer: str,
    source_revision: str = "",
    max_bytes: int | None = None,
) -> ReceivedUpload:
    """Take delivery of one uploaded file, and record what became of it.

    The gate and the staging are ``validate_and_stage``'s, unchanged and shared
    with every other channel. What this adds is the ledger row ADR-0089 says
    every delivery gets, on the one transport that fits — ``push``, because
    somebody handed Corridor bytes it never asked for — authenticated by the
    signed-in person rather than by a machine credential nobody should mint for
    them.

    Every outcome is a row, so nothing is lost between the upload form and the
    confirmation:

    * an authorized upload is ``stored`` the moment its exact bytes are in the
      content-addressed store, before the person has decided anything, and an
      upload abandoned at the preview stays exactly that;
    * a byte or structure refusal is ``terminally_refused`` carrying the rule
      that refused it, with the digest of what actually arrived;
    * a storage or scanner failure is ``transient_failure``, which says nothing
      about the delivery and is why the checkpoint rule will not advance past
      one.

    ``source_revision`` is the person's declaration of *which* revision of the
    source these bytes are. It defaults to the bytes themselves, so uploading
    the same file twice converges on the delivery already taken; declaring a
    revision makes a genuinely new delivery that shares the stored object,
    because storage deduplication and delivery identity are different questions
    and identical bytes can be a new and meaningful source revision (ADR-0015
    leaves what they are to the person, and this module still never guesses it).
    """

    from corridor import source_delivery

    principal = require_human_principal(principal)
    digest = hashlib.sha256(body).hexdigest()
    binding = source_delivery.DeliveryBinding(
        customer=customer,
        project_id=project.id,
        project_slug=project.slug,
        transport="push",
        channel=PRODUCT_UPLOAD_CHANNEL,
        configuration_identity=PRODUCT_UPLOAD_CONFIGURATION,
        delivered_by_principal=principal.subject,
    )
    # The upload form is the whole run: there is no pass and no cursor, and the
    # run is not part of the delivery's identity, so a replay converges on the
    # row the first one wrote and keeps that run's name.
    run_identity = f"product-upload:{uuid4().hex}"
    # Taken before the gate, so a refusal names the exact bytes that arrived
    # and an unusable filename still leaves an identifiable delivery.
    offered = _upload_observation(
        _offered_name(filename) or digest,
        source_revision,
        digest,
        len(body),
    )
    try:
        staged = validate_and_stage(
            body,
            filename,
            max_bytes=max_bytes,
            customer_id=customer,
            project_id=project.id,
            channel=PRODUCT_UPLOAD_CHANNEL,
        )
    except IntakeRefused as exc:
        refused = source_delivery.record_delivery(
            session,
            binding,
            offered,
            disposition=source_delivery.DISPOSITION_TERMINALLY_REFUSED,
            service_identity=UPLOAD_SERVICE_IDENTITY,
            run_identity=run_identity,
            refusal_reason=f"{exc.reason}: {exc}",
        )
        raise UploadRefused(
            exc.reason, str(exc), delivery_id=refused.delivery_id
        ) from exc
    except Exception as exc:
        # The store or the scanner, not the bytes. Recorded before the request
        # fails, so the attempt survives the response that refuses it.
        failed = source_delivery.record_delivery(
            session,
            binding,
            offered,
            disposition=source_delivery.DISPOSITION_TRANSIENT_FAILURE,
            service_identity=UPLOAD_SERVICE_IDENTITY,
            run_identity=run_identity,
            refusal_reason=f"{type(exc).__name__}: {exc}",
        )
        raise UploadNotTaken(
            "This upload could not be stored. Nothing was refused about the "
            "file itself; try again.",
            delivery_id=failed.delivery_id,
        ) from exc

    recorded = source_delivery.take_delivery(
        session,
        binding,
        _upload_observation(
            staged.filename,
            source_revision,
            digest,
            staged.size_bytes,
            bytes_reference=content_key(digest, staged.suffix),
        ),
        service_identity=UPLOAD_SERVICE_IDENTITY,
        run_identity=run_identity,
    )
    # The receipt always names the row the bytes were taken on, never the
    # duplicate observation of it, so a re-posted form reaches the same one.
    taken = source_delivery.stored_delivery(
        session, idempotency_key=recorded.idempotency_key
    )
    return ReceivedUpload(
        staged=staged,
        delivery_id=int(taken.id),
        delivery_identity=taken.delivery_identity,
        replayed=recorded.disposition == source_delivery.DISPOSITION_DUPLICATE,
    )


def _upload_observation(
    external_identity: str,
    source_revision: str,
    digest: str,
    byte_count: int,
    bytes_reference: str = "",
) -> "DeliveryObservation":
    """What was handed over, and the one place an upload's identity is derived.

    The name the person gave the file identifies it, and the declared source
    revision versions it — falling back to the digest, so an undeclared
    re-upload of the same file is the same delivery rather than a second one.
    The provider's own timestamps stay empty: a browser's clock and a
    filesystem's modification time are not the source's facts about itself.
    """

    from corridor.source_delivery import DeliveryObservation

    return DeliveryObservation(
        external_identity=external_identity,
        external_version=(source_revision or "").strip() or digest,
        content_digest=digest,
        bytes_reference=bytes_reference,
        metadata={"filename": external_identity, "byte_count": byte_count},
    )


def _offered_name(filename: str) -> str:
    """The display name, or empty where the upload offered no usable one."""

    try:
        return _safe_filename(filename)
    except IntakeRefused:
        return ""


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
    source_delivery_id: int | None = None,
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

    ``source_delivery_id`` is the ledger row of the delivery these exact bytes
    arrived on (#687). An ordinary upload now holds one, because
    ``receive_upload`` took delivery of it before the preview was drawn, and
    naming it here is what makes the confirmation an act on *that* delivery:
    the row is re-proved against this project, these bytes and the ``stored``
    disposition before anything is written, and one
    ``source_delivery_confirmations`` row records who admitted it, idempotently
    (#823). `later_revision` and `key_date_table` pass one too: both refuse a
    capture whose bytes no *stored* delivery of this project holds, so the row
    they pass is proven before this is called, not inferred afterwards. What
    still holds none is a source that genuinely arrived through no transport —
    paper handed over at a meeting, registered from the curated corpus — and
    there the link stays unknown rather than guessed.
    """

    from corridor.source_delivery import DISPOSITION_STORED, confirm_delivery

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

    delivery = None
    if source_delivery_id is not None:
        delivery = session.get(SourceDelivery, int(source_delivery_id))
        if (
            delivery is None
            or delivery.project_id != project.id
            or delivery.content_sha256 != sha256
            or delivery.disposition != DISPOSITION_STORED
        ):
            raise IntakeConflict(
                "delivery_mismatch",
                "This confirmation does not match the delivery these bytes "
                "arrived on. Upload and preview the file again.",
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
        source_delivery_id=source_delivery_id,
    )
    created = existing is None
    # The admission itself, bound to the delivery rather than only to the
    # Document it produced, so a stored delivery nobody admitted is visibly
    # different from one somebody did (#823).
    confirmation = (
        None
        if delivery is None
        else confirm_delivery(session, delivery=delivery, principal=principal)
    )

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
            "source_delivery_id": source_delivery_id,
        },
    )
    return IntakeConfirmation(
        document_id=document.id,
        sha256=sha256,
        doc_type=doc_type,
        created=created,
        audit_id=entry.id,
        delivery_confirmation_id=None if confirmation is None else confirmation.id,
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


def _upload_sentence(
    exc: HostileContentRefused,
    safe: str,
    suffix: str,
    size_bytes: int,
    limit: int,
) -> str:
    """What a person handing over a file is told, for a shared gate rule.

    The rule is the gate's, and it is the same rule on every channel. The
    sentence is not: a connector writes its refusal into a ledger row an
    operator reads, and this one is shown to somebody who is standing at an
    upload form and can act on it, so it names their file, their limit, and
    what to do. Anything the gate refuses for a reason an uploader cannot act
    on keeps the gate's own wording rather than a guessed instruction.
    """

    if exc.rule == "empty_file":
        return f"{safe!r} is empty."
    if exc.rule == "size_limit_exceeded":
        return (
            f"{safe!r} is {_mib(size_bytes)} MiB; the limit is {_mib(limit)} MiB."
        )
    if exc.rule == "magic_mismatch":
        return (
            f"{safe!r} does not contain {suffix} data. Its contents do not match "
            f"its name, so it cannot be read as that kind of file."
        )
    return exc.reason


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
