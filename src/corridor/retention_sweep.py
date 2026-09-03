"""Run the Class B intermediary TTL as recurring work, not an operator command (#488).

ADR-0080 keeps ADR-0072's automatic expiry for Class B intermediaries: they
"may still expire automatically under labeled product-policy TTLs, with the dry
-run manifest and reachability check". ``retention`` implements that boundary,
but the only thing that ever called it was ``retention_cli``, so intermediary
content expired when somebody remembered to run a command. This module is the
recurring pass; the CLI stays as the operator's recovery entry point over the
same two functions.

Nothing here weakens the boundary. The pass plans and executes the same
manifest a person would: the hold check, the reachability check, and the digest
recheck all run again at execution, and file-backed artifacts still go through
the one ``DeletionPermit`` issuer. Attribution is not invented either — the
schedule declares the person who authorized the standing sweep, and that
principal is the one recorded on every manifest it creates, so a deletion
receipt still names a human with authority under the customer relationship.

A refusal is an outcome, not a crash. When a hold, an open reference, or drift
makes a candidate undeletable, the pass deletes nothing and returns an
attention reading, so the refusal lands in the ordinary receipt family and is
visible in the operations view instead of burning three retries. A pass with
nothing due writes no manifest at all: an empty dry run every week would be a
weekly row saying nothing happened.

This module owns no schedule, timer, or clock. The one supervised Due Work
runtime (#332) discovers, claims, and retries occurrences.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select

from corridor.models import DueWorkSchedule, RetentionManifestItem
from corridor.principals import HumanPrincipal
from corridor.retention import RetentionRefused, execute_retention, plan_retention

# The one server-owned handler key this module's work runs under.  It matches
# ``due_work.HANDLER_RETENTION_SWEEP``; the constant lives here because this
# module is the lower layer and the runtime imports its execution, never the
# reverse.
HANDLER_KEY = "retention_sweep"

_RESULT_SCHEMA_VERSION = "retention-sweep-result-v1"


class RetentionSweepRefusal(ValueError):
    """A retention sweep cannot read its scope safely."""


def execute_retention_sweep(
    session_factory, *, schedule_id: int, clock
) -> dict[str, Any]:
    """Plan and execute one project's Class B expiry, or decline and say why."""

    as_of = _aware_utc(clock.now())
    with session_factory() as reading:
        schedule = reading.get(DueWorkSchedule, schedule_id)
        if schedule is None:
            raise RetentionSweepRefusal("retention-sweep schedule disappeared")
        project_id = schedule.project_id
        configuration_version = schedule.configuration_version
        authorized_by = str(schedule.scope_json.get("authorized_by", ""))

    principal = HumanPrincipal(authorized_by)
    planned = 0
    deleted = 0
    manifest_public_id = ""
    refusal = ""
    with session_factory() as working:
        try:
            manifest = plan_retention(
                working, as_of=as_of, principal=principal, project_id=project_id
            )
            planned = int(
                working.scalar(
                    select(func.count())
                    .select_from(RetentionManifestItem)
                    .where(RetentionManifestItem.manifest_id == manifest.id)
                )
                or 0
            )
            if planned == 0:
                # Nothing is due. The dry run is discarded rather than retained
                # as an empty manifest for every idle slot.
                working.rollback()
            else:
                execute_retention(
                    working,
                    manifest_id=manifest.id,
                    expected_sha256=manifest.content_sha256,
                    executed_at=as_of,
                )
                manifest_public_id = manifest.public_id
                deleted = planned
                working.commit()
        except RetentionRefused as exc:
            working.rollback()
            planned = 0
            deleted = 0
            refusal = _refusal_code(exc)

    return {
        "schema_version": _RESULT_SCHEMA_VERSION,
        "project_id": project_id,
        "configuration_version": configuration_version,
        "observed_at": _iso(as_of),
        "health": "healthy" if not refusal else "retention_attention_required",
        "authorized_by": authorized_by,
        "manifest_public_id": manifest_public_id,
        "planned": planned,
        "deleted": deleted,
        "refusal": refusal,
    }


def _refusal_code(exc: RetentionRefused) -> str:
    """A bounded reason code; the message itself names rows and paths."""

    message = str(exc)
    if "hold" in message:
        return "hold_active"
    if "referenced" in message or "reachable" in message:
        return "content_still_reachable"
    if "changed" in message or "unreadable" in message:
        return "content_changed_after_dry_run"
    return "retention_refused"


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RetentionSweepRefusal("sweep clock must supply an aware datetime")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _aware_utc(value).isoformat()
