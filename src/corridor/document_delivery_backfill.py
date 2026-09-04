"""Link already-registered Documents to the delivery that carried them (#687).

Every intake path that holds a `source_deliveries` row now writes
`documents.source_delivery_id` in the transaction that registers the Document
(`ingest_document`, `source_intake.confirm_intake`, `later_revision`,
`key_date_table`, `email_intake`).  Documents registered *before* that was true
carry a null link, and a null link is not a small cosmetic gap: #675's "Can
wait" limb reads it, and `deltas_outside_coverage_boundary` returns nothing for
an unlinked delta by design, because a reading never claims a check it did not
make.  So the historical rows need the link filled in — but only where the
records already prove it.

**Two rules, both reading a relationship that was retained at the time.**

1. An `inbound_messages` row names both the delivery it arrived on
   (`push_delivery_id`) and the Document its own bytes registered as
   (`document_id`).  That is a foreign key someone already wrote; nothing here
   infers it.
2. A `capture_later_source_revision` or `capture_key_date_table` audit entry is
   the receipt of a capture that *had already refused* to run unless a `stored`
   delivery of this project held these exact bytes.  The receipt names the
   Document, the content digest, the customer's own identity for the source and
   the external version it arrived at, so the ledger row is looked up by those
   four facts and taken only when exactly one matches.

**What this deliberately will not do.** It never orders deliveries or documents
by time to pair them — the same rule #645 records for the retirement watermark,
and the reason #675 keyed the boundary by identity in the first place: a server
clock says when a row was written, not when a source arrived.  It never
overwrites a link that is already set, because identical bytes can be delivered
twice and the first delivery is the one this document came in on.  It leaves
every ambiguous or unproven case null: a corpus document, an adopted baseline
workbook, a manual upload, an email attachment, and any receipt whose digest
matches more than one stored delivery all stay unknown, which is a supported
state and a truthful one.

What was considered and rejected: sweeping `documents.sha256` against every
stored delivery digest in the project.  It links more rows, and most of them
would even be right, but it also claims a delivery for a corpus file whose
bytes happen to have been delivered later, and it claims one on evidence that
is a coincidence of content rather than a record of provenance.  The receipts
above say what actually happened, so they are what this reads.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor import audit
from corridor.models import AuditLog, Document, InboundMessage, SourceDelivery

# The capture receipts that prove a delivery, because the capture that wrote
# one had already refused to run without a stored delivery holding these exact
# bytes for this project.
CAPTURE_ACTIONS = (
    audit.CAPTURE_LATER_SOURCE_REVISION,
    audit.CAPTURE_KEY_DATE_TABLE,
)

INBOUND_MESSAGE_RULE = "inbound_message_delivery"
CAPTURE_RECEIPT_RULE = "capture_receipt_delivery"


@dataclass(frozen=True, slots=True)
class LinkReport:
    """What was linked, by which rule, and what was left unknown."""

    linked: dict[str, int]
    left_unknown: int

    @property
    def total(self) -> int:
        return sum(self.linked.values())

    def __str__(self) -> str:
        by_rule = ", ".join(
            f"{rule}: {count}" for rule, count in sorted(self.linked.items())
        )
        return (
            f"linked {self.total} document(s) to the delivery that carried them "
            f"({by_rule or 'none'}); {self.left_unknown} left unknown"
        )


def link_documents_to_deliveries(
    session: Session, *, project_id: int | None = None
) -> LinkReport:
    """Fill `documents.source_delivery_id` where a retained record proves it.

    Runs in the caller's transaction.  Idempotent: a second pass finds every
    link it made already set and changes nothing.  ``project_id`` bounds the
    pass to one project; omitted, it considers every project.
    """

    linked: dict[str, int] = {}
    unknown = 0
    for rule, pairs in (
        (INBOUND_MESSAGE_RULE, _from_inbound_messages(session, project_id)),
        (CAPTURE_RECEIPT_RULE, _from_capture_receipts(session, project_id)),
    ):
        for document, delivery_id in pairs:
            if delivery_id is None:
                unknown += 1
                continue
            document.source_delivery_id = delivery_id
            session.add(document)
            linked[rule] = linked.get(rule, 0) + 1
    session.flush()
    return LinkReport(linked=linked, left_unknown=unknown)


def _unlinked(session: Session, document_id: int | None, project_id: int | None):
    """The Document this record names, when it is still unlinked and in scope."""

    if document_id is None:
        return None
    document = session.get(Document, int(document_id))
    if document is None or document.source_delivery_id is not None:
        return None
    if project_id is not None and document.project_id != int(project_id):
        return None
    return document


def _from_inbound_messages(session: Session, project_id: int | None):
    """Rule 1: the delivery an inbound message already names, and its Document."""

    rows = session.scalars(
        select(InboundMessage)
        .where(
            InboundMessage.push_delivery_id.is_not(None),
            InboundMessage.document_id.is_not(None),
        )
        .order_by(InboundMessage.id)
    ).all()
    for message in rows:
        document = _unlinked(session, message.document_id, project_id)
        if document is None:
            continue
        delivery = session.get(SourceDelivery, int(message.push_delivery_id))
        # The composite foreign key would refuse a cross-project link anyway;
        # skipping it here means the pass reports it rather than aborting.
        if delivery is None or delivery.project_id != document.project_id:
            yield document, None
            continue
        yield document, int(delivery.id)


def _from_capture_receipts(session: Session, project_id: int | None):
    """Rule 2: the delivery a capture receipt proves, when exactly one matches."""

    receipts = session.scalars(
        select(AuditLog)
        .where(
            AuditLog.action.in_(CAPTURE_ACTIONS),
            AuditLog.entity_type == audit.DOCUMENT,
        )
        .order_by(AuditLog.id)
    ).all()
    for receipt in receipts:
        document = _unlinked(session, receipt.entity_id, project_id)
        if document is None:
            continue
        recorded = receipt.after_json or {}
        digest = str(recorded.get("content_sha256") or "")
        identity = str(recorded.get("external_identity") or "")
        version = str(recorded.get("external_version") or "")
        # The receipt must be describing this Document's own bytes. A receipt
        # naming another digest names another source, whatever it points at.
        if not digest or not identity or not version or document.sha256 != digest:
            yield document, None
            continue
        matches = session.scalars(
            select(SourceDelivery.id).where(
                SourceDelivery.project_id == document.project_id,
                SourceDelivery.content_sha256 == digest,
                SourceDelivery.external_identity == identity,
                SourceDelivery.external_version == version,
                SourceDelivery.disposition == "stored",
            )
        ).all()
        # More than one stored delivery answering to the same four facts is an
        # ambiguity, and an ambiguity is left unknown rather than guessed.
        yield document, (int(matches[0]) if len(matches) == 1 else None)


def _usage() -> int:
    print("usage: document-delivery-backfill link [<project-slug>]", file=sys.stderr)
    print(
        "Fill documents.source_delivery_id where a retained receipt proves the "
        "delivery; leave every unproven document unknown.",
        file=sys.stderr,
    )
    return 2


def main(argv: list[str], *, session_factory=None) -> int:
    if not argv or argv[0] != "link" or len(argv) > 2:
        return _usage()
    slug = argv[1] if len(argv) == 2 else None

    if session_factory is None:
        from corridor.db import WorkerSession as session_factory

    from corridor.models import Project

    with session_factory() as session:
        project_id = None
        if slug is not None:
            project_id = session.scalar(
                select(Project.id).where(Project.slug == slug)
            )
            if project_id is None:
                print(f"no project with slug {slug!r}", file=sys.stderr)
                return 2
        report = link_documents_to_deliveries(session, project_id=project_id)
        session.commit()
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
