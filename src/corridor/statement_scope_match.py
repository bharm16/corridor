"""Render the matcher's recorded ambiguous abstention as a narrowed-set card.

The statement-to-Constraint matcher (#370, ADR-0054) owns whether a statement
has zero, one, or several exact surviving Constraints.  This module owns none
of that matching logic and never re-derives it: it only reads the card the
matcher's abstention receipt already carries — the surviving candidate IDs,
the matched details, and the evidence applied — and binds it to the Constraint
rows the coordination screen is showing.  The card is read-only residue: it
shows what was already considered, and it never selects a scope for the
coordinator (ADR-0035).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


# The abstention card shape the matcher records (#370): candidate ids, the
# details it applied, the evidence verbatim, and the two legitimate human
# choice modes — each survivor, or both/all listed.
SCOPE_MATCH_CARD_KEYS = frozenset(
    {"candidate_dependency_ids", "matched_details", "evidence_applied", "choice_modes"}
)
SCOPE_MATCH_CHOICE_MODES = ("each", "both_all_listed")


@dataclass(frozen=True)
class ScopeMatchCandidate:
    """Read-only Constraint identity and context for one surviving candidate."""

    dependency_id: int
    ref_code: str
    title: str
    location: str | None = None
    source_ref: str | None = None
    station_from: str | None = None
    station_to: str | None = None


@dataclass(frozen=True)
class StatementScopeMatchCard:
    """A display model that never carries a selected scope result."""

    candidate_dependency_ids: tuple[int, ...]
    candidates: tuple[ScopeMatchCandidate, ...]
    matched_details: tuple[str, ...]
    evidence_applied: Mapping[str, object]


def scope_match_card_data(value: object) -> Mapping[str, object] | None:
    """Find the matcher's ambiguous card inside recorded abstention detail.

    The abstention receipt's ``eligibility_json`` belongs to the matcher; this
    reader only recognizes the card mapping wherever the receipt nests it and
    validates the shape strictly.  Anything else — a different reason, a
    malformed card, a single survivor — yields no card rather than a guess.
    """
    if isinstance(value, Mapping):
        if SCOPE_MATCH_CARD_KEYS <= set(value.keys()) and _valid_card(value):
            return value
        for item in value.values():
            found = scope_match_card_data(item)
            if found is not None:
                return found
    if isinstance(value, (list, tuple)):
        for item in value:
            found = scope_match_card_data(item)
            if found is not None:
                return found
    return None


def read_statement_scope_match_card(
    card: Mapping[str, object],
    candidates: tuple[ScopeMatchCandidate, ...],
) -> StatementScopeMatchCard | None:
    """Bind the matcher's abstention card to exactly the rows it left open.

    ``candidates`` are the screen's active Constraint rows.  When a surviving
    Constraint is no longer among them — the row was dismissed or reassigned
    after the abstention — the card is stale and nothing renders; the screen's
    ordinary explicit scope choices remain.
    """
    if not _valid_card(card):
        return None
    ids = tuple(int(identity) for identity in card["candidate_dependency_ids"])
    by_id = {candidate.dependency_id: candidate for candidate in candidates}
    if len(by_id) != len(candidates) or not set(ids) <= set(by_id):
        return None
    return StatementScopeMatchCard(
        candidate_dependency_ids=ids,
        candidates=tuple(by_id[identity] for identity in ids),
        matched_details=tuple(str(detail) for detail in card["matched_details"]),
        evidence_applied=dict(card["evidence_applied"]),
    )


def _valid_card(card: Mapping[str, object]) -> bool:
    ids = card.get("candidate_dependency_ids")
    if not isinstance(ids, (list, tuple)) or len(ids) < 2:
        return False
    if any(
        isinstance(identity, bool) or not isinstance(identity, int) or identity <= 0
        for identity in ids
    ):
        return False
    if len(set(ids)) != len(ids):
        return False
    details = card.get("matched_details")
    if not isinstance(details, (list, tuple)) or not all(
        isinstance(detail, str) for detail in details
    ):
        return False
    if not isinstance(card.get("evidence_applied"), Mapping):
        return False
    modes = card.get("choice_modes")
    if not isinstance(modes, (list, tuple)) or tuple(modes) != SCOPE_MATCH_CHOICE_MODES:
        return False
    return True
