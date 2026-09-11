"""Install the separate operations database, never the customer schema (#656).

The earlier identity implementation kept every receipt in the customer database.
Whole-environment destruction therefore needs a different database and metadata
root. This bootstrap owns exactly the registry, the immutable external receipts,
and the disposition plans that produce them (#514); it refuses a database already
containing any other application relation.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    inspect,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError


CONTROL_PLANE_METADATA = MetaData(schema="control_plane")
ENVIRONMENTS = Table(
    "customer_environments",
    CONTROL_PLANE_METADATA,
    Column("environment_id", String(128), primary_key=True),
    Column("customer_id", String(128), nullable=False),
    Column("deployment_id", String(128), nullable=False),
    Column("database_host", String(253), nullable=False),
    Column("database_port", Integer, nullable=False),
    Column("database_name", String(63), nullable=False),
    Column("web_credential_ref", String(256), nullable=False),
    Column("worker_credential_ref", String(256), nullable=False),
    Column("object_namespace_ref", String(512), nullable=False),
    Column("connector_configuration_ref", String(512), nullable=False),
    Column("enabled", Boolean, nullable=False),
    Column("hold", Boolean, nullable=False),
    UniqueConstraint("customer_id", "deployment_id"),
    UniqueConstraint("database_host", "database_port", "database_name"),
    CheckConstraint("database_port between 1 and 65535"),
    CheckConstraint("web_credential_ref ~ '^env:[A-Z][A-Z0-9_]*$'"),
    CheckConstraint("worker_credential_ref ~ '^env:[A-Z][A-Z0-9_]*$'"),
)
DESTRUCTION_RECEIPTS = Table(
    "destruction_receipts",
    CONTROL_PLANE_METADATA,
    Column("receipt_id", String(128), primary_key=True),
    Column(
        "environment_id",
        ForeignKey("control_plane.customer_environments.environment_id"),
        nullable=False,
    ),
    Column("operation_id", String(128), nullable=False),
    Column("component", String(32), nullable=False),
    Column("outcome", String(16), nullable=False),
    Column("evidence_ref", String(512), nullable=False),
    Column("recorded_by", String(128), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "component in ('postgresql', 'object_namespace', 'backups', 'encryption_key', 'environment')"
    ),
    CheckConstraint("outcome in ('completed', 'failed')"),
)
# The dry-run disposition plan (#514, ADR-0080/0083). It records the whole
# -environment manifest digest and its resolved retention precedence so the
# executor can refuse a stale plan and resume a partial one. It carries no
# customer content: the components it destroys are whole-environment units, and
# the removed digests live on the destruction receipts, not here.
DISPOSITION_PLANS = Table(
    "disposition_plans",
    CONTROL_PLANE_METADATA,
    Column("plan_id", String(128), primary_key=True),
    Column(
        "environment_id",
        ForeignKey("control_plane.customer_environments.environment_id"),
        nullable=False,
    ),
    Column("manifest_sha256", String(64), nullable=False),
    Column("provider_resources_sha256", String(64), nullable=True),
    Column("provider_resources", JSON, nullable=True),
    Column("status", String(16), nullable=False),
    Column("resolved_retain_until", DateTime(timezone=True), nullable=True),
    Column("created_by", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "status in ('dry_run', 'executed', 'refused', 'partial')"
    ),
)

DISPOSITION_REHEARSAL_RECEIPTS = Table(
    "disposition_rehearsal_receipts", CONTROL_PLANE_METADATA,
    Column("receipt_id", String(128), primary_key=True),
    Column("environment_id", ForeignKey("control_plane.customer_environments.environment_id"), nullable=False),
    Column("operation_id", String(128), nullable=False),
    Column("phase", String(32), nullable=False),
    Column("outcome", String(16), nullable=False),
    Column("evidence", JSON, nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("phase in ('restore', 'state_verification', 'cleanup', 'backup_expiration', 'hold_cancellation', 'execution_boundary')"),
    CheckConstraint("outcome in ('pending', 'completed', 'refused')"),
)

# --- #827 The limited onboarding authorization (ADR-0099) -------------------
#
# ADR-0099 makes the control plane authoritative for the permission onboarding
# runs under, and ADR-0083's control-plane clause is why it lives here rather
# than in the customer database: an authorization to process a named customer's
# data is cross-customer operations state, it has to stay legible after the
# customer environment is disposed of, and it must not put the customer's own
# workbook in a store that spans customers.
#
# **Identifiers and digests only.** The row names the governing customer
# authorization and the retained evidence that supports it, and carries neither
# the signed document nor any of the customer's source material. The preview a
# coordinator approved, the mapping and the adoption receipt stay in the
# customer environment, which is the split the two ADRs already draw.
#
# **Immutable, like every other control-plane record.** A reissue is a new row
# at a higher version; what happened to an authorization afterwards is an
# append-only event. `prevent_rewrite` holds both, so a withdrawal cannot be
# edited into never having been requested.
ONBOARDING_AUTHORIZATIONS = Table(
    "onboarding_authorizations",
    CONTROL_PLANE_METADATA,
    Column("authorization_id", String(128), primary_key=True),
    Column("version", Integer, primary_key=True),
    Column(
        "environment_id",
        ForeignKey("control_plane.customer_environments.environment_id"),
        nullable=False,
    ),
    Column("customer_id", String(128), nullable=False),
    Column("project_slug", String(128), nullable=False),
    Column("permitted_operations", String(512), nullable=False),
    Column("source_scope", String(256), nullable=False),
    Column("governing_authorization_id", String(128), nullable=False),
    Column("governing_authorization_version", String(64), nullable=False),
    Column("evidence_ref", String(512), nullable=False),
    Column("evidence_sha256", String(64), nullable=False),
    Column("issued_by", String(128), nullable=False),
    Column("issued_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("version >= 1"),
    CheckConstraint("expires_at > issued_at"),
    CheckConstraint("evidence_sha256 ~ '^[0-9a-f]{64}$'"),
    CheckConstraint("length(btrim(permitted_operations)) > 0"),
)

# What happened to one authorization after it was issued. Withdrawal is three
# separate recorded facts on purpose: the customer's request, its enforcement in
# the customer environment, and an enforcement that failed. ADR-0099 refuses to
# let a request be described as fully enforced while the customer database can
# still exercise the grant, and that is only sayable if they are separate rows.
ONBOARDING_AUTHORIZATION_EVENTS = Table(
    "onboarding_authorization_events",
    CONTROL_PLANE_METADATA,
    Column("event_id", String(128), primary_key=True),
    Column("authorization_id", String(128), nullable=False),
    Column("version", Integer, nullable=False),
    Column("kind", String(48), nullable=False),
    Column("requested_by", String(256), nullable=True),
    Column("requested_at", DateTime(timezone=True), nullable=True),
    Column("executed_by", String(128), nullable=False),
    Column("executed_at", DateTime(timezone=True), nullable=False),
    Column("reason", String(1024), nullable=True),
    Column("detail", String(1024), nullable=True),
    CheckConstraint(
        "kind in ('revalidated', 'withdrawal_requested', 'withdrawal_enforced', "
        "'withdrawal_enforcement_failed', 'governing_authorization_superseded')"
    ),
    CheckConstraint(
        "kind <> 'withdrawal_requested' or (requested_by is not null "
        "and requested_at is not null and reason is not null)"
    ),
)

OPERATIONS_ROLE = "corridor_control_operations"
RESOLVER_ROLE = "corridor_control_resolver"


def _role_is_bounded(connection, role):
    return connection.scalar(text(
        "select not rolcanlogin and not rolsuper and not rolcreatedb "
        "and not rolcreaterole and not rolbypassrls "
        "from pg_catalog.pg_roles where rolname = :role"
    ), {"role": role})


def _ensure_control_plane_role(connection, role):
    """Converge on a concurrently created cluster role using fresh catalog state.

    Registries in separate databases share pg_authid. An IF NOT EXISTS lookup
    does not serialize their CREATE ROLE statements. Roll back only the failed
    savepoint, classify the exact provider diagnostic, then verify the winner's
    role before allowing the current database bootstrap to continue.
    """
    if role not in {OPERATIONS_ROLE, RESOLVER_ROLE}:
        raise ValueError("unknown control-plane role")
    bounded = _role_is_bounded(connection, role)
    if bounded is not None:
        if not bounded:
            raise ValueError("existing control-plane role has incompatible capabilities")
        return
    savepoint = connection.begin_nested()
    try:
        connection.execute(text(f"create role {role} nologin nosuperuser nocreatedb nocreaterole nobypassrls"))
    except DBAPIError as error:
        savepoint.rollback()
        original = error.orig
        code = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
        constraint = getattr(getattr(original, "diag", None), "constraint_name", None)
        collision = code == "42710" or (code == "23505" and constraint == "pg_authid_rolname_index")
        if not collision or _role_is_bounded(connection, role) is not True:
            raise
    else:
        savepoint.commit()


def initialize_control_plane(engine: Engine) -> None:
    """Bootstrap through an explicitly supplied schema-owner connection.

    Roles are NOLOGIN capabilities: deployment provisions distinct logins and
    grants the appropriate one. No development password is installed here.
    Re-running checks the existing inventory and reapplies the same grants.
    """
    if engine.dialect.name != "postgresql":
        raise ValueError("control plane requires separate PostgreSQL")
    with engine.begin() as connection:
        inspector = inspect(connection)
        for schema in inspector.get_schema_names():
            if schema in {"information_schema", "pg_catalog"} or schema.startswith(
                "pg_"
            ):
                continue
            allowed = (
                {
                    "customer_environments",
                    "destruction_receipts",
                    "disposition_plans",
                    "disposition_rehearsal_receipts",
                    "onboarding_authorizations",
                    "onboarding_authorization_events",
                }
                if schema == "control_plane"
                else set()
            )
            relations = set(inspector.get_table_names(schema=schema)) | set(
                inspector.get_view_names(schema=schema)
            )
            if relations - allowed:
                raise ValueError(
                    "control plane refuses a database containing customer or unrelated relations"
                )
        connection.execute(text("create schema if not exists control_plane"))
        CONTROL_PLANE_METADATA.create_all(connection)
        # Additive control-plane upgrade; customer Alembic owns no table here.
        connection.execute(text("alter table control_plane.disposition_plans add column if not exists provider_resources_sha256 varchar(64)"))
        connection.execute(text("alter table control_plane.disposition_plans add column if not exists provider_resources json"))
        for role in (OPERATIONS_ROLE, RESOLVER_ROLE):
            _ensure_control_plane_role(connection, role)
        database = connection.dialect.identifier_preparer.quote(
            connection.scalar(text("select current_database()"))
        )
        connection.execute(text(f"revoke connect on database {database} from public"))
        connection.execute(
            text(
                f"grant connect on database {database} to {OPERATIONS_ROLE}, {RESOLVER_ROLE}"
            )
        )
        connection.execute(text("revoke create on schema public from public"))
        connection.execute(text("revoke all on schema control_plane from public"))
        connection.execute(
            text("revoke all on all tables in schema control_plane from public")
        )
        connection.execute(
            text(
                f"grant usage on schema control_plane to {OPERATIONS_ROLE}, {RESOLVER_ROLE}"
            )
        )
        connection.execute(
            text(
                f"grant select, insert on all tables in schema control_plane to {OPERATIONS_ROLE}"
            )
        )
        connection.execute(
            text(
                f"grant update (enabled, hold, connector_configuration_ref) on control_plane.customer_environments to {OPERATIONS_ROLE}"
            )
        )
        # A disposition plan is insert-once; only its status advances (dry_run ->
        # executed/partial/refused). Column-scoped update keeps the manifest
        # digest, retention and binding immutable after the dry run (#514).
        connection.execute(
            text(
                f"grant update (status) on control_plane.disposition_plans to {OPERATIONS_ROLE}"
            )
        )
        connection.execute(
            text("""
            create or replace function control_plane.prevent_rewrite() returns trigger
            language plpgsql set search_path = pg_catalog as $$
            begin raise exception 'control-plane history is immutable'; end $$;
            drop trigger if exists disposition_rehearsal_receipts_immutable on control_plane.disposition_rehearsal_receipts;
            create trigger disposition_rehearsal_receipts_immutable before update or delete on control_plane.disposition_rehearsal_receipts
                for each row execute function control_plane.prevent_rewrite();
            drop trigger if exists disposition_rehearsal_receipts_no_truncate on control_plane.disposition_rehearsal_receipts;
            create trigger disposition_rehearsal_receipts_no_truncate before truncate on control_plane.disposition_rehearsal_receipts
                for each statement execute function control_plane.prevent_rewrite();
            drop trigger if exists destruction_receipts_immutable on control_plane.destruction_receipts;
            create trigger destruction_receipts_immutable before update or delete on control_plane.destruction_receipts
                for each row execute function control_plane.prevent_rewrite();
            drop trigger if exists destruction_receipts_no_truncate on control_plane.destruction_receipts;
            create trigger destruction_receipts_no_truncate before truncate on control_plane.destruction_receipts
                for each statement execute function control_plane.prevent_rewrite();
            drop trigger if exists onboarding_authorizations_immutable on control_plane.onboarding_authorizations;
            create trigger onboarding_authorizations_immutable before update or delete on control_plane.onboarding_authorizations
                for each row execute function control_plane.prevent_rewrite();
            drop trigger if exists onboarding_authorizations_no_truncate on control_plane.onboarding_authorizations;
            create trigger onboarding_authorizations_no_truncate before truncate on control_plane.onboarding_authorizations
                for each statement execute function control_plane.prevent_rewrite();
            drop trigger if exists onboarding_authorization_events_immutable on control_plane.onboarding_authorization_events;
            create trigger onboarding_authorization_events_immutable before update or delete on control_plane.onboarding_authorization_events
                for each row execute function control_plane.prevent_rewrite();
            drop trigger if exists onboarding_authorization_events_no_truncate on control_plane.onboarding_authorization_events;
            create trigger onboarding_authorization_events_no_truncate before truncate on control_plane.onboarding_authorization_events
                for each statement execute function control_plane.prevent_rewrite();
            drop trigger if exists customer_environments_no_delete on control_plane.customer_environments;
            create trigger customer_environments_no_delete before delete on control_plane.customer_environments
                for each row execute function control_plane.prevent_rewrite();
            drop trigger if exists customer_environments_no_truncate on control_plane.customer_environments;
            create trigger customer_environments_no_truncate before truncate on control_plane.customer_environments
                for each statement execute function control_plane.prevent_rewrite();
            create or replace function control_plane.preserve_binding() returns trigger
            language plpgsql set search_path = pg_catalog as $$
            begin
                if (to_jsonb(new) - array['enabled','hold','connector_configuration_ref'])
                    is distinct from (to_jsonb(old) - array['enabled','hold','connector_configuration_ref']) then
                    raise exception 'customer environment binding is immutable';
                end if;
                return new;
            end $$;
            drop trigger if exists customer_environments_immutable_binding on control_plane.customer_environments;
            create trigger customer_environments_immutable_binding before update on control_plane.customer_environments
                for each row execute function control_plane.preserve_binding();
            create or replace function control_plane.resolve_environment(customer text, environment text, deployment text)
                returns setof control_plane.customer_environments
                language sql stable security definer set search_path = pg_catalog, control_plane as $$
                select * from control_plane.customer_environments
                where customer_id = customer and environment_id = environment
                    and deployment_id = deployment and enabled;
            $$;
            revoke all on all functions in schema control_plane from public;
        """)
        )
        connection.execute(
            text(
                f"grant execute on function control_plane.resolve_environment(text,text,text) to {RESOLVER_ROLE}, {OPERATIONS_ROLE}"
            )
        )
