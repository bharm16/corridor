"""What a Due Work handler declares, and what the runtime hands it back.

Every handler used to declare itself inside ``corridor.due_work``: fifteen
declaration dataclasses repeating the same twelve scheduling fields, fifteen
validators repeating the same gate-7 dict, fifteen ``configure_`` wrappers, and
a 535-line ``elif`` chain rebuilding each declaration from persisted columns —
a third and fourth copy of every handler's scope shape. Moving those next to
each handler's effectful function needed one seam both sides could import, and
it could not be ``due_work`` itself: the runtime already imports every handler
module, so a handler importing the runtime would close an import cycle the
architecture ratchet refuses (``tests/test_architecture.py``).

So this module holds the contract and nothing else: the shared scheduling
fields, the shared gate-7 validation and configuration shape, the registration
value a handler publishes, the resolved schedule the runtime hands an effectful
handler in place of a re-read row, and the one retained-reading read a handler
needs. It owns no lifecycle: leases, claims, retries, receipts and recovery
stay in ``corridor.due_work``, which is the only module that may make a
registration effective.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from datetime import datetime, timezone
import re
from typing import Any, ClassVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from corridor.models import DueWorkOccurrence, DueWorkReceipt

# The one gate-7 configuration shape every declaration is retained as. Its
# digest is persisted on the schedule row, so the key set and every value
# spelling here are a compatibility surface, not a formatting choice.
GATE_7_SCHEMA_VERSION = "due-work-gate-7-v1"

CONFIGURATION_VERSION = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
# A deployed identity: an extractor, a policy, a matcher, a location, a channel
# or a connector. One spelling, because a persisted identity that is valid for
# one handler and invalid for another would be a defect, not a policy.
DECLARED_IDENTITY = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
COHORT_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
# A bare DNS name: no scheme, port, path, or wildcard.
DECLARED_HOST = re.compile(
    r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,62})(\.[a-z0-9]([a-z0-9-]{0,62}))+$"
)


class DueWorkRefusal(ValueError):
    """A schedule, claim, result, or runtime identity is unsafe."""


def aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DueWorkRefusal("Due Work clock must supply an aware datetime")
    return value.astimezone(timezone.utc)


def iso_timestamp(value: datetime) -> str:
    return aware_utc(value).isoformat()


@dataclass(frozen=True)
class DueWorkScheduling:
    """The scheduling and resource fields every Due Work declaration carries.

    The runtime owns them: it persists them on the schedule row, enforces the
    lease and deadline it derives from them, and rebuilds them when it
    revalidates a stored schedule. A handler's declaration composes them by
    inheriting this shape, so its own scope fields are the only thing it
    repeats, and ``dataclasses.replace`` still reaches a scheduling field.
    """

    # Which server-owned handler this declaration is for. A declaration says
    # only that; the registry decides whether the handler exists, so an object
    # that names a key nothing registered is refused rather than dispatched.
    handler_key: ClassVar[str] = ""

    project_id: int
    configuration_version: str
    starts_at: datetime
    cadence: str
    timezone_name: str
    missed_run_policy: str
    retention_days: int
    max_attempts: int
    backoff_seconds: int
    claim_ttl_seconds: int
    deadline_seconds: int
    concurrency_limit: int
    model_token_budget: int
    notification_budget: int


SCHEDULING_FIELDS: tuple[str, ...] = tuple(
    field.name for field in fields(DueWorkScheduling)
)


@dataclass(frozen=True)
class ResolvedSchedule:
    """One persisted schedule as data, resolved once by the runtime.

    An effectful handler used to re-open a session and re-read its own schedule
    row for the project, the configuration version, and its scope — nine copies
    of the same read, each with its own "schedule disappeared" refusal. The
    runtime resolves the row once, before the claim's transaction closes, and
    hands this value to the handler and to revalidation.
    """

    schedule_id: int
    handler_key: str
    project_id: int
    configuration_version: str
    scope: Mapping[str, Any]
    # The retained gate-7 configuration. A declaration field that is recorded
    # there rather than in the scope — a comparison predecessor, a clone budget
    # — is rebuilt from this, so revalidation refuses an edited row with the
    # handler's own message rather than a bare digest mismatch.
    configuration: Mapping[str, Any]
    starts_at: datetime
    cadence: str
    timezone_name: str
    missed_run_policy: str
    retention_days: int
    max_attempts: int
    backoff_seconds: int
    claim_ttl_seconds: int
    deadline_seconds: int
    concurrency_limit: int
    model_token_budget: int
    notification_budget: int

    def scheduling_fields(self) -> dict[str, Any]:
        """The shared declaration arguments this stored schedule was written from."""

        return {name: getattr(self, name) for name in SCHEDULING_FIELDS}


@dataclass(frozen=True)
class ValidatedDeclaration:
    """What one validated declaration is retained as.

    ``configuration`` is digested and persisted whole; ``input_identity`` is
    digested into the schedule's ``input_identity_sha256`` and is a different
    shape from ``configuration["input_identity"]`` — both are compatibility
    surfaces, so a handler returns exactly the mapping its schedules were
    written with.
    """

    configuration: dict[str, Any]
    input_identity: dict[str, Any]

    @property
    def scope(self) -> dict[str, Any]:
        return self.configuration["scope"]


@dataclass(frozen=True)
class HandlerRegistration:
    """One handler's whole declaration surface, published to the runtime.

    The runtime keeps the lease, the retries, the receipts and the recovery; a
    registration says only what this handler's work is declared as, how a
    declaration is validated, how a persisted row is rebuilt and re-checked,
    and what to run. Persisted rows select a registration by key from the
    server-owned table in ``corridor.due_work``, so a row can never name an
    import, a command, or a destination.
    """

    key: str
    scope_kind: str
    idempotency_contract: str
    max_result_bytes: int
    model_token_budget: int
    notification_budget: int
    declaration_type: type | None = None
    # A declaration in, its retained gate-7 configuration and digest-able input
    # identity out; a refusal means no schedule is written.
    validate: Callable[[Any], ValidatedDeclaration] | None = None
    # A stored schedule's columns in, this handler's declaration out, so the
    # runtime can re-derive what the row should say and compare.
    stored_declaration: Callable[[ResolvedSchedule], Any] | None = None
    # A read-only handler runs inside a `reading` session and returns a result
    # dict the runtime finalizes. An effectful handler owns its own durable
    # commits and instead takes an `EffectfulContext`; exactly one is set.
    run: Callable[..., dict[str, Any]] | None = None
    run_effectful: Callable[[Any], dict[str, Any]] | None = None
    # A published handler has no cadence slot: its occurrences are created one
    # per durable request by this hook, which `enqueue_due_work` calls instead
    # of computing a due slot. The hook returns the occurrence keys it has, so
    # the enqueue result names them exactly as a cadence occurrence is named.
    publish: Callable[..., tuple[str, ...]] | None = None
    # A declaration whose validity also depends on committed rows (an active
    # roster identity, say) checks them here, inside the configuring session.
    configure_check: Callable[[Session, Any], None] | None = None
    # Enabling a new configuration disables the project's prior schedules for
    # this handler. A handler whose scope names one of several concurrent
    # subjects — a location, a connector, a cohort — supersedes only the
    # schedule with the same input identity.
    disable_same_input_only: bool = False

    def revalidate(self, stored: ResolvedSchedule) -> ValidatedDeclaration:
        """Rebuild this handler's declaration from persisted columns and re-check it."""

        if self.stored_declaration is None or self.validate is None:
            raise DueWorkRefusal("persisted Due Work handler declares no validation")
        return self.validate(self.stored_declaration(stored))


def validate_scheduling(
    declaration: DueWorkScheduling,
    *,
    subject: str,
    cadence: str = "hourly",
    timezone_name: str = "UTC",
    missed_run_policy: str = "latest_only",
    schedule_refusal: str | None = None,
    schedule_valid: bool = True,
    checks: tuple[tuple[bool, str], ...] = (),
    claim_ttl_seconds: tuple[int, int] = (30, 3600),
    backoff_seconds: tuple[int, int] = (0, 3600),
    model_token_budget: tuple[int, int] = (0, 0),
    notification_budget: tuple[int, int] = (0, 0),
    resources_valid: bool = True,
) -> datetime:
    """Check the shared gate-7 envelope, and return the aligned ``starts_at``.

    Every handler declares the same envelope with its own bounds, so this is
    one implementation with arguments rather than fifteen near-copies. The
    refusal messages are the ones each handler already raised: an operator
    reading a refusal, and the tests that match on it, see no change.
    """

    starts_at = aware_utc(declaration.starts_at)
    if starts_at.minute or starts_at.second or starts_at.microsecond:
        raise DueWorkRefusal(f"{subject} starts_at must align to a UTC hour")
    if not CONFIGURATION_VERSION.fullmatch(declaration.configuration_version):
        raise DueWorkRefusal("Due Work configuration version is invalid")
    if (
        declaration.cadence != cadence
        or declaration.timezone_name != timezone_name
        or declaration.missed_run_policy != missed_run_policy
        or not schedule_valid
    ):
        raise DueWorkRefusal(
            schedule_refusal
            or (
                f"{subject} supports only {cadence} {timezone_name} "
                f"{missed_run_policy.replace('_', '-')} scheduling"
            )
        )
    # A handler's own declared parameters, checked between the schedule and the
    # resource envelope so each refuses with its own message in its own order.
    for holds, refusal in checks:
        if not holds:
            raise DueWorkRefusal(refusal)
    if not (
        1 <= declaration.max_attempts <= 5
        and backoff_seconds[0] <= declaration.backoff_seconds <= backoff_seconds[1]
        and claim_ttl_seconds[0]
        <= declaration.claim_ttl_seconds
        <= claim_ttl_seconds[1]
        and 1 <= declaration.deadline_seconds <= declaration.claim_ttl_seconds
        and declaration.concurrency_limit == 1
        and declaration.retention_days >= 365
        and model_token_budget[0]
        <= declaration.model_token_budget
        <= model_token_budget[1]
        and notification_budget[0]
        <= declaration.notification_budget
        <= notification_budget[1]
        and resources_valid
    ):
        raise DueWorkRefusal(f"{subject} gate-7 resource declaration is invalid")
    return starts_at


def gate7_configuration(
    declaration: DueWorkScheduling,
    *,
    handler: str,
    scope: dict[str, Any],
    input_identity: dict[str, Any],
    idempotency_contract: str,
    starts_at: datetime,
    extra: Mapping[str, Any] | None = None,
    extra_resources: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The retained gate-7 configuration, exactly as schedules were written with it."""

    return {
        "schema_version": GATE_7_SCHEMA_VERSION,
        "handler": handler,
        "project_id": declaration.project_id,
        "scope": scope,
        "configuration_version": declaration.configuration_version,
        "input_identity": input_identity,
        **dict(extra or {}),
        "starts_at": iso_timestamp(starts_at),
        "cadence": declaration.cadence,
        "timezone": declaration.timezone_name,
        "missed_run_policy": declaration.missed_run_policy,
        "retention": {
            "policy": "retain_all_terminal_receipts",
            "minimum_days": declaration.retention_days,
        },
        "retry": {
            "max_attempts": declaration.max_attempts,
            "backoff_seconds": declaration.backoff_seconds,
        },
        "resources": {
            "claim_ttl_seconds": declaration.claim_ttl_seconds,
            "deadline_seconds": declaration.deadline_seconds,
            "concurrency_limit": declaration.concurrency_limit,
            "model_token_budget": declaration.model_token_budget,
            "notification_budget": declaration.notification_budget,
            **dict(extra_resources or {}),
        },
        "authorized_destinations": [],
        "idempotency_contract": idempotency_contract,
    }


def previous_completed_reading(
    session: Session, *, schedule_id: int, handler_key: str
) -> dict[str, Any]:
    """This schedule's newest completed handler result, or an empty mapping.

    Ordered by receipt identity rather than ``finished_at``: the identifier is
    monotonic in insertion order no matter what any clock said, and "newest"
    here must mean the last one retained. A handler whose next pass continues
    where the last one stopped — a change-summary window, a fact watermark —
    reads its own floor through this one seam.
    """

    result = session.scalars(
        select(DueWorkReceipt.handler_result_json)
        .join(
            DueWorkOccurrence,
            DueWorkOccurrence.id == DueWorkReceipt.occurrence_id,
        )
        .where(
            DueWorkOccurrence.scheduled_job_id == schedule_id,
            DueWorkReceipt.handler_key == handler_key,
            DueWorkReceipt.execution_outcome == "completed",
        )
        .order_by(DueWorkReceipt.id.desc())
        .limit(1)
    ).first()
    return dict(result or {})
