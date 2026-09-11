"""What a coordinator reported about one capture, bound to that exact capture (#836).

Folded into this transition for the same window reason as the blocks around it:
``corridor.migrations.policy`` allows one unreleased transition and this is it.

ADR-0100 adds one ancillary action to the focused Review form. It changes
nothing in the accepted record and resolves no Proposed Delta, so it is not a
disposition and gets no column on one; what it needs is a place to say that a
named person challenged a named capture against a named passage, and why.

**The relation exists because the delta cannot answer the question.** A
``ProposedDelta`` carries subject, field, source family and source revision and
no foreign key to a Source Fact, so the screen reconstructs the capture behind a
delta as the newest Fact for that subject and field inside the delta's own
source lineage. That answer moves the moment a second capture of the same
document and field lands. A request that stored only the delta would therefore
store a query, and operations opening it weeks later would be looking at a
different capture than the person who reported the defect. ``fact_id`` and
``fact_content_sha256`` are recorded instead, and both are immutable -- facts
are append-only and ``uq_facts_content_sha256`` makes the digest the Fact's own
identity.

**The composite foreign keys are the point, not decoration.** ``fact_id`` is
named through ``(project_id, document_id, fact_id)``, so ``document_id`` is the
capture's own document by construction rather than by a caller's assertion. Both
segment columns are then named through that same ``document_id``, so "the
passage the coordinator selected belongs to this capture's own source, in this
project" is unrepresentable-if-wrong rather than merely tested. Operations
corrects a capture against bytes that are already retained; a passage in another
customer's file, or in another file of this customer's, is not that act.

**The cited passage and the selected passage are different columns on purpose.**
``source_segment_id`` is where the capture said it read the value;
``selected_source_segment_id`` is where the coordinator says it should have been
read. Reading the wrong cell is one of the defects being reported, so collapsing
the two would erase the report.

**Append-only, and written only through the command.** A wrong report is
corrected by making another attributable report, never by editing the statement
of what somebody said and when. The relation carries the same
``enforce_delta_record_decision_write`` guard the Review Packet relations carry,
so not even the schema owner inserts one raw, and the runtime capabilities hold
``select`` and nothing else.

**No coordination designation is proved on the row.** ADR-0100 makes this an act
about a capture. It writes no accepted authority and makes nothing effective, so
the roster gate every project surface already passes through is what admits the
caller -- exactly as it is for the retained correspondence relations (#652), and
unlike #533's release or #839's preparation, which do prove a designation
because they make something effective.

**What is deliberately absent.** There is no outcome, no state and no closure
column. What became of a request is answered by the corrected capture and by
what the lifecycle records about the original finding, and ADR-0100 leaves the
no-change half of that lifecycle explicitly undecided -- a status column here
would be this migration quietly answering it.

What was considered and rejected:

- **An ``audit_log`` entry.** The audit log attributes an act; it is not
  queryable state, and both the screen and the operations half have to *read*
  open requests. It also cannot carry a foreign key, which is the whole
  mechanism above.
- **Columns on ``proposed_deltas``.** A Proposed Delta is one immutable
  occurrence of a difference (#518). A request about the capture behind it is a
  different authorship with a different lifetime, and several may be made.
- **A column on ``facts``.** The spine's capture tables are written only by the
  source-append commands and say what the source says. A customer's complaint
  about a capture is not part of what the source says.

The downgrade drops the relation whole rather than opening it to raw writes,
and refuses where rows exist, because the supported predecessor has no shape a
retained report could be carried back into.
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

CORRECTION_TABLE = "capture_correction_requests"

CAPTURE_CORRECTION_SCHEMA = f"""
create table public.{CORRECTION_TABLE} (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    delta_id bigint not null,
    document_id bigint not null,
    fact_id bigint not null,
    fact_content_sha256 character varying(64) not null,
    source_segment_id bigint,
    selected_source_segment_id bigint not null,
    expected_interpretation text not null,
    reported_by_principal character varying(128) not null,
    reported_at timestamp with time zone not null,
    idempotency_key character varying(160) not null,
    recorded_at timestamp with time zone not null default now(),
    constraint uq_{CORRECTION_TABLE}_project_id unique (project_id, id),
    constraint uq_{CORRECTION_TABLE}_key unique (project_id, idempotency_key),
    constraint fk_{CORRECTION_TABLE}_delta
        foreign key (project_id, delta_id)
        references public.proposed_deltas (project_id, id),
    constraint fk_{CORRECTION_TABLE}_fact
        foreign key (project_id, document_id, fact_id)
        references public.facts (project_id, document_id, id),
    constraint fk_{CORRECTION_TABLE}_cited_passage
        foreign key (project_id, document_id, source_segment_id)
        references public.source_segments (project_id, document_id, id),
    constraint fk_{CORRECTION_TABLE}_selected_passage
        foreign key (project_id, document_id, selected_source_segment_id)
        references public.source_segments (project_id, document_id, id),
    constraint ck_{CORRECTION_TABLE}_digest check (
        fact_content_sha256 ~ '^[0-9a-f]{{64}}$'
    ),
    constraint ck_{CORRECTION_TABLE}_interpretation check (
        length(btrim(expected_interpretation)) > 0
        and length(expected_interpretation) <= 2000
    ),
    constraint ck_{CORRECTION_TABLE}_principal check (
        length(btrim(reported_by_principal)) > 0
    ),
    constraint ck_{CORRECTION_TABLE}_key_text check (
        length(btrim(idempotency_key)) > 0
    )
);

create index ix_{CORRECTION_TABLE}_project_id
    on public.{CORRECTION_TABLE} (project_id);
create index ix_{CORRECTION_TABLE}_delta_id
    on public.{CORRECTION_TABLE} (project_id, delta_id);
create index ix_{CORRECTION_TABLE}_fact_id
    on public.{CORRECTION_TABLE} (project_id, fact_id);

create trigger trg_{CORRECTION_TABLE}_write
    before insert or update or delete on public.{CORRECTION_TABLE}
    for each row execute function public.enforce_delta_record_decision_write();
create trigger trg_{CORRECTION_TABLE}_truncate
    before truncate on public.{CORRECTION_TABLE}
    for each statement execute function public.enforce_delta_record_decision_write();
"""


REPORT_CAPTURE_CORRECTION = f"""
create function public.report_capture_correction(
    p_project_id bigint,
    p_delta_id bigint,
    p_fact_id bigint,
    p_fact_content_sha256 character varying,
    p_source_segment_id bigint,
    p_selected_source_segment_id bigint,
    p_expected_interpretation text,
    p_principal character varying,
    p_reported_at timestamp with time zone,
    p_idempotency_key character varying
) returns jsonb
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            prior {CORRECTION_TABLE}%ROWTYPE;
            capture facts%ROWTYPE;
            selected_document bigint;
            delta_project bigint;
            request_id bigint;
        begin
            if p_principal is null or length(btrim(p_principal)) = 0 then
                raise exception 'capture_correction:missing_principal reporting an extraction error names the person reporting it'
                    using errcode='23514';
            end if;
            if p_idempotency_key is null
               or length(btrim(p_idempotency_key)) = 0 then
                raise exception 'capture_correction:missing_idempotency_key reporting an extraction error needs an idempotency key'
                    using errcode='23514';
            end if;
            if p_reported_at is null then
                raise exception 'capture_correction:missing_reported_at reporting an extraction error records when it was reported'
                    using errcode='23514';
            end if;
            if p_expected_interpretation is null
               or length(btrim(p_expected_interpretation)) = 0 then
                raise exception 'capture_correction:missing_interpretation reporting an extraction error records what the passage says'
                    using errcode='23514';
            end if;

            select * into prior from {CORRECTION_TABLE}
             where project_id = p_project_id
               and idempotency_key = p_idempotency_key;
            if found then
                if prior.fact_id <> p_fact_id
                   or prior.delta_id <> p_delta_id then
                    raise exception 'capture_correction:key_bound_to_other_content the report key is already bound to another capture'
                        using errcode='23514';
                end if;
                return jsonb_build_object('request_id', prior.id, 'created', false);
            end if;

            select project_id into delta_project from proposed_deltas
             where id = p_delta_id;
            if delta_project is null or delta_project <> p_project_id then
                raise exception 'capture_correction:cross_project_delta proposed change % is not this project''s to report on', p_delta_id
                    using errcode='23514';
            end if;

            select * into capture from facts
             where id = p_fact_id and project_id = p_project_id;
            if not found then
                raise exception 'capture_correction:cross_project_capture capture % is not this project''s to report on', p_fact_id
                    using errcode='23514';
            end if;
            if capture.document_id is null then
                raise exception 'capture_correction:no_source_document capture % was not read from a document, so there is no extraction to correct', p_fact_id
                    using errcode='23514';
            end if;
            if capture.content_sha256 is distinct from p_fact_content_sha256 then
                raise exception 'capture_correction:capture_identity_moved the capture named by this report is not the one whose digest it recorded'
                    using errcode='23514';
            end if;

            select document_id into selected_document from source_segments
             where id = p_selected_source_segment_id
               and project_id = p_project_id;
            if selected_document is null
               or selected_document <> capture.document_id then
                raise exception 'capture_correction:passage_not_in_this_source the selected passage is not one this source retained'
                    using errcode='23514';
            end if;

            insert into {CORRECTION_TABLE} (
                project_id, delta_id, document_id, fact_id,
                fact_content_sha256, source_segment_id,
                selected_source_segment_id, expected_interpretation,
                reported_by_principal, reported_at, idempotency_key
            ) values (
                p_project_id, p_delta_id, capture.document_id, p_fact_id,
                p_fact_content_sha256, p_source_segment_id,
                p_selected_source_segment_id,
                btrim(p_expected_interpretation),
                p_principal, p_reported_at, p_idempotency_key
            ) returning id into request_id;
            return jsonb_build_object('request_id', request_id, 'created', true);
        end; $$;
"""

REPORT_CAPTURE_CORRECTION_SIGNATURE = (
    "(bigint, bigint, bigint, character varying, bigint, bigint, text, "
    "character varying, timestamp with time zone, character varying)"
)


# A report names one customer's own source and one of their own findings, so it
# answers #531's partition exactly as the Proposed Delta it is about does.
CAPTURE_CORRECTION_PARTITION_POLICIES = f"""
do $$
declare
    v_roles text;
    v_table text := '{CORRECTION_TABLE}';
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

CAPTURE_CORRECTION_SCHEMA_DOWN = f"""
drop function if exists public.report_capture_correction{REPORT_CAPTURE_CORRECTION_SIGNATURE};
drop table if exists public.{CORRECTION_TABLE} cascade;
"""


def upgrade(op) -> None:
    # After the Review Packet block, whose relations this one's composite
    # foreign keys resolve against, and after the spine tables the baseline
    # created. Nothing later in the revision names this table.
    op.execute(CAPTURE_CORRECTION_SCHEMA)
    # A new table arrives carrying the schema owner's default privileges, which
    # hand every runtime login full access, so the write half is taken back
    # explicitly and only the command's role keeps it. The same terms the
    # Review Packet relations were granted.
    op.execute(f"revoke all on public.{CORRECTION_TABLE} from {RUNTIME_LOGINS}")
    op.execute(f"grant select on public.{CORRECTION_TABLE} to {RUNTIME_LOGINS}")
    op.execute(
        f"grant select, insert on public.{CORRECTION_TABLE} to {RECORD_DECISION_ROLE}"
    )
    op.execute(
        f"grant usage, select on sequence public.{CORRECTION_TABLE}_id_seq "
        f"to {RECORD_DECISION_ROLE}"
    )
    op.execute(REPORT_CAPTURE_CORRECTION)
    op.execute(
        f"alter function public.report_capture_correction"
        f"{REPORT_CAPTURE_CORRECTION_SIGNATURE} owner to {RECORD_DECISION_ROLE}"
    )
    op.execute(
        f"revoke all on function public.report_capture_correction"
        f"{REPORT_CAPTURE_CORRECTION_SIGNATURE} from public"
    )
    # Reporting an extraction error is an attributable human act on the Review
    # screen, so it joins the other decision commands on the web capability
    # alone (ADR-0100).
    op.execute(
        f"grant execute on function public.report_capture_correction"
        f"{REPORT_CAPTURE_CORRECTION_SIGNATURE} to corridor_web"
    )
    op.execute(CAPTURE_CORRECTION_PARTITION_POLICIES)


def downgrade(op) -> None:
    # Before the Review Packet block unwinds the relations this block's
    # composite foreign keys name, mirroring the upgrade's order. The supported
    # predecessor has no such table, so a retained report would go silently on
    # the way down; refuse rather than lose what a person said about a capture.
    if op.get_bind().scalar(
        sa.text(f"select exists (select 1 from public.{CORRECTION_TABLE})")
    ):
        raise RuntimeError(
            "retained extraction-error reports cannot be represented by the "
            "supported predecessor"
        )
    op.execute(
        f"do $$ begin "
        f"if exists (select 1 from pg_tables where schemaname = 'public' "
        f"and tablename = '{CORRECTION_TABLE}') then "
        f"revoke select on public.{CORRECTION_TABLE} from {RUNTIME_LOGINS}; "
        f"end if; end $$;"
    )
    op.execute(CAPTURE_CORRECTION_SCHEMA_DOWN)
