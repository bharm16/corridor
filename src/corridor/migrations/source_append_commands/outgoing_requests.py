"""Retained outgoing requests (#652).

The chase list's `unanswered_request` band needs a retained request to make
silence a fact (ADR-0090); until this transition there was no table to read
and `read_retained_outgoing_requests` truthfully returned nothing.  This is
Corridor's own outgoing correspondence, not source-derived evidence and not
an accepted-record decision, so it does not join the spine's append matrix.
It reuses the record-decision role and the immutable-receipt idiom the
baseline-adoption receipt established: one `SECURITY DEFINER` append command
writes it, a guard trigger refuses every other write, and the runtime
capabilities read it and hold no write on it.  A received response stops the
silence clock; full receipt and delivery tracking is out of scope (#652).
Folded into this revision because the migration window holds one unreleased
transition (`migrations/policy.py`).
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


OUTGOING_REQUEST_TABLES = ("outgoing_requests", "outgoing_request_responses")

OUTGOING_REQUEST_SCHEMA = """
create table public.outgoing_requests (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    follow_up_plan_id bigint not null,
    external_organization character varying(255) not null,
    responsible_role character varying(255),
    question text not null,
    covered_subject_keys jsonb not null,
    content_sha256 character varying(64) not null,
    sent_bytes bytea,
    sent_on date not null,
    sent_by_principal character varying(128) not null,
    expected_response_by date not null,
    boundary_rule_version character varying(64),
    boundary_interval_days integer,
    idempotency_key character varying(160) not null,
    recorded_at timestamp with time zone not null default now(),
    constraint uq_outgoing_requests_project_id unique (project_id, id),
    constraint uq_outgoing_requests_key unique (project_id, idempotency_key),
    constraint fk_outgoing_requests_plan
        foreign key (project_id, follow_up_plan_id)
        references public.delta_follow_up_plans (project_id, id),
    constraint ck_outgoing_requests_organization check (
        length(btrim(external_organization)) > 0
    ),
    constraint ck_outgoing_requests_question check (
        length(btrim(question)) > 0
    ),
    constraint ck_outgoing_requests_principal check (
        length(btrim(sent_by_principal)) > 0
    ),
    constraint ck_outgoing_requests_digest check (
        content_sha256 ~ '^[0-9a-f]{64}$'
    ),
    constraint ck_outgoing_requests_boundary check (
        expected_response_by >= sent_on
    ),
    constraint ck_outgoing_requests_subjects check (
        jsonb_typeof(covered_subject_keys) = 'array'
    ),
    constraint ck_outgoing_requests_bytes check (
        sent_bytes is null or octet_length(sent_bytes) > 0
    ),
    constraint ck_outgoing_requests_interval check (
        boundary_interval_days is null or boundary_interval_days >= 0
    )
);
create index ix_outgoing_requests_project_id
    on public.outgoing_requests (project_id);
create index ix_outgoing_requests_follow_up_plan_id
    on public.outgoing_requests (follow_up_plan_id);

create table public.outgoing_request_responses (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    request_id bigint not null,
    received_on date not null,
    recorded_by_principal character varying(128) not null,
    recorded_at timestamp with time zone not null default now(),
    constraint uq_outgoing_request_responses_request
        unique (project_id, request_id),
    constraint fk_outgoing_request_responses_request
        foreign key (project_id, request_id)
        references public.outgoing_requests (project_id, id),
    constraint ck_outgoing_request_responses_principal check (
        length(btrim(recorded_by_principal)) > 0
    )
);
create index ix_outgoing_request_responses_project_id
    on public.outgoing_request_responses (project_id);
create index ix_outgoing_request_responses_request_id
    on public.outgoing_request_responses (request_id);

create function public.enforce_outgoing_request_write() returns trigger
    language plpgsql
    as $$
        begin
            if tg_op <> 'INSERT' then
                raise exception 'a retained outgoing request is append-only'
                    using errcode='23514';
            end if;
            if current_user <> 'corridor_fact_decision_writer' then
                raise exception 'a retained outgoing request is written only through its append command'
                    using errcode='23514';
            end if;
            return new;
        end; $$;

create trigger trg_outgoing_requests_write
    before insert or update or delete on public.outgoing_requests
    for each row execute function public.enforce_outgoing_request_write();
create trigger trg_outgoing_requests_truncate
    before truncate on public.outgoing_requests
    for each statement execute function public.enforce_outgoing_request_write();
create trigger trg_outgoing_request_responses_write
    before insert or update or delete on public.outgoing_request_responses
    for each row execute function public.enforce_outgoing_request_write();
create trigger trg_outgoing_request_responses_truncate
    before truncate on public.outgoing_request_responses
    for each statement execute function public.enforce_outgoing_request_write();
"""

OUTGOING_REQUEST_SCHEMA_DOWN = """
drop table if exists public.outgoing_request_responses;
drop table if exists public.outgoing_requests;
drop function if exists public.enforce_outgoing_request_write();
"""

APPEND_OUTGOING_REQUEST = """
create function public.append_outgoing_request(
    p_project_id bigint,
    p_follow_up_plan_id bigint,
    p_external_organization character varying,
    p_responsible_role character varying,
    p_question text,
    p_covered_subject_keys jsonb,
    p_content_sha256 character varying,
    p_sent_bytes bytea,
    p_sent_on date,
    p_sent_by_principal character varying,
    p_expected_response_by date,
    p_boundary_rule_version character varying,
    p_boundary_interval_days integer,
    p_idempotency_key character varying
) returns bigint
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            existing record;
            new_id bigint;
        begin
            if not exists (
                select 1 from delta_follow_up_plans
                 where id = p_follow_up_plan_id and project_id = p_project_id
            ) then
                raise exception 'the follow-up plan an outgoing request advances is outside its project'
                    using errcode = '23514';
            end if;
            if p_covered_subject_keys is null
               or jsonb_typeof(p_covered_subject_keys) <> 'array'
               or jsonb_array_length(p_covered_subject_keys) = 0 then
                raise exception 'a retained outgoing request covers at least one Utility Conflict'
                    using errcode = '23514';
            end if;
            if p_content_sha256 is null or p_content_sha256 !~ '^[0-9a-f]{64}$' then
                raise exception 'a retained outgoing request needs the digest of what was sent'
                    using errcode = '23514';
            end if;
            if p_sent_bytes is not null
               and p_content_sha256 is distinct from
                   encode(sha256(p_sent_bytes), 'hex') then
                raise exception 'retained outgoing request digest does not match its exact bytes'
                    using errcode = '23514';
            end if;
            if p_expected_response_by < p_sent_on then
                raise exception 'an expected-response boundary falls on or after the day the request went out'
                    using errcode = '23514';
            end if;
            select id, content_sha256 into existing
              from outgoing_requests
             where project_id = p_project_id
               and idempotency_key = p_idempotency_key;
            if found then
                if existing.content_sha256 is distinct from p_content_sha256 then
                    raise exception 'an outgoing request is already retained under this key with different content'
                        using errcode = '23514';
                end if;
                return existing.id;
            end if;
            insert into outgoing_requests (
                project_id, follow_up_plan_id, external_organization,
                responsible_role, question, covered_subject_keys, content_sha256,
                sent_bytes, sent_on, sent_by_principal, expected_response_by,
                boundary_rule_version, boundary_interval_days, idempotency_key
            ) values (
                p_project_id, p_follow_up_plan_id, p_external_organization,
                p_responsible_role, p_question, p_covered_subject_keys,
                p_content_sha256, p_sent_bytes, p_sent_on, p_sent_by_principal,
                p_expected_response_by, p_boundary_rule_version,
                p_boundary_interval_days, p_idempotency_key
            ) returning id into new_id;
            return new_id;
        end; $$;
"""

APPEND_OUTGOING_REQUEST_RESPONSE = """
create function public.append_outgoing_request_response(
    p_project_id bigint,
    p_request_id bigint,
    p_received_on date,
    p_recorded_by_principal character varying
) returns bigint
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            request_sent_on date;
            existing record;
            new_id bigint;
        begin
            select sent_on into request_sent_on
              from outgoing_requests
             where id = p_request_id and project_id = p_project_id;
            if not found then
                raise exception 'the outgoing request a response answers is outside its project'
                    using errcode = '23514';
            end if;
            if p_received_on < request_sent_on then
                raise exception 'a response cannot arrive before the request was sent'
                    using errcode = '23514';
            end if;
            select id, received_on into existing
              from outgoing_request_responses
             where project_id = p_project_id and request_id = p_request_id;
            if found then
                if existing.received_on is distinct from p_received_on then
                    raise exception 'this outgoing request already has a recorded response on a different day'
                        using errcode = '23514';
                end if;
                return existing.id;
            end if;
            insert into outgoing_request_responses (
                project_id, request_id, received_on, recorded_by_principal
            ) values (
                p_project_id, p_request_id, p_received_on, p_recorded_by_principal
            ) returning id into new_id;
            return new_id;
        end; $$;
"""

OUTGOING_REQUEST_COMMANDS = {
    "append_outgoing_request": (
        "(bigint, bigint, character varying, character varying, text, jsonb, "
        "character varying, bytea, date, character varying, date, "
        "character varying, integer, character varying)"
    ),
    "append_outgoing_request_response": "(bigint, bigint, date, character varying)",
}

# Both tables carry ``project_id`` and hold one customer's correspondence, so
# they answer the partition the way the record tables do (#657): the human web
# capability reads only its declared partition, and the unpartitioned roles —
# the record-decision role its command runs as among them — read and write
# across projects. The policy name matches ``p_%_project_partition`` so the
# live check in ``test_project_partition_and_offboarding.py`` finds it, and the
# two tables are recorded in ``access.PARTITIONED_RELATIONS`` beside it.
OUTGOING_REQUEST_PARTITION_POLICIES = f"""
do $$
declare
    v_roles text;
    v_table text;
begin
    select string_agg(quote_ident(rolname), ', ' order by rolname)
      into v_roles
      from pg_roles
     where rolname in ({_UNPARTITIONED_ROLES_SQL});
    foreach v_table in array array['outgoing_requests',
                                   'outgoing_request_responses'] loop
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
    end loop;
end $$;
"""


def upgrade(op) -> None:
    # Before the PUBLIC sweep, and after the Review Packet block above: the
    # request's composite foreign key resolves against delta_follow_up_plans,
    # which that block creates. Corridor-originated correspondence is written
    # only by the record-decision role's command and read by the runtime
    # capabilities, exactly as the baseline-adoption receipt is.
    op.execute(OUTGOING_REQUEST_SCHEMA)
    for table in OUTGOING_REQUEST_TABLES:
        # A new table arrives carrying the schema owner's default privileges,
        # which hand every runtime login full access, so the write half is
        # taken back explicitly and only the command's role keeps it.
        op.execute(f"revoke all on public.{table} from {RUNTIME_LOGINS}")
        op.execute(f"grant select on public.{table} to {RUNTIME_LOGINS}")
        op.execute(
            f"grant select, insert on public.{table} to {RECORD_DECISION_ROLE}"
        )
        op.execute(
            f"grant usage, select on sequence public.{table}_id_seq "
            f"to {RECORD_DECISION_ROLE}"
        )
    op.execute(APPEND_OUTGOING_REQUEST)
    op.execute(APPEND_OUTGOING_REQUEST_RESPONSE)
    for name, signature in OUTGOING_REQUEST_COMMANDS.items():
        op.execute(
            f"alter function public.{name}{signature} owner to {RECORD_DECISION_ROLE}"
        )
        op.execute(f"revoke all on function public.{name}{signature} from public")
        # Retaining a sent request and recording its response are attributable
        # human acts, so they join the other decision commands on the web
        # capability alone (#652, ADR-0076).
        op.execute(
            f"grant execute on function public.{name}{signature} to corridor_web"
        )
    op.execute(OUTGOING_REQUEST_PARTITION_POLICIES)


def downgrade(op) -> None:
    # First among the feature reversals, mirroring the upgrade's last feature
    # block. The supported predecessor has no such table, so a retained request
    # would go silently on the way down; refuse rather than lose irreplaceable
    # correspondence. The two commands and the append-only guard drop with the
    # tables, and the runtime read grant goes with them.
    if op.get_bind().scalar(
        sa.text("select exists (select 1 from public.outgoing_requests)")
    ):
        raise RuntimeError(
            "retained outgoing requests cannot be represented by the supported predecessor"
        )
    op.execute(
        f"drop function if exists public.append_outgoing_request_response"
        f"{OUTGOING_REQUEST_COMMANDS['append_outgoing_request_response']}"
    )
    op.execute(
        f"drop function if exists public.append_outgoing_request"
        f"{OUTGOING_REQUEST_COMMANDS['append_outgoing_request']}"
    )
    op.execute(OUTGOING_REQUEST_SCHEMA_DOWN)
