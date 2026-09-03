"""Receive untrusted mail at Corridor's one configured intake address.

Email is evidence, not an instruction channel.  This module stores an RFC 5322
message byte-for-byte, preserves the headers that establish its deterministic
thread, and routes only by exact registered evidence (ADR-0058, ADR-0059,
ADR-0062).  It deliberately does not parse a command from a subject/body, infer
Supersession, mint an organization, or call an extractor or model in the request.

The public seam is ``receive_message``.  A deployment webhook or a service-mailbox
poller authenticates *before* calling it; this module accepts only the server
received raw bytes and refuses mail not addressed to the configured service
address.  The transaction which stores a message also stores its route/triage
residue, so a crash cannot turn a received message into an untraceable fact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from hashlib import sha256
from pathlib import Path
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.config import settings
from corridor.ingest import ingest_document
from corridor.models import (
    Dependency,
    Document,
    InboundMessage,
    InboundRouteTriage,
    InboundThread,
    IntakeProjectIdentifier,
    Project,
)
from corridor.object_storage import store_bytes
from corridor.principals import HumanPrincipal, require_human_principal
from corridor.source_intake import IntakeRefused, validate_and_stage


class InboundMailRefused(ValueError):
    """The server received bytes that are outside the one-address boundary."""


class InboundRouteConflict(ValueError):
    """A triage answer is stale, foreign, or attempts to rewrite a route."""


@dataclass(frozen=True, slots=True)
class ReceivedMessage:
    message_id: int
    thread_id: int
    project_id: int | None
    route_status: str
    route_evidence: dict
    created: bool


def normalize_identifier(value: str) -> str:
    """Normalize case and spacing only; routing remains an exact identifier match."""

    return " ".join((value or "").split()).casefold()


def register_project_identifier(
    session: Session, *, project: Project, kind: str, value: str
) -> IntakeProjectIdentifier:
    """Register one agency-issued routing identifier, never a project-name alias."""

    kind = normalize_identifier(kind)
    value = normalize_identifier(value)
    if not kind or not value:
        raise ValueError("an intake identifier needs a kind and value")
    existing = session.scalars(
        select(IntakeProjectIdentifier).where(
            IntakeProjectIdentifier.project_id == project.id,
            IntakeProjectIdentifier.kind == kind,
            IntakeProjectIdentifier.value_normalized == value,
        )
    ).first()
    if existing is not None:
        return existing
    record = IntakeProjectIdentifier(
        project_id=project.id, kind=kind, value_normalized=value
    )
    session.add(record)
    session.flush()
    return record


def receive_message(
    session: Session,
    *,
    raw_bytes: bytes,
    service_address: str,
) -> ReceivedMessage:
    """Store one delivered raw message and its deterministic project route.

    Duplicate raw bytes are idempotent.  Headers alone establish a reply's
    thread: an unknown In-Reply-To/References chain starts a new thread rather
    than borrowing a similarly-worded conversation.  A known routed thread
    wins only after its headers resolve to it, preventing spoofed headers from
    binding a new thread to an unrelated subject.
    """

    if not raw_bytes:
        raise InboundMailRefused("empty mail cannot enter intake")
    message = BytesParser(policy=policy.default).parsebytes(raw_bytes)
    if not _addresses_service(message, service_address):
        raise InboundMailRefused("mail was not delivered to the configured intake address")

    digest = sha256(raw_bytes).hexdigest()
    existing = session.scalars(
        select(InboundMessage).where(InboundMessage.raw_sha256 == digest)
    ).first()
    if existing is not None:
        return ReceivedMessage(
            message_id=existing.id,
            thread_id=existing.thread_id,
            project_id=existing.project_id,
            route_status=existing.route_status,
            route_evidence=existing.route_evidence_json,
            created=False,
        )

    message_id = _header_id(message.get("Message-ID"))
    if message_id:
        same_id = session.scalars(
            select(InboundMessage).where(InboundMessage.message_id == message_id)
        ).first()
        if same_id is not None:
            # Same Message-ID with different bytes is not a resend.  Preserve no
            # ambiguous provenance and make the transport retry with canonical data.
            raise InboundMailRefused("Message-ID was already received with different bytes")

    headers = _retained_headers(message)
    body = _body_text(message)
    attachments = _attachment_parts(message)
    attachment_hashes = tuple(sha256(payload).hexdigest() for _n, payload in attachments)
    thread = _thread_for_headers(session, headers)
    if thread is None:
        thread = InboundThread()
        session.add(thread)
        session.flush()

    sender = _first_address(message.get("From"))
    project_id, evidence, dependency_id = _route_project(
        session,
        thread=thread,
        body=body,
        attachment_hashes=attachment_hashes,
        attachment_names=tuple(name for name, _payload in attachments),
        sender=sender,
    )
    # The sender matching a registered row contact is retained as attribution
    # evidence under the existing identity tiers (ADR-0051) whether or not it
    # was the deciding routing tier. It strengthens a later match; it never
    # becomes an authority or mints an Organization.
    evidence = {
        **evidence,
        "sender_registered_contact_project_ids": sorted(
            _sender_contact_project_ids(session, sender)
        ),
    }
    if project_id is not None:
        thread.project_id = project_id
        if thread.dependency_id is None and dependency_id is not None:
            thread.dependency_id = dependency_id
        status = "routed"
    else:
        status = "triage"
        # A contradictory reply keeps its raw provenance and conflict evidence,
        # but must not create a card capable of rewriting the routed thread it
        # referenced.  Only an unbound thread has an honest routing question.
        if thread.project_id is None:
            _ensure_triage(session, thread, evidence["candidate_project_ids"])

    path = _store_raw(digest, raw_bytes)
    inbound = InboundMessage(
        raw_sha256=digest,
        storage_path=str(path),
        message_id=message_id,
        sender=sender,
        subject=str(message.get("Subject") or ""),
        sent_at=_parsed_date(message.get("Date")),
        headers_json=headers,
        body_text=body,
        thread_id=thread.id,
        project_id=project_id,
        route_status=status,
        route_evidence_json=evidence,
    )
    session.add(inbound)
    session.flush()
    # The cited row binding is recorded only after the immutable message has an
    # id.  Replies inherit the existing binding; no content on a reply can move
    # a thread to another row.
    if thread.bound_by_message_id is None and dependency_id is not None:
        thread.bound_by_message_id = inbound.id
        session.flush()
    if project_id is not None:
        _register_routed_content(session, inbound, message)
    return ReceivedMessage(
        message_id=inbound.id,
        thread_id=thread.id,
        project_id=project_id,
        route_status=status,
        route_evidence=evidence,
        created=True,
    )


def resolve_route_triage(
    session: Session,
    *,
    thread_id: int,
    project_id: int,
    principal: HumanPrincipal,
) -> None:
    """Record the one attributable choice for an unresolved thread.

    The answer binds the thread once.  Later replies inherit it through their
    headers; neither a name nor a later message can rewrite it.
    """

    principal = require_human_principal(principal)
    triage = session.scalars(
        select(InboundRouteTriage).where(InboundRouteTriage.thread_id == thread_id)
    ).first()
    thread = session.get(InboundThread, thread_id)
    if triage is None or thread is None or triage.state != "pending":
        raise InboundRouteConflict("this thread has no pending route triage")
    if thread.project_id is not None:
        raise InboundRouteConflict(
            "a route triage cannot rewrite this already routed thread"
        )
    if session.get(Project, project_id) is None:
        raise InboundRouteConflict("the selected project does not exist")
    thread.project_id = project_id
    triage.state = "resolved"
    triage.resolved_project_id = project_id
    triage.resolved_by = principal.subject
    triage.resolved_at = datetime.now().astimezone()
    session.flush()
    # The answered thread now has a project, so its retained messages register
    # their content the same way an automatically routed message does.
    for waiting in session.scalars(
        select(InboundMessage)
        .where(InboundMessage.thread_id == thread_id)
        .order_by(InboundMessage.id)
    ).all():
        if waiting.project_id is None:
            waiting.project_id = project_id
            waiting.route_status = "routed"
            stored = Path(waiting.storage_path)
            if stored.exists():
                parsed = BytesParser(policy=policy.default).parsebytes(
                    stored.read_bytes()
                )
                _register_routed_content(session, waiting, parsed)
    session.flush()


def _register_routed_content(session: Session, inbound: InboundMessage, message) -> None:
    """Register a routed message's body and attachments as project sources.

    Everything here is the ordinary shared intake: #349's bounded limits and
    content-addressed staging for attachments, `ingest_document` registration
    and page parsing for both, identical bytes deduping to the already
    registered Document. What is deliberately absent is any inference —
    supersession, rendition equivalence, document dates, registry ids, and
    organizations all stay exactly as unresolved as an upload leaves them.
    """

    if inbound.body_text.strip() and inbound.document_id is None:
        body_document = ingest_document(
            session,
            project_id=inbound.project_id,
            path=Path(inbound.storage_path),
            doc_type="email",
            images_dir=settings.corpus_images,
            filename=_message_filename(inbound),
        )
        inbound.document_id = body_document.id
    receipts = list(inbound.attachments_json or [])
    already = {receipt.get("sha256") for receipt in receipts}
    for filename, payload in _attachment_parts(message):
        digest = sha256(payload).hexdigest()
        if digest in already:
            continue
        try:
            staged = validate_and_stage(payload, filename or f"{digest[:12]}.bin")
            registered = ingest_document(
                session,
                project_id=inbound.project_id,
                path=staged.stored_path,
                doc_type="other",
                images_dir=settings.corpus_images,
                filename=staged.filename,
            )
            receipts.append(
                {
                    "filename": staged.filename,
                    "sha256": staged.sha256,
                    "document_id": registered.id,
                }
            )
        except IntakeRefused as refusal:
            # The raw message retains the bytes; the receipt says exactly why
            # this attachment is not a registered Document.
            receipts.append(
                {
                    "filename": filename,
                    "sha256": digest,
                    "refused": refusal.reason,
                }
            )
        already.add(digest)
    inbound.attachments_json = receipts
    session.flush()


def _attachment_parts(message) -> tuple[tuple[str, bytes], ...]:
    return tuple(
        (str(part.get_filename() or ""), part.get_payload(decode=True))
        for part in message.iter_attachments()
        if part.get_payload(decode=True) is not None
    )


def _message_filename(inbound: InboundMessage) -> str:
    subject = " ".join((inbound.subject or "").split())
    return f"{subject or 'message'} ({inbound.raw_sha256[:12]}).eml"


def _route_project(
    session: Session,
    *,
    thread: InboundThread,
    body: str,
    attachment_hashes: tuple[str, ...],
    attachment_names: tuple[str, ...],
    sender: str | None,
) -> tuple[int | None, dict, int | None]:
    """Apply ADR-0059's ordered exact tiers, retaining the deciding evidence."""

    document_ids = (
        set(
            session.scalars(
                select(Document.project_id).where(
                    Document.sha256.in_(attachment_hashes)
                )
            ).all()
        )
        if attachment_hashes
        else set()
    )
    if not document_ids and attachment_names:
        # A revision carries new bytes but the same registered document
        # identity. The registry id is an agency-issued identifier — never a
        # name — so an exact registry-id token in an attachment's filename is
        # document-identity evidence for that document's project.
        registered = session.scalars(
            select(Document).where(Document.registry_id.is_not(None))
        ).all()
        joined_names = " ".join(attachment_names)
        document_ids = {
            document.project_id
            for document in registered
            if _contains_exact_identifier(
                joined_names, normalize_identifier(document.registry_id)
            )
        }
    dependency_id: int | None = None
    if document_ids:
        independent = _single_or_triage("document_identity", document_ids)
    else:
        identifiers = list(session.scalars(select(IntakeProjectIdentifier)).all())
        identifier_ids = {
            item.project_id
            for item in identifiers
            if _contains_exact_identifier(body, item.value_normalized)
        }
        if identifier_ids:
            independent = _single_or_triage("project_identifier", identifier_ids)
        else:
            dependencies = [
                item for item in session.scalars(select(Dependency)).all()
                if _dependency_mentions(body, item)
            ]
            dependency_ids = {item.project_id for item in dependencies}
            if len(dependencies) == 1:
                dependency_id = dependencies[0].id
            if dependency_ids:
                independent = _single_or_triage("record_identifier", dependency_ids)
            else:
                sender_ids = _sender_contact_project_ids(session, sender)
                independent = (
                    _single_or_triage("sender_contact", sender_ids)
                    if sender_ids
                    else (None, {"tier": "none", "candidate_project_ids": []})
                )

    if thread.project_id is None:
        return (*independent, dependency_id)
    _project_id, independent_evidence = independent
    candidates = set(independent_evidence["candidate_project_ids"])
    if candidates and thread.project_id not in candidates:
        # A forged References header must not let a project-A message swallow
        # independent project-B evidence.  The reply's own evidence points only
        # at other projects, so preserve the contradiction for a person rather
        # than accepting whichever header arrived first.  When the thread's own
        # project is among the reply's candidates, the deterministic header
        # chain legitimately disambiguates the ambiguity a human already
        # resolved (ADR-0062), so the binding is inherited below.
        return None, {
            "tier": "thread_header_conflict",
            "candidate_project_ids": sorted(candidates | {thread.project_id}),
        }, None
    return thread.project_id, {
        "tier": "thread_headers",
        "candidate_project_ids": [thread.project_id],
    }, thread.dependency_id


def _single_or_triage(tier: str, candidates: set[int]) -> tuple[int | None, dict]:
    ids = sorted(candidates)
    return (
        (ids[0] if len(ids) == 1 else None),
        {"tier": tier, "candidate_project_ids": ids},
    )


def _dependency_mentions(body: str, dependency: Dependency) -> bool:
    values = (dependency.ref_code, dependency.source_ref, dependency.station_from, dependency.station_to)
    return any(value and _contains_exact_identifier(body, normalize_identifier(value)) for value in values)


def _sender_contact_project_ids(session: Session, sender: str | None) -> set[int]:
    """Return projects whose retained row contact states this exact email address.

    A sender is provenance and a narrowing signal, never an Organization-creation
    rule.  The matrix's contact cell is retained source wording, so parse email
    tokens only; matching a person's display name would reintroduce name routing.
    """

    normalized_sender = (sender or "").strip().casefold()
    if not normalized_sender:
        return set()
    return {
        dependency.project_id
        for dependency in session.scalars(select(Dependency)).all()
        if normalized_sender
        in {
            address.casefold()
            for address in re.findall(
                r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
                dependency.external_contact or "",
                flags=re.IGNORECASE,
            )
        }
    }


def _contains_exact_identifier(text: str, value: str) -> bool:
    if not value:
        return False
    # Whitespace/case normalization is the only tolerance.  Boundaries prevent
    # CSJ 12 from matching 112 and preserve the no-name-routing rule.
    pattern = r"(?<![\w])" + re.escape(value).replace(r"\ ", r"\s+") + r"(?![\w])"
    return re.search(pattern, normalize_identifier(text)) is not None


def _thread_for_headers(session: Session, headers: dict) -> InboundThread | None:
    references = [headers.get("in_reply_to"), *headers.get("references", [])]
    references = [value for value in references if value]
    if not references:
        return None
    thread_ids = set(
        session.scalars(
            select(InboundMessage.thread_id).where(InboundMessage.message_id.in_(references))
        ).all()
    )
    # A References chain claiming two known Corridor threads is malformed
    # provenance.  Starting a new thread keeps both original conversations
    # intact instead of selecting one by incidental database ordering.
    return session.get(InboundThread, next(iter(thread_ids))) if len(thread_ids) == 1 else None


def _ensure_triage(session: Session, thread: InboundThread, candidate_ids: list[int]) -> None:
    existing = session.scalars(select(InboundRouteTriage).where(InboundRouteTriage.thread_id == thread.id)).first()
    if existing is None:
        session.add(InboundRouteTriage(thread_id=thread.id, candidate_project_ids=candidate_ids))


def _retained_headers(message) -> dict:
    return {
        "message_id": _header_id(message.get("Message-ID")),
        "in_reply_to": _header_id(message.get("In-Reply-To")),
        "references": _header_ids(message.get("References")),
        "from": str(message.get("From") or ""),
        "to": str(message.get("To") or ""),
        "cc": str(message.get("Cc") or ""),
        "date": str(message.get("Date") or ""),
        "subject": str(message.get("Subject") or ""),
    }


def _header_id(value: str | None) -> str | None:
    ids = _header_ids(value)
    return ids[0] if ids else None


def _header_ids(value: str | None) -> list[str]:
    return re.findall(r"<[^<>\s]+>", value or "")


def _addresses_service(message, service_address: str) -> bool:
    service = (service_address or "").strip().casefold()
    if not service:
        raise InboundMailRefused("the service address is not configured")
    # Python's strict address parser rejects the whole list if blank header
    # placeholders accompany an otherwise valid address, so pass only headers
    # that were actually present.
    fields = [str(message.get(name)) for name in ("To", "Cc", "Delivered-To") if message.get(name)]
    addresses = getaddresses(fields)
    return any(_is_service_alias(address, service) for _name, address in addresses)


def _is_service_alias(address: str, service: str) -> bool:
    local, separator, domain = address.casefold().partition("@")
    service_local, service_separator, service_domain = service.partition("@")
    if not separator or not service_separator or domain != service_domain:
        return False
    return local.split("+", 1)[0] == service_local


def _first_address(value: str | None) -> str | None:
    addresses = getaddresses([value or ""])
    return addresses[0][1].casefold() if addresses else None


def _body_text(message) -> str:
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                return str(part.get_content() or "")
        return ""
    return str(message.get_content() or "") if message.get_content_type() == "text/plain" else ""


def _parsed_date(value: str | None) -> datetime | None:
    try:
        parsed = parsedate_to_datetime(value) if value else None
        return parsed.astimezone() if parsed and parsed.tzinfo else parsed
    except (TypeError, ValueError):
        return None


def _store_raw(digest: str, raw_bytes: bytes) -> Path:
    # The one content-addressed store every source shares, so `stored_file`
    # resolves a registered raw message exactly like any other Document.
    return store_bytes(raw_bytes, sha256=digest, suffix=".eml")
