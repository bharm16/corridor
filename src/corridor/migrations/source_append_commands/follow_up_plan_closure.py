"""How a Follow-up Plan stops being an outside ask (#835).

Folded into this transition for the same window reason as the blocks above:
``corridor.migrations.policy`` allows one unreleased transition and this is it.

#526 gave Needs coordination a recorded decision — the exact question, who owes
the answer, when it returns — and gave it no way to end.  ``delta_follow_up_plans``
is insert-only under ``enforce_delta_record_decision_write``, and the two readers
that decide whether a plan is still waiting (``project_workflow.outstanding_follow_up``
and ``native_follow_up_reading.read_adopted_follow_up_plans``) drop a plan for
exactly two reasons: the Proposed Delta it was raised on stopped being open, or
the one packet act that recorded it was reversed.  So a coordinator who had
recorded the wrong question, named the wrong External Organization, or no longer
needed an outside answer at all had nothing to record.

**Appending a second plan was not the missing feature; it was the bug.**
``delta_follow_up_plans`` already takes more than one row per Proposed Delta and
nothing in the record says one replaced another, so a corrected question became
*two live outside asks for one question* — the week would list both and the chase
list would contact somebody twice.  What was missing is the statement that one
closed, and that is this relation.

**Closing is not #834's Undo, and must not be spelled as one.**
``reverse_review_packet`` says the recorded act never stood: it appends a
compensating revision, restores each predecessor decision, and takes the packet's
own revision and cited evidence back with it.  Cancelling a plan says the
opposite — the ask was real, it was recorded correctly, and it is no longer
needed.  A cancellation that reused the reversal would erase the fact that
anybody was ever asked.

**Two kinds, and the shape of each is unrepresentable in the other's.**
``superseded`` names the successor plan that replaces this one; ``cancelled``
names a structured reason, because ADR-0038 requires one for the legacy plan
lifecycle and the spine's plan is the same act on a different relation.
``uq_delta_follow_up_plan_closures_plan`` is what makes a plan close exactly
once: a second closure of the same plan is not a correction, it is two
statements about one plan, and the readers would have to choose between them.

**No Project Record revision.**  ADR-0084 §1 leaves accept, edit and reject as
the only dispositions that write one.  A plan writes no ``delta_dispositions``
row and leaves the proposed value unaccepted, and closing one changes the record
even less: the Proposed Delta was open before and is open after, and it is still
decided on the review screen.  The successor a ``superseded`` closure names is
recorded by ``record_delta_follow_up_plan`` against the project's *current
accepted revision head* — which is how ``native_follow_up_reading`` already reads
that column, as the accepted record a plan was recorded against — rather than by
opening a revision the act has no business writing.

**Who may close one is proved here, not only in the Python that calls in**
(#839).  The maintainer settled on 2026-09-10 that Project Coordination may
confirm coverage and request preparation, and read-only membership confers
neither.  Retiring somebody's outside ask, or replacing the question they
recorded, is coordination work of that same class, and the route is reachable on
its own rather than only from inside a packet act — so this relation carries
#839's ``enforce_coordination_designation`` trigger, naming its own principal
column.  It is reused rather than re-written: one function that reads the
principal out of ``TG_ARGV[0]`` already serves two relations and now serves
three, and a second near-copy would be a second place for the roster rule to
drift.

That trigger sits *beside* ``enforce_delta_record_decision_write`` rather than
instead of it, because the two answer different questions: one asks whether the
write arrived through the typed command as the decision role, the other whether
the person it names may coordinate this project.  Neither implies the other.

The downgrade drops the relation whole rather than opening it to raw writes,
because it is born in this transition: there is no predecessor shape a closure
could be carried back into.
"""

from __future__ import annotations

from corridor.migrations.source_append_commands.project_partition import (
    _UNPARTITIONED_ROLES_SQL,
)
from corridor.migrations.source_append_commands.roles import (
    RECORD_DECISION_ROLE,
    RUNTIME_LOGINS,
)


CLOSURE_TABLE = "delta_follow_up_plan_closures"

#: What a closure says happened to the plan.  ``superseded`` names the plan that
#: replaced it; ``cancelled`` names a structured reason it is no longer needed.
CLOSURE_KINDS = ("superseded", "cancelled")

#: The structured reasons a cancellation may carry.  ADR-0038 requires a
#: structured cancellation reason on the legacy plan lifecycle and refuses free
#: text as a substitute; the spine's plan is the same act and carries the same
#: rule.  Free prose belongs in ``note`` beside one of these, never instead.
CANCELLATION_REASONS = (
    "answered_another_way",
    "no_longer_needed",
    "raised_in_error",
    "asked_of_the_wrong_party",
)

_CLOSURE_KINDS_SQL = ", ".join(f"'{kind}'" for kind in CLOSURE_KINDS)
_CANCELLATION_REASONS_SQL = ", ".join(
    f"'{reason}'" for reason in CANCELLATION_REASONS
)


FOLLOW_UP_PLAN_CLOSURE_SCHEMA = f"""
create table public.{CLOSURE_TABLE} (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    plan_id bigint not null,
    closure_kind character varying(32) not null,
    successor_plan_id bigint,
    cancellation_reason character varying(64),
    note text,
    closed_by_principal character varying(128) not null,
    closed_at timestamp with time zone not null,
    idempotency_key character varying(160) not null,
    recorded_at timestamp with time zone not null default now(),
    constraint uq_{CLOSURE_TABLE}_project_id unique (project_id, id),
    -- A plan closes once. A second closure is not a correction; it is two
    -- statements about one plan, and the readers would have to choose.
    constraint uq_{CLOSURE_TABLE}_plan unique (plan_id),
    constraint uq_{CLOSURE_TABLE}_key unique (project_id, idempotency_key),
    constraint fk_{CLOSURE_TABLE}_plan foreign key (project_id, plan_id)
        references public.delta_follow_up_plans (project_id, id),
    constraint fk_{CLOSURE_TABLE}_successor
        foreign key (project_id, successor_plan_id)
        references public.delta_follow_up_plans (project_id, id),
    constraint ck_{CLOSURE_TABLE}_kind check (
        closure_kind in ({_CLOSURE_KINDS_SQL})
    ),
    -- Each kind's shape is unrepresentable in the other's: a supersession
    -- names its successor and no reason, a cancellation names a structured
    -- reason and no successor.
    constraint ck_{CLOSURE_TABLE}_shape check (
        (closure_kind = 'superseded' and successor_plan_id is not null
             and cancellation_reason is null)
        or (closure_kind = 'cancelled' and successor_plan_id is null
             and cancellation_reason is not null)
    ),
    constraint ck_{CLOSURE_TABLE}_reason check (
        cancellation_reason is null
        or cancellation_reason in ({_CANCELLATION_REASONS_SQL})
    ),
    constraint ck_{CLOSURE_TABLE}_not_self check (
        successor_plan_id is null or successor_plan_id <> plan_id
    ),
    constraint ck_{CLOSURE_TABLE}_principal check (
        length(btrim(closed_by_principal)) > 0
    ),
    constraint ck_{CLOSURE_TABLE}_key_text check (
        length(btrim(idempotency_key)) > 0
    ),
    constraint ck_{CLOSURE_TABLE}_note check (
        note is null or (length(btrim(note)) > 0 and length(note) <= 2000)
    )
);

create index ix_{CLOSURE_TABLE}_project_id
    on public.{CLOSURE_TABLE} (project_id);
create index ix_{CLOSURE_TABLE}_plan_id
    on public.{CLOSURE_TABLE} (plan_id);

-- The same guard the Review Packet relations carry: only the record-decision
-- role writes, and only by insert. A wrong closure is corrected by recording a
-- new plan, never by editing the statement that the old one closed.
create trigger trg_{CLOSURE_TABLE}_write
    before insert or update or delete on public.{CLOSURE_TABLE}
    for each row execute function public.enforce_delta_record_decision_write();
create trigger trg_{CLOSURE_TABLE}_truncate
    before truncate on public.{CLOSURE_TABLE}
    for each statement execute function public.enforce_delta_record_decision_write();

-- #839's proof, reused rather than re-written. It reads the principal out of
-- the column its own trigger names, so one function serves every relation a
-- coordinating person appends to.
create trigger trg_{CLOSURE_TABLE}_designated
    before insert on public.{CLOSURE_TABLE}
    for each row execute function public.enforce_coordination_designation(
        'closed_by_principal'
    );
"""


CLOSE_DELTA_FOLLOW_UP_PLAN = f"""
create function public.close_delta_follow_up_plan(
    p_project_id bigint,
    p_plan_id bigint,
    p_principal character varying,
    p_closure_kind character varying,
    p_successor_plan_id bigint,
    p_cancellation_reason character varying,
    p_note text,
    p_closed_at timestamp with time zone,
    p_idempotency_key character varying
) returns jsonb
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            prior {CLOSURE_TABLE}%ROWTYPE;
            plan_delta bigint;
            successor_delta bigint;
            closure_id bigint;
        begin
            if p_principal is null or length(btrim(p_principal)) = 0 then
                raise exception 'review_packet:missing_principal closing a Follow-up Plan names the person closing it'
                    using errcode='23514';
            end if;
            if p_idempotency_key is null
               or length(btrim(p_idempotency_key)) = 0 then
                raise exception 'review_packet:missing_idempotency_key closing a Follow-up Plan needs an idempotency key'
                    using errcode='23514';
            end if;
            if p_closed_at is null then
                raise exception 'review_packet:missing_decided_at closing a Follow-up Plan records when it was closed'
                    using errcode='23514';
            end if;
            if p_closure_kind is null
               or p_closure_kind not in ({_CLOSURE_KINDS_SQL}) then
                raise exception 'review_packet:invalid_closure_kind a Follow-up Plan is superseded by another plan or cancelled with a reason'
                    using errcode='23514';
            end if;

            select * into prior from {CLOSURE_TABLE}
             where project_id = p_project_id
               and idempotency_key = p_idempotency_key;
            if found then
                if prior.plan_id <> p_plan_id then
                    raise exception 'review_packet:key_bound_to_other_content the closure key is already bound to another Follow-up Plan'
                        using errcode='23514';
                end if;
                return jsonb_build_object('closure_id', prior.id, 'created', false);
            end if;

            select delta_id into plan_delta from delta_follow_up_plans
             where id = p_plan_id and project_id = p_project_id;
            if plan_delta is null then
                raise exception 'review_packet:cross_project_plan Follow-up Plan % is not this project''s to close', p_plan_id
                    using errcode='23514';
            end if;
            if exists (
                select 1 from {CLOSURE_TABLE} where plan_id = p_plan_id
            ) then
                raise exception 'review_packet:already_closed Follow-up Plan % is already closed', p_plan_id
                    using errcode='23514';
            end if;

            if p_closure_kind = 'superseded' then
                if p_successor_plan_id is null then
                    raise exception 'review_packet:missing_successor_plan a superseded Follow-up Plan names the plan that replaces it'
                        using errcode='23514';
                end if;
                select delta_id into successor_delta from delta_follow_up_plans
                 where id = p_successor_plan_id and project_id = p_project_id;
                if successor_delta is null then
                    raise exception 'review_packet:cross_project_plan Follow-up Plan % is not this project''s to close', p_successor_plan_id
                        using errcode='23514';
                end if;
                -- A plan follows one work subject (ADR-0038). A successor on
                -- another Proposed Delta would silently move the question.
                if successor_delta <> plan_delta then
                    raise exception 'review_packet:successor_on_other_delta the replacing Follow-up Plan is raised on a different proposed change'
                        using errcode='23514';
                end if;
            else
                if p_cancellation_reason is null
                   or p_cancellation_reason not in ({_CANCELLATION_REASONS_SQL}) then
                    raise exception 'review_packet:missing_cancellation_reason cancelling a Follow-up Plan records a structured reason, never free text alone'
                        using errcode='23514';
                end if;
            end if;

            insert into {CLOSURE_TABLE} (
                project_id, plan_id, closure_kind, successor_plan_id,
                cancellation_reason, note, closed_by_principal, closed_at,
                idempotency_key
            ) values (
                p_project_id, p_plan_id, p_closure_kind,
                case when p_closure_kind = 'superseded'
                     then p_successor_plan_id else null end,
                case when p_closure_kind = 'cancelled'
                     then p_cancellation_reason else null end,
                nullif(btrim(coalesce(p_note, '')), ''),
                p_principal, p_closed_at, p_idempotency_key
            ) returning id into closure_id;
            return jsonb_build_object('closure_id', closure_id, 'created', true);
        end; $$;
"""

CLOSE_DELTA_FOLLOW_UP_PLAN_SIGNATURE = (
    "(bigint, bigint, character varying, character varying, bigint, "
    "character varying, text, timestamp with time zone, character varying)"
)


# A closure names one customer's own outside ask, so it answers #531's
# partition exactly as the plan it closes does.
FOLLOW_UP_PLAN_CLOSURE_PARTITION_POLICIES = f"""
do $$
declare
    v_roles text;
    v_table text := '{CLOSURE_TABLE}';
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

FOLLOW_UP_PLAN_CLOSURE_SCHEMA_DOWN = f"""
drop function if exists public.close_delta_follow_up_plan{CLOSE_DELTA_FOLLOW_UP_PLAN_SIGNATURE};
drop table if exists public.{CLOSURE_TABLE} cascade;
"""


def upgrade(op) -> None:
    # After `coverage_preparation`, because the designation trigger below is
    # #839's function and that block creates it, and after `review_packets`,
    # because the composite foreign key names the plan relation it creates.
    op.execute(FOLLOW_UP_PLAN_CLOSURE_SCHEMA)
    # A new table arrives carrying the schema owner's default privileges, which
    # hand every runtime login full access. The same terms the Review Packet
    # relations were granted: the application reads a closure and writes none.
    op.execute(f"revoke all on public.{CLOSURE_TABLE} from {RUNTIME_LOGINS}")
    op.execute(f"grant select on public.{CLOSURE_TABLE} to {RUNTIME_LOGINS}")
    op.execute(
        f"grant select, insert on public.{CLOSURE_TABLE} to {RECORD_DECISION_ROLE}"
    )
    op.execute(
        f"grant usage, select on sequence public.{CLOSURE_TABLE}_id_seq "
        f"to {RECORD_DECISION_ROLE}"
    )
    op.execute(CLOSE_DELTA_FOLLOW_UP_PLAN)
    op.execute(
        f"alter function public.close_delta_follow_up_plan"
        f"{CLOSE_DELTA_FOLLOW_UP_PLAN_SIGNATURE} owner to {RECORD_DECISION_ROLE}"
    )
    op.execute(
        f"revoke all on function public.close_delta_follow_up_plan"
        f"{CLOSE_DELTA_FOLLOW_UP_PLAN_SIGNATURE} from public"
    )
    # Closing a Follow-up Plan is an attributable human act, so it joins the
    # other decision commands on the web capability alone.
    op.execute(
        f"grant execute on function public.close_delta_follow_up_plan"
        f"{CLOSE_DELTA_FOLLOW_UP_PLAN_SIGNATURE} to corridor_web"
    )
    op.execute(FOLLOW_UP_PLAN_CLOSURE_PARTITION_POLICIES)


def downgrade(op) -> None:
    op.execute(
        f"do $$ begin "
        f"if exists (select 1 from pg_tables where schemaname = 'public' "
        f"and tablename = '{CLOSURE_TABLE}') then "
        f"revoke select on public.{CLOSURE_TABLE} from {RUNTIME_LOGINS}; "
        f"end if; end $$;"
    )
    op.execute(FOLLOW_UP_PLAN_CLOSURE_SCHEMA_DOWN)
