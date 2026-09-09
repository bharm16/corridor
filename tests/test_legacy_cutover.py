"""A quiet sample or elapsed clock alone cannot close the rollback window."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from corridor.legacy_cutover import CutoverWindow, ShadowObservation, RetirementDisposition, assess_cutover
from corridor.reader_coverage import CONTRACTS, CoverageResult

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = START + timedelta(days=2)
WINDOW = CutoverWindow(START, END, frozenset({"customer-a", "customer-b"}),
                       "local:traffic-owner", "local:decision-owner", timedelta(days=1))
EQUIVALENCE = tuple(CoverageResult(item.name, True, 1, ()) for item in CONTRACTS)


def _observations():
    return tuple(ShadowObservation(customer, time, 0, "a" * 64)
        for customer in sorted(WINDOW.cohort) for time in (START, START+timedelta(days=1), END))


def test_missing_customer_and_divergence_defeat_the_elapsed_window():
    observations = _observations()
    incomplete = assess_cutover(WINDOW, at=END, equivalence=EQUIVALENCE,
        observations=tuple(item for item in observations if item.customer == "customer-a"))
    assert not incomplete.retirement_ready
    assert any("customer-b" in reason for reason in incomplete.blockers)
    breached = assess_cutover(WINDOW, at=END, equivalence=EQUIVALENCE,
        observations=(replace(observations[0], divergences=1), *observations[1:]))
    assert breached.rollback_required and not breached.retirement_ready


def test_retirement_needs_named_decision_and_verified_expired_backups():
    disposition = RetirementDisposition("local:decision-owner", "retire", True, True,
                                         False, END, True, "control-plane:receipt-1")
    ready = assess_cutover(WINDOW, at=END, equivalence=EQUIVALENCE,
        observations=_observations(), disposition=disposition)
    assert ready.retirement_ready and not ready.switch_ready
    for changed in (replace(disposition, decision_by="local:other"),
                    replace(disposition, hold_active=True),
                    replace(disposition, backup_expiry_verified=False),
                    replace(disposition, backup_expiry=END+timedelta(days=1))):
        assert not assess_cutover(WINDOW, at=END, equivalence=EQUIVALENCE,
            observations=_observations(), disposition=changed).retirement_ready
    assert not assess_cutover(WINDOW, at=END, equivalence=EQUIVALENCE[:-1],
        observations=_observations(), disposition=disposition).retirement_ready
