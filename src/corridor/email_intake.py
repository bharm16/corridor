"""Receive untrusted mail and register it inside the project it was delivered to.

Email is evidence, not an instruction channel.  This module stores an RFC 5322
message byte-for-byte, preserves the headers that establish its deterministic
thread, and routes only by exact registered evidence (ADR-0058, ADR-0062).  It
deliberately does not parse a command from a subject/body, infer Supersession,
mint an organization, or call an extractor or model in the request.  The
transaction which stores a message also stores its route/triage residue, so a
crash cannot turn a received message into an untraceable fact.

There are two front doors, and only one of them is the current design.

``receive_pushed_message`` is it (#511).  A ``PushCredential`` — the alias the
transport actually delivered to, or a webhook secret — binds one customer and
one project through ``corridor.push_intake`` *before* a single byte of the
message is parsed, and the parse then runs inside that ``PushBinding``.  A
crafted message therefore cannot move itself between customers or projects: the
thread its headers name, the bytes it duplicates, and the rows its body cites
are all looked up inside the bound project and nowhere else.  Content-based
inference survives exactly where ADR-0078 left it, choosing among rows already
inside the boundary or leaving the choice as bounded triage.

``receive_message`` is the frozen global-address path ADR-0059 defined and
ADR-0078 superseded: one service address, and the project inferred from the
message's own content.  It is kept while deployments migrate their transports
onto bound aliases, and it gains no new capability.  Its rows are the same rows
the bound path writes, so migrating a project costs no raw MIME and no thread
history — a reply to a legacy thread continues that thread once the alias binds
the project the thread already resolved to.
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
from corridor.push_intake import (
    MAIL_CHANNELS,
    PushBinding,
    PushCredential,
    PushPayload,
    accept_delivery,
    bind_credential,
)
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
    thread = _thread_for_headers(session, headers, project_id=None)
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


def receive_pushed_message(
    session: Session,
    *,
    credential: PushCredential,
    raw_bytes: bytes,
    transport_delivery_id: str | None = None,
) -> ReceivedMessage:
    """Receive one mail delivery inside the boundary its credential establishes.

    The order is the whole security property, so it is worth stating plainly:
    bind, persist, then parse.  ``bind_credential`` resolves the alias the
    transport delivered to — never a ``To``, ``Cc``, or ``Delivered-To`` header,
    which the sender wrote — to one customer and one project.  Only then are the
    bytes stored and the message parsed, and every later lookup is scoped to the
    bound project: the thread its headers claim, the bytes it may duplicate, and
    the rows its body cites.  A message that arrives on one project's alias
    cannot end up in another project or another customer whatever it contains.

    Replay is idempotent by delivery identity (ADR-0083).  A transport that
    retries after a crash reaches the delivery already taken and the message
    already registered, not a second copy of either.
    """

    binding = bind_credential(session, credential)
    if binding.channel not in MAIL_CHANNELS:
        raise InboundMailRefused("this push credential does not carry mail")
    if not raw_bytes:
        raise InboundMailRefused("empty mail cannot enter intake")
    receipt = accept_delivery(
        session,
        binding,
        PushPayload(
            body=raw_bytes,
            filename="message.eml",
            transport_delivery_id=transport_delivery_id,
        ),
    )
    already = session.scalars(
        select(InboundMessage).where(
            InboundMessage.push_delivery_id == receipt.delivery_id
        )
    ).first()
    if already is not None:
        return ReceivedMessage(
            message_id=already.id,
            thread_id=already.thread_id,
            project_id=already.project_id,
            route_status=already.route_status,
            route_evidence=already.route_evidence_json,
            created=False,
        )
    return _register_bound_message(
        session, binding=binding, receipt=receipt, raw_bytes=raw_bytes
    )


def _register_bound_message(
    session: Session,
    *,
    binding: PushBinding,
    receipt,
    raw_bytes: bytes,
) -> ReceivedMessage:
    """Parse and register one delivery, entirely inside an established binding."""

    # The first parse in the whole path, and it happens with the customer and
    # project already decided by the credential.
    message = BytesParser(policy=policy.default).parsebytes(raw_bytes)
    digest = receipt.envelope.content_digest

    same_bytes = session.scalars(
        select(InboundMessage).where(
            InboundMessage.raw_sha256 == digest,
            InboundMessage.project_id == binding.project_id,
        )
    ).first()
    if same_bytes is not None:
        return ReceivedMessage(
            message_id=same_bytes.id,
            thread_id=same_bytes.thread_id,
            project_id=same_bytes.project_id,
            route_status=same_bytes.route_status,
            route_evidence=same_bytes.route_evidence_json,
            created=False,
        )

    message_id = _header_id(message.get("Message-ID"))
    if message_id:
        same_id = session.scalars(
            select(InboundMessage).where(
                InboundMessage.message_id == message_id,
                InboundMessage.project_id == binding.project_id,
            )
        ).first()
        if same_id is not None:
            # Same Message-ID with different bytes is not a resend.  The check
            # is inside the boundary, so a guessed identifier cannot refuse a
            # delivery in a project the sender cannot see.
            raise InboundMailRefused(
                "Message-ID was already received with different bytes"
            )

    headers = _retained_headers(message)
    body = _body_text(message)
    attachments = _attachment_parts(message)
    attachment_hashes = tuple(sha256(payload).hexdigest() for _n, payload in attachments)
    thread = _thread_for_headers(session, headers, project_id=binding.project_id)
    if thread is None:
        thread = InboundThread(project_id=binding.project_id)
        session.add(thread)
        session.flush()

    sender = _first_address(message.get("From"))
    evidence, dependency_id = _resolve_within_boundary(
        session,
        project_id=binding.project_id,
        body=body,
        attachment_hashes=attachment_hashes,
        attachment_names=tuple(name for name, _payload in attachments),
        sender=sender,
    )
    evidence = {
        "boundary": "push_credential",
        "customer": binding.customer,
        "channel": binding.channel,
        "credential_id": binding.credential_id,
        "delivery_identity": receipt.envelope.delivery_identity,
        # The bound project is the only candidate there has ever been; the key
        # keeps its shape for the readback the global-address path also feeds.
        "candidate_project_ids": [binding.project_id],
        **evidence,
    }
    if thread.dependency_id is None and dependency_id is not None:
        thread.dependency_id = dependency_id

    inbound = InboundMessage(
        raw_sha256=digest,
        storage_path=str(receipt.staged_path),
        message_id=message_id,
        sender=sender,
        subject=str(message.get("Subject") or ""),
        sent_at=_parsed_date(message.get("Date")),
        headers_json=headers,
        body_text=body,
        thread_id=thread.id,
        project_id=binding.project_id,
        route_status="routed",
        route_evidence_json=evidence,
        push_delivery_id=receipt.delivery_id,
    )
    session.add(inbound)
    session.flush()
    if thread.bound_by_message_id is None and dependency_id is not None:
        thread.bound_by_message_id = inbound.id
        session.flush()
    _register_routed_content(session, inbound, message)
    return ReceivedMessage(
        message_id=inbound.id,
        thread_id=thread.id,
        project_id=binding.project_id,
        route_status="routed",
        route_evidence=evidence,
        created=True,
    )


def _resolve_within_boundary(
    session: Session,
    *,
    project_id: int,
    body: str,
    attachment_hashes: tuple[str, ...],
    attachment_names: tuple[str, ...],
    sender: str | None,
) -> tuple[dict, int | None]:
    """Choose a row inside the bound project, never a project.

    Every tier ADR-0059 defined survives here as ADR-0078 left it: an exact
    match *inside* an already bound customer.  Evidence that resolves outside
    the boundary decides nothing and is counted rather than named, because a
    message in one customer's record must not carry another customer's
    identifiers even as a rejected candidate.

    ``in_project_resolution`` is the honest outcome of the inference the
    boundary still permits: ``row_bound`` when exactly one Constraint in the
    project matched, ``ambiguous`` when several did — the bounded triage
    ADR-0078 allows, left visible with the row unbound — and ``unmatched``
    when the message cites no row this project holds.
    """

    outside = 0
    tier = "none"

    matched_documents = (
        list(
            session.scalars(
                select(Document.project_id).where(Document.sha256.in_(attachment_hashes))
            ).all()
        )
        if attachment_hashes
        else []
    )
    if not matched_documents and attachment_names:
        joined_names = " ".join(attachment_names)
        matched_documents = [
            document.project_id
            for document in session.scalars(
                select(Document).where(Document.registry_id.is_not(None))
            ).all()
            if _contains_exact_identifier(
                joined_names, normalize_identifier(document.registry_id)
            )
        ]
    outside += sum(1 for owner in matched_documents if owner != project_id)
    if any(owner == project_id for owner in matched_documents):
        tier = "document_identity"

    identifier_owners = [
        item.project_id
        for item in session.scalars(select(IntakeProjectIdentifier)).all()
        if _contains_exact_identifier(body, item.value_normalized)
    ]
    outside += sum(1 for owner in identifier_owners if owner != project_id)
    if tier == "none" and any(owner == project_id for owner in identifier_owners):
        tier = "project_identifier"

    matched_dependencies = [
        item
        for item in session.scalars(select(Dependency)).all()
        if _dependency_mentions(body, item)
    ]
    outside += sum(1 for item in matched_dependencies if item.project_id != project_id)
    in_project = sorted(
        item.id for item in matched_dependencies if item.project_id == project_id
    )
    if tier == "none" and in_project:
        tier = "record_identifier"

    sender_owners = _sender_contact_project_ids(session, sender)
    if tier == "none" and project_id in sender_owners:
        tier = "sender_contact"

    if len(in_project) == 1:
        resolution, dependency_id = "row_bound", in_project[0]
    elif in_project:
        resolution, dependency_id = "ambiguous", None
    else:
        resolution, dependency_id = "unmatched", None

    return {
        "tier": tier,
        "in_project_resolution": resolution,
        "candidate_dependency_ids": in_project,
        "out_of_boundary_evidence": outside,
        "sender_registered_contact_project_ids": sorted(
            sender_owners & {project_id}
        ),
    }, dependency_id


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


def _thread_for_headers(
    session: Session, headers: dict, *, project_id: int | None
) -> InboundThread | None:
    """Resolve a reply's thread from its headers alone, inside one boundary.

    ``project_id`` is the bound project of a pushed delivery, or ``None`` on the
    frozen global-address path.  When it is given, a reference is only followed
    to a message and a thread already in that project, so a forged In-Reply-To
    or References chain naming another customer's conversation resolves to
    nothing and starts a new thread rather than joining theirs.
    """

    references = [headers.get("in_reply_to"), *headers.get("references", [])]
    references = [value for value in references if value]
    if not references:
        return None
    referenced = select(InboundMessage.thread_id).where(
        InboundMessage.message_id.in_(references)
    )
    if project_id is not None:
        referenced = referenced.where(InboundMessage.project_id == project_id)
    thread_ids = set(session.scalars(referenced).all())
    # A References chain claiming two known Corridor threads is malformed
    # provenance.  Starting a new thread keeps both original conversations
    # intact instead of selecting one by incidental database ordering.
    if len(thread_ids) != 1:
        return None
    thread = session.get(InboundThread, next(iter(thread_ids)))
    if project_id is not None and (thread is None or thread.project_id != project_id):
        return None
    return thread


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
