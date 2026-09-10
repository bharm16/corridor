"""Resolve Delta (#519, ADR-0076, ADR-0083, ADR-0084, ADR-0085).

Resolving one Proposed Delta is the act that finally moves the accepted
record, so it belongs to the record-decision role and to nothing else.  The
occurrence, the group, and the lineage links stay with the source-append
role above; ``delta_dispositions``, the decision that binds one disposition
to its Project Record revision, the Support Assessments that decision relied
on, and the Work List scheduling receipt move here.

Three identities are again deliberately three things:

  * ``delta_dispositions`` (created above, #518) stays the lifecycle marker
    the live-state query walks: one per delta, so a delta resolves once.
  * ``delta_record_decisions`` is the **authority binding**: which revision
    carried the decision, which typed effect it had, which principal made
    it, what accepted revision they had observed, and — for an edit — the
    constrained basis that made an edited value source-backed rather than
    free text.
  * ``delta_decision_supports`` names the **effective Support Assessments**
    (#530) the decision relied on.  Locator validity is never consulted
    here, so a passed Source Passage Check can never stand in for support.

``resolve_proposed_delta_decision`` is the one writer.  Passing a revision
makes it a child of #526's packet-owned transaction: it writes the decision
into that revision and creates no intermediate one.  Passing none makes it a
standalone act that opens exactly one revision itself.  Either way the same
validation runs before any authoritative write, so the two contexts cannot
drift into two sets of decision rules.

``defer_proposed_delta`` writes only the dated scheduling receipt: the delta
stays open and no Project Record revision exists (ADR-0084, ADR-0085).
"""

from __future__ import annotations

from corridor.migrations.source_append_commands.roles import (
    RUNTIME_LOGINS,
    SOURCE_APPEND_ROLE,
)


RESOLVE_DELTA_ROLE = "corridor_fact_decision_writer"

# Every refusal the statements below raise, listed once.  The plpgsql is the
# authority and its tokens are its own words; this list exists so the agreement
# with the readable half is *declared* rather than discovered when the two
# vocabularies drift.  `corridor.delta_refusals` declares each of these codes
# with the outcome status a screen routes on and which half may raise it, and
# `tests/test_delta_resolution.py` parses the `resolve_delta:<code>` tokens out
# of this module with the runtime's own expression and fails if this list, the
# declared vocabulary, or the statements disagree.  Nothing reads this constant
# at run time: adding a name to it grants no refusal, and removing a raise from
# a statement is what actually retires one.
RESOLVE_DELTA_REFUSAL_CODES = (
    "already_effective",
    "already_resolved",
    "ambiguous_effective_decision",
    "append_only",
    "cross_project_delta",
    "cross_project_fact",
    "cross_project_revision",
    "field_mismatch",
    "invalid_action",
    "key_bound_to_other_content",
    "missing_decided_at",
    "missing_idempotency_key",
    "missing_principal",
    "missing_record_effect",
    "missing_support",
    "missing_wake_condition",
    "stale_accepted_revision",
    "subject_mismatch",
    "superseded_delta",
    "unauthorized_writer",
)

RESOLVE_DELTA_TABLES = (
    "delta_record_decisions",
    "delta_decision_supports",
)

RESOLVE_DELTA_EFFECT_KINDS_SQL = (
    "'new_subject', 'changed_field', 'timing', 'organization', "
    "'apparent_removal', 'contradiction', 'schedule_key_date', 'closure'"
)

RESOLVE_DELTA_SCHEMA = f"""
create table public.delta_record_decisions (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    delta_id bigint not null,
    disposition_id bigint not null references public.delta_dispositions (id),
    revision_id bigint not null references public.project_record_revisions (id),
    disposition character varying(32) not null,
    effect_kind character varying(32) not null,
    organization_change_kind character varying(32),
    decided_by_principal character varying(128) not null,
    observed_accepted_revision_id bigint
        references public.project_record_revisions (id),
    edit_basis jsonb,
    idempotency_key character varying(160) not null,
    decided_at timestamp with time zone not null,
    recorded_at timestamp with time zone not null default now(),
    constraint uq_delta_record_decisions_disposition unique (disposition_id),
    constraint uq_delta_record_decisions_delta unique (delta_id),
    constraint uq_delta_record_decisions_key unique (project_id, idempotency_key),
    constraint uq_delta_record_decisions_project_id unique (project_id, id),
    constraint fk_delta_record_decisions_delta
        foreign key (project_id, delta_id)
        references public.proposed_deltas (project_id, id),
    constraint ck_delta_record_decisions_disposition check (
        disposition in ('accept', 'edit', 'reject')
    ),
    constraint ck_delta_record_decisions_effect_kind check (
        effect_kind in ({RESOLVE_DELTA_EFFECT_KINDS_SQL})
    ),
    -- Correction and changed ownership fail differently, so an organization
    -- effect says which one it was and no other effect may claim one.
    constraint ck_delta_record_decisions_organization check (
        (effect_kind = 'organization'
         and organization_change_kind in ('correction', 'changed_ownership'))
        or (effect_kind <> 'organization' and organization_change_kind is null)
    ),
    -- An edit carries the basis that made its value source-backed; accept and
    -- reject never carry one.
    constraint ck_delta_record_decisions_edit_basis check (
        (disposition = 'edit') = (edit_basis is not null)
    ),
    constraint ck_delta_record_decisions_principal check (
        length(btrim(decided_by_principal)) > 0
    )
);
create index ix_delta_record_decisions_project_id
    on public.delta_record_decisions (project_id);
create index ix_delta_record_decisions_revision_id
    on public.delta_record_decisions (revision_id);

create table public.delta_decision_supports (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    decision_id bigint not null,
    support_assessment_id bigint not null,
    ordinal integer not null,
    constraint uq_delta_decision_supports_member
        unique (decision_id, support_assessment_id),
    constraint uq_delta_decision_supports_ordinal unique (decision_id, ordinal),
    constraint fk_delta_decision_supports_decision
        foreign key (project_id, decision_id)
        references public.delta_record_decisions (project_id, id),
    constraint fk_delta_decision_supports_assessment
        foreign key (project_id, support_assessment_id)
        references public.support_assessments (project_id, id),
    constraint ck_delta_decision_supports_ordinal check (ordinal > 0)
);
create index ix_delta_decision_supports_project_id
    on public.delta_decision_supports (project_id);
create index ix_delta_decision_supports_assessment
    on public.delta_decision_supports (support_assessment_id);

create function public.enforce_delta_record_decision_write() returns trigger
    language plpgsql
    as $$
        begin
            if current_user <> '{RESOLVE_DELTA_ROLE}' then
                raise exception 'resolve_delta:unauthorized_writer Resolve Delta requires the typed decision command'
                    using errcode='23514';
            end if;
            if tg_op <> 'INSERT' then
                raise exception 'resolve_delta:append_only a wrong decision is corrected by a later attributable decision, never by an edit'
                    using errcode='23514';
            end if;
            return new;
        end; $$;

create trigger trg_delta_record_decisions_write
    before insert or update or delete on public.delta_record_decisions
    for each row execute function public.enforce_delta_record_decision_write();
create trigger trg_delta_record_decisions_truncate
    before truncate on public.delta_record_decisions
    for each statement
    execute function public.enforce_delta_record_decision_write();
create trigger trg_delta_decision_supports_write
    before insert or update or delete on public.delta_decision_supports
    for each row execute function public.enforce_delta_record_decision_write();
create trigger trg_delta_decision_supports_truncate
    before truncate on public.delta_decision_supports
    for each statement
    execute function public.enforce_delta_record_decision_write();

-- The disposition and the Work List scheduling receipt are written by the
-- same commands, so the same guard holds them.  Without this a caller
-- holding the schema owner could still record a resolution with no
-- revision, no cited support, and no authority row -- the exact parallel
-- path this ticket exists to close.
create trigger trg_delta_dispositions_write
    before insert or update or delete on public.delta_dispositions
    for each row execute function public.enforce_delta_record_decision_write();
create trigger trg_delta_dispositions_truncate
    before truncate on public.delta_dispositions
    for each statement
    execute function public.enforce_delta_record_decision_write();
create trigger trg_delta_deferrals_write
    before insert or update or delete on public.delta_deferrals
    for each row execute function public.enforce_delta_record_decision_write();
create trigger trg_delta_deferrals_truncate
    before truncate on public.delta_deferrals
    for each statement
    execute function public.enforce_delta_record_decision_write();
"""

RESOLVE_DELTA_SCHEMA_DOWN = """
drop trigger if exists trg_delta_deferrals_truncate on public.delta_deferrals;
drop trigger if exists trg_delta_deferrals_write on public.delta_deferrals;
drop trigger if exists trg_delta_dispositions_truncate on public.delta_dispositions;
drop trigger if exists trg_delta_dispositions_write on public.delta_dispositions;
drop trigger if exists trg_delta_decision_supports_truncate on public.delta_decision_supports;
drop trigger if exists trg_delta_decision_supports_write on public.delta_decision_supports;
drop trigger if exists trg_delta_record_decisions_truncate on public.delta_record_decisions;
drop trigger if exists trg_delta_record_decisions_write on public.delta_record_decisions;
drop table if exists public.delta_decision_supports cascade;
drop table if exists public.delta_record_decisions cascade;
drop function if exists public.enforce_delta_record_decision_write() cascade;
"""

OPEN_DELTA_RESOLUTION_REVISION = """
create function public.open_delta_resolution_revision(
    p_project_id bigint,
    p_principal character varying,
    p_idempotency_key character varying
) returns bigint
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            existing bigint;
            predecessor bigint;
            opened bigint;
        begin
            if p_principal is null or length(btrim(p_principal)) = 0 then
                raise exception 'resolve_delta:missing_principal a Resolve Delta revision names the person deciding'
                    using errcode='23514';
            end if;
            if p_idempotency_key is null
               or length(btrim(p_idempotency_key)) = 0 then
                raise exception 'resolve_delta:missing_idempotency_key a Resolve Delta revision needs an idempotency key'
                    using errcode='23514';
            end if;
            select id into existing from project_record_revisions
             where project_id = p_project_id
               and idempotency_key = p_idempotency_key;
            if found then
                return existing;
            end if;
            select max(id) into predecessor from project_record_revisions
             where project_id = p_project_id;
            insert into project_record_revisions (
                project_id, predecessor_revision_id, command_type,
                human_principal, released_policy, idempotency_key
            ) values (
                p_project_id, predecessor, 'resolve_delta',
                p_principal, null, p_idempotency_key
            ) returning id into opened;
            return opened;
        end; $$;
"""

OPEN_DELTA_RESOLUTION_REVISION_SIGNATURE = (
    "(bigint, character varying, character varying)"
)

RESOLVE_PROPOSED_DELTA_DECISION = f"""
create function public.resolve_proposed_delta_decision(
    p_project_id bigint,
    p_delta_id bigint,
    p_disposition character varying,
    p_effect_kind character varying,
    p_organization_change_kind character varying,
    p_principal character varying,
    p_idempotency_key character varying,
    p_observed_accepted_revision_id bigint,
    p_decided_at timestamp with time zone,
    p_effective_value jsonb,
    p_rationale text,
    p_edit_basis jsonb,
    p_record_effects jsonb,
    p_support_assessment_ids bigint[],
    p_revision_id bigint
) returns jsonb
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            delta proposed_deltas%ROWTYPE;
            prior delta_record_decisions%ROWTYPE;
            effect jsonb;
            decided facts%ROWTYPE;
            effect_disposition text;
            effective_count integer;
            live_predecessor bigint;
            live_revision bigint;
            support_id bigint;
            slot integer := 0;
            revision bigint;
            disposition_id bigint;
            decision_id bigint;
            new_decision bigint;
            written bigint[] := '{{}}';
            opened boolean := false;
        begin
            if p_principal is null or length(btrim(p_principal)) = 0 then
                raise exception 'resolve_delta:missing_principal a Resolve Delta names the responsible human principal'
                    using errcode='23514';
            end if;
            if p_idempotency_key is null
               or length(btrim(p_idempotency_key)) = 0 then
                raise exception 'resolve_delta:missing_idempotency_key a Resolve Delta needs an idempotency key'
                    using errcode='23514';
            end if;
            if p_disposition not in ('accept', 'edit', 'reject') then
                raise exception 'resolve_delta:invalid_action % is not a semantic delta disposition', p_disposition
                    using errcode='23514';
            end if;
            if p_effect_kind not in ({RESOLVE_DELTA_EFFECT_KINDS_SQL}) then
                raise exception 'resolve_delta:invalid_action % is not a typed delta effect', p_effect_kind
                    using errcode='23514';
            end if;
            if p_decided_at is null then
                raise exception 'resolve_delta:missing_decided_at a Resolve Delta records when it was decided'
                    using errcode='23514';
            end if;

            -- A replay of the same act returns what it already wrote.
            select * into prior from delta_record_decisions
             where project_id = p_project_id
               and idempotency_key = p_idempotency_key;
            if found then
                if prior.delta_id <> p_delta_id
                   or prior.disposition <> p_disposition then
                    raise exception 'resolve_delta:key_bound_to_other_content the Resolve Delta key is already bound to a different decision'
                        using errcode='23514';
                end if;
                return jsonb_build_object(
                    'revision_id', prior.revision_id,
                    'disposition_id', prior.disposition_id,
                    'decision_id', prior.id,
                    'fact_decision_ids', (
                        select coalesce(jsonb_agg(fd.id order by fd.id), '[]'::jsonb)
                          from fact_decisions fd
                         where fd.revision_id = prior.revision_id
                    ),
                    'created', false
                );
            end if;

            select * into delta from proposed_deltas
             where id = p_delta_id and project_id = p_project_id;
            if not found then
                raise exception 'resolve_delta:cross_project_delta Proposed Delta % is not this project''s to resolve', p_delta_id
                    using errcode='23514';
            end if;
            if exists (
                select 1 from delta_dispositions where delta_id = p_delta_id
            ) then
                raise exception 'resolve_delta:already_resolved Proposed Delta % is already resolved; correct it with a later decision', p_delta_id
                    using errcode='23514';
            end if;
            if exists (
                select 1 from delta_supersessions where prior_delta_id = p_delta_id
            ) then
                raise exception 'resolve_delta:superseded_delta Proposed Delta % was superseded by a newer source version', p_delta_id
                    using errcode='23514';
            end if;

            if p_revision_id is not null and not exists (
                select 1 from project_record_revisions
                 where id = p_revision_id and project_id = p_project_id
            ) then
                raise exception 'resolve_delta:cross_project_revision the packet revision belongs to another project'
                    using errcode='23514';
            end if;
            if p_observed_accepted_revision_id is not null and not exists (
                select 1 from project_record_revisions
                 where id = p_observed_accepted_revision_id
                   and project_id = p_project_id
            ) then
                raise exception 'resolve_delta:cross_project_revision the observed accepted revision belongs to another project'
                    using errcode='23514';
            end if;

            if p_disposition = 'reject' then
                if p_record_effects is not null
                   and jsonb_array_length(p_record_effects) > 0 then
                    raise exception 'resolve_delta:invalid_action keeping the current accepted value changes no effective decision'
                        using errcode='23514';
                end if;
            else
                if p_record_effects is null
                   or jsonb_typeof(p_record_effects) <> 'array'
                   or jsonb_array_length(p_record_effects) = 0 then
                    raise exception 'resolve_delta:missing_record_effect an accepted or edited value names the Source Facts it makes effective'
                        using errcode='23514';
                end if;
                if p_support_assessment_ids is null
                   or cardinality(p_support_assessment_ids) = 0 then
                    raise exception 'resolve_delta:missing_support a semantic decision names the effective Support Assessments it relied on'
                        using errcode='23514';
                end if;
            end if;

            -- Every named Support Assessment is this project's and still
            -- effective.  Locator validity is never read here: a passed
            -- Source Passage Check is not support (ADR-0082).
            foreach support_id in array coalesce(p_support_assessment_ids, '{{}}'::bigint[])
            loop
                if not exists (
                    select 1 from support_assessments
                     where id = support_id and project_id = p_project_id
                       and superseded_by is null
                ) then
                    raise exception 'resolve_delta:missing_support Support Assessment % is not an effective assessment of this project', support_id
                        using errcode='23514';
                end if;
            end loop;

            -- Validate every record effect before writing any of them.
            for effect in
                select value from jsonb_array_elements(
                    coalesce(p_record_effects, '[]'::jsonb)
                )
            loop
                effect_disposition := coalesce(effect->>'disposition', 'include');
                if effect_disposition not in ('include', 'do_not_add') then
                    raise exception 'resolve_delta:invalid_action % is not a record effect disposition', effect_disposition
                        using errcode='23514';
                end if;
                select * into decided from facts
                 where id = (effect->>'fact_id')::bigint
                   and project_id = p_project_id;
                if not found then
                    raise exception 'resolve_delta:cross_project_fact a Resolve Delta decides only Source Facts this project captured'
                        using errcode='23514';
                end if;
                if decided.subject_key <> delta.target_subject_identity then
                    raise exception 'resolve_delta:subject_mismatch a Resolve Delta decides the delta''s exact subject'
                        using errcode='23514';
                end if;
                if delta.target_type = 'existing_subject'
                   and effect_disposition = 'include'
                   and decided.fact_type is distinct from delta.target_field then
                    raise exception 'resolve_delta:field_mismatch a Resolve Delta decides the delta''s exact field'
                        using errcode='23514';
                end if;
                if effect_disposition = 'include' and exists (
                    select 1 from support_assessments
                     where project_id = p_project_id
                       and proposition_kind = 'source_fact'
                       and fact_id = decided.id
                       and superseded_by is null
                       and evidence_role = 'value_support'
                       and assessment in ('supported', 'partially_supported')
                       and id = any(coalesce(p_support_assessment_ids, '{{}}'::bigint[]))
                ) is not true then
                    raise exception 'resolve_delta:missing_support Source Fact % has no effective value support this decision names', decided.id
                        using errcode='23514';
                end if;
                if exists (
                    select 1 from fact_decisions
                     where fact_id = decided.id and superseded_by is null
                ) and effect_disposition = 'include' then
                    raise exception 'resolve_delta:already_effective Source Fact % is already the effective accepted value', decided.id
                        using errcode='23514';
                end if;

                -- The accepted revision the coordinator observed must still
                -- be the one this exact subject and field stands on.
                select count(*), max(id), max(revision_id)
                  into effective_count, live_predecessor, live_revision
                  from fact_decisions
                 where project_id = p_project_id
                   and subject_key = decided.subject_key
                   and fact_type = decided.fact_type
                   and superseded_by is null;
                if effective_count > 1 then
                    raise exception 'resolve_delta:ambiguous_effective_decision % of % holds more than one effective decision', decided.fact_type, decided.subject_key
                        using errcode='23514';
                end if;
                if effective_count = 1
                   and live_revision > coalesce(p_observed_accepted_revision_id, 0) then
                    raise exception 'resolve_delta:stale_accepted_revision the accepted record moved to revision % after revision % was read', live_revision, coalesce(p_observed_accepted_revision_id, 0)
                        using errcode='23514';
                end if;
            end loop;

            -- Nothing above wrote anything.  From here the act is atomic.
            if p_revision_id is not null then
                -- #526 owns the transaction and the one revision it writes;
                -- this child contributes its decision and opens nothing.
                revision := p_revision_id;
            else
                revision := public.open_delta_resolution_revision(
                    p_project_id, p_principal, p_idempotency_key
                );
                opened := true;
            end if;

            insert into delta_dispositions (
                project_id, delta_id, disposition, decided_at,
                decided_by_principal, decided_by_policy, rationale,
                effective_value
            ) values (
                p_project_id, p_delta_id, p_disposition, p_decided_at,
                p_principal, null, p_rationale, p_effective_value
            ) returning id into disposition_id;

            insert into delta_record_decisions (
                project_id, delta_id, disposition_id, revision_id, disposition,
                effect_kind, organization_change_kind, decided_by_principal,
                observed_accepted_revision_id, edit_basis, idempotency_key,
                decided_at
            ) values (
                p_project_id, p_delta_id, disposition_id, revision,
                p_disposition, p_effect_kind, p_organization_change_kind,
                p_principal, p_observed_accepted_revision_id, p_edit_basis,
                p_idempotency_key, p_decided_at
            ) returning id into decision_id;

            foreach support_id in array coalesce(p_support_assessment_ids, '{{}}'::bigint[])
            loop
                slot := slot + 1;
                insert into delta_decision_supports (
                    project_id, decision_id, support_assessment_id, ordinal
                ) values (p_project_id, decision_id, support_id, slot);
            end loop;

            for effect in
                select value from jsonb_array_elements(
                    coalesce(p_record_effects, '[]'::jsonb)
                )
            loop
                effect_disposition := coalesce(effect->>'disposition', 'include');
                select * into decided from facts
                 where id = (effect->>'fact_id')::bigint;
                select max(id) into live_predecessor from fact_decisions
                 where project_id = p_project_id
                   and subject_key = decided.subject_key
                   and fact_type = decided.fact_type
                   and superseded_by is null;
                new_decision := nextval('fact_decisions_id_seq');
                if live_predecessor is not null then
                    -- The predecessor is retired, never deleted: its row, its
                    -- revision, and its fact all remain readable.
                    update fact_decisions set superseded_by = new_decision
                     where id = live_predecessor;
                end if;
                insert into fact_decisions (
                    id, project_id, fact_id, subject_key, fact_type,
                    revision_id, disposition, superseded_by
                ) values (
                    new_decision, p_project_id, decided.id, decided.subject_key,
                    decided.fact_type, revision, effect_disposition, null
                );
                written := written || new_decision;
            end loop;

            return jsonb_build_object(
                'revision_id', revision,
                'revision_opened', opened,
                'disposition_id', disposition_id,
                'decision_id', decision_id,
                'fact_decision_ids', to_jsonb(written),
                'created', true
            );
        end; $$;
"""

RESOLVE_PROPOSED_DELTA_DECISION_SIGNATURE = (
    "(bigint, bigint, character varying, character varying, character varying, "
    "character varying, character varying, bigint, timestamp with time zone, "
    "jsonb, text, jsonb, jsonb, bigint[], bigint)"
)

DEFER_PROPOSED_DELTA = """
create function public.defer_proposed_delta(
    p_project_id bigint,
    p_delta_id bigint,
    p_principal character varying,
    p_deferred_at timestamp with time zone,
    p_deferred_until timestamp with time zone,
    p_wake_condition character varying,
    p_reason text
) returns bigint
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            deferral_id bigint;
        begin
            if p_principal is null or length(btrim(p_principal)) = 0 then
                raise exception 'resolve_delta:missing_principal a deferral names the person scheduling it'
                    using errcode='23514';
            end if;
            if p_deferred_at is null then
                raise exception 'resolve_delta:missing_decided_at a deferral records when it was scheduled'
                    using errcode='23514';
            end if;
            if p_deferred_until is null and p_wake_condition is null then
                raise exception 'resolve_delta:missing_wake_condition a deferral carries a return date or a wake condition'
                    using errcode='23514';
            end if;
            if not exists (
                select 1 from proposed_deltas
                 where id = p_delta_id and project_id = p_project_id
            ) then
                raise exception 'resolve_delta:cross_project_delta Proposed Delta % is not this project''s to defer', p_delta_id
                    using errcode='23514';
            end if;
            if exists (
                select 1 from delta_dispositions where delta_id = p_delta_id
            ) then
                raise exception 'resolve_delta:already_resolved Proposed Delta % is resolved and no longer schedulable', p_delta_id
                    using errcode='23514';
            end if;
            if exists (
                select 1 from delta_supersessions where prior_delta_id = p_delta_id
            ) then
                raise exception 'resolve_delta:superseded_delta Proposed Delta % was superseded by a newer source version', p_delta_id
                    using errcode='23514';
            end if;
            insert into delta_deferrals (
                project_id, delta_id, deferred_at, deferred_until,
                wake_condition, scheduled_by_principal, reason
            ) values (
                p_project_id, p_delta_id, p_deferred_at, p_deferred_until,
                p_wake_condition, p_principal, p_reason
            ) returning id into deferral_id;
            return deferral_id;
        end; $$;
"""

DEFER_PROPOSED_DELTA_SIGNATURE = (
    "(bigint, bigint, character varying, timestamp with time zone, "
    "timestamp with time zone, character varying, text)"
)

RESOLVE_DELTA_COMMANDS = {
    "open_delta_resolution_revision": OPEN_DELTA_RESOLUTION_REVISION_SIGNATURE,
    "resolve_proposed_delta_decision": RESOLVE_PROPOSED_DELTA_DECISION_SIGNATURE,
    "defer_proposed_delta": DEFER_PROPOSED_DELTA_SIGNATURE,
}

# The tables the Resolve Delta commands write that the source-append role
# created above.  The decision role needs the same append rights on them, and
# the disposition is no longer something a source append may write.
RESOLVE_DELTA_ADOPTED_TABLES = ("delta_dispositions", "delta_deferrals")


def upgrade(op) -> None:
    op.execute(RESOLVE_DELTA_SCHEMA)
    for table in RESOLVE_DELTA_TABLES:
        # The application reads one delta's decision and its cited support and
        # writes neither; only the command the decision role owns writes them.
        op.execute(f"grant select on public.{table} to {RUNTIME_LOGINS}")
        op.execute(
            f"revoke insert, update, delete, truncate on public.{table} "
            f"from {RUNTIME_LOGINS}"
        )
        op.execute(
            f"grant select, insert on public.{table} to {RESOLVE_DELTA_ROLE}"
        )
        op.execute(
            f"grant usage, select on sequence public.{table}_id_seq "
            f"to {RESOLVE_DELTA_ROLE}"
        )
    # Resolving a delta is a record decision, not a source append: the
    # disposition and the Work List scheduling receipt move to the role that
    # owns accepted authority, and the append role loses them.
    for table in RESOLVE_DELTA_ADOPTED_TABLES:
        op.execute(
            f"grant select, insert on public.{table} to {RESOLVE_DELTA_ROLE}"
        )
        op.execute(
            f"grant usage, select on sequence public.{table}_id_seq "
            f"to {RESOLVE_DELTA_ROLE}"
        )
        op.execute(f"revoke insert on public.{table} from {SOURCE_APPEND_ROLE}")
    # What the commands read to prove scope, lifecycle, and support.
    for table in (
        "proposed_deltas",
        "delta_groups",
        "delta_dispositions",
        "delta_supersessions",
        "delta_deferrals",
        "support_assessments",
    ):
        op.execute(f"grant select on public.{table} to {RESOLVE_DELTA_ROLE}")
    for body in (
        OPEN_DELTA_RESOLUTION_REVISION,
        RESOLVE_PROPOSED_DELTA_DECISION,
        DEFER_PROPOSED_DELTA,
    ):
        op.execute(body)
    for name, signature in RESOLVE_DELTA_COMMANDS.items():
        op.execute(
            f"alter function public.{name}{signature} owner to {RESOLVE_DELTA_ROLE}"
        )
        op.execute(f"revoke all on function public.{name}{signature} from public")
        # Accept, edit, reject, and defer are attributable human acts, so they
        # join the other decision commands on the web capability alone.
        op.execute(
            f"grant execute on function public.{name}{signature} to corridor_web"
        )


def downgrade(op) -> None:
    for name, signature in RESOLVE_DELTA_COMMANDS.items():
        op.execute(f"drop function if exists public.{name}{signature}")
    for table in RESOLVE_DELTA_ADOPTED_TABLES:
        op.execute(
            f"do $$ begin "
            f"if exists (select 1 from pg_tables where schemaname = 'public' "
            f"and tablename = '{table}') then "
            f"revoke select, insert on public.{table} from {RESOLVE_DELTA_ROLE}; "
            f"grant insert on public.{table} to {SOURCE_APPEND_ROLE}; "
            f"end if; end $$;"
        )
    for table in RESOLVE_DELTA_TABLES:
        op.execute(
            f"do $$ begin "
            f"if exists (select 1 from pg_tables where schemaname = 'public' "
            f"and tablename = '{table}') then "
            f"revoke select on public.{table} from {RUNTIME_LOGINS}; "
            f"end if; end $$;"
        )
    op.execute(RESOLVE_DELTA_SCHEMA_DOWN)
