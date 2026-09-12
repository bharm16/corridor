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

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, ClassVar

from sqlalchemy import func, select

from corridor.due_work_contract import (
    DueWorkRefusal,
    DueWorkScheduling,
    HandlerRegistration,
    ResolvedSchedule,
    ValidatedDeclaration,
    gate7_configuration,
    validate_scheduling,
)
from corridor.models import DueWorkSchedule, RetentionManifestItem
from corridor.principals import HumanPrincipal, InvalidHumanPrincipal
from corridor.retention import (
    RetentionRefused,
    execute_retention_in_batches,
    plan_retention,
)

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
    manifest_id: int | None = None
    manifest_sha256 = ""
    # Plan in its own transaction and commit it, so the bounded execution
    # batches -- each its own transaction, releasing the ordering boundary
    # between them (#956 C) -- can see the manifest. A refusal at plan time
    # (a still-reachable citation, drift) is an outcome, not a crash.
    try:
        with session_factory() as planning:
            manifest = plan_retention(
                planning, as_of=as_of, principal=principal, project_id=project_id
            )
            planned = int(
                planning.scalar(
                    select(func.count())
                    .select_from(RetentionManifestItem)
                    .where(RetentionManifestItem.manifest_id == manifest.id)
                )
                or 0
            )
            if planned == 0:
                # Nothing is due. The dry run is discarded rather than retained
                # as an empty manifest for every idle slot.
                planning.rollback()
            else:
                manifest_id = manifest.id
                manifest_sha256 = manifest.content_sha256
                planning.commit()
    except RetentionRefused as exc:
        planned = 0
        refusal = _refusal_code(exc)

    if manifest_id is not None and not refusal:
        acknowledgement = execute_retention_in_batches(
            session_factory,
            manifest_id=manifest_id,
            expected_sha256=manifest_sha256,
            executed_at=as_of,
        )
        planned = acknowledgement["requested"]
        deleted = acknowledgement["enforced"]
        refusal = acknowledgement["refusal"]
        manifest_public_id = acknowledgement["manifest_public_id"]

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


# --- The Due Work declaration this pass runs under ------------------------
#
# The runtime keeps the lease, the retries and the receipt; what a sweep *is*
# — weekly, attributable to a named person, spending no model tokens and
# sending nothing — is this module's own statement, so it lives here with the
# execution rather than in the runtime (card 6).


@dataclass(frozen=True)
class RetentionSweepDeclaration(DueWorkScheduling):
    """One validated gate-7 declaration that enables the Class B TTL sweep.

    ``authorized_by`` is the named person with authority under the customer
    relationship who authorized this standing sweep (ADR-0080).  It is a
    ``HumanPrincipal`` subject, not a role label, and it is the actor recorded
    on every dry-run manifest and deletion receipt the sweep produces, so an
    automatic expiry is still attributable to somebody.  The sweep reads no
    model and sends nothing; it is weekly because the Class B TTL is measured
    in days, not hours.
    """

    handler_key: ClassVar[str] = HANDLER_KEY

    authorized_by: str

    @classmethod
    def released_weekly(
        cls,
        *,
        project_id: int,
        configuration_version: str,
        authorized_by: str,
        starts_at: datetime,
    ) -> "RetentionSweepDeclaration":
        return cls(
            project_id=project_id,
            configuration_version=configuration_version,
            authorized_by=authorized_by,
            starts_at=starts_at,
            cadence="weekly",
            timezone_name="UTC",
            missed_run_policy="latest_only",
            retention_days=3650,
            max_attempts=3,
            backoff_seconds=300,
            claim_ttl_seconds=1800,
            deadline_seconds=1800,
            concurrency_limit=1,
            model_token_budget=0,
            notification_budget=0,
        )


def _validated_declaration(
    declaration: RetentionSweepDeclaration,
) -> ValidatedDeclaration:
    """Validate one Class B retention-sweep declaration."""

    try:
        HumanPrincipal(declaration.authorized_by)
    except InvalidHumanPrincipal as exc:
        raise DueWorkRefusal(
            "retention-sweep authorization must name a human principal"
        ) from exc
    starts_at = validate_scheduling(
        declaration, subject="retention-sweep", cadence="weekly"
    )
    scope = {
        "project_id": declaration.project_id,
        "authorized_by": declaration.authorized_by,
    }
    return ValidatedDeclaration(
        configuration=gate7_configuration(
            declaration,
            handler=HANDLER_KEY,
            scope=scope,
            input_identity={
                "kind": "project_class_b_intermediaries-v1",
                "project_id": declaration.project_id,
            },
            idempotency_contract="at_least_once_reconcilable",
            starts_at=starts_at,
            # ADR-0080: only the five Class B intermediary families and
            # registered processing artifacts are reachable, and every deletion
            # still passes the hold check, the reachability check, and the
            # dry-run manifest.
            extra={"retention_class": "class_b"},
        ),
        input_identity={
            "handler": HANDLER_KEY,
            "project_id": declaration.project_id,
            "retention_class": "class_b",
        },
    )


def _stored_declaration(stored: ResolvedSchedule) -> RetentionSweepDeclaration:
    return RetentionSweepDeclaration(
        **stored.scheduling_fields(),
        authorized_by=stored.scope.get("authorized_by", ""),
    )


def _run_due_work(context) -> dict[str, Any]:
    """Expire one project's due Class B intermediaries for a claimed occurrence.

    The manifest, the hold and reachability rechecks, and the deletion permit
    all stay in ``corridor.retention``; this adapter only turns the runtime's
    claim into that sweep. A refusal comes back as an attention reading rather
    than an exception, so a held or still-reachable candidate is visible in the
    receipt instead of burning the occurrence's retries.
    """

    return execute_retention_sweep(
        context.session_factory,
        schedule_id=context.schedule.schedule_id,
        clock=context.clock,
    )


DUE_WORK_REGISTRATION = HandlerRegistration(
    key=HANDLER_KEY,
    scope_kind="one_project_class_b_intermediaries",
    idempotency_contract="at_least_once_reconcilable",
    max_result_bytes=4096,
    model_token_budget=0,
    notification_budget=0,
    declaration_type=RetentionSweepDeclaration,
    validate=_validated_declaration,
    stored_declaration=_stored_declaration,
    run_effectful=_run_due_work,
)
