"""Match a statement to Constraints only when its retained facts say one row.

The statement-review assistant originally held a small station-and-term scorer
privately.  That was useful for ordering a person's choices, but could never
be a Record Inclusion predicate: a high score is still a guess.  ADR-0054
allows this module to share the observations while separating their two uses.

``shortlist_dependencies`` ranks deterministic observations for a reader.
``match_statement_scope`` treats the same observations as filters and returns
an exact result *only* when one active Constraint survives every applicable
filter.  Context is supplied by the owning intake/coordination path as
recorded associations; source words are never commands and no model output is
an authority-shaped input.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from typing import Iterable

from corridor.merge import parse_station
from corridor.models import Dependency
from corridor.verify import normalize


MATCHER_VERSION = "statement-identifying-language-v1"
# Changing any individual evidence contract changes the rule fingerprint.  The
# mapping is deliberately data, rather than an incidental list in a digest, so
# a later evidence kind cannot silently pool its readings with this one.
MATCHER_EVIDENCE_VERSIONS = {
    "explicit_reference": "v1",
    "identifying_terms": "v1",
    "station": "v1",
    "thread": "v1",
    "recorded_ask": "v1",
    "row_state": "v1",
    "promise_chain": "v1",
    "row_contact": "v1",
}


@dataclass(frozen=True)
class StatementMatchContext:
    """Already-recorded context the caller may apply as an exact filter.

    The IDs are not extracted guesses.  They are associations owned by the
    email/thread, follow-up, or commitment services.  An empty or plural ask
    deliberately contributes nothing; it is not an instruction to select one.
    """

    thread_dependency_ids: tuple[int, ...] = ()
    open_ask_dependency_ids: tuple[int, ...] = ()
    promise_dependency_ids: tuple[int, ...] = ()
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
        f"{name}:{version}" for name, version in sorted(MATCHER_EVIDENCE_VERSIONS.items())
    )
    return sha256(f"{MATCHER_VERSION}|{material}".encode()).hexdigest()


def shortlist_dependencies(
    dependencies: Iterable[Dependency], *, station_text: str | None, terms: tuple[str, ...]
) -> tuple[DependencyShortlistSignal, ...]:
    """Rank the assistant's bounded options from shared, non-decisive signals."""
    source_station = parse_station(station_text)
    wanted = tuple(_normalized_terms(terms))
    ranked: list[DependencyShortlistSignal] = []
    for dependency in dependencies:
        signals = _signals_for(dependency, source_station=source_station, terms=wanted)
        # Ranking remains presentation-only.  Exact admission below never
        # consults this number or a threshold.
        rank = (10 if "station_containment" in signals else 0) + sum(
            signal.startswith("term:") for signal in signals
        )
        ranked.append(DependencyShortlistSignal(dependency.id, rank, signals))
    return tuple(sorted(ranked, key=lambda item: (-item.rank, item.dependency_id)))


def match_statement_scope(
    dependencies: Iterable[Dependency],
    *,
    organization_id: int,
    wording: str,
    station_text: str | None = None,
    context: StatementMatchContext | None = None,
) -> StatementScopeMatch:
    """Apply ADR-0054's exact stack, retaining every deciding observation."""
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
            value and normalize(value) in source
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
    has_identifying_language = bool(references or station_hits or any(term_hits.values()))
    evidence: dict = {
        "matcher_version": MATCHER_VERSION,
        "matcher_fingerprint": matcher_fingerprint(),
        "evidence_versions": dict(MATCHER_EVIDENCE_VERSIONS),
        "explicit_reference_ids": tuple(dependency.id for dependency in references),
        "station": station_text if source_station is not None else None,
        "station_dependency_ids": tuple(sorted(station_hits)),
        "matched_terms": tuple(
            sorted({term for terms in term_hits.values() for term in terms})
        ),
    }
    if not has_identifying_language:
        return StatementScopeMatch("unknown", (), evidence)

    # Each Layer 1 fact narrows only when it has an exact row-side match.  A
    # stated station that fits no active row is an honest no-survivor, never a
    # reason to discard the station and fall through to a score.
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

    # A relocation promise cannot apply to a protect-in-place row.  Dismissed
    # rows were excluded before any matcher work; they are no longer active.
    if _is_relocation_promise(source):
        before = tuple(sorted(dependency.id for dependency in survivors))
        survivors = {
            dependency
            for dependency in survivors
            if dependency.resolution_strategy != "protect_in_place"
        }
        evidence["row_state"] = {
            "promise_verb": "relocate",
            "before_dependency_ids": before,
            "after_dependency_ids": tuple(sorted(d.id for d in survivors)),
        }

    survivors = _apply_recorded_context(survivors, context, evidence)
    ids = tuple(sorted(dependency.id for dependency in survivors))
    if len(ids) == 1:
        return StatementScopeMatch("exact", ids, evidence)
    if not ids:
        return StatementScopeMatch("none", (), evidence)
    card = {
        "candidate_dependency_ids": ids,
        "matched_details": evidence["matched_terms"],
        "evidence_applied": evidence,
        "choice_modes": ("each", "both_all_listed"),
    }
    return StatementScopeMatch("ambiguous", ids, evidence, card)


def _apply_recorded_context(
    survivors: set[Dependency], context: StatementMatchContext, evidence: dict
) -> set[Dependency]:
    def narrow(name: str, ids: tuple[int, ...], *, only_if_one: bool = False) -> None:
        nonlocal survivors
        valid = tuple(sorted(set(ids)))
        if only_if_one and len(valid) != 1:
            return
        overlap = {dependency for dependency in survivors if dependency.id in valid}
        if overlap:
            survivors = overlap
            evidence[f"{name}_dependency_ids"] = valid

    narrow("thread", context.thread_dependency_ids)
    narrow("open_ask", context.open_ask_dependency_ids, only_if_one=True)
    narrow("promise", context.promise_dependency_ids, only_if_one=True)
    contacts = {
        name: value.strip().lower()
        for name, value in (("sender", context.sender), ("named_speaker", context.named_speaker))
        if value and value.strip()
    }
    matching = {
        dependency
        for dependency in survivors
        if any(_contact_matches(dependency.external_contact, value) for value in contacts.values())
    }
    if len(matching) == 1:
        survivors = matching
        evidence["contacts"] = contacts
    return survivors


def _signals_for(
    dependency: Dependency, *, source_station: float | None, terms: tuple[str, ...]
) -> tuple[str, ...]:
    signals = ["registered_party_match"]
    if source_station is not None and _station_contains(dependency, source_station):
        signals.append("station_containment")
    haystack = _haystack(dependency)
    signals.extend(f"term:{term}" for term in terms if term in haystack)
    return tuple(signals)


def _dependency_terms_in_wording(dependency: Dependency, wording: str) -> tuple[str, ...]:
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


def _normal_phrase(value: str | None) -> str:
    return " ".join(normalize((value or "").replace("-", " ")).split())


def _normalized_terms(terms: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(term for term in (_normal_phrase(term) for term in terms) if term)


def _haystack(dependency: Dependency) -> str:
    return _normal_phrase(
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


def _station_contains(dependency: Dependency, station: float) -> bool:
    values = [
        parsed
        for parsed in (parse_station(dependency.station_from), parse_station(dependency.station_to))
        if parsed is not None
    ]
    return bool(values) and min(values) <= station <= max(values)


def _is_relocation_promise(wording: str) -> bool:
    return any(token in wording.split() for token in ("relocate", "relocated", "relocating"))


def _contact_matches(contact: str | None, person: str) -> bool:
    contact_normalized = normalize(contact or "")
    person_normalized = normalize(person)
    return bool(person_normalized) and person_normalized in contact_normalized
