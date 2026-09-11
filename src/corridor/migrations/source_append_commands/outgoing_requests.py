"""Retained outgoing requests, and what came back against them (#652, #837).

The chase list's `unanswered_request` band needs a retained request to make
silence a fact (ADR-0090); until this transition there was no table to read
and `read_retained_outgoing_requests` truthfully returned nothing.  This is
Corridor's own outgoing correspondence, not source-derived evidence and not
an accepted-record decision, so it does not join the spine's append matrix.
It reuses the record-decision role and the immutable-receipt idiom the
baseline-adoption receipt established: one `SECURITY DEFINER` append command
writes each relation, a guard trigger refuses every other write, and the
runtime capabilities read them and hold no write on them.

**The plans a request advances are a relation** (#837, finishing the accepted
#652 contract of 2026-09-04).  The first shape took one `follow_up_plan_id`
per request, and a follow-up bundle is one interaction covering several
questions: one email advances as many Follow-up Plans as the coordinator
addressed in it, and one plan takes several requests before anybody answers.
A column could record only the first of those honestly, so `outgoing_request_plans`
carries the relation and the request carries no plan column at all.
`append_outgoing_request` writes the request and its whole plan set in one
statement, so a half-related request is not a row this schema can hold.

**The exact sent content is retained through the storage interface**, never as
a `bytea` column: ADR-0079 makes object storage the one backend and every key
keeps its `<sha[:2]>/<sha><suffix>` layout.  Because the key *contains* the
digest, `ck_outgoing_requests_content_key` proves here that the two agree
without the bytes ever reaching PostgreSQL.  The first shape allowed a digest
with no content, for the case where whoever sent it kept no bytes; #837 takes
that back on the accepted contract's own words, because a coordinator looking
at a no-response finding has to be able to read what was actually asked.

**The sender and the recorder are two columns.**  Corridor sends nothing, so
`sent_by_principal` may name a colleague the roster has never heard of, and
`recorded_by_principal` is the person sitting in front of Corridor stating that
it went out.  "Who sent this" and "who says it was sent" are different claims.

**A response carries its evidence and its completeness.**  One of four things
is what a coordinator actually has -- the incoming Document, the Source
Delivery that brought it, the exact Source Segment, or an attributable manual
observation -- and `ck_outgoing_request_responses_evidence` makes the other
three unrepresentable in that row.  Several observations may answer one
request, because an acknowledgement on Monday and the substance on Friday are
two things that happened; the one-response-per-request unique of the first
shape is gone and idempotency moves onto the caller's key.  Recording one stops
the no-response clock and settles nothing else: no disposition, no plan
lifecycle act, no Project Record revision is written here or reachable from
here.

**Correction is an append.**  A mistaken request or response is superseded by a
corrected one naming it and saying why, held to one successor apiece by a
partial unique index, and "superseded" is derived from that successor's
existence rather than stored as a status.

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


OUTGOING_REQUEST_TABLES = (
    "outgoing_requests",
    "outgoing_request_plans",
    "outgoing_request_responses",
)

# What one observation says about how much of the ask the reply answered. The
# maintainer's three (#652, 2026-09-04): "whether the response was complete,
# partial, or merely acknowledged". `corridor.outgoing_requests` holds the
# runtime copy; a migration states its own vocabulary because what it writes is
# frozen once a database has run it.
RESPONSE_COMPLETENESS = ("acknowledgement", "partial", "substantive")
RESPONSE_EVIDENCE_KINDS = (
    "document",
    "source_delivery",
    "source_segment",
    "manual_observation",
)
_COMPLETENESS_SQL = ", ".join(f"'{value}'" for value in RESPONSE_COMPLETENESS)
_EVIDENCE_KINDS_SQL = ", ".join(f"'{value}'" for value in RESPONSE_EVIDENCE_KINDS)

OUTGOING_REQUEST_SCHEMA = f"""
create table public.outgoing_requests (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    external_organization character varying(255) not null,
    responsible_role character varying(255),
    question text not null,
    covered_subject_keys jsonb not null,
    content_sha256 character varying(64) not null,
    -- The object-storage key the exact sent content lives under. Not the bytes:
    -- ADR-0079 makes the store the one backend, and the key carries the digest
    -- so their agreement is checkable here without them.
    sent_content_key character varying(160) not null,
    sent_on date not null,
    sent_by_principal character varying(128) not null,
    recorded_by_principal character varying(128) not null,
    expected_response_by date not null,
    boundary_rule_version character varying(64),
    boundary_interval_days integer,
    supersedes_request_id bigint,
    correction_reason text,
    idempotency_key character varying(160) not null,
    recorded_at timestamp with time zone not null default now(),
    constraint uq_outgoing_requests_project_id unique (project_id, id),
    constraint uq_outgoing_requests_key unique (project_id, idempotency_key),
    constraint fk_outgoing_requests_supersedes
        foreign key (project_id, supersedes_request_id)
        references public.outgoing_requests (project_id, id),
    constraint ck_outgoing_requests_organization check (
        length(btrim(external_organization)) > 0
    ),
    constraint ck_outgoing_requests_question check (
        length(btrim(question)) > 0
    ),
    constraint ck_outgoing_requests_principal check (
        length(btrim(sent_by_principal)) > 0
    ),
    constraint ck_outgoing_requests_recorder check (
        length(btrim(recorded_by_principal)) > 0
    ),
    constraint ck_outgoing_requests_digest check (
        content_sha256 ~ '^[0-9a-f]{{64}}$'
    ),
    -- The store's only key layout is the first two characters of the digest,
    -- a slash, the digest, then the suffix, so positions 1..67 of the key are
    -- determined by the digest and position 68 onward is the suffix. Comparing
    -- the key against itself that way proves the two agree. Spelled out in
    -- words rather than shown as a Python slice, because SQLAlchemy reads a
    -- colon in a SQL comment as a bind parameter and the migration dies on it.
    constraint ck_outgoing_requests_content_key check (
        sent_content_key = substr(content_sha256, 1, 2) || '/'
            || content_sha256 || substr(sent_content_key, 68)
    ),
    constraint ck_outgoing_requests_boundary check (
        expected_response_by >= sent_on
    ),
    constraint ck_outgoing_requests_subjects check (
        jsonb_typeof(covered_subject_keys) = 'array'
    ),
    constraint ck_outgoing_requests_interval check (
        boundary_interval_days is null or boundary_interval_days >= 0
    ),
    -- A correction names what it corrects and why, or is not a correction.
    constraint ck_outgoing_requests_correction check (
        (supersedes_request_id is null) = (correction_reason is null)
    )
);
create index ix_outgoing_requests_project_id
    on public.outgoing_requests (project_id);
-- One correction per corrected request: a chain, never a fork, so "which
-- record stands" has exactly one answer.
create unique index uq_outgoing_requests_correction
    on public.outgoing_requests (project_id, supersedes_request_id)
    where supersedes_request_id is not null;

create table public.outgoing_request_plans (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    request_id bigint not null,
    follow_up_plan_id bigint not null,
    recorded_at timestamp with time zone not null default now(),
    constraint uq_outgoing_request_plans_row unique (project_id, id),
    constraint uq_outgoing_request_plans_pair
        unique (project_id, request_id, follow_up_plan_id),
    constraint fk_outgoing_request_plans_request
        foreign key (project_id, request_id)
        references public.outgoing_requests (project_id, id),
    constraint fk_outgoing_request_plans_plan
        foreign key (project_id, follow_up_plan_id)
        references public.delta_follow_up_plans (project_id, id)
);
create index ix_outgoing_request_plans_project_id
    on public.outgoing_request_plans (project_id);
create index ix_outgoing_request_plans_plan_id
    on public.outgoing_request_plans (follow_up_plan_id);

create table public.outgoing_request_responses (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    request_id bigint not null,
    received_on date not null,
    completeness character varying(32) not null,
    evidence_kind character varying(32) not null,
    document_id bigint,
    source_delivery_id bigint,
    source_segment_id bigint,
    observation text,
    observed_by_principal character varying(128),
    source_reference character varying(255) not null,
    recorded_by_principal character varying(128) not null,
    supersedes_response_id bigint,
    correction_reason text,
    idempotency_key character varying(160) not null,
    recorded_at timestamp with time zone not null default now(),
    constraint uq_outgoing_request_responses_row unique (project_id, id),
    constraint uq_outgoing_request_responses_key
        unique (project_id, idempotency_key),
    constraint fk_outgoing_request_responses_request
        foreign key (project_id, request_id)
        references public.outgoing_requests (project_id, id),
    constraint fk_outgoing_request_responses_document
        foreign key (project_id, document_id)
        references public.documents (project_id, id),
    constraint fk_outgoing_request_responses_delivery
        foreign key (source_delivery_id, project_id)
        references public.source_deliveries (id, project_id),
    constraint fk_outgoing_request_responses_segment
        foreign key (project_id, source_segment_id)
        references public.source_segments (project_id, id),
    constraint fk_outgoing_request_responses_supersedes
        foreign key (project_id, supersedes_response_id)
        references public.outgoing_request_responses (project_id, id),
    constraint ck_outgoing_request_responses_principal check (
        length(btrim(recorded_by_principal)) > 0
    ),
    constraint ck_outgoing_request_responses_reference check (
        length(btrim(source_reference)) > 0
    ),
    constraint ck_outgoing_request_responses_completeness check (
        completeness in ({_COMPLETENESS_SQL})
    ),
    constraint ck_outgoing_request_responses_evidence_kind check (
        evidence_kind in ({_EVIDENCE_KINDS_SQL})
    ),
    -- The four kinds, made unrepresentable in each other's shape. A row that
    -- names a document and a telephone call is not a row this schema can hold,
    -- which is what stops "linked to its evidence" from becoming "has an
    -- evidence column somebody filled in".
    constraint ck_outgoing_request_responses_evidence check (
        (evidence_kind = 'document' and document_id is not null
             and source_delivery_id is null and source_segment_id is null
             and observation is null and observed_by_principal is null)
        or (evidence_kind = 'source_delivery' and source_delivery_id is not null
             and document_id is null and source_segment_id is null
             and observation is null and observed_by_principal is null)
        or (evidence_kind = 'source_segment' and source_segment_id is not null
             and document_id is null and source_delivery_id is null
             and observation is null and observed_by_principal is null)
        or (evidence_kind = 'manual_observation' and observation is not null
             and observed_by_principal is not null
             and document_id is null and source_delivery_id is null
             and source_segment_id is null)
    ),
    constraint ck_outgoing_request_responses_observation check (
        observation is null or length(btrim(observation)) > 0
    ),
    constraint ck_outgoing_request_responses_observer check (
        observed_by_principal is null
            or length(btrim(observed_by_principal)) > 0
    ),
    constraint ck_outgoing_request_responses_correction check (
        (supersedes_response_id is null) = (correction_reason is null)
    )
);
create index ix_outgoing_request_responses_project_id
    on public.outgoing_request_responses (project_id);
create index ix_outgoing_request_responses_request_id
    on public.outgoing_request_responses (request_id);
create unique index uq_outgoing_request_responses_correction
    on public.outgoing_request_responses (project_id, supersedes_response_id)
    where supersedes_response_id is not null;

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
"""

# One guard over all three relations. The plan relation joins it for the same
# reason the other two carry it: "this request covered that plan" is a
# statement about correspondence that happened, and nothing edits one.
OUTGOING_REQUEST_TRIGGERS = "".join(
    f"""
create trigger trg_{table}_write
    before insert or update or delete on public.{table}
    for each row execute function public.enforce_outgoing_request_write();
create trigger trg_{table}_truncate
    before truncate on public.{table}
    for each statement execute function public.enforce_outgoing_request_write();
"""
    for table in OUTGOING_REQUEST_TABLES
)

OUTGOING_REQUEST_SCHEMA_DOWN = """
drop table if exists public.outgoing_request_responses;
drop table if exists public.outgoing_request_plans;
drop table if exists public.outgoing_requests;
drop function if exists public.enforce_outgoing_request_write();
"""

APPEND_OUTGOING_REQUEST = """
create function public.append_outgoing_request(
    p_project_id bigint,
    p_follow_up_plan_ids bigint[],
    p_external_organization character varying,
    p_responsible_role character varying,
    p_question text,
    p_covered_subject_keys jsonb,
    p_content_sha256 character varying,
    p_sent_content_key character varying,
    p_sent_on date,
    p_sent_by_principal character varying,
    p_recorded_by_principal character varying,
    p_expected_response_by date,
    p_boundary_rule_version character varying,
    p_boundary_interval_days integer,
    p_supersedes_request_id bigint,
    p_correction_reason text,
    p_idempotency_key character varying
) returns bigint
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            named bigint[];
            existing record;
            new_id bigint;
        begin
            if p_follow_up_plan_ids is null
               or array_length(p_follow_up_plan_ids, 1) is null then
                raise exception 'a retained outgoing request advances at least one Follow-up Plan'
                    using errcode = '23514';
            end if;
            select array_agg(distinct plan_id) into named
              from unnest(p_follow_up_plan_ids) as plan_id;
            if (select count(*) from delta_follow_up_plans
                 where project_id = p_project_id
                   and id = any(named)) <> array_length(named, 1) then
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
            if p_sent_content_key is distinct from
               substr(p_content_sha256, 1, 2) || '/' || p_content_sha256
                   || substr(coalesce(p_sent_content_key, ''), 68) then
                raise exception 'retained outgoing request content is not stored under its own digest'
                    using errcode = '23514';
            end if;
            if p_expected_response_by < p_sent_on then
                raise exception 'an expected-response boundary falls on or after the day the request went out'
                    using errcode = '23514';
            end if;
            if (p_supersedes_request_id is null)
               <> (p_correction_reason is null) then
                raise exception 'a correction names the request it corrects and why'
                    using errcode = '23514';
            end if;
            if p_supersedes_request_id is not null
               and not exists (
                   select 1 from outgoing_requests
                    where id = p_supersedes_request_id
                      and project_id = p_project_id
               ) then
                raise exception 'the outgoing request a correction corrects is outside its project'
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
                project_id, external_organization, responsible_role, question,
                covered_subject_keys, content_sha256, sent_content_key, sent_on,
                sent_by_principal, recorded_by_principal, expected_response_by,
                boundary_rule_version, boundary_interval_days,
                supersedes_request_id, correction_reason, idempotency_key
            ) values (
                p_project_id, p_external_organization, p_responsible_role,
                p_question, p_covered_subject_keys, p_content_sha256,
                p_sent_content_key, p_sent_on, p_sent_by_principal,
                p_recorded_by_principal, p_expected_response_by,
                p_boundary_rule_version, p_boundary_interval_days,
                p_supersedes_request_id, p_correction_reason, p_idempotency_key
            ) returning id into new_id;
            -- The request and its whole plan set in one statement, so a
            -- half-related request cannot exist even for the length of a
            -- transaction.
            insert into outgoing_request_plans (
                project_id, request_id, follow_up_plan_id
            )
            select p_project_id, new_id, plan_id
              from unnest(named) as plan_id;
            return new_id;
        end; $$;
"""

APPEND_OUTGOING_REQUEST_RESPONSE = f"""
create function public.append_outgoing_request_response(
    p_project_id bigint,
    p_request_id bigint,
    p_received_on date,
    p_completeness character varying,
    p_evidence_kind character varying,
    p_document_id bigint,
    p_source_delivery_id bigint,
    p_source_segment_id bigint,
    p_observation text,
    p_observed_by_principal character varying,
    p_source_reference character varying,
    p_recorded_by_principal character varying,
    p_supersedes_response_id bigint,
    p_correction_reason text,
    p_idempotency_key character varying
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
            if p_completeness is null
               or p_completeness not in ({_COMPLETENESS_SQL}) then
                raise exception 'a recorded response says whether it acknowledged, answered part, or answered'
                    using errcode = '23514';
            end if;
            if p_evidence_kind is null
               or p_evidence_kind not in ({_EVIDENCE_KINDS_SQL}) then
                raise exception 'a recorded response links to a Document, a Source Delivery, a Source Segment, or an attributable manual observation'
                    using errcode = '23514';
            end if;
            if (p_supersedes_response_id is null)
               <> (p_correction_reason is null) then
                raise exception 'a correction names the response it corrects and why'
                    using errcode = '23514';
            end if;
            if p_supersedes_response_id is not null
               and not exists (
                   select 1 from outgoing_request_responses
                    where id = p_supersedes_response_id
                      and project_id = p_project_id
                      and request_id = p_request_id
               ) then
                raise exception 'a correction corrects a response recorded against the same request'
                    using errcode = '23514';
            end if;
            select id, received_on into existing
              from outgoing_request_responses
             where project_id = p_project_id
               and idempotency_key = p_idempotency_key;
            if found then
                if existing.received_on is distinct from p_received_on then
                    raise exception 'a response is already recorded under this key on a different day'
                        using errcode = '23514';
                end if;
                return existing.id;
            end if;
            insert into outgoing_request_responses (
                project_id, request_id, received_on, completeness,
                evidence_kind, document_id, source_delivery_id,
                source_segment_id, observation, observed_by_principal,
                source_reference, recorded_by_principal,
                supersedes_response_id, correction_reason, idempotency_key
            ) values (
                p_project_id, p_request_id, p_received_on, p_completeness,
                p_evidence_kind, p_document_id, p_source_delivery_id,
                p_source_segment_id, p_observation, p_observed_by_principal,
                p_source_reference, p_recorded_by_principal,
                p_supersedes_response_id, p_correction_reason,
                p_idempotency_key
            ) returning id into new_id;
            return new_id;
        end; $$;
"""

OUTGOING_REQUEST_COMMANDS = {
    "append_outgoing_request": (
        "(bigint, bigint[], character varying, character varying, text, jsonb, "
        "character varying, character varying, date, character varying, "
        "character varying, date, character varying, integer, bigint, text, "
        "character varying)"
    ),
    "append_outgoing_request_response": (
        "(bigint, bigint, date, character varying, character varying, bigint, "
        "bigint, bigint, text, character varying, character varying, "
        "character varying, bigint, text, character varying)"
    ),
}

# All three tables carry ``project_id`` and hold one customer's correspondence,
# so they answer the partition the way the record tables do (#657): the human
# web capability reads only its declared partition, and the unpartitioned roles
# — the record-decision role its command runs as among them — read and write
# across projects. The policy name matches ``p_%_project_partition`` so the
# live check in ``test_project_partition_and_offboarding.py`` finds it, and the
# three tables are recorded in ``access.PARTITIONED_RELATIONS`` beside it.
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
                                   'outgoing_request_plans',
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
    # plan relation's composite foreign key resolves against
    # delta_follow_up_plans, which that block creates, and a response's
    # evidence resolves against `documents`, `source_deliveries` (whose
    # `(id, project_id)` unique the coverage block adds) and `source_segments`.
    # Corridor-originated correspondence is written only by the record-decision
    # role's command and read by the runtime capabilities, exactly as the
    # baseline-adoption receipt is.
    op.execute(OUTGOING_REQUEST_SCHEMA)
    op.execute(OUTGOING_REQUEST_TRIGGERS)
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
