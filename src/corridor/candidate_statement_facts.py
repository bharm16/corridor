"""Prepare Evidence-supported facts from one statement Candidate.

The browser, Event Admission, guided coordination, and Evidence Investigator
used to parse the same Candidate payload independently.  That let one reader
accept a timing shape or party wording that another reader refused.  This
module owns the read-only preparation of Candidate facts; it never admits a
statement or makes a human decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.external_statements import CitedStatementEvidence, StatementTiming
from corridor.models import Candidate, DocPage, Document, ExternalOrg
from corridor.verify import normalize


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
    organizations = tuple(
        session.scalars(select(ExternalOrg).order_by(ExternalOrg.id)).all()
    )

    affected_wording = str(fields.get("external_org") or "").strip()
    stated_wording = str(fields.get("stated_party") or "").strip()
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
            organizations, affected_wording, cited_text, visible_text
        ),
        stated_party=_party_fact(
            organizations, stated_wording, cited_text, visible_text
        ),
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
            document_id = int(citation["document_id"])
            page_no = int(citation["page"])
            quote = str(citation["quote"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if document_id <= 0 or page_no <= 0 or not quote:
            continue
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


def _party_fact(
    organizations: tuple[ExternalOrg, ...],
    wording: str,
    cited_text: str,
    visible_text: str,
) -> CandidatePartyFact:
    wanted = normalize(wording)
    if not wanted:
        return CandidatePartyFact("", (), False, False)
    matches = tuple(
        organization.id
        for organization in organizations
        if wanted
        in {
            normalize(value)
            for value in (organization.name, *(organization.aliases or ()))
            if value
        }
    )
    return CandidatePartyFact(
        wording=wording,
        registered_external_org_ids=matches,
        is_supported=wanted in cited_text,
        is_visible=wanted in visible_text,
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
        visible_timing = StatementTiming.day(evidence[0].quote, timing.start_date)
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
