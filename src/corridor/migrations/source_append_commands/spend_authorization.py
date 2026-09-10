"""One spend-authorization declaration the five assistance configurations reference (#811).

The Coordination Summary, the production-run explanation, the
extraction-failure diagnosis, the revision-change explanation and the
source-intake draft each spend model budget only under an attributable,
append-only configuration row.  The baseline gave each of the five its own
table, and each table carried the same nine columns under the same nine CHECK
constraints: the model, the input, output and time limits, one request, no
retry, the retention class, the observation context and the declaring actor.
Audit card C03 counted the copies.  Five tables saying the same sentence is
five places for the sentence to drift, and a reader who wants to know what a
project has authorized has five relations to read.

**``spend_authorizations``** is that sentence once.  One row is one person's
declaration for one project and one permitted operation, carrying the nine
facts and the nine checks, plus the operation itself and the moment it took
effect.  The relation is append-only in the same words the five predecessors
used: a changed bound is another declaration, never an edit.

**Each family keeps its table and gains the reference.**  The five
configuration tables lose the nine common columns and their checks and gain
``authorization_id`` and ``operation``.  The reference is a composite foreign
key on ``(authorization_id, project_id, operation)`` against a matching unique
key on the declaration, and ``operation`` on the family table is fixed by a
check to that family's own name, so a declaration made for an intake draft
cannot back a Coordination Summary configuration and a declaration for one
project cannot back a configuration in another.  ``authorization_id`` is
unique on every family table: one declaration backs one configuration, and
two independently declared acts whose limits match stay two declarations.
What each family keeps is its own — the prompt it runs and, for the summary,
the source scope it reads — with its request table, receipts and retention
untouched.

**The migration is not the author.**  Every existing configuration row
becomes a declaration carrying its original ``created_by`` as the declaring
actor and its original ``created_at`` as the moment it took effect; no
synthetic actor and no migration-time timestamp is written.  Ids are allocated
from the declaration's sequence in configuration-id order per family so each
configuration is bound to exactly the row made from it, without matching on
values that two rows might share.  The family relations' immutability guards
are disabled for the one statement that writes the reference and re-enabled
after it, as the observation binding did for its backfill.  The downgrade
restores the nine columns from the declaration each row names, under the
constraint names and expressions the baseline gave them, and refuses while a
declaration exists that no configuration references, because the supported
predecessor has no place for it.

**Grants and partition.**  The relation is project-scoped, so it answers the
partition the way the record tables do (#657): the policy name matches
``p_%_project_partition`` and it is recorded in
``access.PARTITIONED_RELATIONS``.  Every runtime login reads it; the worker
capability keeps the append the five predecessors already gave it, and the
web capability writes nothing here, as it writes nothing to the five
configurations this relation now backs.
"""

from __future__ import annotations

import sqlalchemy as sa

from corridor.migrations.source_append_commands.project_partition import (
    _UNPARTITIONED_ROLES_SQL,
)
from corridor.migrations.source_append_commands.roles import RUNTIME_LOGINS

AUTHORIZATION_TABLE = "spend_authorizations"

# The five family configuration relations, the operation each one is permitted,
# and the prefix the baseline gave that family's check constraints.
FAMILY_CONFIGURATIONS = (
    ("coordination_summary_configurations", "coordination_summary", "summary_config"),
    (
        "production_run_explanation_configurations",
        "production_run_explanation",
        "run_explanation_config",
    ),
    (
        "extraction_failure_diagnosis_configurations",
        "extraction_failure_diagnosis",
        "failure_diagnosis_config",
    ),
    (
        "revision_change_explanation_configurations",
        "revision_change_explanation",
        "rev_change_expl_cfg",
    ),
    ("source_intake_draft_configurations", "source_intake_draft", "intake_draft_config"),
)
_OPERATIONS_SQL = ", ".join(f"'{operation}'" for _t, operation, _p in FAMILY_CONFIGURATIONS)

# The nine common columns, in the order the baseline declared them on every
# family table, and the declaration column each one becomes.
COMMON_COLUMNS = (
    ("model", "model", "character varying(128)"),
    ("max_input_tokens", "max_input_tokens", "integer"),
    ("max_output_tokens", "max_output_tokens", "integer"),
    ("timeout_seconds", "timeout_seconds", "integer"),
    ("max_requests", "max_requests", "integer"),
    ("retry_policy", "retry_policy", "character varying(32)"),
    ("retention_policy", "retention_policy", "character varying(64)"),
    ("observation_context", "observation_context", "character varying(128)"),
    ("created_by", "declared_by", "character varying(128)"),
    ("created_at", "effective_from", "timestamp with time zone"),
)

# The nine checks, by the suffix every family's constraint name ends in, with
# the expression the baseline wrote for it. The declaration carries them under
# its own name; the downgrade restores them under each family's.
COMMON_CHECKS = (
    ("model", "length(btrim(model)) > 0"),
    ("input_budget", "max_input_tokens between 1 and 200000"),
    ("output_budget", "max_output_tokens between 1 and 20000"),
    ("timeout", "timeout_seconds between 1 and 600"),
    ("one_request", "max_requests = 1"),
    ("no_retry", "retry_policy = 'none'"),
    (
        "retention",
        "retention_policy in ('retained_indefinitely', 'class_b_30_days')",
    ),
    ("context", "length(btrim(observation_context)) > 0"),
)

SPEND_AUTHORIZATION_SCHEMA = f"""
create table public.spend_authorizations (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    -- The one operation this declaration permits. Every family configuration
    -- references the declaration through (id, project_id, operation), so a
    -- declaration cannot back a configuration of another family or project.
    operation character varying(48) not null,
    model character varying(128) not null,
    max_input_tokens integer not null,
    max_output_tokens integer not null,
    timeout_seconds integer not null,
    max_requests integer not null,
    retry_policy character varying(32) not null,
    retention_policy character varying(64) not null,
    observation_context character varying(128) not null,
    -- The person who declared it, and the moment the declaration took effect.
    declared_by character varying(128) not null,
    effective_from timestamp with time zone not null default now(),
    constraint ck_spend_authorization_operation
        check (operation in ({_OPERATIONS_SQL})),
    constraint ck_spend_authorization_model check (length(btrim(model)) > 0),
    constraint ck_spend_authorization_input_budget
        check (max_input_tokens between 1 and 200000),
    constraint ck_spend_authorization_output_budget
        check (max_output_tokens between 1 and 20000),
    constraint ck_spend_authorization_timeout
        check (timeout_seconds between 1 and 600),
    constraint ck_spend_authorization_one_request check (max_requests = 1),
    constraint ck_spend_authorization_no_retry check (retry_policy = 'none'),
    -- The baseline admitted the pre-#355 indefinite class on every family
    -- table so those rows could be carried; only class_b_30_days authorizes.
    constraint ck_spend_authorization_retention
        check (retention_policy in ('retained_indefinitely', 'class_b_30_days')),
    constraint ck_spend_authorization_context
        check (length(btrim(observation_context)) > 0),
    constraint ck_spend_authorization_actor
        check (length(btrim(declared_by)) > 0),
    constraint uq_spend_authorization_scope unique (id, project_id, operation)
);
create index ix_spend_authorizations_project_id
    on public.spend_authorizations (project_id);

create function public.reject_spend_authorizations_mutation() returns trigger
language plpgsql set search_path = pg_catalog as $$
begin
    raise exception 'a spend authorization is immutable: a changed bound is a new declaration, never an edit';
end $$;
revoke all on function public.reject_spend_authorizations_mutation() from public;
create trigger spend_authorizations_are_immutable
    before update or delete on public.spend_authorizations
    for each row execute function public.reject_spend_authorizations_mutation();
create trigger spend_authorizations_reject_truncate
    before truncate on public.spend_authorizations
    for each statement execute function public.reject_spend_authorizations_mutation();
"""

SPEND_AUTHORIZATION_PARTITION_POLICIES = f"""
do $$
declare
    v_roles text;
    v_table text := '{AUTHORIZATION_TABLE}';
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

SPEND_AUTHORIZATION_SCHEMA_DOWN = """
drop table public.spend_authorizations;
drop function public.reject_spend_authorizations_mutation();
"""


def _family_columns_sql() -> str:
    return ", ".join(family for family, _declaration, _type in COMMON_COLUMNS)


def _declaration_columns_sql() -> str:
    return ", ".join(declaration for _family, declaration, _type in COMMON_COLUMNS)


def _carry_declarations_forward(op, table: str, operation: str) -> None:
    """Make one declaration from each configuration row and bind the row to it.

    The declaration ids are drawn from the sequence in configuration-id order
    inside one statement, so each configuration is bound to exactly the row
    made from it; nothing is matched on values two rows might share, and the
    actor and the moment come across exactly as the row recorded them.
    """

    op.execute(
        f"""
        with numbered as (
            select f.id as configuration_id,
                   nextval('public.{AUTHORIZATION_TABLE}_id_seq') as authorization_id,
                   f.project_id, {_family_columns_sql()}
              from public.{table} f
             order by f.id
        ), declared as (
            insert into public.{AUTHORIZATION_TABLE}
                (id, project_id, operation, {_declaration_columns_sql()})
            select authorization_id, project_id, '{operation}',
                   {_family_columns_sql()}
              from numbered
            returning id
        )
        update public.{table} f
           set authorization_id = n.authorization_id,
               operation = '{operation}'
          from numbered n
         where f.id = n.configuration_id
        """
    )


def _carry_declarations_back(op, table: str) -> None:
    """Restore the nine columns on each configuration row from its declaration."""

    assignments = ", ".join(
        f"{family} = a.{declaration}"
        for family, declaration, _type in COMMON_COLUMNS
    )
    op.execute(
        f"""
        update public.{table} f
           set {assignments}
          from public.{AUTHORIZATION_TABLE} a
         where a.id = f.authorization_id
        """
    )


def upgrade(op) -> None:
    # After the observation binding and before the sibling transitions: it
    # alters the five configuration relations the baseline created and
    # creates one relation nothing later in the revision names.
    op.execute(SPEND_AUTHORIZATION_SCHEMA)
    for table, operation, prefix in FAMILY_CONFIGURATIONS:
        op.execute(
            f"alter table public.{table} "
            "add column authorization_id bigint, "
            "add column operation character varying(48)"
        )
        # The relation is append-only, and its guard would refuse the one
        # statement that binds each row to the declaration made from it.
        # Binding is not a rewrite of a configuration; disable the guard for
        # that statement alone.
        op.execute(f"alter table public.{table} disable trigger {table}_are_immutable")
        _carry_declarations_forward(op, table, operation)
        op.execute(f"alter table public.{table} enable trigger {table}_are_immutable")
        op.execute(
            f"alter table public.{table} "
            "alter column authorization_id set not null, "
            "alter column operation set not null, "
            f"add constraint ck_{prefix}_operation check (operation = '{operation}'), "
            f"add constraint uq_{table}_authorization unique (authorization_id), "
            f"add constraint fk_{table}_authorization "
            "foreign key (authorization_id, project_id, operation) "
            f"references public.{AUTHORIZATION_TABLE} (id, project_id, operation)"
        )
        dropped_checks = ", ".join(
            f"drop constraint ck_{prefix}_{suffix}" for suffix, _expr in COMMON_CHECKS
        )
        dropped_columns = ", ".join(
            f"drop column {family}" for family, _declaration, _type in COMMON_COLUMNS
        )
        op.execute(
            f"alter table public.{table} {dropped_checks}, "
            f"drop constraint ck_{prefix}_actor, {dropped_columns}"
        )
    # A new table arrives carrying the schema owner's default privileges,
    # which hand every runtime login full access. Every runtime login reads
    # it; the worker keeps the append the five configurations already gave
    # it, and the web capability writes nothing here.
    op.execute(f"revoke all on public.{AUTHORIZATION_TABLE} from {RUNTIME_LOGINS}")
    op.execute(f"grant select on public.{AUTHORIZATION_TABLE} to {RUNTIME_LOGINS}")
    op.execute(f"grant insert on public.{AUTHORIZATION_TABLE} to corridor_worker")
    op.execute(
        f"grant usage, select on sequence public.{AUTHORIZATION_TABLE}_id_seq "
        "to corridor_worker"
    )
    op.execute(SPEND_AUTHORIZATION_PARTITION_POLICIES)


def downgrade(op) -> None:
    # First among the feature reversals, mirroring the upgrade's last feature
    # block. A declaration no configuration references has no place in the
    # supported predecessor, so a database holding one refuses to go down
    # rather than drop a person's declaration.
    referenced = " union all ".join(
        f"select authorization_id from public.{table}"
        for table, _operation, _prefix in FAMILY_CONFIGURATIONS
    )
    if op.get_bind().scalar(
        sa.text(
            f"select exists (select 1 from public.{AUTHORIZATION_TABLE} a "
            f"where a.id not in ({referenced}))"
        )
    ):
        raise RuntimeError(
            "a spend authorization no configuration references cannot be "
            "represented by the supported predecessor"
        )
    for table, _operation, prefix in FAMILY_CONFIGURATIONS:
        added = ", ".join(
            f"add column {family} {column_type}"
            for family, _declaration, column_type in COMMON_COLUMNS
        )
        op.execute(f"alter table public.{table} {added}")
        op.execute(f"alter table public.{table} disable trigger {table}_are_immutable")
        _carry_declarations_back(op, table)
        op.execute(f"alter table public.{table} enable trigger {table}_are_immutable")
        not_null = ", ".join(
            f"alter column {family} set not null"
            for family, _declaration, _type in COMMON_COLUMNS
        )
        restored_checks = ", ".join(
            f"add constraint ck_{prefix}_{suffix} check ({expression})"
            for suffix, expression in COMMON_CHECKS
        )
        op.execute(
            f"alter table public.{table} {not_null}, "
            "alter column created_at set default now(), "
            f"{restored_checks}, "
            f"add constraint ck_{prefix}_actor check (length(btrim(created_by)) > 0), "
            f"drop constraint fk_{table}_authorization, "
            f"drop constraint uq_{table}_authorization, "
            f"drop constraint ck_{prefix}_operation, "
            "drop column authorization_id, drop column operation"
        )
    op.execute(SPEND_AUTHORIZATION_SCHEMA_DOWN)
