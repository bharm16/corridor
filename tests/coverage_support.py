"""One confirmed coverage declaration, for the tests that prepare a candidate.

#675 made the coverage state a candidate is prepared under a persisted,
confirmed ``issue_coverage_declarations`` row rather than a value a caller
composes, so every test that prepares a candidate needs one to exist. This
builds it through the real ``confirm_coverage`` seam — the digest of the
reading handed in is the digest that is confirmed — so the fixture cannot
record a confirmation the module would refuse.

The *reading* is synthesised rather than derived, and deliberately: these
fixtures are about what a candidate binds, and a reading derived from whatever
documents a fixture happened to create would make every candidate assertion
depend on the fixture's corpus. ``tests/test_issue_coverage.py`` is where the
derivation itself is proved, against real deliveries and real receipts.

``variant`` changes the source keys inside the digested reading without
changing a single word any artifact renders, which is exactly what a test
needs when it wants a second, differently identified declaration for a second
week's candidate.

Nothing here reads a clock.
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from sqlalchemy.orm import Session

from corridor.issue_coverage import (
    CoverageLine,
    DerivedCoverageReading,
    confirm_coverage,
)
from corridor.issue_profile import effective_issue_inventory
from corridor.issue_rendering import SourceCoverage
from corridor.models import IssueCoverageDeclaration, Project
from corridor.principals import HumanPrincipal


DEFAULT_LINES: tuple[SourceCoverage, ...] = (
    SourceCoverage(
        source_name="Weekly utility conflict matrix",
        requirement="required",
        state="read",
        detail="the 2026-03-01 revision was read in full",
    ),
)


def coverage_reading(
    session: Session,
    project: Project,
    *,
    cutoff: datetime,
    lines: Sequence[SourceCoverage] = DEFAULT_LINES,
    variant: str = "week",
    through_source_delivery_id: int | None = None,
) -> DerivedCoverageReading:
    """The reading a fixture confirms, over this project's effective profile."""

    inventory = effective_issue_inventory(session, project.id, cutoff)
    assert inventory is not None, (
        "a coverage reading needs an issue profile in force at the cutoff; "
        "register one before declaring coverage"
    )
    return DerivedCoverageReading(
        project_id=int(project.id),
        cutoff=cutoff,
        through_source_delivery_id=through_source_delivery_id,
        profile_id=int(inventory.profile_id),
        profile_identity=inventory.profile_identity,
        profile_version=int(inventory.profile_version),
        profile_sha256=inventory.content_sha256,
        requires_every_source_read=bool(inventory.coverage_requirements),
        lines=tuple(
            CoverageLine(
                source_key=f"document:{variant}:{ordinal}",
                source_name=line.source_name,
                requirement=line.requirement,
                state=line.state,
                detail=line.detail,
            )
            for ordinal, line in enumerate(lines, start=1)
        ),
    )


def declare_coverage(
    session: Session,
    project: Project,
    *,
    cutoff: datetime,
    principal: HumanPrincipal,
    confirmed_at: datetime,
    lines: Sequence[SourceCoverage] = DEFAULT_LINES,
    variant: str = "week",
    through_source_delivery_id: int | None = None,
) -> IssueCoverageDeclaration:
    """Confirm one coverage reading and return the row a candidate names."""

    reading = coverage_reading(
        session,
        project,
        cutoff=cutoff,
        lines=lines,
        variant=variant,
        through_source_delivery_id=through_source_delivery_id,
    )
    return confirm_coverage(
        session,
        project_id=int(project.id),
        reading=reading,
        confirmed_reading_digest=reading.reading_digest,
        principal=principal,
        confirmed_at=confirmed_at,
        idempotency_key=f"coverage:{reading.reading_digest}",
    )
