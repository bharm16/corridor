"""Interpret untrusted prose into pending typed Facts under one bounded seam.

Earlier prose extractors each accepted generic provider dictionaries, repeated
partial validation, and constructed mutable proposals directly.  This runtime
instead gives the model opaque segment/subject references, validates one strict
typed output locally, measures omissions, and delegates its only persistence to
``append_source_facts``.  It cannot include anything in the Project Record;
interpretation-bearing Fact contracts remain human-gated (ADR-0068..0070).
The output schema carries references only — segment ids, roles, subject ids —
and no value field at all (#446): the wording a proposal asserts is whatever
its cited value-source segment materializes, so a model literal has nowhere to
land.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Literal

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor import facts as facts_module
from corridor import typed_output as typed_output_module
from corridor.extractor_lineage import (
    injected_extractor_config,
    token_usage_delta,
    usage_snapshot,
)
from corridor.extraction_runs import SourceFactAppendResult, append_source_facts
from corridor.facts import (
    FACT_TYPE_CONTRACTS,
    replay_proposed_fact_value,
)
from corridor.llm import StructuredClient
from corridor.models import Candidate, Dependency, Document, ExternalOrg, SourceSegment
from corridor.typed_output import (
    StrictOutputModel,
    TypedOutputValidationError,
    strict_output_schema,
    validate_typed_output,
)
from corridor.prompt_library import installed_prompt
from corridor.prose_spans import prose_segment_filter


PROMPT_VERSION = "prose_interpretation_v1"
PROMPT = installed_prompt(PROMPT_VERSION)
SCHEMA_VERSION = "prose_interpretation_output_v2"


class SegmentReference(StrictOutputModel):
    """One opaque Source Segment id carrying one support role."""

    segment_id: int = Field(gt=0)
    role: Literal["value_source", "attribution_source", "context"]


class SubjectReference(StrictOutputModel):
    """One opaque registered subject candidate returned without name matching."""

    subject_type: Literal["external_org"]
    subject_id: int = Field(gt=0)


class ProseFactProposal(StrictOutputModel):
    """One reference-only, human-gated Fact proposal from prose."""

    fact_type: Literal["statement_wording"]
    sources: tuple[SegmentReference, ...] = Field(min_length=2)
    subject_candidates: tuple[SubjectReference, ...] = Field(min_length=1)


class ProseInterpretationOutput(StrictOutputModel):
    """The complete strict provider output before contextual validation."""

    read_segment_ids: tuple[int, ...]
    proposals: tuple[ProseFactProposal, ...]


class ProseCompleteness(StrictOutputModel):
    """Per-document omission measurement persisted on the Extraction Run."""

    schema_version: Literal["prose-segment-accounting-v1"] = (
        "prose-segment-accounting-v1"
    )
    reader_version: Literal["prose_interpretation_v1"] = PROMPT_VERSION
    reader_path: Literal["prose_interpretation"] = "prose_interpretation"
    document_id: int = Field(gt=0)
    detected_segment_count: int = Field(ge=0)
    read_segment_count: int = Field(ge=0)
    proposed_fact_count: int = Field(ge=0)
    unread_segment_ids: tuple[int, ...]
    proposed_subject_candidate_ids: tuple[int, ...]
    unproposed_subject_candidate_ids: tuple[int, ...]


@dataclass(frozen=True)
class RegisteredSubjectCandidate:
    """Current adapter shape; #453 supplies the stable registry service."""

    subject_type: str
    subject_id: int
    display_name: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class ProseInterpretationResult:
    """Typed provider result, completeness receipt, and scoped append outcome."""

    output: ProseInterpretationOutput
    completeness: ProseCompleteness
    append: SourceFactAppendResult


SubjectProvider = Callable[
    [Session, int], tuple[RegisteredSubjectCandidate, ...]
]


def read_typed_prose(client, *, system: str, user: str, output_type, factual_checks=()):
    """One shared reference-only provider/validation boundary for prose lanes."""
    raw = client.complete(system=system, user=user, schema=strict_output_schema(output_type))
    return validate_typed_output(output_type, raw, factual_checks=factual_checks)


def interpret_prose_document(
    session: Session,
    document: Document,
    *,
    client: StructuredClient,
    source_path: str | Path,
    idempotency_key: str,
    subject_provider: SubjectProvider | None = None,
) -> ProseInterpretationResult:
    """Validate one bounded model result, then append through the spine command."""

    segments = tuple(
        session.scalars(
            select(SourceSegment)
            .where(
                SourceSegment.document_id == document.id,
                prose_segment_filter(SourceSegment),
            )
            .order_by(SourceSegment.ordinal)
        ).all()
    )
    if not segments:
        raise ValueError("prose interpretation requires exact prose segments")
    subjects = (subject_provider or registered_subject_candidates)(
        session, document.project_id
    )
    schema = strict_output_schema(ProseInterpretationOutput)
    system = PROMPT.text
    user = json.dumps(
        {
            "document_id": document.id,
            "segments": [
                {
                    "segment_id": segment.id,
                    "kind": segment.kind,
                    "page_no": segment.page_no,
                    "text": segment.exact_text,
                }
                for segment in segments
            ],
            "subject_candidates": [
                {
                    "subject_type": subject.subject_type,
                    "subject_id": subject.subject_id,
                    "display_name": subject.display_name,
                    "aliases": list(subject.aliases),
                }
                for subject in subjects
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    usage_before = usage_snapshot(client)
    segment_by_id = {segment.id: segment for segment in segments}
    subject_by_identity = {
        (subject.subject_type, subject.subject_id): subject for subject in subjects
    }
    output = read_typed_prose(
        client, system=system, user=user, output_type=ProseInterpretationOutput,
        factual_checks=(
            lambda value: _validate_references(
                value,
                segment_by_id=segment_by_id,
                subject_by_identity=subject_by_identity,
            ),
        ),
    )
    token_usage = token_usage_delta(usage_before, usage_snapshot(client), document_ids=[document.id])
    completeness = _measure_completeness(
        document_id=document.id,
        segments=segments,
        subjects=subjects,
        output=output,
    )
    candidates = tuple(
        _candidate_from_proposal(
            document,
            proposal,
            segment_by_id=segment_by_id,
            subject_by_identity=subject_by_identity,
            model=getattr(client, "model", None),
        )
        for proposal in output.proposals
    )
    config = injected_extractor_config(
        extractor="prose_interpretation",
        prompt_version=PROMPT_VERSION,
        model=getattr(client, "model", None),
        schema_version=SCHEMA_VERSION,
        prompt_bytes=system.encode("utf-8"),
        schema=schema,
        postprocessor_bytes=(
            Path(__file__).read_bytes()
            + Path(typed_output_module.__file__).read_bytes()
            + Path(facts_module.__file__).read_bytes()
        ),
        request_controls={
            "api": "structured_client",
            "provider_base_url": getattr(client, "base_url", None),
            "reasoning_effort": getattr(client, "effort", None),
            "store": False,
            "strict": True,
            "flex": getattr(client, "flex", None),
        },
    )
    appended = append_source_facts(
        session,
        document,
        idempotency_key=idempotency_key,
        prompt_version=PROMPT_VERSION,
        schema_version=SCHEMA_VERSION,
        candidate_count=len(candidates),
        page_errors=0,
        candidates=candidates,
        model=getattr(client, "model", None),
        extractor_config=config,
        token_usage=token_usage,
        row_accounting_json=completeness.model_dump(mode="json"),
        source_path=source_path,
    )
    return ProseInterpretationResult(output, completeness, appended)


def registered_subject_candidates(
    session: Session, project_id: int
) -> tuple[RegisteredSubjectCandidate, ...]:
    """Current project-subject adapter, replaced by #453's registry service."""

    parties = tuple(
        session.scalars(
            select(ExternalOrg)
            .join(Dependency, Dependency.external_org_id == ExternalOrg.id)
            .where(Dependency.project_id == project_id)
            .distinct()
            .order_by(ExternalOrg.id)
        ).all()
    )
    return tuple(
        RegisteredSubjectCandidate(
            subject_type="external_org",
            subject_id=party.id,
            display_name=party.name,
            aliases=tuple(party.aliases or ()),
        )
        for party in parties
    )


def _validate_references(
    output: ProseInterpretationOutput,
    *,
    segment_by_id: dict[int, SourceSegment],
    subject_by_identity: dict[tuple[str, int], RegisteredSubjectCandidate],
) -> None:
    read_ids = output.read_segment_ids
    if len(set(read_ids)) != len(read_ids) or set(read_ids) - set(segment_by_id):
        raise ValueError("read segments contain unknown or repeated references")
    proposal_identities = set()
    for proposal in output.proposals:
        contract = FACT_TYPE_CONTRACTS[proposal.fact_type]
        roles = {source.role for source in proposal.sources}
        if roles != contract.required_roles or len(proposal.sources) != len(roles):
            raise ValueError("proposal source roles do not match the Fact contract")
        source_ids = tuple(source.segment_id for source in proposal.sources)
        if set(source_ids) - set(segment_by_id) or set(source_ids) - set(read_ids):
            raise ValueError("proposal contains an unread or unknown segment reference")
        value_sources = tuple(
            segment_by_id[source.segment_id].exact_text
            for source in proposal.sources
            if source.role == "value_source"
        )
        try:
            replay_proposed_fact_value(proposal.fact_type, value_sources)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        if len(proposal.subject_candidates) != 1:
            raise ValueError("statement wording needs one registered subject candidate")
        subject_ref = proposal.subject_candidates[0]
        subject = subject_by_identity.get(
            (subject_ref.subject_type, subject_ref.subject_id)
        )
        if subject is None:
            raise ValueError("unknown subject candidate reference")
        attribution_sources = tuple(
            segment_by_id[source.segment_id]
            for source in proposal.sources
            if source.role == "attribution_source"
        )
        if len(attribution_sources) != 1:
            raise ValueError("statement wording needs one attribution source")
        if _matched_subject_wording(subject, attribution_sources[0].exact_text) is None:
            raise ValueError("subject candidate does not replay from attribution source")
        identity = (
            proposal.fact_type,
            source_ids,
            subject_ref.subject_type,
            subject_ref.subject_id,
        )
        if identity in proposal_identities:
            raise ValueError("typed Fact proposals cannot repeat")
        proposal_identities.add(identity)


def _measure_completeness(
    *,
    document_id: int,
    segments: tuple[SourceSegment, ...],
    subjects: tuple[RegisteredSubjectCandidate, ...],
    output: ProseInterpretationOutput,
) -> ProseCompleteness:
    all_segment_ids = {segment.id for segment in segments}
    read_ids = set(output.read_segment_ids)
    proposed_subject_ids = {
        subject.subject_id
        for proposal in output.proposals
        for subject in proposal.subject_candidates
    }
    all_subject_ids = {subject.subject_id for subject in subjects}
    return ProseCompleteness(
        document_id=document_id,
        detected_segment_count=len(all_segment_ids),
        read_segment_count=len(read_ids),
        proposed_fact_count=len(output.proposals),
        unread_segment_ids=tuple(sorted(all_segment_ids - read_ids)),
        proposed_subject_candidate_ids=tuple(sorted(proposed_subject_ids)),
        unproposed_subject_candidate_ids=tuple(
            sorted(all_subject_ids - proposed_subject_ids)
        ),
    )


def _candidate_from_proposal(
    document: Document,
    proposal: ProseFactProposal,
    *,
    segment_by_id: dict[int, SourceSegment],
    subject_by_identity: dict[tuple[str, int], RegisteredSubjectCandidate],
    model: str | None,
) -> Candidate:
    value_source = next(
        source for source in proposal.sources if source.role == "value_source"
    )
    segment = segment_by_id[value_source.segment_id]
    attribution_source = next(
        source for source in proposal.sources if source.role == "attribution_source"
    )
    attribution_segment = segment_by_id[attribution_source.segment_id]
    subject_ref = proposal.subject_candidates[0]
    subject = subject_by_identity[(subject_ref.subject_type, subject_ref.subject_id)]
    wording = _matched_subject_wording(subject, attribution_segment.exact_text)
    assert wording is not None
    description = replay_proposed_fact_value(
        proposal.fact_type, (segment.exact_text,)
    )
    return Candidate(
        project_id=document.project_id,
        source_document_id=document.id,
        source_pages=[segment.page_no],
        kind="event",
        state="pending",
        citations_verified=True,
        payload_json={
            "kind": "event",
            "fields": {
                "description": description,
                "external_org": wording,
                "stated_party": wording,
            },
            "citations": [
                {
                    "document_id": document.id,
                    "page": segment.page_no,
                    "quote": description,
                    "verified": True,
                    "whole_row": False,
                }
            ],
            "fact_sources": [
                {
                    "segment_id": source.segment_id,
                    "role": source.role,
                }
                for source in proposal.sources
            ],
            "tier": "prose_interpretation",
            "text_source": "source_segment",
        },
        confidence=None,
        prompt_version=PROMPT_VERSION,
        model=model,
    )


def _matched_subject_wording(
    subject: RegisteredSubjectCandidate, source_text: str
) -> str | None:
    matches = tuple(
        wording
        for wording in (subject.display_name, *subject.aliases)
        if wording and wording in source_text
    )
    return max(matches, key=lambda value: (len(value), value), default=None)
