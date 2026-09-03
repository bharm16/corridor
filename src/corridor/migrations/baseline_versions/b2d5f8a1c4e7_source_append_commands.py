"""Source appends become commands the source-append role owns (#492).

Revision ID: b2d5f8a1c4e7
Revises: a1c4e7b0d2f3

The baseline created ``corridor_source_append`` and gave it nothing to own:
the runtime capabilities still held raw ``INSERT`` on ``source_segments``,
``facts``, and the Extracted Proposal tables, so the application could write a
segment whose digest did not match its words, a Fact whose source lay in
another project, or a proposal grouping Facts from another run, and the
database would accept every one of them.

This revision moves those appends behind four narrow ``SECURITY DEFINER``
commands.  Each enforces what the table constraints cannot: project scope of
every typed reference, the digest of every exact text it stores or cites,
locator identity, and idempotent replay.  The runtime capabilities keep
``SELECT`` on the source tables, lose every write on them, and receive
``EXECUTE`` on the commands alone.  The same test that holds the accepted
boundary (``tests/test_database_authority.py``) proves the append boundary
against the real logins.

The legacy ``candidates`` table stays where it is: it is the frozen legacy
Extracted Proposal representation (ADR-0081), not the spine.

The same transition also carries the Support Assessment relation (#530,
ADR-0082), folded in here because the migration window holds one unreleased
transition and this is it.  ``support_assessments`` binds one typed
proposition, a Source Fact or an Extracted Proposal, to one or more Source
Segments of the same project and rendition through
``support_assessment_sources``, and records the evidence role, the assessment,
and exactly one authority: a human principal, or a released policy with its
ruleset identity.  The rows are append-only; a correction is a new row that
supersedes its predecessor once, so current and as-of readings walk the
history instead of overwriting it.  The fifth command,
``append_support_assessment``, is the only writer: it proves the proposition
and every segment lie in the project and rendition, derives the content
digest so a replay returns the row it already wrote, and lets a competing
writer either converge on the same row or be refused, never duplicate an
effective assessment.  A guard trigger refuses any write that does not arrive
as the source-append role, so not even the schema owner can insert one raw.

Proposed Delta and accepted-field propositions do not exist yet (#518, #509).
When they do, they join ``ck_support_assessments_proposition`` as a new kind
with their own typed column and composite foreign key; the relation never
accepts an unchecked object-type/object-id pair.  Locator validity (#493) is
a separate mechanical fact this relation never reads.

The same transition also carries the baseline/delta operating mode (#520,
ADR-0076 as amended by ADR-0083 and ADR-0084), folded in here for the same
reason: the window holds one unreleased transition.  A project's mode is
derived from ``project_baseline_adoptions``, one immutable receipt written
only by ``adopt_project_baseline``, so the move from legacy to adopted
baseline happens once and cannot be reversed by an update.  Triggers on
``dependencies`` and ``dependency_events`` then refuse the legacy paths'
accepted-value writes for an adopted project, and
``include_structured_cell_fact_decision`` — the one legacy writer that is
itself a SQL function — is re-issued with the mode check as its first act.
The protection is therefore in the database, not in the Python that happens
to call it.

The same transition finally carries Adopt Baseline itself (#509), for the same
window reason.  ``project_baseline_sources`` holds the accepted data-baseline
identity, ``project_baseline_source_rows`` holds source-row identity separately
from Project Record subject identity, and ``project_baseline_formats`` holds the
output-template and field-mapping identities, which a later act may replace on
their own without re-adopting anything or changing an accepted value.
``adopt_project_record_baseline`` writes all three, one Project Record revision,
and one separately identified ``fact_decisions`` row per adopted Source Fact, in
one command, so a half-adopted project is not a state a caller can construct.
"""

from __future__ import annotations

from alembic import op


revision = "b2d5f8a1c4e7"
down_revision = "a1c4e7b0d2f3"
branch_labels = None
depends_on = None


SOURCE_APPEND_ROLE = "corridor_source_append"
RUNTIME_LOGINS = "corridor_web, corridor_worker"

# Tables the application may now append to only through a command.
SOURCE_TABLES = (
    "source_segments",
    "facts",
    "fact_sources",
    "fact_applies_to",
    "fact_closure_results",
    "fact_closure_sources",
    "fact_statement_timings",
    "extracted_proposals",
    "extracted_proposal_facts",
    "source_fact_append_receipts",
)

# The Support Assessment relation (#530): created here, appended only through
# its command, readable by the runtime capabilities.
SUPPORT_TABLES = (
    "support_assessments",
    "support_assessment_sources",
)

# The Proposed Delta relation (#518): created here, appended only through
# its command, readable by the runtime capabilities.
DELTA_TABLES = (
    "delta_groups",
    "proposed_deltas",
    "delta_dispositions",
    "delta_supersessions",
    "delta_deferrals",
)

# Tables the commands read to prove a typed reference lies in scope.
REFERENCED_TABLES = (
    "projects",
    "documents",
    "dependency_events",
    "dependencies",
    "candidates",
    "extraction_runs",
)

COMMANDS = {
    "append_source_segments": "(bigint, bigint, bigint, jsonb)",
    "append_fact": (
        "(bigint, bigint, bigint, character varying, character varying, text, "
        "text, date, bigint, bigint, character varying, character varying, "
        "character varying, jsonb, jsonb)"
    ),
    "append_extracted_proposal": (
        "(bigint, bigint, bigint, bigint, character varying, text, jsonb, bigint[])"
    ),
    "append_source_fact_receipt": (
        "(bigint, bigint, bigint, character varying, character varying)"
    ),
    "append_support_assessment": (
        "(bigint, character varying, bigint, bigint, bigint[], character varying, "
        "character varying, character varying, character varying, "
        "character varying, timestamp with time zone, bigint)"
    ),
    "append_proposed_deltas": (
        "(bigint, character varying, character varying, bigint, bigint, jsonb)"
    ),
}

EVIDENCE_ROLES_SQL = "'value_support', 'attribution', 'timing', 'scope', 'context'"
ASSESSMENTS_SQL = (
    "'supported', 'partially_supported', 'contradicted', 'unclear', 'not_assessed'"
)


APPEND_SOURCE_SEGMENTS = """
create function public.append_source_segments(
    p_project_id bigint,
    p_document_id bigint,
    p_statement_id bigint,
    p_segments jsonb
) returns bigint[]
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            item jsonb;
            segment_kind text;
            exact text;
            digest text;
            existing record;
            appended bigint[] := '{}';
            segment_id bigint;
        begin
            if (p_document_id is null) = (p_statement_id is null) then
                raise exception 'source segments belong to one document or one statement'
                    using errcode = '23514';
            end if;
            if p_document_id is not null and not exists (
                select 1 from documents
                 where id = p_document_id and project_id = p_project_id
            ) then
                raise exception 'source segment document is outside its project'
                    using errcode = '23514';
            end if;
            if p_statement_id is not null and not exists (
                select 1 from dependency_events
                 where id = p_statement_id and project_id = p_project_id
            ) then
                raise exception 'source segment statement is outside its project'
                    using errcode = '23514';
            end if;
            if p_segments is null or jsonb_typeof(p_segments) <> 'array' then
                raise exception 'source segments must be a list'
                    using errcode = '23514';
            end if;
            for item in select value from jsonb_array_elements(p_segments) loop
                segment_kind := item ->> 'kind';
                exact := item ->> 'exact_text';
                digest := item ->> 'content_sha256';
                if exact is null or length(exact) = 0 then
                    raise exception 'source segment needs exact text'
                        using errcode = '23514';
                end if;
                if digest is distinct from
                    encode(sha256(convert_to(exact, 'UTF8')), 'hex') then
                    raise exception 'source segment digest does not match its exact text'
                        using errcode = '23514';
                end if;
                if segment_kind = 'spreadsheet_cell' then
                    select id, content_sha256, ordinal into existing
                      from source_segments
                     where document_id = p_document_id
                       and kind = 'spreadsheet_cell'
                       and sheet_name = item ->> 'sheet_name'
                       and cell_range = item ->> 'cell_range';
                elsif segment_kind = 'prose_span' then
                    select id, content_sha256, ordinal into existing
                      from source_segments
                     where document_id = p_document_id
                       and kind = 'prose_span'
                       and page_no = (item ->> 'page_no')::integer
                       and start_offset = (item ->> 'start_offset')::integer
                       and end_offset = (item ->> 'end_offset')::integer;
                elsif segment_kind = 'recorded_verbal_statement' then
                    select id, content_sha256, ordinal into existing
                      from source_segments
                     where kind = 'recorded_verbal_statement'
                       and statement_id = p_statement_id;
                else
                    raise exception 'unrecognized source segment kind'
                        using errcode = '23514';
                end if;
                if found then
                    if existing.content_sha256 <> digest
                        or existing.ordinal <> (item ->> 'ordinal')::integer then
                        raise exception 'source segment locator is already bound to different content'
                            using errcode = '23514';
                    end if;
                    appended := appended || existing.id;
                    continue;
                end if;
                insert into source_segments (
                    project_id, document_id, statement_id, kind, exact_text,
                    content_sha256, ordinal, sheet_name, cell_range, page_no,
                    start_offset, end_offset
                ) values (
                    p_project_id, p_document_id, p_statement_id, segment_kind, exact,
                    digest, (item ->> 'ordinal')::integer, item ->> 'sheet_name',
                    item ->> 'cell_range', (item ->> 'page_no')::integer,
                    (item ->> 'start_offset')::integer, (item ->> 'end_offset')::integer
                ) returning id into segment_id;
                appended := appended || segment_id;
            end loop;
            return appended;
        end; $$;
"""


APPEND_FACT = """
create function public.append_fact(
    p_project_id bigint,
    p_document_id bigint,
    p_extraction_run_id bigint,
    p_fact_type character varying,
    p_subject_kind character varying,
    p_subject_key text,
    p_text_value text,
    p_date_value date,
    p_external_org_value_id bigint,
    p_document_value_id bigint,
    p_transformation character varying,
    p_recorded_by character varying,
    p_content_sha256 character varying,
    p_sources jsonb,
    p_satellites jsonb
) returns bigint
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            existing record;
            new_id bigint;
            link jsonb;
            segment record;
            member text;
            timing jsonb;
            slot integer;
        begin
            if p_content_sha256 is null or p_content_sha256 !~ '^[0-9a-f]{64}$' then
                raise exception 'appended Fact needs its content digest'
                    using errcode = '23514';
            end if;
            if p_document_id is not null and not exists (
                select 1 from documents
                 where id = p_document_id and project_id = p_project_id
            ) then
                raise exception 'Fact document is outside its project'
                    using errcode = '23514';
            end if;
            if p_extraction_run_id is not null and not exists (
                select 1 from extraction_runs
                 where id = p_extraction_run_id and document_id = p_document_id
            ) then
                raise exception 'Fact extraction run belongs to another document'
                    using errcode = '23514';
            end if;
            if p_document_value_id is not null and not exists (
                select 1 from documents
                 where id = p_document_value_id and project_id = p_project_id
            ) then
                raise exception 'Fact document value is outside its project'
                    using errcode = '23514';
            end if;
            if p_sources is null or jsonb_typeof(p_sources) <> 'array' then
                raise exception 'Fact sources must be a list'
                    using errcode = '23514';
            end if;
            if p_satellites is not null and jsonb_typeof(p_satellites) <> 'object' then
                raise exception 'Fact satellites must be an object'
                    using errcode = '23514';
            end if;

            -- Replay: the digest covers every value, reference, and source
            -- link, so an identical digest is the same Fact appended again.
            select id, project_id into existing from facts
             where content_sha256 = p_content_sha256;
            if found then
                if existing.project_id <> p_project_id then
                    raise exception 'Fact digest is already bound in another project'
                        using errcode = '23514';
                end if;
                return existing.id;
            end if;

            insert into facts (
                project_id, document_id, extraction_run_id, fact_type,
                subject_kind, subject_key, text_value, date_value,
                date_range_start, date_range_end, external_org_value_id,
                document_value_id, transformation, recorded_by, content_sha256
            ) values (
                p_project_id, p_document_id, p_extraction_run_id, p_fact_type,
                p_subject_kind, p_subject_key, p_text_value, p_date_value,
                null, null, p_external_org_value_id,
                p_document_value_id, p_transformation, p_recorded_by, p_content_sha256
            ) returning id into new_id;

            for link in select value from jsonb_array_elements(p_sources) loop
                select id, project_id, document_id, exact_text, content_sha256
                  into segment from source_segments
                 where id = (link ->> 'source_segment_id')::bigint;
                if not found or segment.project_id <> p_project_id then
                    raise exception 'Fact source segment is outside its project'
                        using errcode = '23514';
                end if;
                if p_document_id is not null
                    and segment.document_id is distinct from p_document_id then
                    raise exception 'Fact source belongs to another rendition'
                        using errcode = '23514';
                end if;
                if encode(sha256(convert_to(segment.exact_text, 'UTF8')), 'hex')
                    <> segment.content_sha256 then
                    raise exception 'Fact source segment digest does not match its text'
                        using errcode = '23514';
                end if;
                insert into fact_sources (
                    project_id, document_id, fact_id, source_segment_id, role, ordinal
                ) values (
                    p_project_id, p_document_id, new_id, segment.id,
                    link ->> 'role', coalesce((link ->> 'ordinal')::integer, 1)
                );
            end loop;

            if p_satellites ? 'applies_to' then
                slot := 0;
                for member in
                    select value from jsonb_array_elements_text(p_satellites -> 'applies_to')
                loop
                    slot := slot + 1;
                    if not exists (
                        select 1 from dependencies
                         where id = member::bigint and project_id = p_project_id
                    ) then
                        raise exception 'Applies To member is outside its project'
                            using errcode = '23514';
                    end if;
                    insert into fact_applies_to (
                        project_id, fact_id, dependency_id, ordinal
                    ) values (p_project_id, new_id, member::bigint, slot);
                end loop;
            end if;

            if p_satellites ? 'closure' then
                if (p_satellites -> 'closure' ->> 'successor_dependency_id') is not null
                    and not exists (
                        select 1 from dependencies
                         where id = (p_satellites -> 'closure' ->> 'successor_dependency_id')::bigint
                           and project_id = p_project_id
                    ) then
                    raise exception 'closure successor is outside its project'
                        using errcode = '23514';
                end if;
                insert into fact_closure_results (
                    project_id, fact_id, closure_kind, successor_dependency_id
                ) values (
                    p_project_id, new_id,
                    p_satellites -> 'closure' ->> 'closure_kind',
                    (p_satellites -> 'closure' ->> 'successor_dependency_id')::bigint
                );
                slot := 0;
                for member in
                    select value from jsonb_array_elements_text(
                        p_satellites -> 'closure' -> 'governing_source_segment_ids'
                    )
                loop
                    slot := slot + 1;
                    select id, project_id, document_id into segment
                      from source_segments where id = member::bigint;
                    if not found or segment.project_id <> p_project_id
                        or segment.document_id is distinct from p_document_id then
                        raise exception 'closure governing source crosses rendition'
                            using errcode = '23514';
                    end if;
                    insert into fact_closure_sources (
                        project_id, document_id, fact_id, source_segment_id, ordinal
                    ) values (p_project_id, p_document_id, new_id, segment.id, slot);
                end loop;
            end if;

            if p_satellites ? 'timings' then
                for timing in
                    select value from jsonb_array_elements(p_satellites -> 'timings')
                loop
                    insert into fact_statement_timings (
                        project_id, fact_id, timing_role, text, precision,
                        start_date, end_date
                    ) values (
                        p_project_id, new_id, timing ->> 'role', timing ->> 'text',
                        timing ->> 'precision', (timing ->> 'start_date')::date,
                        (timing ->> 'end_date')::date
                    );
                end loop;
            end if;

            return new_id;
        end; $$;
"""


APPEND_EXTRACTED_PROPOSAL = """
create function public.append_extracted_proposal(
    p_project_id bigint,
    p_document_id bigint,
    p_extraction_run_id bigint,
    p_candidate_id bigint,
    p_kind character varying,
    p_subject_key text,
    p_candidate_metadata jsonb,
    p_fact_ids bigint[]
) returns bigint
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            existing record;
            new_id bigint;
            member bigint;
            fact record;
            slot integer := 0;
        begin
            if not exists (
                select 1 from documents
                 where id = p_document_id and project_id = p_project_id
            ) then
                raise exception 'Extracted Proposal document is outside its project'
                    using errcode = '23514';
            end if;
            if not exists (
                select 1 from extraction_runs
                 where id = p_extraction_run_id and document_id = p_document_id
            ) then
                raise exception 'Extracted Proposal run belongs to another document'
                    using errcode = '23514';
            end if;
            if not exists (
                select 1 from candidates
                 where id = p_candidate_id and project_id = p_project_id
                   and source_document_id = p_document_id
            ) then
                raise exception 'Extracted Proposal Candidate is outside its rendition'
                    using errcode = '23514';
            end if;
            if p_fact_ids is null or cardinality(p_fact_ids) = 0 then
                raise exception 'Extracted Proposal needs at least one Fact'
                    using errcode = '23514';
            end if;
            select id, candidate_id into existing from extracted_proposals
             where extraction_run_id = p_extraction_run_id
               and subject_key = p_subject_key;
            if found then
                if existing.candidate_id <> p_candidate_id then
                    raise exception 'Extracted Proposal subject is already bound to another Candidate'
                        using errcode = '23514';
                end if;
                return existing.id;
            end if;
            insert into extracted_proposals (
                project_id, document_id, extraction_run_id, candidate_id, kind,
                subject_key, candidate_metadata_json
            ) values (
                p_project_id, p_document_id, p_extraction_run_id, p_candidate_id,
                p_kind, p_subject_key, p_candidate_metadata
            ) returning id into new_id;
            foreach member in array p_fact_ids loop
                slot := slot + 1;
                select project_id, document_id, extraction_run_id, subject_key
                  into fact from facts where id = member;
                if not found or fact.project_id <> p_project_id
                    or fact.document_id is distinct from p_document_id
                    or fact.extraction_run_id is distinct from p_extraction_run_id then
                    raise exception 'Extracted Proposal Fact belongs to another run'
                        using errcode = '23514';
                end if;
                if fact.subject_key <> p_subject_key then
                    raise exception 'Extracted Proposal Fact names another subject'
                        using errcode = '23514';
                end if;
                insert into extracted_proposal_facts (
                    project_id, document_id, extraction_run_id, proposal_id,
                    fact_id, ordinal
                ) values (
                    p_project_id, p_document_id, p_extraction_run_id, new_id,
                    member, slot
                );
            end loop;
            return new_id;
        end; $$;
"""


APPEND_SOURCE_FACT_RECEIPT = """
create function public.append_source_fact_receipt(
    p_project_id bigint,
    p_document_id bigint,
    p_extraction_run_id bigint,
    p_idempotency_key character varying,
    p_content_sha256 character varying
) returns bigint
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            existing record;
            new_id bigint;
        begin
            if not exists (
                select 1 from documents
                 where id = p_document_id and project_id = p_project_id
            ) then
                raise exception 'source Fact append receipt document is outside its project'
                    using errcode = '23514';
            end if;
            if not exists (
                select 1 from extraction_runs
                 where id = p_extraction_run_id and document_id = p_document_id
            ) then
                raise exception 'source Fact append receipt run belongs to another document'
                    using errcode = '23514';
            end if;
            select id, content_sha256 into existing from source_fact_append_receipts
             where project_id = p_project_id and idempotency_key = p_idempotency_key;
            if found then
                if existing.content_sha256 <> p_content_sha256 then
                    raise exception 'source Fact append key is already bound to different content'
                        using errcode = '23514';
                end if;
                return existing.id;
            end if;
            insert into source_fact_append_receipts (
                project_id, document_id, extraction_run_id, idempotency_key,
                content_sha256
            ) values (
                p_project_id, p_document_id, p_extraction_run_id, p_idempotency_key,
                p_content_sha256
            ) returning id into new_id;
            return new_id;
        end; $$;
"""


# --- The Proposed Delta relation (#518, ADR-0075, ADR-0083) -----------------

PROPOSED_DELTA_SCHEMA = """
create table public.delta_groups (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    source_family character varying(64) not null,
    source_revision character varying(128) not null,
    document_id bigint references public.documents (id),
    statement_id bigint references public.dependency_events (id),
    created_at timestamp with time zone not null default now(),
    constraint uq_delta_groups_project_id unique (project_id, id)
);
create index ix_delta_groups_project_id on public.delta_groups (project_id);

create table public.proposed_deltas (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    group_id bigint not null,
    content_sha256 character varying(64) not null,
    change_type character varying(32) not null,
    target_type character varying(32) not null,
    target_subject_identity character varying(128) not null,
    target_field character varying(64),
    accepted_value jsonb,
    proposed_value jsonb,
    source_family character varying(64) not null,
    source_revision character varying(128) not null,
    comparison_rule_version character varying(64) not null,
    accepted_baseline_revision character varying(128),
    created_at timestamp with time zone not null default now(),
    constraint uq_proposed_deltas_project_id unique (project_id, id),
    constraint uq_proposed_deltas_content unique (content_sha256),
    constraint fk_proposed_deltas_group
        foreign key (project_id, group_id)
        references public.delta_groups (project_id, id),
    constraint ck_proposed_deltas_change_type check (
        change_type in ('add', 'modify', 'apparent_removal')
    ),
    constraint ck_proposed_deltas_target_type check (
        target_type in ('existing_subject', 'proposed_subject')
    ),
    constraint ck_proposed_deltas_content_sha256 check (
        content_sha256 ~ '^[0-9a-f]{64}$'
    )
);
create index ix_proposed_deltas_project_id on public.proposed_deltas (project_id);
create index ix_proposed_deltas_group_id on public.proposed_deltas (group_id);
create index ix_proposed_deltas_target on public.proposed_deltas (project_id, target_subject_identity);

create table public.delta_dispositions (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    delta_id bigint not null,
    disposition character varying(32) not null,
    decided_at timestamp with time zone not null,
    decided_by_principal character varying(128),
    decided_by_policy character varying(128),
    rationale text,
    effective_value jsonb,
    recorded_at timestamp with time zone not null default now(),
    constraint uq_delta_dispositions_delta unique (delta_id),
    constraint fk_delta_dispositions_delta
        foreign key (project_id, delta_id)
        references public.proposed_deltas (project_id, id),
    constraint ck_delta_dispositions_disposition check (
        disposition in ('accept', 'edit', 'reject')
    ),
    constraint ck_delta_dispositions_authority_xor check (
        (decided_by_principal is null) <> (decided_by_policy is null)
    )
);
create index ix_delta_dispositions_delta_id on public.delta_dispositions (delta_id);

create table public.delta_supersessions (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    prior_delta_id bigint not null,
    superseding_delta_id bigint not null,
    reason character varying(64) not null,
    superseded_at timestamp with time zone not null default now(),
    constraint uq_delta_supersessions_prior unique (prior_delta_id),
    constraint fk_delta_supersessions_prior
        foreign key (project_id, prior_delta_id)
        references public.proposed_deltas (project_id, id),
    constraint fk_delta_supersessions_superseding
        foreign key (project_id, superseding_delta_id)
        references public.proposed_deltas (project_id, id),
    constraint ck_delta_supersessions_not_self check (prior_delta_id <> superseding_delta_id)
);
create index ix_delta_supersessions_prior on public.delta_supersessions (prior_delta_id);
create index ix_delta_supersessions_superseding on public.delta_supersessions (superseding_delta_id);

create table public.delta_deferrals (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    delta_id bigint not null,
    deferred_at timestamp with time zone not null,
    deferred_until timestamp with time zone,
    wake_condition character varying(128),
    scheduled_by_principal character varying(128) not null,
    reason text,
    recorded_at timestamp with time zone not null default now(),
    constraint fk_delta_deferrals_delta
        foreign key (project_id, delta_id)
        references public.proposed_deltas (project_id, id)
);
create index ix_delta_deferrals_delta on public.delta_deferrals (delta_id);
"""

PROPOSED_DELTA_SCHEMA_DOWN = """
drop table if exists public.delta_deferrals cascade;
drop table if exists public.delta_supersessions cascade;
drop table if exists public.delta_dispositions cascade;
drop table if exists public.proposed_deltas cascade;
drop table if exists public.delta_groups cascade;
"""

# --- The Support Assessment relation (#530, ADR-0082) ----------------------

SUPPORT_ASSESSMENT_SCHEMA = f"""
alter table public.extracted_proposals
    add constraint uq_extracted_proposals_project_id unique (project_id, id);
alter table public.extracted_proposals
    add constraint uq_extracted_proposals_document_scope_id
    unique (project_id, document_id, id);

create table public.support_assessments (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    -- The proposition's rendition, copied so the composite keys below can
    -- hold every segment to the same document; null only for a
    -- document-less verbal Fact, where the project-scoped keys still hold.
    document_id bigint,
    proposition_kind character varying(32) not null,
    fact_id bigint,
    extracted_proposal_id bigint,
    proposed_delta_id bigint,
    evidence_role character varying(32) not null,
    assessment character varying(32) not null,
    human_principal character varying(128),
    released_policy character varying(128),
    ruleset_version character varying(64),
    superseded_by bigint references public.support_assessments (id)
        deferrable initially deferred,
    content_sha256 character varying(64) not null,
    assessed_at timestamp with time zone not null,
    recorded_at timestamp with time zone not null default now(),
    constraint uq_support_assessments_project_id unique (project_id, id),
    constraint uq_support_assessments_scope_id unique (project_id, document_id, id),
    constraint uq_support_assessments_content unique (content_sha256),
    constraint uq_support_assessments_superseded_by unique (superseded_by),
    constraint fk_support_assessments_fact_scope
        foreign key (project_id, document_id, fact_id)
        references public.facts (project_id, document_id, id),
    constraint fk_support_assessments_fact_project
        foreign key (project_id, fact_id)
        references public.facts (project_id, id),
    constraint fk_support_assessments_proposal_scope
        foreign key (project_id, document_id, extracted_proposal_id)
        references public.extracted_proposals (project_id, document_id, id),
    constraint fk_support_assessments_proposal_project
        foreign key (project_id, extracted_proposal_id)
        references public.extracted_proposals (project_id, id),
    constraint fk_support_assessments_delta_project
        foreign key (project_id, proposed_delta_id)
        references public.proposed_deltas (project_id, id),
    -- Exactly one typed proposition. A new proposition class (Proposed
    -- Delta, accepted field) adds a kind, a column, and a composite key
    -- here; it never becomes an unchecked object-type/object-id pair.
    constraint ck_support_assessments_proposition check (
        (proposition_kind = 'source_fact'
         and fact_id is not null and extracted_proposal_id is null and proposed_delta_id is null)
        or (proposition_kind = 'extracted_proposal'
         and extracted_proposal_id is not null and fact_id is null and proposed_delta_id is null
         and document_id is not null)
        or (proposition_kind = 'proposed_delta'
         and proposed_delta_id is not null and fact_id is null and extracted_proposal_id is null)
    ),
    constraint ck_support_assessments_evidence_role check (
        evidence_role in ({EVIDENCE_ROLES_SQL})
    ),
    constraint ck_support_assessments_assessment check (
        assessment in ({ASSESSMENTS_SQL})
    ),
    constraint ck_support_assessments_authority_xor check (
        (human_principal is null) <> (released_policy is null)
    ),
    constraint ck_support_assessments_human_principal check (
        human_principal is null or length(trim(human_principal)) > 0
    ),
    constraint ck_support_assessments_released_policy check (
        released_policy is null or length(trim(released_policy)) > 0
    ),
    constraint ck_support_assessments_ruleset check (
        (released_policy is null) = (ruleset_version is null)
        and (ruleset_version is null or length(trim(ruleset_version)) > 0)
    ),
    constraint ck_support_assessments_content_sha256 check (
        content_sha256 ~ '^[0-9a-f]{{64}}$'
    ),
    constraint ck_support_assessments_not_self check (superseded_by <> id)
);
create index ix_support_assessments_project_id
    on public.support_assessments (project_id);
create index ix_support_assessments_fact_id
    on public.support_assessments (fact_id);
create index ix_support_assessments_extracted_proposal_id
    on public.support_assessments (extracted_proposal_id);
create index ix_support_assessments_proposed_delta_id
    on public.support_assessments (proposed_delta_id);
-- One EFFECTIVE assessment per proposition and role; a correction
-- supersedes it rather than sitting beside it.
create unique index uq_support_assessments_effective_fact
    on public.support_assessments (fact_id, evidence_role)
    where superseded_by is null and fact_id is not null;
create unique index uq_support_assessments_effective_proposal
    on public.support_assessments (extracted_proposal_id, evidence_role)
    where superseded_by is null and extracted_proposal_id is not null;
create unique index uq_support_assessments_effective_delta
    on public.support_assessments (proposed_delta_id, evidence_role)
    where superseded_by is null and proposed_delta_id is not null;

create table public.support_assessment_sources (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    document_id bigint,
    support_assessment_id bigint not null,
    source_segment_id bigint not null,
    ordinal integer not null,
    constraint uq_support_assessment_sources_segment
        unique (support_assessment_id, source_segment_id),
    constraint uq_support_assessment_sources_ordinal
        unique (support_assessment_id, ordinal),
    constraint fk_support_assessment_sources_assessment_scope
        foreign key (project_id, document_id, support_assessment_id)
        references public.support_assessments (project_id, document_id, id),
    constraint fk_support_assessment_sources_assessment_project
        foreign key (project_id, support_assessment_id)
        references public.support_assessments (project_id, id),
    constraint fk_support_assessment_sources_segment_scope
        foreign key (project_id, document_id, source_segment_id)
        references public.source_segments (project_id, document_id, id),
    constraint fk_support_assessment_sources_segment_project
        foreign key (project_id, source_segment_id)
        references public.source_segments (project_id, id),
    constraint ck_support_assessment_sources_ordinal check (ordinal > 0)
);
create index ix_support_assessment_sources_project_id
    on public.support_assessment_sources (project_id);
create index ix_support_assessment_sources_assessment_id
    on public.support_assessment_sources (support_assessment_id);
create index ix_support_assessment_sources_segment_id
    on public.support_assessment_sources (source_segment_id);

-- Not even the schema owner writes these raw: every row arrives through the
-- command, and the only change a row ever sees is becoming superseded once.
create function public.enforce_support_assessment_write() returns trigger
    language plpgsql
    as $$
        begin
            if current_user <> 'corridor_source_append' then
                raise exception 'Support Assessment requires the append command'
                    using errcode = '23514';
            end if;
            if tg_op = 'DELETE' or tg_op = 'TRUNCATE' then
                raise exception 'Support Assessments are append-only'
                    using errcode = '23514';
            end if;
            if tg_op = 'UPDATE' then
                if new.id is distinct from old.id
                   or new.project_id is distinct from old.project_id
                   or new.document_id is distinct from old.document_id
                   or new.proposition_kind is distinct from old.proposition_kind
                   or new.fact_id is distinct from old.fact_id
                   or new.extracted_proposal_id is distinct from old.extracted_proposal_id
                   or new.proposed_delta_id is distinct from old.proposed_delta_id
                   or new.evidence_role is distinct from old.evidence_role
                   or new.assessment is distinct from old.assessment
                   or new.human_principal is distinct from old.human_principal
                   or new.released_policy is distinct from old.released_policy
                   or new.ruleset_version is distinct from old.ruleset_version
                   or new.content_sha256 is distinct from old.content_sha256
                   or new.assessed_at is distinct from old.assessed_at
                   or new.recorded_at is distinct from old.recorded_at
                   or old.superseded_by is not null
                   or new.superseded_by is null then
                    raise exception 'Support Assessment may only become superseded once'
                        using errcode = '23514';
                end if;
            end if;
            return new;
        end; $$;
create function public.enforce_support_assessment_sources_write() returns trigger
    language plpgsql
    as $$
        begin
            if current_user <> 'corridor_source_append' then
                raise exception 'Support Assessment requires the append command'
                    using errcode = '23514';
            end if;
            if tg_op <> 'INSERT' then
                raise exception 'Support Assessment sources are append-only'
                    using errcode = '23514';
            end if;
            return new;
        end; $$;
create trigger trg_support_assessments_guard
    before insert or update or delete on public.support_assessments
    for each row execute function public.enforce_support_assessment_write();
create trigger trg_support_assessments_no_truncate
    before truncate on public.support_assessments
    for each statement execute function public.enforce_support_assessment_write();
create trigger trg_support_assessment_sources_guard
    before insert or update or delete on public.support_assessment_sources
    for each row execute function public.enforce_support_assessment_sources_write();
create trigger trg_support_assessment_sources_no_truncate
    before truncate on public.support_assessment_sources
    for each statement execute function public.enforce_support_assessment_sources_write();
"""

SUPPORT_ASSESSMENT_SCHEMA_DOWN = """
drop trigger if exists trg_support_assessment_sources_no_truncate on public.support_assessment_sources;
drop trigger if exists trg_support_assessment_sources_guard on public.support_assessment_sources;
drop trigger if exists trg_support_assessments_no_truncate on public.support_assessments;
drop trigger if exists trg_support_assessments_guard on public.support_assessments;
drop function if exists public.enforce_support_assessment_sources_write();
drop function if exists public.enforce_support_assessment_write();
drop table if exists public.support_assessment_sources cascade;
drop table if exists public.support_assessments cascade;
alter table if exists public.extracted_proposals
    drop constraint if exists uq_extracted_proposals_document_scope_id;
alter table if exists public.extracted_proposals
    drop constraint if exists uq_extracted_proposals_project_id;
"""


APPEND_SUPPORT_ASSESSMENT = f"""
create function public.append_support_assessment(
    p_project_id bigint,
    p_proposition_kind character varying,
    p_fact_id bigint,
    p_extracted_proposal_id bigint,
    p_source_segment_ids bigint[],
    p_evidence_role character varying,
    p_assessment character varying,
    p_human_principal character varying,
    p_released_policy character varying,
    p_ruleset_version character varying,
    p_assessed_at timestamp with time zone,
    p_supersedes_id bigint
) returns bigint
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            proposition record;
            predecessor record;
            segment record;
            existing_id bigint;
            new_id bigint;
            member bigint;
            slot integer := 0;
            digest text;
            decided timestamp with time zone;
        begin
            -- Exactly one typed proposition, in this project.
            if p_proposition_kind = 'source_fact' then
                if p_fact_id is null or p_extracted_proposal_id is not null then
                    raise exception 'Support Assessment of a Source Fact names exactly one Fact'
                        using errcode = '23514';
                end if;
                select id, project_id, document_id into proposition
                  from facts where id = p_fact_id;
            elsif p_proposition_kind = 'extracted_proposal' then
                if p_extracted_proposal_id is null or p_fact_id is not null then
                    raise exception 'Support Assessment of an Extracted Proposal names exactly one proposal'
                        using errcode = '23514';
                end if;
                select id, project_id, document_id into proposition
                  from extracted_proposals where id = p_extracted_proposal_id;
            else
                raise exception 'unrecognized Support Assessment proposition kind'
                    using errcode = '23514';
            end if;
            if not found or proposition.project_id <> p_project_id then
                raise exception 'Support Assessment proposition is outside its project'
                    using errcode = '23514';
            end if;

            if p_evidence_role is null
                or p_evidence_role not in ({EVIDENCE_ROLES_SQL}) then
                raise exception 'unrecognized Support Assessment evidence role'
                    using errcode = '23514';
            end if;
            if p_assessment is null or p_assessment not in ({ASSESSMENTS_SQL}) then
                raise exception 'unrecognized Support Assessment assessment'
                    using errcode = '23514';
            end if;

            -- Exactly one authority: a human principal, or a released policy
            -- with the ruleset it applied.
            if (p_human_principal is null) = (p_released_policy is null) then
                raise exception 'Support Assessment authority is one human principal or one released policy'
                    using errcode = '23514';
            end if;
            if p_human_principal is not null and length(trim(p_human_principal)) = 0 then
                raise exception 'Support Assessment requires an attributed principal'
                    using errcode = '23514';
            end if;
            if p_released_policy is not null and (
                length(trim(p_released_policy)) = 0
                or p_ruleset_version is null
                or length(trim(p_ruleset_version)) = 0
            ) then
                raise exception 'Support Assessment by a released policy needs its ruleset identity'
                    using errcode = '23514';
            end if;
            if p_released_policy is null and p_ruleset_version is not null then
                raise exception 'Support Assessment ruleset identity belongs to a released policy'
                    using errcode = '23514';
            end if;

            -- One or more Source Segments, each in the project and the
            -- proposition's rendition, none named twice.
            if p_source_segment_ids is null or cardinality(p_source_segment_ids) = 0 then
                raise exception 'Support Assessment needs at least one Source Segment'
                    using errcode = '23514';
            end if;
            if (select count(distinct s) from unnest(p_source_segment_ids) as s)
                <> cardinality(p_source_segment_ids) then
                raise exception 'Support Assessment names a Source Segment twice'
                    using errcode = '23514';
            end if;
            foreach member in array p_source_segment_ids loop
                select id, project_id, document_id into segment
                  from source_segments where id = member;
                if not found or segment.project_id <> p_project_id then
                    raise exception 'Support Assessment source segment is outside its project'
                        using errcode = '23514';
                end if;
                if segment.document_id is distinct from proposition.document_id then
                    raise exception 'Support Assessment source belongs to another rendition'
                        using errcode = '23514';
                end if;
            end loop;

            -- The digest is derived here, from every value that makes this
            -- the same assessment, so a replay cannot forge or omit it.
            digest := encode(sha256(convert_to(concat_ws('|',
                'support_assessment_v1',
                p_project_id::text,
                p_proposition_kind,
                coalesce(p_fact_id::text, ''),
                coalesce(p_extracted_proposal_id::text, ''),
                p_evidence_role,
                p_assessment,
                coalesce(p_human_principal, ''),
                coalesce(p_released_policy, ''),
                coalesce(p_ruleset_version, ''),
                coalesce(p_supersedes_id::text, ''),
                array_to_string(p_source_segment_ids, ',')
            ), 'UTF8')), 'hex');
            select id into existing_id from support_assessments
             where content_sha256 = digest;
            if found then
                return existing_id;
            end if;

            decided := coalesce(p_assessed_at, now());
            new_id := nextval('support_assessments_id_seq');

            if p_supersedes_id is not null then
                select id, project_id, proposition_kind, fact_id,
                       extracted_proposal_id, evidence_role, superseded_by,
                       assessed_at
                  into predecessor
                  from support_assessments
                 where id = p_supersedes_id
                   for update;
                if not found
                    or predecessor.project_id <> p_project_id
                    or predecessor.proposition_kind <> p_proposition_kind
                    or predecessor.fact_id is distinct from p_fact_id
                    or predecessor.extracted_proposal_id is distinct from p_extracted_proposal_id
                    or predecessor.evidence_role <> p_evidence_role then
                    raise exception 'Support Assessment predecessor is not an assessment of the same proposition and role'
                        using errcode = '23514';
                end if;
                if predecessor.superseded_by is not null then
                    -- A competing writer got here first. If it recorded this
                    -- very assessment, converge on its row; otherwise the
                    -- predecessor this caller read is stale.
                    select id into existing_id from support_assessments
                     where content_sha256 = digest;
                    if found then
                        return existing_id;
                    end if;
                    raise exception 'Support Assessment predecessor is stale'
                        using errcode = '23514';
                end if;
                if decided < predecessor.assessed_at then
                    raise exception 'Support Assessment cannot precede the assessment it supersedes'
                        using errcode = '23514';
                end if;
                -- Retire the predecessor before its successor exists (the
                -- foreign key is deferred), so the effective index never
                -- sees both at once.
                update support_assessments set superseded_by = new_id
                 where id = p_supersedes_id;
            end if;

            begin
                insert into support_assessments (
                    id, project_id, document_id, proposition_kind, fact_id,
                    extracted_proposal_id, evidence_role, assessment,
                    human_principal, released_policy, ruleset_version,
                    content_sha256, assessed_at
                ) values (
                    new_id, p_project_id, proposition.document_id,
                    p_proposition_kind, p_fact_id, p_extracted_proposal_id,
                    p_evidence_role, p_assessment, p_human_principal,
                    p_released_policy, p_ruleset_version, digest, decided
                );
            exception when unique_violation then
                -- Two writers raced on the same effective slot. The same
                -- assessment converges on the row that won; a different one
                -- must read the winner and supersede it deliberately.
                select id into existing_id from support_assessments
                 where content_sha256 = digest;
                if found then
                    return existing_id;
                end if;
                raise exception 'an effective Support Assessment already exists for this proposition and role; supersede it'
                    using errcode = '23505';
            end;

            foreach member in array p_source_segment_ids loop
                slot := slot + 1;
                insert into support_assessment_sources (
                    project_id, document_id, support_assessment_id,
                    source_segment_id, ordinal
                ) values (
                    p_project_id, proposition.document_id, new_id, member, slot
                );
            end loop;
            return new_id;
        end; $$;
"""


APPEND_PROPOSED_DELTAS = """
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

            insert into delta_groups (
                project_id, source_family, source_revision, document_id, statement_id
            ) values (
                p_project_id, p_source_family, p_source_revision, p_document_id, p_statement_id
            ) returning id into v_group_id;

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

                select id into delta_id from proposed_deltas where content_sha256 = content_hash;
                if delta_id is null then
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
                    ) returning id into delta_id;
                end if;
                appended := array_append(appended, delta_id);
            end loop;

            return appended;
        end; $$;
"""


# --- Baseline/delta operating mode (#520, ADR-0076, ADR-0083, ADR-0084) ----
#
# The mode is not a column anybody can set.  It is derived from one immutable
# baseline-adoption receipt: a project with a receipt is in `adopted_baseline`
# mode, a project without one is `legacy`.  The receipt is written once, by one
# command the record-decision role owns, and can never be updated, deleted, or
# truncated, so the transition is one-way by construction rather than by
# convention.

OPERATING_MODE_ROLE = "corridor_fact_decision_writer"

# The legacy tables whose rows are accepted values: the Constraint Record that
# `run_dependency_admission` and the schedule update paths write, and the
# statements `run_event_admission` admits.  Work List scheduling, deferral, and
# packet presentation are deliberately absent: they are ADR-0035 human work,
# not accepted authority, and #524 governs them (ADR-0084).
OPERATING_MODE_GUARDED_TABLES = ("dependencies", "dependency_events")

BASELINE_ADOPTION_SCHEMA = """
create table public.project_baseline_adoptions (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    -- The revision Adopt Baseline writes (#509).  Nullable so this transition
    -- exists before the importer does and a test fixture can reach adopted
    -- mode without it; #509 passes the revision it created in the same
    -- transaction.
    revision_id bigint references public.project_record_revisions (id),
    adopted_by_principal character varying(128) not null,
    baseline_source_sha256 character varying(64) not null,
    importer_identity character varying(128) not null,
    importer_version character varying(64) not null,
    idempotency_key character varying(160) not null,
    adopted_at timestamp with time zone not null default now(),
    constraint uq_project_baseline_adoptions_project unique (project_id),
    constraint ck_project_baseline_adoptions_digest check (
        baseline_source_sha256 ~ '^[0-9a-f]{64}$'
    ),
    constraint ck_project_baseline_adoptions_principal check (
        length(btrim(adopted_by_principal)) > 0
    )
);
"""

BASELINE_ADOPTION_SCHEMA_DOWN = """
drop table if exists public.project_baseline_adoptions cascade;
"""

OPERATING_MODE_FUNCTIONS = """
create function public.project_operating_mode(p_project_id bigint) returns text
    language sql stable
    set search_path to 'public'
    as $$
        select case
            when exists (
                select 1 from public.project_baseline_adoptions
                 where project_id = p_project_id
            ) then 'adopted_baseline'
            else 'legacy'
        end;
    $$;

create function public.enforce_project_baseline_adoption_write() returns trigger
    language plpgsql
    as $$
        begin
            if tg_op <> 'INSERT' then
                raise exception 'Baseline adoption is immutable: the operating mode moves from legacy to adopted baseline once and never back'
                    using errcode='23514';
            end if;
            if current_user <> 'corridor_fact_decision_writer' then
                raise exception 'Baseline adoption requires the typed adoption command'
                    using errcode='23514';
            end if;
            return new;
        end; $$;

create trigger trg_project_baseline_adoptions_write
    before insert or update or delete on public.project_baseline_adoptions
    for each row execute function public.enforce_project_baseline_adoption_write();

create trigger trg_project_baseline_adoptions_truncate
    before truncate on public.project_baseline_adoptions
    for each statement execute function public.enforce_project_baseline_adoption_write();

create function public.refuse_legacy_accepted_write_for_adopted_project()
    returns trigger
    language plpgsql security definer
    set search_path to 'public'
    as $$
        begin
            if public.project_operating_mode(new.project_id) = 'adopted_baseline' then
                raise exception 'project % has an adopted baseline: the legacy % path may capture Source Facts and propose deltas, but may not write an accepted value',
                        new.project_id, tg_table_name
                    using errcode='23514';
            end if;
            return new;
        end; $$;
"""

OPERATING_MODE_FUNCTIONS_DOWN = """
drop function if exists public.refuse_legacy_accepted_write_for_adopted_project();
drop function if exists public.enforce_project_baseline_adoption_write() cascade;
drop function if exists public.project_operating_mode(bigint);
"""

ADOPT_PROJECT_BASELINE = """
create function public.adopt_project_baseline(
    p_project_id bigint,
    p_adopted_by_principal character varying,
    p_baseline_source_sha256 character varying,
    p_importer_identity character varying,
    p_importer_version character varying,
    p_idempotency_key character varying,
    p_revision_id bigint
) returns jsonb
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            existing project_baseline_adoptions%ROWTYPE;
            new_id bigint;
        begin
            if p_revision_id is not null and not exists (
                select 1 from project_record_revisions
                 where id = p_revision_id and project_id = p_project_id
            ) then
                raise exception 'the adoption revision belongs to another project'
                    using errcode='23514';
            end if;
            select * into existing from project_baseline_adoptions
             where project_id = p_project_id;
            if found then
                if existing.idempotency_key is distinct from p_idempotency_key
                   or existing.baseline_source_sha256
                       is distinct from p_baseline_source_sha256 then
                    raise exception 'project % already has an adopted baseline; the operating mode transition happens once', p_project_id
                        using errcode='23514';
                end if;
                return jsonb_build_object(
                    'adoption_id', existing.id,
                    'project_id', p_project_id,
                    'mode', 'adopted_baseline',
                    'created', false
                );
            end if;
            insert into project_baseline_adoptions (
                project_id, revision_id, adopted_by_principal,
                baseline_source_sha256, importer_identity, importer_version,
                idempotency_key
            ) values (
                p_project_id, p_revision_id, p_adopted_by_principal,
                p_baseline_source_sha256, p_importer_identity, p_importer_version,
                p_idempotency_key
            ) returning id into new_id;
            return jsonb_build_object(
                'adoption_id', new_id,
                'project_id', p_project_id,
                'mode', 'adopted_baseline',
                'created', true
            );
        end; $$;
"""

ADOPT_PROJECT_BASELINE_SIGNATURE = (
    "(bigint, character varying, character varying, character varying, "
    "character varying, character varying, bigint)"
)

# The one legacy accepted-value writer that really is a SQL function.  It is
# re-issued here with the mode check as its first act, so the refusal lives in
# the command itself and not only in the Python that calls it; the downgrade
# re-issues the released body unchanged.
STRUCTURED_CELL_INCLUSION_RELEASED = """\
CREATE OR REPLACE FUNCTION public.include_structured_cell_fact_decision(p_project_id bigint, p_fact_id bigint, p_subject_key text, p_fact_type character varying, p_idempotency_key character varying, p_policy character varying) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public'
    AS $$
        declare
            existing_revision bigint;
            existing_fact bigint;
            current_decision bigint;
            current_fact bigint;
            predecessor_revision bigint;
            new_revision bigint;
            new_decision bigint;
            eligible boolean;
        begin
            if p_policy <> 'structured-cell-record-inclusion-v1'
               or p_fact_type not in ('utility_id', 'external_org', 'external_org_contact', 'utility_type', 'utility_subtype', 'utility_function', 'operational_status', 'size', 'material', 'oh_ug', 'row_placement', 'orientation', 'baseline', 'station_from', 'station_to', 'offset_from', 'offset_to', 'sue_level', 'conflict_description', 'resolution_strategy', 'notes', 'alignment', 'location_start', 'location_end', 'offset_side', 'potential_conflict', 'data_source', 'marked_resolution', 'committed_date', 'action_due_date', 'need_date', 'applies_to', 'closure_result') then
                raise exception 'unrecognized structured-cell inclusion policy'
                    using errcode='23514';
            end if;
            select exists (
                select 1 from facts
                join active_extraction_runs
                  on active_extraction_runs.document_id = facts.document_id
                 and active_extraction_runs.extraction_run_id = facts.extraction_run_id
                join extracted_proposal_facts
                  on extracted_proposal_facts.fact_id = facts.id
                join extracted_proposals
                  on extracted_proposals.id = extracted_proposal_facts.proposal_id
                join candidates on candidates.id = extracted_proposals.candidate_id
                where facts.id = p_fact_id
                  and facts.project_id = p_project_id
                  and facts.subject_key = p_subject_key
                  and facts.fact_type = p_fact_type
                  and candidates.state in ('accepted', 'merged')
                  and candidates.merged_into is not null
                  and (
                      facts.fact_type <> 'external_org'
                      or facts.external_org_value_id is not null
                  )
                  and (
                      facts.fact_type <> 'applies_to'
                      or exists (
                          select 1 from fact_applies_to
                          where fact_applies_to.fact_id = facts.id
                      )
                  )
                  and (
                      facts.fact_type <> 'closure_result'
                      or (
                          exists (
                              select 1 from fact_closure_results
                              where fact_closure_results.fact_id = facts.id
                                and fact_closure_results.closure_kind =
                                    'source_marked_resolved'
                          )
                          and exists (
                              select 1 from fact_closure_sources
                              where fact_closure_sources.fact_id = facts.id
                          )
                      )
                  )
                  and exists (
                      select 1 from fact_sources
                      join source_segments
                        on source_segments.id = fact_sources.source_segment_id
                      where fact_sources.fact_id = facts.id
                        and fact_sources.role = 'value_source'
                        and source_segments.kind = 'spreadsheet_cell'
                  )
                  and not exists (
                      select 1 from fact_sources
                      join source_segments
                        on source_segments.id = fact_sources.source_segment_id
                      where fact_sources.fact_id = facts.id
                        and fact_sources.role = 'value_source'
                        and source_segments.kind <> 'spreadsheet_cell'
                  )
            ) into eligible;
            if not eligible then
                raise exception 'Fact is not eligible for released structured-cell inclusion'
                    using errcode='23514';
            end if;
            select id into existing_revision from project_record_revisions
             where project_id = p_project_id and idempotency_key = p_idempotency_key;
            if existing_revision is not null then
                select id, fact_id into current_decision, existing_fact from fact_decisions
                 where revision_id = existing_revision;
                if current_decision is null or existing_fact <> p_fact_id then
                    raise exception 'Record Inclusion key is bound to different content'
                        using errcode='23514';
                end if;
                return jsonb_build_object(
                    'revision_id', existing_revision,
                    'decision_id', current_decision,
                    'created', false
                );
            end if;
            select id, fact_id into current_decision, current_fact from fact_decisions
             where project_id = p_project_id and subject_key = p_subject_key
               and fact_type = p_fact_type and superseded_by is null;
            if current_decision is not null and current_fact = p_fact_id then
                select revision_id into existing_revision from fact_decisions
                 where id = current_decision;
                return jsonb_build_object(
                    'revision_id', existing_revision,
                    'decision_id', current_decision,
                    'created', false
                );
            end if;
            select max(id) into predecessor_revision from project_record_revisions
             where project_id = p_project_id;
            insert into project_record_revisions (
                project_id, predecessor_revision_id, command_type,
                human_principal, released_policy, idempotency_key
            ) values (
                p_project_id, predecessor_revision, 'include_structured_cell_fact',
                null, p_policy, p_idempotency_key
            ) returning id into new_revision;
            new_decision := nextval('fact_decisions_id_seq');
            if current_decision is not null then
                update fact_decisions set superseded_by = new_decision
                 where id = current_decision;
            end if;
            insert into fact_decisions (
                id, project_id, fact_id, subject_key, fact_type, revision_id, superseded_by
            ) values (
                new_decision, p_project_id, p_fact_id, p_subject_key,
                p_fact_type, new_revision, null
            );
            return jsonb_build_object(
                'revision_id', new_revision,
                'decision_id', new_decision,
                'created', true
            );
        end; $$;
"""

STRUCTURED_CELL_INCLUSION_MODE_CHECK = """\
            if project_operating_mode(p_project_id) = 'adopted_baseline' then
                raise exception 'project % has an adopted baseline: structured-cell inclusion captures Source Facts and proposes deltas, and may not replace an accepted value', p_project_id
                    using errcode='23514';
            end if;
"""

STRUCTURED_CELL_INCLUSION_MODE_GUARDED = (
    STRUCTURED_CELL_INCLUSION_RELEASED.replace(
        "\n        begin\n",
        "\n        begin\n" + STRUCTURED_CELL_INCLUSION_MODE_CHECK,
        1,
    )
)


# --- Adopt Baseline (#509, ADR-0076, ADR-0083) -----------------------------
#
# Adopt Baseline is the one bulk human act that turns the customer's own UCM
# workbook or system export into the initial accepted Project Record.  It is
# folded in here for the same reason as the two relations above: the migration
# window holds one unreleased transition and this is it.
#
# Three identities are deliberately three things, because conflating them is
# how a later format change becomes an unattributed edit of accepted values:
#
#   * ``project_baseline_sources`` is the **accepted data-baseline identity** —
#     the exact bytes, digest, customer, source identity, adopted worksheet or
#     record scope, importer identity and version, and the coordinator preview
#     the named person actually adopted.  One row per project, so a project has
#     one initial Adopt Baseline and a second is refused rather than silently
#     overwriting the first.
#   * ``project_baseline_source_rows`` is **source-row identity**, kept distinct
#     from Project Record subject identity so two rows that repeat one Utility
#     Conflict ID never collapse into one subject.  It also carries the external
#     system identifiers and source URLs the workbook itself printed, so a later
#     read-only deep link (#527, #528) resolves without re-reading the file.
#   * ``project_baseline_formats`` is the **output-template identity** and the
#     **field-mapping identity**, registered separately and replaceable on their
#     own attributable act.  Registering a replacement supersedes its
#     predecessor and writes no accepted value; it is not a second adoption.
#
# The write path is ``adopt_project_record_baseline``: one ``SECURITY DEFINER``
# command owned by the record-decision role that writes one Project Record
# revision, one separately identified ``fact_decisions`` row per adopted Source
# Fact, the three identities, and every source row — or nothing.  The
# application runtime role reads all three tables and writes none of them, so a
# partial commit is not something a caller can construct.

BASELINE_RECORD_TABLES = (
    "project_baseline_sources",
    "project_baseline_source_rows",
    "project_baseline_formats",
)

BASELINE_RECORD_SCHEMA = """
create table public.project_baseline_sources (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    revision_id bigint not null references public.project_record_revisions (id),
    document_id bigint not null references public.documents (id),
    content_sha256 character varying(64) not null,
    byte_size bigint not null,
    filename text not null,
    -- The customer's own name for this exact revision, and whose record it is.
    source_identity character varying(160) not null,
    customer character varying(160) not null,
    source_kind character varying(32) not null,
    -- The adopted worksheet or record scope, the columns no canonical field
    -- names, the coordinator questions the adopting person saw, and the
    -- Corridor operations reading that resolved the workbook mechanics. Kept
    -- because an unknown column that is not retained is an unknown column that
    -- was silently discarded.
    worksheet_scope jsonb not null,
    unknown_columns jsonb not null,
    coordinator_questions jsonb not null,
    operations_summary jsonb not null,
    importer_identity character varying(128) not null,
    importer_version character varying(64) not null,
    preview_fingerprint character varying(64) not null,
    adopted_by_principal character varying(128) not null,
    idempotency_key character varying(160) not null,
    adopted_at timestamp with time zone not null default now(),
    constraint uq_project_baseline_sources_project unique (project_id),
    constraint uq_project_baseline_sources_project_id unique (project_id, id),
    constraint uq_project_baseline_sources_revision unique (revision_id),
    constraint ck_project_baseline_sources_digest check (
        content_sha256 ~ '^[0-9a-f]{64}$'
    ),
    constraint ck_project_baseline_sources_preview check (
        preview_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    constraint ck_project_baseline_sources_kind check (
        source_kind in ('ucm_workbook', 'system_export')
    ),
    constraint ck_project_baseline_sources_principal check (
        length(btrim(adopted_by_principal)) > 0
    ),
    constraint ck_project_baseline_sources_byte_size check (byte_size > 0)
);

create index ix_project_baseline_sources_project_id
    on public.project_baseline_sources (project_id);

create table public.project_baseline_source_rows (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    baseline_source_id bigint not null,
    source_row_key character varying(160) not null,
    sheet_name text not null,
    row_number integer not null,
    business_identity character varying(128),
    record_subject_key character varying(160),
    external_system_id character varying(160),
    source_url text,
    excluded boolean not null default false,
    exclusion_reason character varying(64),
    constraint fk_project_baseline_source_rows_source foreign key
        (project_id, baseline_source_id)
        references public.project_baseline_sources (project_id, id),
    constraint uq_project_baseline_source_rows_key unique
        (baseline_source_id, source_row_key),
    constraint uq_project_baseline_source_rows_subject unique
        (baseline_source_id, record_subject_key),
    constraint ck_project_baseline_source_rows_row_number check (row_number > 0),
    -- An adopted row resolves to exactly one Project Record subject; an
    -- excluded row resolves to none and says why. Neither is ever a row the
    -- adoption dropped without saying so.
    constraint ck_project_baseline_source_rows_exclusion check (
        (excluded and record_subject_key is null and exclusion_reason is not null)
        or (not excluded and record_subject_key is not null
            and exclusion_reason is null)
    )
);

create index ix_project_baseline_source_rows_project_id
    on public.project_baseline_source_rows (project_id);
create index ix_project_baseline_source_rows_source
    on public.project_baseline_source_rows (baseline_source_id);

create table public.project_baseline_formats (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    format_kind character varying(32) not null,
    format_identity character varying(160) not null,
    format_version character varying(64) not null,
    content_sha256 character varying(64) not null,
    registered_by_principal character varying(128) not null,
    idempotency_key character varying(160) not null,
    superseded_by bigint references public.project_baseline_formats (id)
        deferrable initially deferred,
    registered_at timestamp with time zone not null default now(),
    constraint uq_project_baseline_formats_key unique
        (project_id, idempotency_key),
    constraint uq_project_baseline_formats_superseded_by unique (superseded_by),
    constraint ck_project_baseline_formats_kind check (
        format_kind in ('output_template', 'field_mapping')
    ),
    constraint ck_project_baseline_formats_digest check (
        content_sha256 ~ '^[0-9a-f]{64}$'
    ),
    constraint ck_project_baseline_formats_principal check (
        length(btrim(registered_by_principal)) > 0
    )
);

create index ix_project_baseline_formats_project_id
    on public.project_baseline_formats (project_id);
create unique index uq_project_baseline_formats_effective
    on public.project_baseline_formats (project_id, format_kind)
    where superseded_by is null;

create function public.enforce_project_baseline_record_write() returns trigger
    language plpgsql
    as $$
        begin
            if current_user <> 'corridor_fact_decision_writer' then
                raise exception 'Adopt Baseline requires the typed adoption command'
                    using errcode='23514';
            end if;
            if tg_op <> 'INSERT' then
                raise exception 'The adopted baseline is append-only: a replacement is a new record change, never an edit'
                    using errcode='23514';
            end if;
            return new;
        end; $$;

create function public.enforce_project_baseline_format_write() returns trigger
    language plpgsql
    as $$
        begin
            if current_user <> 'corridor_fact_decision_writer' then
                raise exception 'Adopt Baseline requires the typed adoption command'
                    using errcode='23514';
            end if;
            if tg_op = 'INSERT' then
                return new;
            end if;
            if tg_op = 'UPDATE' then
                if new.id is distinct from old.id
                   or new.project_id is distinct from old.project_id
                   or new.format_kind is distinct from old.format_kind
                   or new.format_identity is distinct from old.format_identity
                   or new.format_version is distinct from old.format_version
                   or new.content_sha256 is distinct from old.content_sha256
                   or new.registered_by_principal
                       is distinct from old.registered_by_principal
                   or new.idempotency_key is distinct from old.idempotency_key
                   or new.registered_at is distinct from old.registered_at
                   or old.superseded_by is not null
                   or new.superseded_by is null then
                    raise exception 'A registered format may only become superseded once'
                        using errcode='23514';
                end if;
                return new;
            end if;
            raise exception 'The adopted baseline is append-only: a replacement is a new record change, never an edit'
                using errcode='23514';
        end; $$;

create trigger trg_project_baseline_sources_write
    before insert or update or delete on public.project_baseline_sources
    for each row execute function public.enforce_project_baseline_record_write();
create trigger trg_project_baseline_sources_truncate
    before truncate on public.project_baseline_sources
    for each statement
    execute function public.enforce_project_baseline_record_write();

create trigger trg_project_baseline_source_rows_write
    before insert or update or delete on public.project_baseline_source_rows
    for each row execute function public.enforce_project_baseline_record_write();
create trigger trg_project_baseline_source_rows_truncate
    before truncate on public.project_baseline_source_rows
    for each statement
    execute function public.enforce_project_baseline_record_write();

create trigger trg_project_baseline_formats_write
    before insert or update or delete on public.project_baseline_formats
    for each row execute function public.enforce_project_baseline_format_write();
create trigger trg_project_baseline_formats_truncate
    before truncate on public.project_baseline_formats
    for each statement
    execute function public.enforce_project_baseline_format_write();
"""

BASELINE_RECORD_SCHEMA_DOWN = """
drop table if exists public.project_baseline_source_rows cascade;
drop table if exists public.project_baseline_formats cascade;
drop table if exists public.project_baseline_sources cascade;
drop function if exists public.enforce_project_baseline_record_write() cascade;
drop function if exists public.enforce_project_baseline_format_write() cascade;
"""

ADOPT_PROJECT_RECORD_BASELINE = """
create function public.adopt_project_record_baseline(
    p_project_id bigint,
    p_principal character varying,
    p_idempotency_key character varying,
    p_baseline jsonb,
    p_rows jsonb,
    p_formats jsonb,
    p_fact_ids bigint[]
) returns jsonb
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            existing project_baseline_sources%ROWTYPE;
            adopted_document bigint;
            predecessor_revision bigint;
            new_revision bigint;
            new_source bigint;
            row_payload jsonb;
            format_payload jsonb;
            decided facts%ROWTYPE;
            fact_id bigint;
        begin
            if p_principal is null or length(btrim(p_principal)) = 0 then
                raise exception 'Adopt Baseline names the person adopting'
                    using errcode='23514';
            end if;
            if p_idempotency_key is null
               or length(btrim(p_idempotency_key)) = 0 then
                raise exception 'Adopt Baseline needs an idempotency key'
                    using errcode='23514';
            end if;
            adopted_document := (p_baseline->>'document_id')::bigint;

            select * into existing from project_baseline_sources
             where project_id = p_project_id;
            if found then
                if existing.idempotency_key is distinct from p_idempotency_key
                   or existing.content_sha256
                       is distinct from (p_baseline->>'content_sha256')
                   or existing.preview_fingerprint
                       is distinct from (p_baseline->>'preview_fingerprint') then
                    raise exception 'project % already adopted a baseline; replacing it is a new record change, not another initial adoption', p_project_id
                        using errcode='23514';
                end if;
                return jsonb_build_object(
                    'revision_id', existing.revision_id,
                    'baseline_source_id', existing.id,
                    'created', false
                );
            end if;

            if not exists (
                select 1 from documents
                 where id = adopted_document and project_id = p_project_id
            ) then
                raise exception 'the adopted source belongs to another project'
                    using errcode='23514';
            end if;

            -- A nonempty accepted Project Record is never silently adopted
            -- over: it needs an explicit migration, a reconciliation, or a
            -- fresh customer environment.
            if exists (
                select 1 from fact_decisions
                 where project_id = p_project_id and superseded_by is null
            ) or exists (
                select 1 from dependencies where project_id = p_project_id
            ) then
                raise exception 'project % already holds an accepted Project Record; adopting a baseline over it needs an explicit migration or reconciliation, or a fresh environment', p_project_id
                    using errcode='23514';
            end if;

            if exists (
                select 1 from unnest(coalesce(p_fact_ids, '{}'::bigint[]))
                          as requested(id)
                 left join facts
                        on facts.id = requested.id
                       and facts.project_id = p_project_id
                       and facts.document_id = adopted_document
                 where facts.id is null
            ) then
                raise exception 'Adopt Baseline decides only Source Facts this project captured from the adopted source'
                    using errcode='23514';
            end if;

            select max(id) into predecessor_revision
              from project_record_revisions where project_id = p_project_id;
            insert into project_record_revisions (
                project_id, predecessor_revision_id, command_type,
                human_principal, released_policy, idempotency_key
            ) values (
                p_project_id, predecessor_revision, 'adopt_baseline',
                p_principal, null, p_idempotency_key
            ) returning id into new_revision;

            insert into project_baseline_sources (
                project_id, revision_id, document_id, content_sha256, byte_size,
                filename, source_identity, customer, source_kind,
                worksheet_scope, unknown_columns, coordinator_questions,
                operations_summary, importer_identity, importer_version,
                preview_fingerprint, adopted_by_principal, idempotency_key
            ) values (
                p_project_id, new_revision, adopted_document,
                p_baseline->>'content_sha256',
                (p_baseline->>'byte_size')::bigint,
                p_baseline->>'filename', p_baseline->>'source_identity',
                p_baseline->>'customer', p_baseline->>'source_kind',
                p_baseline->'worksheet_scope', p_baseline->'unknown_columns',
                p_baseline->'coordinator_questions',
                p_baseline->'operations_summary',
                p_baseline->>'importer_identity',
                p_baseline->>'importer_version',
                p_baseline->>'preview_fingerprint', p_principal,
                p_idempotency_key
            ) returning id into new_source;

            for row_payload in
                select value from jsonb_array_elements(coalesce(p_rows, '[]'::jsonb))
            loop
                insert into project_baseline_source_rows (
                    project_id, baseline_source_id, source_row_key, sheet_name,
                    row_number, business_identity, record_subject_key,
                    external_system_id, source_url, excluded, exclusion_reason
                ) values (
                    p_project_id, new_source, row_payload->>'source_row_key',
                    row_payload->>'sheet_name',
                    (row_payload->>'row_number')::integer,
                    row_payload->>'business_identity',
                    row_payload->>'record_subject_key',
                    row_payload->>'external_system_id',
                    row_payload->>'source_url',
                    coalesce((row_payload->>'excluded')::boolean, false),
                    row_payload->>'exclusion_reason'
                );
            end loop;

            for format_payload in
                select value
                  from jsonb_array_elements(coalesce(p_formats, '[]'::jsonb))
            loop
                insert into project_baseline_formats (
                    project_id, format_kind, format_identity, format_version,
                    content_sha256, registered_by_principal, idempotency_key
                ) values (
                    p_project_id, format_payload->>'format_kind',
                    format_payload->>'format_identity',
                    format_payload->>'format_version',
                    format_payload->>'content_sha256', p_principal,
                    p_idempotency_key || ':' || (format_payload->>'format_kind')
                );
            end loop;

            foreach fact_id in array coalesce(p_fact_ids, '{}'::bigint[])
            loop
                select * into decided from facts where id = fact_id;
                insert into fact_decisions (
                    project_id, fact_id, subject_key, fact_type, revision_id,
                    disposition, superseded_by
                ) values (
                    p_project_id, fact_id, decided.subject_key,
                    decided.fact_type, new_revision, 'include', null
                );
            end loop;

            return jsonb_build_object(
                'revision_id', new_revision,
                'baseline_source_id', new_source,
                'created', true
            );
        end; $$;
"""

ADOPT_PROJECT_RECORD_BASELINE_SIGNATURE = (
    "(bigint, character varying, character varying, jsonb, jsonb, jsonb, bigint[])"
)

REGISTER_BASELINE_FORMAT = """
create function public.register_baseline_format(
    p_project_id bigint,
    p_format_kind character varying,
    p_format_identity character varying,
    p_format_version character varying,
    p_content_sha256 character varying,
    p_principal character varying,
    p_idempotency_key character varying
) returns jsonb
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            existing project_baseline_formats%ROWTYPE;
            superseded bigint;
            new_id bigint;
        begin
            if p_principal is null or length(btrim(p_principal)) = 0 then
                raise exception 'a format registration names the person registering it'
                    using errcode='23514';
            end if;
            select * into existing from project_baseline_formats
             where project_id = p_project_id
               and idempotency_key = p_idempotency_key;
            if found then
                if existing.format_kind is distinct from p_format_kind
                   or existing.content_sha256 is distinct from p_content_sha256 then
                    raise exception 'the format registration key is bound to different content'
                        using errcode='23514';
                end if;
                return jsonb_build_object(
                    'format_id', existing.id, 'created', false
                );
            end if;
            if not exists (
                select 1 from project_baseline_sources
                 where project_id = p_project_id
            ) then
                raise exception 'a project registers an output template or field mapping only after it adopts its data baseline'
                    using errcode='23514';
            end if;
            select id into superseded from project_baseline_formats
             where project_id = p_project_id
               and format_kind = p_format_kind
               and superseded_by is null;
            new_id := nextval('project_baseline_formats_id_seq');
            if superseded is not null then
                update project_baseline_formats set superseded_by = new_id
                 where id = superseded;
            end if;
            insert into project_baseline_formats (
                id, project_id, format_kind, format_identity, format_version,
                content_sha256, registered_by_principal, idempotency_key,
                superseded_by
            ) values (
                new_id, p_project_id, p_format_kind, p_format_identity,
                p_format_version, p_content_sha256, p_principal,
                p_idempotency_key, null
            );
            return jsonb_build_object('format_id', new_id, 'created', true);
        end; $$;
"""

REGISTER_BASELINE_FORMAT_SIGNATURE = (
    "(bigint, character varying, character varying, character varying, "
    "character varying, character varying, character varying)"
)

BASELINE_RECORD_COMMANDS = {
    "adopt_project_record_baseline": ADOPT_PROJECT_RECORD_BASELINE_SIGNATURE,
    "register_baseline_format": REGISTER_BASELINE_FORMAT_SIGNATURE,
}


# --- Project-bound push intake (#511) -------------------------------------
# Folded into this transition for the same window reason as the blocks above.
# ADR-0078 replaced ADR-0059's one global address, whose project was inferred
# from the message body, with a binding declared before the bytes arrive;
# ADR-0083 named the push half of that design.  These two tables are what a
# binding needs to exist before anything is parsed: the credential registry an
# alias or webhook resolves through, and the delivery ledger that makes a
# replay idempotent by delivery identity.
#
# The registry stores a one-way digest of the credential material and never
# the material: ADR-0079 as amended keeps connector credentials in the control
# plane, and recognizing a presented credential needs nothing more.  Neither
# table is updatable by a runtime capability except to revoke a credential, so
# an alias cannot be re-pointed at another project by the application.
#
# `inbound_messages` also loses its two cross-project unique constraints.  The
# same bytes and the same Message-ID delivered to two different projects are
# two deliveries, not a duplicate: a global constraint would either hand one
# customer's stored message back to another customer's alias, or let a guessed
# Message-ID refuse a delivery in a project the sender cannot see.  Identity is
# scoped to the boundary, exactly as ADR-0078 scopes thread identity.
PUSH_INTAKE_SCHEMA = """
create table public.push_intake_credentials (
    id bigserial primary key,
    customer text not null,
    project_id bigint not null references public.projects(id),
    channel character varying(32) not null,
    credential_sha256 character varying(64) not null,
    state character varying(16) not null default 'active',
    created_at timestamp with time zone not null default now(),
    constraint uq_push_intake_credential_digest unique (credential_sha256),
    constraint ck_push_intake_credential_customer
        check (length(btrim(customer)) > 0),
    constraint ck_push_intake_credential_channel
        check (channel in ('project_alias', 'shared_mailbox', 'webhook')),
    constraint ck_push_intake_credential_state
        check (state in ('active', 'revoked')),
    constraint ck_push_intake_credential_digest
        check (credential_sha256 ~ '^[0-9a-f]{64}$')
);

create index ix_push_intake_credentials_project_id
    on public.push_intake_credentials (project_id);

create table public.push_deliveries (
    id bigserial primary key,
    credential_id bigint not null
        references public.push_intake_credentials(id),
    customer text not null,
    project_id bigint not null references public.projects(id),
    channel character varying(32) not null,
    external_identity text not null,
    external_version text not null,
    original_timestamps_json jsonb not null default '{}'::jsonb,
    content_sha256 character varying(64) not null,
    bytes_reference text not null,
    metadata_json jsonb not null default '{}'::jsonb,
    delivery_identity character varying(64) not null,
    idempotency_key character varying(64) not null,
    received_at timestamp with time zone not null default now(),
    constraint uq_push_delivery_idempotency unique (idempotency_key),
    constraint ck_push_delivery_content_sha256
        check (content_sha256 ~ '^[0-9a-f]{64}$'),
    constraint ck_push_delivery_identity
        check (delivery_identity ~ '^[0-9a-f]{64}$'),
    constraint ck_push_delivery_idempotency
        check (idempotency_key ~ '^[0-9a-f]{64}$')
);

create index ix_push_deliveries_project_id
    on public.push_deliveries (project_id);

alter table public.inbound_messages
    add column push_delivery_id bigint references public.push_deliveries(id);

alter table public.inbound_messages
    add constraint uq_inbound_message_push_delivery unique (push_delivery_id);

alter table public.inbound_messages
    drop constraint inbound_messages_raw_sha256_key;

alter table public.inbound_messages
    add constraint uq_inbound_message_project_bytes
    unique (project_id, raw_sha256);

alter table public.inbound_messages
    drop constraint uq_inbound_message_message_id;

alter table public.inbound_messages
    add constraint uq_inbound_message_project_message_id
    unique (project_id, message_id);
"""

PUSH_INTAKE_SCHEMA_DOWN = """
alter table public.inbound_messages
    drop constraint if exists uq_inbound_message_project_message_id;

alter table public.inbound_messages
    add constraint uq_inbound_message_message_id unique (message_id);

alter table public.inbound_messages
    drop constraint if exists uq_inbound_message_project_bytes;

alter table public.inbound_messages
    add constraint inbound_messages_raw_sha256_key unique (raw_sha256);

alter table public.inbound_messages
    drop constraint if exists uq_inbound_message_push_delivery;

alter table public.inbound_messages drop column if exists push_delivery_id;

drop table if exists public.push_deliveries;

drop table if exists public.push_intake_credentials;
"""

PUSH_INTAKE_TABLES = ("push_intake_credentials", "push_deliveries")


def upgrade() -> None:
    """Create the append commands and take back the raw source-table writes."""

    op.execute(PROPOSED_DELTA_SCHEMA)
    op.execute(SUPPORT_ASSESSMENT_SCHEMA)
    # A table created here carries no grants at all; the runtime
    # capabilities read the relation, and only the command writes it.
    for table in SUPPORT_TABLES + DELTA_TABLES:
        op.execute(f"grant select on public.{table} to {RUNTIME_LOGINS}")

    op.execute(f"grant usage on schema public to {SOURCE_APPEND_ROLE}")
    for table in REFERENCED_TABLES:
        op.execute(f"grant select on public.{table} to {SOURCE_APPEND_ROLE}")
    for table in SOURCE_TABLES + SUPPORT_TABLES + DELTA_TABLES:
        op.execute(f"grant select, insert on public.{table} to {SOURCE_APPEND_ROLE}")
        op.execute(
            f"grant usage, select on sequence public.{table}_id_seq "
            f"to {SOURCE_APPEND_ROLE}"
        )
    # Supersession is the one change an assessment ever sees, and the guard
    # trigger holds it to that column, once.
    op.execute(
        "grant update (superseded_by) on public.support_assessments "
        f"to {SOURCE_APPEND_ROLE}"
    )

    for body in (
        APPEND_SOURCE_SEGMENTS,
        APPEND_FACT,
        APPEND_EXTRACTED_PROPOSAL,
        APPEND_SOURCE_FACT_RECEIPT,
        APPEND_SUPPORT_ASSESSMENT,
        APPEND_PROPOSED_DELTAS,
    ):
        op.execute(body)
    for name, signature in COMMANDS.items():
        op.execute(
            f"alter function public.{name}{signature} owner to {SOURCE_APPEND_ROLE}"
        )
        # PostgreSQL grants EXECUTE to PUBLIC on every new function, and a
        # default-privilege revoke does not persist (#545); each command
        # takes PUBLIC back itself.
        op.execute(f"revoke all on function public.{name}{signature} from public")
        op.execute(
            f"grant execute on function public.{name}{signature} to {RUNTIME_LOGINS}"
        )

    for table in SOURCE_TABLES + SUPPORT_TABLES + DELTA_TABLES:
        op.execute(
            f"revoke insert, update, delete, truncate on public.{table} "
            f"from {RUNTIME_LOGINS}"
        )

    # --- Baseline/delta operating mode (#520) -----------------------------
    op.execute(BASELINE_ADOPTION_SCHEMA)
    op.execute(OPERATING_MODE_FUNCTIONS)
    op.execute(ADOPT_PROJECT_BASELINE)
    op.execute(STRUCTURED_CELL_INCLUSION_MODE_GUARDED)
    for table in OPERATING_MODE_GUARDED_TABLES:
        op.execute(
            f"create trigger trg_{table}_operating_mode "
            f"before insert or update on public.{table} "
            "for each row execute function "
            "public.refuse_legacy_accepted_write_for_adopted_project()"
        )
    # The mode is readable by the application; only the record-decision role's
    # command can establish it.  A new table arrives carrying the schema's
    # default privileges, so the write half is taken back explicitly.
    op.execute(
        f"grant select on public.project_baseline_adoptions to {RUNTIME_LOGINS}"
    )
    op.execute(
        "revoke insert, update, delete, truncate "
        f"on public.project_baseline_adoptions from {RUNTIME_LOGINS}"
    )
    op.execute(f"grant usage on schema public to {OPERATING_MODE_ROLE}")
    op.execute(
        "grant select, insert on public.project_baseline_adoptions "
        f"to {OPERATING_MODE_ROLE}"
    )
    op.execute(
        "grant usage, select on sequence public.project_baseline_adoptions_id_seq "
        f"to {OPERATING_MODE_ROLE}"
    )
    op.execute(
        f"alter function public.adopt_project_baseline"
        f"{ADOPT_PROJECT_BASELINE_SIGNATURE} owner to {OPERATING_MODE_ROLE}"
    )
    for function in (
        f"adopt_project_baseline{ADOPT_PROJECT_BASELINE_SIGNATURE}",
        "refuse_legacy_accepted_write_for_adopted_project()",
    ):
        op.execute(f"revoke all on function public.{function} from public")
    # Adopt Baseline is a bulk human command, so it joins the other human
    # decision commands on the web capability alone (#509, ADR-0076).
    op.execute(
        f"grant execute on function public.adopt_project_baseline"
        f"{ADOPT_PROJECT_BASELINE_SIGNATURE} to corridor_web"
    )

    # --- Adopt Baseline (#509) --------------------------------------------
    op.execute(BASELINE_RECORD_SCHEMA)
    op.execute(ADOPT_PROJECT_RECORD_BASELINE)
    op.execute(REGISTER_BASELINE_FORMAT)
    for table in BASELINE_RECORD_TABLES:
        # The application reads the adopted baseline and writes none of it;
        # a new table arrives with the schema's default privileges, so the
        # write half is taken back explicitly.
        op.execute(f"grant select on public.{table} to {RUNTIME_LOGINS}")
        op.execute(
            f"revoke insert, update, delete, truncate on public.{table} "
            f"from {RUNTIME_LOGINS}"
        )
        op.execute(
            f"grant select, insert on public.{table} to {OPERATING_MODE_ROLE}"
        )
        op.execute(
            f"grant usage, select on sequence public.{table}_id_seq "
            f"to {OPERATING_MODE_ROLE}"
        )
    # Supersession is the one change a registered format ever sees, and the
    # guard trigger holds it to that column, once.
    op.execute(
        "grant update (superseded_by) on public.project_baseline_formats "
        f"to {OPERATING_MODE_ROLE}"
    )
    for name, signature in BASELINE_RECORD_COMMANDS.items():
        op.execute(
            f"alter function public.{name}{signature} owner to "
            f"{OPERATING_MODE_ROLE}"
        )
        op.execute(f"revoke all on function public.{name}{signature} from public")
        # Adopt Baseline and a later format registration are attributable human
        # acts, so they join the other decision commands on the web capability
        # alone (#509, ADR-0076).
        op.execute(
            f"grant execute on function public.{name}{signature} to corridor_web"
        )

    # --- Project-bound push intake (#511) ---------------------------------
    op.execute(PUSH_INTAKE_SCHEMA)
    for table in PUSH_INTAKE_TABLES:
        # The binding registry and the delivery ledger are append-only to the
        # application: a credential may be revoked and nothing else, so no
        # runtime capability can re-point an alias at another project or erase
        # the record of a delivery it already took.  The privileges are
        # written out rather than left to the schema's defaults, because the
        # revocation is the point.
        op.execute(f"grant select, insert on public.{table} to {RUNTIME_LOGINS}")
        op.execute(
            f"grant usage, select on sequence public.{table}_id_seq "
            f"to {RUNTIME_LOGINS}"
        )
        op.execute(
            f"revoke update, delete, truncate on public.{table} "
            f"from {RUNTIME_LOGINS}"
        )
    op.execute(
        "grant update (state) on public.push_intake_credentials to corridor_web"
    )


def downgrade() -> None:
    """Drop the commands and hand the raw source-table writes back.

    The Support Assessment relation was born in this transition, so the
    downgrade removes it whole rather than opening it to raw writes.
    """

    # --- Project-bound push intake (#511) ---------------------------------
    op.execute(PUSH_INTAKE_SCHEMA_DOWN)

    # --- Adopt Baseline (#509) --------------------------------------------
    for name, signature in BASELINE_RECORD_COMMANDS.items():
        op.execute(f"drop function if exists public.{name}{signature}")
    for table in BASELINE_RECORD_TABLES:
        op.execute(
            f"do $$ begin "
            f"if exists (select 1 from pg_tables where schemaname = 'public' "
            f"and tablename = '{table}') then "
            f"revoke select on public.{table} from {RUNTIME_LOGINS}; "
            f"end if; end $$;"
        )
    op.execute(BASELINE_RECORD_SCHEMA_DOWN)

    # --- Baseline/delta operating mode (#520) -----------------------------
    for table in OPERATING_MODE_GUARDED_TABLES:
        op.execute(f"drop trigger if exists trg_{table}_operating_mode on public.{table}")
    op.execute(
        f"drop function if exists public.adopt_project_baseline"
        f"{ADOPT_PROJECT_BASELINE_SIGNATURE}"
    )
    op.execute(STRUCTURED_CELL_INCLUSION_RELEASED)
    op.execute(OPERATING_MODE_FUNCTIONS_DOWN)
    op.execute(BASELINE_ADOPTION_SCHEMA_DOWN)

    for table in SOURCE_TABLES:
        op.execute(
            f"grant insert, update, delete on public.{table} to {RUNTIME_LOGINS}"
        )
    for name in COMMANDS:
        op.execute(f"drop function if exists public.{name}")
    for table in SOURCE_TABLES:
        op.execute(
            f"revoke usage, select on sequence public.{table}_id_seq "
            f"from {SOURCE_APPEND_ROLE}"
        )
        op.execute(f"revoke select, insert on public.{table} from {SOURCE_APPEND_ROLE}")
    op.execute(
        f"do $$ begin "
        f"if exists (select 1 from pg_tables where schemaname = 'public' and tablename = 'support_assessments') then "
        f"revoke update (superseded_by) on public.support_assessments from {SOURCE_APPEND_ROLE}; "
        f"end if; end $$;"
    )
    for table in REFERENCED_TABLES:
        op.execute(f"revoke select on public.{table} from {SOURCE_APPEND_ROLE}")
    op.execute(f"revoke usage on schema public from {SOURCE_APPEND_ROLE}")
    for table in SUPPORT_TABLES + DELTA_TABLES:
        op.execute(
            f"do $$ begin "
            f"if exists (select 1 from pg_tables where schemaname = 'public' and tablename = '{table}') then "
            f"revoke select on public.{table} from {RUNTIME_LOGINS}; "
            f"end if; end $$;"
        )
    op.execute(SUPPORT_ASSESSMENT_SCHEMA_DOWN)
    op.execute(PROPOSED_DELTA_SCHEMA_DOWN)

