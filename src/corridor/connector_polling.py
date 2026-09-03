"""Poll one connected location's PullConnector on a schedule (#488, #496).

``location_discovery`` already walks an HTML index and registers Documents, but
it is not the pull contract ADR-0083 fixed: it has no cursor, so every pass
re-enumerates the whole location and can never say what it has already taken
delivery of.  This module runs the four-method ``PullConnector`` instead, one
bounded pass per claimed Due Work occurrence.

Where the cursor lives was the design question.  A checkpoint table was
rejected: the migration window is closed (``corridor.migrations.policy``), and
the runtime already writes exactly the durable, append-only, ordered record a
cursor needs.  The token a pass reached is returned in the handler result, so
the runtime retains it on that occurrence's completed receipt *after* every
listed change is durably in the content-addressed store under its digest.  A
crash between storage and the receipt leaves the previous token standing, so
the next pass re-lists and re-stores bytes that are already there — ADR-0083's
rule that a checkpoint never advances past an unstored change, kept without a
second record of the same fact.

Which connector a persisted schedule may name is a server-owned registry, for
the same reason the Due Work handler registry is one: a stored row selects a
built-in adapter and can never name an import, a command, or an arbitrary
destination.

This module owns no schedule, timer, or clock.  The one supervised Due Work
runtime (#332) discovers, claims, and retries occurrences; this is the bounded,
idempotent work one claimed occurrence performs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.connectors.box import BoxPullConnector
from corridor.connectors.pull_connector import (
    ChangeItem,
    PullConnector,
    sync_pull_connector,
)
from corridor.models import (
    DueWorkOccurrence,
    DueWorkReceipt,
    DueWorkSchedule,
    Project,
)

# The one server-owned handler key this module's work runs under.  It matches
# ``due_work.HANDLER_CONNECTOR_POLLING``; the constant lives here because this
# module is the lower layer and the runtime imports its execution, never the
# reverse.
HANDLER_KEY = "connector_polling"

_RESULT_SCHEMA_VERSION = "connector-polling-result-v1"


class ConnectorPollingRefusal(ValueError):
    """A declared connector cannot be built or polled safely."""


def _box_shared_files(scope: Mapping[str, Any]) -> PullConnector:
    """The Box/TxDOT RID adapter over the one declared shared URL."""

    return BoxPullConnector(shared_urls=(str(scope["source_url"]),))


CONNECTOR_FACTORIES: Mapping[str, Callable[[Mapping[str, Any]], PullConnector]] = (
    MappingProxyType({"txdot-rid-box-v1": _box_shared_files})
)


class CheckpointRecorder:
    """Observe the token one pass advanced to without widening the contract.

    ``PullConnector`` has no way to report the token it checkpointed, and
    adding one would change #496's four-method interface for every adapter.
    This delegate records the token as it passes through, so the handler can
    retain it on the receipt while the connector still performs its own
    checkpoint exactly as ADR-0083 requires.
    """

    def __init__(self, connector: PullConnector) -> None:
        self._connector = connector
        self.token: str | None = None

    def list_changes(
        self, cursor: str | None = None
    ) -> tuple[Sequence[ChangeItem], str]:
        return self._connector.list_changes(cursor)

    def fetch_version(self, item_id: str, version_id: str) -> bytes:
        return self._connector.fetch_version(item_id, version_id)

    def get_metadata(self, item_id: str) -> dict[str, Any]:
        return self._connector.get_metadata(item_id)

    def checkpoint(self, token: str) -> None:
        self._connector.checkpoint(token)
        self.token = token


def last_checkpoint_token(session: Session, schedule_id: int) -> str | None:
    """The token this schedule's newest completed attempt durably reached.

    Ordered by receipt identity rather than ``finished_at``: the identifier is
    monotonic in insertion order no matter what any clock said, and "newest"
    here must mean the last one retained.
    """

    result = session.scalars(
        select(DueWorkReceipt.handler_result_json)
        .join(
            DueWorkOccurrence,
            DueWorkOccurrence.id == DueWorkReceipt.occurrence_id,
        )
        .where(
            DueWorkOccurrence.scheduled_job_id == schedule_id,
            DueWorkReceipt.handler_key == HANDLER_KEY,
            DueWorkReceipt.execution_outcome == "completed",
        )
        .order_by(DueWorkReceipt.id.desc())
        .limit(1)
    ).first()
    token = (result or {}).get("checkpoint_token") or None
    return str(token) if token else None


def execute_connector_polling(
    session_factory,
    *,
    schedule_id: int,
    clock,
    connector: PullConnector | None = None,
) -> dict[str, Any]:
    """Take delivery of one connected location's changes since its checkpoint.

    ``connector`` is injectable so a test can drive the contract without an
    outbound request; production leaves it unset and the declared adapter is
    built from the server-owned registry.  The pass holds no runtime
    transaction while it fetches and stores, and the token it returns is
    retained only if the runtime completes the attempt.
    """

    observed_at = _aware_utc(clock.now())
    with session_factory() as reading:
        schedule = reading.get(DueWorkSchedule, schedule_id)
        if schedule is None:
            raise ConnectorPollingRefusal("connector-polling schedule disappeared")
        scope = dict(schedule.scope_json)
        project_id = schedule.project_id
        configuration_version = schedule.configuration_version
        project_slug = reading.scalar(
            select(Project.slug).where(Project.id == project_id)
        )
        if project_slug is None:
            raise ConnectorPollingRefusal(f"project {project_id} does not exist")
        cursor = last_checkpoint_token(reading, schedule_id)

    connector_identity = str(scope.get("connector_identity", ""))
    if connector is None:
        factory = CONNECTOR_FACTORIES.get(connector_identity)
        if factory is None:
            raise ConnectorPollingRefusal("connector is not server-owned")
        connector = factory(scope)

    recorder = CheckpointRecorder(connector)
    envelopes = sync_pull_connector(
        recorder,
        customer=str(scope["customer"]),
        project=project_slug,
        channel=str(scope["channel"]),
        cursor=cursor,
    )
    checkpoint_token = recorder.token or cursor
    advanced = recorder.token is not None and recorder.token != cursor
    # Taking delivery of changes without moving the cursor means the next pass
    # re-takes exactly the same ones: bounded and safe, but never finished.
    health = (
        "polling_attention_required"
        if envelopes and not advanced
        else "healthy"
    )
    return {
        "schema_version": _RESULT_SCHEMA_VERSION,
        "project_id": project_id,
        "configuration_version": configuration_version,
        "observed_at": _iso(observed_at),
        "health": health,
        "channel": str(scope["channel"]),
        "connector_identity": connector_identity,
        "cursor": cursor or "",
        "checkpoint_token": checkpoint_token or "",
        "advanced": advanced,
        "changes_taken": len(envelopes),
    }


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ConnectorPollingRefusal("polling clock must supply an aware datetime")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _aware_utc(value).isoformat()
