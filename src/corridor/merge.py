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
from collections.abc import Collection
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.models import Dependency, EvidenceLink, ExternalOrg

# Two records within this distance are plausibly the same facility. Roughly a
# city block: closer than the spacing between distinct utility crossings, wide
# enough to absorb the disagreement between a matrix and a field note.
STATION_TOLERANCE_FT = 500.0

# Relative weights. Station dominates when present; text decides when it is
# not, which is the common case for minutes and email.
WEIGHTS = {"station": 3.0, "source_ref": 2.0, "type": 1.0, "text": 1.5}

# Below this, a suggestion is noise. Offering five weak matches invites a
# reviewer under time pressure to pick one, and merging a non-duplicate
# corrupts the ledger exactly as badly as accepting a duplicate.
MIN_MATCH_SCORE = 0.5

_STATION = re.compile(r"(\d{1,5})\s*\+\s*(\d{1,2}(?:\.\d+)?)")
_SPLIT = re.compile(r"\d\s*$")
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
    # Set when stationing positively rules the pair out, as opposed to
    # merely failing to support it.
    excluded_by: str | None = None


def parse_station(value: str | None) -> float | None:
    """`1149+00` -> 114900.0 feet along the alignment.

    Returns None rather than 0.0 for anything unparseable: zero is a real
    station, and guessing it would silently place a record at the origin.
    """
    if not value:
        return None
    text = str(value)
    match = _STATION.search(text)
    if not match:
        return None
    # A digit immediately before the match means the station number itself
    # was broken and this is only its tail: `1 109+59` would read as
    # `109+59`, placing the record 100,000 feet away — a plausible station,
    # so wrong silently. A `STA ` prefix is not that, hence digit not text.
    if _SPLIT.search(text[: match.start()]):
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


def score_match(
    fields: dict,
    dependency: Dependency,
    *,
    source_document_id: int | None = None,
    evidence_documents: Collection[int] = (),
) -> Match:
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

    # Positive evidence when it matches, and nothing when it does not.
    # TxDOT renumbers between revisions — 54 of the 73 ids that vanish
    # between the last two Project A revisions reappear as the same party at
    # the same station under a new id — so a differing id ranks a match down
    # but must never exclude it. And ids are not unique within a revision
    # either (47 reused in 7-22-2025), so equality is evidence, not proof.
    candidate_ref = _normalize(fields.get("utility_id"))
    if candidate_ref and dependency.source_ref:
        hit = candidate_ref == _normalize(dependency.source_ref)
        signals.append(
            Signal(
                "source_ref",
                1.0 if hit else 0.0,
                WEIGHTS["source_ref"],
                f"{fields.get('utility_id')} vs {dependency.source_ref}",
            )
        )

    candidate_type = _normalize(fields.get("utility_type") or fields.get("dep_type"))
    if candidate_type:
        target = f"{_normalize(dependency.title)} {_normalize(dependency.dep_type)}"
        hit = candidate_type in target
        signals.append(
            Signal("type", 1.0 if hit else 0.0, WEIGHTS["type"], candidate_type)
        )

    # Read what a matrix row actually carries. This previously read `title`
    # and `location_desc`, which the matrix extractor never emits, so the
    # candidate side collapsed to "<type> <party>" — identical for every row
    # of a party, and so a constant wearing the costume of a comparison.
    candidate_text = _normalize(
        " ".join(
            str(fields.get(k) or "")
            for k in (
                "utility_type",
                "external_org",
                "location_start",
                "location_end",
                "alignment",
            )
        )
    )
    target_text = _normalize(f"{dependency.title} {dependency.location_desc or ''}")
    if candidate_text and target_text:
        ratio = SequenceMatcher(None, candidate_text, target_text).ratio()
        signals.append(Signal("text", ratio, WEIGHTS["text"], f"{ratio:.2f}"))

    weight = sum(s.weight for s in signals) or 1.0
    total = sum(s.score * s.weight for s in signals) / weight

    # Stationing is a discriminator, not just a positive signal. Two records
    # for the same utility more than the tolerance apart are two different
    # facilities — that is affirmative evidence they are not the same, and no
    # amount of matching text should outweigh it.
    excluded = "stationing places these apart" if station == 0.0 else None

    # Provenance is the stronger discriminator, and the one the scores cannot
    # reach. A utility matrix lists each facility once, so two rows of one
    # matrix are two facilities however alike they score — parallel runs
    # along a corridor share a party, a type and a station range by nature.
    # If this dependency is already evidenced by the document the candidate
    # came from, they are siblings, not the same record seen twice.
    if source_document_id is not None and source_document_id in evidence_documents:
        excluded = "already cites this document"

    return Match(
        dependency=dependency, total=total, signals=signals, excluded_by=excluded
    )


def rank_matches(
    session: Session,
    project_id: int,
    fields: dict,
    limit: int = 10,
    *,
    source_document_id: int | None = None,
) -> list[Match]:
    """Candidates for merge, best first.

    Blocking is on the resolved party and is a hard filter, not a signal: a
    different owner is not a weak match, it is not a match.

    `source_document_id` is the document the candidate was extracted from.
    Passing it excludes the dependencies already evidenced by that same
    document, which is what keeps sibling rows of one matrix apart.
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

    # One query for the whole block rather than one per dependency.
    evidence: dict[int, set[int]] = {}
    if source_document_id is not None and dependencies:
        for dep_id, doc_id in session.execute(
            select(EvidenceLink.dependency_id, EvidenceLink.document_id).where(
                EvidenceLink.dependency_id.in_([d.id for d in dependencies])
            )
        ):
            evidence.setdefault(dep_id, set()).add(doc_id)

    matches = [
        score_match(
            fields,
            d,
            source_document_id=source_document_id,
            evidence_documents=evidence.get(d.id, ()),
        )
        for d in dependencies
    ]
    matches = [
        m for m in matches if m.excluded_by is None and m.total >= MIN_MATCH_SCORE
    ]
    matches.sort(key=lambda m: m.total, reverse=True)
    return matches[:limit]
