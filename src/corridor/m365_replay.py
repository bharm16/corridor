"""Replay recorded Graph deliveries into synthetic projects through normal intake.

This is #497's build-only entry point.  The four-method adapter supplies exact
bytes; the shared delivery ledger and registration consumer
(``corridor.delivery_registration``) own the rest.  No tenant credential, model,
accepted-record command or live factory is used.  The caller commits before
using the returned cursor; a failed transaction cannot publish an advance.
Repeating a recording converges on retained input.

What this module stopped being is as important as what it is.  It held the only
code in the product that registered what a pull connector delivered, and around
that it had grown a second polling runtime beside ``connector_polling``: its own
delivery ledger, its own registration of a Document and a message, and its own
transaction shape.  The ledger and the registration are now the shared ones, so
what remains here is what is genuinely particular to replaying a recording: it
supplies a recorded transport and a synthetic project, it finds or creates the
disabled schedule that owns the cursor for this exact recording (no operator
configured one, and ADR-0089 puts the cursor with the configuration), and it
keeps one advance per run identity so replaying a recording twice is one
outcome rather than two.
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

from corridor.connectors.microsoft365 import GraphLocation, Microsoft365PullConnector, RecordedGraphTransport
from corridor.connectors.pull_connector import SourceEnvelope, sync_pull_connector
from corridor.delivery_registration import (
    MESSAGE_CHANNELS,
    DeliveryTransactions,
    PassLedger,
    SourceDeclaration,
)
from corridor.models import Project, DueWorkSchedule, ConnectorCheckpointAdvance
from corridor.source_delivery import (DeliveryBinding,
    current_checkpoint_token, record_checkpoint_advance)
from corridor.due_work import ConnectorPollingDeclaration, configure_connector_polling
from corridor.project_lock import lock_project

SERVICE_IDENTITY = "corridor.m365-replay"


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

    def declare(envelope: SourceEnvelope) -> SourceDeclaration:
        """The kinds the operator supplied with this recording, and only those.

        A recorded round is replayed by an operator who has the recording in
        front of them, so a library item whose kind nobody stated is refused
        rather than registered with its kind unresolved: the input is
        incomplete, and a build command may say so.  A live poll has nobody to
        ask at delivery time and registers the source anyway (ADR-0007).
        """

        native_id = envelope.metadata["native_id"]
        if envelope.channel in MESSAGE_CHANNELS:
            return SourceDeclaration(
                attachment_source_kinds=(attachment_doc_types or {}).get(native_id))
        kind = (doc_types or {}).get(native_id)
        if not kind or kind == "email":
            raise ValueError("each library item requires an explicit source type mapping")
        return SourceDeclaration(source_kind=kind)

    ledger = PassLedger(DeliveryTransactions.within(session), binding,
                        service_identity=SERVICE_IDENTITY, run_identity=run_identity,
                        declare=declare)

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
        service = SERVICE_IDENTITY + "/" + input_digest
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
            ledger=ledger, on_envelope_stored=ledger.register)
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
            "registered": [row.as_dict() for row in ledger.registered],
            "dispositions": [row.disposition for row in sync.records]}


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
