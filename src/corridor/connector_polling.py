"""Poll one connected location's PullConnector on a schedule (#488, #496, #599).

``location_discovery`` already walks an HTML index and registers Documents, but
it is not the pull contract ADR-0083 fixed: it has no cursor, so every pass
re-enumerates the whole location and can never say what it has already taken
delivery of.  This module runs the four-method ``PullConnector`` instead, one
bounded pass per claimed Due Work occurrence.

Where the cursor lives was the design question, and the answer changed.  A
checkpoint table was rejected here once, in these words: "the migration window
is closed (``corridor.migrations.policy``), and the runtime already writes
exactly the durable, append-only, ordered record a cursor needs."  The second
half was true and the first half was a scheduling constraint standing in for a
design one.  The consequence was that the external cursor became a derived
property of *receipt retention*: a receipt sweep, a retention-policy change, or
an ordinary cleanup would reset a live connector's cursor or land it on a stale
token, and #488 could only mitigate that by retaining those receipts for 3650
days.  ADR-0089 reverses the decision.  The cursor now lives with the connector
configuration, in ``connector_checkpoint_advances``, and the current checkpoint
is derived from the newest advance recorded there.  The Due Work receipt is
still the record of the occurrence; it is no longer the record of the cursor.

The pass also records what it took delivery of.  ADR-0083 made the
``SourceEnvelope`` common to pull and push and #511 persisted only the push
half, so a pull delivery existed nowhere and a delivery the intake gate refused
was simply lost.  Both transports now write one ``source_deliveries`` family
through ``corridor.source_delivery``, and the checkpoint advance names the
deliveries it covered — which is what lets the database refuse an advance past
a transient failure.

Which connector a persisted schedule may name is a server-owned registry, for
the same reason the Due Work handler registry is one: a stored row selects a
built-in adapter and can never name an import, a command, or an arbitrary
destination.

This module owns no schedule, timer, or clock.  The one supervised Due Work
runtime (#332) discovers, claims, and retries occurrences; this is the bounded,
idempotent work one claimed occurrence performs.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Callable, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.connectors.box import BoxPullConnector
from corridor.connectors.pull_connector import (
    ChangeItem,
    PullConnector,
    sync_pull_connector,
)
from corridor.models import (
    DueWorkSchedule,
    Project,
)
from corridor.source_delivery import (
    DISPOSITION_STORED,
    DeliveryBinding,
    DeliveryObservation,
    current_checkpoint_token,
    record_checkpoint_advance,
    record_delivery,
    take_delivery,
)

# The one server-owned handler key this module's work runs under.  It matches
# ``due_work.HANDLER_CONNECTOR_POLLING``; the constant lives here because this
# module is the lower layer and the runtime imports its execution, never the
# reverse.
HANDLER_KEY = "connector_polling"

# Who took delivery, for the ledger ADR-0089 shares with the push half.
SERVICE_IDENTITY = "corridor.connector_polling"

_RESULT_SCHEMA_VERSION = "connector-polling-result-v1"


class ConnectorPollingRefusal(ValueError):
    """A declared connector cannot be built or polled safely."""


def _box_shared_files(scope: Mapping[str, Any]) -> PullConnector:
    """The Box/TxDOT RID adapter over the one declared shared URL."""

    return BoxPullConnector(shared_urls=(str(scope["source_url"]),))


def _recorded_m365(scope: Mapping[str, Any]) -> PullConnector:
    """Offline configurations require the replay command's injected recording."""
    raise ConnectorPollingRefusal("recorded Microsoft 365 configurations run only through m365-replay")


CONNECTOR_FACTORIES: Mapping[str, Callable[[Mapping[str, Any]], PullConnector]] = (
    MappingProxyType({"txdot-rid-box-v1": _box_shared_files,
                     "m365-graph-recording-v1": _recorded_m365})
)


class LedgerWriter:
    """Record one pass's deliveries, one committed transaction at a time.

    Each delivery is written in its own short transaction rather than under one
    long one held open across the fetches, which is the property #488 already
    had and this must not give up: the pass holds no runtime transaction while
    it talks to the external system.  Committing per delivery is also what makes
    the refusal evidence durable *before* the pass decides whether it may
    advance past it (ADR-0089).
    """

    def __init__(self, session_factory, binding: DeliveryBinding, run_identity: str):
        from corridor.activation_runtime import DeliveryActivationContext
        self.activation_context = DeliveryActivationContext(session_factory, binding)
        self._session_factory = session_factory
        self._binding = binding
        self._run_identity = run_identity
        self.dispositions: Counter[str] = Counter()

    def record(
        self,
        item: ChangeItem,
        *,
        disposition: str,
        content_digest: str,
        bytes_reference: str,
        refusal_reason: str | None,
    ) -> int:
        observation = DeliveryObservation(
            external_identity=item.item_id,
            external_version=item.version_id,
            content_digest=content_digest,
            bytes_reference=bytes_reference,
            original_timestamps=dict(item.original_timestamps),
            metadata=dict(item.metadata),
        )
        with self._session_factory() as writing:
            with writing.begin():
                if disposition == DISPOSITION_STORED:
                    recorded = take_delivery(
                        writing,
                        self._binding,
                        observation,
                        service_identity=SERVICE_IDENTITY,
                        run_identity=self._run_identity,
                    )
                else:
                    recorded = record_delivery(
                        writing,
                        self._binding,
                        observation,
                        disposition=disposition,
                        service_identity=SERVICE_IDENTITY,
                        run_identity=self._run_identity,
                        refusal_reason=refusal_reason,
                    )
        self.dispositions[recorded.disposition] += 1
        return recorded.delivery_id


def execute_connector_polling(
    session_factory,
    *,
    schedule_id: int,
    clock,
    run_identity: str,
    connector: PullConnector | None = None,
) -> dict[str, Any]:
    """Take delivery of one connected location's changes since its checkpoint.

    ``connector`` is injectable so a test can drive the contract without an
    outbound request; production leaves it unset and the declared adapter is
    built from the server-owned registry.  The pass holds no runtime
    transaction while it fetches and stores: each delivery is recorded in its
    own committed transaction, and the advance is recorded last, so a crash
    anywhere in the pass leaves the cursor where it was.
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
        cursor = current_checkpoint_token(reading, schedule_id)

    connector_identity = str(scope.get("connector_identity", ""))
    if connector is None:
        factory = CONNECTOR_FACTORIES.get(connector_identity)
        if factory is None:
            raise ConnectorPollingRefusal("connector is not server-owned")
        connector = factory(scope)

    channel = str(scope["channel"])
    binding = DeliveryBinding(
        customer=str(scope["customer"]),
        project_id=project_id,
        project_slug=project_slug,
        transport="pull",
        channel=channel,
        configuration_identity=connector_identity,
        configuration_version=configuration_version,
    )
    ledger = LedgerWriter(session_factory, binding, run_identity)

    sync = sync_pull_connector(
        connector,
        customer=binding.customer,
        project=project_slug,
        channel=channel,
        cursor=cursor,
        ledger=ledger,
    )

    checkpoint_token = sync.checkpoint_token or cursor
    # ``sync.advanced`` is set in exactly the pass that called
    # ``connector.checkpoint(next_token)``, and its ``checkpoint_token`` is
    # that same token; a delegate that observed the call as it passed through
    # could only repeat what the result already reports.
    advanced = sync.advanced
    if advanced:
        with session_factory() as writing:
            with writing.begin():
                record_checkpoint_advance(
                    writing,
                    project_id=project_id,
                    schedule_id=schedule_id,
                    configuration_identity=connector_identity,
                    configuration_version=configuration_version,
                    channel=channel,
                    checkpoint_token=str(sync.checkpoint_token),
                    service_identity=SERVICE_IDENTITY,
                    run_identity=run_identity,
                    delivery_ids=sync.delivery_ids(),
                )
    # Taking delivery of changes without moving the cursor means the next pass
    # re-takes exactly the same ones: bounded and safe, but never finished.
    health = (
        "polling_attention_required"
        if sync.records and not advanced
        else "healthy"
    )
    return {
        "schema_version": _RESULT_SCHEMA_VERSION,
        "project_id": project_id,
        "configuration_version": configuration_version,
        "observed_at": _iso(observed_at),
        "health": health,
        "channel": channel,
        "connector_identity": connector_identity,
        "cursor": cursor or "",
        "checkpoint_token": checkpoint_token or "",
        "advanced": advanced,
        "changes_taken": len(sync.envelopes),
        "dispositions": dict(sorted(ledger.dispositions.items())),
        "blocked_by": list(sync.blocked_by),
    }


def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ConnectorPollingRefusal("polling clock must supply an aware datetime")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _aware_utc(value).isoformat()
