"""Merge ranking: which existing Dependency is this candidate already?

The spec calls merging the core interaction, and it is the one place where a
wrong answer is unrecoverable — accepting a duplicate instead of merging
corrupts the ledger silently, while a missed extraction is only a gap.

Two properties are deliberate:

**Deterministic and explainable, not embeddings.** Every signal reports its
own contribution, so when the top suggestion is wrong you can see which
signal misled you and fix that rule. An embedding that ranks badly just
ranks badly.

**Stationing carries the weight, because it is numeric.** `245+00` and
`445+00` differ by one character as text and by two thousand feet on the
ground. Text similarity actively destroys that distinction; a subtraction
preserves it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.models import Dependency, ExternalOrg

# Two records within this distance are plausibly the same facility. Roughly a
# city block: closer than the spacing between distinct utility crossings, wide
# enough to absorb the disagreement between a matrix and a field note.
STATION_TOLERANCE_FT = 500.0

# Relative weights. Station dominates when present; text decides when it is
# not, which is the common case for minutes and email.
WEIGHTS = {"station": 3.0, "type": 1.0, "text": 1.5}

_STATION = re.compile(r"(\d{1,5})\s*\+\s*(\d{1,2}(?:\.\d+)?)")
_WS = re.compile(r"\s+")


@dataclass
class Signal:
    name: str
    score: float
    weight: float
    detail: str = ""


@dataclass
class Match:
    dependency: Dependency
    total: float
    signals: list[Signal] = field(default_factory=list)


def parse_station(value: str | None) -> float | None:
    """`1149+00` -> 114900.0 feet along the alignment.

    Returns None rather than 0.0 for anything unparseable: zero is a real
    station, and guessing it would silently place a record at the origin.
    """
    if not value:
        return None
    match = _STATION.search(str(value))
    if not match:
        return None
    return float(match.group(1)) * 100 + float(match.group(2))


def station_score(
    a_from: str | None, a_to: str | None, b_from: str | None, b_to: str | None
) -> float | None:
    """1.0 identical, 0.0 far apart, None when either side has no stationing.

    None is not zero. Absence of stationing is no evidence, not contrary
    evidence — scoring it zero would rank every minutes-derived candidate
    below every matrix-derived one regardless of how well they otherwise
    match.
    """
    a1, a2 = parse_station(a_from), parse_station(a_to)
    b1, b2 = parse_station(b_from), parse_station(b_to)
    if a1 is None and a2 is None:
        return None
    if b1 is None and b2 is None:
        return None

    a_lo, a_hi = _span(a1, a2)
    b_lo, b_hi = _span(b1, b2)

    overlap = min(a_hi, b_hi) - max(a_lo, b_lo)
    if overlap >= 0:
        shorter = min(a_hi - a_lo, b_hi - b_lo)
        if shorter <= 0:
            # A point inside a range: containment is a strong signal.
            return 1.0
        return min(1.0, overlap / shorter)

    gap = -overlap
    if gap >= STATION_TOLERANCE_FT:
        return 0.0
    return 1.0 - (gap / STATION_TOLERANCE_FT)


def _span(lo: float | None, hi: float | None) -> tuple[float, float]:
    values = [v for v in (lo, hi) if v is not None]
    return min(values), max(values)


def _normalize(text: str | None) -> str:
    return _WS.sub(" ", (text or "").strip()).casefold()


def resolve_org(session: Session, name: str | None) -> ExternalOrg | None:
    """One party is named many ways; blocking must collapse them first."""
    target = _normalize(name)
    if not target:
        return None
    for org in session.scalars(select(ExternalOrg)):
        if _normalize(org.name) == target:
            return org
        if any(_normalize(alias) == target for alias in (org.aliases or [])):
            return org
    return None


def score_match(fields: dict, dependency: Dependency) -> Match:
    signals: list[Signal] = []

    station = station_score(
        fields.get("station_from"),
        fields.get("station_to"),
        dependency.station_from,
        dependency.station_to,
    )
    if station is not None:
        signals.append(
            Signal(
                "station",
                station,
                WEIGHTS["station"],
                f"{fields.get('station_from') or '—'}→{fields.get('station_to') or '—'} "
                f"vs {dependency.station_from or '—'}→{dependency.station_to or '—'}",
            )
        )

    candidate_type = _normalize(fields.get("utility_type") or fields.get("dep_type"))
    if candidate_type:
        target = f"{_normalize(dependency.title)} {_normalize(dependency.dep_type)}"
        hit = candidate_type in target
        signals.append(
            Signal("type", 1.0 if hit else 0.0, WEIGHTS["type"], candidate_type)
        )

    candidate_text = _normalize(
        " ".join(
            str(fields.get(k) or "")
            for k in ("title", "utility_type", "external_org", "location_desc")
        )
    )
    target_text = _normalize(f"{dependency.title} {dependency.location_desc or ''}")
    if candidate_text and target_text:
        ratio = SequenceMatcher(None, candidate_text, target_text).ratio()
        signals.append(Signal("text", ratio, WEIGHTS["text"], f"{ratio:.2f}"))

    weight = sum(s.weight for s in signals) or 1.0
    total = sum(s.score * s.weight for s in signals) / weight
    return Match(dependency=dependency, total=total, signals=signals)


def rank_matches(
    session: Session, project_id: int, fields: dict, limit: int = 10
) -> list[Match]:
    """Candidates for merge, best first.

    Blocking is on the resolved party and is a hard filter, not a signal: a
    different owner is not a weak match, it is not a match.
    """
    org = resolve_org(session, fields.get("external_org"))
    if org is None:
        return []

    dependencies = session.scalars(
        select(Dependency).where(
            Dependency.project_id == project_id,
            Dependency.external_org_id == org.id,
        )
    ).all()

    matches = [score_match(fields, d) for d in dependencies]
    matches.sort(key=lambda m: m.total, reverse=True)
    return matches[:limit]
