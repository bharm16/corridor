"""Adapt statement HTTP forms to the statement-coordination interface.

The browser carries strings and repeated fields; the domain command accepts
typed, evidence-bound drafts.  This module owns that translation so every
statement route reports malformed input through one refusal vocabulary and no
generic queue parser can silently replace statement behavior.
"""

from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from corridor.candidate_statement_facts import (
    CandidateStatementFacts,
    prepare_candidate_statement_facts,
)
from corridor.external_statements import (
    CitedStatementEvidence,
    StatementScope,
    StatementTiming,
)
from corridor import presentation
from corridor.models import Candidate
from corridor.statement_coordination import (
    StatementCoordinationDraft,
    StatementCoordinationPredecessors,
    StatementCoordinationRefusal,
    StatementFactCorrectionDraft,
)


# The screen's disabled control and this module's refusal are the same offer,
# so the sentence is minted once beside the reading that decides it.
CANDIDATE_EVIDENCE_UNAVAILABLE = presentation.GUIDED_SAVE_EVIDENCE_UNAVAILABLE


@dataclass(frozen=True)
class CandidateStatementEvidenceView:
    """Registered page context rendered and selected by the statement form."""

    document_id: int
    page_no: int
    quote: str
    filename: str
    page_text: str | None
    page_text_source: str | None
    has_page_image: bool
    supporting_quote_available: bool


def statement_coordination_draft(
    session: Session, candidate: Candidate, form
) -> StatementCoordinationDraft:
    """Build the atomic guided-Save command from one submitted form."""
    return StatementCoordinationDraft(
        candidate_id=candidate.id,
        affected_external_org_id=required_positive_form_id(
            form, "affected_external_org_id"
        ),
        stated_party=_required_form_text(
            form, "stated_party", "the organization that made the statement"
        ),
        stated_external_org_id=required_positive_form_id(
            form, "stated_external_org_id"
        ),
        event_date=optional_form_date(form, "event_date"),
        description=_required_form_text(form, "description", "what the party said"),
        new_timing=_form_timing(form, "new_timing", required=True),
        previous_timing=_form_timing(form, "previous_timing", required=False),
        evidence=supporting_statement_evidence(session, candidate, form),
        scope=statement_scope_from_form(form),
        internal_owner_roster_entry_id=required_positive_form_id(
            form, "internal_owner_roster_entry_id"
        ),
        next_action=_required_form_text(form, "next_action", "a Next Action"),
        action_due_date=optional_form_date(form, "action_due_date"),
        action_due_date_unknown_reason=(
            str(form.get("action_due_date_unknown_reason") or "").strip() or None
        ),
        milestone_impact=(str(form.get("milestone_impact") or "").strip() or None),
        milestone_ids=tuple(
            _required_positive_value(value, "milestone_id")
            for value in form.getlist("milestone_id")
        ),
        expected=StatementCoordinationPredecessors(
            candidate_state=str(form.get("expected_candidate_state") or "pending"),
            commitment_lineage_id=_optional_positive_form_id(
                form, "expected_commitment_lineage_id"
            ),
            statement_event_id=_optional_positive_form_id(
                form, "expected_statement_event_id"
            ),
            scope_decision_id=_optional_positive_form_id(
                form, "expected_scope_decision_id"
            ),
            internal_owner_decision_id=_optional_positive_form_id(
                form, "expected_internal_owner_decision_id"
            ),
            next_action_decision_id=_optional_positive_form_id(
                form, "expected_next_action_decision_id"
            ),
            milestone_impact_decision_id=_optional_positive_form_id(
                form, "expected_milestone_impact_decision_id"
            ),
        ),
    )


def statement_fact_correction_draft(
    form, candidate_id: int
) -> StatementFactCorrectionDraft:
    """Build one Evidence-bound factual correction from submitted fields."""
    evidence = CitedStatementEvidence(
        required_positive_form_id(form, "evidence_document_id"),
        required_positive_form_id(form, "evidence_page_no"),
        _required_form_text(form, "evidence_quote", "a supporting source passage"),
    )
    return StatementFactCorrectionDraft(
        candidate_id=candidate_id,
        expected_statement_event_id=required_positive_form_id(
            form, "expected_statement_event_id"
        ),
        affected_external_org_id=required_positive_form_id(
            form, "affected_external_org_id"
        ),
        stated_party=_required_form_text(
            form, "stated_party", "the organization that made the statement"
        ),
        stated_external_org_id=required_positive_form_id(
            form, "stated_external_org_id"
        ),
        event_date=optional_form_date(form, "event_date"),
        description=_required_form_text(form, "description", "what the party said"),
        new_timing=_form_timing(form, "new_timing", required=True),
        previous_timing=_form_timing(form, "previous_timing", required=False),
        evidence=(evidence,),
    )


def candidate_statement_evidence_view(
    facts: CandidateStatementFacts,
) -> tuple[CandidateStatementEvidenceView, ...]:
    """Render the extractor's immutable citations and registered page context."""
    return tuple(
        CandidateStatementEvidenceView(
            document_id=item.document_id,
            page_no=item.page_no,
            quote=item.quote,
            filename=item.filename,
            page_text=item.page_text,
            page_text_source=item.page_text_source,
            has_page_image=item.has_page_image,
            supporting_quote_available=item.is_reviewable,
        )
        for item in facts.evidence
    )


def supporting_statement_evidence(
    session: Session, candidate: Candidate, form
) -> tuple[CitedStatementEvidence, ...]:
    """Bind optional supporting wording to a source page already on screen."""
    facts = prepare_candidate_statement_facts(session, candidate)
    visible_evidence = candidate_statement_evidence_view(facts)
    offer = presentation.read_supporting_evidence_offer(
        evidence_available=facts.evidence_is_reviewable
    )
    if not offer.available:
        raise StatementCoordinationRefusal(offer.refusal)
    page_index_value = str(form.get("supporting_page_index") or "").strip()
    quote = str(form.get("supporting_quote") or "").strip()
    if not page_index_value and not quote:
        return ()
    if not page_index_value or not quote:
        raise StatementCoordinationRefusal(
            "choose a visible registered source page and its exact supporting quote together"
        )
    try:
        page_index = int(page_index_value)
        if page_index < 0:
            raise IndexError
        selected = visible_evidence[page_index]
        if not selected.supporting_quote_available:
            raise StatementCoordinationRefusal(
                "the rendered source page is unavailable as supporting documentation"
            )
        document_id = selected.document_id
        page_no = selected.page_no
    except StatementCoordinationRefusal:
        raise
    except (IndexError, TypeError, ValueError) as exc:
        raise StatementCoordinationRefusal(
            "choose a visible registered source page for the supporting passage"
        ) from exc
    return (CitedStatementEvidence(document_id, page_no, quote),)


def statement_scope_from_form(form) -> StatementScope:
    """Read one explicit Commitment Scope choice without inferring a default."""
    mode = str(form.get("scope_mode") or "").strip()
    if mode == "unknown":
        return StatementScope.unknown()
    if mode == "all_active":
        return StatementScope.all_active()
    if mode == "selected":
        return StatementScope.selected(
            tuple(
                _required_positive_value(value, "dependency_id")
                for value in form.getlist("dependency_id")
            )
        )
    raise StatementCoordinationRefusal("choose which constraints this statement applies to, or keep the links unknown")


def required_positive_form_id(form, name: str) -> int:
    """Read one required positive identity in statement-refusal vocabulary."""
    return _required_positive_value(form.get(name), name)


def optional_form_date(form, name: str) -> date | None:
    """Read an optional ISO date in statement-refusal vocabulary."""
    raw = str(form.get(name) or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise StatementCoordinationRefusal(
            f"{name.replace('_', ' ')} must be a date"
        ) from exc


def _form_timing(form, prefix: str, *, required: bool) -> StatementTiming | None:
    text = str(form.get(f"{prefix}_text") or "").strip()
    precision = str(form.get(f"{prefix}_precision") or "").strip()
    start = optional_form_date(form, f"{prefix}_start_date")
    end = optional_form_date(form, f"{prefix}_end_date")
    if not any((text, precision, start, end)):
        if required:
            raise StatementCoordinationRefusal("the statement needs its new timing")
        return None
    if not text or not precision:
        raise StatementCoordinationRefusal(
            "each stated timing needs source wording and precision"
        )
    if precision == "day":
        if start is None or end != start:
            raise StatementCoordinationRefusal(
                "an exact-day timing needs the same start and end date"
            )
        return StatementTiming.day(text, start)
    if precision == "month":
        if start is None or end is None:
            raise StatementCoordinationRefusal(
                "a month timing needs its calendar bounds"
            )
        return StatementTiming(text, "month", start, end)
    if precision == "approximate":
        if start is not None or end is not None:
            raise StatementCoordinationRefusal(
                "an approximate timing cannot claim calendar bounds"
            )
        return StatementTiming.approximate(text)
    raise StatementCoordinationRefusal(
        "timing precision must be day, month, or approximate"
    )


def _required_form_text(form, name: str, label: str) -> str:
    value = str(form.get(name) or "").strip()
    if not value:
        raise StatementCoordinationRefusal(f"{label} is required")
    return value


def _optional_positive_form_id(form, name: str) -> int | None:
    value = form.get(name)
    if value is None or not str(value).strip():
        return None
    return _required_positive_value(value, name)


def _required_positive_value(value, name: str) -> int:
    try:
        identity = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise StatementCoordinationRefusal(
            f"{name} must be a positive identity"
        ) from exc
    if identity <= 0:
        raise StatementCoordinationRefusal(f"{name} must be a positive identity")
    return identity
