"""The authorized source bindings a project may take delivery on (#886).

Folded into this transition for the same window reason as the blocks around
it: ``corridor.migrations.policy`` allows one unreleased transition and this is
it.

``activation_runtime.require_source_delivery`` compared a delivery's channel
and configuration identity against the **single** ``source_channel`` /
``source_configuration`` pair on the activation configuration.  One pair is one
channel, so a deployment activated for a project alias refused a product upload
(#823) even though the delivery was valid and the database accepted it -- and a
measured pilot needs both, because #845 requires at least one genuine
project-connected channel and ADR-0058 keeps manual upload as the explicit rare
fallback.

The fix is not "``product_upload`` is also allowed".  A manual upload is a
delivery somebody hands over; whether this project accepts one is exactly as
much an authorization question as whether it accepts a connector's.  So what
replaces the pair is a **versioned set of authorized source bindings**, and
every binding in it -- the upload included -- is there because somebody
recorded it.

**Two relations, and the same shape #827 built an hour earlier.**
``project_onboarding_grants`` is a per-project, immutable, versioned record
written only by a restricted operations actor, read through one
``SECURITY DEFINER`` standing function so a Python reader and the database can
never disagree, superseded by recording a higher version, and readable as
history afterwards.  This is that, for source bindings, deliberately rather
than a second authorization format beside it:

``project_source_authorizations`` is one recorded version of the set, with the
governing customer authorization it was issued under -- the same two columns
``project_onboarding_grants`` carries, so one governing authorization is named
the same way by both records -- and ``binding_set_sha256``, the canonical
digest of the bindings that version carries.

``project_source_authorization_bindings`` is the set itself, one row per
binding: the channel, the configuration identity and version, the source
classes that binding permits, and the authentication mode that applies to it.
Several rows may name the same channel, which is how a project that takes
delivery from two folders on one connector says so; the key is the whole
``(channel, configuration identity, configuration version)`` triple.

**The digest is the database's, not a number the caller hands it.**
``record_project_source_authorization`` derives ``binding_set_sha256`` from the
rows it is about to write, in one stated construction: a JSON array of the
bindings, each with its source classes sorted and de-duplicated, the array
ordered by the binding key.  Nothing in Python recomputes it -- it is read
back, never re-derived -- so the encoding exists once and there is no second
one to disagree with (#457, and the same reason ``corridor.digests`` exists).

**Revocation is a version, not a flag.**  Recording a higher version whose set
omits a binding withdraws that binding; recording one whose set is empty
withdraws them all.  Either way the predecessor row and its bindings stay
exactly as recorded, every delivery already recorded under them stays readable,
and what stops is *new* deliveries.  That is ADR-0099's shape as #827 built it
-- "no ``consumed`` column", the state derived from the retained record -- and
it is why there is no event relation here: the attributable act is the new
version, with its issuing and recording actors and its instants on the row.

**What is deliberately not here.**  No expiry and no revalidation window: a
source authorization is a deployment-authority decision an operator changes by
recording a new version, not a lease, and #827's window exists because
ADR-0099 requires one for protected onboarding *writes*.  No per-delivery
comparison of ``permitted_source_classes``: a ``DeliveryBinding`` carries no
source class, and inventing one so this column could be compared would put a
guess in the ledger.  The column records what the authorization permits, is
covered by the digest, and is read back beside the binding.

The downgrade drops both relations whole and refuses where an authorization
survives, because the supported predecessor has no shape that could carry a
recorded set of bindings.
"""

from __future__ import annotations

import sqlalchemy as sa

from corridor.migrations.source_append_commands.project_partition import (
    _UNPARTITIONED_ROLES_SQL,
)
from corridor.migrations.source_append_commands.roles import (
    RECORD_DECISION_ROLE,
    RUNTIME_LOGINS,
)

AUTHORIZATION_TABLE = "project_source_authorizations"
BINDING_TABLE = "project_source_authorization_bindings"

SOURCE_AUTHORIZATION_TABLES = (AUTHORIZATION_TABLE, BINDING_TABLE)

#: How the transport that carried a delivery authenticated it, which is the
#: fifth thing an authorized binding names.  The three are #823's own account
#: of the delivery family: a pulled delivery "is bound by its connector
#: configuration and authenticates neither way", and a pushed one names "a
#: machine credential, or the person who handed it over".
AUTHENTICATION_MODES = (
    "connector_configuration",
    "machine_credential",
    "human_principal",
)

_AUTHENTICATION_MODES_SQL = ", ".join(f"'{mode}'" for mode in AUTHENTICATION_MODES)


SOURCE_AUTHORIZATION_SCHEMA = f"""
create table public.{AUTHORIZATION_TABLE} (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    authorization_identity character varying(128) not null,
    authorization_version integer not null,
    customer character varying(128) not null,
    environment character varying(128) not null,
    governing_authorization_identity character varying(128) not null,
    governing_authorization_version character varying(64) not null,
    binding_set_sha256 character varying(64) not null,
    issued_at timestamp with time zone not null,
    issued_by_actor character varying(128) not null,
    recorded_by_actor character varying(128) not null,
    recorded_at timestamp with time zone not null default now(),
    constraint uq_{AUTHORIZATION_TABLE}_project_id unique (project_id, id),
    constraint uq_{AUTHORIZATION_TABLE}_version
        unique (project_id, authorization_identity, authorization_version),
    constraint ck_{AUTHORIZATION_TABLE}_version
        check (authorization_version >= 1),
    constraint ck_{AUTHORIZATION_TABLE}_digest
        check (binding_set_sha256 ~ '^[0-9a-f]{{64}}$'),
    constraint ck_{AUTHORIZATION_TABLE}_actors check (
        length(btrim(authorization_identity)) > 0
        and length(btrim(customer)) > 0
        and length(btrim(environment)) > 0
        and length(btrim(governing_authorization_identity)) > 0
        and length(btrim(governing_authorization_version)) > 0
        and length(btrim(issued_by_actor)) > 0
        and length(btrim(recorded_by_actor)) > 0
    )
);

create index ix_{AUTHORIZATION_TABLE}_project_id
    on public.{AUTHORIZATION_TABLE} (project_id);

create table public.{BINDING_TABLE} (
    id bigserial primary key,
    project_id bigint not null,
    authorization_id bigint not null,
    channel character varying(32) not null,
    configuration_identity text not null,
    configuration_version text not null default '',
    permitted_source_classes text[] not null,
    authentication_mode character varying(32) not null,
    constraint uq_{BINDING_TABLE}_project_id unique (project_id, id),
    constraint fk_{BINDING_TABLE}_authorization
        foreign key (project_id, authorization_id)
        references public.{AUTHORIZATION_TABLE} (project_id, id),
    constraint uq_{BINDING_TABLE}_selection unique
        (authorization_id, channel, configuration_identity, configuration_version),
    constraint ck_{BINDING_TABLE}_selection check (
        length(btrim(channel)) > 0
        and length(btrim(configuration_identity)) > 0
    ),
    constraint ck_{BINDING_TABLE}_mode
        check (authentication_mode in ({_AUTHENTICATION_MODES_SQL})),
    constraint ck_{BINDING_TABLE}_classes check (
        array_length(permitted_source_classes, 1) >= 1
        and array_position(permitted_source_classes, null) is null
        and array_position(permitted_source_classes, '') is null
    )
);

create index ix_{BINDING_TABLE}_authorization_id
    on public.{BINDING_TABLE} (authorization_id);
"""


SOURCE_AUTHORIZATION_GUARDS = f"""
create function public.enforce_source_authorization_write() returns trigger
    language plpgsql
    as $$
        begin
            if tg_op <> 'INSERT' then
                raise exception 'a recorded source authorization is immutable: withdrawing or widening one is a new version, never an edit of what somebody authorized'
                    using errcode='23514';
            end if;
            if current_user <> '{RECORD_DECISION_ROLE}' then
                raise exception 'source authorization records require their typed command'
                    using errcode='23514';
            end if;
            return new;
        end; $$;
"""

SOURCE_AUTHORIZATION_TRIGGERS = "\n".join(
    f"""
create trigger trg_{table}_write
    before insert or update or delete on public.{table}
    for each row execute function public.enforce_source_authorization_write();
create trigger trg_{table}_truncate
    before truncate on public.{table}
    for each statement execute function public.enforce_source_authorization_write();
"""
    for table in SOURCE_AUTHORIZATION_TABLES
)


# The one canonical form of a binding set, stated here and nowhere else.  The
# bindings are rendered as a JSON array ordered by the binding key, each source
# class list sorted and de-duplicated, and the digest is taken over that text.
# Python reads the result back and never recomputes it, so this encoding has no
# sibling that could disagree with it.
RECORD_PROJECT_SOURCE_AUTHORIZATION = f"""
create function public.record_project_source_authorization(
    p_project_id bigint,
    p_authorization_identity character varying,
    p_authorization_version integer,
    p_customer character varying,
    p_environment character varying,
    p_governing_identity character varying,
    p_governing_version character varying,
    p_bindings jsonb,
    p_issued_at timestamp with time zone,
    p_issued_by_actor character varying,
    p_recorded_by_actor character varying
) returns jsonb
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            prior {AUTHORIZATION_TABLE}%ROWTYPE;
            held {AUTHORIZATION_TABLE}%ROWTYPE;
            held_found boolean;
            normalized jsonb;
            digest character varying(64);
            new_authorization_id bigint;
            distinct_selections integer;
            submitted integer;
        begin
            if jsonb_typeof(p_bindings) <> 'array' then
                raise exception 'source_authorization:malformed_binding_set an authorized source binding set is a JSON array'
                    using errcode='23514';
            end if;

            select coalesce(
                       jsonb_agg(
                           jsonb_build_object(
                               'channel', item ->> 'channel',
                               'configuration_identity',
                                   item ->> 'configuration_identity',
                               'configuration_version',
                                   coalesce(item ->> 'configuration_version', ''),
                               'authentication_mode',
                                   item ->> 'authentication_mode',
                               'permitted_source_classes',
                                   to_jsonb((
                                       select array_agg(distinct entry order by entry)
                                         from jsonb_array_elements_text(
                                                  item -> 'permitted_source_classes'
                                              ) as entry
                                   ))
                           )
                           order by item ->> 'channel',
                                    item ->> 'configuration_identity',
                                    coalesce(item ->> 'configuration_version', '')
                       ),
                       '[]'::jsonb
                   )
              into normalized
              from jsonb_array_elements(p_bindings) as item;

            if exists (
                select 1 from jsonb_array_elements(normalized) as entry
                 where coalesce(btrim(entry ->> 'channel'), '') = ''
                    or coalesce(btrim(entry ->> 'configuration_identity'), '') = ''
                    or coalesce(btrim(entry ->> 'authentication_mode'), '') = ''
                    or jsonb_typeof(entry -> 'permitted_source_classes') <> 'array'
            ) then
                raise exception 'source_authorization:malformed_binding an authorized source binding names a channel, a configuration, an authentication mode and the source classes it permits'
                    using errcode='23514';
            end if;

            select count(*),
                   count(distinct (entry ->> 'channel',
                                   entry ->> 'configuration_identity',
                                   entry ->> 'configuration_version'))
              into submitted, distinct_selections
              from jsonb_array_elements(normalized) as entry;
            if submitted <> distinct_selections then
                raise exception 'source_authorization:duplicate_binding one channel, configuration and version is one binding, named once'
                    using errcode='23514';
            end if;

            digest := encode(sha256(convert_to(normalized::text, 'UTF8')), 'hex');

            select * into prior from {AUTHORIZATION_TABLE}
             where project_id = p_project_id
               and authorization_identity = p_authorization_identity
               and authorization_version = p_authorization_version;
            if found then
                if prior.binding_set_sha256 <> digest
                   or prior.customer <> p_customer
                   or prior.environment <> p_environment
                   or prior.governing_authorization_identity
                       is distinct from p_governing_identity
                   or prior.governing_authorization_version
                       is distinct from p_governing_version then
                    raise exception 'source_authorization:version_bound_to_other_terms this source authorization version already names a different set; record a higher version'
                        using errcode='23514';
                end if;
                return jsonb_build_object(
                    'authorization_id', prior.id, 'created', false,
                    'binding_set_sha256', prior.binding_set_sha256
                );
            end if;

            select * into held from {AUTHORIZATION_TABLE}
             where project_id = p_project_id
             order by authorization_version desc, id desc
             limit 1;
            held_found := found;
            if held_found
               and held.authorization_identity <> p_authorization_identity then
                raise exception 'source_authorization:authorization_identity_differs this project already records its authorized source bindings under another identity'
                    using errcode='23514';
            end if;
            if held_found
               and held.authorization_version >= p_authorization_version then
                raise exception 'source_authorization:version_went_backwards a later version of this source authorization is already recorded'
                    using errcode='23514';
            end if;

            insert into {AUTHORIZATION_TABLE} (
                project_id, authorization_identity, authorization_version,
                customer, environment, governing_authorization_identity,
                governing_authorization_version, binding_set_sha256,
                issued_at, issued_by_actor, recorded_by_actor
            ) values (
                p_project_id, p_authorization_identity, p_authorization_version,
                p_customer, p_environment, p_governing_identity,
                p_governing_version, digest, p_issued_at, p_issued_by_actor,
                p_recorded_by_actor
            ) returning id into new_authorization_id;

            insert into {BINDING_TABLE} (
                project_id, authorization_id, channel, configuration_identity,
                configuration_version, permitted_source_classes,
                authentication_mode
            )
            select p_project_id, new_authorization_id,
                   entry ->> 'channel', entry ->> 'configuration_identity',
                   entry ->> 'configuration_version',
                   (
                       select array_agg(item order by ordinality)
                         from jsonb_array_elements_text(
                                  entry -> 'permitted_source_classes'
                              ) with ordinality as classes(item, ordinality)
                   ),
                   entry ->> 'authentication_mode'
              from jsonb_array_elements(normalized) as entry;

            return jsonb_build_object(
                'authorization_id', new_authorization_id, 'created', true,
                'binding_set_sha256', digest
            );
        end; $$;
"""

RECORD_PROJECT_SOURCE_AUTHORIZATION_SIGNATURE = (
    "(bigint, character varying, integer, character varying, "
    "character varying, character varying, character varying, jsonb, "
    "timestamp with time zone, character varying, character varying)"
)


# One reading of whether a delivery's own selection is authorized right now,
# used by the activation gate and by anything that wants to say why a delivery
# was refused.  It answers about the version *in force*; whether that version
# is the one this deployment was activated against is the activation receipt's
# question, and `corridor.activation_runtime` asks it with the three values
# this function returns.
SOURCE_BINDING_STANDING = f"""
create function public.source_binding_standing(
    p_project_id bigint,
    p_customer character varying,
    p_channel character varying,
    p_configuration_identity text,
    p_configuration_version text,
    p_authentication_mode character varying
) returns jsonb
    language plpgsql stable security definer
    set search_path to 'public'
    as $$
        declare
            held {AUTHORIZATION_TABLE}%ROWTYPE;
            binding {BINDING_TABLE}%ROWTYPE;
            recorded jsonb;
        begin
            select * into held from {AUTHORIZATION_TABLE}
             where project_id = p_project_id
             order by authorization_version desc, id desc
             limit 1;
            if not found then
                return jsonb_build_object(
                    'permitted', false, 'reason', 'no_source_authorization'
                );
            end if;

            recorded := jsonb_build_object(
                'authorization_id', held.id,
                'authorization_identity', held.authorization_identity,
                'authorization_version', held.authorization_version,
                'binding_set_sha256', held.binding_set_sha256,
                'governing_authorization_identity',
                    held.governing_authorization_identity,
                'governing_authorization_version',
                    held.governing_authorization_version
            );

            if held.customer <> p_customer then
                return recorded || jsonb_build_object(
                    'permitted', false,
                    'reason', 'source_authorization_names_another_customer'
                );
            end if;

            select * into binding from {BINDING_TABLE}
             where authorization_id = held.id
               and channel = p_channel
               and configuration_identity = p_configuration_identity
               and configuration_version = p_configuration_version;
            if not found then
                return recorded || jsonb_build_object(
                    'permitted', false,
                    'reason', 'source_binding_not_authorized'
                );
            end if;
            if binding.authentication_mode <> p_authentication_mode then
                return recorded || jsonb_build_object(
                    'permitted', false,
                    'reason', 'source_authentication_mode_not_authorized',
                    'authentication_mode', binding.authentication_mode,
                    'permitted_source_classes',
                        to_jsonb(binding.permitted_source_classes)
                );
            end if;

            return recorded || jsonb_build_object(
                'permitted', true,
                'binding_id', binding.id,
                'authentication_mode', binding.authentication_mode,
                'permitted_source_classes',
                    to_jsonb(binding.permitted_source_classes)
            );
        end; $$;
"""

SOURCE_BINDING_STANDING_SIGNATURE = (
    "(bigint, character varying, character varying, text, text, "
    "character varying)"
)


SOURCE_AUTHORIZATION_COMMANDS = {
    "record_project_source_authorization": (
        RECORD_PROJECT_SOURCE_AUTHORIZATION_SIGNATURE
    ),
    "source_binding_standing": SOURCE_BINDING_STANDING_SIGNATURE,
}

#: Who may execute what.  Recording the authorized set is the restricted
#: operations actor's, exactly as recording an onboarding grant is (#827): the
#: party whose sources are being taken delivery of is never the party that
#: permits the channel.  Reading the standing is both runtime capabilities',
#: because both take delivery -- the worker on a connector pass, the web
#: capability when a person hands a file over.
SOURCE_AUTHORIZATION_COMMAND_GRANTS = {
    "record_project_source_authorization": ("corridor_worker",),
    "source_binding_standing": ("corridor_web", "corridor_worker"),
}


SOURCE_AUTHORIZATION_PARTITION_POLICIES = "\n".join(
    f"""
do $$
declare
    v_roles text;
    v_table text := '{table}';
begin
    select string_agg(quote_ident(rolname), ', ' order by rolname)
      into v_roles
      from pg_roles
     where rolname in ({_UNPARTITIONED_ROLES_SQL});
    execute format(
        'alter table public.%I enable row level security', v_table
    );
    execute format(
        'create policy %I on public.%I for all to corridor_web '
        'using (project_id = any(public.current_project_partition())) '
        'with check (project_id = any(public.current_project_partition()))',
        'p_' || v_table || '_project_partition', v_table
    );
    if v_roles is not null then
        execute format(
            'create policy %I on public.%I for all to %s '
            'using (true) with check (true)',
            'p_' || v_table || '_unpartitioned', v_table, v_roles
        );
    end if;
end $$;
"""
    for table in SOURCE_AUTHORIZATION_TABLES
)


SOURCE_AUTHORIZATION_SCHEMA_DOWN = "\n".join(
    [
        *(
            f"drop function if exists public.{name}{signature};"
            for name, signature in SOURCE_AUTHORIZATION_COMMANDS.items()
        ),
        "drop function if exists public.enforce_source_authorization_write() cascade;",
        *(
            f"drop table if exists public.{table} cascade;"
            for table in reversed(SOURCE_AUTHORIZATION_TABLES)
        ),
    ]
)


def upgrade(op) -> None:
    # After `project_partition`, whose `current_project_partition` scopes both
    # relations, and after the delivery ledger blocks whose channels and
    # configurations a recorded binding names. Nothing later in the revision
    # names these relations.
    op.execute(SOURCE_AUTHORIZATION_SCHEMA)
    op.execute(SOURCE_AUTHORIZATION_GUARDS)
    op.execute(SOURCE_AUTHORIZATION_TRIGGERS)
    for table in SOURCE_AUTHORIZATION_TABLES:
        # A new table arrives with the schema owner's default privileges, which
        # hand every runtime login full access; the write half is taken back and
        # only the command's role keeps it.
        op.execute(f"revoke all on public.{table} from {RUNTIME_LOGINS}")
        op.execute(f"grant select on public.{table} to {RUNTIME_LOGINS}")
        op.execute(f"grant select, insert on public.{table} to {RECORD_DECISION_ROLE}")
        op.execute(
            f"grant usage, select on sequence public.{table}_id_seq "
            f"to {RECORD_DECISION_ROLE}"
        )
    op.execute(RECORD_PROJECT_SOURCE_AUTHORIZATION)
    op.execute(SOURCE_BINDING_STANDING)
    for name, signature in SOURCE_AUTHORIZATION_COMMANDS.items():
        op.execute(
            f"alter function public.{name}{signature} owner to {RECORD_DECISION_ROLE}"
        )
        op.execute(f"revoke all on function public.{name}{signature} from public")
        for login in SOURCE_AUTHORIZATION_COMMAND_GRANTS[name]:
            op.execute(f"grant execute on function public.{name}{signature} to {login}")
    op.execute(SOURCE_AUTHORIZATION_PARTITION_POLICIES)


def downgrade(op) -> None:
    # Before `project_partition` unwinds the function both policies name,
    # mirroring the upgrade's order. The supported predecessor has one channel
    # and one configuration on the activation configuration, which cannot carry
    # a recorded set, so refuse rather than drop the record silently.
    if op.get_bind().scalar(
        sa.text(f"select exists (select 1 from public.{AUTHORIZATION_TABLE})")
    ):
        raise RuntimeError(
            "recorded source authorizations cannot be represented by the "
            "supported predecessor"
        )
    for table in SOURCE_AUTHORIZATION_TABLES:
        op.execute(
            f"do $$ begin "
            f"if exists (select 1 from pg_tables where schemaname = 'public' "
            f"and tablename = '{table}') then "
            f"revoke select on public.{table} from {RUNTIME_LOGINS}; "
            f"end if; end $$;"
        )
    op.execute(SOURCE_AUTHORIZATION_SCHEMA_DOWN)
