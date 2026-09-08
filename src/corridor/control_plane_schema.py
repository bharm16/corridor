"""Install the separate operations database, never the customer schema (#656).

The earlier identity implementation kept every receipt in the customer database.
Whole-environment destruction therefore needs a different database and metadata
root. This bootstrap owns exactly the registry and immutable external receipts;
it refuses a database already containing any other application relation.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    inspect,
    text,
)
from sqlalchemy.engine import Engine


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

OPERATIONS_ROLE = "corridor_control_operations"
RESOLVER_ROLE = "corridor_control_resolver"


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
                {"customer_environments", "destruction_receipts"}
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
        for role in (OPERATIONS_ROLE, RESOLVER_ROLE):
            connection.execute(
                text(
                    f"do $$ begin if not exists (select 1 from pg_roles where rolname = '{role}') "
                    f"then create role {role} nologin nosuperuser nocreatedb nocreaterole nobypassrls; "
                    "end if; end $$"
                )
            )
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
        connection.execute(
            text("""
            create or replace function control_plane.prevent_rewrite() returns trigger
            language plpgsql set search_path = pg_catalog as $$
            begin raise exception 'control-plane history is immutable'; end $$;
            drop trigger if exists destruction_receipts_immutable on control_plane.destruction_receipts;
            create trigger destruction_receipts_immutable before update or delete on control_plane.destruction_receipts
                for each row execute function control_plane.prevent_rewrite();
            drop trigger if exists destruction_receipts_no_truncate on control_plane.destruction_receipts;
            create trigger destruction_receipts_no_truncate before truncate on control_plane.destruction_receipts
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
