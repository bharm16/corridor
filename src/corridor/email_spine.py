"""Capture one project-bound email thread through the spine (#455).

The predecessor reader wrote one legacy Candidate per closing message; it had
no Source Facts and left previous conclusions actionable after reversals.
This lane takes #511's retained envelope, gives a bounded provider references
only, materializes one current authored statement or a source-cited question,
and appends a delta against the accepted record. No accepted authority is written.
All provider behavior is injectable; fixtures and deployment use the same seam.
"""

from __future__ import annotations

from hashlib import sha256
from email.utils import getaddresses
import json
from pathlib import Path
from typing import Literal

from pydantic import Field
from sqlalchemy import func, select

from corridor.connectors.pull_connector import SourceEnvelope
from corridor.analytics import AnalyticsEvent, EventFamily, emit_event
from corridor.delta_generation import accepted_values, revision_label
from corridor.email_segments import append_email_segments
from corridor.extraction_runs import record_extraction_run
from corridor.extractor_lineage import injected_extractor_config, token_usage_delta, usage_snapshot, zero_token_usage
from corridor.materializer import materialize_email_metadata, materialize_prose_wording
from corridor.measurement_collection import binding_for_source
from corridor.models import (
    DeltaSupersession, Document, ExtractionRun, Fact, InboundMessage, InboundThread, InboundThreadReading,
    Project, ProjectRecordRevision, SourceDelivery,
)
from corridor.proposed_deltas import (
    ExistingSubjectTarget, ProposedDeltaValues, ProposedSubjectTarget, create_proposed_delta_group,
)
from corridor.source_append import append_email_thread_reading, append_fact
from corridor.storage import stored_file
from corridor.typed_output import StrictOutputModel, strict_output_schema, validate_typed_output


PROMPT_VERSION = "email_thread_v1"
SCHEMA_VERSION = "email-thread-output-v1"
PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts/email_thread_v1.md"
MAX_TURNS = 20
MAX_THREAD_CHARACTERS = 120_000


class EmailCaptureRefused(ValueError):
    """The bound reading cannot prove its inputs or its proposed outcome."""


class EmailThreadOutput(StrictOutputModel):
    """One outcome selected by reference; no provider-supplied values or authority."""
    resolution: Literal["concluded", "unresolved"]
    segment_id: int = Field(gt=0)
    read_segment_ids: tuple[int, ...]


def email_extractor_config(client):
    """Seal the same reader contract for route selection and completed attempts."""
    from corridor import email_segments, materializer

    return injected_extractor_config(
        extractor="email_thread", prompt_version=PROMPT_VERSION,
        model=getattr(client, "model", None), schema_version=SCHEMA_VERSION,
        prompt_bytes=PROMPT_PATH.read_bytes(), schema=strict_output_schema(EmailThreadOutput),
        postprocessor_bytes=(Path(__file__).read_bytes() + Path(email_segments.__file__).read_bytes()
                             + Path(materializer.__file__).read_bytes()),
        request_controls={"api": "structured_client", "strict": True, "store": False,
                          "provider_base_url": getattr(client, "base_url", None),
                          "reasoning_effort": getattr(client, "effort", None)},
    )


def envelope_for_delivery(session, delivery_id: int) -> SourceEnvelope:
    """Reconstruct a stored envelope for the operator/worker command."""
    row = session.get_one(SourceDelivery, delivery_id)
    project = session.get_one(Project, row.project_id)
    return SourceEnvelope(
        customer=row.customer, project=project.slug, channel=row.channel,
        external_identity=row.external_identity, external_version=row.external_version,
        original_timestamps=dict(row.original_timestamps_json or {}),
        content_digest=row.content_sha256, bytes_reference=row.bytes_reference,
        metadata=dict(row.metadata_json or {}), delivery_identity=row.delivery_identity,
        idempotency_key=row.idempotency_key,
    )


def _bound_thread(session, envelope):
    delivery = session.scalar(select(SourceDelivery).where(
        SourceDelivery.idempotency_key == envelope.idempotency_key,
        SourceDelivery.disposition == "stored",
    ))
    if delivery is None or envelope_for_delivery(session, delivery.id) != envelope:
        raise EmailCaptureRefused("email requires the exact stored customer/project envelope")
    inbound = session.scalar(select(InboundMessage).where(
        InboundMessage.project_id == delivery.project_id,
        InboundMessage.raw_sha256 == envelope.content_digest,
        InboundMessage.push_delivery_id.is_not(None),
    ))
    if inbound is None:
        raise EmailCaptureRefused("envelope has no project-bound registered MIME message")
    thread = session.scalar(select(InboundThread).where(
        InboundThread.id == inbound.thread_id,
        InboundThread.project_id == delivery.project_id,
    ).with_for_update())
    if thread is None:
        raise EmailCaptureRefused("message thread is outside its bound project")
    turns = session.scalars(select(InboundMessage).where(
        InboundMessage.thread_id == thread.id,
    ).order_by(InboundMessage.id)).all()
    if len(turns) > MAX_TURNS or any(turn.project_id != thread.project_id for turn in turns):
        raise EmailCaptureRefused("thread exceeds its bound turn count or project")
    return thread, turns


def _source_turns(session, turns):
    result, by_id = [], {}
    characters = 0
    for turn in turns:
        document = session.get(Document, turn.document_id)
        path = stored_file(document)
        if document is None or path is None:
            raise EmailCaptureRefused("thread turn has no retained Document bytes")
        if document.project_id != turn.project_id or document.sha256 != turn.raw_sha256:
            raise EmailCaptureRefused("thread turn rendition crosses its source")
        segments = append_email_segments(session, document, path.read_bytes())
        by_id.update({segment.id: segment for segment in segments})
        characters += sum(len(segment.exact_text) for segment in segments)
        if characters > MAX_THREAD_CHARACTERS:
            raise EmailCaptureRefused("thread exceeds the complete-context character budget")
        result.append({
            "message_id": turn.id, "raw_sha256": turn.raw_sha256,
            "sender": turn.sender, "segments": [
                {"id": segment.id, "section": segment.location_json["section"],
                 "header_name": segment.location_json["header_name"], "text": segment.exact_text}
                for segment in segments
            ],
        })
    return result, by_id


def inspect_email_thread(session, envelope: SourceEnvelope) -> dict:
    """Expose bounded retained sources and stable references for a configured reader."""
    thread, turns = _bound_thread(session, envelope)
    source_turns, _ = _source_turns(session, turns)
    return {"thread_id": thread.id, "turns": source_turns}


def current_email_thread_reading(session, *, project_id: int, thread_id: int):
    """The latest source-cited outcome, including an unresolved closing question."""
    return session.scalar(select(InboundThreadReading).join(InboundThread).where(
        InboundThread.id == thread_id, InboundThread.project_id == project_id,
        InboundThreadReading.input_sha256.is_not(None),
    ).order_by(InboundThreadReading.closing_message_id.desc()).limit(1))


def capture_email_thread(session, envelope: SourceEnvelope, *, client):
    """Capture the complete current thread once; a failed attempt rolls back as a unit."""
    with session.begin_nested():
        thread, turns = _bound_thread(session, envelope)
        closing = turns[-1]
        identity = [{"message_id": turn.id, "raw_sha256": turn.raw_sha256} for turn in turns]
        input_digest = _digest({"turns": identity, "reader": PROMPT_VERSION})
        existing = session.scalar(select(InboundThreadReading).where(
            InboundThreadReading.thread_id == thread.id,
            InboundThreadReading.closing_message_id == closing.id,
        ))
        if existing is not None:
            if existing.input_sha256 != input_digest:
                raise EmailCaptureRefused("existing closing-turn reading has different inputs")
            return existing
        source_turns, by_id = _source_turns(session, turns)
        # The record lock serializes comparison with Resolve Delta, which owns
        # the same project lock. Provider latency cannot race a changed baseline.
        session.scalar(select(Project).where(Project.id == thread.project_id).with_for_update())
        accepted = accepted_values(session, thread.project_id)
        revision = session.scalar(select(func.max(ProjectRecordRevision.id)).where(
            ProjectRecordRevision.project_id == thread.project_id))
        schema = strict_output_schema(EmailThreadOutput)
        system = PROMPT_PATH.read_text()
        before = usage_snapshot(client)
        raw = client.complete(system=system, user=json.dumps({"turns": source_turns}), schema=schema)
        output = validate_typed_output(EmailThreadOutput, raw)
        selected = by_id.get(output.segment_id)
        if selected is None or selected.document_id != closing.document_id or not selected.exact_text.strip():
            raise EmailCaptureRefused("outcome must cite an exact segment of the closing turn")
        read_ids = set(output.read_segment_ids)
        if len(read_ids) != len(output.read_segment_ids) or not read_ids <= by_id.keys():
            raise EmailCaptureRefused("reader returned duplicate or foreign segment references")
        required = {s.id for s in by_id.values() if s.location_json["section"] == "body" and s.exact_text.strip()}
        if selected.id not in read_ids or not required <= read_ids:
            raise EmailCaptureRefused("reader omitted authored turns from its outcome")
        document = session.get_one(Document, closing.document_id)
        usage = token_usage_delta(before, usage_snapshot(client), document_ids=[document.id])
        config = email_extractor_config(client)
        if session.scalar(select(func.max(ProjectRecordRevision.id)).where(
                ProjectRecordRevision.project_id == thread.project_id)) != revision:
            raise EmailCaptureRefused("accepted revision moved while reading email")
        runs = {}
        for turn in turns:
            doc = session.get_one(Document, turn.document_id)
            prior = session.scalar(select(Fact).where(Fact.document_id == doc.id,
                Fact.fact_type == "email_header").order_by(Fact.id).limit(1))
            if doc.id != document.id and prior is not None:
                run = session.get_one(ExtractionRun, prior.extraction_run_id)
            else:
                run = record_extraction_run(session, doc, prompt_version=PROMPT_VERSION,
                    schema_version=SCHEMA_VERSION, candidate_count=0, page_errors=0,
                    model=getattr(client, "model", None), extractor_config=config,
                    token_usage=usage if doc.id == document.id else zero_token_usage(doc.id))
            runs[doc.id] = run
            if any(s.document_id == doc.id and (
                    s.location_json["section"] == "draft" or (
                        s.location_json["section"] == "header" and s.location_json["part_path"] == []
                        and s.location_json["header_name"] == "x-unsent" and s.exact_text.strip() == "1"))
                    for s in by_id.values()):
                continue
            for segment in by_id.values():
                if segment.document_id == doc.id and segment.location_json["section"] in {"header", "attachment"}:
                    _append_value(session, doc, run, f"email-message:{turn.id}", materialize_email_metadata(segment))
        fact, delta = None, None
        subject = f"email-thread:{thread.id}"
        if output.resolution == "concluded":
            senders = [s for s in by_id.values() if s.document_id == document.id
                and s.location_json["section"] == "header" and s.location_json["part_path"] == []
                and s.location_json["header_name"] == "from"]
            if len(senders) != 1 or not closing.sender:
                raise EmailCaptureRefused("concluded thread requires one retained sender")
            addresses = getaddresses([senders[0].exact_text])
            if len(addresses) != 1 or not addresses[0][1]:
                raise EmailCaptureRefused("concluded thread requires unambiguous sender attribution")
            value = materialize_prose_wording(selected, senders[0], attribution=senders[0].exact_text)
            fact = _append_value(session, document, runs[document.id], subject, value)
            key = (subject, "statement_wording")
            if accepted.get(key) != fact.text_value:
                known = any(item[0] == subject for item in accepted)
                (delta,) = create_proposed_delta_group(session, project_id=thread.project_id,
                    source_family=subject, source_revision=input_digest, document_id=document.id,
                    deltas=(ProposedDeltaValues(
                        change_type="modify" if key in accepted else "add",
                        target=ExistingSubjectTarget(subject, "statement_wording") if known
                            else ProposedSubjectTarget(subject, ("statement_wording",)),
                        accepted_value=accepted.get(key),
                        proposed_value=fact.text_value if known else {"statement_wording": fact.text_value},
                        comparison_rule_version=PROMPT_VERSION,
                        accepted_baseline_revision=revision_label(revision),
                    ),))
        context = [{**item, "extraction_run_id": runs[turn.document_id].id,
                    "read_segment_ids": [s["id"] for s in item["segments"] if s["id"] in read_ids],
                    "unread_segment_ids": [s["id"] for s in item["segments"] if s["id"] not in read_ids]}
                   for item, turn in zip(source_turns, turns, strict=True)]
        # Text lives only in Source Segments; the retained context carries references.
        for item in context:
            item["segment_ids"] = [s["id"] for s in item.pop("segments")]
        reading = append_email_thread_reading(session,
            project_id=thread.project_id, thread_id=thread.id, closing_message_id=closing.id,
            input_sha256=input_digest, source_fact_id=fact.id if fact else None,
            proposed_delta_id=delta.id if delta else None,
            question_segment_id=None if fact else selected.id,
            context=context, prompt_version=PROMPT_VERSION, model=getattr(client, "model", None))
        delivery = session.get_one(SourceDelivery, closing.push_delivery_id)
        for supersession in session.scalars(select(DeltaSupersession).where(
                DeltaSupersession.source_reading_id == reading.id)):
            emit_event(AnalyticsEvent(family=EventFamily.DELTA_SUPERSESSION,
                binding=binding_for_source(session, delivery),
                payload={"project_id": thread.project_id,
                         "prior_delta_id": supersession.prior_delta_id,
                         "superseding_delta_id": supersession.superseding_delta_id,
                         "source_reading_id": reading.id, "source_revision": input_digest,
                         "comparison_rule_version": PROMPT_VERSION}))
        return reading


def _append_value(session, document, run, subject, value):
    identity = {"project": document.project_id, "document": document.sha256,
                "subject": subject, "type": value.fact_type, "text": value.text_value,
                "sources": value.source_links, "transformation": value.transformation}
    return append_fact(session, project_id=document.project_id, document_id=document.id,
        extraction_run_id=run.id, subject_kind="statement_candidate" if value.fact_type == "statement_wording" else "email_message",
        subject_key=subject, recorded_by=PROMPT_VERSION, content_sha256=_digest(identity), value=value)


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
