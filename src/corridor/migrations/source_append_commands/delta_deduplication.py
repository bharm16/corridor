"""Permanent-state de-duplication (#457, #859, ADR-0018, ADR-0081, ADR-0083).

Every family above already knows what makes a row the same row.  What most
of them do not have is that knowledge in *permanent state*: the identity is
derived where the row is written — inside a ``SECURITY DEFINER`` command's
body, or in the Python that calls it — and the table itself would accept a
second copy.  A rule that lives in a writer holds only for as long as every
writer remembers it, and ADR-0081's convergence is a claim about the record,
not about today's call graph.  Each identity below therefore becomes a
constraint, so a duplicate stops being unlikely and becomes unrepresentable.

  * **Source Facts.**  ``append_fact`` refuses a Fact with no digest and
    returns the existing row when one already carries it, but
    ``facts.content_sha256`` was nullable behind a *partial* unique index.
    A Fact with no identity was representable, and two of them were
    indistinguishable.  The column becomes ``not null`` and the index
    becomes a total unique constraint: every Fact has an identity, and it is
    its own.

  * **Proposed Deltas.**  ``proposed_deltas.content_sha256`` was already
    unique, and the project is inside the digest, so the occurrence was
    safe.  Its *group* was not: ``append_proposed_deltas`` inserted a fresh
    ``delta_groups`` row on every call, so replaying one source version —
    which ``delta_generation`` does on every budgeted batch and after every
    crash — left another group behind even when it appended no delta at all.
    ADR-0075 makes a group one atomic source change, so its identity is the
    source version it came from, and the command now converges on the group
    that version already opened.

  * **Decisions.**  ``delta_record_decisions``, the Review Packet receipt,
    the Follow-up Plan, and the Undo already carry ``(project_id,
    idempotency_key)``; ``fact_decisions`` did not, because its identity is
    its revision's.  Nothing said a revision decides a Fact once, though, so
    ``include_structured_cell_fact_decision``'s replay read
    (``where revision_id = …``) was reading a set it assumed was a row.  The
    dated Work List deferral of ADR-0084 had no identity at all: it is
    scheduling rather than a record decision, so it carries no Project Record
    idempotency key, and a retried Defer wrote a second receipt for the same
    act.  #457 made its identity the delta, the instant it was scheduled at,
    and the person who scheduled it, which was wrong in both directions
    (#903): inside one instant a caller asking for a *different* return date
    was answered with the old receipt as a success, and a deliberate second
    reschedule inside that same instant could not be recorded at all.  A
    timestamp says when an act happened; it is not how two intentional acts
    are told apart.  So the caller names its own request instead, the delta
    and that name are the identity, and the command compares the payload the
    replay carries against the receipt it would converge on: equal is the same
    act and returns it, different is a contradiction and is refused.  A
    genuine reschedule is a *fresh* request that names the receipt it expects
    to replace, so a submission composed against a schedule someone else has
    already moved is refused rather than silently replacing theirs.

  * **Project Record revisions.**  ``(project_id, idempotency_key)`` was
    already unique and correctly scoped.  What was missing is that the key
    had to *be* one: every command refuses a blank key in its own body, and
    the column accepted ``''``, which is the same non-identity for all of
    them.

  * **Connector deliveries.**  ``push_deliveries.idempotency_key`` was
    unique, but it was computed in Python and the database never checked it
    against the row it was stored on, so a writer that derived it wrongly —
    or not at all — defeated the dedup while satisfying the constraint.  The
    envelope's structural key ``(project_id, delivery_identity,
    content_sha256)`` becomes unique in its own right, and a trigger
    re-derives ADR-0083's two digests from the row's own columns and the
    bound project's slug and refuses a row whose identity is not its own.

  * **Revision Comparisons.**  ``create_revision_comparison`` already reads
    the receipt an execution retained -- the two exact Extraction Runs, the
    matcher version, and the canonical matcher configuration -- and returns
    that one rather than running again.  Nothing in the record stopped a
    second writer from appending another receipt for the same execution,
    though: the project row lock held the window shut, so an invariant
    ADR-0018 states about an immutable run lived inside one procedure.  That
    execution identity becomes unique.  The receipt's own ``content_sha256``
    is deliberately not part of the key, because two receipts that disagree
    about identical inputs must collide and be refused rather than be
    retained as if they were two different executions (#859).

What was considered and rejected: repairing existing duplicates.  A
constraint added over data that violates it must either fail or change the
record, and merging two Source Facts or two decisions is a semantic act no
migration has the authority to perform (ADR-0080 as amended by ADR-0083
governs disposition; nothing here disposes of anything).  The transition
therefore counts what it cannot represent and refuses, naming the family, in
the same shape #512's backfill uses.
"""

from __future__ import annotations

import sqlalchemy as sa

from corridor.migrations.source_append_commands.resolve_delta import (
    DEFER_PROPOSED_DELTA_SIGNATURE,
)
from corridor.migrations.source_append_commands.roles import (
    COMMANDS,
    RECORD_DECISION_ROLE,
    RUNTIME_LOGINS,
    SOURCE_APPEND_ROLE,
)


DEDUPLICATION_REFUSALS = (
    (
        "Source Facts with no identity digest",
        "select count(*) from facts where content_sha256 is null",
    ),
    (
        "Source Facts sharing one identity digest",
        "select coalesce(sum(extra), 0) from ("
        "  select count(*) - 1 as extra from facts"
        "   where content_sha256 is not null"
        "   group by content_sha256 having count(*) > 1) duplicated",
    ),
    (
        "Proposed Delta groups sharing one source version",
        "select coalesce(sum(extra), 0) from ("
        "  select count(*) - 1 as extra from delta_groups"
        "   group by project_id, source_family, source_revision,"
        "            document_id, statement_id"
        "  having count(*) > 1) duplicated",
    ),
    (
        "Record Inclusion decisions repeating one Fact in one revision",
        "select coalesce(sum(extra), 0) from ("
        "  select count(*) - 1 as extra from fact_decisions"
        "   group by revision_id, fact_id having count(*) > 1) duplicated",
    ),
    (
        "Work List deferrals repeating one scheduling request",
        "select coalesce(sum(extra), 0) from ("
        "  select count(*) - 1 as extra from delta_deferrals"
        "   group by delta_id, request_identity"
        "  having count(*) > 1) duplicated",
    ),
    (
        "Project Record revisions carrying a blank idempotency key",
        "select count(*) from project_record_revisions"
        " where length(btrim(idempotency_key)) = 0",
    ),
    (
        "connector deliveries sharing one envelope identity",
        "select coalesce(sum(extra), 0) from ("
        "  select count(*) - 1 as extra from push_deliveries"
        "   group by project_id, delivery_identity, content_sha256"
        "  having count(*) > 1) duplicated",
    ),
    (
        "Revision Comparison receipts sharing one execution identity",
        "select coalesce(sum(extra), 0) from ("
        "  select count(*) - 1 as extra from revision_comparison_runs"
        "   group by predecessor_extraction_run_id,"
        "            successor_extraction_run_id, matcher_version,"
        "            matcher_config"
        "  having count(*) > 1) duplicated",
    ),
    (
        "connector deliveries whose stored identity is not their own",
        "select count(*) from push_deliveries delivery"
        "  join projects project on project.id = delivery.project_id"
        " where delivery.delivery_identity is distinct from"
        "       encode(sha256(convert_to(concat_ws(':', delivery.customer,"
        "           project.slug, delivery.channel, delivery.external_identity,"
        "           delivery.external_version), 'UTF8')), 'hex')",
    ),
)

DEDUPLICATED_IDENTITIES = """
alter table public.facts alter column content_sha256 set not null;
drop index if exists public.uq_facts_content_sha256;
alter table public.facts
    add constraint uq_facts_content_sha256 unique (content_sha256);

alter table public.delta_groups
    add constraint uq_delta_groups_source_change
    unique nulls not distinct
        (project_id, source_family, source_revision, document_id, statement_id);

alter table public.fact_decisions
    add constraint uq_fact_decisions_revision_fact unique (revision_id, fact_id);

alter table public.delta_deferrals
    add constraint uq_delta_deferrals_request
    unique (delta_id, request_identity);

alter table public.delta_deferrals
    add constraint ck_delta_deferrals_request_identity
    check (length(btrim(request_identity)) > 0);

alter table public.project_record_revisions
    add constraint ck_project_record_revisions_idempotency_key
    check (length(btrim(idempotency_key)) > 0);

alter table public.push_deliveries
    add constraint uq_push_deliveries_envelope
    unique (project_id, delivery_identity, content_sha256);

create function public.enforce_push_delivery_identity() returns trigger
    language plpgsql
    as $$
        declare
            bound_slug text;
            derived_identity text;
            derived_key text;
        begin
            select slug into bound_slug from projects where id = new.project_id;
            if bound_slug is null then
                raise exception 'push_intake:unbound_delivery a delivery names a project that does not exist'
                    using errcode='23514';
            end if;
            derived_identity := encode(sha256(convert_to(concat_ws(':',
                new.customer, bound_slug, new.channel,
                new.external_identity, new.external_version), 'UTF8')), 'hex');
            derived_key := encode(sha256(convert_to(concat_ws(':',
                derived_identity, new.content_sha256), 'UTF8')), 'hex');
            if new.delivery_identity is distinct from derived_identity then
                raise exception 'push_intake:delivery_identity a delivery identity is derived from the binding and the transport, never supplied'
                    using errcode='23514';
            end if;
            if new.idempotency_key is distinct from derived_key then
                raise exception 'push_intake:idempotency_key a delivery idempotency key is derived from its identity and its bytes, never supplied'
                    using errcode='23514';
            end if;
            return new;
        end; $$;

create trigger trg_push_deliveries_identity
    before insert on public.push_deliveries
    for each row execute function public.enforce_push_delivery_identity();

alter table public.revision_comparison_runs
    add constraint uq_revision_comparison_execution_identity
    unique (predecessor_extraction_run_id, successor_extraction_run_id,
            matcher_version, matcher_config);
"""

DEDUPLICATED_IDENTITIES_DOWN = """
alter table public.revision_comparison_runs
    drop constraint if exists uq_revision_comparison_execution_identity;
drop trigger if exists trg_push_deliveries_identity on public.push_deliveries;
drop function if exists public.enforce_push_delivery_identity() cascade;
alter table public.push_deliveries
    drop constraint if exists uq_push_deliveries_envelope;
alter table public.project_record_revisions
    drop constraint if exists ck_project_record_revisions_idempotency_key;
alter table public.delta_deferrals
    drop constraint if exists ck_delta_deferrals_request_identity;
alter table public.delta_deferrals
    drop constraint if exists uq_delta_deferrals_request;
alter table public.fact_decisions
    drop constraint if exists uq_fact_decisions_revision_fact;
alter table public.delta_groups
    drop constraint if exists uq_delta_groups_source_change;
alter table public.facts drop constraint if exists uq_facts_content_sha256;
create unique index uq_facts_content_sha256
    on public.facts using btree (content_sha256)
 where (content_sha256 is not null);
alter table public.facts alter column content_sha256 drop not null;
"""

# The two commands whose replay had to converge rather than append.  Neither
# signature changes, so the callers, the accepted-authority allowlist, and the
# grants above are untouched; only the body learns the identity the constraint
# now holds.
APPEND_PROPOSED_DELTAS_DEDUPLICATED = """
create function public.append_proposed_deltas(
    p_project_id bigint,
    p_source_family character varying,
    p_source_revision character varying,
    p_document_id bigint,
    p_statement_id bigint,
    p_deltas jsonb
) returns bigint[]
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            v_group_id bigint;
            item jsonb;
            ch_type text;
            tgt_type text;
            tgt_id text;
            tgt_field text;
            acc_val jsonb;
            prop_val jsonb;
            comp_rule text;
            acc_base text;
            content_hash text;
            delta_id bigint;
            appended bigint[] := '{}';
        begin
            if not exists (select 1 from projects where id = p_project_id) then
                raise exception 'project does not exist' using errcode = '23514';
            end if;
            if p_document_id is not null and not exists (
                select 1 from documents where id = p_document_id and project_id = p_project_id
            ) then
                raise exception 'document is outside its project' using errcode = '23514';
            end if;
            if p_statement_id is not null and not exists (
                select 1 from dependency_events where id = p_statement_id and project_id = p_project_id
            ) then
                raise exception 'statement is outside its project' using errcode = '23514';
            end if;
            if p_deltas is null or jsonb_typeof(p_deltas) <> 'array' or jsonb_array_length(p_deltas) = 0 then
                raise exception 'deltas must be a non-empty list' using errcode = '23514';
            end if;

            -- One atomic source change per source version (#457).  A replay,
            -- and every later batch of the same version, joins the group that
            -- version already opened instead of leaving another behind.
            insert into delta_groups (
                project_id, source_family, source_revision, document_id, statement_id
            ) values (
                p_project_id, p_source_family, p_source_revision, p_document_id, p_statement_id
            )
            on conflict on constraint uq_delta_groups_source_change do nothing
            returning id into v_group_id;
            if v_group_id is null then
                select id into v_group_id from delta_groups
                 where project_id = p_project_id
                   and source_family = p_source_family
                   and source_revision = p_source_revision
                   and document_id is not distinct from p_document_id
                   and statement_id is not distinct from p_statement_id;
            end if;

            for item in select value from jsonb_array_elements(p_deltas) loop
                ch_type := item ->> 'change_type';
                tgt_type := item ->> 'target_type';
                tgt_id := item ->> 'target_subject_identity';
                tgt_field := item ->> 'target_field';
                acc_val := item -> 'accepted_value';
                prop_val := item -> 'proposed_value';
                comp_rule := coalesce(item ->> 'comparison_rule_version', 'v1');
                acc_base := item ->> 'accepted_baseline_revision';

                if ch_type not in ('add', 'modify', 'apparent_removal') then
                    raise exception 'invalid change_type: %', ch_type using errcode = '23514';
                end if;
                if tgt_type not in ('existing_subject', 'proposed_subject') then
                    raise exception 'invalid target_type: %', tgt_type using errcode = '23514';
                end if;
                if tgt_id is null or length(trim(tgt_id)) = 0 then
                    raise exception 'target_subject_identity is required' using errcode = '23514';
                end if;

                content_hash := encode(sha256(convert_to(
                    concat_ws(':', p_project_id, ch_type, tgt_type, tgt_id, coalesce(tgt_field, ''),
                              coalesce(acc_val::text, ''), coalesce(prop_val::text, ''),
                              p_source_family, p_source_revision, comp_rule),
                    'UTF8'
                )), 'hex');

                -- The digest is the delta's identity, so a replay and a
                -- competing writer converge on the row it already names
                -- rather than racing between a read and an insert (#457).
                insert into proposed_deltas (
                    project_id, group_id, content_sha256, change_type,
                    target_type, target_subject_identity, target_field,
                    accepted_value, proposed_value, source_family, source_revision,
                    comparison_rule_version, accepted_baseline_revision
                ) values (
                    p_project_id, v_group_id, content_hash, ch_type,
                    tgt_type, tgt_id, tgt_field,
                    acc_val, prop_val, p_source_family, p_source_revision,
                    comp_rule, acc_base
                )
                on conflict on constraint uq_proposed_deltas_content do nothing
                returning id into delta_id;
                if delta_id is null then
                    select id into delta_id from proposed_deltas
                     where content_sha256 = content_hash;
                end if;
                appended := array_append(appended, delta_id);
            end loop;

            return appended;
        end; $$;
"""

DEFER_PROPOSED_DELTA_DEDUPLICATED = """
create function public.defer_proposed_delta(
    p_project_id bigint,
    p_delta_id bigint,
    p_principal character varying,
    p_deferred_at timestamp with time zone,
    p_deferred_until timestamp with time zone,
    p_wake_condition character varying,
    p_reason text,
    p_request_identity character varying,
    p_supersedes bigint
) returns bigint
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            deferral_id bigint;
            asked delta_deferrals%rowtype;
            in_force bigint;
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
            if p_request_identity is null or length(btrim(p_request_identity)) = 0 then
                raise exception 'resolve_delta:missing_request_identity a deferral names the request that asked for it'
                    using errcode='23514';
            end if;
            if not exists (
                select 1 from proposed_deltas
                 where id = p_delta_id and project_id = p_project_id
            ) then
                raise exception 'resolve_delta:cross_project_delta Proposed Delta % is not this project''s to defer', p_delta_id
                    using errcode='23514';
            end if;
            -- One delta carries at most one terminal relationship, and a
            -- concurrent competing act serialises here rather than each
            -- reading an empty table (ADR-0101).  It is taken before the
            -- replay read below as well, so two submissions racing on the
            -- same schedule cannot both find it unreplaced (#903).
            perform public.lock_proposed_delta_terminal(p_delta_id);
            -- Scheduling writes no revision (ADR-0084), so the act carries no
            -- Project Record idempotency key; the delta and the identity the
            -- caller gave its request are what make two calls one act (#903).
            -- The instant is deliberately not part of that: it records when
            -- the act happened, and a retry that arrived a microsecond later
            -- would otherwise become a new act simply by being later.
            select * into asked from delta_deferrals
             where delta_id = p_delta_id
               and request_identity = p_request_identity;
            if found then
                -- The same request asking for something else is not a replay
                -- of this receipt, and answering with it would report success
                -- for a schedule the record does not hold (#903).  What the
                -- caller wants is a fresh request naming this receipt as the
                -- one it expects to replace.
                if asked.scheduled_by_principal is distinct from p_principal
                   or asked.deferred_until is distinct from p_deferred_until
                   or asked.wake_condition is distinct from p_wake_condition
                   or asked.reason is distinct from p_reason
                   or asked.supersedes_deferral_id is distinct from p_supersedes then
                    raise exception 'resolve_delta:schedule_bound_to_other_content this request was already recorded asking for something else, so nothing was recorded a second time; open the week again and make the change you want'
                        using errcode='23514';
                end if;
                return asked.id;
            end if;
            -- An effective disposition, not merely a row: a decision the
            -- coordinator undid is retained history and settles nothing,
            -- so the question it answered is answerable again (#948).
            if public.proposed_delta_effective_disposition(p_delta_id)
               is not null then
                raise exception 'resolve_delta:already_resolved Proposed Delta % is resolved and no longer schedulable', p_delta_id
                    using errcode='23514';
            end if;
            if exists (
                select 1 from delta_supersessions where prior_delta_id = p_delta_id
            ) then
                raise exception 'resolve_delta:superseded_delta Proposed Delta % was superseded by a newer source version', p_delta_id
                    using errcode='23514';
            end if;
            -- A scheduled return to a comparison that no longer exists is a
            -- return to nothing (ADR-0101).
            if public.proposed_delta_capture_correction(p_delta_id) is not null then
                raise exception 'resolve_delta:capture_corrected_delta Proposed Delta % left Review because its source reading was corrected, so there is nothing to schedule a return to', p_delta_id
                    using errcode='23514';
            end if;
            -- A new request that means to *replace* a schedule names the
            -- receipt it believed was in force, and it has to still be in
            -- force (#903).  That is the whole difference between a deliberate
            -- second reschedule and a submission composed against a reading
            -- someone else has already moved: both carry a fresh identity, and
            -- only the one that names what actually stands is carried out.
            -- A null predecessor claims nothing, which is what a first Defer
            -- of an unscheduled change is; the readable half is the one that
            -- knows whether a schedule stands, and refuses the reschedule of a
            -- change that is not deferred before this command is reached.
            if p_supersedes is not null then
                select id into in_force from delta_deferrals
                 where delta_id = p_delta_id
                   and id not in (
                        select child.deferral_id
                          from delta_review_packet_children child
                          join delta_review_packet_reversals reversal
                            on reversal.receipt_id = child.receipt_id
                         where child.deferral_id is not null)
                 order by id desc
                 limit 1;
                if in_force is distinct from p_supersedes then
                    raise exception 'resolve_delta:stale_schedule the schedule this request expected to replace is not the one in force for Proposed Delta %, so nothing was recorded; open the week again to see the return date that stands', p_delta_id
                        using errcode='23514';
                end if;
            end if;
            insert into delta_deferrals (
                project_id, delta_id, request_identity, supersedes_deferral_id,
                deferred_at, deferred_until,
                wake_condition, scheduled_by_principal, reason
            ) values (
                p_project_id, p_delta_id, p_request_identity, p_supersedes,
                p_deferred_at, p_deferred_until,
                p_wake_condition, p_principal, p_reason
            ) returning id into deferral_id;
            return deferral_id;
        end; $$;
"""


def _refuse_representable_duplicates(bind) -> None:
    """Refuse the transition rather than change a record it cannot merge.

    Every identity this block makes permanent is one the writers already
    believed in, so an existing violation means a writer was wrong, and which
    of two rows is the record is a question only a person can answer.
    """

    found = [
        (family, count)
        for family, statement in DEDUPLICATION_REFUSALS
        if (count := bind.execute(sa.text(statement)).scalar_one())
    ]
    if found:
        detail = "; ".join(f"{count} {family}" for family, count in found)
        raise RuntimeError(
            "#457 de-duplication refuses: permanent-state identity cannot be "
            f"established over existing rows — {detail}. Nothing is merged or "
            "dropped here: resolve the duplicates as an attributable record "
            "act first."
        )


def upgrade(op) -> None:
    # Last, because it constrains what every block above created: the identity
    # each family already derived becomes a property of the record itself.
    _refuse_representable_duplicates(op.get_bind())
    op.execute(DEDUPLICATED_IDENTITIES)
    # Both commands keep their signatures, so only the body is replaced; the
    # owner and grants are re-applied because a dropped function takes them.
    op.execute(
        f"drop function public.append_proposed_deltas"
        f"{COMMANDS['append_proposed_deltas']}"
    )
    op.execute(APPEND_PROPOSED_DELTAS_DEDUPLICATED)
    op.execute(
        f"alter function public.append_proposed_deltas"
        f"{COMMANDS['append_proposed_deltas']} owner to {SOURCE_APPEND_ROLE}"
    )
    op.execute(
        f"revoke all on function public.append_proposed_deltas"
        f"{COMMANDS['append_proposed_deltas']} from public"
    )
    op.execute(
        f"grant execute on function public.append_proposed_deltas"
        f"{COMMANDS['append_proposed_deltas']} to {RUNTIME_LOGINS}"
    )
    op.execute(
        f"drop function public.defer_proposed_delta{DEFER_PROPOSED_DELTA_SIGNATURE}"
    )
    op.execute(DEFER_PROPOSED_DELTA_DEDUPLICATED)
    op.execute(
        f"alter function public.defer_proposed_delta"
        f"{DEFER_PROPOSED_DELTA_SIGNATURE} owner to {RECORD_DECISION_ROLE}"
    )
    op.execute(
        f"revoke all on function public.defer_proposed_delta"
        f"{DEFER_PROPOSED_DELTA_SIGNATURE} from public"
    )
    op.execute(
        f"grant execute on function public.defer_proposed_delta"
        f"{DEFER_PROPOSED_DELTA_SIGNATURE} to corridor_web"
    )


def downgrade(op) -> None:
    # First among what remains, because the upgrade added it last.  Only the
    # constraints and the delivery-identity guard are undone here: the two
    # commands whose bodies this block replaced keep their signatures, and the
    # loops below drop them by that signature exactly as they did before #457.
    op.execute(DEDUPLICATED_IDENTITIES_DOWN)
