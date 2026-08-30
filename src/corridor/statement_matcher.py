"""Match a statement to Constraints only when its retained facts say one row.

The statement-review assistant originally held a small station-and-term scorer
privately.  That was useful for ordering a person's choices, but could never
be a Record Inclusion predicate: a high score is still a guess.  ADR-0054
moves the scorer here and separates its two uses.

``shortlist_dependencies`` ranks the assistant's bounded options — the exact
observations the private scorer made, unchanged, still presentation-only.
``match_statement_scope`` applies ADR-0054's evidence stack as deterministic
*filters* and returns an exact result only when one active Constraint survives
the full stack.  Every deciding fact is verbatim presence, station
containment, recorded state, or a recorded association; no similarity score
or threshold decides anything.  Context arrives as already-recorded
associations owned by the thread/follow-up/commitment services — source words
are data, never commands, and a message-id chain is provenance only.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Iterable

from corridor.merge import parse_station
from corridor.models import Dependency
from corridor.verify import normalize


MATCHER_VERSION = "statement-identifying-language-v1"
# Each evidence kind carries its own version, and all of them feed the matcher
# fingerprint.  Adding an evidence kind adds an entry, which changes the
# fingerprint, which voids any prior ADR-0050 replay pass — readings are never
# pooled across versions.  The mapping is deliberately data rather than an
# incidental list inside a digest so a later evidence kind cannot slip in
# silently.
MATCHER_EVIDENCE_VERSIONS = {
    "explicit_reference": "v1",
    "identifying_terms": "v1",
    "station": "v1",
    "row_state": "v1",
    "thread": "v1",
    "recorded_ask": "v1",
    "promise_chain": "v1",
    "row_contact": "v1",
}

# The exact future-action verbs whose row-state applicability is defined.  A
# relocation promise cannot apply to a protect-in-place row or a row whose
# Commitment already has Completion Reported.  Any other verb fits both, so
# state narrows nothing (the ticket's "a promise whose verb fits both rows is
# not narrowed by state").
_RELOCATION_VERBS = frozenset({"relocate", "relocated", "relocating"})


@dataclass(frozen=True)
class StatementMatchContext:
    """Already-recorded context the caller may apply as exact filters.

    Every ID here is a recorded association owned by the thread, follow-up,
    commitment, or closure machinery — never an extracted guess, and never
    text read out of the statement itself.  An empty or plural ask
    deliberately contributes nothing; it is not an instruction to select one.
    """

    # Constraints earlier messages in the same thread (by message-id chain)
    # were recorded against.  The chain is provenance data only.
    thread_dependency_ids: tuple[int, ...] = ()
    # Constraints named by currently open recorded follow-up asks for this
    # organization.  Only an exactly-one set narrows.
    open_ask_dependency_ids: tuple[int, ...] = ()
    # Constraints inside the organization's open Commitment scopes.
    promise_dependency_ids: tuple[int, ...] = ()
    # Constraints whose governing Commitment has Completion Reported
    # (a verified Closure) — the "completed/cleared" row state.
    closed_dependency_ids: tuple[int, ...] = ()
    sender: str | None = None
    named_speaker: str | None = None


@dataclass(frozen=True)
class StatementScopeMatch:
    kind: str  # exact | ambiguous | none | unknown
    dependency_ids: tuple[int, ...]
    evidence: dict
    card: dict | None = None


@dataclass(frozen=True)
class DependencyShortlistSignal:
    dependency_id: int
    rank: int
    signals: tuple[str, ...]


def matcher_fingerprint() -> str:
    """The stable per-evidence-kind fingerprint included in policy receipts."""
    material = "|".join(
        f"{name}:{version}"
        for name, version in sorted(MATCHER_EVIDENCE_VERSIONS.items())
    )
    return sha256(f"{MATCHER_VERSION}|{material}".encode()).hexdigest()


# --------------------------------------------------------------------------- #
# The assistant's shortlist — ranking only, behavior-identical to the scorer  #
# it replaces in the Statement Review Assistant's private tool layer.         #
# --------------------------------------------------------------------------- #


def shortlist_dependencies(
    dependencies: Iterable[Dependency],
    *,
    station_text: str | None,
    terms: tuple[str, ...],
) -> tuple[DependencyShortlistSignal, ...]:
    """Rank bounded options from shared, non-decisive signals.

    This reproduces the assistant's original scorer exactly — the same
    casefold normalization, the same score, the same (score, ref_code, id)
    ordering — so extraction changed no reader-visible result.  Exact
    admission never consults the rank or any threshold.
    """
    source_station = parse_station(station_text)
    term_keys = tuple(
        key for key in (_casefold(term) for term in terms) if key
    )
    ranked: list[tuple[int, str, int, DependencyShortlistSignal]] = []
    for dependency in dependencies:
        signals: list[str] = []
        if source_station is not None and _station_contains(
            dependency, source_station
        ):
            signals.append("station_containment")
        haystack = _casefold(
            " ".join(
                value
                for value in (
                    dependency.ref_code,
                    dependency.source_ref,
                    dependency.title,
                    dependency.location_desc,
                )
                if value
            )
        )
        term_hits = 0
        for term in term_keys:
            if term in haystack:
                term_hits += 1
                signals.append(f"term:{term}")
        rank = (10 if source_station is not None and "station_containment" in signals else 0) + term_hits
        ranked.append(
            (
                rank,
                dependency.ref_code,
                dependency.id,
                DependencyShortlistSignal(dependency.id, rank, tuple(signals)),
            )
        )
    ranked.sort(key=lambda row: (-row[0], row[1], row[2]))
    return tuple(row[3] for row in ranked)


# --------------------------------------------------------------------------- #
# The exact tier — ADR-0054's evidence stack as deterministic filters.        #
# --------------------------------------------------------------------------- #


def match_statement_scope(
    dependencies: Iterable[Dependency],
    *,
    organization_id: int,
    wording: str,
    station_text: str | None = None,
    context: StatementMatchContext | None = None,
) -> StatementScopeMatch:
    """Apply the full evidence stack, retaining every deciding observation.

    Layer 1 is the sentence's own identifying language: explicit references,
    identifying row terms, and station containment.  Layer 2 is recorded
    context: row state, thread continuity, an answer to exactly one open
    recorded ask, promise chains, and the per-row registered contact.  All
    filters stack; ``exact`` is returned only for a sole survivor of the full
    stack.  With no identifying language and no deciding context the result
    is ``unknown`` and the existing unknown-scope path applies, unchanged.
    """
    source = _normal_phrase(wording)
    source_station = parse_station(station_text)
    context = context or StatementMatchContext()
    active = tuple(
        sorted(
            (
                dependency
                for dependency in dependencies
                if dependency.external_org_id == organization_id
                and dependency.dismissed_at is None
            ),
            key=lambda dependency: dependency.id,
        )
    )
    references = tuple(
        dependency
        for dependency in active
        if any(
            value and _normal_phrase(value) in source
            for value in (dependency.ref_code, dependency.source_ref)
        )
    )
    term_hits = {
        dependency.id: _dependency_terms_in_wording(dependency, source)
        for dependency in active
    }
    station_hits = {
        dependency.id
        for dependency in active
        if source_station is not None and _station_contains(dependency, source_station)
    }
    has_identifying_language = bool(
        references or station_hits or any(term_hits.values())
    )
    # Everything below is retained on the receipt; JSON-native values only so
    # a reloaded receipt compares equal to a freshly computed one.
    evidence: dict = {
        "matcher_version": MATCHER_VERSION,
        "matcher_fingerprint": matcher_fingerprint(),
        "evidence_versions": dict(MATCHER_EVIDENCE_VERSIONS),
        "explicit_reference_ids": [dependency.id for dependency in references],
        "station": station_text if source_station is not None else None,
        "station_dependency_ids": sorted(station_hits),
        "matched_terms": sorted(
            {term for terms in term_hits.values() for term in terms}
        ),
    }
    if not has_identifying_language and _context_is_empty(context):
        return StatementScopeMatch("unknown", (), evidence)

    # Each layer-1 fact narrows only when it has an exact row-side match.  A
    # stated station that fits no active row is an honest no-survivor, never
    # a reason to discard the station and fall back to a score.
    survivors = set(active)
    if references:
        survivors.intersection_update(references)
    if source_station is not None:
        survivors.intersection_update(
            dependency for dependency in active if dependency.id in station_hits
        )
    matched_term_dependencies = {
        dependency for dependency in active if term_hits[dependency.id]
    }
    if matched_term_dependencies:
        survivors.intersection_update(matched_term_dependencies)

    survivors = _apply_row_state(survivors, source, context, evidence)
    survivors = _apply_recorded_context(survivors, context, evidence)

    ids = tuple(sorted(dependency.id for dependency in survivors))
    if len(ids) == 1:
        return StatementScopeMatch("exact", ids, evidence)
    if not has_identifying_language:
        # Context existed but did not determine one row.  That is not a
        # narrowed set — the sentence itself named nothing — so the existing
        # unknown-scope path applies, unchanged.
        return StatementScopeMatch("unknown", (), evidence)
    if not ids:
        return StatementScopeMatch("none", (), evidence)
    # Several survivors: never auto-select.  The narrowed-set card carries
    # each candidate, the matched details, and the evidence already applied,
    # and offers "both/all listed" because a statement can legitimately cover
    # several rows (ADR-0035/0042: suggestions order but never select).
    card = {
        "candidate_dependency_ids": list(ids),
        "matched_details": evidence["matched_terms"],
        "evidence_applied": evidence,
        "choice_modes": ["each", "both_all_listed"],
    }
    return StatementScopeMatch("ambiguous", ids, evidence, card)


def _apply_row_state(
    survivors: set[Dependency],
    wording: str,
    context: StatementMatchContext,
    evidence: dict,
) -> set[Dependency]:
    """State filters candidates by what the promise's own verb can apply to.

    A relocation promise cannot match a protect-in-place row or a row whose
    Commitment has Completion Reported.  A verb without a defined
    applicability rule narrows nothing.
    """
    if not _is_relocation_promise(wording):
        return survivors
    closed = set(context.closed_dependency_ids)
    before = sorted(dependency.id for dependency in survivors)
    survivors = {
        dependency
        for dependency in survivors
        if dependency.resolution_strategy != "protect_in_place"
        and dependency.id not in closed
    }
    evidence["row_state"] = {
        "promise_verb": "relocate",
        "before_dependency_ids": before,
        "after_dependency_ids": sorted(d.id for d in survivors),
    }
    return survivors


def _apply_recorded_context(
    survivors: set[Dependency], context: StatementMatchContext, evidence: dict
) -> set[Dependency]:
    def narrow(name: str, ids: tuple[int, ...], *, only_if_one: bool = False) -> None:
        nonlocal survivors
        valid = sorted(set(ids))
        if not valid:
            return
        if only_if_one and len(valid) != 1:
            # Zero or several recorded asks/promises contribute nothing.
            return
        overlap = {dependency for dependency in survivors if dependency.id in valid}
        if overlap:
            survivors = overlap
            evidence[f"{name}_dependency_ids"] = valid

    # A reply resolves within its thread's established set first; a thread
    # with no recorded association (or none overlapping) contributes nothing.
    narrow("thread", context.thread_dependency_ids)
    narrow("open_ask", context.open_ask_dependency_ids, only_if_one=True)
    narrow("promise", context.promise_dependency_ids)
    contacts = {
        name: value.strip().lower()
        for name, value in (
            ("sender", context.sender),
            ("named_speaker", context.named_speaker),
        )
        if value and value.strip()
    }
    if contacts:
        matching = {
            dependency
            for dependency in survivors
            if any(
                _contact_matches(dependency.external_contact, value)
                for value in contacts.values()
            )
        }
        # Only an exactly-one registered-contact match narrows; a contact
        # registered on several surviving rows proves nothing about which.
        if len(matching) == 1:
            survivors = matching
            evidence["contacts"] = contacts
    return survivors


def _context_is_empty(context: StatementMatchContext) -> bool:
    return not (
        context.thread_dependency_ids
        or context.open_ask_dependency_ids
        or context.promise_dependency_ids
        or (context.sender or "").strip()
        or (context.named_speaker or "").strip()
    )


def _dependency_terms_in_wording(
    dependency: Dependency, wording: str
) -> tuple[str, ...]:
    values = (dependency.title, dependency.location_desc, dependency.source_ref)
    return tuple(
        sorted(
            {
                value
                for value in (_normal_phrase(value) for value in values)
                if value and len(value) >= 3 and value in wording
            }
        )
    )


def _casefold(value: object) -> str:
    """The assistant scorer's original normalization, preserved verbatim."""
    return " ".join(str(value or "").casefold().split())


def _normal_phrase(value: str | None) -> str:
    return " ".join(normalize((value or "").replace("-", " ")).split())


def _station_contains(dependency: Dependency, station: float) -> bool:
    values = [
        parsed
        for parsed in (
            parse_station(dependency.station_from),
            parse_station(dependency.station_to),
        )
        if parsed is not None
    ]
    return bool(values) and min(values) <= station <= max(values)


def _is_relocation_promise(wording: str) -> bool:
    return any(token in _RELOCATION_VERBS for token in wording.split())


def _contact_matches(contact: str | None, person: str) -> bool:
    contact_normalized = normalize(contact or "")
    person_normalized = normalize(person)
    return bool(person_normalized) and person_normalized in contact_normalized
