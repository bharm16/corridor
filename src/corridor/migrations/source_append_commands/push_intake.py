"""Project-bound push intake (#511).

Folded into this transition for the same window reason as the blocks above.
ADR-0078 replaced ADR-0059's one global address, whose project was inferred
from the message body, with a binding declared before the bytes arrive;
ADR-0083 named the push half of that design.  These two tables are what a
binding needs to exist before anything is parsed: the credential registry an
alias or webhook resolves through, and the delivery ledger that makes a
replay idempotent by delivery identity.

The registry stores a one-way digest of the credential material and never
the material: ADR-0079 as amended keeps connector credentials in the control
plane, and recognizing a presented credential needs nothing more.  Neither
table is updatable by a runtime capability except to revoke a credential, so
an alias cannot be re-pointed at another project by the application.

`inbound_messages` also loses its two cross-project unique constraints.  The
same bytes and the same Message-ID delivered to two different projects are
two deliveries, not a duplicate: a global constraint would either hand one
customer's stored message back to another customer's alias, or let a guessed
Message-ID refuse a delivery in a project the sender cannot see.  Identity is
scoped to the boundary, exactly as ADR-0078 scopes thread identity.
"""

from __future__ import annotations

from corridor.migrations.source_append_commands.roles import RUNTIME_LOGINS


PUSH_INTAKE_SCHEMA = """
create table public.push_intake_credentials (
    id bigserial primary key,
    customer text not null,
    project_id bigint not null references public.projects(id),
    channel character varying(32) not null,
    credential_sha256 character varying(64) not null,
    state character varying(16) not null default 'active',
    created_at timestamp with time zone not null default now(),
    constraint uq_push_intake_credential_digest unique (credential_sha256),
    constraint ck_push_intake_credential_customer
        check (length(btrim(customer)) > 0),
    constraint ck_push_intake_credential_channel
        check (channel in ('project_alias', 'shared_mailbox', 'webhook')),
    constraint ck_push_intake_credential_state
        check (state in ('active', 'revoked')),
    constraint ck_push_intake_credential_digest
        check (credential_sha256 ~ '^[0-9a-f]{64}$')
);

create index ix_push_intake_credentials_project_id
    on public.push_intake_credentials (project_id);

create table public.push_deliveries (
    id bigserial primary key,
    credential_id bigint not null
        references public.push_intake_credentials(id),
    customer text not null,
    project_id bigint not null references public.projects(id),
    channel character varying(32) not null,
    external_identity text not null,
    external_version text not null,
    original_timestamps_json jsonb not null default '{}'::jsonb,
    content_sha256 character varying(64) not null,
    bytes_reference text not null,
    metadata_json jsonb not null default '{}'::jsonb,
    delivery_identity character varying(64) not null,
    idempotency_key character varying(64) not null,
    received_at timestamp with time zone not null default now(),
    constraint uq_push_delivery_idempotency unique (idempotency_key),
    constraint ck_push_delivery_content_sha256
        check (content_sha256 ~ '^[0-9a-f]{64}$'),
    constraint ck_push_delivery_identity
        check (delivery_identity ~ '^[0-9a-f]{64}$'),
    constraint ck_push_delivery_idempotency
        check (idempotency_key ~ '^[0-9a-f]{64}$')
);

create index ix_push_deliveries_project_id
    on public.push_deliveries (project_id);

alter table public.inbound_messages
    add column push_delivery_id bigint references public.push_deliveries(id);

alter table public.inbound_messages
    add constraint uq_inbound_message_push_delivery unique (push_delivery_id);

alter table public.inbound_messages
    drop constraint inbound_messages_raw_sha256_key;

alter table public.inbound_messages
    add constraint uq_inbound_message_project_bytes
    unique (project_id, raw_sha256);

alter table public.inbound_messages
    drop constraint uq_inbound_message_message_id;

alter table public.inbound_messages
    add constraint uq_inbound_message_project_message_id
    unique (project_id, message_id);
"""

PUSH_INTAKE_SCHEMA_DOWN = """
alter table public.inbound_messages
    drop constraint if exists uq_inbound_message_project_message_id;

alter table public.inbound_messages
    add constraint uq_inbound_message_message_id unique (message_id);

alter table public.inbound_messages
    drop constraint if exists uq_inbound_message_project_bytes;

alter table public.inbound_messages
    add constraint inbound_messages_raw_sha256_key unique (raw_sha256);

alter table public.inbound_messages
    drop constraint if exists uq_inbound_message_push_delivery;

alter table public.inbound_messages drop column if exists push_delivery_id;

drop table if exists public.push_deliveries;

drop table if exists public.push_intake_credentials;
"""

PUSH_INTAKE_TABLES = ("push_intake_credentials", "push_deliveries")


def upgrade(op) -> None:
    op.execute(PUSH_INTAKE_SCHEMA)
    for table in PUSH_INTAKE_TABLES:
        # The binding registry and the delivery ledger are append-only to the
        # application: a credential may be revoked and nothing else, so no
        # runtime capability can re-point an alias at another project or erase
        # the record of a delivery it already took.  The privileges are
        # written out rather than left to the schema's defaults, because the
        # revocation is the point.
        op.execute(f"grant select, insert on public.{table} to {RUNTIME_LOGINS}")
        op.execute(
            f"grant usage, select on sequence public.{table}_id_seq "
            f"to {RUNTIME_LOGINS}"
        )
        op.execute(
            f"revoke update, delete, truncate on public.{table} "
            f"from {RUNTIME_LOGINS}"
        )
    op.execute(
        "grant update (state) on public.push_intake_credentials to corridor_web"
    )


def downgrade(op) -> None:
    op.execute(PUSH_INTAKE_SCHEMA_DOWN)
