"""Review Packet resolution (#526, ADR-0035, ADR-0084, ADR-0085).

ADR-0085 makes a Review Packet a *derived presentation* over open Proposed
Deltas: no packet is stored, no packet has a lifecycle, and two readings of
the same state rebuild the same packets.  What must be durable is the
**act** — the one attributable transaction a coordinator committed — and
that is what this block records.

``delta_review_packet_receipts`` holds one act: the grouping rule and key
the coordinator was shown, the principal, the accepted revision they had
read, and the *optional* one Project Record revision the act produced.  The
revision is null exactly when every child was a dated Defer, because
scheduling writes no revision (ADR-0084).

``delta_review_packet_children`` keeps each child's own identity, because
ADR-0035 forbids one Save collapsing the distinct domain acts inside it:
the exact delta, the position it was shown in, the source revision the
coordinator had read for it, the outcome they chose, and the one identity
that outcome produced — a Human Record Decision (#519), a Follow-up Plan,
or a scheduling receipt.

``delta_follow_up_plans`` is the spine's Follow-up Plan.  ADR-0084 forbids
settling an external fact with free text, so Needs coordination has to be a
recorded decision rather than a discarded selection: the exact question, who
owes the answer, when it returns, what it affects, and the evidence that
raised it.  It joins the packet's revision as a separately identified
decision and writes no disposition, so the delta stays open and the proposed
value stays unaccepted.  The frozen legacy ``follow_up_plan_receipts`` over
``work_decisions`` is not extended for adopted-baseline projects (ADR-0084).

``reverse_review_packet`` is Undo.  It never deletes and never cascades: it
appends one compensating revision restoring each predecessor decision the
packet superseded, and refuses outright when a later act already depends on
a result.  A later *correction* is not this command — it is one new delta
resolved by a later attributable decision (#519), which leaves the original
receipt and its children exactly as recorded.
"""

from __future__ import annotations

from corridor.migrations.source_append_commands.roles import (
    RECORD_DECISION_ROLE,
    RUNTIME_LOGINS,
)


REVIEW_PACKET_TABLES = (
    "delta_follow_up_plans",
    "delta_follow_up_plan_evidence",
    "delta_review_packet_receipts",
    "delta_review_packet_children",
    "delta_review_packet_supports",
    "delta_review_packet_reversals",
)

PACKET_CHILD_OUTCOMES_SQL = (
    "'apply', 'keep_current', 'edit_and_apply', 'needs_coordination', 'defer'"
)
PACKET_SEMANTIC_OUTCOMES_SQL = "'apply', 'keep_current', 'edit_and_apply'"
PACKET_GROUPING_KEY_KINDS_SQL = (
    "'source_revision', 'coordination_question', 'shared_commitment'"
)

REVIEW_PACKET_SCHEMA = f"""
create table public.delta_follow_up_plans (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    delta_id bigint not null,
    revision_id bigint not null references public.project_record_revisions (id),
    open_question text not null,
    responsible_principal character varying(128),
    responsible_organization character varying(255),
    return_date timestamp with time zone,
    affected_scope jsonb not null,
    recorded_by_principal character varying(128) not null,
    idempotency_key character varying(160) not null,
    recorded_at timestamp with time zone not null,
    created_at timestamp with time zone not null default now(),
    constraint uq_delta_follow_up_plans_project_id unique (project_id, id),
    constraint uq_delta_follow_up_plans_key unique (project_id, idempotency_key),
    constraint fk_delta_follow_up_plans_delta
        foreign key (project_id, delta_id)
        references public.proposed_deltas (project_id, id),
    constraint ck_delta_follow_up_plans_question check (
        length(btrim(open_question)) > 0
    ),
    -- A coordination question owes its answer to someone: a named person, an
    -- External Organization, or both.  "Someone will look into it" is the
    -- state this decision exists to replace.
    constraint ck_delta_follow_up_plans_responsible check (
        responsible_principal is not null or responsible_organization is not null
    ),
    constraint ck_delta_follow_up_plans_scope_object check (
        jsonb_typeof(affected_scope) = 'object'
    ),
    constraint ck_delta_follow_up_plans_principal check (
        length(btrim(recorded_by_principal)) > 0
    )
);
create index ix_delta_follow_up_plans_project_id
    on public.delta_follow_up_plans (project_id);
create index ix_delta_follow_up_plans_delta_id
    on public.delta_follow_up_plans (delta_id);
create index ix_delta_follow_up_plans_revision_id
    on public.delta_follow_up_plans (revision_id);

create table public.delta_follow_up_plan_evidence (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    plan_id bigint not null,
    support_assessment_id bigint not null,
    ordinal integer not null,
    constraint uq_delta_follow_up_plan_evidence_member
        unique (plan_id, support_assessment_id),
    constraint uq_delta_follow_up_plan_evidence_ordinal unique (plan_id, ordinal),
    constraint fk_delta_follow_up_plan_evidence_plan
        foreign key (project_id, plan_id)
        references public.delta_follow_up_plans (project_id, id),
    constraint fk_delta_follow_up_plan_evidence_assessment
        foreign key (project_id, support_assessment_id)
        references public.support_assessments (project_id, id),
    constraint ck_delta_follow_up_plan_evidence_ordinal check (ordinal > 0)
);
create index ix_delta_follow_up_plan_evidence_project_id
    on public.delta_follow_up_plan_evidence (project_id);
create index ix_delta_follow_up_plan_evidence_assessment
    on public.delta_follow_up_plan_evidence (support_assessment_id);

create table public.delta_review_packet_receipts (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    revision_id bigint references public.project_record_revisions (id),
    grouping_rule_version character varying(64) not null,
    grouping_key_kind character varying(32) not null,
    grouping_key character varying(255) not null,
    decided_by_principal character varying(128) not null,
    observed_accepted_revision_id bigint
        references public.project_record_revisions (id),
    idempotency_key character varying(160) not null,
    decided_at timestamp with time zone not null,
    recorded_at timestamp with time zone not null default now(),
    constraint uq_delta_review_packet_receipts_project_id unique (project_id, id),
    constraint uq_delta_review_packet_receipts_key
        unique (project_id, idempotency_key),
    constraint ck_delta_review_packet_receipts_key_kind check (
        grouping_key_kind in ({PACKET_GROUPING_KEY_KINDS_SQL})
    ),
    constraint ck_delta_review_packet_receipts_rule_version check (
        length(btrim(grouping_rule_version)) > 0
    ),
    constraint ck_delta_review_packet_receipts_principal check (
        length(btrim(decided_by_principal)) > 0
    )
);
create index ix_delta_review_packet_receipts_project_id
    on public.delta_review_packet_receipts (project_id);
create index ix_delta_review_packet_receipts_revision_id
    on public.delta_review_packet_receipts (revision_id);

create table public.delta_review_packet_children (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    receipt_id bigint not null,
    ordinal integer not null,
    delta_id bigint not null,
    outcome character varying(32) not null,
    observed_source_revision character varying(128) not null,
    decision_id bigint,
    follow_up_plan_id bigint,
    deferral_id bigint references public.delta_deferrals (id),
    constraint uq_delta_review_packet_children_ordinal unique (receipt_id, ordinal),
    constraint uq_delta_review_packet_children_delta unique (receipt_id, delta_id),
    constraint uq_delta_review_packet_children_deferral unique (deferral_id),
    constraint fk_delta_review_packet_children_receipt
        foreign key (project_id, receipt_id)
        references public.delta_review_packet_receipts (project_id, id),
    constraint fk_delta_review_packet_children_delta
        foreign key (project_id, delta_id)
        references public.proposed_deltas (project_id, id),
    constraint fk_delta_review_packet_children_decision
        foreign key (project_id, decision_id)
        references public.delta_record_decisions (project_id, id),
    constraint fk_delta_review_packet_children_plan
        foreign key (project_id, follow_up_plan_id)
        references public.delta_follow_up_plans (project_id, id),
    constraint ck_delta_review_packet_children_outcome check (
        outcome in ({PACKET_CHILD_OUTCOMES_SQL})
    ),
    constraint ck_delta_review_packet_children_ordinal check (ordinal > 0),
    -- One outcome, one identity.  A child that named two, or none, would be
    -- exactly the lost child identity ADR-0035 forbids.
    constraint ck_delta_review_packet_children_one_identity check (
        (case when decision_id is null then 0 else 1 end)
        + (case when follow_up_plan_id is null then 0 else 1 end)
        + (case when deferral_id is null then 0 else 1 end) = 1
    ),
    constraint ck_delta_review_packet_children_semantic check (
        (outcome in ({PACKET_SEMANTIC_OUTCOMES_SQL})) = (decision_id is not null)
    ),
    constraint ck_delta_review_packet_children_coordination check (
        (outcome = 'needs_coordination') = (follow_up_plan_id is not null)
    ),
    constraint ck_delta_review_packet_children_defer check (
        (outcome = 'defer') = (deferral_id is not null)
    )
);
create index ix_delta_review_packet_children_project_id
    on public.delta_review_packet_children (project_id);
create index ix_delta_review_packet_children_receipt_id
    on public.delta_review_packet_children (receipt_id);
create index ix_delta_review_packet_children_delta_id
    on public.delta_review_packet_children (delta_id);

create table public.delta_review_packet_supports (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    receipt_id bigint not null,
    support_assessment_id bigint not null,
    ordinal integer not null,
    constraint uq_delta_review_packet_supports_member
        unique (receipt_id, support_assessment_id),
    constraint uq_delta_review_packet_supports_ordinal unique (receipt_id, ordinal),
    constraint fk_delta_review_packet_supports_receipt
        foreign key (project_id, receipt_id)
        references public.delta_review_packet_receipts (project_id, id),
    constraint fk_delta_review_packet_supports_assessment
        foreign key (project_id, support_assessment_id)
        references public.support_assessments (project_id, id),
    constraint ck_delta_review_packet_supports_ordinal check (ordinal > 0)
);
create index ix_delta_review_packet_supports_project_id
    on public.delta_review_packet_supports (project_id);
create index ix_delta_review_packet_supports_assessment
    on public.delta_review_packet_supports (support_assessment_id);

create table public.delta_review_packet_reversals (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    receipt_id bigint not null,
    revision_id bigint references public.project_record_revisions (id),
    reversed_by_principal character varying(128) not null,
    idempotency_key character varying(160) not null,
    reversed_at timestamp with time zone not null,
    recorded_at timestamp with time zone not null default now(),
    constraint uq_delta_review_packet_reversals_receipt unique (receipt_id),
    constraint uq_delta_review_packet_reversals_key
        unique (project_id, idempotency_key),
    constraint fk_delta_review_packet_reversals_receipt
        foreign key (project_id, receipt_id)
        references public.delta_review_packet_receipts (project_id, id),
    constraint ck_delta_review_packet_reversals_principal check (
        length(btrim(reversed_by_principal)) > 0
    )
);
create index ix_delta_review_packet_reversals_project_id
    on public.delta_review_packet_reversals (project_id);

-- The same guard the Resolve Delta tables carry: only the record-decision
-- role writes, and only by insert.  Without it a caller holding the schema
-- owner could record a packet act with no revision, no children, and no
-- authority -- the parallel path this ticket exists to close.
create trigger trg_delta_follow_up_plans_write
    before insert or update or delete on public.delta_follow_up_plans
    for each row execute function public.enforce_delta_record_decision_write();
create trigger trg_delta_follow_up_plans_truncate
    before truncate on public.delta_follow_up_plans
    for each statement execute function public.enforce_delta_record_decision_write();
create trigger trg_delta_follow_up_plan_evidence_write
    before insert or update or delete on public.delta_follow_up_plan_evidence
    for each row execute function public.enforce_delta_record_decision_write();
create trigger trg_delta_follow_up_plan_evidence_truncate
    before truncate on public.delta_follow_up_plan_evidence
    for each statement execute function public.enforce_delta_record_decision_write();
create trigger trg_delta_review_packet_receipts_write
    before insert or update or delete on public.delta_review_packet_receipts
    for each row execute function public.enforce_delta_record_decision_write();
create trigger trg_delta_review_packet_receipts_truncate
    before truncate on public.delta_review_packet_receipts
    for each statement execute function public.enforce_delta_record_decision_write();
create trigger trg_delta_review_packet_children_write
    before insert or update or delete on public.delta_review_packet_children
    for each row execute function public.enforce_delta_record_decision_write();
create trigger trg_delta_review_packet_children_truncate
    before truncate on public.delta_review_packet_children
    for each statement execute function public.enforce_delta_record_decision_write();
create trigger trg_delta_review_packet_supports_write
    before insert or update or delete on public.delta_review_packet_supports
    for each row execute function public.enforce_delta_record_decision_write();
create trigger trg_delta_review_packet_supports_truncate
    before truncate on public.delta_review_packet_supports
    for each statement execute function public.enforce_delta_record_decision_write();
create trigger trg_delta_review_packet_reversals_write
    before insert or update or delete on public.delta_review_packet_reversals
    for each row execute function public.enforce_delta_record_decision_write();
create trigger trg_delta_review_packet_reversals_truncate
    before truncate on public.delta_review_packet_reversals
    for each statement execute function public.enforce_delta_record_decision_write();
"""

REVIEW_PACKET_SCHEMA_DOWN = """
drop table if exists public.delta_review_packet_reversals cascade;
drop table if exists public.delta_review_packet_supports cascade;
drop table if exists public.delta_review_packet_children cascade;
drop table if exists public.delta_review_packet_receipts cascade;
drop table if exists public.delta_follow_up_plan_evidence cascade;
drop table if exists public.delta_follow_up_plans cascade;
"""

RECORD_DELTA_FOLLOW_UP_PLAN = """
create function public.record_delta_follow_up_plan(
    p_project_id bigint,
    p_delta_id bigint,
    p_revision_id bigint,
    p_principal character varying,
    p_question text,
    p_responsible_principal character varying,
    p_responsible_organization character varying,
    p_return_date timestamp with time zone,
    p_affected_scope jsonb,
    p_evidence_ids bigint[],
    p_recorded_at timestamp with time zone,
    p_idempotency_key character varying
) returns jsonb
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            prior delta_follow_up_plans%ROWTYPE;
            evidence_id bigint;
            slot integer := 0;
            plan_id bigint;
        begin
            if p_principal is null or length(btrim(p_principal)) = 0 then
                raise exception 'review_packet:missing_principal a Follow-up Plan names the person recording it'
                    using errcode='23514';
            end if;
            if p_idempotency_key is null
               or length(btrim(p_idempotency_key)) = 0 then
                raise exception 'review_packet:missing_idempotency_key a Follow-up Plan needs an idempotency key'
                    using errcode='23514';
            end if;
            if p_question is null or length(btrim(p_question)) = 0 then
                raise exception 'review_packet:missing_question a Follow-up Plan records the exact open question'
                    using errcode='23514';
            end if;
            if (p_responsible_principal is null
                or length(btrim(p_responsible_principal)) = 0)
               and (p_responsible_organization is null
                    or length(btrim(p_responsible_organization)) = 0) then
                raise exception 'review_packet:missing_responsible_party a Follow-up Plan names the person or organization who owes the answer'
                    using errcode='23514';
            end if;
            if p_recorded_at is null then
                raise exception 'review_packet:missing_decided_at a Follow-up Plan records when it was created'
                    using errcode='23514';
            end if;

            select * into prior from delta_follow_up_plans
             where project_id = p_project_id
               and idempotency_key = p_idempotency_key;
            if found then
                if prior.delta_id <> p_delta_id then
                    raise exception 'review_packet:key_bound_to_other_content the Follow-up Plan key is already bound to another delta'
                        using errcode='23514';
                end if;
                return jsonb_build_object('plan_id', prior.id, 'created', false);
            end if;

            if not exists (
                select 1 from proposed_deltas
                 where id = p_delta_id and project_id = p_project_id
            ) then
                raise exception 'review_packet:cross_project_delta Proposed Delta % is not this project''s to coordinate', p_delta_id
                    using errcode='23514';
            end if;
            if exists (
                select 1 from delta_dispositions where delta_id = p_delta_id
            ) then
                raise exception 'review_packet:already_resolved Proposed Delta % is already resolved', p_delta_id
                    using errcode='23514';
            end if;
            if exists (
                select 1 from delta_supersessions where prior_delta_id = p_delta_id
            ) then
                raise exception 'review_packet:superseded_delta Proposed Delta % was superseded by a newer source version', p_delta_id
                    using errcode='23514';
            end if;
            if not exists (
                select 1 from project_record_revisions
                 where id = p_revision_id and project_id = p_project_id
            ) then
                raise exception 'review_packet:cross_project_revision the packet revision belongs to another project'
                    using errcode='23514';
            end if;

            -- The evidence that raised the question is cited support, never a
            -- locator check (ADR-0082).
            foreach evidence_id in array coalesce(p_evidence_ids, '{}'::bigint[])
            loop
                if not exists (
                    select 1 from support_assessments
                     where id = evidence_id and project_id = p_project_id
                       and superseded_by is null
                ) then
                    raise exception 'review_packet:missing_support Support Assessment % is not an effective assessment of this project', evidence_id
                        using errcode='23514';
                end if;
            end loop;

            insert into delta_follow_up_plans (
                project_id, delta_id, revision_id, open_question,
                responsible_principal, responsible_organization, return_date,
                affected_scope, recorded_by_principal, idempotency_key,
                recorded_at
            ) values (
                p_project_id, p_delta_id, p_revision_id, p_question,
                nullif(btrim(coalesce(p_responsible_principal, '')), ''),
                nullif(btrim(coalesce(p_responsible_organization, '')), ''),
                p_return_date, coalesce(p_affected_scope, '{}'::jsonb),
                p_principal, p_idempotency_key, p_recorded_at
            ) returning id into plan_id;

            foreach evidence_id in array coalesce(p_evidence_ids, '{}'::bigint[])
            loop
                slot := slot + 1;
                insert into delta_follow_up_plan_evidence (
                    project_id, plan_id, support_assessment_id, ordinal
                ) values (p_project_id, plan_id, evidence_id, slot);
            end loop;

            return jsonb_build_object('plan_id', plan_id, 'created', true);
        end; $$;
"""

RECORD_DELTA_FOLLOW_UP_PLAN_SIGNATURE = (
    "(bigint, bigint, bigint, character varying, text, character varying, "
    "character varying, timestamp with time zone, jsonb, bigint[], "
    "timestamp with time zone, character varying)"
)

RECORD_REVIEW_PACKET_RECEIPT = f"""
create function public.record_review_packet_receipt(
    p_project_id bigint,
    p_revision_id bigint,
    p_grouping_rule_version character varying,
    p_grouping_key_kind character varying,
    p_grouping_key character varying,
    p_principal character varying,
    p_observed_accepted_revision_id bigint,
    p_decided_at timestamp with time zone,
    p_idempotency_key character varying,
    p_children jsonb,
    p_support_ids bigint[]
) returns jsonb
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            prior delta_review_packet_receipts%ROWTYPE;
            child jsonb;
            expected integer := 0;
            outcome text;
            child_delta bigint;
            decision_id bigint;
            plan_id bigint;
            deferral_id bigint;
            support_id bigint;
            slot integer := 0;
            receipt_id bigint;
            seen bigint[] := '{{}}';
        begin
            if p_principal is null or length(btrim(p_principal)) = 0 then
                raise exception 'review_packet:missing_principal a packet act names the person deciding'
                    using errcode='23514';
            end if;
            if p_idempotency_key is null
               or length(btrim(p_idempotency_key)) = 0 then
                raise exception 'review_packet:missing_idempotency_key a packet act needs an idempotency key'
                    using errcode='23514';
            end if;
            if p_grouping_rule_version is null
               or length(btrim(p_grouping_rule_version)) = 0 then
                raise exception 'review_packet:missing_grouping_rule the receipt records the grouping-rule version the packet was built by'
                    using errcode='23514';
            end if;
            if p_grouping_key_kind not in ({PACKET_GROUPING_KEY_KINDS_SQL}) then
                raise exception 'review_packet:invalid_grouping_key % is not an adaptive packet key', p_grouping_key_kind
                    using errcode='23514';
            end if;
            if p_decided_at is null then
                raise exception 'review_packet:missing_decided_at a packet act records when it was decided'
                    using errcode='23514';
            end if;
            if p_children is null
               or jsonb_typeof(p_children) <> 'array'
               or jsonb_array_length(p_children) = 0 then
                raise exception 'review_packet:empty_packet a packet act names the exact ordered child set it decided'
                    using errcode='23514';
            end if;

            select * into prior from delta_review_packet_receipts
             where project_id = p_project_id
               and idempotency_key = p_idempotency_key;
            if found then
                return jsonb_build_object('receipt_id', prior.id, 'created', false);
            end if;

            if p_revision_id is not null and not exists (
                select 1 from project_record_revisions
                 where id = p_revision_id and project_id = p_project_id
            ) then
                raise exception 'review_packet:cross_project_revision the packet revision belongs to another project'
                    using errcode='23514';
            end if;

            for child in select value from jsonb_array_elements(p_children)
            loop
                expected := expected + 1;
                if (child->>'ordinal')::integer <> expected then
                    raise exception 'review_packet:unordered_children a packet receipt records its children in the order they were shown'
                        using errcode='23514';
                end if;
                outcome := child->>'outcome';
                if outcome not in ({PACKET_CHILD_OUTCOMES_SQL}) then
                    raise exception 'review_packet:invalid_outcome % is not a packet child outcome', outcome
                        using errcode='23514';
                end if;
                child_delta := (child->>'delta_id')::bigint;
                if child_delta = any(seen) then
                    raise exception 'review_packet:duplicate_child Proposed Delta % appears twice in one packet', child_delta
                        using errcode='23514';
                end if;
                seen := seen || child_delta;
                if not exists (
                    select 1 from proposed_deltas
                     where id = child_delta and project_id = p_project_id
                ) then
                    raise exception 'review_packet:cross_project_delta Proposed Delta % is not this project''s to decide', child_delta
                        using errcode='23514';
                end if;
                if child->>'observed_source_revision' is null
                   or length(btrim(child->>'observed_source_revision')) = 0 then
                    raise exception 'review_packet:missing_source_revision every child names the source version the coordinator read'
                        using errcode='23514';
                end if;

                decision_id := (child->>'decision_id')::bigint;
                plan_id := (child->>'follow_up_plan_id')::bigint;
                deferral_id := (child->>'deferral_id')::bigint;
                if outcome in ({PACKET_SEMANTIC_OUTCOMES_SQL}) then
                    if decision_id is null or plan_id is not null
                       or deferral_id is not null then
                        raise exception 'review_packet:invalid_outcome a semantic child names exactly its Human Record Decision'
                            using errcode='23514';
                    end if;
                    if not exists (
                        select 1 from delta_record_decisions
                         where id = decision_id and project_id = p_project_id
                           and delta_id = child_delta
                           and revision_id = p_revision_id
                    ) then
                        raise exception 'review_packet:child_identity_mismatch the named decision is not this packet''s decision on delta %', child_delta
                            using errcode='23514';
                    end if;
                elsif outcome = 'needs_coordination' then
                    if plan_id is null or decision_id is not null
                       or deferral_id is not null then
                        raise exception 'review_packet:invalid_outcome a coordination child names exactly its Follow-up Plan'
                            using errcode='23514';
                    end if;
                    if not exists (
                        select 1 from delta_follow_up_plans
                         where id = plan_id and project_id = p_project_id
                           and delta_id = child_delta
                           and revision_id = p_revision_id
                    ) then
                        raise exception 'review_packet:child_identity_mismatch the named Follow-up Plan is not this packet''s plan on delta %', child_delta
                            using errcode='23514';
                    end if;
                else
                    if deferral_id is null or decision_id is not null
                       or plan_id is not null then
                        raise exception 'review_packet:invalid_outcome a dated Defer names exactly its scheduling receipt'
                            using errcode='23514';
                    end if;
                    if not exists (
                        select 1 from delta_deferrals
                         where id = deferral_id and project_id = p_project_id
                           and delta_id = child_delta
                    ) then
                        raise exception 'review_packet:child_identity_mismatch the named scheduling receipt is not this packet''s deferral on delta %', child_delta
                            using errcode='23514';
                    end if;
                end if;
            end loop;

            -- A scheduling-only act writes no Project Record revision, and an
            -- act carrying any semantic or Follow-up Plan decision writes
            -- exactly one (ADR-0084, ADR-0085).
            if p_revision_id is null and exists (
                select 1 from jsonb_array_elements(p_children) as element
                 where element.value->>'outcome' <> 'defer'
            ) then
                raise exception 'review_packet:missing_revision a packet carrying a semantic or Follow-up Plan decision commits one Project Record revision'
                    using errcode='23514';
            end if;
            if p_revision_id is not null and not exists (
                select 1 from jsonb_array_elements(p_children) as element
                 where element.value->>'outcome' <> 'defer'
            ) then
                raise exception 'review_packet:unexpected_revision a packet of dated deferrals alone writes no Project Record revision'
                    using errcode='23514';
            end if;

            foreach support_id in array coalesce(p_support_ids, '{{}}'::bigint[])
            loop
                if not exists (
                    select 1 from support_assessments
                     where id = support_id and project_id = p_project_id
                       and superseded_by is null
                ) then
                    raise exception 'review_packet:missing_support Support Assessment % is not an effective assessment of this project', support_id
                        using errcode='23514';
                end if;
            end loop;

            insert into delta_review_packet_receipts (
                project_id, revision_id, grouping_rule_version,
                grouping_key_kind, grouping_key, decided_by_principal,
                observed_accepted_revision_id, idempotency_key, decided_at
            ) values (
                p_project_id, p_revision_id, p_grouping_rule_version,
                p_grouping_key_kind, p_grouping_key, p_principal,
                p_observed_accepted_revision_id, p_idempotency_key, p_decided_at
            ) returning id into receipt_id;

            for child in select value from jsonb_array_elements(p_children)
            loop
                insert into delta_review_packet_children (
                    project_id, receipt_id, ordinal, delta_id, outcome,
                    observed_source_revision, decision_id, follow_up_plan_id,
                    deferral_id
                ) values (
                    p_project_id, receipt_id, (child->>'ordinal')::integer,
                    (child->>'delta_id')::bigint, child->>'outcome',
                    child->>'observed_source_revision',
                    (child->>'decision_id')::bigint,
                    (child->>'follow_up_plan_id')::bigint,
                    (child->>'deferral_id')::bigint
                );
            end loop;

            foreach support_id in array coalesce(p_support_ids, '{{}}'::bigint[])
            loop
                slot := slot + 1;
                insert into delta_review_packet_supports (
                    project_id, receipt_id, support_assessment_id, ordinal
                ) values (p_project_id, receipt_id, support_id, slot);
            end loop;

            return jsonb_build_object('receipt_id', receipt_id, 'created', true);
        end; $$;
"""

RECORD_REVIEW_PACKET_RECEIPT_SIGNATURE = (
    "(bigint, bigint, character varying, character varying, character varying, "
    "character varying, bigint, timestamp with time zone, character varying, "
    "jsonb, bigint[])"
)

REVERSE_REVIEW_PACKET = """
create function public.reverse_review_packet(
    p_project_id bigint,
    p_receipt_id bigint,
    p_principal character varying,
    p_reversed_at timestamp with time zone,
    p_idempotency_key character varying
) returns jsonb
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            receipt delta_review_packet_receipts%ROWTYPE;
            prior delta_review_packet_reversals%ROWTYPE;
            written fact_decisions%ROWTYPE;
            predecessor fact_decisions%ROWTYPE;
            restored bigint;
            revision bigint;
            predecessor_revision bigint;
            reversal_id bigint;
            compensated bigint[] := '{}';
        begin
            if p_principal is null or length(btrim(p_principal)) = 0 then
                raise exception 'review_packet:missing_principal an Undo names the person reversing the act'
                    using errcode='23514';
            end if;
            if p_idempotency_key is null
               or length(btrim(p_idempotency_key)) = 0 then
                raise exception 'review_packet:missing_idempotency_key an Undo needs an idempotency key'
                    using errcode='23514';
            end if;
            if p_reversed_at is null then
                raise exception 'review_packet:missing_decided_at an Undo records when it was made'
                    using errcode='23514';
            end if;

            select * into prior from delta_review_packet_reversals
             where project_id = p_project_id
               and idempotency_key = p_idempotency_key;
            if found then
                return jsonb_build_object(
                    'reversal_id', prior.id,
                    'revision_id', prior.revision_id,
                    'restored_fact_decision_ids', '[]'::jsonb,
                    'created', false
                );
            end if;

            select * into receipt from delta_review_packet_receipts
             where id = p_receipt_id and project_id = p_project_id;
            if not found then
                raise exception 'review_packet:cross_project_receipt packet receipt % is not this project''s to undo', p_receipt_id
                    using errcode='23514';
            end if;
            if exists (
                select 1 from delta_review_packet_reversals
                 where receipt_id = p_receipt_id
            ) then
                raise exception 'review_packet:already_reversed packet receipt % was already undone', p_receipt_id
                    using errcode='23514';
            end if;

            -- Undo never cascades through later work (ADR-0035).  If anything
            -- the packet made effective has since been superseded, or a delta
            -- it scheduled or raised a question about has since been resolved,
            -- a later act depends on this one and the correct move is a
            -- targeted correction, not an Undo.
            if receipt.revision_id is not null and exists (
                select 1 from fact_decisions
                 where revision_id = receipt.revision_id
                   and superseded_by is not null
            ) then
                raise exception 'review_packet:later_act_depends a later decision already superseded a value this packet made effective'
                    using errcode='23514';
            end if;
            if exists (
                select 1
                  from delta_review_packet_children c
                  join delta_dispositions d on d.delta_id = c.delta_id
                 where c.receipt_id = p_receipt_id
                   and (c.deferral_id is not null or c.follow_up_plan_id is not null)
            ) then
                raise exception 'review_packet:later_act_depends a delta this packet left open has since been resolved'
                    using errcode='23514';
            end if;

            revision := null;
            if receipt.revision_id is not null then
                select max(id) into predecessor_revision
                  from project_record_revisions where project_id = p_project_id;
                insert into project_record_revisions (
                    project_id, predecessor_revision_id, command_type,
                    human_principal, released_policy, idempotency_key
                ) values (
                    p_project_id, predecessor_revision, 'reverse_review_packet',
                    p_principal, null, p_idempotency_key
                ) returning id into revision;

                for written in
                    select * from fact_decisions
                     where revision_id = receipt.revision_id
                     order by id
                loop
                    select * into predecessor from fact_decisions
                     where superseded_by = written.id;
                    restored := nextval('fact_decisions_id_seq');
                    update fact_decisions set superseded_by = restored
                     where id = written.id;
                    if predecessor.id is not null then
                        -- The predecessor value returns as a new decision; the
                        -- retired row itself is never rewritten.
                        insert into fact_decisions (
                            id, project_id, fact_id, subject_key, fact_type,
                            revision_id, disposition, superseded_by
                        ) values (
                            restored, p_project_id, predecessor.fact_id,
                            predecessor.subject_key, predecessor.fact_type,
                            revision, predecessor.disposition, null
                        );
                    else
                        -- Nothing stood here before the packet, so the
                        -- compensating decision says the value is not added.
                        insert into fact_decisions (
                            id, project_id, fact_id, subject_key, fact_type,
                            revision_id, disposition, superseded_by
                        ) values (
                            restored, p_project_id, written.fact_id,
                            written.subject_key, written.fact_type,
                            revision, 'do_not_add', null
                        );
                    end if;
                    compensated := compensated || restored;
                end loop;
            end if;

            insert into delta_review_packet_reversals (
                project_id, receipt_id, revision_id, reversed_by_principal,
                idempotency_key, reversed_at
            ) values (
                p_project_id, p_receipt_id, revision, p_principal,
                p_idempotency_key, p_reversed_at
            ) returning id into reversal_id;

            return jsonb_build_object(
                'reversal_id', reversal_id,
                'revision_id', revision,
                'restored_fact_decision_ids', to_jsonb(compensated),
                'created', true
            );
        end; $$;
"""

REVERSE_REVIEW_PACKET_SIGNATURE = (
    "(bigint, bigint, character varying, timestamp with time zone, "
    "character varying)"
)

REVIEW_PACKET_COMMANDS = {
    "record_delta_follow_up_plan": RECORD_DELTA_FOLLOW_UP_PLAN_SIGNATURE,
    "record_review_packet_receipt": RECORD_REVIEW_PACKET_RECEIPT_SIGNATURE,
    "reverse_review_packet": REVERSE_REVIEW_PACKET_SIGNATURE,
}


def upgrade(op) -> None:
    op.execute(REVIEW_PACKET_SCHEMA)
    for table in REVIEW_PACKET_TABLES:
        # One guided packet act is accepted authority: the application reads
        # the receipt, its children, the Follow-up Plans, and the Undo, and
        # writes none of them.  A new table arrives with the schema's default
        # privileges, so the write half is taken back explicitly.
        op.execute(f"grant select on public.{table} to {RUNTIME_LOGINS}")
        op.execute(
            f"revoke insert, update, delete, truncate on public.{table} "
            f"from {RUNTIME_LOGINS}"
        )
        op.execute(
            f"grant select, insert on public.{table} to {RECORD_DECISION_ROLE}"
        )
        op.execute(
            f"grant usage, select on sequence public.{table}_id_seq "
            f"to {RECORD_DECISION_ROLE}"
        )
    for body in (
        RECORD_DELTA_FOLLOW_UP_PLAN,
        RECORD_REVIEW_PACKET_RECEIPT,
        REVERSE_REVIEW_PACKET,
    ):
        op.execute(body)
    for name, signature in REVIEW_PACKET_COMMANDS.items():
        op.execute(
            f"alter function public.{name}{signature} owner to {RECORD_DECISION_ROLE}"
        )
        op.execute(f"revoke all on function public.{name}{signature} from public")
        # Saving a packet, recording a Follow-up Plan, and undoing the act are
        # attributable human acts, so they join the other decision commands on
        # the web capability alone.
        op.execute(
            f"grant execute on function public.{name}{signature} to corridor_web"
        )


def downgrade(op) -> None:
    for name, signature in REVIEW_PACKET_COMMANDS.items():
        op.execute(f"drop function if exists public.{name}{signature}")
    for table in REVIEW_PACKET_TABLES:
        op.execute(
            f"do $$ begin "
            f"if exists (select 1 from pg_tables where schemaname = 'public' "
            f"and tablename = '{table}') then "
            f"revoke select on public.{table} from {RUNTIME_LOGINS}; "
            f"end if; end $$;"
        )
    op.execute(REVIEW_PACKET_SCHEMA_DOWN)
