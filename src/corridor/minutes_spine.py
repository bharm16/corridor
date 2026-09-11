"""Capture the five minutes capabilities as cited Facts and deltas (#456).

The legacy minutes lane emitted per-page Candidates and relied on later legacy
admission. This lane uses the shared reference-only prose boundary and materializer,
compares with the adopted record, and retains unresolved source work explicitly.
One declared source family supplies revision lineage; content never selects it.
"""

from __future__ import annotations

from corridor import digests
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Literal

from pydantic import Field
from sqlalchemy import func, select

from corridor.extraction_runs import record_extraction_run
from corridor.extractor_lineage import injected_extractor_config, token_usage_delta, usage_snapshot, zero_token_usage
from corridor.materializer import materialize_prose_wording, materialize_prose_scope, materialize_typed_satellite
from corridor.models import (Document, Fact, FactSource, MinutesCapture, Project, ProjectRecordRevision, SourceDelivery, SourceSegment)
from corridor.operating_mode import is_adopted_baseline
from corridor.prompt_library import installed_prompt
from corridor.prose_interpretation import read_typed_prose
from corridor.prose_spans import prose_segment_filter
from corridor.project_lock import lock_project
from corridor.proposed_delta_comparison import StatedSubject, compare_stated_subjects
from corridor.proposed_deltas import create_proposed_delta_group
from corridor.reader_segments import replay_native_segments
from corridor.record_projection import read_project_record_as_of_revision, record_value_payload
from corridor.source_append import (ClosureValues, ScopeSubjectValues, TimingValues, append_fact, append_minutes_capture)
from corridor.statement_timing_parser import statement_timing_options, is_required_timing
from corridor.statement_values import reports_completion, states_unknown_scope
from corridor.storage import stored_file
from corridor.subject_resolution import registered_subject_candidates
from corridor.typed_output import StrictOutputModel, strict_output_schema


PROMPT_VERSION = "minutes_spine_v1"
PROMPT = installed_prompt(PROMPT_VERSION)
SCHEMA_VERSION = "minutes-five-fields-v1"
MAX_SEGMENTS = 500
MAX_CHARACTERS = 120_000


class MinutesCaptureRefused(ValueError):
    """The source reading cannot prove its bound inputs or supported references."""


class ScopeReference(StrictOutputModel):
    subject_ref: int = Field(gt=0)
    segment_id: int = Field(gt=0)


class MinutesStatement(StrictOutputModel):
    kind: Literal["commitment", "timing_change", "completion_report", "unresolved"]
    wording_segment_id: int = Field(gt=0)
    attribution_segment_id: int | None
    organization_id: int | None
    person_id: int | None
    timing_ref: int | None
    predecessor_ref: int | None
    scope: tuple[ScopeReference, ...]


class MinutesOutput(StrictOutputModel):
    read_segment_ids: tuple[int, ...]
    statements: tuple[MinutesStatement, ...]


@dataclass(frozen=True)
class MinutesSpan:
    segment: SourceSegment
    role: str
    block: int
    attribution_id: int | None


def _normalize(text):
    return " ".join(text.split()).casefold()


def _contains(text, value):
    return re.search(r"(?<![\w-])" + re.escape(value) + r"(?![\w-])", text, re.I)


def _spans(segments):
    result = []
    block, attribution, page = 0, None, None
    for segment in segments:
        text = segment.exact_text.strip()
        if segment.page_no != page:
            block, attribution, page = block + 1, None, segment.page_no
        if re.match(r"^(?:DRAFT(?: MINUTES)?|DRAFT MEETING NOTES)\s*$", text, re.I):
            role = "draft"
        elif text.startswith(">") or re.match(r"^(?:Quoted history|Previous minutes)\s*:", text, re.I):
            role = "quoted_history"
        elif re.match(r"^(?:Meeting(?: date| title)?|Date|Attendees|Location)\s*:", text, re.I):
            role = "meeting_metadata"
        elif re.match(r"^(?:Agenda|Action Items|Item \d+)\b", text, re.I):
            role, block, attribution = "agenda", block + 1, None
        elif re.fullmatch(r"\d+\.", text):
            role, block, attribution = "agenda", block + 1, None
        elif re.match(r"^[^:\n]{1,100}:\s*$", text):
            role, attribution = "speaker_label", segment.id
            block += 1
        else:
            role = "action_item" if re.match(r"^[^:\n]{1,100}:\s*\S", text) else "body"
            if role == "action_item":
                attribution = segment.id
        result.append(MinutesSpan(segment, role, block, attribution))
    return tuple(result)


def minutes_extractor_config(client):
    from corridor import materializer, statement_timing_parser, statement_values

    return injected_extractor_config(extractor="minutes_spine", prompt_version=PROMPT_VERSION,
        model=getattr(client, "model", None), schema_version=SCHEMA_VERSION,
        prompt_bytes=PROMPT.data, schema=strict_output_schema(MinutesOutput),
        postprocessor_bytes=Path(__file__).read_bytes() + Path(materializer.__file__).read_bytes()
            + Path(statement_timing_parser.__file__).read_bytes() + Path(statement_values.__file__).read_bytes(),
        request_controls={"api": "structured_client", "strict": True, "store": False,
                          "provider_base_url": getattr(client, "base_url", None), "reasoning_effort": getattr(client, "effort", None)})


def _prepare(session, document):
    project = session.get_one(Project, document.project_id)
    if document.doc_type != "minutes" or not is_adopted_baseline(session, project.id):
        raise MinutesCaptureRefused("minutes capture requires a registered source in an adopted project")
    if document.parse_status != "parsed":
        raise MinutesCaptureRefused("minutes source did not parse successfully")
    path = getattr(document, "_stored_path", None) or stored_file(document)
    if path is None or sha256(Path(path).read_bytes()).hexdigest() != document.sha256:
        raise MinutesCaptureRefused("minutes source bytes do not match their registered rendition")
    segments = session.scalars(select(SourceSegment).where(
        SourceSegment.document_id == document.id, SourceSegment.project_id == project.id,
        prose_segment_filter(SourceSegment)).order_by(SourceSegment.ordinal)).all()
    if not segments or len(segments) > MAX_SEGMENTS or sum(len(s.exact_text) for s in segments) > MAX_CHARACTERS:
        raise MinutesCaptureRefused("minutes source exceeds or lacks its complete prose budget")
    replay_native_segments(document, segments, path)
    spans = _spans(segments)
    revision = session.scalar(select(func.max(ProjectRecordRevision.id)).where(ProjectRecordRevision.project_id == project.id))
    accepted = {(value.subject_key, value.fact_type): value for value in read_project_record_as_of_revision(session, project.id, revision)}
    subjects = registered_subject_candidates(session, project.id)
    organizations = {subject.subject_id: subject for subject in subjects if subject.subject_type == "external_org"}
    people = {subject.subject_id: subject for subject in subjects if subject.subject_type == "person"}
    timings = {index: (segment, option.value, is_required_timing(segment.exact_text, option)) for index, (segment, option) in enumerate(
        ((span.segment, option) for span in spans for option in statement_timing_options(span.segment.exact_text)), start=1)}
    predecessors = {}
    for (key, field), value in accepted.items():
        if field != "statement_wording":
            continue
        evidence = session.scalars(select(SourceSegment).join(FactSource, FactSource.source_segment_id == SourceSegment.id)
            .where(FactSource.fact_id == value.fact_id, FactSource.role == "attribution_source")).all()
        matching = [org_id for org_id, org in organizations.items()
                    if any(_contains(s.exact_text, alias) for s in evidence for alias in (org.display_name, *org.aliases))]
        if len(matching) == 1:
            scope = accepted.get((key, "applies_to"))
            predecessors[key] = {"ref": value.fact_id, "organization_id": matching[0], "wording": value.text_value,
                                 "scope": list(scope.applies_to_subject_keys) if scope else []}
    context = {"document_id": document.id, "accepted_revision_id": revision,
        "segments": [{"id": span.segment.id, "role": span.role, "text": span.segment.exact_text,
                      "attribution_segment_id": span.attribution_id, "block": span.block} for span in spans],
        "organizations": [{"id": key, "name": value.display_name, "aliases": list(value.aliases)} for key, value in organizations.items()],
        "people": [{"id": key, "name": value.display_name, "aliases": list(value.aliases)} for key, value in people.items()],
        "project_side_parties": project.project_side_parties,
        "timings": [{"ref": ref, "segment_id": segment.id, "purpose": "required_by" if required else "stated", **value}
                    for ref, (segment, value, required) in timings.items()],
        "subjects": [{"ref": value.fact_id, "subject_key": key, "reference": value.text_value} for (key, field), value in accepted.items() if field == "utility_id"],
        "predecessors": [{"subject_key": key, **value} for key, value in predecessors.items()]}
    return project, spans, accepted, organizations, people, timings, predecessors, context


def inspect_minutes(session, document):
    """The exact bounded catalog a configured reader receives."""
    return _prepare(session, document)[-1]


def capture_minutes(session, document, *, client, source_family: str | None = None, source_revision: str | None = None):
    """Append one source revision's five-field reading; retry returns its receipt."""
    delivery = session.get(SourceDelivery, document.source_delivery_id) if document.source_delivery_id else None
    if delivery and (delivery.project_id != document.project_id or delivery.content_sha256 != document.sha256):
        raise MinutesCaptureRefused("minutes delivery is outside its registered source")
    family = source_family or (delivery.external_identity if delivery else None) or document.registry_id or f"document:{document.sha256}"
    version = source_revision or (delivery.external_version if delivery else None) or document.sha256
    digest = _digest({"document": document.sha256, "family": family, "version": version, "reader": PROMPT_VERSION})
    with session.begin_nested():
        lock_project(session, document.project_id)
        existing = session.scalar(select(MinutesCapture).where(MinutesCapture.project_id == document.project_id,
            MinutesCapture.source_family == family, MinutesCapture.source_revision == version))
        if existing:
            if existing.input_sha256 != digest:
                raise MinutesCaptureRefused("minutes source revision is already bound to different inputs")
            return existing
        project, spans, accepted, organizations, people, timings, predecessors, context = _prepare(session, document)
        span_by_id = {span.segment.id: span for span in spans}
        draft = any(span.role == "draft" for span in spans)
        before = usage_snapshot(client)
        output = (MinutesOutput(read_segment_ids=tuple(span_by_id), statements=()) if draft else
            read_typed_prose(client, system=PROMPT.text, user=json.dumps(context), output_type=MinutesOutput))
        if len(output.read_segment_ids) != len(set(output.read_segment_ids)) or set(output.read_segment_ids) != set(span_by_id):
            raise MinutesCaptureRefused("minutes reader must account for every bounded source segment")
        identifiers = [statement.wording_segment_id for statement in output.statements]
        if len(set(identifiers)) != len(identifiers):
            raise MinutesCaptureRefused("one source statement cannot produce duplicate outcomes")
        if session.scalar(select(func.max(ProjectRecordRevision.id)).where(ProjectRecordRevision.project_id == project.id)) != context["accepted_revision_id"]:
            raise MinutesCaptureRefused("accepted revision moved during minutes reading")
        run = record_extraction_run(session, document, prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION, model=getattr(client, "model", None),
            extractor_config=minutes_extractor_config(client), candidate_count=0, page_errors=0,
            token_usage=zero_token_usage(document.id) if draft else token_usage_delta(before, usage_snapshot(client), document_ids=[document.id]))
        outcomes = []
        for statement in output.statements:
            outcome = _capture_statement(session, document, run, statement, span_by_id, accepted, organizations,
                people, timings, predecessors, project, family, version, context["accepted_revision_id"])
            outcomes.append(outcome)
        for span in spans:
            if span.segment.id not in identifiers:
                outcomes.append({"segment_id": span.segment.id, "status": "excluded" if draft or span.role not in {"body", "action_item"} else "unresolved",
                    "reasons": ["draft_source" if draft else span.role if span.role not in {"body", "action_item"} else "unproposed_source_statement"],
                    "fact_ids": [], "delta_ids": [], "scope_state": "unknown"})
        return append_minutes_capture(session, project_id=project.id, document_id=document.id,
            extraction_run_id=run.id, source_family=family, source_revision=version, input_sha256=digest,
            accepted_revision_id=context["accepted_revision_id"],
            output={"schema_version": SCHEMA_VERSION, "read_segment_ids": list(output.read_segment_ids), "outcomes": outcomes})


def _capture_statement(session, document, run, statement, spans, accepted, organizations, people,
                       timings, predecessors, project, family, version, revision):
    span = spans.get(statement.wording_segment_id)
    if span is None:
        raise MinutesCaptureRefused("minutes statement names a foreign source segment")
    predecessor_key = next((key for key, value in predecessors.items() if value["ref"] == statement.predecessor_ref), None)
    outcome = {"segment_id": span.segment.id, "kind": statement.kind, "status": "unresolved",
               "reasons": [], "fact_ids": [], "delta_ids": [], "scope_state": "unknown",
               "predecessor_subject_key": predecessor_key}
    reasons = outcome["reasons"]
    if span.role in {"draft", "quoted_history", "meeting_metadata", "agenda", "speaker_label"}:
        reasons.append(span.role)
        return outcome
    if statement.kind == "unresolved":
        reasons.append("source_statement_unresolved")
        return outcome
    attribution = spans.get(statement.attribution_segment_id)
    if attribution is None or statement.attribution_segment_id != span.attribution_id:
        reasons.append("attribution_unresolved")
        return outcome
    label = attribution.segment.exact_text.split(":", 1)[0].strip()
    if any(_contains(label, name) for name in project.project_side_parties):
        outcome["status"] = "excluded"
        reasons.append("project_side_speaker")
        return outcome
    orgs = [key for key, value in organizations.items() if any(_contains(label, alias) for alias in (value.display_name, *value.aliases))]
    if len(orgs) != 1 or statement.organization_id != orgs[0]:
        reasons.append("attribution_unresolved")
        return outcome
    matched_people = [key for key, person in people.items()
                      if any(_contains(label, alias) for alias in (person.display_name, *person.aliases))]
    if len(matched_people) > 1 or (statement.person_id is not None and matched_people != [statement.person_id]):
        reasons.append("person_attribution_unresolved")
        return outcome
    scope_members = []
    for reference in statement.scope:
        subject_key, target = next(((key, value) for (key, field), value in accepted.items()
                                   if field == "utility_id" and value.fact_id == reference.subject_ref), (None, None))
        source = spans.get(reference.segment_id)
        if target is None or source is None:
            raise MinutesCaptureRefused("minutes scope names a foreign subject or segment")
        match = _contains(source.segment.exact_text, target.text_value)
        same_reference = [value for (key, field), value in accepted.items()
                          if field == "utility_id" and value.text_value == target.text_value]
        if (not match or len(same_reference) != 1 or source.block != span.block
                or (source.segment.id != span.segment.id and source.role != "agenda")):
            reasons.append("scope_unresolved")
            return outcome
        scope_members.append(ScopeSubjectValues(subject_key, source.segment.id, match.group(0)))
    if len({member.subject_key for member in scope_members}) != len(scope_members):
        raise MinutesCaptureRefused("minutes scope repeats a subject")
    if statement.kind in {"timing_change", "completion_report"}:
        eligible = [key for key, value in predecessors.items() if value["organization_id"] == statement.organization_id
                    and (not scope_members or not value["scope"] or set(value["scope"]) & {member.subject_key for member in scope_members})]
        if len(eligible) != 1 or predecessor_key != eligible[0]:
            reasons.append("predecessor_unresolved")
            return outcome
        subject = eligible[0]
    else:
        if statement.predecessor_ref is not None:
            reasons.append("new_commitment_has_predecessor")
            return outcome
        subject = f"minutes:{_digest(family)[:24]}:{_digest((span.segment.exact_text, label))}"
    timing = None
    if statement.timing_ref is not None:
        timing_source = timings.get(statement.timing_ref)
        if timing_source is None or timing_source[0].id != span.segment.id:
            raise MinutesCaptureRefused("minutes timing must replay from its statement")
        if timing_source[2]:
            reasons.append("required_by_is_not_promised_timing")
            return outcome
        timing = timing_source[1]
        if statement.kind == "timing_change" and re.search(r"\b(?:changed?|moved?|revised?|rescheduled?|shifted?|extended?)\b", span.segment.exact_text, re.I):
            options = [value for source, value, required in timings.values() if source.id == span.segment.id and not required]
            if len(options) > 1 and timing != options[-1]:
                reasons.append("new_timing_unresolved")
                return outcome
    if statement.kind == "timing_change" and timing is None:
        reasons.append("new_timing_unresolved")
        return outcome
    if statement.kind == "completion_report" and not reports_completion(span.segment.exact_text):
        reasons.append("completion_not_explicit")
        return outcome
    facts = []
    word = materialize_prose_wording(span.segment, attribution.segment, attribution=label)
    facts.append((_append(session, document, run, subject, word), span.segment.exact_text))
    if timing:
        value = materialize_typed_satellite("statement_timing", span.segment)
        members = (TimingValues("new", timing["text"], timing["precision"],
            date.fromisoformat(timing["start_date"]) if timing["start_date"] else None,
            date.fromisoformat(timing["end_date"]) if timing["end_date"] else None),)
        canonical = {"timings": [{"role": "new", **timing}]}
        facts.append((_append(session, document, run, subject, value, canonical=canonical, timings=members), canonical))
    if scope_members or states_unknown_scope(span.segment.exact_text):
        source_ids = list(dict.fromkeys(member.source_segment_id for member in scope_members)) or [span.segment.id]
        value = materialize_prose_scope(tuple(spans[source_id].segment for source_id in source_ids))
        canonical = {"mode": "selected" if scope_members else "unknown", "subject_keys": sorted(member.subject_key for member in scope_members)}
        facts.append((_append(session, document, run, subject, value, canonical=canonical,
                              applies_to_subjects=tuple(sorted(scope_members, key=lambda member: member.subject_key))), canonical))
    if statement.kind == "completion_report":
        value = materialize_typed_satellite("closure_result", span.segment)
        canonical = {"closure_kind": "completion_reported"}
        facts.append((_append(session, document, run, subject, value, canonical=canonical,
            closure=ClosureValues("completion_reported", None, (span.segment.id,))), canonical))
    # One shared comparison (`proposed_delta_comparison`): this statement's
    # canonical values against the accepted record, read through the same
    # projection payload the record itself renders.
    stated_types = {fact.fact_type for fact, _ in facts}
    comparison = compare_stated_subjects(
        accepted={key: record_value_payload(value) for key, value in accepted.items()
                  if key[0] == subject and key[1] in stated_types},
        stated=(StatedSubject(subject_identity=subject,
            values=tuple((fact.fact_type, value) for fact, value in facts),
            paired=any(key == subject for key, _ in accepted)),),
        comparison_rule_version=PROMPT_VERSION,
        # Retained label: this lane names its baseline without the absent-revision
        # spelling `revision_label` gives, and a stored delta holds it.
        accepted_baseline_revision=f"revision:{revision}",
    )
    deltas = comparison.deltas
    created = create_proposed_delta_group(session, project_id=project.id,
        source_family=sha256(f"minutes:{family}".encode()).hexdigest(), source_revision=_digest((version, document.sha256)),
        document_id=document.id, deltas=deltas)
    outcome.update(status="captured", subject_key=subject, organization_id=statement.organization_id,
        person_id=statement.person_id, fact_ids=[fact.id for fact, _ in facts], delta_ids=[delta.id for delta in created],
        scope_state="selected" if scope_members else "unknown",
        scope_subject_keys=[member.subject_key for member in scope_members])
    if not scope_members and not states_unknown_scope(span.segment.exact_text):
        prior_scope = accepted.get((subject, "applies_to"))
        if prior_scope and prior_scope.applies_to_subject_keys:
            outcome["scope_state"] = "accepted_context"
    return outcome


def _append(session, document, run, subject, value, *, canonical=None, **satellites):
    digest = _digest({"document": document.sha256, "project": document.project_id, "subject": subject,
        "type": value.fact_type, "sources": value.source_links, "text": value.text_value,
        "transformation": value.transformation, "typed_value": canonical})
    return append_fact(session, project_id=document.project_id, document_id=document.id, extraction_run_id=run.id,
        subject_kind="statement_candidate", subject_key=subject, recorded_by=PROMPT_VERSION,
        content_sha256=digest, value=value, **satellites)


# Retained encoding: a stored delivery/identity key computed with non-ASCII
# escaped; see `corridor.digests.ascii_escaped_json`.
_digest = digests.ascii_escaped_sha256
