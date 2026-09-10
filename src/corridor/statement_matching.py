"""Deterministic statement-to-Constraint matching signals, in one place.

The Evidence Investigator's private shortlist tool and the ordinary guided
statement screen both need the same explainable ordering signals: registered
party, station containment, and verbatim term presence over a Constraint's
own row text.  Duplicating that arithmetic would let the two orderings drift
apart silently.  This module owns the pure signal computation; callers own
party resolution, term selection, protection checks, and presentation.

``statement_matcher.shortlist_dependencies`` had kept a verbatim copy of the
normalizer, the station test, the row-text haystack and the rank weight, and
that copy is what the Evidence Investigator actually ran; the two orderings
were one edit away from disagreeing.  Both now call these functions.

ADR-0054 dissolves these signals into the identifying-language matcher
(#370); when that matcher absorbs the seam it takes one implementation.
Nothing here decides scope: signals order a list and never select a party, a
Constraint, or an Applies To answer (ADR-0035, ADR-0042).
"""

from __future__ import annotations

from corridor.merge import parse_station
from corridor.models import Dependency


def normalize_match_text(value: object) -> str:
    """Casefold and collapse whitespace exactly as the investigator scorer does."""
    return " ".join(str(value or "").casefold().split())


def station_contains(source: float, low: float | None, high: float | None) -> bool:
    """True when a parsed statement station falls inside a Constraint's range."""
    if low is None and high is None:
        return False
    if low is None:
        low = high
    if high is None:
        high = low
    assert low is not None and high is not None
    return min(low, high) <= source <= max(low, high)


def dependency_station_contains(dependency: Dependency, station: float) -> bool:
    """True when a parsed statement station falls inside this Constraint's range."""
    return station_contains(
        station,
        parse_station(dependency.station_from),
        parse_station(dependency.station_to),
    )


def dependency_haystack(dependency: Dependency) -> str:
    """The Constraint's own row text, normalized, that a verbatim term is sought in."""
    return normalize_match_text(
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


def dependency_match_signals(
    dependency: Dependency,
    *,
    source_stations: tuple[float, ...],
    term_keys: tuple[str, ...],
) -> tuple[tuple[str, ...], int]:
    """Name the deterministic signals one active same-party Constraint earns.

    Returns the ordered signal names and the verbatim term-hit count.  The
    caller has already restricted candidates to the statement's registered
    party inside one project, so ``registered_party_match`` is always first.
    """
    signals = ["registered_party_match"]
    if any(
        dependency_station_contains(dependency, source) for source in source_stations
    ):
        signals.append("station_overlap")
    haystack = dependency_haystack(dependency)
    term_hits = sum(term in haystack for term in term_keys)
    if term_hits:
        signals.append("source_term_match")
    return tuple(signals), term_hits


STATION_CONTAINMENT_WEIGHT = 10


def match_score(*, station_containment: bool, term_hits: int) -> int:
    """The one rank weight: station containment first, then verbatim term hits.

    Named arguments rather than a signal tuple, because the two callers spell
    the station signal differently on their own receipts — ``station_overlap``
    on a suggestion, ``station_containment`` on a shortlist — and the weight
    must not depend on which spelling reaches it.
    """
    return (STATION_CONTAINMENT_WEIGHT if station_containment else 0) + term_hits
