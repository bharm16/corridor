"""Replay recorded Graph deliveries into synthetic projects through normal intake.

This is #497's build-only entry point. The four-method adapter supplies exact
bytes; the existing delivery ledger and MIME/document intake own registration.
No tenant credential, model, accepted-record command or live factory is used.
The caller commits before using the returned cursor; a failed transaction
cannot publish an advance. Repeating a recording converges on retained input.
"""

import argparse
import base64
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session
from corridor.config import settings

from corridor.connectors.microsoft365 import GraphLocation, Microsoft365PullConnector, RecordedGraphTransport
from corridor.connectors.pull_connector import sync_pull_connector
from corridor.email_intake import receive_pulled_message
from corridor.ingest import ingest_document
from corridor.models import Project, DueWorkSchedule, ConnectorCheckpointAdvance
from corridor.object_storage import content_store, store_bytes
from corridor.source_delivery import (DeliveryBinding, DeliveryObservation, record_delivery,
    require_stored_envelope, current_checkpoint_token, record_checkpoint_advance)
from corridor.due_work import ConnectorPollingDeclaration, configure_connector_polling
from corridor.project_lock import lock_project


class RecordingDeliveryLedger:
    """The normal delivery relation, inside the replay's one transaction."""

    def __init__(self, session: Session, binding: DeliveryBinding, run_identity: str):
        self.session, self.binding, self.run_identity = session, binding, run_identity

    def record(self, item, *, disposition, content_digest, bytes_reference, refusal_reason):
        return record_delivery(self.session, self.binding,
            DeliveryObservation(item.item_id, item.version_id, content_digest, bytes_reference,
                                item.original_timestamps, item.metadata),
            disposition=disposition, service_identity="corridor.m365-replay",
            run_identity=self.run_identity, refusal_reason=refusal_reason).delivery_id


def replay_graph(session: Session, *, project: Project, customer: str,
                 connector: Microsoft365PullConnector, run_identity: str,
                 doc_types: dict[str, str] | None = None,
                 attachment_doc_types: dict[str, dict[str, str]] | None = None,
                 cursor: str | None = None, observed_at: datetime | None = None) -> dict:
    """Register a recording; caller must commit before publishing its cursor."""
    if not project.is_synthetic or not isinstance(connector.transport, RecordedGraphTransport):
        raise ValueError("offline Graph replay requires a synthetic project and recorded transport")
    if not customer.strip() or not run_identity.strip():
        raise ValueError("Graph replay requires bound customer and run identity")
    location = connector.location
    configuration = sha256(json.dumps(asdict(location), sort_keys=True).encode()).hexdigest()
    instant = observed_at or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("Graph replay requires an aware observation time")
    binding = DeliveryBinding(customer, project.id, project.slug, "pull", location.channel,
                              "m365-graph-recording-v1", configuration)
    registered: list[dict] = []

    def register(envelope):
        delivery = require_stored_envelope(session, envelope)
        if location.kind == "mailbox":
            message = receive_pulled_message(session, envelope=envelope,
                attachment_doc_types=(attachment_doc_types or {}).get(envelope.metadata["native_id"]))
            registered.append({"delivery_id": delivery.id, "message_id": message.message_id,
                               "thread_id": message.thread_id})
        else:
            kind = (doc_types or {}).get(envelope.metadata["native_id"])
            if not kind or kind == "email":
                raise ValueError("each library item requires an explicit source type mapping")
            filename = envelope.metadata["filename"]
            body = content_store().get(envelope.bytes_reference, sha256=envelope.content_digest)
            path = store_bytes(body, sha256=envelope.content_digest, suffix=Path(filename).suffix.lower())
            document = ingest_document(session, project_id=project.id, path=path, doc_type=kind,
                images_dir=settings.corpus_images,
                filename=filename, expected_sha256=envelope.content_digest, source_delivery_id=delivery.id)
            registered.append({"delivery_id": delivery.id, "document_id": document.id,
                               "external_version": envelope.external_version})

    with session.begin_nested():
        lock_project(session, project.id)
        schedule = session.scalar(select(DueWorkSchedule).where(
            DueWorkSchedule.project_id == project.id,
            DueWorkSchedule.handler_key == "connector_polling",
            DueWorkSchedule.configuration_version == configuration))
        if schedule is None:
            schedule = configure_connector_polling(session, ConnectorPollingDeclaration.released_hourly(
                project_id=project.id, configuration_version=configuration, customer=customer,
                channel=location.channel, connector_identity="m365-graph-recording-v1",
                source_url=location.delta_url, starts_at=instant.replace(minute=0, second=0, microsecond=0)), now=instant)
            # This configuration owns the cursor, but schedules no live work.
            schedule.disabled_at = instant
            session.flush()
        if schedule.scope_json.get("customer") != customer or schedule.scope_json.get("source_url") != location.delta_url:
            raise ValueError("recording configuration is already bound differently")
        if schedule.disabled_at is None:
            raise ValueError("offline Graph cursor configuration must not schedule live work")
        input_digest = sha256(json.dumps({"recording": connector.transport.content_sha256,
            "doc_types": doc_types, "attachments": attachment_doc_types, "cursor": cursor}, sort_keys=True).encode()).hexdigest()
        service = "corridor.m365-replay/" + input_digest
        existing = session.scalar(select(ConnectorCheckpointAdvance).where(
            ConnectorCheckpointAdvance.schedule_id == schedule.id,
            ConnectorCheckpointAdvance.run_identity == run_identity))
        if existing is not None and existing.service_identity != service:
            raise ValueError("Graph run identity already names another recording")
        current_cursor = current_checkpoint_token(session, schedule.id)
        if existing is None and cursor is not None and cursor != current_cursor:
            raise ValueError("Graph requested cursor differs from durable checkpoint")
        start_cursor = (session.scalar(select(ConnectorCheckpointAdvance.checkpoint_token).where(
            ConnectorCheckpointAdvance.schedule_id == schedule.id,
            ConnectorCheckpointAdvance.id < existing.id).order_by(ConnectorCheckpointAdvance.id.desc()).limit(1))
            if existing is not None else current_cursor)
        sync = sync_pull_connector(connector, customer=customer, project=project.slug,
            channel=location.channel, cursor=start_cursor,
            ledger=RecordingDeliveryLedger(session, binding, run_identity), on_envelope_stored=register)
        if set(attachment_doc_types or {}) - {e.metadata["native_id"] for e in sync.envelopes}:
            raise ValueError("attachment mappings name messages outside this recorded round")
        if sync.advanced and existing is None:
            record_checkpoint_advance(session, project_id=project.id, schedule_id=schedule.id,
                configuration_identity=binding.configuration_identity, configuration_version=configuration,
                channel=location.channel, checkpoint_token=sync.checkpoint_token,
                service_identity=service, run_identity=run_identity, delivery_ids=sync.delivery_ids())
    return {"schema_version": "m365-recording-replay-v1", "synthetic": True,
            "project_id": project.id, "customer": customer, "configuration_sha256": configuration,
            "schedule_id": schedule.id,
            "checkpoint_token": sync.checkpoint_token, "advanced": sync.advanced,
            "registered": registered, "dispositions": [row.disposition for row in sync.records]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    parser.add_argument("--project-id", required=True, type=int)
    parser.add_argument("--customer", required=True)
    parser.add_argument("--run-identity", required=True)
    parser.add_argument("--cursor")
    args = parser.parse_args()
    data = json.loads(args.recording.read_text())
    location = GraphLocation(**data["location"])
    versions = {}
    for item in data["versions"]:
        key = (item["item_id"], item["version_id"])
        body = base64.b64decode(item["body_base64"], validate=True)
        if sha256(body).hexdigest() != item["sha256"] or key in versions:
            raise ValueError("recorded Graph version digest or unique identity failed")
        versions[key] = body
    connector = Microsoft365PullConnector(location,
        RecordedGraphTransport(pages=data["pages"], versions=versions))
    from corridor.db import WorkerSession as DatabaseSession
    with DatabaseSession() as session, session.begin():
        result = replay_graph(session, project=session.get_one(Project, args.project_id),
            customer=args.customer, connector=connector, run_identity=args.run_identity,
            cursor=args.cursor, doc_types=data.get("doc_types"),
            attachment_doc_types=data.get("attachment_doc_types"))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
