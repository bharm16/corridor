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

**The seventh proof: the reported passage applies to this subject and field
(#945).**  ADR-0100's same-document rule was the only thing standing between a
correction and any cell of the workbook, and #945 proved end to end what that
allows: another Utility Conflict's own valid cell selected as the supporting
passage, a Source Fact written under the challenged conflict's subject carrying
the neighbour's value, a ``supported`` ``value_support`` assessment naming only
the neighbour's cell, the proposal retired and that value proposed onto the
record.  ADR-0082 already forbade the result -- support is a relation between
*one proposition* and its evidence, and a passage can support one proposition
and be irrelevant to another.

So the command derives, for itself, what the retained source structure says
about the passage the report named, and refuses a substantiated outcome the
structure does not hold to this subject and this field.  Three things make it
a proof rather than a restated opinion:

- **It is computed here, from rows a correction cannot write**: the passage's
  own typed locator, the customer's adopted ``project_baseline_source_rows``
  registration, and a retained heading cell of the passage's own column.  No
  Fact is an input -- not the challenged capture, not a neighbour's, and above
  all not the corrected capture this command is about to insert.  A caller
  cannot manufacture its own admission by creating a Fact that claims the
  subject, which is the circularity the rule names.
- **The caller's stated subject is contradicted rather than believed.**  The
  subject half is entirely the command's; a stated one that disagrees is a
  bounded refusal.  The field half rests on the released heading vocabulary,
  which stays in ``sheets.column_mapping`` rather than being restated in SQL
  where two copies would drift -- so what is proved here is that the claim is
  anchored to real retained bytes above the passage in its own column, and the
  exact heading text is copied off that cell so a later reader can check the
  vocabulary claim without trusting anyone's summary of it.
- **The relation refuses it too.**  ``ck_capture_correction_results_applicable_correction``
  admits a corrected capture only beside the ``applicable`` verdict, so the
  containment survives a future caller that forgets the rule.

Two neighbouring proofs were loosened by the same defect and are tightened with
it: the corrected capture must be about the challenged subject and field, and
its Support Assessment must cite **the passage the report named** rather than
merely some passage of the same document -- which is how a caller assembling
its own convenient assessment used to get through.

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

from hashlib import sha256
import json

import sqlalchemy as sa

from corridor.migrations.source_append_commands.project_partition import (
    _UNPARTITIONED_ROLES_SQL,
)
from corridor.migrations.source_append_commands.roles import (
    RECORD_DECISION_ROLE,
    RUNTIME_LOGINS,
    SOURCE_APPEND_ROLE,
)
from corridor.sheets import HEADING_FIELD_VOCABULARY

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

#: What the retained source structure said about the reported passage (#945),
#: spelled where the check constraint spells it. ``applicable`` is the only one
#: a corrected capture may be written under: the other three each record that
#: the evidence does not hold this passage to the challenged subject and field,
#: which ADR-0082 has always said is what support means.
APPLICABLE = "applicable"
OTHER_SUBJECT = "other_subject"
OTHER_FIELD = "other_field"
UNCLEAR = "unclear"
APPLICABILITY_VERDICTS = (APPLICABLE, OTHER_SUBJECT, OTHER_FIELD, UNCLEAR)
_VERDICTS_SQL = ", ".join(f"'{value}'" for value in APPLICABILITY_VERDICTS)


# --- The released heading vocabulary, as the command's own trusted copy (#945).
#
# The field a structured column carries is what the released heading vocabulary
# names its retained header, and the command derives that for itself rather than
# trusting a caller's word for it: a heading cell is a mapping record the caller
# may identify, but what the column *means* is the command's answer, exactly as
# the passage's subject already is.  The vocabulary lives in ``sheets`` for every
# other reader (``column_mapping`` and the capture path it files by), so this is
# a versioned copy *derived* from that one -- generated here, never re-spelled,
# so the two cannot state two different vocabularies.  A released migration's
# bytes are frozen, so ``tests/test_capture_correction_retirement.py`` proves the
# copy the running schema carries still matches ``sheets`` and the version below
# still digests it; a change to the vocabulary that forgot this block would fail
# there rather than let the writing transaction and the reader drift apart.
#
# The keys of ``HEADING_FIELD_VOCABULARY`` are already collapsed and casefolded
# the way ``column_mapping`` keys them, so the lookup normalises an arbitrary
# retained heading to the same shape (``lower(btrim(collapse whitespace))``,
# which is ``" ".join(str(x).split()).casefold()`` for the ASCII headings a
# published form prints) before it compares.


def _sql_text_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


_HEADING_FIELD_ROWS = ",\n            ".join(
    f"({_sql_text_literal(heading)}, {_sql_text_literal(field)})"
    for heading, field in sorted(HEADING_FIELD_VOCABULARY.items())
)

#: A stable digest of the frozen mapping, so "which vocabulary said so" is a
#: checkable version rather than an implicit one.  Recomputed by the drift test
#: from ``sheets`` and compared against what the schema returns.
STRUCTURED_HEADING_VOCABULARY_VERSION = sha256(
    json.dumps(
        sorted(HEADING_FIELD_VOCABULARY.items()), separators=(",", ":")
    ).encode("utf-8")
).hexdigest()


STRUCTURED_HEADING_FIELD_VOCABULARY = f"""
create function public.structured_heading_field_vocabulary()
    returns table (normalized_heading text, canonical_field text)
    language sql
    immutable
    set search_path to 'public'
    as $$
        select * from (values
            {_HEADING_FIELD_ROWS}
        ) as v (normalized_heading, canonical_field);
    $$;
"""

STRUCTURED_HEADING_VOCABULARY_VERSION_FN = f"""
create function public.structured_heading_vocabulary_version()
    returns text
    language sql
    immutable
    set search_path to 'public'
    as $$
        select '{STRUCTURED_HEADING_VOCABULARY_VERSION}'::text;
    $$;
"""

STRUCTURED_HEADING_FIELD = r"""
create function public.structured_heading_field(p_heading text)
    returns text
    language sql
    stable
    set search_path to 'public'
    as $$
        select v.canonical_field
          from public.structured_heading_field_vocabulary() v
         where v.normalized_heading
             = lower(btrim(regexp_replace(coalesce(p_heading, ''), '\s+', ' ', 'g')));
    $$;
"""

STRUCTURED_HEADING_FIELD_VOCABULARY_SIGNATURE = "()"
STRUCTURED_HEADING_VOCABULARY_VERSION_SIGNATURE = "()"
STRUCTURED_HEADING_FIELD_SIGNATURE = "(text)"


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
    -- #945's proof: what the retained source structure said about the passage
    -- the report named, and the evidence it said it from. The verdict is this
    -- command's own derivation, never the caller's word, and the heading text
    -- is copied off the named cell here rather than supplied.
    applicability_verdict character varying(32) not null,
    passage_subject_identity character varying(160),
    passage_field character varying(64),
    passage_field_heading_segment_id bigint,
    passage_field_heading_text text,
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
    constraint fk_{RESULT_TABLE}_field_heading
        foreign key (project_id, document_id, passage_field_heading_segment_id)
        references public.source_segments (project_id, document_id, id),
    constraint ck_{RESULT_TABLE}_challenged_digest check (
        challenged_fact_sha256 ~ '^[0-9a-f]{{64}}$'
    ),
    constraint ck_{RESULT_TABLE}_outcome check (outcome in ({_OUTCOMES_SQL})),
    constraint ck_{RESULT_TABLE}_applicability check (
        applicability_verdict in ({_VERDICTS_SQL})
    ),
    -- The containment, as the relation's own shape rather than as a rule the
    -- command remembers to apply (#945). A substantiated correction asserts a
    -- value about a subject and a field, so it exists only where the retained
    -- evidence held the reported passage to that subject and that field; the
    -- other three verdicts can only ever be recorded as an investigation that
    -- concluded nothing. Whatever else a future caller gets wrong, it cannot
    -- write a corrected capture over a passage the structure contradicts.
    constraint ck_{RESULT_TABLE}_applicable_correction check (
        outcome = '{INCONCLUSIVE}' or applicability_verdict = '{APPLICABLE}'
    ),
    constraint ck_{RESULT_TABLE}_applicable_shape check (
        applicability_verdict <> '{APPLICABLE}'
        or (passage_subject_identity is not null
            and passage_field is not null
            and passage_field_heading_segment_id is not null)
    ),
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


# "Which decision on this delta is in force?", in one place (#948).  A Proposed
# Delta may carry more than one disposition once an Undo has returned it to
# Review, and the retained rows are never rewritten, so "is this delta settled?"
# stopped being "does a row exist".  It is the highest generation no reversal
# names -- and a reversal names a disposition through the authority binding the
# decision wrote and the packet child that carried it, which is the same chain
# `live_delta_status` already walks for a reversed deferral.  A standalone
# resolution belongs to no packet, so no reversal can name it and it stays
# effective, which is the behaviour that family has always had.
#
# `language sql` and `stable`, like the sibling above, so a caller keeps its own
# authority: this reads no more than the role calling it could read itself.
PROPOSED_DELTA_EFFECTIVE_DISPOSITION = """
create function public.proposed_delta_effective_disposition(p_delta_id bigint)
    returns bigint
    language plpgsql
    stable
    set search_path to 'public'
    as $$
        declare
            v_effective bigint[];
        begin
            select coalesce(array_agg(d.id order by d.generation), '{}'::bigint[])
              into v_effective
              from public.delta_dispositions d
             where d.delta_id = p_delta_id
               and not exists (
                    select 1
                      from public.delta_record_decisions decision
                      join public.delta_review_packet_children child
                        on child.decision_id = decision.id
                      join public.delta_review_packet_reversals reversal
                        on reversal.receipt_id = child.receipt_id
                     where decision.disposition_id = d.id
               );
            -- Deliberately not "take the highest generation". Two decisions in
            -- force for one delta is history contradicting itself, and a
            -- reader that quietly picked one would be the mechanism that made
            -- a contradictory write look harmless -- the failure ADR-0101
            -- names for its own precedence order. No command can write this
            -- pair: the generation is assigned here, under the terminal lock,
            -- from the decisions already reversed. It is raised rather than
            -- resolved so an import or a raw insert that produced it is seen.
            if coalesce(array_length(v_effective, 1), 0) > 1 then
                raise exception
                    'Proposed Delta % carries more than one effective decision (%); its resolution history contradicts itself',
                    p_delta_id, v_effective
                    using errcode='23514';
            end if;
            return v_effective[1];
        end; $$;
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
    p_passage_subject_identity character varying,
    p_passage_field character varying,
    p_passage_field_heading_segment_id bigint,
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
            selected source_segments%ROWTYPE;
            heading source_segments%ROWTYPE;
            registered project_baseline_source_rows%ROWTYPE;
            live_revision bigint;
            result_id bigint;
            retirement_id bigint;
            retires boolean;
            v_challenged_field character varying;
            v_row integer;
            v_column character varying;
            v_row_identity character varying;
            v_passage_subject character varying;
            v_passage_field character varying;
            v_heading_id bigint;
            v_heading_text text;
            v_verdict character varying;
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

            -- Proof 2: the reported passage carries a value for *this*
            -- subject and *this* field (#945). Being a passage of the same
            -- source is necessary and is not sufficient: another Utility
            -- Conflict's own cell is a passage of this workbook and says
            -- nothing about this conflict, and a Required By cell on the right
            -- row is the right conflict under the wrong field. The verdict is
            -- derived here, from rows the correction cannot write -- the
            -- passage's own locator, the customer's adopted source-row
            -- registration, and a retained heading cell of the passage's own
            -- column. No Fact is an input, so the corrected capture this
            -- command is about to record cannot be the evidence that admits
            -- it, which is the circularity ADR-0082 rules out.
            v_challenged_field := coalesce(delta.target_field, challenged.fact_type);
            select * into selected from source_segments
             where id = request.selected_source_segment_id
               and project_id = p_project_id;
            if not found then
                raise exception 'capture_correction:passage_not_retained the passage this report named is no longer retained'
                    using errcode='23514';
            end if;
            if selected.kind = 'spreadsheet_cell'
               and selected.sheet_name is not null
               and selected.cell_range ~ '^[A-Z]+[1-9][0-9]*$' then
                v_column := substring(selected.cell_range from '^[A-Z]+');
                v_row := (substring(selected.cell_range from '[0-9]+$'))::int;
                -- One row resolves under two retained rules and the product
                -- uses both: the `sheet_name!worksheet_row_number` identity
                -- every structured capture is filed under, and the adopted
                -- row's own record_subject_key, which is the customer's
                -- resolution of that row and is its business identity where
                -- the form prints one. A row resolves to either, so what is
                -- asked is whether the challenged subject is one of them --
                -- which refuses a neighbouring row whichever space the delta
                -- is stated in. A row the adoption excluded resolves to
                -- nothing: it is not in the record.
                v_row_identity := selected.sheet_name || '!' || v_row::text;
                select * into registered from project_baseline_source_rows
                 where project_id = p_project_id
                   and sheet_name = selected.sheet_name
                   and row_number = v_row
                 order by baseline_source_id desc, id desc
                 limit 1;
                if not found then
                    v_passage_subject := v_row_identity;
                elsif registered.excluded
                      or registered.record_subject_key is null then
                    v_passage_subject := null;
                elsif challenged.subject_key in (
                    v_row_identity, registered.record_subject_key
                ) then
                    v_passage_subject := challenged.subject_key;
                else
                    v_passage_subject := registered.record_subject_key;
                end if;
            end if;
            -- The subject half is the command's own answer, so a caller that
            -- states a different one is contradicted rather than believed.
            if p_passage_subject_identity is distinct from v_passage_subject then
                raise exception 'capture_correction:passage_subject_disagrees the stated subject for this passage is not the one its retained source row resolves to'
                    using errcode='23514';
            end if;
            -- The field the passage's column carries is what the released
            -- heading vocabulary names its retained header, and it is derived
            -- here from that header's own words -- never taken as the caller's
            -- word for it (#945). The caller may identify the heading cell (a
            -- mapping record); it may not supply what the column means. The
            -- cell has to be a retained heading of the passage's own column,
            -- above it, and the field is then read from its exact text through
            -- the same vocabulary `sheets.column_mapping` files every
            -- structured capture by -- copied into the schema as a versioned,
            -- command-trusted table so the writing transaction and the reader
            -- cannot state two vocabularies. A heading the vocabulary does not
            -- name establishes nothing, so the field stays unclear and the cell
            -- is not recorded as evidence for a claim it does not support. The
            -- exact heading text is copied off the cell here so a later reader
            -- can re-check the vocabulary claim against those retained bytes.
            if p_passage_field_heading_segment_id is not null
               and v_row is not null then
                select * into heading from source_segments
                 where id = p_passage_field_heading_segment_id
                   and project_id = p_project_id;
                if not found
                   or heading.document_id is distinct from selected.document_id
                   or heading.kind <> 'spreadsheet_cell'
                   or heading.sheet_name is distinct from selected.sheet_name
                   or heading.cell_range !~ '^[A-Z]+[1-9][0-9]*$'
                   or substring(heading.cell_range from '^[A-Z]+') <> v_column
                   or (substring(heading.cell_range from '[0-9]+$'))::int >= v_row then
                    raise exception 'capture_correction:field_heading_not_this_column the heading this field claim rests on is not a retained cell above this passage in its own column'
                        using errcode='23514';
                end if;
                v_passage_field := public.structured_heading_field(heading.exact_text);
                if v_passage_field is not null then
                    v_heading_id := heading.id;
                    v_heading_text := heading.exact_text;
                end if;
            end if;
            -- The stated field is contradicted rather than believed, exactly as
            -- the stated subject is: what the retained header names through the
            -- released vocabulary is the answer, and a stated field the header
            -- does not draw from it is a bounded refusal.
            if p_passage_field is distinct from v_passage_field then
                raise exception 'capture_correction:passage_field_disagrees the stated field for this passage is not the one its retained header names through the released heading vocabulary'
                    using errcode='23514';
            end if;

            if v_passage_subject is null or v_passage_field is null then
                v_verdict := '{UNCLEAR}';
            elsif v_passage_subject is distinct from challenged.subject_key then
                v_verdict := '{OTHER_SUBJECT}';
            elsif v_passage_field is distinct from v_challenged_field then
                v_verdict := '{OTHER_FIELD}';
            else
                v_verdict := '{APPLICABLE}';
            end if;
            if retires and v_verdict <> '{APPLICABLE}' then
                raise exception 'capture_correction:passage_not_applicable the retained source does not establish that the reported passage carries % for %; no corrected capture may be recorded from it', v_challenged_field, challenged.subject_key
                    using errcode='23514';
            end if;

            -- Proof 3: the corrected capture is supported by the retained
            -- source. The reporter's expected interpretation is a reason for
            -- an investigation and never evidence, so what is checked is an
            -- effective Support Assessment naming this Fact, assessed
            -- supported, citing the very passage the report named -- not
            -- merely some passage of this document, which is how a caller
            -- assembling its own convenient assessment used to get through.
            -- The corrected capture must also be about the challenged subject
            -- and field, so a Fact recorded under some other subject cannot be
            -- offered as this one's correction.
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
                if corrected.subject_key is distinct from challenged.subject_key
                   or corrected.fact_type is distinct from v_challenged_field then
                    raise exception 'capture_correction:corrected_capture_other_subject the corrected capture is about % %, not the % of % this report challenged', corrected.subject_key, corrected.fact_type, v_challenged_field, challenged.subject_key
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
                       where sources.support_assessment_id = support.id
                         and sources.source_segment_id
                             = request.selected_source_segment_id
                   ) then
                    raise exception 'capture_correction:corrected_capture_unsupported the corrected capture is not held to this source by an effective Support Assessment citing the passage this report named'
                        using errcode='23514';
                end if;
            end if;

            -- Proof 4: the recomparison used the declared rule and the stated
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

                -- Proof 5: the delta is still eligible. A customer decision
                -- made during the investigation is preserved, not undone, and
                -- a newer source version that already superseded the delta
                -- keeps its own explanation.
                -- An effective disposition, not merely a row: a decision the
                -- coordinator undid is retained history and settles nothing,
                -- so the question it answered is answerable again (#948).
                if public.proposed_delta_effective_disposition(p_delta_id)
                   is not null then
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
                accepted_revision_id, comparison_rule_version,
                applicability_verdict, passage_subject_identity, passage_field,
                passage_field_heading_segment_id, passage_field_heading_text,
                outcome,
                replacement_delta_id, finding, authorized_by_principal,
                executed_by, recorded_at, idempotency_key
            ) values (
                p_project_id, p_request_id, p_delta_id, request.document_id,
                p_challenged_fact_id, p_challenged_fact_sha256,
                p_corrected_fact_id, p_corrected_support_assessment_id,
                p_accepted_revision_id, btrim(p_comparison_rule_version),
                v_verdict, v_passage_subject, v_passage_field,
                v_heading_id, v_heading_text,
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
    "bigint, character varying, character varying, character varying, bigint, "
    "character varying, bigint, text, "
    "character varying, character varying, timestamp with time zone, "
    "character varying)"
)

LOCK_PROPOSED_DELTA_TERMINAL_SIGNATURE = "(bigint)"
PROPOSED_DELTA_CAPTURE_CORRECTION_SIGNATURE = "(bigint)"
PROPOSED_DELTA_EFFECTIVE_DISPOSITION_SIGNATURE = "(bigint)"


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
drop function if exists public.structured_heading_field{STRUCTURED_HEADING_FIELD_SIGNATURE};
drop function if exists public.structured_heading_vocabulary_version{STRUCTURED_HEADING_VOCABULARY_VERSION_SIGNATURE};
drop function if exists public.structured_heading_field_vocabulary{STRUCTURED_HEADING_FIELD_VOCABULARY_SIGNATURE};
drop function if exists public.proposed_delta_effective_disposition{PROPOSED_DELTA_EFFECTIVE_DISPOSITION_SIGNATURE};
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
    # `proposed_delta_effective_disposition` is `security invoker` for the same
    # reason, and the same two bulk sweeps call it to skip a delta whose
    # decision still stands (#948). It walks the authority binding, the packet
    # child and the reversal, and the source-append role reads none of those
    # three today; execute without select is the `insufficient_privilege` that
    # sibling above was fixed for. It reads them and writes none of them.
    op.execute(
        "grant select on public.delta_record_decisions, "
        "public.delta_review_packet_children, "
        f"public.delta_review_packet_reversals to {SOURCE_APPEND_ROLE}"
    )

    op.execute(LOCK_PROPOSED_DELTA_TERMINAL)
    op.execute(PROPOSED_DELTA_CAPTURE_CORRECTION)
    # The third shared predicate, created here for the same reason the second
    # is: it joins `delta_record_decisions`, `delta_review_packet_children` and
    # `delta_review_packet_reversals`, and a `language sql` body is parsed when
    # the function is created, so it has to follow the families that make those
    # relations. The commands that call it are plpgsql and bind late, exactly
    # as they already do for `proposed_delta_capture_correction` (#948).
    op.execute(PROPOSED_DELTA_EFFECTIVE_DISPOSITION)
    # Read-only and lock-only helpers, called from inside other roles' own
    # `security definer` commands, so every role that owns one of those needs
    # execute. They are `security invoker`: a caller keeps its own authority
    # and neither helper can be used to reach anything the caller could not.
    for signature in (
        f"lock_proposed_delta_terminal{LOCK_PROPOSED_DELTA_TERMINAL_SIGNATURE}",
        f"proposed_delta_capture_correction"
        f"{PROPOSED_DELTA_CAPTURE_CORRECTION_SIGNATURE}",
        f"proposed_delta_effective_disposition"
        f"{PROPOSED_DELTA_EFFECTIVE_DISPOSITION_SIGNATURE}",
    ):
        op.execute(f"revoke all on function public.{signature} from public")
        op.execute(
            f"grant execute on function public.{signature} to {RUNTIME_LOGINS}, "
            f"{RECORD_DECISION_ROLE}, {SOURCE_APPEND_ROLE}"
        )

    # The released heading vocabulary, as the command's own trusted copy (#945).
    # `structured_heading_field` is `language sql`, so its body is parsed at
    # creation and the vocabulary table function it reads has to exist first.
    # The command that calls it is plpgsql and binds late, so it may follow.
    op.execute(STRUCTURED_HEADING_FIELD_VOCABULARY)
    op.execute(STRUCTURED_HEADING_VOCABULARY_VERSION_FN)
    op.execute(STRUCTURED_HEADING_FIELD)
    # The command derives the field through these inside its own `security
    # definer` body, so its owner needs execute; the runtime logins and the
    # source-append role get it too, on the same terms as the other read-only
    # helpers, so a later reader or check can resolve a heading the same way.
    for signature in (
        f"structured_heading_field_vocabulary"
        f"{STRUCTURED_HEADING_FIELD_VOCABULARY_SIGNATURE}",
        f"structured_heading_vocabulary_version"
        f"{STRUCTURED_HEADING_VOCABULARY_VERSION_SIGNATURE}",
        f"structured_heading_field{STRUCTURED_HEADING_FIELD_SIGNATURE}",
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
