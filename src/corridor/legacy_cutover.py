"""Evaluate a declared bounded legacy cutover without changing live traffic.

Writer routing, shadow observations and retirement are different decisions.
This build-only evaluator makes missing cohort coverage, a breached divergence
threshold and incomplete backup disposition explicit. A passing plan is input
to the named operator's command, never permission for this module to write.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from corridor.reader_coverage import CONTRACTS, CoverageResult


@dataclass(frozen=True)
class CutoverWindow:
    starts_at: datetime
    ends_at: datetime
    cohort: frozenset[str]
    traffic_owner: str
    decision_authority: str
    max_observation_gap: timedelta
    divergence_threshold: int = 0

    def __post_init__(self):
        if (self.starts_at.tzinfo is None or self.ends_at.tzinfo is None
            or self.ends_at <= self.starts_at or not self.cohort
            or any(not customer.strip() for customer in self.cohort)
            or not self.traffic_owner.strip() or not self.decision_authority.strip()
            or self.max_observation_gap <= timedelta(0)
            or self.max_observation_gap > self.ends_at - self.starts_at
            or self.divergence_threshold != 0):
            raise ValueError("cutover requires a bounded window, cohort, named owners, sampling budget and zero semantic divergence")


@dataclass(frozen=True)
class ShadowObservation:
    customer: str
    observed_at: datetime
    divergences: int
    comparison_digest: str


@dataclass(frozen=True)
class RetirementDisposition:
    decision_by: str
    window_decision: str
    compatibility_writes_removed: bool
    history_readable: bool
    hold_active: bool
    backup_expiry: datetime
    backup_expiry_verified: bool
    disposition_receipt: str


@dataclass(frozen=True)
class CutoverAssessment:
    switch_ready: bool
    rollback_required: bool
    retirement_ready: bool
    blockers: tuple[str, ...]


def assess_cutover(window: CutoverWindow, *, at: datetime,
                   equivalence: tuple[CoverageResult, ...],
                   observations: tuple[ShadowObservation, ...],
                   disposition: RetirementDisposition | None = None) -> CutoverAssessment:
    """Require observed full-window cohort coverage and retained-source custody."""
    if at.tzinfo is None:
        raise ValueError("cutover evaluation time must be timezone-aware")
    blockers = []
    coverage_complete = (len(equivalence) == len(CONTRACTS)
        and {row.surface for row in equivalence} == {item.name for item in CONTRACTS}
        and all(row.passed for row in equivalence))
    if not coverage_complete:
        blockers.append("reader field and record coverage has not passed on all seven surfaces")
    if at < window.starts_at:
        blockers.append("rollback window has not started")
    valid = []
    seen = set()
    for item in observations:
        identity = (item.customer, item.observed_at)
        if (item.customer not in window.cohort or item.observed_at.tzinfo is None
            or not window.starts_at <= item.observed_at <= min(at, window.ends_at)
            or item.divergences < 0 or len(item.comparison_digest) != 64
            or any(char not in "0123456789abcdef" for char in item.comparison_digest)
            or identity in seen):
            blockers.append("shadow observation is outside its declared cohort/window, duplicated or lacks a comparison digest")
            continue
        seen.add(identity)
        valid.append(item)
    breached = any(item.divergences > window.divergence_threshold for item in valid)
    if breached:
        blockers.append("observed semantic divergence requires rollback by the named authority")
    complete_window = at >= window.ends_at
    for customer in sorted(window.cohort):
        times = sorted(item.observed_at for item in valid if item.customer == customer)
        boundary = min(max(at, window.starts_at), window.ends_at)
        if not times or any(right-left > window.max_observation_gap
            for left,right in zip([window.starts_at,*times], [*times,boundary])):
            blockers.append(f"shadow comparison coverage is missing or exceeds the allowed gap for {customer}")
            complete_window = False
    shadow_valid = not blockers
    switch_ready = shadow_valid and window.starts_at <= at < window.ends_at
    retirement_ready = shadow_valid and complete_window
    if retirement_ready:
        if (disposition is None or disposition.decision_by != window.decision_authority
            or disposition.window_decision != "retire" or not disposition.compatibility_writes_removed
            or not disposition.history_readable or disposition.hold_active
            or disposition.backup_expiry.tzinfo is None or disposition.backup_expiry > at
            or not disposition.backup_expiry_verified or not disposition.disposition_receipt.strip()):
            blockers.append("retirement needs the named decision, removed compatibility writes, readable history, no hold and verified backup disposition")
            retirement_ready = False
    elif at < window.ends_at:
        blockers.append("bounded rollback window remains open")
    return CutoverAssessment(switch_ready, breached, retirement_ready, tuple(blockers))
