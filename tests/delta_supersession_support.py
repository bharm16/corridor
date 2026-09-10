"""Insert a delta supersession row directly, for tests that need one to read.

``delta_supersessions`` has no writer in the product.  The Proposed Delta
lifecycle appends occurrences and the record-decision role's commands resolve
and defer them; nothing grants the runtime logins ``insert`` on this table
(``b2d5f8a1c4e7``), so the raw ORM insert that used to live in
``corridor.proposed_deltas`` could never have run in production.  It was a
scenario fixture wearing production clothes.

Tests still need superseded deltas, because the readers under test
(``delta_resolution.live_delta_status``,
``review_packet_reading.current_deltas`` and everything built on it) must
place one correctly.  Those tests run as the schema owner, so the insert
works here.  It lives with the tests that need it and is not importable from
``corridor``.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from corridor.models import DeltaSupersession


def record_delta_supersession(
    session: Session,
    *,
    project_id: int,
    prior_delta_id: int,
    superseding_delta_id: int,
    reason: str = "newer_source_revision",
) -> DeltaSupersession:
    """Link an old delta to a newer superseding delta in the same source lineage."""

    row = DeltaSupersession(
        project_id=project_id,
        prior_delta_id=prior_delta_id,
        superseding_delta_id=superseding_delta_id,
        reason=reason,
    )
    session.add(row)
    session.flush()
    return row
