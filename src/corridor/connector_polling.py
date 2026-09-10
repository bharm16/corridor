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

The pass also registers what it took delivery of.  Recording a delivery is not
showing anybody a source, and for a long time this handler did only the first:
it never passed ``on_envelope_stored``, so a production Box or TxDOT poll
stored bytes, wrote a ledger row, advanced its cursor, and registered no
Document at all.  ``corridor.delivery_registration`` is the one consumer of a
stored delivery now, shared with the offline replay command that used to hold
the only registering code, and this pass hands it each stored delivery in its
own short transaction after that delivery has committed.

Which connector a persisted schedule may name is a server-owned registry, for
the same reason the Due Work handler registry is one: a stored row selects a
built-in adapter and can never name an import, a command, or an arbitrary
destination.

This module owns no schedule, timer, or clock.  The one supervised Due Work
runtime (#332) discovers, claims, and retries occurrences; this is the bounded,
idempotent work one claimed occurrence performs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Callable, ClassVar, Mapping

from sqlalchemy import select

from corridor.connectors.box import BoxPullConnector
from corridor.connectors.pull_connector import (
    PullConnector,
    sync_pull_connector,
)
from corridor.delivery_registration import DeliveryTransactions, PassLedger
from corridor.due_work_contract import (
    COHORT_IDENTITY,
    DECLARED_HOST,
    DECLARED_IDENTITY,
    DueWorkRefusal,
    DueWorkScheduling,
    HandlerRegistration,
    ResolvedSchedule,
    ValidatedDeclaration,
    gate7_configuration,
    validate_scheduling,
)
from corridor.models import (
    DueWorkSchedule,
    Project,
)
from corridor.source_delivery import (
    DeliveryBinding,
    current_checkpoint_token,
    record_checkpoint_advance,
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
    own committed transaction, the source it carried is registered in another
    one straight after, and the advance is recorded last, so a crash anywhere
    in the pass leaves the cursor where it was.

    A registration that fails therefore raises out of the pass rather than
    being counted and passed over.  Nothing is lost either way — the delivery
    is already committed — but the cursor does not move, so the next attempt
    re-lists that same change and registers it against the delivery the ledger
    already holds.  Swallowing the failure and advancing would leave bytes
    nobody can see and no record that anything was missing, which is the silent
    outcome ADR-0089 refuses everywhere else.

    What the pass reports is unchanged: ``due_work`` validates this result
    against an exact key set for the ``connector-polling-result-v1`` schema, so
    naming the registrations here is a schema revision rather than an extra
    key, and every registration is already durable in the record it wrote.
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
    ledger = PassLedger(
        DeliveryTransactions.committing(session_factory),
        binding,
        service_identity=SERVICE_IDENTITY,
        run_identity=run_identity,
    )

    sync = sync_pull_connector(
        connector,
        customer=binding.customer,
        project=project_slug,
        channel=channel,
        cursor=cursor,
        ledger=ledger,
        on_envelope_stored=ledger.register,
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


# --- The Due Work declaration this pass runs under ------------------------
#
# What one poll is declared to be — one normalized ingress identity, one
# installed connector, one https location, no model spend and no destination —
# belongs with the pass that honours it. The runtime keeps the lease (card 6).


@dataclass(frozen=True)
class ConnectorPollingDeclaration(DueWorkScheduling):
    """One validated gate-7 declaration that enables connector checkpoint polling.

    Scope names the exact normalized ingress identity of #496: the customer and
    channel that, with the project and the item's own identity and version,
    make the delivery identity ADR-0083 fixes, plus the server-owned connector
    the schedule may build and the one location it may reach.  Polling reads no
    model, sends nothing, and writes only content-addressed bytes, so the model
    and notification budgets must both be a declared zero and no destination is
    authorized.
    """

    handler_key: ClassVar[str] = HANDLER_KEY

    customer: str
    channel: str
    connector_identity: str
    source_url: str

    @classmethod
    def released_hourly(
        cls,
        *,
        project_id: int,
        configuration_version: str,
        customer: str,
        channel: str,
        connector_identity: str,
        source_url: str,
        starts_at: datetime,
    ) -> "ConnectorPollingDeclaration":
        return cls(
            project_id=project_id,
            configuration_version=configuration_version,
            customer=customer,
            channel=channel,
            connector_identity=connector_identity,
            source_url=source_url,
            starts_at=starts_at,
            cadence="hourly",
            timezone_name="UTC",
            missed_run_policy="latest_only",
            retention_days=3650,
            max_attempts=3,
            backoff_seconds=120,
            claim_ttl_seconds=900,
            deadline_seconds=600,
            concurrency_limit=1,
            model_token_budget=0,
            notification_budget=0,
        )


def _validated_declaration(
    declaration: ConnectorPollingDeclaration,
) -> ValidatedDeclaration:
    """Validate one connector-polling declaration.

    Only an installed connector and one https location inside it are ever
    enabled; credentials never widen the scope, and a second connector or a
    second location on the same project is a different identity with its own
    schedule and its own checkpoint.
    """

    from urllib.parse import urlparse

    if not COHORT_IDENTITY.fullmatch(declaration.customer):
        raise DueWorkRefusal("connector-polling customer identity is invalid")
    if not DECLARED_IDENTITY.fullmatch(declaration.channel):
        raise DueWorkRefusal("connector-polling channel identity is invalid")
    if not DECLARED_IDENTITY.fullmatch(declaration.connector_identity):
        raise DueWorkRefusal("connector-polling connector identity is invalid")
    if declaration.connector_identity not in CONNECTOR_FACTORIES:
        raise DueWorkRefusal("connector-polling connector is not installed")
    parsed = urlparse(declaration.source_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not DECLARED_HOST.fullmatch(parsed.hostname)
        or len(declaration.source_url) > 2048
    ):
        raise DueWorkRefusal(
            "connector-polling source url must be one https location"
        )
    starts_at = validate_scheduling(declaration, subject="connector-polling")
    scope = {
        "project_id": declaration.project_id,
        "customer": declaration.customer,
        "channel": declaration.channel,
        "connector_identity": declaration.connector_identity,
        "source_url": declaration.source_url,
    }
    return ValidatedDeclaration(
        configuration=gate7_configuration(
            declaration,
            handler=HANDLER_KEY,
            scope=scope,
            input_identity={"kind": "declared_pull_connector-v1", **scope},
            idempotency_contract="at_least_once_reconcilable",
            starts_at=starts_at,
            # ADR-0083: the token advances only after every change up to it is
            # in the content-addressed store under its digest. The retained
            # completed receipt is where this schedule's token stands.
            extra={"checkpoint_policy": "advance_after_durable_storage"},
        ),
        input_identity={"handler": HANDLER_KEY, **scope},
    )


def _stored_declaration(stored: ResolvedSchedule) -> ConnectorPollingDeclaration:
    return ConnectorPollingDeclaration(
        **stored.scheduling_fields(),
        customer=stored.scope.get("customer", ""),
        channel=stored.scope.get("channel", ""),
        connector_identity=stored.scope.get("connector_identity", ""),
        source_url=stored.scope.get("source_url", ""),
    )


def _run_due_work(context) -> dict[str, Any]:
    """Take delivery of one connected location's changes for a claimed occurrence.

    The pass stores every listed change in the content-addressed store, records
    each delivery in the shared ledger, and only then records the advance its
    cursor reached, so the checkpoint can never advance past an unstored change
    or a transient failure (ADR-0083 as extended by ADR-0089). It reads no model
    and holds no runtime transaction while fetching. The attempt identity is the
    run identity the ledger and the advance are attributed to.
    """

    return execute_connector_polling(
        context.session_factory,
        schedule_id=context.schedule.schedule_id,
        clock=context.clock,
        run_identity=context.claim.attempt_id,
    )


DUE_WORK_REGISTRATION = HandlerRegistration(
    key=HANDLER_KEY,
    scope_kind="one_declared_pull_connector",
    idempotency_contract="at_least_once_reconcilable",
    max_result_bytes=4096,
    model_token_budget=0,
    notification_budget=0,
    declaration_type=ConnectorPollingDeclaration,
    validate=_validated_declaration,
    stored_declaration=_stored_declaration,
    run_effectful=_run_due_work,
    disable_same_input_only=True,
)
