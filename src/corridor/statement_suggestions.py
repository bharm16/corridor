"""Read deterministic, non-selecting statement-scope suggestions safely.

The Evidence Investigator originally owned a small deterministic shortlist for
its private tool namespace.  Ordinary guided statement review needs the same
kind of project-scoped context, but must not read a packet, inherit a model
rank, or turn a suggestion into a scope choice.  This module owns that public
read boundary.  It returns nothing until explicit eligibility is declared and
withholds the entire read while any declared observation window is open.

The signal arithmetic itself lives in ``corridor.statement_matching`` — the
same seam the investigator's private shortlist consumes — so the ADR-0054
identifying-language matcher (#370) can absorb one implementation, not two.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.merge import parse_station, resolve_org
from corridor.statement_matching import (
    dependency_match_signals,
    match_score,
    normalize_match_text,
)
from corridor.models import (
    Candidate,
    Dependency,
    EvidenceInvestigationCandidateReviewStart,
    EvidenceInvestigationShadowCase,
    EvidenceInvestigationShadowOutcome,
    StatementSuggestionEligibilityDeclaration,
    StatementSuggestionProtection,
    StatementSuggestionProtectionEnd,
)
from corridor.verify import normalize


SUGGESTION_CONTRACT_VERSION = "statement-suggestions-v1"
_PROTECTION_KINDS = frozenset({"shadow_cohort", "no_agent_baseline"})
_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class StatementSuggestion:
    """One current Constraint ordered by named deterministic signals only."""

    dependency_id: int
    ref_code: str
    source_ref: str | None
    title: str
    location_desc: str | None
    station_from: str | None
    station_to: str | None
    signals: tuple[str, ...]
    term_hits: int

    @property
    def selected(self) -> bool:
        """Suggestions are context, never a saved or default scope choice."""
        return False


def declare_statement_suggestion_eligibility(
    session: Session, project_id: int, candidate_id: int
) -> StatementSuggestionEligibilityDeclaration:
    """Declare one untouched ordinary Candidate eligible for deterministic order."""
    candidate = _current_statement_candidate(session, project_id, candidate_id)
    _require_unobserved_candidate(session, candidate)
    existing = session.scalar(
        select(StatementSuggestionEligibilityDeclaration).where(
            StatementSuggestionEligibilityDeclaration.candidate_id == candidate.id
        )
    )
    if existing is not None:
        return existing
    declaration = StatementSuggestionEligibilityDeclaration(
        project_id=project_id,
        candidate_id=candidate.id,
        contract_version=SUGGESTION_CONTRACT_VERSION,
    )
    session.add(declaration)
    session.flush([declaration])
    return declaration


def declare_statement_suggestion_protection(
    session: Session,
    project_id: int,
    candidate_id: int,
    *,
    kind: str,
    observation_contract: str,
) -> StatementSuggestionProtection:
    """Freeze one no-packet/no-ranking protection window before review starts."""
    if kind not in _PROTECTION_KINDS:
        raise ValueError("unknown statement-suggestion protection kind")
    if not observation_contract.strip():
        raise ValueError("a protection window needs its declared observation contract")
    candidate = _current_statement_candidate(session, project_id, candidate_id)
    _require_unobserved_candidate(session, candidate)
    existing = session.scalar(
        select(StatementSuggestionProtection).where(
            StatementSuggestionProtection.candidate_id == candidate.id,
            StatementSuggestionProtection.kind == kind,
            StatementSuggestionProtection.observation_contract
            == observation_contract.strip(),
        )
    )
    if existing is not None:
        return existing
    protection = StatementSuggestionProtection(
        project_id=project_id,
        candidate_id=candidate.id,
        kind=kind,
        observation_contract=observation_contract.strip(),
    )
    session.add(protection)
    session.flush([protection])
    return protection


def end_statement_suggestion_protection(
    session: Session, protection_id: int
) -> StatementSuggestionProtectionEnd:
    """Append an explicit end; an outcome capture never ends a window by itself."""
    protection = session.get(StatementSuggestionProtection, protection_id)
    if protection is None:
        raise ValueError("statement-suggestion protection does not exist")
    if protection.kind != "no_agent_baseline":
        raise ValueError(
            "a shadow cohort remains protected by its immutable frozen membership"
        )
    ended = session.scalar(
        select(StatementSuggestionProtectionEnd).where(
            StatementSuggestionProtectionEnd.protection_id == protection.id
        )
    )
    if ended is not None:
        return ended
    ended = StatementSuggestionProtectionEnd(
        protection_id=protection.id,
        ended_at=datetime.now(timezone.utc),
    )
    session.add(ended)
    session.flush([ended])
    return ended


def read_statement_suggestions(
    session: Session, project_id: int, candidate_id: int
) -> tuple[StatementSuggestion, ...]:
    """Return current project suggestions only when the exact protection read allows it."""
    candidate = _current_statement_candidate(session, project_id, candidate_id)
    if _protected_or_unresolved(session, candidate):
        return ()
    fields = (candidate.payload_json or {}).get("fields") or {}
    party_name = str(fields.get("external_org") or "").strip()
    if not party_name:
        return ()
    party = resolve_org(session, party_name)
    if party is None:
        return ()
    dependencies = session.scalars(
        select(Dependency)
        .where(
            Dependency.project_id == project_id,
            Dependency.dismissed_at.is_(None),
            Dependency.external_org_id == party.id,
        )
        .order_by(Dependency.ref_code, Dependency.id)
    ).all()
    suggestions = [_suggestion(fields, dependency) for dependency in dependencies]
    suggestions = [item for item in suggestions if len(item.signals) > 1]
    return tuple(
        sorted(
            suggestions,
            key=lambda item: (
                -int("explicit_constraint_reference" in item.signals),
                -match_score(
                    station_containment="station_overlap" in item.signals,
                    term_hits=item.term_hits,
                ),
                item.ref_code,
                item.dependency_id,
            ),
        )
    )


def _current_statement_candidate(
    session: Session, project_id: int, candidate_id: int
) -> Candidate:
    candidate = session.get(Candidate, candidate_id)
    if (
        candidate is None
        or candidate.project_id != project_id
        or candidate.kind != "event"
        or candidate.state != "pending"
        or not candidate.citations_verified
    ):
        raise ValueError("statement suggestions require one current eligible Candidate")
    return candidate


def _require_unobserved_candidate(session: Session, candidate: Candidate) -> None:
    """Protection/eligibility is declared before any result can contaminate it.

    A terminal model attempt is already cohort history, even if its packet is
    absent or failed validation.  Allowing a later eligibility declaration to
    call that case ordinary would erase the very comparison this gate protects.
    """
    observed = session.scalar(
        select(EvidenceInvestigationCandidateReviewStart.id).where(
            EvidenceInvestigationCandidateReviewStart.candidate_id == candidate.id
        )
    )
    shadow_outcome = session.scalar(
        select(EvidenceInvestigationShadowOutcome.id)
        .join(
            EvidenceInvestigationShadowCase,
            EvidenceInvestigationShadowCase.id
            == EvidenceInvestigationShadowOutcome.shadow_case_id,
        )
        .where(EvidenceInvestigationShadowCase.candidate_id == candidate.id)
    )
    shadow_case = session.scalar(
        select(EvidenceInvestigationShadowCase.id).where(
            EvidenceInvestigationShadowCase.candidate_id == candidate.id
        )
    )
    if observed is not None or shadow_outcome is not None or shadow_case is not None:
        raise ValueError(
            "suggestion protection must be declared before review or a human outcome"
        )


def _protected_or_unresolved(session: Session, candidate: Candidate) -> bool:
    """Fail closed without querying a packet, runtime version, or packet visibility."""
    eligibility = session.scalar(
        select(StatementSuggestionEligibilityDeclaration.id).where(
            StatementSuggestionEligibilityDeclaration.candidate_id == candidate.id,
            StatementSuggestionEligibilityDeclaration.project_id == candidate.project_id,
            StatementSuggestionEligibilityDeclaration.contract_version
            == SUGGESTION_CONTRACT_VERSION,
        )
    )
    if eligibility is None:
        return True
    shadow_case = session.scalar(
        select(EvidenceInvestigationShadowCase.id)
        .where(EvidenceInvestigationShadowCase.candidate_id == candidate.id)
        .limit(1)
    )
    if shadow_case is not None:
        return True
    active_protection = session.scalar(
        select(StatementSuggestionProtection.id)
        .outerjoin(
            StatementSuggestionProtectionEnd,
            StatementSuggestionProtectionEnd.protection_id
            == StatementSuggestionProtection.id,
        )
        .where(
            StatementSuggestionProtection.project_id == candidate.project_id,
            StatementSuggestionProtection.candidate_id == candidate.id,
            StatementSuggestionProtectionEnd.id.is_(None),
        )
        .limit(1)
    )
    return active_protection is not None


def _suggestion(fields: dict, dependency: Dependency) -> StatementSuggestion:
    shared, term_hits = dependency_match_signals(
        dependency,
        source_stations=_statement_stations(fields),
        term_keys=_statement_terms(fields),
    )
    signals = list(shared)
    reference = normalize(str(fields.get("conflict_ref") or ""))
    if reference and reference in {
        normalize(dependency.ref_code),
        normalize(dependency.source_ref or ""),
    }:
        signals.insert(1, "explicit_constraint_reference")
    return StatementSuggestion(
        dependency_id=dependency.id,
        ref_code=dependency.ref_code,
        source_ref=dependency.source_ref,
        title=dependency.title,
        location_desc=dependency.location_desc,
        station_from=dependency.station_from,
        station_to=dependency.station_to,
        signals=tuple(signals),
        term_hits=term_hits,
    )


def _statement_stations(fields: dict) -> tuple[float, ...]:
    parsed = (
        parse_station(fields.get("station_from")),
        parse_station(fields.get("station_to")),
    )
    return tuple(value for value in parsed if value is not None)


def _statement_terms(fields: dict) -> tuple[str, ...]:
    """Derive shortlist terms from the statement's own recorded description."""
    tokens = _WORD.findall(normalize_match_text(str(fields.get("description") or "")))
    return tuple(
        dict.fromkeys(
            token
            for token in tokens
            if len(token) >= 4
            and token not in {"near", "station", "will", "that", "this", "with"}
        )
    )
