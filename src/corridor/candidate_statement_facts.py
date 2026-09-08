"""Prepare Evidence-supported facts from one statement Candidate.

The browser, Event Admission, guided coordination, and Evidence Investigator
used to parse the same Candidate payload independently.  That let one reader
accept a timing shape or party wording that another reader refused.  This
module owns Candidate fact preparation and records the released exact subject
resolution result against immutable prose support.  It never admits a
statement or makes a human decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.models import (
    Candidate,
    DocPage,
    Document,
    ExtractedProposal,
    ExtractedProposalFact,
    Fact,
    FactSource,
    SourceSegment,
)
from corridor.statement_values import CitedStatementEvidence, StatementTiming
from corridor.subject_resolution import (
    RegisteredSubjectCandidate,
    SubjectResolutionRefusal,
    normalize_subject_reference,
    registered_subject_candidates,
    resolve_subject_reference,
)
from corridor.verify import normalize
from corridor.prose_spans import prose_segment_filter


@dataclass(frozen=True)
class CandidateEvidence:
    """One immutable Candidate citation with its registered page context."""

    document_id: int
    page_no: int
    quote: str
    filename: str
    page_text: str | None
    page_text_source: str | None
    has_page_image: bool

    @property
    def is_reviewable(self) -> bool:
        return self.page_text_source == "cells" or self.has_page_image

    @property
    def cited(self) -> CitedStatementEvidence:
        return CitedStatementEvidence(self.document_id, self.page_no, self.quote)


@dataclass(frozen=True)
class CandidatePartyFact:
    """One party wording and the exact registered identity Evidence supports."""

    wording: str
    registered_external_org_ids: tuple[int, ...]
    is_supported: bool
    is_visible: bool
    resolution_state: str | None = None
    attention_reason: str | None = None
    rule_identity: str | None = None
    resolution_attempt_id: int | None = None

    @property
    def external_org_id(self) -> int | None:
        if self.is_supported and len(self.registered_external_org_ids) == 1:
            return self.registered_external_org_ids[0]
        return None

    @property
    def visible_external_org_id(self) -> int | None:
        if self.is_visible and len(self.registered_external_org_ids) == 1:
            return self.registered_external_org_ids[0]
        return None


@dataclass(frozen=True)
class CandidateTimingFact:
    """One timing parsed only at the precision the Candidate supplied."""

    is_present: bool
    timing: StatementTiming | None
    visible_timing: StatementTiming | None
    is_supported: bool
    is_visible: bool
    invalid_reason: str | None = None


@dataclass(frozen=True)
class CandidateStatementFacts:
    """The read-only facts every statement Candidate adapter may consume."""

    fields: dict
    evidence: tuple[CandidateEvidence, ...]
    evidence_is_complete: bool
    affected_party: CandidatePartyFact
    stated_party: CandidatePartyFact
    source_stated_party_wording: str
    description: str
    description_is_supported: bool
    event_date: date | None
    event_date_is_valid: bool
    new_timing: CandidateTimingFact
    previous_timing: CandidateTimingFact

    @property
    def evidence_is_reviewable(self) -> bool:
        return self.evidence_is_complete and all(
            item.is_reviewable for item in self.evidence
        )

    @property
    def cited_evidence(self) -> tuple[CitedStatementEvidence, ...]:
        return tuple(item.cited for item in self.evidence)

def prepare_candidate_statement_facts(
    session: Session, candidate: Candidate
) -> CandidateStatementFacts:
    """Prepare one Candidate without promoting any proposal into Ledger state."""
    payload = candidate.payload_json if isinstance(candidate.payload_json, dict) else {}
    raw_fields = payload.get("fields")
    fields = raw_fields if isinstance(raw_fields, dict) else {}
    citations = payload.get("citations")
    citation_values = citations if isinstance(citations, list) else []
    evidence = _candidate_evidence(session, candidate, citation_values)
    evidence_is_complete = (
        candidate.citations_verified
        and bool(citation_values)
        and len(evidence) == len(citation_values)
    )
    cited_text = _cited_evidence_text(evidence)
    visible_text = _visible_evidence_text(evidence)
    attribution_segment, requires_attribution_segment = _statement_attribution_segment(
        session, candidate
    )
    subject_candidates = (
        registered_subject_candidates(session, candidate.project_id)
        if attribution_segment is None and not requires_attribution_segment
        else ()
    )

    affected_wording = str(fields.get("external_org") or "").strip()
    stated_wording = str(fields.get("stated_party") or "").strip()
    source_stated_party_wording = stated_wording or affected_wording
    description = str(fields.get("description") or "").strip()
    raw_event_date = fields.get("event_date")
    event_date = _parse_date(raw_event_date)
    event_date_is_valid = raw_event_date in (None, "") or event_date is not None
    new_timing = _candidate_timing_fact(
        fields.get("committed_date"), cited_text, visible_text, evidence
    )
    previous_timing = _candidate_timing_fact(
        fields.get("previous_timing"), cited_text, visible_text, evidence
    )
    return CandidateStatementFacts(
        fields=fields,
        evidence=evidence,
        evidence_is_complete=evidence_is_complete,
        affected_party=_party_fact(
            session,
            project_id=candidate.project_id,
            subject_candidates=subject_candidates,
            source_segment=attribution_segment,
            requires_source_segment=requires_attribution_segment,
            wording=affected_wording,
            cited_text=cited_text,
            visible_text=visible_text,
            usage="affected_subject",
        ),
        stated_party=_party_fact(
            session,
            project_id=candidate.project_id,
            subject_candidates=subject_candidates,
            source_segment=attribution_segment,
            requires_source_segment=requires_attribution_segment,
            wording=stated_wording,
            cited_text=cited_text,
            visible_text=visible_text,
            usage="statement_speaker",
        ),
        source_stated_party_wording=source_stated_party_wording,
        description=description,
        description_is_supported=(
            bool(description) and normalize(description) in cited_text
        ),
        event_date=event_date,
        event_date_is_valid=event_date_is_valid,
        new_timing=new_timing,
        previous_timing=previous_timing,
    )


def _candidate_evidence(
    session: Session,
    candidate: Candidate,
    citations: list,
) -> tuple[CandidateEvidence, ...]:
    evidence: list[CandidateEvidence] = []
    for citation in citations:
        if not isinstance(citation, dict) or citation.get("verified") is not True:
            continue
        try:
            document_id = citation["document_id"]
            page_no = citation["page"]
            quote = citation["quote"]
        except KeyError:
            continue
        if (
            isinstance(document_id, bool)
            or not isinstance(document_id, int)
            or document_id <= 0
            or isinstance(page_no, bool)
            or not isinstance(page_no, int)
            or page_no <= 0
            or not isinstance(quote, str)
            or not quote.strip()
        ):
            continue
        quote = quote.strip()
        document = session.scalar(
            select(Document).where(
                Document.id == document_id,
                Document.project_id == candidate.project_id,
            )
        )
        page = session.scalar(
            select(DocPage).where(
                DocPage.document_id == document_id,
                DocPage.page_no == page_no,
            )
        )
        if document is None or page is None:
            continue
        has_page_image = bool(page.image_path and Path(page.image_path).is_file())
        evidence.append(
            CandidateEvidence(
                document_id=document_id,
                page_no=page_no,
                quote=quote,
                filename=document.filename,
                page_text=page.text,
                page_text_source=page.text_source,
                has_page_image=has_page_image,
            )
        )
    return tuple(evidence)


def _statement_attribution_segment(
    session: Session, candidate: Candidate
) -> tuple[SourceSegment | None, bool]:
    """Return the proposal's exact role-tagged prose source, never page text."""

    if candidate.id is None:
        return None, False
    has_proposal = session.scalar(
        select(ExtractedProposal.id)
        .where(ExtractedProposal.candidate_id == candidate.id)
        .limit(1)
    )
    if has_proposal is None:
        return None, False
    segments = tuple(
        session.scalars(
            select(SourceSegment)
            .join(FactSource, FactSource.source_segment_id == SourceSegment.id)
            .join(Fact, Fact.id == FactSource.fact_id)
            .join(ExtractedProposalFact, ExtractedProposalFact.fact_id == Fact.id)
            .join(
                ExtractedProposal,
                ExtractedProposal.id == ExtractedProposalFact.proposal_id,
            )
            .where(
                ExtractedProposal.candidate_id == candidate.id,
                ExtractedProposal.project_id == candidate.project_id,
                ExtractedProposal.document_id == candidate.source_document_id,
                Fact.fact_type == "statement_wording",
                FactSource.role == "attribution_source",
                prose_segment_filter(SourceSegment),
            )
            .order_by(SourceSegment.id)
        ).all()
    )
    return (segments[0] if len(segments) == 1 else None), True


def _party_fact(
    session: Session,
    *,
    project_id: int,
    subject_candidates: tuple[RegisteredSubjectCandidate, ...],
    source_segment: SourceSegment | None,
    requires_source_segment: bool,
    wording: str,
    cited_text: str,
    visible_text: str,
    usage: str,
) -> CandidatePartyFact:
    wanted = normalize(wording)
    if not wanted:
        return CandidatePartyFact("", (), False, False)
    if requires_source_segment and source_segment is None:
        return CandidatePartyFact(
            wording=wording,
            registered_external_org_ids=(),
            is_supported=False,
            is_visible=wanted in visible_text,
            resolution_state="unresolved",
            attention_reason="statement_attribution_source_unavailable",
        )
    supported_text = (
        normalize(source_segment.exact_text)
        if source_segment is not None
        else cited_text
    )
    if source_segment is not None:
        try:
            result = resolve_subject_reference(
                session,
                project_id=project_id,
                source_segment_id=source_segment.id,
                reference_kind="organization_name",
                raw_reference=wording,
                expected_subject_type="external_org",
                usage=usage,
            )
        except SubjectResolutionRefusal:
            return CandidatePartyFact(
                wording=wording,
                registered_external_org_ids=(),
                is_supported=False,
                is_visible=wanted in visible_text,
                resolution_state="unresolved",
                attention_reason="subject_reference_not_in_prose_segment",
            )
        matches = (
            (result.subject_id,)
            if result.subject_type == "external_org" and result.subject_id is not None
            else tuple(
                candidate.subject_id
                for candidate in result.candidates
                if candidate.subject_type == "external_org"
            )
        )
        return CandidatePartyFact(
            wording=wording,
            registered_external_org_ids=matches,
            is_supported=wanted in supported_text,
            is_visible=wanted in visible_text,
            resolution_state=result.state,
            attention_reason=result.attention_reason,
            rule_identity=result.rule_identity,
            resolution_attempt_id=result.attempt_id,
        )

    matches = _registered_external_org_ids(subject_candidates, wording)
    return CandidatePartyFact(
        wording=wording,
        registered_external_org_ids=matches,
        is_supported=wanted in supported_text,
        is_visible=wanted in visible_text,
    )


def _registered_external_org_ids(
    subject_candidates: tuple[RegisteredSubjectCandidate, ...], wording: str
) -> tuple[int, ...]:
    """Preserve legacy reads through the canonical exact registry only."""

    wanted = normalize_subject_reference("organization_name", wording)
    return tuple(
        candidate.subject_id
        for candidate in subject_candidates
        if candidate.subject_type == "external_org"
        and wanted
        in {
            normalize_subject_reference("organization_name", value)
            for value in (candidate.display_name, *candidate.aliases)
        }
    )


def _candidate_timing_fact(
    value: object,
    cited_text: str,
    visible_text: str,
    evidence: tuple[CandidateEvidence, ...],
) -> CandidateTimingFact:
    is_present = value not in (None, "")
    timing = _parse_candidate_timing(value) if is_present else None
    visible_timing = (
        timing
        if timing is not None and normalize(timing.text) in visible_text
        else None
    )
    if (
        visible_timing is None
        and timing is not None
        and timing.precision == "day"
        and timing.start_date is not None
        and evidence
    ):
        visible_timing = _visible_day_timing(timing, evidence)
    return CandidateTimingFact(
        is_present=is_present,
        timing=timing,
        visible_timing=visible_timing,
        is_supported=(
            timing is not None
            and normalize(timing.text) in cited_text
        ),
        is_visible=visible_timing is not None,
        invalid_reason=(
            _candidate_timing_invalid_reason(value)
            if is_present and timing is None
            else None
        ),
    )


def _parse_candidate_timing(value: object) -> StatementTiming | None:
    if isinstance(value, dict):
        text = str(value.get("text") or "").strip()
        precision = str(value.get("precision") or "").strip()
        if precision == "approximate" and text:
            if value.get("start_date") in (None, "") and value.get("end_date") in (
                None,
                "",
            ):
                return StatementTiming.approximate(text)
            return None
        start = _parse_date(value.get("start_date"))
        end = _parse_date(value.get("end_date"))
        if precision == "day" and start is not None and end == start and text:
            return StatementTiming.day(text, start)
        if precision == "month" and start is not None and end is not None and text:
            expected = StatementTiming.month(text, start.year, start.month)
            return (
                expected
                if start == expected.start_date and end == expected.end_date
                else None
            )
        return None
    parsed = _parse_date(value)
    return StatementTiming.day(str(value), parsed) if parsed is not None else None


def _candidate_timing_invalid_reason(value: object) -> str:
    if isinstance(value, dict) and value.get("precision") == "month":
        start = _parse_date(value.get("start_date"))
        end = _parse_date(value.get("end_date"))
        if start is not None and end is not None:
            return "invalid_calendar_bounds"
    return "invalid_shape"


def _visible_day_timing(
    timing: StatementTiming,
    evidence: tuple[CandidateEvidence, ...],
) -> StatementTiming | None:
    """Preserve the exact visible day wording instead of substituting its quote."""
    value = timing.start_date
    if value is None:
        return None
    named = re.compile(
        rf"\b{re.escape(value.strftime('%B'))}\s+0?{value.day}"
        rf"(?:st|nd|rd|th)?(?:,\s*|\s+){value.year}\b",
        re.IGNORECASE,
    )
    numeric = re.compile(
        rf"\b0?{value.month}/0?{value.day}/(?:{value.year}|{value.year % 100:02d})\b"
    )
    for item in evidence:
        for source_text in (item.quote, item.page_text or ""):
            match = named.search(source_text) or numeric.search(source_text)
            if match is not None:
                return StatementTiming.day(match.group(0), value)
    return None


def _parse_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for date_format in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            continue
    return None


def _visible_evidence_text(evidence: tuple[CandidateEvidence, ...]) -> str:
    return normalize(
        " ".join(
            value
            for item in evidence
            for value in (item.quote, item.page_text or "")
            if value
        )
    )


def _cited_evidence_text(evidence: tuple[CandidateEvidence, ...]) -> str:
    return normalize(" ".join(item.quote for item in evidence))
