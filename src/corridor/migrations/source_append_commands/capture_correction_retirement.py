"""What a corrected capture established, and the proposal it retires (#836, #842).

Folded into this transition for the same window reason as the blocks around it:
``corridor.migrations.policy`` allows one unreleased transition and this is it.

ADR-0101 decides the lifecycle half ADR-0100 left open. #836 recorded the
report and refused the no-change exit by name (``NO_CHANGE_EXIT_UNAVAILABLE``),
because neither exit the schema offered was true: a ``reject`` disposition
files a coordinator decision nobody made, and ``ck_delta_supersessions_successor``
admits only a newer source version's successor artifact, which a re-read of the
*same* version does not produce. This block is the relationship that closes
that gap, plus the receipt that proves the conclusion it rests on.

**Two relations, because a corrected Source Fact alone proves nothing.**
ADR-0101 is explicit that the relationship "must not reduce to
``old_delta_id -> corrected_fact_id``": whether a corrected capture equals the
accepted value depends on *which* accepted revision was read and *which*
comparison rule was applied, and a pair of foreign keys records neither. So the
proof is a row of its own.

- ``capture_correction_results`` is the investigation's outcome: the request
  that caused it, the exact challenged capture by immutable identity and
  digest, the corrected capture beside it with the Support Assessment that
  holds it to the retained source, the accepted revision the recomparison read,
  the comparison rule and version it ran under, the conclusion, the replacement
  proposal where there is one, the responsible operations actor and the service
  identity that executed the work, and an idempotency identity. Every one of
  ADR-0101's nine proof items is here or reachable from here without
  recomputation.
- ``delta_capture_corrections`` is the relationship itself -- ADR-0101's
  ``DeltaCaptureCorrection`` -- and it says one thing: *this particular Proposed
  Delta no longer represents an actionable comparison because the particular
  capture on which it depended was corrected.* It is a prior delta and the
  result that established it, and nothing else, because nothing else is part of
  that assertion.

**Why the outcome is not a column on the relationship.** An investigation that
cannot be substantiated has a result and retires nothing; ADR-0101 requires
that outcome recorded honestly rather than dressed as a successful correction.
If the outcome lived on the relationship, an inconclusive result would need a
relationship row that does not relate, and every reader asking "is this delta
retired?" would have to filter on a word instead of on a row's existence. Two
relations keep the reader's question the same shape as the three terminal
questions it already asks: a row exists, or it does not.

**One retirement per delta, by a unique constraint.** ``uq_delta_capture_corrections_delta``
is what makes an exact retry idempotent rather than a second retirement, and it
is also the serialisation point between two competing retirements: the second
inserter waits on the index, then finds the row and replays it.

**The terminal-state invariant is enforced at every write, in both
directions.** ADR-0101 says precedence between terminal reasons is a reader's
presentation rule and "should not be the mechanism that makes contradictory
writes appear harmless". So this block adds two helpers the *other* commands
call, rather than restating the rule in each of them:

- ``lock_proposed_delta_terminal`` takes a transaction-scoped advisory lock on
  one delta. Every command that writes a terminal relationship takes it before
  it checks for one, so a concurrent competing pair serialises: one wins and
  the other sees the winner's row and raises its own bounded refusal instead of
  committing a contradiction. Without it both acts read an empty table, both
  insert, and the delta ends up carrying two terminal relationships that only
  the readers' precedence order hides.
- ``proposed_delta_capture_correction`` answers "is this delta retired by a
  capture correction, and by which result?" in one place, so
  ``resolve_proposed_delta_decision``, ``defer_proposed_delta`` and
  ``record_delta_follow_up_plan`` each gain one call rather than a copy of a
  predicate that can drift. The bulk supersession sweeps in ``email_spine`` and
  ``minutes_spine`` skip a retired delta the same way they already skip a
  disposed one.

Both helpers are ``security invoker`` and read-only, so a caller's own command
keeps its own authority; they are forward-referenced by commands created
earlier in this revision, which plpgsql resolves at execution rather than at
creation.

**Append-only, and written only through the command.** Both relations carry
``enforce_delta_record_decision_write``, the same guard the Review Packet
relations and #836's request relation carry, so not even the schema owner
inserts one raw and the runtime capabilities hold ``select`` and nothing else.
A retirement recorded in error is not edited: ADR-0101 answers it with the
recomparison that follows a further corrected capture.

**The command writes no accepted value and fabricates no disposition.** It
inserts into these two relations and nothing else. That is ADR-0101's fifth
required proof, and it is structural here rather than asserted: the function
body contains no write to ``delta_dispositions``, ``fact_decisions`` or
``project_record_revisions``, and takes no argument that could name one.

What was considered and rejected:

- **A ``status`` column on ``proposed_deltas``.** ADR-0101 rejects it by name.
  Standing is derived from immutable relationships so it cannot drift from its
  evidence, and a mutable column would be writable by any path that forgot the
  guard -- and would lose the request, the corrected capture, the accepted
  revision, the rule version and the executor along with it.
- **Widening ``delta_supersessions``.** ``ck_delta_supersessions_successor``
  requires a successor artifact, and a correction that produced no replacement
  names none of the three. Relaxing it would make one column mean two different
  causes with different evidence.
- **One relation carrying both the result and the retirement.** See above: the
  inconclusive outcome has a result and no retirement, so the two have
  different cardinality against a delta as well as different meanings.

The downgrade drops both relations whole and refuses where rows exist, because
the supported predecessor has no shape a recorded correction result could be
carried back into.
"""

from __future__ import annotations

import sqlalchemy as sa

from corridor.migrations.source_append_commands.project_partition import (
    _UNPARTITIONED_ROLES_SQL,
)
from corridor.migrations.source_append_commands.roles import (
    RECORD_DECISION_ROLE,
    RUNTIME_LOGINS,
    SOURCE_APPEND_ROLE,
)

RESULT_TABLE = "capture_correction_results"
RETIREMENT_TABLE = "delta_capture_corrections"

#: What one investigation concluded. ``no_change`` and ``still_differs`` are
#: successful corrections and each retires the prior proposal; ``inconclusive``
#: retires nothing, because ADR-0101 forbids claiming a successful correction
#: from missing or ambiguous evidence.
NO_CHANGE = "no_change"
STILL_DIFFERS = "still_differs"
INCONCLUSIVE = "inconclusive"
CORRECTION_OUTCOMES = (NO_CHANGE, STILL_DIFFERS, INCONCLUSIVE)
_OUTCOMES_SQL = ", ".join(f"'{value}'" for value in CORRECTION_OUTCOMES)


CAPTURE_CORRECTION_RETIREMENT_SCHEMA = f"""
create table public.{RESULT_TABLE} (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    request_id bigint not null,
    delta_id bigint not null,
    document_id bigint not null,
    challenged_fact_id bigint not null,
    challenged_fact_sha256 character varying(64) not null,
    corrected_fact_id bigint,
    corrected_support_assessment_id bigint,
    accepted_revision_id bigint references public.project_record_revisions (id),
    comparison_rule_version character varying(64) not null,
    outcome character varying(32) not null,
    replacement_delta_id bigint,
    finding text not null,
    authorized_by_principal character varying(128) not null,
    executed_by character varying(128) not null,
    recorded_at timestamp with time zone not null,
    written_at timestamp with time zone not null default now(),
    idempotency_key character varying(160) not null,
    constraint uq_{RESULT_TABLE}_project_id unique (project_id, id),
    constraint uq_{RESULT_TABLE}_key unique (project_id, idempotency_key),
    constraint fk_{RESULT_TABLE}_request
        foreign key (project_id, request_id)
        references public.capture_correction_requests (project_id, id),
    constraint fk_{RESULT_TABLE}_delta
        foreign key (project_id, delta_id)
        references public.proposed_deltas (project_id, id),
    constraint fk_{RESULT_TABLE}_replacement
        foreign key (project_id, replacement_delta_id)
        references public.proposed_deltas (project_id, id),
    constraint fk_{RESULT_TABLE}_challenged_capture
        foreign key (project_id, document_id, challenged_fact_id)
        references public.facts (project_id, document_id, id),
    constraint fk_{RESULT_TABLE}_corrected_capture
        foreign key (project_id, document_id, corrected_fact_id)
        references public.facts (project_id, document_id, id),
    constraint fk_{RESULT_TABLE}_support
        foreign key (project_id, corrected_support_assessment_id)
        references public.support_assessments (project_id, id),
    constraint ck_{RESULT_TABLE}_challenged_digest check (
        challenged_fact_sha256 ~ '^[0-9a-f]{{64}}$'
    ),
    constraint ck_{RESULT_TABLE}_outcome check (outcome in ({_OUTCOMES_SQL})),
    -- The three outcomes, each shaped as itself. A no-change conclusion needs
    -- the accepted revision it was drawn against, because that revision is
    -- what the coordinator is shown; a still-differing one needs the
    -- replacement it produced; and an unsubstantiated one may carry neither a
    -- corrected capture nor a replacement, which is what stops it being
    -- recorded as a successful correction.
    constraint ck_{RESULT_TABLE}_outcome_shape check (
        (outcome = '{NO_CHANGE}'
            and corrected_fact_id is not null
            and corrected_support_assessment_id is not null
            and accepted_revision_id is not null
            and replacement_delta_id is null)
        or (outcome = '{STILL_DIFFERS}'
            and corrected_fact_id is not null
            and corrected_support_assessment_id is not null
            and replacement_delta_id is not null)
        or (outcome = '{INCONCLUSIVE}'
            and corrected_fact_id is null
            and corrected_support_assessment_id is null
            and accepted_revision_id is null
            and replacement_delta_id is null)
    ),
    constraint ck_{RESULT_TABLE}_rule check (
        length(btrim(comparison_rule_version)) > 0
    ),
    constraint ck_{RESULT_TABLE}_finding check (
        length(btrim(finding)) > 0 and length(finding) <= 2000
    ),
    -- ADR-0101 keeps the responsible operations actor and the service identity
    -- that performed the work distinct, and records both. A queued re-capture
    -- running under a service identity does not become the author of the
    -- decision to correct.
    constraint ck_{RESULT_TABLE}_authorized_by check (
        length(btrim(authorized_by_principal)) > 0
    ),
    constraint ck_{RESULT_TABLE}_executed_by check (
        length(btrim(executed_by)) > 0
    ),
    constraint ck_{RESULT_TABLE}_key_text check (
        length(btrim(idempotency_key)) > 0
    )
);

create index ix_{RESULT_TABLE}_project_id on public.{RESULT_TABLE} (project_id);
create index ix_{RESULT_TABLE}_delta_id
    on public.{RESULT_TABLE} (project_id, delta_id);
create index ix_{RESULT_TABLE}_request_id
    on public.{RESULT_TABLE} (project_id, request_id);

create trigger trg_{RESULT_TABLE}_write
    before insert or update or delete on public.{RESULT_TABLE}
    for each row execute function public.enforce_delta_record_decision_write();
create trigger trg_{RESULT_TABLE}_truncate
    before truncate on public.{RESULT_TABLE}
    for each statement execute function public.enforce_delta_record_decision_write();

create table public.{RETIREMENT_TABLE} (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    delta_id bigint not null,
    result_id bigint not null,
    retired_at timestamp with time zone not null,
    recorded_at timestamp with time zone not null default now(),
    constraint uq_{RETIREMENT_TABLE}_project_id unique (project_id, id),
    -- One retirement per Proposed Delta. This is what makes an exact retry the
    -- same act rather than a second one, and it is the index a competing
    -- retirement waits on.
    constraint uq_{RETIREMENT_TABLE}_delta unique (delta_id),
    constraint fk_{RETIREMENT_TABLE}_delta
        foreign key (project_id, delta_id)
        references public.proposed_deltas (project_id, id),
    constraint fk_{RETIREMENT_TABLE}_result
        foreign key (project_id, result_id)
        references public.{RESULT_TABLE} (project_id, id)
);

create index ix_{RETIREMENT_TABLE}_project_id
    on public.{RETIREMENT_TABLE} (project_id);
create index ix_{RETIREMENT_TABLE}_result_id
    on public.{RETIREMENT_TABLE} (project_id, result_id);

create trigger trg_{RETIREMENT_TABLE}_write
    before insert or update or delete on public.{RETIREMENT_TABLE}
    for each row execute function public.enforce_delta_record_decision_write();
create trigger trg_{RETIREMENT_TABLE}_truncate
    before truncate on public.{RETIREMENT_TABLE}
    for each statement execute function public.enforce_delta_record_decision_write();
"""


# The serialisation point every terminal writer takes, stated once. A
# transaction-scoped advisory lock rather than a row lock: `proposed_deltas` is
# append-only and the roles that run these commands hold no `update` on it, so
# `select ... for update` is not available to them, and the thing being made
# mutually exclusive is not a row's contents but the right to append the delta's
# one terminal relationship.
LOCK_PROPOSED_DELTA_TERMINAL = """
create function public.lock_proposed_delta_terminal(p_delta_id bigint)
    returns void
    language plpgsql
    set search_path to 'public'
    as $$
        begin
            perform pg_advisory_xact_lock(
                hashtextextended('proposed-delta-terminal:' || p_delta_id::text, 0)
            );
        end; $$;
"""

# "Is this delta retired by a capture correction, and by which result?", in one
# place. Every write command that must refuse a retired delta calls this rather
# than spelling the predicate again; a rule spelled in four commands is a rule
# that is three commands out of date the first time it changes.
PROPOSED_DELTA_CAPTURE_CORRECTION = f"""
create function public.proposed_delta_capture_correction(p_delta_id bigint)
    returns bigint
    language sql
    stable
    set search_path to 'public'
    as $$
        select result_id from public.{RETIREMENT_TABLE}
         where delta_id = p_delta_id
         limit 1;
    $$;
"""


RECORD_CAPTURE_CORRECTION_RESULT = f"""
create function public.record_capture_correction_result(
    p_project_id bigint,
    p_request_id bigint,
    p_delta_id bigint,
    p_challenged_fact_id bigint,
    p_challenged_fact_sha256 character varying,
    p_corrected_fact_id bigint,
    p_corrected_support_assessment_id bigint,
    p_accepted_revision_id bigint,
    p_comparison_rule_version character varying,
    p_outcome character varying,
    p_replacement_delta_id bigint,
    p_finding text,
    p_authorized_by_principal character varying,
    p_executed_by character varying,
    p_recorded_at timestamp with time zone,
    p_idempotency_key character varying
) returns jsonb
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            prior {RESULT_TABLE}%ROWTYPE;
            request capture_correction_requests%ROWTYPE;
            challenged facts%ROWTYPE;
            corrected facts%ROWTYPE;
            support support_assessments%ROWTYPE;
            delta proposed_deltas%ROWTYPE;
            live_revision bigint;
            result_id bigint;
            retirement_id bigint;
            retires boolean;
        begin
            if p_authorized_by_principal is null
               or length(btrim(p_authorized_by_principal)) = 0 then
                raise exception 'capture_correction:missing_principal a correction result names the operations actor who authorized it'
                    using errcode='23514';
            end if;
            if p_executed_by is null or length(btrim(p_executed_by)) = 0 then
                raise exception 'capture_correction:missing_executor a correction result names the identity that performed the work'
                    using errcode='23514';
            end if;
            if p_idempotency_key is null
               or length(btrim(p_idempotency_key)) = 0 then
                raise exception 'capture_correction:missing_idempotency_key a correction result needs an idempotency key'
                    using errcode='23514';
            end if;
            if p_recorded_at is null then
                raise exception 'capture_correction:missing_recorded_at a correction result records when it was reached'
                    using errcode='23514';
            end if;
            if p_outcome not in ({_OUTCOMES_SQL}) then
                raise exception 'capture_correction:invalid_outcome % is not a correction outcome', p_outcome
                    using errcode='23514';
            end if;
            retires := p_outcome <> '{INCONCLUSIVE}';

            -- An exact retry is the same act. It is answered before anything
            -- else is read, so a replay costs the same whatever has happened
            -- to the delta since, and the same key presented with different
            -- content is a bounded conflict rather than an overwrite.
            select * into prior from {RESULT_TABLE}
             where project_id = p_project_id
               and idempotency_key = p_idempotency_key;
            if found then
                if prior.delta_id <> p_delta_id
                   or prior.request_id <> p_request_id
                   or prior.outcome <> p_outcome
                   or prior.corrected_fact_id is distinct from p_corrected_fact_id
                   or prior.accepted_revision_id is distinct from p_accepted_revision_id
                   or prior.replacement_delta_id is distinct from p_replacement_delta_id then
                    raise exception 'capture_correction:key_bound_to_other_content the correction-result key is already bound to a different result'
                        using errcode='23514';
                end if;
                select retired.id into retirement_id
                  from {RETIREMENT_TABLE} retired
                 where retired.result_id = prior.id;
                return jsonb_build_object(
                    'result_id', prior.id,
                    'retirement_id', retirement_id,
                    'created', false
                );
            end if;

            -- Proof 1: the exact challenged capture and the request, by the
            -- immutable identity ADR-0100 requires. The request is read by id
            -- and then asked whether it is about *this* delta and *this*
            -- capture, so operations cannot retire whichever current item
            -- happens to resemble the report.
            select * into request from capture_correction_requests
             where id = p_request_id and project_id = p_project_id;
            if not found then
                raise exception 'capture_correction:cross_project_request extraction-error report % is not this project''s to act on', p_request_id
                    using errcode='23514';
            end if;
            if request.delta_id <> p_delta_id
               or request.fact_id <> p_challenged_fact_id
               or request.fact_content_sha256 is distinct from p_challenged_fact_sha256 then
                raise exception 'capture_correction:request_not_for_this_capture extraction-error report % does not name this proposed change and this capture', p_request_id
                    using errcode='23514';
            end if;
            select * into challenged from facts
             where id = p_challenged_fact_id and project_id = p_project_id;
            if not found or challenged.content_sha256 is distinct from p_challenged_fact_sha256 then
                raise exception 'capture_correction:capture_identity_moved the capture named by this result is not the one whose digest it recorded'
                    using errcode='23514';
            end if;

            select * into delta from proposed_deltas
             where id = p_delta_id and project_id = p_project_id;
            if not found then
                raise exception 'capture_correction:cross_project_delta Proposed Delta % is not this project''s to retire', p_delta_id
                    using errcode='23514';
            end if;

            -- Proof 2: the corrected capture is supported by the retained
            -- source. The reporter's expected interpretation is a reason for
            -- an investigation and never evidence, so what is checked is an
            -- effective Support Assessment naming this Fact, assessed
            -- supported, citing at least one passage of the challenged
            -- capture's own document.
            if p_corrected_fact_id is not null then
                select * into corrected from facts
                 where id = p_corrected_fact_id and project_id = p_project_id;
                if not found then
                    raise exception 'capture_correction:cross_project_capture corrected capture % is not this project''s', p_corrected_fact_id
                        using errcode='23514';
                end if;
                if corrected.document_id is distinct from challenged.document_id then
                    raise exception 'capture_correction:corrected_capture_other_source the corrected capture was read from another source; operations corrects a capture against bytes this source already retained'
                        using errcode='23514';
                end if;
                select * into support from support_assessments
                 where id = p_corrected_support_assessment_id
                   and project_id = p_project_id;
                if not found
                   or support.fact_id is distinct from p_corrected_fact_id
                   or support.superseded_by is not null
                   or support.assessment <> 'supported'
                   or not exists (
                       select 1 from support_assessment_sources sources
                        join source_segments segment
                          on segment.id = sources.source_segment_id
                       where sources.support_assessment_id = support.id
                         and segment.document_id = challenged.document_id
                   ) then
                    raise exception 'capture_correction:corrected_capture_unsupported the corrected capture is not held to this source by an effective Support Assessment citing one of its retained passages'
                        using errcode='23514';
                end if;
            end if;

            -- Proof 3: the recomparison used the declared rule and the stated
            -- accepted revision, and both are recorded. A replacement proposal
            -- is checked to be this project's and to be about the same subject
            -- and field, so "replaced by a corrected proposal" names the
            -- proposal it claims to.
            if p_accepted_revision_id is not null and not exists (
                select 1 from project_record_revisions
                 where id = p_accepted_revision_id and project_id = p_project_id
            ) then
                raise exception 'capture_correction:cross_project_revision the accepted revision belongs to another project'
                    using errcode='23514';
            end if;
            if p_replacement_delta_id is not null and not exists (
                select 1 from proposed_deltas
                 where id = p_replacement_delta_id
                   and project_id = p_project_id
                   and target_subject_identity = delta.target_subject_identity
                   and target_field is not distinct from delta.target_field
            ) then
                raise exception 'capture_correction:replacement_not_this_change the corrected proposal does not decide the same subject and field as the proposal it replaces'
                    using errcode='23514';
            end if;

            if retires then
                -- Every terminal writer takes this before it reads the
                -- terminal relations, so a competing resolution, deferral or
                -- retirement serialises here rather than each reading an empty
                -- table and both committing.
                perform public.lock_proposed_delta_terminal(p_delta_id);

                -- Proof 4: the delta is still eligible. A customer decision
                -- made during the investigation is preserved, not undone, and
                -- a newer source version that already superseded the delta
                -- keeps its own explanation.
                if exists (
                    select 1 from delta_dispositions where delta_id = p_delta_id
                ) then
                    raise exception 'capture_correction:already_resolved Proposed Delta % was decided during this investigation; the decision stands and any repair returns through Review', p_delta_id
                        using errcode='23514';
                end if;
                if exists (
                    select 1 from delta_supersessions where prior_delta_id = p_delta_id
                ) then
                    raise exception 'capture_correction:superseded_delta Proposed Delta % was superseded by a newer source version', p_delta_id
                        using errcode='23514';
                end if;

                -- The accepted record may not have moved under the comparison.
                -- A recomparison computed against revision n proves nothing
                -- about revision n+1, so a moved record refuses the retirement
                -- and the comparison is recomputed.
                select max(decided.revision_id) into live_revision
                  from fact_decisions decided
                 where decided.project_id = p_project_id
                   and decided.subject_key = delta.target_subject_identity
                   and decided.superseded_by is null
                   and (delta.target_field is null
                        or decided.fact_type = delta.target_field);
                if live_revision is distinct from p_accepted_revision_id then
                    raise exception 'capture_correction:stale_accepted_revision the accepted record stands at revision %, not the revision % this correction was compared against', coalesce(live_revision, 0), coalesce(p_accepted_revision_id, 0)
                        using errcode='23514';
                end if;
            end if;

            -- Nothing above wrote anything. From here the act is atomic, and
            -- it writes into these two relations only: no accepted value
            -- changes, no Project Record revision is opened, and no
            -- disposition is fabricated.
            insert into {RESULT_TABLE} (
                project_id, request_id, delta_id, document_id,
                challenged_fact_id, challenged_fact_sha256,
                corrected_fact_id, corrected_support_assessment_id,
                accepted_revision_id, comparison_rule_version, outcome,
                replacement_delta_id, finding, authorized_by_principal,
                executed_by, recorded_at, idempotency_key
            ) values (
                p_project_id, p_request_id, p_delta_id, request.document_id,
                p_challenged_fact_id, p_challenged_fact_sha256,
                p_corrected_fact_id, p_corrected_support_assessment_id,
                p_accepted_revision_id, btrim(p_comparison_rule_version),
                p_outcome, p_replacement_delta_id, btrim(p_finding),
                p_authorized_by_principal, p_executed_by, p_recorded_at,
                p_idempotency_key
            ) returning id into result_id;

            if retires then
                insert into {RETIREMENT_TABLE} (
                    project_id, delta_id, result_id, retired_at
                ) values (
                    p_project_id, p_delta_id, result_id, p_recorded_at
                ) returning id into retirement_id;
            end if;

            return jsonb_build_object(
                'result_id', result_id,
                'retirement_id', retirement_id,
                'created', true
            );
        end; $$;
"""

RECORD_CAPTURE_CORRECTION_RESULT_SIGNATURE = (
    "(bigint, bigint, bigint, bigint, character varying, bigint, bigint, "
    "bigint, character varying, character varying, bigint, text, "
    "character varying, character varying, timestamp with time zone, "
    "character varying)"
)

LOCK_PROPOSED_DELTA_TERMINAL_SIGNATURE = "(bigint)"
PROPOSED_DELTA_CAPTURE_CORRECTION_SIGNATURE = "(bigint)"


# A correction result and a retirement name one customer's own finding, so they
# answer #531's partition exactly as the Proposed Delta they are about does.
CAPTURE_CORRECTION_RETIREMENT_POLICIES = f"""
do $$
declare
    v_roles text;
    v_table text;
begin
    select string_agg(quote_ident(rolname), ', ' order by rolname)
      into v_roles
      from pg_roles
     where rolname in ({_UNPARTITIONED_ROLES_SQL});
    foreach v_table in array array['{RESULT_TABLE}', '{RETIREMENT_TABLE}']
    loop
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

CAPTURE_CORRECTION_RETIREMENT_SCHEMA_DOWN = f"""
drop function if exists public.record_capture_correction_result{RECORD_CAPTURE_CORRECTION_RESULT_SIGNATURE};
drop function if exists public.proposed_delta_capture_correction{PROPOSED_DELTA_CAPTURE_CORRECTION_SIGNATURE};
drop function if exists public.lock_proposed_delta_terminal{LOCK_PROPOSED_DELTA_TERMINAL_SIGNATURE};
drop table if exists public.{RETIREMENT_TABLE} cascade;
drop table if exists public.{RESULT_TABLE} cascade;
"""


def upgrade(op) -> None:
    # After `capture_correction`, whose request relation the result's composite
    # foreign key names, and after `review_packets`, whose `proposed_deltas`
    # project-scoped unique both relations name. Before the sibling
    # transitions, because `email_spine` and `minutes_spine` create the bulk
    # supersession sweeps that read `delta_capture_corrections`.
    op.execute(CAPTURE_CORRECTION_RETIREMENT_SCHEMA)
    for table in (RESULT_TABLE, RETIREMENT_TABLE):
        # A new table arrives carrying the schema owner's default privileges,
        # which hand every runtime login full access, so the write half is taken
        # back explicitly and only the command's role keeps it. The same terms
        # #836's request relation was granted.
        op.execute(f"revoke all on public.{table} from {RUNTIME_LOGINS}")
        op.execute(f"grant select on public.{table} to {RUNTIME_LOGINS}")
        op.execute(f"grant select, insert on public.{table} to {RECORD_DECISION_ROLE}")
        op.execute(
            f"grant usage, select on sequence public.{table}_id_seq "
            f"to {RECORD_DECISION_ROLE}"
        )
    # The command reads the request, the captures and their support to prove
    # what it is about; the record-decision role owns it and holds `select`
    # there already through the runtime grants above and the source families.
    op.execute(
        f"grant select on public.capture_correction_requests, "
        f"public.support_assessment_sources to {RECORD_DECISION_ROLE}"
    )
    # `proposed_delta_capture_correction` is `security invoker`, so it reads
    # the retirement relation as whoever called it -- and the two bulk
    # supersession sweeps that call it (`append_email_thread_reading` and
    # `append_minutes_capture`) are `security definer` functions owned by the
    # source-append role, which owns none of this family's relations. Execute
    # without select is a call that raises `insufficient_privilege` the first
    # time a retirement exists, which is exactly what CI found. Every role
    # granted execute below can now read what the helper reads.
    op.execute(
        f"grant select on public.{RETIREMENT_TABLE} to {SOURCE_APPEND_ROLE}"
    )

    op.execute(LOCK_PROPOSED_DELTA_TERMINAL)
    op.execute(PROPOSED_DELTA_CAPTURE_CORRECTION)
    # Read-only and lock-only helpers, called from inside other roles' own
    # `security definer` commands, so every role that owns one of those needs
    # execute. They are `security invoker`: a caller keeps its own authority
    # and neither helper can be used to reach anything the caller could not.
    for signature in (
        f"lock_proposed_delta_terminal{LOCK_PROPOSED_DELTA_TERMINAL_SIGNATURE}",
        f"proposed_delta_capture_correction"
        f"{PROPOSED_DELTA_CAPTURE_CORRECTION_SIGNATURE}",
    ):
        op.execute(f"revoke all on function public.{signature} from public")
        op.execute(
            f"grant execute on function public.{signature} to {RUNTIME_LOGINS}, "
            f"{RECORD_DECISION_ROLE}, {SOURCE_APPEND_ROLE}"
        )

    op.execute(RECORD_CAPTURE_CORRECTION_RESULT)
    op.execute(
        f"alter function public.record_capture_correction_result"
        f"{RECORD_CAPTURE_CORRECTION_RESULT_SIGNATURE} owner to {RECORD_DECISION_ROLE}"
    )
    op.execute(
        f"revoke all on function public.record_capture_correction_result"
        f"{RECORD_CAPTURE_CORRECTION_RESULT_SIGNATURE} from public"
    )
    # The correction procedure runs on the operations surface and in the
    # worker that performs a queued re-capture, so both capabilities execute
    # it. Which principal may ask for it is the technical-operations
    # designation, read live from the roster by `operations_repair`.
    op.execute(
        f"grant execute on function public.record_capture_correction_result"
        f"{RECORD_CAPTURE_CORRECTION_RESULT_SIGNATURE} to {RUNTIME_LOGINS}"
    )
    op.execute(CAPTURE_CORRECTION_RETIREMENT_POLICIES)


def downgrade(op) -> None:
    # Before `capture_correction` unwinds the request relation this block's
    # composite foreign key names, mirroring the upgrade's order. The supported
    # predecessor has no such relations, so a recorded correction result would
    # go silently on the way down, taking the only record of why a proposal
    # left Review with it; refuse instead.
    if op.get_bind().scalar(
        sa.text(f"select exists (select 1 from public.{RESULT_TABLE})")
    ):
        raise RuntimeError(
            "retained capture-correction results cannot be represented by the "
            "supported predecessor"
        )
    op.execute(
        f"do $$ begin "
        f"if exists (select 1 from pg_tables where schemaname = 'public' "
        f"and tablename = '{RESULT_TABLE}') then "
        f"revoke select on public.{RESULT_TABLE}, public.{RETIREMENT_TABLE} "
        f"from {RUNTIME_LOGINS}; "
        f"end if; end $$;"
    )
    op.execute(CAPTURE_CORRECTION_RETIREMENT_SCHEMA_DOWN)
