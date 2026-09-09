"""Bounded environment registration, scoped routing and external receipt custody.

#531 enforces projects within a database; it cannot choose that database or keep
a receipt after it disappears. This module accepts only a separate control-plane
engine and retains identifiers and references. It never imports Project Record
models and exposes no customer content, schema session, or default route.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any
import re

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from corridor.control_plane_schema import (
    DESTRUCTION_RECEIPTS,
    DISPOSITION_PLANS,
    DISPOSITION_REHEARSAL_RECEIPTS,
    ENVIRONMENTS,
)


class RouteRefused(ValueError):
    """The customer environment cannot be established without guessing."""


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", value
    ):
        raise ValueError("a bounded stable identifier is required")
    return value


def reference(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[a-z][a-z0-9-]*:[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,480}", value
    ):
        raise ValueError(
            "a bounded reference is required; credentials and content are not accepted"
        )
    return value


@dataclass(frozen=True)
class EnvironmentRegistration:
    customer_id: str
    environment_id: str
    deployment_id: str
    database_host: str
    database_port: int
    database_name: str
    web_credential_ref: str
    worker_credential_ref: str
    object_namespace_ref: str
    connector_configuration_ref: str
    enabled: bool = True
    hold: bool = False

    def __post_init__(self):
        for value in (self.customer_id, self.environment_id, self.deployment_id):
            identifier(value)
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,253}", self.database_host):
            raise ValueError("database host must be an uncredentialed hostname")
        if (
            not isinstance(self.database_port, int)
            or not 1 <= self.database_port <= 65535
        ):
            raise ValueError("database port is required")
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,62}", self.database_name):
            raise ValueError("database name is required")
        for value in (self.web_credential_ref, self.worker_credential_ref):
            if not re.fullmatch(r"env:[A-Z][A-Z0-9_]{0,200}", value):
                raise ValueError("credential must be an environment variable reference")
        for value in (self.object_namespace_ref, self.connector_configuration_ref):
            reference(value)
        if type(self.enabled) is not bool or type(self.hold) is not bool:
            raise ValueError("enabled and hold must be booleans")


@dataclass(frozen=True)
class DestructionReceipt:
    receipt_id: str
    environment_id: str
    operation_id: str
    component: str
    outcome: str
    evidence_ref: str
    recorded_by: str
    observed_at: datetime

    def __post_init__(self):
        for value in (
            self.receipt_id,
            self.environment_id,
            self.operation_id,
            self.recorded_by,
        ):
            identifier(value)
        reference(self.evidence_ref)
        if self.component not in {
            "postgresql",
            "object_namespace",
            "backups",
            "encryption_key",
            "environment",
        }:
            raise ValueError("unknown destruction component")
        if self.outcome not in {"completed", "failed"}:
            raise ValueError("unknown destruction outcome")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("receipt observation needs an explicit timezone")


@dataclass(frozen=True)
class DispositionPlan:
    """The persisted subset of a whole-environment disposition dry run (#514).

    The full manifest (components, resolved precedence, referential state) is
    recomputed deterministically from the registration and declared inputs; only
    the digest, status, resolved retention and attribution are retained here so
    the executor can refuse a stale plan and resume a partial one.
    """

    plan_id: str
    environment_id: str
    manifest_sha256: str
    status: str
    resolved_retain_until: datetime | None
    created_by: str
    created_at: datetime
    provider_resources_sha256: str | None = None
    provider_resources: dict[str, Any] | None = None

    def __post_init__(self):
        for value in (self.plan_id, self.environment_id, self.created_by):
            identifier(value)
        if not re.fullmatch(r"[0-9a-f]{64}", self.manifest_sha256):
            raise ValueError("manifest digest must be a lowercase hex SHA-256")
        if self.provider_resources_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", self.provider_resources_sha256):
            raise ValueError("provider resource inventory requires a SHA-256")
        if self.status not in {"dry_run", "executed", "refused", "partial"}:
            raise ValueError("unknown disposition plan status")
        if self.resolved_retain_until is not None and (
            self.resolved_retain_until.tzinfo is None
            or self.resolved_retain_until.utcoffset() is None
        ):
            raise ValueError("resolved retention needs an explicit timezone")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("plan creation needs an explicit timezone")


class ControlPlane:
    """Operations use register/state/receipt; the resolver can only lookup."""

    def __init__(self, engine: Engine):
        if engine.dialect.name != "postgresql":
            raise ValueError("control plane requires PostgreSQL")
        self.engine = engine

    def register(
        self, registration: EnvironmentRegistration
    ) -> EnvironmentRegistration:
        with self.engine.begin() as connection:
            connection.execute(
                pg_insert(ENVIRONMENTS)
                .values(**asdict(registration))
                .on_conflict_do_nothing()
            )
            row = (
                connection.execute(
                    select(ENVIRONMENTS).where(
                        ENVIRONMENTS.c.environment_id == registration.environment_id
                    )
                )
                .mappings()
                .first()
            )
            if row is None or dict(row) != asdict(registration):
                raise ValueError(
                    "registration conflicts with an existing customer environment"
                )
        return registration

    def inspect(self, environment_id: str) -> EnvironmentRegistration:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(ENVIRONMENTS).where(
                        ENVIRONMENTS.c.environment_id == identifier(environment_id)
                    )
                )
                .mappings()
                .first()
            )
        if row is None:
            raise RouteRefused("customer environment unavailable")
        return EnvironmentRegistration(**row)

    def lookup(
        self, customer_id: str, environment_id: str, deployment_id: str
    ) -> EnvironmentRegistration:
        try:
            parameters = dict(
                customer=identifier(customer_id),
                environment=identifier(environment_id),
                deployment=identifier(deployment_id),
            )
            with self.engine.connect() as connection:
                row = (
                    connection.execute(
                        text(
                            "select * from control_plane.resolve_environment(:customer,:environment,:deployment)"
                        ),
                        parameters,
                    )
                    .mappings()
                    .first()
                )
            if row is None:
                raise RouteRefused("customer environment unavailable")
            return EnvironmentRegistration(**row)
        except (SQLAlchemyError, ValueError):
            # Driver errors can carry connection strings and SQL parameters.
            raise RouteRefused("customer environment unavailable") from None

    def set_state(
        self,
        environment_id: str,
        *,
        enabled: bool,
        hold: bool,
        connector_configuration_ref: str | None = None,
    ) -> None:
        if type(enabled) is not bool or type(hold) is not bool:
            raise ValueError("enabled and hold must be booleans")
        values: dict[str, bool | str] = dict(enabled=enabled, hold=hold)
        if connector_configuration_ref is not None:
            values["connector_configuration_ref"] = reference(
                connector_configuration_ref
            )
        with self.engine.begin() as connection:
            result = connection.execute(
                update(ENVIRONMENTS)
                .where(ENVIRONMENTS.c.environment_id == identifier(environment_id))
                .values(**values)
            )
            if result.rowcount != 1:
                raise RouteRefused("customer environment unavailable")

    def record_destruction(self, receipt: DestructionReceipt) -> DestructionReceipt:
        """Retain an observed outcome, never authorize or execute destruction."""
        with self.engine.begin() as connection:
            connection.execute(
                pg_insert(DESTRUCTION_RECEIPTS)
                .values(**asdict(receipt))
                .on_conflict_do_nothing()
            )
            row = (
                connection.execute(
                    select(DESTRUCTION_RECEIPTS).where(
                        DESTRUCTION_RECEIPTS.c.receipt_id == receipt.receipt_id
                    )
                )
                .mappings()
                .one()
            )
            if dict(row) != asdict(receipt):
                raise ValueError(
                    "destruction receipt identity was already used for a different observation"
                )
        return receipt

    def destruction_receipts(
        self, environment_id: str
    ) -> tuple[DestructionReceipt, ...]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(DESTRUCTION_RECEIPTS)
                .where(
                    DESTRUCTION_RECEIPTS.c.environment_id == identifier(environment_id)
                )
                .order_by(
                    DESTRUCTION_RECEIPTS.c.observed_at,
                    DESTRUCTION_RECEIPTS.c.receipt_id,
                )
            ).mappings()
            return tuple(DestructionReceipt(**row) for row in rows)

    def record_disposition_rehearsal(self, *, receipt_id, environment_id, operation_id,
                                    phase, outcome, evidence, observed_at):
        """Append provider observations outside the customer database."""
        import json
        for value in (receipt_id, environment_id, operation_id):
            identifier(value)
        if phase not in {"restore", "state_verification", "cleanup", "backup_expiration", "hold_cancellation", "execution_boundary"}:
            raise ValueError("unknown rehearsal phase")
        if outcome not in {"pending", "completed", "refused"}:
            raise ValueError("unknown rehearsal outcome")
        if observed_at.tzinfo is None or len(json.dumps(evidence)) > 65536:
            raise ValueError("rehearsal needs a bounded observation and explicit timezone")
        values = dict(receipt_id=receipt_id, environment_id=environment_id, operation_id=operation_id,
                      phase=phase, outcome=outcome, evidence=evidence, observed_at=observed_at)
        with self.engine.begin() as connection:
            connection.execute(pg_insert(DISPOSITION_REHEARSAL_RECEIPTS).values(**values).on_conflict_do_nothing())
            row = connection.execute(select(DISPOSITION_REHEARSAL_RECEIPTS).where(
                DISPOSITION_REHEARSAL_RECEIPTS.c.receipt_id == receipt_id)).mappings().one()
            if dict(row) != values:
                raise ValueError("rehearsal receipt identity already has another observation")
        return values

    def disposition_rehearsal_receipts(self, environment_id, operation_id):
        with self.engine.connect() as connection:
            return tuple(dict(row) for row in connection.execute(select(DISPOSITION_REHEARSAL_RECEIPTS).where(
                DISPOSITION_REHEARSAL_RECEIPTS.c.environment_id == identifier(environment_id),
                DISPOSITION_REHEARSAL_RECEIPTS.c.operation_id == identifier(operation_id),
            ).order_by(DISPOSITION_REHEARSAL_RECEIPTS.c.observed_at,
                       DISPOSITION_REHEARSAL_RECEIPTS.c.receipt_id)).mappings())

    def record_disposition_plan(self, plan: DispositionPlan) -> DispositionPlan:
        """Retain one dry-run plan; re-recording the same identity is idempotent."""
        with self.engine.begin() as connection:
            connection.execute(
                pg_insert(DISPOSITION_PLANS)
                .values(**asdict(plan))
                .on_conflict_do_nothing()
            )
            row = (
                connection.execute(
                    select(DISPOSITION_PLANS).where(
                        DISPOSITION_PLANS.c.plan_id == plan.plan_id
                    )
                )
                .mappings()
                .one()
            )
            if dict(row) != asdict(plan):
                raise ValueError(
                    "disposition plan identity was already used for a different plan"
                )
        return plan

    def disposition_plan(self, plan_id: str) -> DispositionPlan | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(DISPOSITION_PLANS).where(
                        DISPOSITION_PLANS.c.plan_id == identifier(plan_id)
                    )
                )
                .mappings()
                .first()
            )
        return None if row is None else DispositionPlan(**row)

    def disposition_plans(self, environment_id: str) -> tuple[DispositionPlan, ...]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(DISPOSITION_PLANS)
                .where(
                    DISPOSITION_PLANS.c.environment_id == identifier(environment_id)
                )
                .order_by(DISPOSITION_PLANS.c.created_at, DISPOSITION_PLANS.c.plan_id)
            ).mappings()
            return tuple(DispositionPlan(**row) for row in rows)

    def set_disposition_plan_status(self, plan_id: str, status: str) -> None:
        """Advance the one mutable column; the plan's identity stays fixed."""
        if status not in {"dry_run", "executed", "refused", "partial"}:
            raise ValueError("unknown disposition plan status")
        with self.engine.begin() as connection:
            result = connection.execute(
                update(DISPOSITION_PLANS)
                .where(DISPOSITION_PLANS.c.plan_id == identifier(plan_id))
                .values(status=status)
            )
            if result.rowcount != 1:
                raise RouteRefused("disposition plan unavailable")
