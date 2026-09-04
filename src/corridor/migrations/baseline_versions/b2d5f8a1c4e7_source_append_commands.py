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

The same transition finally carries ADR-0081 stage 1 (#512), again because the
window holds one unreleased transition.  ``recorded_verbal_origins`` is the
spine-native source origin of a Recorded Verbal Statement — the recorder, the
recorded time, the conversation day, the exact words and their digest, and the
re-attestation chain — so ``source_segments`` stops naming a legacy
``dependency_events`` row and ``facts.content_sha256`` stops carrying one inside
the Fact identity digest.  The legacy key survives only in
``recorded_verbal_origin_statements``, the temporary compatibility mapping the
dual-write of stages 1 through 5 still needs.  The backfill gives every existing
dual-written verbal exactly one origin, one mapping, and one attributable
receipt, reproduces each stored Fact digest from its stored row before replacing
it, and records both digests; anything it cannot reconcile aborts the transition
instead of being guessed at.

The transition closes with permanent-state de-duplication (#457), the ADR-0081
convergence prerequisite, again because the window holds one unreleased
transition.  Every family above already derived what makes a row the same row,
but derived it *where the row is written*: in a command's body, or in the
Python that called it.  This block makes each of those identities a constraint
instead — the Fact identity digest, one Proposed Delta group per source
version, one Record Inclusion decision per Fact per revision and one dated
deferral per scheduling act, a Project Record revision key that cannot be
blank, and a connector delivery whose ADR-0083 envelope identity the database
re-derives from the row's own columns.  Existing rows are never merged or
dropped to make room: the transition counts what the new identity cannot
represent and refuses.

The transition ends by making one delivery ledger of the two halves (#599,
ADR-0089), once more because the window holds one unreleased transition.
ADR-0083 already declared the ``SourceEnvelope`` the one normalized ingress
record every channel produces, and the implementation persisted it for push
alone; a pulled delivery existed nowhere and a delivery the intake gate refused
was lost.  ``push_deliveries`` therefore becomes ``source_deliveries`` — by
rename, so every row and every key that names one is carried exactly — with the
transport, the connector or channel configuration, the service and run that
took delivery, the disposition, and the refusal reason ADR-0078 always said a
connected source carries.  ``connector_checkpoint_advances`` takes the external
cursor off the completed Due Work receipt, so deleting a receipt can no longer
reset it, and ``connector_checkpoint_advance_deliveries`` enforces the
checkpoint rule where the coverage is written: never past a transient failure,
and past a refusal only on its recorded evidence.

The transition closes by replacing two copies with references (#605), once
more because the window holds one unreleased transition.
``extractor_configurations`` stores one sealed extractor receipt by its digest
instead of once per Extraction Run, and the run keeps the reference it already
had; the backfill carries every distinct receipt across exactly as stored, and
a run that never recorded one stays explicitly unknown rather than being given
today's deployed configuration.  ``evidence_link_sources`` lets an Evidence
Link name the Source Segment that owns its words (ADR-0068) instead of copying
them; ``evidence_links.quote`` stays, readable and unrewritten, because
proving a cited segment and a stored quote equivalent is a separate piece of
work with its own corpus.
"""

from __future__ import annotations

from hashlib import sha256
import json

from alembic import op
import sqlalchemy as sa


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
# --- Resolve Delta (#519, ADR-0076, ADR-0083, ADR-0084, ADR-0085) ----------
#
# Resolving one Proposed Delta is the act that finally moves the accepted
# record, so it belongs to the record-decision role and to nothing else.  The
# occurrence, the group, and the lineage links stay with the source-append
# role above; ``delta_dispositions``, the decision that binds one disposition
# to its Project Record revision, the Support Assessments that decision relied
# on, and the Work List scheduling receipt move here.
#
# Three identities are again deliberately three things:
#
#   * ``delta_dispositions`` (created above, #518) stays the lifecycle marker
#     the live-state query walks: one per delta, so a delta resolves once.
#   * ``delta_record_decisions`` is the **authority binding**: which revision
#     carried the decision, which typed effect it had, which principal made
#     it, what accepted revision they had observed, and — for an edit — the
#     constrained basis that made an edited value source-backed rather than
#     free text.
#   * ``delta_decision_supports`` names the **effective Support Assessments**
#     (#530) the decision relied on.  Locator validity is never consulted
#     here, so a passed Source Passage Check can never stand in for support.
#
# ``resolve_proposed_delta_decision`` is the one writer.  Passing a revision
# makes it a child of #526's packet-owned transaction: it writes the decision
# into that revision and creates no intermediate one.  Passing none makes it a
# standalone act that opens exactly one revision itself.  Either way the same
# validation runs before any authoritative write, so the two contexts cannot
# drift into two sets of decision rules.
#
# ``defer_proposed_delta`` writes only the dated scheduling receipt: the delta
# stays open and no Project Record revision exists (ADR-0084, ADR-0085).

RESOLVE_DELTA_ROLE = "corridor_fact_decision_writer"

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


# --- Review Packet resolution (#526, ADR-0035, ADR-0084, ADR-0085) ---------
#
# ADR-0085 makes a Review Packet a *derived presentation* over open Proposed
# Deltas: no packet is stored, no packet has a lifecycle, and two readings of
# the same state rebuild the same packets.  What must be durable is the
# **act** — the one attributable transaction a coordinator committed — and
# that is what this block records.
#
# ``delta_review_packet_receipts`` holds one act: the grouping rule and key
# the coordinator was shown, the principal, the accepted revision they had
# read, and the *optional* one Project Record revision the act produced.  The
# revision is null exactly when every child was a dated Defer, because
# scheduling writes no revision (ADR-0084).
#
# ``delta_review_packet_children`` keeps each child's own identity, because
# ADR-0035 forbids one Save collapsing the distinct domain acts inside it:
# the exact delta, the position it was shown in, the source revision the
# coordinator had read for it, the outcome they chose, and the one identity
# that outcome produced — a Human Record Decision (#519), a Follow-up Plan,
# or a scheduling receipt.
#
# ``delta_follow_up_plans`` is the spine's Follow-up Plan.  ADR-0084 forbids
# settling an external fact with free text, so Needs coordination has to be a
# recorded decision rather than a discarded selection: the exact question, who
# owes the answer, when it returns, what it affects, and the evidence that
# raised it.  It joins the packet's revision as a separately identified
# decision and writes no disposition, so the delta stays open and the proposed
# value stays unaccepted.  The frozen legacy ``follow_up_plan_receipts`` over
# ``work_decisions`` is not extended for adopted-baseline projects (ADR-0084).
#
# ``reverse_review_packet`` is Undo.  It never deletes and never cascades: it
# appends one compensating revision restoring each predecessor decision the
# packet superseded, and refuses outright when a later act already depends on
# a result.  A later *correction* is not this command — it is one new delta
# resolved by a later attributable decision (#519), which leaves the original
# receipt and its children exactly as recorded.

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



# --- Spine-native Recorded Verbal origin (#512, ADR-0081 stage 1) -----------
#
# ADR-0074 gave the `recorded_verbal_statement` segment a `statement_id`
# foreign key to `dependency_events`, so the evidence spine depended on a
# legacy Project Record aggregate, and `facts.content_sha256` carried that
# legacy id inside the Fact identity digest.  ADR-0081 stage 1 replaces both:
# the attestation becomes its own row, the segment points at it, and the
# legacy key survives only in `recorded_verbal_origin_statements` — a
# temporary compatibility mapping that retires with the dual-write.
#
# The transition is folded into this revision because the migration window
# holds one unreleased transition and this is it (`migrations/policy.py`).

RECORDED_VERBAL_TABLES = (
    "recorded_verbal_origins",
    "recorded_verbal_origin_statements",
    "recorded_verbal_origin_backfill_receipts",
    "recorded_verbal_origin_fact_digests",
)

# The origin and its legacy mapping are appended by the source-append role's
# command.  The two backfill receipt tables are written once, by this
# migration, and are read-only to everyone afterwards.
RECORDED_VERBAL_APPEND_TABLES = (
    "recorded_verbal_origins",
    "recorded_verbal_origin_statements",
)

RECORDED_VERBAL_COMMANDS = {
    "append_recorded_verbal_origin": (
        "(bigint, text, timestamp with time zone, date, text, "
        "character varying, bigint, bigint)"
    ),
    "append_source_segments": "(bigint, bigint, bigint, jsonb)",
}

RECORDED_VERBAL_ORIGIN_SCHEMA = """
create table public.recorded_verbal_origins (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    recorded_by text not null,
    recorded_at timestamp with time zone not null,
    conversation_date date,
    exact_text text not null,
    content_sha256 character varying(64) not null,
    corrects_origin_id bigint references public.recorded_verbal_origins (id),
    created_at timestamp with time zone not null default now(),
    constraint uq_recorded_verbal_origins_project_id unique (project_id, id),
    constraint uq_recorded_verbal_origins_corrects unique (corrects_origin_id),
    constraint ck_recorded_verbal_origins_recorder
        check (length(trim(recorded_by)) > 0),
    constraint ck_recorded_verbal_origins_exact_text
        check (length(exact_text) > 0),
    constraint ck_recorded_verbal_origins_content_sha256
        check (content_sha256 ~ '^[0-9a-f]{64}$'),
    constraint ck_recorded_verbal_origins_corrects_other
        check (corrects_origin_id is null or corrects_origin_id <> id)
);
create index ix_recorded_verbal_origins_project_id
    on public.recorded_verbal_origins (project_id);

-- The one place a target relation still names a legacy statement.  Both
-- directions are unique, so a legacy statement can never acquire a second
-- origin and an origin can never be re-pointed at a second statement.
create table public.recorded_verbal_origin_statements (
    origin_id bigint primary key
        references public.recorded_verbal_origins (id),
    project_id bigint not null references public.projects (id),
    statement_id bigint not null references public.dependency_events (id),
    created_at timestamp with time zone not null default now(),
    constraint uq_recorded_verbal_origin_statements_statement
        unique (statement_id)
);
create index ix_recorded_verbal_origin_statements_project_id
    on public.recorded_verbal_origin_statements (project_id);

create table public.recorded_verbal_origin_backfill_receipts (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    origin_id bigint not null references public.recorded_verbal_origins (id),
    legacy_statement_id bigint not null,
    source_segment_id bigint not null references public.source_segments (id),
    fact_count integer not null,
    migration_revision character varying(32) not null,
    executed_by text not null,
    created_at timestamp with time zone not null default now(),
    constraint uq_recorded_verbal_backfill_receipts_origin unique (origin_id),
    constraint uq_recorded_verbal_backfill_receipts_statement
        unique (legacy_statement_id),
    constraint uq_recorded_verbal_backfill_receipts_segment
        unique (source_segment_id),
    constraint ck_recorded_verbal_backfill_receipts_executor
        check (length(trim(executed_by)) > 0),
    constraint ck_recorded_verbal_backfill_receipts_fact_count
        check (fact_count >= 0)
);
create index ix_recorded_verbal_origin_backfill_receipts_project_id
    on public.recorded_verbal_origin_backfill_receipts (project_id);

create table public.recorded_verbal_origin_fact_digests (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    receipt_id bigint not null
        references public.recorded_verbal_origin_backfill_receipts (id),
    fact_id bigint not null references public.facts (id),
    prior_content_sha256 character varying(64) not null,
    content_sha256 character varying(64) not null,
    constraint uq_recorded_verbal_origin_fact_digests_fact unique (fact_id),
    constraint ck_recorded_verbal_origin_fact_digests_change
        check (prior_content_sha256 ~ '^[0-9a-f]{64}$'
               and content_sha256 ~ '^[0-9a-f]{64}$'
               and prior_content_sha256 <> content_sha256)
);
create index ix_recorded_verbal_origin_fact_digests_project_id
    on public.recorded_verbal_origin_fact_digests (project_id);
create index ix_recorded_verbal_origin_fact_digests_receipt_id
    on public.recorded_verbal_origin_fact_digests (receipt_id);

alter table public.source_segments
    add column recorded_verbal_origin_id bigint;
"""

# Applied after the backfill has filled `recorded_verbal_origin_id`: the
# locator check and the one-segment-per-origin index move to the spine-native
# column, and the legacy key leaves the target table entirely.
RECORDED_VERBAL_SEGMENT_CUTOVER = """
alter table public.source_segments
    drop constraint ck_source_segments_locator;
alter table public.source_segments
    add constraint ck_source_segments_locator check (
        (kind = 'spreadsheet_cell' and document_id is not null
         and recorded_verbal_origin_id is null and length(sheet_name) > 0
         and cell_range ~ '^[A-Z]+[1-9][0-9]*$' and page_no is null
         and start_offset is null and end_offset is null)
        or (kind = 'prose_span' and document_id is not null
            and recorded_verbal_origin_id is null and sheet_name is null
            and cell_range is null and page_no > 0 and start_offset >= 0
            and end_offset > start_offset)
        or (kind = 'recorded_verbal_statement' and document_id is null
            and recorded_verbal_origin_id is not null and sheet_name is null
            and cell_range is null and page_no is null
            and start_offset is null and end_offset is null)
    );
drop index public.uq_source_segments_statement;
alter table public.source_segments drop column statement_id;
alter table public.source_segments
    add constraint fk_source_segments_recorded_verbal_origin_scope
    foreign key (project_id, recorded_verbal_origin_id)
    references public.recorded_verbal_origins (project_id, id);
create unique index uq_source_segments_recorded_verbal_origin
    on public.source_segments (recorded_verbal_origin_id)
    where kind = 'recorded_verbal_statement';
"""

RECORDED_VERBAL_SEGMENT_RESTORE_COLUMN = """
alter table public.source_segments
    add column statement_id bigint references public.dependency_events (id);
"""

RECORDED_VERBAL_SEGMENT_CUTOVER_DOWN = """
alter table public.source_segments
    drop constraint ck_source_segments_locator;
alter table public.source_segments
    add constraint ck_source_segments_locator check (
        (kind = 'spreadsheet_cell' and document_id is not null
         and statement_id is null and length(sheet_name) > 0
         and cell_range ~ '^[A-Z]+[1-9][0-9]*$' and page_no is null
         and start_offset is null and end_offset is null)
        or (kind = 'prose_span' and document_id is not null
            and statement_id is null and sheet_name is null
            and cell_range is null and page_no > 0 and start_offset >= 0
            and end_offset > start_offset)
        or (kind = 'recorded_verbal_statement' and document_id is null
            and statement_id is not null and sheet_name is null
            and cell_range is null and page_no is null
            and start_offset is null and end_offset is null)
    );
drop index public.uq_source_segments_recorded_verbal_origin;
alter table public.source_segments
    drop constraint fk_source_segments_recorded_verbal_origin_scope;
alter table public.source_segments drop column recorded_verbal_origin_id;
create unique index uq_source_segments_statement
    on public.source_segments (statement_id)
    where kind = 'recorded_verbal_statement';
"""

RECORDED_VERBAL_ORIGIN_SCHEMA_DOWN = """
drop table if exists public.recorded_verbal_origin_fact_digests;
drop table if exists public.recorded_verbal_origin_backfill_receipts;
drop table if exists public.recorded_verbal_origin_statements;
drop table if exists public.recorded_verbal_origins;
"""

APPEND_RECORDED_VERBAL_ORIGIN = """
create function public.append_recorded_verbal_origin(
    p_project_id bigint,
    p_recorded_by text,
    p_recorded_at timestamp with time zone,
    p_conversation_date date,
    p_exact_text text,
    p_content_sha256 character varying,
    p_corrects_origin_id bigint,
    p_legacy_statement_id bigint
) returns bigint
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            existing record;
            origin_id bigint;
        begin
            if p_recorded_by is null or length(trim(p_recorded_by)) = 0 then
                raise exception 'a recorded verbal origin needs its recorder'
                    using errcode = '23514';
            end if;
            if p_recorded_at is null then
                raise exception 'a recorded verbal origin needs its recorded time'
                    using errcode = '23514';
            end if;
            if p_exact_text is null or length(p_exact_text) = 0 then
                raise exception 'a recorded verbal origin needs the exact words'
                    using errcode = '23514';
            end if;
            if p_content_sha256 is distinct from
                encode(sha256(convert_to(p_exact_text, 'UTF8')), 'hex') then
                raise exception 'recorded verbal origin digest does not match its exact words'
                    using errcode = '23514';
            end if;
            if p_corrects_origin_id is not null and not exists (
                select 1 from recorded_verbal_origins
                 where id = p_corrects_origin_id and project_id = p_project_id
            ) then
                raise exception 'corrected recorded verbal origin is outside its project'
                    using errcode = '23514';
            end if;
            if p_legacy_statement_id is not null then
                if not exists (
                    select 1 from dependency_events
                     where id = p_legacy_statement_id and project_id = p_project_id
                ) then
                    raise exception 'recorded verbal statement is outside its project'
                        using errcode = '23514';
                end if;
                -- The dual-write replays the same act; the legacy mapping is
                -- the idempotency key while stages 1 through 5 still write it.
                select origin.id as id,
                       origin.recorded_by as recorded_by,
                       origin.recorded_at as recorded_at,
                       origin.conversation_date as conversation_date,
                       origin.content_sha256 as content_sha256,
                       origin.corrects_origin_id as corrects_origin_id
                  into existing
                  from recorded_verbal_origin_statements mapping
                  join recorded_verbal_origins origin
                    on origin.id = mapping.origin_id
                 where mapping.statement_id = p_legacy_statement_id;
                if found then
                    if existing.recorded_by <> p_recorded_by
                        or existing.recorded_at is distinct from p_recorded_at
                        or existing.conversation_date is distinct from p_conversation_date
                        or existing.content_sha256 <> p_content_sha256
                        or existing.corrects_origin_id is distinct from p_corrects_origin_id then
                        raise exception 'recorded verbal origin is already bound to different content'
                            using errcode = '23514';
                    end if;
                    return existing.id;
                end if;
            end if;
            insert into recorded_verbal_origins (
                project_id, recorded_by, recorded_at, conversation_date,
                exact_text, content_sha256, corrects_origin_id
            ) values (
                p_project_id, p_recorded_by, p_recorded_at, p_conversation_date,
                p_exact_text, p_content_sha256, p_corrects_origin_id
            ) returning id into origin_id;
            if p_legacy_statement_id is not null then
                insert into recorded_verbal_origin_statements (
                    origin_id, project_id, statement_id
                ) values (origin_id, p_project_id, p_legacy_statement_id);
            end if;
            return origin_id;
        end; $$;
"""

# The #492 command re-issued over the spine-native origin.  PostgreSQL will
# not rename an input parameter through `create or replace`, so the third
# parameter changes identity by a drop and a create; the signature, the owner,
# and the grants are unchanged.
APPEND_SOURCE_SEGMENTS_OVER_ORIGIN = """
create function public.append_source_segments(
    p_project_id bigint,
    p_document_id bigint,
    p_recorded_verbal_origin_id bigint,
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
            if (p_document_id is null) = (p_recorded_verbal_origin_id is null) then
                raise exception 'source segments belong to one document or one recorded verbal origin'
                    using errcode = '23514';
            end if;
            if p_document_id is not null and not exists (
                select 1 from documents
                 where id = p_document_id and project_id = p_project_id
            ) then
                raise exception 'source segment document is outside its project'
                    using errcode = '23514';
            end if;
            if p_recorded_verbal_origin_id is not null and not exists (
                select 1 from recorded_verbal_origins
                 where id = p_recorded_verbal_origin_id and project_id = p_project_id
            ) then
                raise exception 'source segment recorded verbal origin is outside its project'
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
                       and recorded_verbal_origin_id = p_recorded_verbal_origin_id;
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
                    project_id, document_id, recorded_verbal_origin_id, kind,
                    exact_text, content_sha256, ordinal, sheet_name, cell_range,
                    page_no, start_offset, end_offset
                ) values (
                    p_project_id, p_document_id, p_recorded_verbal_origin_id,
                    segment_kind, exact, digest, (item ->> 'ordinal')::integer,
                    item ->> 'sheet_name', item ->> 'cell_range',
                    (item ->> 'page_no')::integer,
                    (item ->> 'start_offset')::integer,
                    (item ->> 'end_offset')::integer
                ) returning id into segment_id;
                appended := appended || segment_id;
            end loop;
            return appended;
        end; $$;
"""

# The executor of the one backfill run, recorded on every receipt so the
# migration is attributable and never mistaken for the recorder who attested.
RECORDED_VERBAL_BACKFILL_EXECUTOR = "migration:b2d5f8a1c4e7/512"

# `source_segments` and `facts` refuse every UPDATE at runtime, which is the
# point of an append-only spine.  A schema transition that moves an identity is
# the one thing that cannot be expressed as an append, so the two row guards are
# lifted for the length of the backfill and restored immediately, and every row
# the window touched is named in a receipt.
_APPEND_ONLY_GUARDS = (
    ("source_segments", "trg_source_segments_append_only"),
    ("facts", "trg_facts_append_only"),
)


def _with_append_only_guards_lifted(bind, work) -> None:
    for table, trigger in _APPEND_ONLY_GUARDS:
        bind.execute(sa.text(f"alter table public.{table} disable trigger {trigger}"))
    try:
        work(bind)
    finally:
        for table, trigger in _APPEND_ONLY_GUARDS:
            bind.execute(
                sa.text(f"alter table public.{table} enable trigger {trigger}")
            )

# The Fact identity recipe, frozen at this revision.  `corridor.facts`
# computes the same bytes; the backfill reproduces each stored digest with it
# before it replaces one, so a recipe that has drifted refuses instead of
# rewriting.
_MATERIALIZED_SOURCE_LINKS = {
    "statement_wording": ("value_source", "attribution_source"),
    "statement_timing": ("value_source",),
    "applies_to": ("value_source",),
}


def _frozen_fact_digest(
    *,
    run_identity: dict,
    fact_type: str,
    subject_kind: str,
    subject_key: str,
    text_value: str | None,
    date_value: str | None,
    external_org_value_id: int | None,
    structured_value: object | None,
    source_segment_id: int,
) -> str:
    digest_input = {
        "run": run_identity,
        "fact_type": fact_type,
        "subject_kind": subject_kind,
        "subject_key": subject_key,
        "text_value": text_value,
        "date_value": date_value,
        "external_org_value_id": external_org_value_id,
        "structured_value": structured_value,
        "source_links": [
            {"role": role, "source_segment_id": source_segment_id}
            for role in _MATERIALIZED_SOURCE_LINKS[fact_type]
        ],
    }
    return sha256(
        json.dumps(digest_input, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _structured_value(bind, fact_type: str, fact_id: int):
    """Rebuild the typed satellite exactly as the Fact's replay reads it."""

    if fact_type == "statement_wording":
        return None
    if fact_type == "applies_to":
        members = bind.execute(
            sa.text(
                "select dependency_id from fact_applies_to "
                "where fact_id = :fact_id order by ordinal"
            ),
            {"fact_id": fact_id},
        ).scalars().all()
        return {"dependency_ids": [int(member) for member in members]}
    rows = bind.execute(
        sa.text(
            "select timing_role, text, precision, start_date, end_date "
            "from fact_statement_timings where fact_id = :fact_id "
            "order by timing_role"
        ),
        {"fact_id": fact_id},
    ).all()
    return {
        "timings": [
            {
                "role": row.timing_role,
                "text": row.text,
                "precision": row.precision,
                "start_date": row.start_date.isoformat()
                if row.start_date is not None
                else None,
                "end_date": row.end_date.isoformat()
                if row.end_date is not None
                else None,
            }
            for row in rows
        ]
    }


def _backfill_recorded_verbal_origins(bind) -> None:
    """Give every dual-written verbal one spine-native origin, or refuse.

    One legacy statement becomes exactly one origin, one compatibility
    mapping row, and one attributable receipt; every Fact the verbal already
    carried has its identity digest reproduced from the stored row before it
    is replaced, and both digests are recorded.  A row that cannot be
    reconciled aborts the whole transition rather than being guessed at.
    """

    segments = bind.execute(
        sa.text(
            "select segment.id as segment_id, segment.project_id as project_id, "
            "       segment.statement_id as statement_id, "
            "       statement.created_by as recorded_by, "
            "       statement.created_at as recorded_at, "
            "       statement.event_date as conversation_date, "
            "       segment.exact_text as exact_text, "
            "       segment.content_sha256 as content_sha256 "
            "  from source_segments segment "
            "  join dependency_events statement "
            "    on statement.id = segment.statement_id "
            " where segment.kind = 'recorded_verbal_statement' "
            " order by segment.id"
        )
    ).all()

    orphans = bind.execute(
        sa.text(
            "select count(*) from source_segments "
            " where kind = 'recorded_verbal_statement' and statement_id is null"
        )
    ).scalar_one()
    if orphans:
        raise RuntimeError(
            f"#512 backfill refuses: {orphans} recorded verbal segment(s) name "
            "no legacy statement, so no origin can be reconciled for them"
        )

    migrated_facts = 0
    for segment in segments:
        if segment.recorded_by is None or not segment.recorded_by.strip():
            raise RuntimeError(
                f"#512 backfill refuses: legacy statement {segment.statement_id} "
                "names no recorder, so its attestation cannot be reconstructed"
            )
        origin_id = bind.execute(
            sa.text(
                "insert into recorded_verbal_origins ("
                "project_id, recorded_by, recorded_at, conversation_date, "
                "exact_text, content_sha256"
                ") values ("
                ":project_id, :recorded_by, :recorded_at, :conversation_date, "
                ":exact_text, :content_sha256"
                ") returning id"
            ),
            {
                "project_id": segment.project_id,
                "recorded_by": segment.recorded_by,
                "recorded_at": segment.recorded_at,
                "conversation_date": segment.conversation_date,
                "exact_text": segment.exact_text,
                "content_sha256": segment.content_sha256,
            },
        ).scalar_one()
        bind.execute(
            sa.text(
                "insert into recorded_verbal_origin_statements ("
                "origin_id, project_id, statement_id"
                ") values (:origin_id, :project_id, :statement_id)"
            ),
            {
                "origin_id": origin_id,
                "project_id": segment.project_id,
                "statement_id": segment.statement_id,
            },
        )
        bind.execute(
            sa.text(
                "update source_segments set recorded_verbal_origin_id = :origin_id "
                " where id = :segment_id"
            ),
            {"origin_id": origin_id, "segment_id": segment.segment_id},
        )

        facts = bind.execute(
            sa.text(
                "select fact.id as id, fact.fact_type as fact_type, "
                "       fact.subject_kind as subject_kind, "
                "       fact.subject_key as subject_key, "
                "       fact.text_value as text_value, "
                "       fact.date_value as date_value, "
                "       fact.external_org_value_id as external_org_value_id, "
                "       fact.content_sha256 as content_sha256 "
                "  from facts fact "
                "  join fact_sources source on source.fact_id = fact.id "
                " where source.source_segment_id = :segment_id "
                "   and source.role = 'value_source' "
                " order by fact.id"
            ),
            {"segment_id": segment.segment_id},
        ).all()

        receipt_id = bind.execute(
            sa.text(
                "insert into recorded_verbal_origin_backfill_receipts ("
                "project_id, origin_id, legacy_statement_id, source_segment_id, "
                "fact_count, migration_revision, executed_by"
                ") values ("
                ":project_id, :origin_id, :statement_id, :segment_id, "
                ":fact_count, :revision, :executed_by"
                ") returning id"
            ),
            {
                "project_id": segment.project_id,
                "origin_id": origin_id,
                "statement_id": segment.statement_id,
                "segment_id": segment.segment_id,
                "fact_count": len(facts),
                "revision": revision,
                "executed_by": RECORDED_VERBAL_BACKFILL_EXECUTOR,
            },
        ).scalar_one()

        for fact in facts:
            if fact.fact_type not in _MATERIALIZED_SOURCE_LINKS:
                raise RuntimeError(
                    f"#512 backfill refuses: Fact {fact.id} of type "
                    f"{fact.fact_type!r} takes its value from a recorded verbal "
                    "segment, and this transition knows no identity recipe for it"
                )
            structured = _structured_value(bind, fact.fact_type, fact.id)
            shared = {
                "fact_type": fact.fact_type,
                "subject_kind": fact.subject_kind,
                "subject_key": fact.subject_key,
                "text_value": fact.text_value,
                "date_value": (
                    fact.date_value.isoformat()
                    if fact.date_value is not None
                    else None
                ),
                "external_org_value_id": fact.external_org_value_id,
                "structured_value": structured,
                "source_segment_id": segment.segment_id,
            }
            reproduced = _frozen_fact_digest(
                run_identity={"statement_id": segment.statement_id}, **shared
            )
            if reproduced != fact.content_sha256:
                raise RuntimeError(
                    f"#512 backfill refuses: Fact {fact.id} does not reproduce "
                    "its stored identity digest from its stored row, so its "
                    "spine-native digest cannot be derived"
                )
            replacement = _frozen_fact_digest(
                run_identity={"recorded_verbal_origin_id": origin_id}, **shared
            )
            bind.execute(
                sa.text(
                    "update facts set content_sha256 = :digest where id = :fact_id"
                ),
                {"digest": replacement, "fact_id": fact.id},
            )
            bind.execute(
                sa.text(
                    "insert into recorded_verbal_origin_fact_digests ("
                    "project_id, receipt_id, fact_id, prior_content_sha256, "
                    "content_sha256"
                    ") values ("
                    ":project_id, :receipt_id, :fact_id, :prior, :digest)"
                ),
                {
                    "project_id": segment.project_id,
                    "receipt_id": receipt_id,
                    "fact_id": fact.id,
                    "prior": fact.content_sha256,
                    "digest": replacement,
                },
            )
            migrated_facts += 1

    counted = bind.execute(
        sa.text(
            "select (select count(*) from source_segments "
            "         where kind = 'recorded_verbal_statement') as segments, "
            "       (select count(*) from source_segments "
            "         where kind = 'recorded_verbal_statement' "
            "           and recorded_verbal_origin_id is not null) as pointed, "
            "       (select count(*) from recorded_verbal_origins) as origins, "
            "       (select count(*) from recorded_verbal_origin_statements) "
            "           as mappings, "
            "       (select count(*) from "
            "         recorded_verbal_origin_backfill_receipts) as receipts, "
            "       (select coalesce(sum(fact_count), 0) from "
            "         recorded_verbal_origin_backfill_receipts) as counted_facts, "
            "       (select count(*) from recorded_verbal_origin_fact_digests) "
            "           as digests"
        )
    ).one()
    expected = len(segments)
    if (
        counted.segments != expected
        or counted.pointed != expected
        or counted.origins != expected
        or counted.mappings != expected
        or counted.receipts != expected
        or counted.counted_facts != migrated_facts
        or counted.digests != migrated_facts
    ):
        raise RuntimeError(
            "#512 backfill refuses: the reconciliation is not one-to-one — "
            f"{expected} recorded verbal segment(s) and {migrated_facts} Fact(s) "
            f"produced {counted.pointed} pointed segment(s), {counted.origins} "
            f"origin(s), {counted.mappings} mapping(s), {counted.receipts} "
            f"receipt(s), {counted.counted_facts} counted Fact(s), and "
            f"{counted.digests} digest receipt(s)"
        )


def _restore_legacy_recorded_verbal_state(bind) -> None:
    """Put the legacy statement lineage and Fact identities back, or refuse.

    The downgrade is exact, not best effort: a verbal whose origin carries no
    legacy mapping has no statement to go back to, and a Fact identity is
    restored only from the receipt that recorded what the backfill replaced.
    """

    unmappable = bind.execute(
        sa.text(
            "select count(*) from source_segments segment "
            " where segment.kind = 'recorded_verbal_statement' "
            "   and not exists ("
            "       select 1 from recorded_verbal_origin_statements mapping "
            "        where mapping.origin_id = segment.recorded_verbal_origin_id)"
        )
    ).scalar_one()
    if unmappable:
        raise RuntimeError(
            f"#512 downgrade refuses: {unmappable} recorded verbal segment(s) "
            "have a spine-native origin with no legacy statement to return to"
        )
    bind.execute(
        sa.text(
            "update source_segments segment "
            "   set statement_id = mapping.statement_id "
            "  from recorded_verbal_origin_statements mapping "
            " where segment.recorded_verbal_origin_id = mapping.origin_id"
        )
    )
    bind.execute(
        sa.text(
            "update facts set content_sha256 = receipt.prior_content_sha256 "
            "  from recorded_verbal_origin_fact_digests receipt "
            " where facts.id = receipt.fact_id "
            "   and facts.content_sha256 = receipt.content_sha256"
        )
    )


# --- Permanent-state de-duplication (#457, ADR-0081, ADR-0083) -------------
#
# Every family above already knows what makes a row the same row.  What most
# of them do not have is that knowledge in *permanent state*: the identity is
# derived where the row is written — inside a ``SECURITY DEFINER`` command's
# body, or in the Python that calls it — and the table itself would accept a
# second copy.  A rule that lives in a writer holds only for as long as every
# writer remembers it, and ADR-0081's convergence is a claim about the record,
# not about today's call graph.  Each identity below therefore becomes a
# constraint, so a duplicate stops being unlikely and becomes unrepresentable.
#
#   * **Source Facts.**  ``append_fact`` refuses a Fact with no digest and
#     returns the existing row when one already carries it, but
#     ``facts.content_sha256`` was nullable behind a *partial* unique index.
#     A Fact with no identity was representable, and two of them were
#     indistinguishable.  The column becomes ``not null`` and the index
#     becomes a total unique constraint: every Fact has an identity, and it is
#     its own.
#
#   * **Proposed Deltas.**  ``proposed_deltas.content_sha256`` was already
#     unique, and the project is inside the digest, so the occurrence was
#     safe.  Its *group* was not: ``append_proposed_deltas`` inserted a fresh
#     ``delta_groups`` row on every call, so replaying one source version —
#     which ``delta_generation`` does on every budgeted batch and after every
#     crash — left another group behind even when it appended no delta at all.
#     ADR-0075 makes a group one atomic source change, so its identity is the
#     source version it came from, and the command now converges on the group
#     that version already opened.
#
#   * **Decisions.**  ``delta_record_decisions``, the Review Packet receipt,
#     the Follow-up Plan, and the Undo already carry ``(project_id,
#     idempotency_key)``; ``fact_decisions`` did not, because its identity is
#     its revision's.  Nothing said a revision decides a Fact once, though, so
#     ``include_structured_cell_fact_decision``'s replay read
#     (``where revision_id = …``) was reading a set it assumed was a row.  The
#     dated Work List deferral of ADR-0084 had no identity at all: it is
#     scheduling rather than a record decision, so it carries no idempotency
#     key, and a retried Defer wrote a second receipt for the same act.  Its
#     natural key is the one it already stores — the delta, the instant it was
#     scheduled at, and the person who scheduled it.
#
#   * **Project Record revisions.**  ``(project_id, idempotency_key)`` was
#     already unique and correctly scoped.  What was missing is that the key
#     had to *be* one: every command refuses a blank key in its own body, and
#     the column accepted ``''``, which is the same non-identity for all of
#     them.
#
#   * **Connector deliveries.**  ``push_deliveries.idempotency_key`` was
#     unique, but it was computed in Python and the database never checked it
#     against the row it was stored on, so a writer that derived it wrongly —
#     or not at all — defeated the dedup while satisfying the constraint.  The
#     envelope's structural key ``(project_id, delivery_identity,
#     content_sha256)`` becomes unique in its own right, and a trigger
#     re-derives ADR-0083's two digests from the row's own columns and the
#     bound project's slug and refuses a row whose identity is not its own.
#
# What was considered and rejected: repairing existing duplicates.  A
# constraint added over data that violates it must either fail or change the
# record, and merging two Source Facts or two decisions is a semantic act no
# migration has the authority to perform (ADR-0080 as amended by ADR-0083
# governs disposition; nothing here disposes of anything).  The transition
# therefore counts what it cannot represent and refuses, naming the family, in
# the same shape #512's backfill uses.

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
        "Work List deferrals repeating one scheduling act",
        "select coalesce(sum(extra), 0) from ("
        "  select count(*) - 1 as extra from delta_deferrals"
        "   group by delta_id, deferred_at, scheduled_by_principal"
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
    add constraint uq_delta_deferrals_occurrence
    unique (delta_id, deferred_at, scheduled_by_principal);

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
"""

DEDUPLICATED_IDENTITIES_DOWN = """
drop trigger if exists trg_push_deliveries_identity on public.push_deliveries;
drop function if exists public.enforce_push_delivery_identity() cascade;
alter table public.push_deliveries
    drop constraint if exists uq_push_deliveries_envelope;
alter table public.project_record_revisions
    drop constraint if exists ck_project_record_revisions_idempotency_key;
alter table public.delta_deferrals
    drop constraint if exists uq_delta_deferrals_occurrence;
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
            -- Scheduling writes no revision (ADR-0084), so the act carries no
            -- idempotency key of its own; the delta, the instant it was
            -- scheduled at, and the person who scheduled it are its identity,
            -- and a retry returns the receipt already written (#457).
            select id into deferral_id from delta_deferrals
             where delta_id = p_delta_id
               and deferred_at = p_deferred_at
               and scheduled_by_principal = p_principal;
            if found then
                return deferral_id;
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


# --- One delivery ledger for both transports (#599, ADR-0089) ---------------
#
# Folded into this transition for the same window reason as the blocks above.
#
# ADR-0083 declared the ``SourceEnvelope`` the one normalized ingress record
# every channel produces, pulled or pushed.  The implementation did not come
# out that way: the #511 block above persists the push half and #496 built the
# pull half with no delivery record at all, so "did we take delivery of this
# external version" was a database read on one transport and a re-listing of
# the customer's own system on the other, and a delivery the intake gate (#490)
# refused was recorded for push and lost for pull.  ADR-0089 calls that an
# accidental implementation artifact rather than a domain distinction.
#
# ``push_deliveries`` therefore *becomes* the shared family rather than gaining
# a sibling beside it: a second table would make the asymmetry permanent and
# leave every reader — the intake operator's "what arrived this week", the
# shadow comparison of #499, the disposition inventory of #514, the analytics
# contract of #558 — to union two tables and reconcile two identity rules.  The
# rename carries every existing row and every foreign key that names it, which
# is what makes the reconciliation exact rather than a copy somebody has to
# check.  It is checked anyway, below.
#
# The identity gains the disposition and nothing else.  A delivery may be
# stored and later found duplicate, or refused and never stored, and each of
# those is one outcome of the same delivery; what may not happen is the same
# outcome of the same delivery twice.  ``uq_push_delivery_idempotency`` is
# retired rather than widened: #457's own finding was that a key computed in
# Python and never checked against its row constrains only the writers that
# remember to compute it, and the structural key is what does the work.  The
# key itself stays, and the trigger that re-derives ADR-0083's two digests from
# the row's own columns stays with it, now under the family's name.
#
# ``connector_checkpoint_advances`` takes the cursor off the Due Work receipt.
# #488 kept it there deliberately — this file's own predecessor rejected a
# checkpoint table because the window was closed — and mitigated the
# consequence by retaining those receipts for 3650 days.  Retention is not
# identity: a receipt sweep, a retention-policy change, a disposition under
# ADR-0080, or an ordinary cleanup would reset a live connector's external
# cursor or land it on a stale token.  The advance relation is append-only, the
# current checkpoint is derived from its newest row, and deleting a receipt can
# no longer move anything.
#
# ``connector_checkpoint_advance_deliveries`` is where ADR-0089's checkpoint
# rule is enforced rather than remembered.  An advance may cover a ``stored``
# or ``duplicate`` delivery freely; it may cover a ``terminally_refused`` or
# ``quarantined`` one only because the digest and the refusal evidence are
# durably here, which is what makes advancing past bytes nobody will ever store
# safe; and it may never cover a ``transient_failure``, because a scanner that
# timed out, an object store that rejected a write, or a provider that returned
# a 500 has said nothing about the delivery, and advancing past it drops a
# source revision silently.
UNIFIED_DELIVERY_SCHEMA = """
alter table public.push_deliveries rename to source_deliveries;

alter sequence public.push_deliveries_id_seq rename to source_deliveries_id_seq;

alter index public.ix_push_deliveries_project_id
    rename to ix_source_deliveries_project_id;

alter table public.source_deliveries
    rename constraint push_deliveries_pkey to source_deliveries_pkey;

alter table public.source_deliveries
    rename constraint push_deliveries_credential_id_fkey
    to source_deliveries_credential_id_fkey;

alter table public.source_deliveries
    rename constraint push_deliveries_project_id_fkey
    to source_deliveries_project_id_fkey;

alter table public.source_deliveries
    rename constraint ck_push_delivery_content_sha256
    to ck_source_delivery_content_sha256;

alter table public.source_deliveries
    rename constraint ck_push_delivery_identity to ck_source_delivery_identity;

alter table public.source_deliveries
    rename constraint ck_push_delivery_idempotency
    to ck_source_delivery_idempotency;

alter table public.source_deliveries
    drop constraint uq_push_delivery_idempotency;

alter table public.source_deliveries
    drop constraint uq_push_deliveries_envelope;

alter table public.source_deliveries
    add column transport character varying(8) not null default 'push',
    add column configuration_identity text not null default '',
    add column configuration_version text not null default '',
    add column service_identity text not null default '',
    add column run_identity text not null default '',
    add column disposition character varying(24) not null default 'stored',
    add column refusal_reason text;

-- Every existing row is a pushed delivery whose bytes were taken, bound by
-- the credential that admitted it, and recorded by this transition rather
-- than by a run that ever existed.  Nothing is invented that the row does not
-- already say.
update public.source_deliveries
   set configuration_identity = 'credential:' || credential_id,
       service_identity = 'corridor.push_intake',
       run_identity = 'migration:b2d5f8a1c4e7';

alter table public.source_deliveries
    alter column transport drop default,
    alter column configuration_identity drop default,
    alter column service_identity drop default,
    alter column run_identity drop default,
    alter column disposition drop default;

alter table public.source_deliveries alter column credential_id drop not null;

alter table public.source_deliveries
    add constraint uq_source_deliveries_observation
    unique (project_id, delivery_identity, content_sha256, disposition);

alter table public.source_deliveries
    add constraint ck_source_delivery_transport
        check (transport in ('pull', 'push')),
    add constraint ck_source_delivery_disposition
        check (disposition in ('stored', 'duplicate', 'quarantined',
                               'terminally_refused', 'transient_failure')),
    add constraint ck_source_delivery_push_credential
        check ((transport = 'push') = (credential_id is not null)),
    add constraint ck_source_delivery_configuration
        check (length(btrim(configuration_identity)) > 0),
    add constraint ck_source_delivery_run
        check (length(btrim(service_identity)) > 0
               and length(btrim(run_identity)) > 0),
    add constraint ck_source_delivery_refusal_reason
        check ((disposition in ('stored', 'duplicate'))
               = (refusal_reason is null));

drop trigger trg_push_deliveries_identity on public.source_deliveries;

drop function public.enforce_push_delivery_identity();

create function public.enforce_source_delivery_identity() returns trigger
    language plpgsql
    as $$
        declare
            bound_slug text;
            derived_identity text;
            derived_key text;
        begin
            select slug into bound_slug from projects where id = new.project_id;
            if bound_slug is null then
                raise exception 'source_delivery:unbound_delivery a delivery names a project that does not exist'
                    using errcode='23514';
            end if;
            derived_identity := encode(sha256(convert_to(concat_ws(':',
                new.customer, bound_slug, new.channel,
                new.external_identity, new.external_version), 'UTF8')), 'hex');
            derived_key := encode(sha256(convert_to(concat_ws(':',
                derived_identity, new.content_sha256), 'UTF8')), 'hex');
            if new.delivery_identity is distinct from derived_identity then
                raise exception 'source_delivery:delivery_identity a delivery identity is derived from the binding and the transport, never supplied'
                    using errcode='23514';
            end if;
            if new.idempotency_key is distinct from derived_key then
                raise exception 'source_delivery:idempotency_key a delivery idempotency key is derived from its identity and its bytes, never supplied'
                    using errcode='23514';
            end if;
            return new;
        end; $$;

create trigger trg_source_deliveries_identity
    before insert on public.source_deliveries
    for each row execute function public.enforce_source_delivery_identity();

create table public.connector_checkpoint_advances (
    id bigserial primary key,
    project_id bigint not null references public.projects(id),
    schedule_id bigint not null references public.due_work_schedules(id),
    configuration_identity text not null,
    configuration_version text not null default '',
    channel character varying(32) not null,
    checkpoint_token text not null,
    service_identity text not null,
    run_identity text not null,
    advanced_at timestamp with time zone not null default now(),
    constraint uq_connector_checkpoint_advance
        unique (schedule_id, run_identity, checkpoint_token),
    constraint ck_connector_checkpoint_token
        check (length(btrim(checkpoint_token)) > 0),
    constraint ck_connector_checkpoint_run
        check (length(btrim(service_identity)) > 0
               and length(btrim(run_identity)) > 0)
);

create index ix_connector_checkpoint_advances_project_id
    on public.connector_checkpoint_advances (project_id);

create index ix_connector_checkpoint_advances_schedule_id
    on public.connector_checkpoint_advances (schedule_id);

create table public.connector_checkpoint_advance_deliveries (
    id bigserial primary key,
    advance_id bigint not null
        references public.connector_checkpoint_advances(id),
    delivery_id bigint not null references public.source_deliveries(id),
    constraint uq_connector_checkpoint_advance_delivery
        unique (advance_id, delivery_id)
);

create index ix_connector_checkpoint_advance_deliveries_advance_id
    on public.connector_checkpoint_advance_deliveries (advance_id);

create function public.enforce_checkpoint_advance_coverage() returns trigger
    language plpgsql
    as $$
        declare
            covered public.source_deliveries%rowtype;
        begin
            select * into covered from source_deliveries
             where id = new.delivery_id;
            if covered.disposition = 'transient_failure' then
                raise exception 'source_delivery:transient_failure a checkpoint never advances past a delivery one attempt failed to take'
                    using errcode='23514';
            end if;
            if covered.disposition in ('terminally_refused', 'quarantined')
               and coalesce(btrim(covered.refusal_reason), '') = '' then
                raise exception 'source_delivery:missing_refusal_evidence a refused delivery is advanced past only on its recorded evidence'
                    using errcode='23514';
            end if;
            if covered.disposition = 'quarantined'
               and coalesce(btrim(covered.bytes_reference), '') = '' then
                raise exception 'source_delivery:missing_quarantine_reference a quarantined delivery is advanced past only where its bytes are held'
                    using errcode='23514';
            end if;
            return new;
        end; $$;

create trigger trg_connector_checkpoint_advance_coverage
    before insert on public.connector_checkpoint_advance_deliveries
    for each row execute function public.enforce_checkpoint_advance_coverage();
"""

UNIFIED_DELIVERY_SCHEMA_DOWN = """
drop trigger if exists trg_connector_checkpoint_advance_coverage
    on public.connector_checkpoint_advance_deliveries;

drop function if exists public.enforce_checkpoint_advance_coverage() cascade;

drop table if exists public.connector_checkpoint_advance_deliveries;

drop table if exists public.connector_checkpoint_advances;

alter table public.source_deliveries
    drop constraint if exists ck_source_delivery_refusal_reason,
    drop constraint if exists ck_source_delivery_run,
    drop constraint if exists ck_source_delivery_configuration,
    drop constraint if exists ck_source_delivery_push_credential,
    drop constraint if exists ck_source_delivery_disposition,
    drop constraint if exists ck_source_delivery_transport,
    drop constraint if exists uq_source_deliveries_observation;

alter table public.source_deliveries
    drop column if exists refusal_reason,
    drop column if exists disposition,
    drop column if exists run_identity,
    drop column if exists service_identity,
    drop column if exists configuration_version,
    drop column if exists configuration_identity,
    drop column if exists transport;

alter table public.source_deliveries alter column credential_id set not null;

alter table public.source_deliveries
    rename constraint ck_source_delivery_idempotency
    to ck_push_delivery_idempotency;

alter table public.source_deliveries
    rename constraint ck_source_delivery_identity to ck_push_delivery_identity;

alter table public.source_deliveries
    rename constraint ck_source_delivery_content_sha256
    to ck_push_delivery_content_sha256;

alter table public.source_deliveries
    rename constraint source_deliveries_project_id_fkey
    to push_deliveries_project_id_fkey;

alter table public.source_deliveries
    rename constraint source_deliveries_credential_id_fkey
    to push_deliveries_credential_id_fkey;

alter table public.source_deliveries
    rename constraint source_deliveries_pkey to push_deliveries_pkey;

alter index public.ix_source_deliveries_project_id
    rename to ix_push_deliveries_project_id;

alter sequence public.source_deliveries_id_seq rename to push_deliveries_id_seq;

alter table public.source_deliveries rename to push_deliveries;

-- The guard is renamed rather than rebuilt: it re-derives ADR-0083's digests
-- from the row's own columns and that is unchanged by this block, so the
-- #457 block below finds exactly the trigger and function it created.
alter function public.enforce_source_delivery_identity()
    rename to enforce_push_delivery_identity;

alter trigger trg_source_deliveries_identity on public.push_deliveries
    rename to trg_push_deliveries_identity;

alter table public.push_deliveries
    add constraint uq_push_delivery_idempotency unique (idempotency_key);

alter table public.push_deliveries
    add constraint uq_push_deliveries_envelope
    unique (project_id, delivery_identity, content_sha256);
"""

# The tables the runtime capabilities append to and never rewrite, on the same
# terms the #511 block set for the delivery ledger it renames: a delivery, an
# advance, and the coverage that makes the advance safe are records of what
# happened, so no runtime capability may edit or erase one.
UNIFIED_DELIVERY_TABLES = (
    "connector_checkpoint_advances",
    "connector_checkpoint_advance_deliveries",
)


def _refuse_unrepresentable_delivery_downgrade(bind) -> None:
    """Refuse rather than lose a delivery the push-only shape cannot hold.

    The unified family records pulled deliveries and refused ones; the push
    ledger this downgrade restores can express neither, and dropping the rows
    that do not fit is exactly the loss ADR-0089 set out to remove.  The same
    discipline as #512's downgrade: state what cannot be carried back, and stop.
    """

    blocked = bind.execute(
        sa.text(
            "select count(*) from source_deliveries "
            " where transport <> 'push' or disposition <> 'stored'"
        )
    ).scalar_one()
    if blocked:
        raise RuntimeError(
            f"#599 downgrade refuses: {blocked} delivery row(s) are pulled, "
            "refused, or failed, and the push-only ledger cannot represent "
            "them. Nothing is merged or dropped here."
        )


def _push_delivery_snapshot(bind) -> tuple[tuple, ...]:
    """Every push delivery's identity, read before the family is established."""

    return tuple(
        tuple(row)
        for row in bind.execute(
            sa.text(
                "select id, project_id, delivery_identity, idempotency_key, "
                "       content_sha256, credential_id "
                "  from push_deliveries order by id"
            )
        ).all()
    )


def _reconcile_unified_delivery_family(bind, snapshot) -> None:
    """Prove the renamed family carries every push delivery exactly, or refuse.

    The rename is exact by construction, which is why it was chosen over a copy
    — but "by construction" is the claim and not the proof, and both #512's
    backfill and #457's detections established that a transition states what it
    expects and aborts when the database disagrees.  Nothing is merged or
    dropped here: a mismatch means the unified family cannot be established over
    these rows, and which row is the record is a question only a person can
    answer.
    """

    migrated = tuple(
        tuple(row)
        for row in bind.execute(
            sa.text(
                "select id, project_id, delivery_identity, idempotency_key, "
                "       content_sha256, credential_id "
                "  from source_deliveries order by id"
            )
        ).all()
    )
    if migrated != snapshot:
        raise RuntimeError(
            "#599 delivery-family migration refuses: the unified family does "
            f"not reproduce the {len(snapshot)} push delivery row(s) it was "
            f"established over — {len(migrated)} row(s) came back and their "
            "identities do not match. Nothing is merged or dropped here: "
            "reconcile the deliveries as an attributable act first."
        )
    unmigrated = bind.execute(
        sa.text(
            "select count(*) from source_deliveries "
            " where transport <> 'push' or disposition <> 'stored' "
            "    or credential_id is null "
            "    or length(btrim(configuration_identity)) = 0"
        )
    ).scalar_one()
    if unmigrated:
        raise RuntimeError(
            f"#599 delivery-family migration refuses: {unmigrated} existing "
            "delivery row(s) did not take the pushed, stored, credential-bound "
            "identity every one of them already had."
        )


# --- #610 The stored mapping-revision declaration (ADR-0076, ADR-0083) ------
#
# #597 made a versioned semantic-mapping manifest the authority a template is
# read through, and recorded only its identity, version and digest on the
# registration.  That proves *which* revision a render was performed under and
# not *what* that revision declared, so reproducing a past render depended on
# whoever declared it still holding the declaration.  For a record whose claim
# is auditability that is a gap, not a convenience: a digest nobody can resolve
# is weaker evidence than it looks.
#
# ``project_baseline_format_manifests`` stores the declaration itself, as the
# exact canonical bytes the digest is taken over — the same shape as a Source
# Segment, which retains exact text beside its digest rather than the digest
# alone.  A ``jsonb`` column was rejected for that reason: PostgreSQL
# normalizes key order, whitespace and numbers, so the bytes it gave back would
# no longer be the bytes anyone digested, and the digest could not be checked
# against them at all.
#
# Two constraints make a stored declaration that disagrees with its
# registration unrepresentable rather than merely unlikely.  The check
# constraint recomputes SHA-256 over the stored bytes and requires the row's
# own ``content_sha256``; the composite foreign key requires that identity,
# version and digest to be the registration's.  Together the stored bytes
# digest to exactly what the registration records, and no command, trigger or
# review has to be trusted for it.
#
# The row is keyed by the registration's own id, so a stored declaration cannot
# outlive or precede the act that registered it, and a registration with no row
# here has no stored declaration — an explicit absence, never an empty
# manifest.  ``attach_baseline_format_manifest`` is the one writer, owned by
# the record-decision role like every other write to this family, and a replay
# converges on the row already stored instead of writing a second one.

BASELINE_FORMAT_MANIFEST_TABLES = ("project_baseline_format_manifests",)

BASELINE_FORMAT_MANIFEST_SCHEMA = """
alter table public.project_baseline_formats
    add constraint uq_project_baseline_formats_revision
    unique (id, project_id, format_identity, format_version, content_sha256);

create table public.project_baseline_format_manifests (
    format_id bigint primary key,
    project_id bigint not null,
    format_identity character varying(160) not null,
    format_version character varying(64) not null,
    content_sha256 character varying(64) not null,
    manifest_schema_version character varying(64) not null,
    -- The exact canonical bytes the digest is taken over, not a re-encoding
    -- of them: a digest that cannot be recomputed over what is stored is the
    -- unresolvable digest this block exists to remove.
    declaration text not null,
    constraint fk_project_baseline_format_manifests_registration foreign key
        (format_id, project_id, format_identity, format_version, content_sha256)
        references public.project_baseline_formats
        (id, project_id, format_identity, format_version, content_sha256),
    constraint ck_project_baseline_format_manifests_digest check (
        encode(sha256(convert_to(declaration, 'utf8')), 'hex') = content_sha256
    ),
    constraint ck_project_baseline_format_manifests_schema check (
        length(btrim(manifest_schema_version)) > 0
    )
);

create index ix_project_baseline_format_manifests_project_id
    on public.project_baseline_format_manifests (project_id);
create index ix_project_baseline_format_manifests_revision
    on public.project_baseline_format_manifests
    (format_identity, format_version, content_sha256);

create function public.enforce_project_baseline_format_manifest_write()
    returns trigger
    language plpgsql
    as $$
        begin
            if current_user <> 'corridor_fact_decision_writer' then
                raise exception 'a stored mapping revision is written only by the typed registration command'
                    using errcode='23514';
            end if;
            if tg_op <> 'INSERT' then
                raise exception 'a stored mapping revision is immutable: a changed declaration is a new registration, never an edit'
                    using errcode='23514';
            end if;
            return new;
        end; $$;

create trigger trg_project_baseline_format_manifests_write
    before insert or update or delete
    on public.project_baseline_format_manifests
    for each row
    execute function public.enforce_project_baseline_format_manifest_write();
create trigger trg_project_baseline_format_manifests_truncate
    before truncate on public.project_baseline_format_manifests
    for each statement
    execute function public.enforce_project_baseline_format_manifest_write();
"""

BASELINE_FORMAT_MANIFEST_SCHEMA_DOWN = """
drop table if exists public.project_baseline_format_manifests cascade;
drop function if exists
    public.enforce_project_baseline_format_manifest_write() cascade;
alter table public.project_baseline_formats
    drop constraint if exists uq_project_baseline_formats_revision;
"""

ATTACH_BASELINE_FORMAT_MANIFEST = """
create function public.attach_baseline_format_manifest(
    p_project_id bigint,
    p_format_id bigint,
    p_declaration text
) returns jsonb
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            registration project_baseline_formats%ROWTYPE;
            stored project_baseline_format_manifests%ROWTYPE;
            declared_digest character varying(64);
            declared_schema character varying(64);
        begin
            select * into registration from project_baseline_formats
             where id = p_format_id and project_id = p_project_id;
            if not found then
                raise exception 'format registration % is not a registration of project %', p_format_id, p_project_id
                    using errcode='23514';
            end if;
            if registration.format_kind <> 'field_mapping' then
                raise exception 'only a field mapping is registered as a declared mapping revision'
                    using errcode='23514';
            end if;
            declared_digest := encode(
                sha256(convert_to(p_declaration, 'utf8')), 'hex'
            );
            if declared_digest is distinct from registration.content_sha256 then
                raise exception 'this declaration digests to % and the registration records %', declared_digest, registration.content_sha256
                    using errcode='23514';
            end if;
            select * into stored from project_baseline_format_manifests
             where format_id = p_format_id;
            if found then
                -- The digest above already proved these are the same bytes,
                -- so a replay converges rather than storing a second copy.
                return jsonb_build_object(
                    'format_id', p_format_id, 'created', false
                );
            end if;
            -- An invalid or non-object declaration refuses here: the cast
            -- raises, or the schema version reads back null.
            declared_schema := (p_declaration::jsonb)->>'schema_version';
            if declared_schema is null
               or length(btrim(declared_schema)) = 0 then
                raise exception 'a stored mapping revision names the manifest schema it was written under'
                    using errcode='23514';
            end if;
            insert into project_baseline_format_manifests (
                format_id, project_id, format_identity, format_version,
                content_sha256, manifest_schema_version, declaration
            ) values (
                p_format_id, registration.project_id,
                registration.format_identity, registration.format_version,
                registration.content_sha256, declared_schema, p_declaration
            );
            return jsonb_build_object('format_id', p_format_id, 'created', true);
        end; $$;
"""

ATTACH_BASELINE_FORMAT_MANIFEST_SIGNATURE = "(bigint, bigint, text)"


def _refuse_unrepresentable_manifest_downgrade(bind) -> None:
    """Refuse rather than drop a declaration the digest-only shape cannot hold.

    The registration this downgrade restores carries a mapping revision's
    identity, version and digest and nothing else, so every stored declaration
    would go silently, leaving exactly the unresolvable digests #610 removed.
    The same discipline as #512's and #599's downgrades: state what cannot be
    carried back, and stop.
    """

    stored = bind.execute(
        sa.text(
            "select count(*) from information_schema.tables "
            " where table_schema = 'public' "
            "   and table_name = 'project_baseline_format_manifests'"
        )
    ).scalar_one()
    if not stored:
        return
    blocked = bind.execute(
        sa.text("select count(*) from project_baseline_format_manifests")
    ).scalar_one()
    if blocked:
        raise RuntimeError(
            f"#610 downgrade refuses: {blocked} registered mapping revision(s) "
            "store their full declaration, and the identity-and-digest-only "
            "registration cannot represent it. Nothing is dropped here."
        )


# --- #602 Report Runs bound to the accepted revision (ADR-0075, ADR-0086) ---
#
# A ``report_runs`` row and a ``scheduled_report_publications`` row each carry
# a ``snapshot_json`` copy of the state the report was published against, and
# nothing saying *which* accepted Project Record revision that state was.  A
# copy with no reference beside it is a second source of truth: it can only be
# compared with itself, it cannot be rebuilt, and when it drifts from the
# record nothing in the schema notices.  #518, #599, #604 and #610 removed the
# same shape elsewhere; this block removes it here.
#
# ``revision_id`` is the reference.  It is the accepted revision the reading
# was taken against — the same watermark #488's weekly reading already states
# its counts against, and the same one #534 freezes an issue on — so the two
# surfaces name one revision rather than each holding a private copy.
#
# The column is nullable, and deliberately so: historical rows were written
# before the binding existed and this transition rewrites none of them.  What
# a nullable column must not become is an optional binding, so a trigger
# carries the rule the column cannot.  A new row for a project that has an
# accepted revision must name one; only a project with no accepted revision at
# all writes none, which is an explicit and checked absence rather than a
# missing value.  Refusing every unbound insert outright was rejected for that
# case alone: a legacy project whose accepted record is not on the spine has no
# revision identity to name, and inventing one — a zero, the project id, the
# newest revision of some other project — would be exactly the false reference
# this block exists to prevent.
#
# The composite foreign key does the other half.  ``project_record_revisions``
# gains a ``(id, project_id)`` unique key, and each binding resolves against
# it, so a run bound to another project's revision is unrepresentable rather
# than merely unlikely.  The two guards are independent: a null binding is
# refused only by the trigger, and a foreign binding only by the key.
#
# ``snapshot_json`` stays, demoted to a rebuildable compatibility cache and
# labelled as one in the database itself.  Removing it or giving it an expiry
# rule waits on the semantic-equivalence proof (#603): the legacy diff reads
# it today, and dropping a cache before proving what rebuilds it is how a
# weekly report starts reporting a change that did not happen.
#
# **That demotion was wrong, and the #633 block at the end of this file
# corrects it.**  #603 measured what a revision can rebuild and found three
# things it cannot answer at all, so the column is the immutable Report
# Reading payload of a dated occurrence rather than a cache of anything
# (ADR-0092).  This paragraph is left as written because it is why the
# ``revision_id`` binding below has the shape it has; the column comment it
# describes is replaced later in the same transition.

REPORT_REVISION_BINDING_SCHEMA = """
alter table public.project_record_revisions
    add constraint uq_project_record_revisions_project_revision
    unique (id, project_id);

alter table public.report_runs add column revision_id bigint;
alter table public.report_runs
    add constraint fk_report_runs_revision
    foreign key (revision_id, project_id)
    references public.project_record_revisions (id, project_id);
create index ix_report_runs_revision_id
    on public.report_runs (revision_id);

alter table public.scheduled_report_publications add column revision_id bigint;
alter table public.scheduled_report_publications
    add constraint fk_scheduled_report_publications_revision
    foreign key (revision_id, project_id)
    references public.project_record_revisions (id, project_id);
create index ix_scheduled_report_publications_revision_id
    on public.scheduled_report_publications (revision_id);

comment on column public.report_runs.snapshot_json is
    'Rebuildable compatibility cache (#602). The authority is revision_id; '
    'this copy is retained only until #603 proves what rebuilds it.';
comment on column public.scheduled_report_publications.snapshot_json is
    'Rebuildable compatibility cache (#602). The authority is revision_id; '
    'this copy is retained only until #603 proves what rebuilds it.';

create function public.enforce_report_revision_binding()
    returns trigger
    language plpgsql
    as $$
        begin
            if tg_op = 'UPDATE'
               and old.revision_id is not null
               and new.revision_id is distinct from old.revision_id then
                raise exception 'the accepted revision a % was produced against is not rewritten', tg_table_name
                    using errcode='23514';
            end if;
            if tg_op = 'INSERT'
               and new.revision_id is null
               and exists (
                   select 1 from project_record_revisions
                    where project_id = new.project_id
               ) then
                raise exception 'a new % names the accepted Project Record revision it was produced against', tg_table_name
                    using errcode='23514';
            end if;
            return new;
        end; $$;

-- report_runs is an ordinary mutable relation, so the update arm has work to
-- do here.  scheduled_report_publications is already append-only by its own
-- trigger, so this one only has to guard the insert.
create trigger trg_report_runs_revision_binding
    before insert or update on public.report_runs
    for each row
    execute function public.enforce_report_revision_binding();
create trigger trg_scheduled_report_publications_revision_binding
    before insert on public.scheduled_report_publications
    for each row
    execute function public.enforce_report_revision_binding();
"""

REPORT_REVISION_BINDING_SCHEMA_DOWN = """
drop trigger if exists trg_scheduled_report_publications_revision_binding
    on public.scheduled_report_publications;
drop trigger if exists trg_report_runs_revision_binding on public.report_runs;
drop function if exists public.enforce_report_revision_binding() cascade;

comment on column public.scheduled_report_publications.snapshot_json is null;
comment on column public.report_runs.snapshot_json is null;

alter table public.scheduled_report_publications
    drop constraint if exists fk_scheduled_report_publications_revision;
drop index if exists public.ix_scheduled_report_publications_revision_id;
alter table public.scheduled_report_publications
    drop column if exists revision_id;

alter table public.report_runs
    drop constraint if exists fk_report_runs_revision;
drop index if exists public.ix_report_runs_revision_id;
alter table public.report_runs drop column if exists revision_id;

alter table public.project_record_revisions
    drop constraint if exists uq_project_record_revisions_project_revision;
"""

REPORT_REVISION_BOUND_TABLES = ("report_runs", "scheduled_report_publications")


def _refuse_unrepresentable_report_binding_downgrade(bind) -> None:
    """Refuse rather than drop a binding the snapshot-only shape cannot hold.

    The shape this downgrade restores is a ``snapshot_json`` copy and nothing
    that says which accepted revision it was taken against, which is the
    unreferenced copy #602 removes.  Dropping the column to get back there
    would recreate it silently, for every report already bound.  The same
    discipline as #599's and #610's downgrades: count what cannot be carried
    back, name it, and stop.
    """

    blocked = 0
    for table in REPORT_REVISION_BOUND_TABLES:
        bound = bind.execute(
            sa.text(
                "select count(*) from information_schema.columns "
                " where table_schema = 'public' and table_name = :table "
                "   and column_name = 'revision_id'"
            ),
            {"table": table},
        ).scalar_one()
        if not bound:
            continue
        blocked += bind.execute(
            sa.text(f"select count(*) from {table} where revision_id is not null")
        ).scalar_one()
    if blocked:
        raise RuntimeError(
            f"#602 downgrade refuses: {blocked} report reading(s) name the "
            "accepted Project Record revision they were produced against, and "
            "the snapshot-only shape cannot represent it. Nothing is dropped "
            "here."
        )


# --- #605 One stored extractor configuration, and evidence that cites
# --- segments instead of copying their words (ADR-0068, ADR-0082) ----------
#
# Two copies removed, both by the same move: store the value once and keep a
# reference to it.
#
# **The extractor configuration.**  Every sealed ``extraction_runs`` row
# carries its whole ``extractor_config_json`` receipt — prompt version, model,
# schema, three source digests, request controls and the runtime's Python and
# dependency-lock identity.  Every run of one deployed extractor seals a
# byte-identical object, which is why the digest column beside it exists at
# all.  So the receipt is written once per run and is the same object each
# time: a value masquerading as a possession.  ``extractor_configurations``
# stores it once, keyed by that digest, and the run keeps only the reference
# it already had.
#
# The registry is immutable.  A run that names a configuration is asserting
# what it actually ran; a configuration that could be edited afterwards would
# let that assertion quietly become false, so a trigger refuses every update
# and delete rather than trusting a convention.
#
# The backfill preserves exact legacy configuration and invents none.  Every
# distinct digest already stored is inserted with the exact receipt that was
# stored under it, and the foreign key is added afterwards, so a legacy run
# keeps referencing precisely what it ran.  A run written before the seal
# existed has ``extractor_config_sha256 is null`` and stays that way: null is
# a checked, explicit "not known", and giving it today's deployed
# configuration would be a fabrication that reads exactly like a measurement.
# The inline ``extractor_config_json`` copy is not dropped either — the
# constraint below simply stops requiring it, and requires instead that a copy
# still present is *identical* to the registry row, so no reader loses a
# receipt and no copy can drift from the row it duplicates.
#
# The receipt's shape rule moves into ``extractor_configuration_receipt_is_valid``
# so the registry and the run constraint share one definition of it, rather
# than the second restating the first — the same defect, one level up.
#
# **Evidence citations.**  ADR-0068 decided that a Source Segment owns its
# exact text once and that consumers reference it.  ``evidence_links.quote``
# is the copy that decision supersedes.  ``evidence_link_sources`` is the
# relationship that replaces it: link, segment, ordinal, and nothing else —
# no text column, so a citation written through it cannot carry a second copy
# of the words.  It mirrors ``fact_sources`` and ``support_assessment_sources``
# rather than inventing a shape, and its composite keys make a citation of
# another document's or another project's segment unrepresentable.
#
# ``evidence_links.quote`` stays, and stays readable.  Proving that a cited
# segment's text and a legacy quote say the same thing is a separate piece of
# work with its own corpus; until that proof exists, rewriting the column in
# bulk would be replacing evidence with something merely believed equivalent.
#
# The table takes ordinary grants rather than joining the source-append
# command family.  Those commands exist to enforce what constraints cannot —
# the digest of stored text, locator identity, project scope of an untyped
# reference.  This row stores no text, holds no locator, and its scope is a
# composite foreign key, so a ``SECURITY DEFINER`` wrapper would add ceremony
# and no guarantee.

EXTRACTOR_CONFIGURATION_REGISTRY_SCHEMA = """
create function public.extractor_configuration_receipt_is_valid(receipt jsonb)
    returns boolean
    language sql
    immutable
    as $$
        select receipt is not null
           and jsonb_typeof(receipt) = 'object'
           and receipt ?& array[
                   'receipt_version', 'extractor', 'prompt_version', 'model',
                   'schema_version', 'prompt_sha256', 'schema_sha256',
                   'postprocessor_sha256', 'request_controls', 'runtime'
               ]
           and jsonb_typeof(receipt -> 'receipt_version') = 'number'
           and receipt ->> 'receipt_version' = '1'
           and jsonb_typeof(receipt -> 'extractor') = 'string'
           and length(trim(receipt ->> 'extractor')) > 0
           and jsonb_typeof(receipt -> 'prompt_version') = 'string'
           and jsonb_typeof(receipt -> 'schema_version') = 'string'
           and jsonb_typeof(receipt -> 'prompt_sha256') = 'string'
           and jsonb_typeof(receipt -> 'schema_sha256') = 'string'
           and jsonb_typeof(receipt -> 'postprocessor_sha256') = 'string'
           and jsonb_typeof(receipt -> 'request_controls') = 'object'
           and jsonb_typeof(receipt -> 'runtime') = 'object'
           and receipt -> 'runtime' ?& array[
                   'python_implementation', 'python_version',
                   'dependency_lock_sha256', 'packages'
               ]
           and jsonb_typeof(
                   receipt -> 'runtime' -> 'python_implementation'
               ) = 'string'
           and length(trim(
                   receipt -> 'runtime' ->> 'python_implementation'
               )) > 0
           and jsonb_typeof(receipt -> 'runtime' -> 'python_version') = 'string'
           and length(trim(receipt -> 'runtime' ->> 'python_version')) > 0
           and jsonb_typeof(
                   receipt -> 'runtime' -> 'dependency_lock_sha256'
               ) = 'string'
           and receipt -> 'runtime' ->> 'dependency_lock_sha256'
               ~ '^[0-9a-f]{64}$'
           and jsonb_typeof(receipt -> 'runtime' -> 'packages') = 'object'
    $$;

create table public.extractor_configurations (
    config_sha256 character varying(64) not null,
    config_json jsonb not null,
    registered_at timestamp with time zone default now() not null,
    constraint extractor_configurations_pkey primary key (config_sha256),
    constraint ck_extractor_configurations_digest
        check (config_sha256 ~ '^[0-9a-f]{64}$'),
    constraint ck_extractor_configurations_receipt
        check (public.extractor_configuration_receipt_is_valid(config_json))
);

comment on table public.extractor_configurations is
    'One sealed extractor configuration, stored once by digest (#605). '
    'Immutable: a run that references one is asserting what it ran.';

create function public.refuse_extractor_configuration_rewrite()
    returns trigger
    language plpgsql
    as $$
        begin
            raise exception
                'a registered extractor configuration is immutable'
                using errcode='23514';
        end; $$;

create trigger trg_extractor_configurations_immutable
    before update or delete on public.extractor_configurations
    for each row
    execute function public.refuse_extractor_configuration_rewrite();

create function public.extraction_run_configuration_is_valid(
    reference character varying,
    inline_receipt jsonb,
    run_prompt_version character varying,
    run_schema_version character varying,
    run_model character varying,
    run_prompt_sha256 character varying,
    run_schema_sha256 character varying,
    run_postprocessor_sha256 character varying
)
    returns boolean
    language sql
    stable
    as $$
        select exists (
            select 1
              from public.extractor_configurations registry
             where registry.config_sha256 = reference
               and (
                   inline_receipt is null
                   or inline_receipt = registry.config_json
               )
               and public.extractor_configuration_receipt_is_valid(
                       registry.config_json
                   )
               and registry.config_json ->> 'prompt_version'
                   = run_prompt_version
               and registry.config_json ->> 'schema_version'
                   = run_schema_version
               and registry.config_json ->> 'prompt_sha256'
                   = run_prompt_sha256
               and registry.config_json ->> 'schema_sha256'
                   = run_schema_sha256
               and registry.config_json ->> 'postprocessor_sha256'
                   = run_postprocessor_sha256
               and (
                   (
                       run_model is null
                       and jsonb_typeof(registry.config_json -> 'model')
                           = 'null'
                   )
                   or (
                       run_model is not null
                       and jsonb_typeof(registry.config_json -> 'model')
                           = 'string'
                       and registry.config_json ->> 'model' = run_model
                   )
               )
        )
    $$;

comment on column public.extraction_runs.extractor_config_json is
    'Superseded for new writes by extractor_config_sha256 (#605). The '
    'authority is the extractor_configurations row that digest names; a '
    'copy still stored here must be identical to it. Retained until a '
    'sibling ticket proves the retirement of every reader.';
"""

# The receipt shape moved into a function, so the run's constraint states the
# reference rule and the token-usage rule and nothing else. Every clause the
# old expression spelled out inline is still enforced, one call away.
EXTRACTION_RUN_CONFIG_REFERENCE_SCHEMA = """
insert into public.extractor_configurations (config_sha256, config_json)
select distinct on (run.extractor_config_sha256)
       run.extractor_config_sha256,
       run.extractor_config_json
  from public.extraction_runs run
 where run.extractor_config_sha256 is not null
   and run.extractor_config_json is not null
 order by run.extractor_config_sha256, run.id
on conflict (config_sha256) do nothing;

alter table public.extraction_runs
    add constraint fk_extraction_runs_extractor_configuration
    foreign key (extractor_config_sha256)
    references public.extractor_configurations (config_sha256);

alter table public.extraction_runs
    drop constraint ck_extraction_runs_config_receipt_shape;

alter table public.extraction_runs
    add constraint ck_extraction_runs_config_receipt_shape check ((
            (
                prompt_sha256 is null
                and schema_sha256 is null
                and postprocessor_sha256 is null
                and extractor_config_json is null
                and extractor_config_sha256 is null
                and token_usage_json is null
            )
            or
            (
                prompt_sha256 is not null
                and schema_sha256 is not null
                and postprocessor_sha256 is not null
                and extractor_config_sha256 is not null
                and token_usage_json is not null
                and prompt_sha256 ~ '^[0-9a-f]{64}$'
                and schema_sha256 ~ '^[0-9a-f]{64}$'
                and postprocessor_sha256 ~ '^[0-9a-f]{64}$'
                and extractor_config_sha256 ~ '^[0-9a-f]{64}$'
                and extraction_run_configuration_is_valid(
                    extractor_config_sha256,
                    extractor_config_json,
                    prompt_version,
                    schema_version,
                    model,
                    prompt_sha256,
                    schema_sha256,
                    postprocessor_sha256
                )
                and jsonb_typeof(token_usage_json) = 'object'
                and token_usage_json ?& array[
                    'scope', 'document_ids', 'measurement'
                ]
                and jsonb_typeof(token_usage_json -> 'scope') = 'string'
                and jsonb_typeof(token_usage_json -> 'measurement') = 'string'
                and token_usage_json ->> 'scope' in ('run', 'batch')
                and jsonb_typeof(token_usage_json -> 'document_ids') = 'array'
                and jsonb_array_length(token_usage_json -> 'document_ids') > 0
                and (
                    (
                        token_usage_json ->> 'scope' = 'run'
                        and jsonb_array_length(
                            token_usage_json -> 'document_ids'
                        ) = 1
                    )
                    or (
                        token_usage_json ->> 'scope' = 'batch'
                        and jsonb_array_length(
                            token_usage_json -> 'document_ids'
                        ) > 1
                    )
                )
                and (
                    (
                        token_usage_json ->> 'measurement' = 'unavailable'
                        and token_usage_json ? 'reason'
                        and jsonb_typeof(
                            token_usage_json -> 'reason'
                        ) = 'string'
                        and length(trim(token_usage_json ->> 'reason')) > 0
                    )
                    or (
                        token_usage_json ->> 'measurement' = 'exact'
                        and token_usage_json ?& array[
                            'prompt_tokens', 'completion_tokens',
                            'reasoning_tokens', 'cached_tokens'
                        ]
                        and jsonb_typeof(
                            token_usage_json -> 'prompt_tokens'
                        ) = 'number'
                        and jsonb_typeof(
                            token_usage_json -> 'completion_tokens'
                        ) = 'number'
                        and jsonb_typeof(
                            token_usage_json -> 'reasoning_tokens'
                        ) = 'number'
                        and jsonb_typeof(
                            token_usage_json -> 'cached_tokens'
                        ) = 'number'
                        and token_usage_json ->> 'prompt_tokens' ~ '^[0-9]+$'
                        and token_usage_json ->> 'completion_tokens' ~ '^[0-9]+$'
                        and token_usage_json ->> 'reasoning_tokens' ~ '^[0-9]+$'
                        and token_usage_json ->> 'cached_tokens' ~ '^[0-9]+$'
                        and (token_usage_json ->> 'prompt_tokens')::numeric >= 0
                        and (token_usage_json ->> 'completion_tokens')::numeric >= 0
                        and (token_usage_json ->> 'reasoning_tokens')::numeric >= 0
                        and (token_usage_json ->> 'cached_tokens')::numeric >= 0
                    )
                )
                and extraction_token_usage_membership_is_valid(
                    document_id,
                    token_usage_json
                )
            ) is true
    ));
"""

EVIDENCE_SEGMENT_CITATION_SCHEMA = """
alter table public.evidence_links
    add constraint uq_evidence_links_document_id unique (document_id, id);

create table public.evidence_link_sources (
    id bigserial primary key,
    project_id bigint not null,
    document_id bigint not null,
    evidence_link_id bigint not null,
    source_segment_id bigint not null,
    ordinal integer not null,
    created_at timestamp with time zone default now() not null,
    constraint uq_evidence_link_sources_segment
        unique (evidence_link_id, source_segment_id),
    constraint uq_evidence_link_sources_ordinal
        unique (evidence_link_id, ordinal),
    constraint ck_evidence_link_sources_ordinal check (ordinal > 0),
    constraint evidence_link_sources_project_id_fkey
        foreign key (project_id) references public.projects (id),
    constraint fk_evidence_link_sources_link_scope
        foreign key (document_id, evidence_link_id)
        references public.evidence_links (document_id, id),
    constraint fk_evidence_link_sources_segment_scope
        foreign key (project_id, document_id, source_segment_id)
        references public.source_segments (project_id, document_id, id)
);

create index ix_evidence_link_sources_project_id
    on public.evidence_link_sources (project_id);
create index ix_evidence_link_sources_document_id
    on public.evidence_link_sources (document_id);
create index ix_evidence_link_sources_evidence_link_id
    on public.evidence_link_sources (evidence_link_id);
create index ix_evidence_link_sources_source_segment_id
    on public.evidence_link_sources (source_segment_id);

comment on table public.evidence_link_sources is
    'One Source Segment an Evidence Link cites (ADR-0068, #605). It holds no '
    'text: the segment owns the exact words once.';
comment on column public.evidence_links.quote is
    'Superseded for new writes by evidence_link_sources (ADR-0068, #605). '
    'Retained and readable; no bulk rewrite until a sibling ticket proves '
    'a cited segment and a legacy quote equivalent over a matched corpus.';
"""

EVIDENCE_SEGMENT_CITATION_SCHEMA_DOWN = """
comment on column public.evidence_links.quote is null;
drop table if exists public.evidence_link_sources;
alter table public.evidence_links
    drop constraint if exists uq_evidence_links_document_id;
"""

EXTRACTOR_CONFIGURATION_REGISTRY_SCHEMA_DOWN = """
comment on column public.extraction_runs.extractor_config_json is null;

alter table public.extraction_runs
    drop constraint if exists ck_extraction_runs_config_receipt_shape;
alter table public.extraction_runs
    drop constraint if exists fk_extraction_runs_extractor_configuration;

alter table public.extraction_runs
    add constraint ck_extraction_runs_config_receipt_shape check ((
            (
                prompt_sha256 is null
                and schema_sha256 is null
                and postprocessor_sha256 is null
                and extractor_config_json is null
                and extractor_config_sha256 is null
                and token_usage_json is null
            )
            or
            (
                prompt_sha256 is not null
                and schema_sha256 is not null
                and postprocessor_sha256 is not null
                and extractor_config_json is not null
                and extractor_config_sha256 is not null
                and token_usage_json is not null
                and prompt_sha256 ~ '^[0-9a-f]{64}$'
                and schema_sha256 ~ '^[0-9a-f]{64}$'
                and postprocessor_sha256 ~ '^[0-9a-f]{64}$'
                and extractor_config_sha256 ~ '^[0-9a-f]{64}$'
                and jsonb_typeof(extractor_config_json) = 'object'
                and extractor_config_json ?& array[
                    'receipt_version', 'extractor', 'prompt_version', 'model',
                    'schema_version', 'prompt_sha256', 'schema_sha256',
                    'postprocessor_sha256', 'request_controls', 'runtime'
                ]
                and jsonb_typeof(
                    extractor_config_json -> 'receipt_version'
                ) = 'number'
                and extractor_config_json ->> 'receipt_version' = '1'
                and jsonb_typeof(
                    extractor_config_json -> 'extractor'
                ) = 'string'
                and length(trim(extractor_config_json ->> 'extractor')) > 0
                and jsonb_typeof(
                    extractor_config_json -> 'prompt_version'
                ) = 'string'
                and jsonb_typeof(
                    extractor_config_json -> 'schema_version'
                ) = 'string'
                and jsonb_typeof(
                    extractor_config_json -> 'prompt_sha256'
                ) = 'string'
                and jsonb_typeof(
                    extractor_config_json -> 'schema_sha256'
                ) = 'string'
                and jsonb_typeof(
                    extractor_config_json -> 'postprocessor_sha256'
                ) = 'string'
                and jsonb_typeof(
                    extractor_config_json -> 'request_controls'
                ) = 'object'
                and jsonb_typeof(
                    extractor_config_json -> 'runtime'
                ) = 'object'
                and extractor_config_json -> 'runtime' ?& array[
                    'python_implementation', 'python_version',
                    'dependency_lock_sha256', 'packages'
                ]
                and jsonb_typeof(
                    extractor_config_json -> 'runtime' ->
                        'python_implementation'
                ) = 'string'
                and length(trim(
                    extractor_config_json -> 'runtime' ->>
                        'python_implementation'
                )) > 0
                and jsonb_typeof(
                    extractor_config_json -> 'runtime' -> 'python_version'
                ) = 'string'
                and length(trim(
                    extractor_config_json -> 'runtime' ->> 'python_version'
                )) > 0
                and jsonb_typeof(
                    extractor_config_json -> 'runtime' ->
                        'dependency_lock_sha256'
                ) = 'string'
                and extractor_config_json -> 'runtime' ->>
                    'dependency_lock_sha256' ~ '^[0-9a-f]{64}$'
                and jsonb_typeof(
                    extractor_config_json -> 'runtime' -> 'packages'
                ) = 'object'
                and extractor_config_json ->> 'prompt_version' = prompt_version
                and extractor_config_json ->> 'schema_version' = schema_version
                and extractor_config_json ->> 'prompt_sha256' = prompt_sha256
                and extractor_config_json ->> 'schema_sha256' = schema_sha256
                and extractor_config_json ->> 'postprocessor_sha256' =
                    postprocessor_sha256
                and (
                    (
                        model is null
                        and jsonb_typeof(
                            extractor_config_json -> 'model'
                        ) = 'null'
                    )
                    or (
                        model is not null
                        and jsonb_typeof(
                            extractor_config_json -> 'model'
                        ) = 'string'
                        and extractor_config_json ->> 'model' = model
                    )
                )
                and jsonb_typeof(token_usage_json) = 'object'
                and token_usage_json ?& array[
                    'scope', 'document_ids', 'measurement'
                ]
                and jsonb_typeof(token_usage_json -> 'scope') = 'string'
                and jsonb_typeof(token_usage_json -> 'measurement') = 'string'
                and token_usage_json ->> 'scope' in ('run', 'batch')
                and jsonb_typeof(token_usage_json -> 'document_ids') = 'array'
                and jsonb_array_length(token_usage_json -> 'document_ids') > 0
                and (
                    (
                        token_usage_json ->> 'scope' = 'run'
                        and jsonb_array_length(
                            token_usage_json -> 'document_ids'
                        ) = 1
                    )
                    or (
                        token_usage_json ->> 'scope' = 'batch'
                        and jsonb_array_length(
                            token_usage_json -> 'document_ids'
                        ) > 1
                    )
                )
                and (
                    (
                        token_usage_json ->> 'measurement' = 'unavailable'
                        and token_usage_json ? 'reason'
                        and jsonb_typeof(
                            token_usage_json -> 'reason'
                        ) = 'string'
                        and length(trim(token_usage_json ->> 'reason')) > 0
                    )
                    or (
                        token_usage_json ->> 'measurement' = 'exact'
                        and token_usage_json ?& array[
                            'prompt_tokens', 'completion_tokens',
                            'reasoning_tokens', 'cached_tokens'
                        ]
                        and jsonb_typeof(
                            token_usage_json -> 'prompt_tokens'
                        ) = 'number'
                        and jsonb_typeof(
                            token_usage_json -> 'completion_tokens'
                        ) = 'number'
                        and jsonb_typeof(
                            token_usage_json -> 'reasoning_tokens'
                        ) = 'number'
                        and jsonb_typeof(
                            token_usage_json -> 'cached_tokens'
                        ) = 'number'
                        and token_usage_json ->> 'prompt_tokens' ~ '^[0-9]+$'
                        and token_usage_json ->> 'completion_tokens' ~ '^[0-9]+$'
                        and token_usage_json ->> 'reasoning_tokens' ~ '^[0-9]+$'
                        and token_usage_json ->> 'cached_tokens' ~ '^[0-9]+$'
                        and (token_usage_json ->> 'prompt_tokens')::numeric >= 0
                        and (token_usage_json ->> 'completion_tokens')::numeric >= 0
                        and (token_usage_json ->> 'reasoning_tokens')::numeric >= 0
                        and (token_usage_json ->> 'cached_tokens')::numeric >= 0
                    )
                )
                and extraction_token_usage_membership_is_valid(
                    document_id,
                    token_usage_json
                )
            ) is true
    ));

drop function if exists public.extraction_run_configuration_is_valid(
    character varying, jsonb, character varying, character varying,
    character varying, character varying, character varying, character varying
);
drop trigger if exists trg_extractor_configurations_immutable
    on public.extractor_configurations;
drop function if exists public.refuse_extractor_configuration_rewrite() cascade;
drop table if exists public.extractor_configurations;
drop function if exists public.extractor_configuration_receipt_is_valid(jsonb);
"""

EXTRACTOR_CONFIGURATION_TABLES = ("extractor_configurations",)
EVIDENCE_SEGMENT_CITATION_TABLES = ("evidence_link_sources",)


def _refuse_unreferenced_configuration_downgrade(bind) -> None:
    """Refuse rather than silently restore the per-run configuration copy.

    The shape this downgrade returns to requires every sealed run to carry its
    own ``extractor_config_json``.  A run written after this transition
    references the registry and stores no copy, so going back would either
    drop the run's only configuration or re-copy the registry row into it and
    call that history.  Same discipline as #599, #602 and #610: count what
    cannot be carried back, name it, and stop.
    """

    registered = bind.execute(
        sa.text(
            "select count(*) from information_schema.tables "
            " where table_schema = 'public' "
            "   and table_name = 'extractor_configurations'"
        )
    ).scalar_one()
    if not registered:
        return
    blocked = bind.execute(
        sa.text(
            "select count(*) from public.extraction_runs "
            " where extractor_config_sha256 is not null "
            "   and extractor_config_json is null"
        )
    ).scalar_one()
    if blocked:
        raise RuntimeError(
            f"#605 downgrade refuses: {blocked} Extraction Run(s) reference a "
            "stored extractor configuration and hold no copy of it, and the "
            "per-run-copy shape cannot represent that. Nothing is dropped "
            "here."
        )


def _refuse_unreferenced_evidence_citation_downgrade(bind) -> None:
    """Refuse rather than drop the segment citations a link already carries."""

    present = bind.execute(
        sa.text(
            "select count(*) from information_schema.tables "
            " where table_schema = 'public' "
            "   and table_name = 'evidence_link_sources'"
        )
    ).scalar_one()
    if not present:
        return
    blocked = bind.execute(
        sa.text("select count(*) from public.evidence_link_sources")
    ).scalar_one()
    if blocked:
        raise RuntimeError(
            f"#605 downgrade refuses: {blocked} Evidence Link citation(s) name "
            "the Source Segment that owns their words, and the quote-copy "
            "shape cannot represent it. Nothing is dropped here."
        )


# --- #633 The retained report reading is evidence, not a cache (ADR-0092) ---
#
# #602 bound each ``report_runs`` and ``scheduled_report_publications`` row to
# the accepted Project Record revision it was taken against, and demoted
# ``snapshot_json`` beside it to a "rebuildable compatibility cache" awaiting
# the expiry rule #603 was expected to make writable.  #603 measured it
# instead, and found the demotion wrong.  A revision reproduces the two record
# fields the spine carries and the Ledger identity — 212 field comparisons, no
# disagreement — and cannot answer three things at all: the documentation
# requirement and the Constraint Alerts of one reading under one ruleset, one
# threshold configuration and one date; which subjects the report covered; and
# the date projected from the external party's current statement, which shares
# a name with the ``committed_date`` Fact and is a different quantity.
#
# ADR-0092 records that those belong to the Report Reading occurrence, a third
# owner beside the revision and the release package.  So the only change here
# is what the database says the column is.  A comment that calls immutable
# published evidence a rebuildable cache is an instruction to a future
# implementer to delete it, and the honest version of that comment cannot be
# written, because no rule would make it true.
#
# Nothing else moves.  No column is added, no constraint is changed, and no
# stored payload is rewritten: a version 1 payload is read exactly as it was
# written, and only newly written payloads carry the schema version, the
# content digest and the governed names.  A retention rule is deliberately
# absent — the payload's retention is the run's, which ends with the governing
# customer-environment retention and never on a cache TTL.

REPORT_READING_PAYLOAD_COMMENT = """
comment on column public.report_runs.snapshot_json is
    'Immutable Report Reading payload (#633, ADR-0092): the population this '
    'report covered, its derived outcomes, the statement-projected '
    'published_promised_for, and the rules and thresholds used. Not a cache '
    'of revision_id and not rebuildable from it; retained as long as the run '
    'is, on no cache TTL.';
comment on column public.scheduled_report_publications.snapshot_json is
    'Immutable Report Reading payload (#633, ADR-0092): the population this '
    'reading covered, its derived outcomes, the statement-projected '
    'published_promised_for, and the rules and thresholds used. Not a cache '
    'of revision_id and not rebuildable from it; retained as long as the '
    'publication and its released package are, on no cache TTL.';
"""

# The downgrade restores #602's wording rather than clearing the comment,
# because the block below this one in ``downgrade`` is #602's, and it is what
# owns clearing it.  Restoring a description the schema at that point no
# longer justifies is worse than restoring the one it was given there.
REPORT_READING_PAYLOAD_COMMENT_DOWN = """
comment on column public.report_runs.snapshot_json is
    'Rebuildable compatibility cache (#602). The authority is revision_id; '
    'this copy is retained only until #603 proves what rebuilds it.';
comment on column public.scheduled_report_publications.snapshot_json is
    'Rebuildable compatibility cache (#602). The authority is revision_id; '
    'this copy is retained only until #603 proves what rebuilds it.';
"""


# --- #531 Project authorization is a data partition, not a where clause -----
#
# ADR-0083 corrected ADR-0079: one database per customer does not make the
# project a database boundary, so "projects remain authorization and
# data-partition boundaries inside the customer database".  Until now that
# boundary was ``_authorize`` in ``web/app.py`` plus a ``project_id ==`` in
# every reader.  That is an application filter: one query written without the
# predicate reads another project's rows and nothing anywhere refuses.
#
# The partition is therefore moved into PostgreSQL.  The four project-scoped
# spine relations the product reads carry row-level security, and the web
# capability sees only the rows of the projects its *declared partition*
# names.  A declared partition is not something the application can assert:
# ``open_project_partition`` proves an active roster entry for the principal
# before it declares one, and the declaration is sealed with a secret that
# lives in a table no runtime login can read.  A login that sets the setting
# by hand produces a scope whose seal does not verify, and an unverified scope
# is the empty scope.  So a forgotten ``where`` clause now returns nothing
# instead of another customer project's rows.
#
# Scope is transaction-local (``set_config(..., true)``), so a pooled
# connection cannot carry one request's partition into the next, and a
# rollback takes the partition with it.
#
# The worker capability is deliberately *not* partitioned: a background run
# carries no person's authorization to enforce, its isolation boundary is the
# customer database (ADR-0079), and every command-line entry point in
# ``corridor`` reads whichever project it was pointed at.  The three command
# roles are unpartitioned for a different reason: each already proves project
# scope on every typed reference it touches, and partitioning them would break
# the very commands that enforce scope.

PARTITIONED_TABLES = (
    "source_segments",
    "facts",
    "extracted_proposals",
    "proposed_deltas",
)

# Every role that must see the whole customer database: the worker capability,
# the three command-owner roles, and the opt-in legacy development login where
# a deployment created one.
UNPARTITIONED_ROLES = (
    "corridor_worker",
    "corridor_source_append",
    "corridor_fact_decision_writer",
    "corridor_statement_retirement",
    "corridor_legacy_dev",
)

PARTITION_COMMANDS = {
    "current_project_partition": "()",
    "seal_project_partition": "(text)",
    "open_project_partition": "(text, bigint)",
    "open_member_project_partition": "(text)",
    "close_project_partition": "()",
}

PROJECT_PARTITION_SCHEMA = """
create table public.project_partition_secrets (
    id smallint not null,
    secret text not null,
    constraint pk_project_partition_secrets primary key (id),
    constraint ck_project_partition_secrets_singleton check (id = 1),
    constraint ck_project_partition_secrets_secret check (length(secret) >= 32)
);

comment on table public.project_partition_secrets is
    'The seal key for a declared project partition (#531). No runtime login '
    'holds any privilege on this table: a capability that could read it could '
    'forge a partition for a project nobody granted it.';

-- The schema owner carries default privileges that hand every new table to the
-- runtime logins (`alter default privileges ... grant`), so this table has to
-- take them back explicitly. Creating it grants nothing on purpose; inheriting
-- a blanket grant would make the seal readable and every partition forgeable.
do $$
declare
    v_roles text;
begin
    select string_agg(quote_ident(rolname), ', ' order by rolname)
      into v_roles
      from pg_roles
     where rolname in ('corridor_web', 'corridor_worker', 'corridor_legacy_dev');
    if v_roles is not null then
        execute format(
            'revoke all on public.project_partition_secrets from %s', v_roles
        );
    end if;
end $$;

insert into public.project_partition_secrets (id, secret)
values (1, encode(sha256((gen_random_uuid()::text || clock_timestamp()::text)::bytea), 'hex'));
"""

PROJECT_PARTITION_SCHEMA_DOWN = """
drop table if exists public.project_partition_secrets;
"""

# The seal is derived, never stored per session, so declaring a partition
# writes nothing and costs no row.  ``p_scope`` is the canonical
# comma-separated ascending id list; the empty string is the empty partition,
# which is what an offboarded person's connection gets.
SEAL_PROJECT_PARTITION = """
create function public.seal_project_partition(p_scope text)
returns void
language plpgsql
security definer
as $$
declare
    v_secret text;
begin
    select secret into strict v_secret
      from public.project_partition_secrets
     where id = 1;
    perform set_config('corridor.project_partition', p_scope, true);
    perform set_config(
        'corridor.project_partition_seal',
        encode(sha256((v_secret || ':' || p_scope)::bytea), 'hex'),
        true
    );
end;
$$;
"""

# Null and the empty array both refuse every row.  Returning null for an
# unverified seal rather than raising keeps the policy cheap and keeps the
# failure uniform: an unset partition and a forged one are the same partition.
CURRENT_PROJECT_PARTITION = """
create function public.current_project_partition()
returns bigint[]
language plpgsql
stable
security definer
as $$
declare
    v_scope text := current_setting('corridor.project_partition', true);
    v_seal text := current_setting('corridor.project_partition_seal', true);
    v_secret text;
begin
    if v_scope is null then
        return null;
    end if;
    select secret into strict v_secret
      from public.project_partition_secrets
     where id = 1;
    if v_seal is distinct from
        encode(sha256((v_secret || ':' || v_scope)::bytea), 'hex')
    then
        return null;
    end if;
    if v_scope = '' then
        return array[]::bigint[];
    end if;
    return string_to_array(v_scope, ',')::bigint[];
end;
$$;
"""

OPEN_PROJECT_PARTITION = """
create function public.open_project_partition(
    p_principal_subject text,
    p_project_id bigint
)
returns bigint
language plpgsql
security definer
as $$
begin
    if not exists (
        select 1
          from public.project_roster_entries
         where project_id = p_project_id
           and principal_subject = p_principal_subject
           and active
    ) then
        raise exception
            'principal % holds no active membership of project %',
            p_principal_subject, p_project_id
            using errcode = '42501';
    end if;
    perform public.seal_project_partition(p_project_id::text);
    return p_project_id;
end;
$$;
"""

# The cross-project reading (#537) needs a partition too, and the honest one is
# every project this person is currently on.  A person with no active
# membership left declares the empty partition, which is exactly what
# offboarding should leave behind: a live connection that can still be
# authenticated but can read no project's rows.
OPEN_MEMBER_PROJECT_PARTITION = """
create function public.open_member_project_partition(p_principal_subject text)
returns bigint[]
language plpgsql
security definer
as $$
declare
    v_ids bigint[];
begin
    select coalesce(array_agg(project_id order by project_id), array[]::bigint[])
      into v_ids
      from public.project_roster_entries
     where principal_subject = p_principal_subject
       and active;
    perform public.seal_project_partition(array_to_string(v_ids, ','));
    return v_ids;
end;
$$;
"""

CLOSE_PROJECT_PARTITION = """
create function public.close_project_partition()
returns void
language plpgsql
security definer
as $$
begin
    perform set_config('corridor.project_partition', '', true);
    perform set_config('corridor.project_partition_seal', '', true);
end;
$$;
"""

# ``seal_project_partition`` is the one command that declares a partition
# without proving anything, so it is never granted: only the three commands
# above call it, and they run as its owner.
PARTITION_COMMANDS_GRANTED = (
    "current_project_partition",
    "open_project_partition",
    "open_member_project_partition",
    "close_project_partition",
)

_PARTITIONED_TABLES_SQL = ", ".join(f"'{table}'" for table in PARTITIONED_TABLES)
_UNPARTITIONED_ROLES_SQL = ", ".join(f"'{role}'" for role in UNPARTITIONED_ROLES)

PROJECT_PARTITION_POLICIES = f"""
do $$
declare
    v_roles text;
    v_table text;
begin
    select string_agg(quote_ident(rolname), ', ' order by rolname)
      into v_roles
      from pg_roles
     where rolname in ({_UNPARTITIONED_ROLES_SQL});
    foreach v_table in array array[{_PARTITIONED_TABLES_SQL}] loop
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

PROJECT_PARTITION_POLICIES_DOWN = f"""
do $$
declare
    v_table text;
begin
    foreach v_table in array array[{_PARTITIONED_TABLES_SQL}] loop
        execute format(
            'drop policy if exists %I on public.%I',
            'p_' || v_table || '_project_partition', v_table
        );
        execute format(
            'drop policy if exists %I on public.%I',
            'p_' || v_table || '_unpartitioned', v_table
        );
        execute format(
            'alter table public.%I disable row level security', v_table
        );
    end loop;
end $$;
"""


# --- #640 The per-project external-issue profile (ADR-0091, ADR-0086) -------
#
# ADR-0091 made the externally issued set per-project configuration with only
# the updated UCM mandatory, and recorded in the same breath that the
# configuration "is not modelled".  Nothing implemented it, so #536 shipped
# ADR-0085's three consequence levels deliberately underived — a level is a
# projection onto the next issue's declared content, and there was no declared
# content to project onto — and #529 carried a fixed four-artifact list that
# ADR-0091 had already retired.  These two tables are that configuration.
#
# **The mandatory member is a column, not a row.**  ``updated_ucm`` is the one
# artifact with no configuration switch, so it is a property of the profile and
# not an entry in the configured set: a profile with no UCM is unrepresentable
# because ``ucm_renderer_identity`` is ``not null``, and a second UCM entry is
# unrepresentable because ``updated_ucm`` is not a value the artifact table's
# type check admits.  A nullable row in a set-membership table would have made
# both states insertable and left "always" to a Python branch.  The artifact
# table therefore holds exactly what ADR-0091 made configurable.
#
# **Two layers, no third.**  A profile names artifact types and the renderer
# revision each is produced by; what a given renderer revision contains — which
# accepted fields, alerts, follow-up content and disclosures — is that
# renderer's own declaration.  There is no per-field column here and no place to
# add one, which is the point: an inventory assembled field by field per
# customer is a report builder, not a configured issue set.
#
# **History is a chain, and the chain is the timing rule.**  Each version names
# its predecessor through a composite foreign key that carries the predecessor's
# project, identity, version and effective instant, and
# ``ck_project_issue_profiles_succession`` requires the version to be exactly
# one higher and the effective instant to be strictly later.  So the monotonic,
# non-backdatable, single-lineage, append-only history is four constraints
# rather than a Python comparison anybody can forget: a caller cannot register a
# version effective at or before the currently effective one, and therefore
# cannot retroactively change what an earlier reporting cutoff was configured to
# issue.  Strictly later, not at-or-later, is deliberate — equality would leave
# one instant whose configured answer could still be rewritten.
# ``uq_project_issue_profiles_successor`` refuses a fork, and the partial unique
# index refuses a second lineage in one project.
#
# **The digest covers the whole configuration.**  ``declaration`` holds the
# exact canonical bytes the digest is taken over, as ``text`` and never
# ``jsonb``, for #610's reason: PostgreSQL normalizes key order, whitespace and
# numbers, so the bytes it gave back would no longer be the bytes anyone
# digested.  ``ck_project_issue_profiles_digest`` recomputes SHA-256 over the
# stored bytes, so a declaration that does not digest to what the row claims
# cannot be stored at all.  Every other column is *derived from* those bytes by
# the one command, so no column can disagree with the digest.
#
# **The template and mapping are references, not copies.**  A profile names the
# registered output template and field mapping (#509, #597) its artifacts render
# through, and the composite foreign keys carry ``project_id`` and a generated
# constant kind — so a profile naming another project's registration, or naming
# a field mapping where a template belongs, is unrepresentable rather than
# refused by a lookup somebody could skip.
#
# **Who configures.**  The command is owned by the record-decision role and
# granted to the web capability alone; ``issue_profile.register_issue_profile``
# proves the Project Coordination designation before calling it.  The external
# releaser deliberately holds no authority here: that person authorizes a
# prepared package (#533), and release approval must not become package design.

ISSUE_PROFILE_TABLES = (
    "project_issue_profiles",
    "project_issue_profile_artifacts",
)

# ADR-0091's configured members.  The mandatory updated UCM is deliberately
# absent: it is a column on the profile, so it can be neither omitted nor
# entered twice.
CONFIGURED_ARTIFACT_TYPES = (
    "accepted_change_summary",
    "chase_list",
    "weekly_coordination_report",
    "provenance_sidecar",
)

_CONFIGURED_ARTIFACT_TYPES_SQL = ", ".join(
    f"'{name}'" for name in CONFIGURED_ARTIFACT_TYPES
)

ISSUE_PROFILE_SCHEMA = f"""
-- A profile names one registration of one kind in one project. The existing
-- revision key carries identity, version and digest but not the kind, and the
-- kind is exactly what stops a field mapping standing where a template belongs.
alter table public.project_baseline_formats
    add constraint uq_project_baseline_formats_project_kind
    unique (id, project_id, format_kind);

create table public.project_issue_profiles (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    profile_identity character varying(160) not null,
    -- An integer, so a blank version is not a value this column can hold.
    profile_version integer not null,
    content_sha256 character varying(64) not null,
    -- The exact canonical bytes the digest is taken over, not a re-encoding.
    declaration text not null,
    declaration_schema_version character varying(64) not null,
    -- The business instant the caller supplied. Never a clock reading: the
    -- configuration says when it takes effect, and nothing infers it.
    effective_from timestamp with time zone not null,
    -- ADR-0091's one mandatory member, held as a property of the profile
    -- because its participation is not configurable.
    ucm_renderer_identity character varying(160) not null,
    ucm_renderer_version character varying(64) not null,
    output_template_format_id bigint not null,
    output_template_kind character varying(32)
        generated always as ('output_template') stored,
    field_mapping_format_id bigint not null,
    field_mapping_kind character varying(32)
        generated always as ('field_mapping') stored,
    -- The predecessor, carried as its whole identity so the succession check
    -- compares against the real row and not a number a caller supplied.
    supersedes_id bigint,
    supersedes_version integer,
    supersedes_effective_from timestamp with time zone,
    registered_by_principal character varying(128) not null,
    idempotency_key character varying(160) not null,
    registered_at timestamp with time zone not null default now(),
    constraint uq_project_issue_profiles_key
        unique (project_id, idempotency_key),
    constraint uq_project_issue_profiles_version
        unique (project_id, profile_identity, profile_version),
    constraint uq_project_issue_profiles_row unique (id, project_id),
    constraint uq_project_issue_profiles_chain
        unique (id, project_id, profile_identity, profile_version, effective_from),
    -- One successor per version: two versions cannot both claim the same
    -- predecessor, so the history is a chain and never a fork.
    constraint uq_project_issue_profiles_successor unique (supersedes_id),
    constraint fk_project_issue_profiles_supersedes foreign key
        (supersedes_id, project_id, profile_identity, supersedes_version,
         supersedes_effective_from)
        references public.project_issue_profiles
        (id, project_id, profile_identity, profile_version, effective_from),
    constraint fk_project_issue_profiles_template foreign key
        (output_template_format_id, project_id, output_template_kind)
        references public.project_baseline_formats (id, project_id, format_kind),
    constraint fk_project_issue_profiles_mapping foreign key
        (field_mapping_format_id, project_id, field_mapping_kind)
        references public.project_baseline_formats (id, project_id, format_kind),
    constraint ck_project_issue_profiles_identity check (
        length(btrim(profile_identity)) > 0
    ),
    constraint ck_project_issue_profiles_key check (
        length(btrim(idempotency_key)) > 0
    ),
    constraint ck_project_issue_profiles_principal check (
        length(btrim(registered_by_principal)) > 0
    ),
    constraint ck_project_issue_profiles_schema check (
        length(btrim(declaration_schema_version)) > 0
    ),
    constraint ck_project_issue_profiles_version check (profile_version >= 1),
    constraint ck_project_issue_profiles_ucm check (
        length(btrim(ucm_renderer_identity)) > 0
        and length(btrim(ucm_renderer_version)) > 0
    ),
    constraint ck_project_issue_profiles_digest check (
        encode(sha256(convert_to(declaration, 'utf8')), 'hex') = content_sha256
    ),
    -- Version 1 opens the chain; every later version follows its predecessor by
    -- exactly one and takes effect strictly after it.
    constraint ck_project_issue_profiles_succession check (
        (supersedes_id is null and profile_version = 1
            and supersedes_version is null
            and supersedes_effective_from is null)
        or (supersedes_id is not null and supersedes_version is not null
            and supersedes_effective_from is not null
            and profile_version = supersedes_version + 1
            and effective_from > supersedes_effective_from)
    )
);

-- One lineage per project: a second root would be a second configured set with
-- no ordering between them.
create unique index uq_project_issue_profiles_root
    on public.project_issue_profiles (project_id)
    where supersedes_id is null;
create index ix_project_issue_profiles_effective
    on public.project_issue_profiles (project_id, effective_from);

create table public.project_issue_profile_artifacts (
    id bigserial primary key,
    profile_id bigint not null,
    project_id bigint not null,
    -- 'updated_ucm' is deliberately not admitted here: the mandatory member is
    -- the profile's own column, so it can be neither dropped nor doubled.
    artifact_type character varying(48) not null,
    renderer_identity character varying(160) not null,
    renderer_version character varying(64) not null,
    constraint uq_project_issue_profile_artifacts_type
        unique (profile_id, artifact_type),
    constraint fk_project_issue_profile_artifacts_profile foreign key
        (profile_id, project_id)
        references public.project_issue_profiles (id, project_id),
    constraint ck_project_issue_profile_artifacts_type check (
        artifact_type in ({_CONFIGURED_ARTIFACT_TYPES_SQL})
    ),
    constraint ck_project_issue_profile_artifacts_renderer check (
        length(btrim(renderer_identity)) > 0
        and length(btrim(renderer_version)) > 0
    )
);

create index ix_project_issue_profile_artifacts_project_id
    on public.project_issue_profile_artifacts (project_id);

create function public.enforce_project_issue_profile_write()
    returns trigger
    language plpgsql
    as $$
        begin
            if current_user <> 'corridor_fact_decision_writer' then
                raise exception 'an issue profile is written only by the typed registration command'
                    using errcode='23514';
            end if;
            if tg_op <> 'INSERT' then
                raise exception 'an issue profile version is immutable: a changed profile is a new version, never an edit'
                    using errcode='23514';
            end if;
            return new;
        end; $$;

create trigger trg_project_issue_profiles_write
    before insert or update or delete
    on public.project_issue_profiles
    for each row
    execute function public.enforce_project_issue_profile_write();
create trigger trg_project_issue_profiles_truncate
    before truncate on public.project_issue_profiles
    for each statement
    execute function public.enforce_project_issue_profile_write();
create trigger trg_project_issue_profile_artifacts_write
    before insert or update or delete
    on public.project_issue_profile_artifacts
    for each row
    execute function public.enforce_project_issue_profile_write();
create trigger trg_project_issue_profile_artifacts_truncate
    before truncate on public.project_issue_profile_artifacts
    for each statement
    execute function public.enforce_project_issue_profile_write();
"""

ISSUE_PROFILE_SCHEMA_DOWN = """
drop table if exists public.project_issue_profile_artifacts cascade;
drop table if exists public.project_issue_profiles cascade;
drop function if exists public.enforce_project_issue_profile_write() cascade;
alter table public.project_baseline_formats
    drop constraint if exists uq_project_baseline_formats_project_kind;
"""

# The one writer.  Every stored column but the row's own key is derived from
# the digested declaration, so the artifact set, the renderer revisions and the
# named template and mapping are all covered by ``content_sha256`` and no
# parameter can contradict it.
REGISTER_PROJECT_ISSUE_PROFILE = f"""
create function public.register_project_issue_profile(
    p_project_id bigint,
    p_profile_identity character varying,
    p_declaration text,
    p_effective_from timestamp with time zone,
    p_principal character varying,
    p_idempotency_key character varying
) returns jsonb
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            existing project_issue_profiles%ROWTYPE;
            previous project_issue_profiles%ROWTYPE;
            declared jsonb;
            declared_schema character varying(64);
            declared_digest character varying(64);
            template_id bigint;
            mapping_id bigint;
            entry jsonb;
            new_id bigint;
            new_version integer;
        begin
            if p_principal is null or length(btrim(p_principal)) = 0 then
                raise exception 'an issue profile names the person registering it'
                    using errcode='23514';
            end if;
            if p_profile_identity is null
               or length(btrim(p_profile_identity)) = 0 then
                raise exception 'an issue profile needs an identity'
                    using errcode='23514';
            end if;
            if p_idempotency_key is null
               or length(btrim(p_idempotency_key)) = 0 then
                raise exception 'an issue profile registration needs an idempotency key'
                    using errcode='23514';
            end if;
            if p_effective_from is null then
                raise exception 'an issue profile takes effect from an explicitly supplied business instant'
                    using errcode='23514';
            end if;
            declared_digest := encode(
                sha256(convert_to(p_declaration, 'utf8')), 'hex'
            );
            select * into existing from project_issue_profiles
             where project_id = p_project_id
               and idempotency_key = p_idempotency_key;
            if found then
                if existing.content_sha256 is distinct from declared_digest
                   or existing.effective_from is distinct from p_effective_from
                   or existing.profile_identity is distinct from p_profile_identity
                then
                    raise exception 'the issue profile registration key is bound to different content'
                        using errcode='23514';
                end if;
                return jsonb_build_object(
                    'profile_id', existing.id,
                    'profile_version', existing.profile_version,
                    'created', false
                );
            end if;
            -- An invalid or non-object declaration refuses here: the cast
            -- raises, or the schema version reads back null.
            declared := p_declaration::jsonb;
            declared_schema := declared->>'schema_version';
            if declared_schema is null
               or length(btrim(declared_schema)) = 0 then
                raise exception 'an issue profile names the declaration schema it was written under'
                    using errcode='23514';
            end if;
            if declared->'updated_ucm' is null
               or jsonb_typeof(declared->'updated_ucm') <> 'object' then
                raise exception 'every issue profile declares the updated UCM it renders; it is the one member with no configuration switch'
                    using errcode='23514';
            end if;
            select id into template_id from project_baseline_formats
             where project_id = p_project_id
               and format_kind = 'output_template'
               and format_identity = declared->'output_template'->>'identity'
               and format_version = declared->'output_template'->>'version'
               and content_sha256 = declared->'output_template'->>'content_sha256';
            if template_id is null then
                raise exception 'the declared output template is not a registration of project %', p_project_id
                    using errcode='23514';
            end if;
            select id into mapping_id from project_baseline_formats
             where project_id = p_project_id
               and format_kind = 'field_mapping'
               and format_identity = declared->'field_mapping'->>'identity'
               and format_version = declared->'field_mapping'->>'version'
               and content_sha256 = declared->'field_mapping'->>'content_sha256';
            if mapping_id is null then
                raise exception 'the declared field mapping is not a registration of project %', p_project_id
                    using errcode='23514';
            end if;
            select * into previous from project_issue_profiles
             where project_id = p_project_id
             order by profile_version desc
             limit 1;
            if found then
                if previous.profile_identity is distinct from p_profile_identity
                then
                    raise exception 'project % already configures issue profile %; a project has one profile lineage, not two', p_project_id, previous.profile_identity
                        using errcode='23514';
                end if;
                if p_effective_from <= previous.effective_from then
                    raise exception 'version % takes effect at %, at or before the effective version %; an earlier reporting cutoff cannot be reconfigured after the fact', previous.profile_version + 1, p_effective_from, previous.effective_from
                        using errcode='23514';
                end if;
                new_version := previous.profile_version + 1;
            else
                new_version := 1;
            end if;
            new_id := nextval('project_issue_profiles_id_seq');
            insert into project_issue_profiles (
                id, project_id, profile_identity, profile_version,
                content_sha256, declaration, declaration_schema_version,
                effective_from, ucm_renderer_identity, ucm_renderer_version,
                output_template_format_id, field_mapping_format_id,
                supersedes_id, supersedes_version, supersedes_effective_from,
                registered_by_principal, idempotency_key
            ) values (
                new_id, p_project_id, p_profile_identity, new_version,
                declared_digest, p_declaration, declared_schema,
                p_effective_from,
                declared->'updated_ucm'->>'renderer_identity',
                declared->'updated_ucm'->>'renderer_version',
                template_id, mapping_id,
                case when new_version = 1 then null else previous.id end,
                case when new_version = 1 then null
                     else previous.profile_version end,
                case when new_version = 1 then null
                     else previous.effective_from end,
                p_principal, p_idempotency_key
            );
            for entry in
                select value from jsonb_array_elements(
                    coalesce(declared->'configured_artifacts', '[]'::jsonb)
                )
            loop
                insert into project_issue_profile_artifacts (
                    profile_id, project_id, artifact_type,
                    renderer_identity, renderer_version
                ) values (
                    new_id, p_project_id, entry->>'artifact_type',
                    entry->>'renderer_identity', entry->>'renderer_version'
                );
            end loop;
            return jsonb_build_object(
                'profile_id', new_id,
                'profile_version', new_version,
                'created', true
            );
        end; $$;
"""

REGISTER_PROJECT_ISSUE_PROFILE_SIGNATURE = (
    "(bigint, character varying, text, timestamp with time zone, "
    "character varying, character varying)"
)

# The configured issue set decides what one customer receives, so it carries
# #531's partition for exactly #531's reason: a reader that forgets its
# ``where`` clause must see nothing, not another project's configuration.
ISSUE_PROFILE_PARTITION_POLICIES = f"""
do $$
declare
    v_roles text;
    v_table text;
begin
    select string_agg(quote_ident(rolname), ', ' order by rolname)
      into v_roles
      from pg_roles
     where rolname in ({_UNPARTITIONED_ROLES_SQL});
    foreach v_table in array array['project_issue_profiles',
                                   'project_issue_profile_artifacts'] loop
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


def _refuse_unrepresentable_issue_profile_downgrade(bind) -> None:
    """Refuse rather than drop a configured issue set nothing else records.

    The schema this downgrade restores has nowhere to hold which artifacts a
    project issues, so every registered profile version would go silently and
    the fixed-four enumeration ADR-0091 retired would be the only reading left.
    The same discipline as #512's, #599's and #610's downgrades: state what
    cannot be carried back, and stop.
    """

    stored = bind.execute(
        sa.text(
            "select count(*) from information_schema.tables "
            " where table_schema = 'public' "
            "   and table_name = 'project_issue_profiles'"
        )
    ).scalar_one()
    if not stored:
        return
    blocked = bind.execute(
        sa.text("select count(*) from project_issue_profiles")
    ).scalar_one()
    if blocked:
        raise RuntimeError(
            f"#640 downgrade refuses: {blocked} issue profile version(s) record "
            "what each project externally issues, and the schema without them "
            "cannot represent it. Nothing is dropped here."
        )


# --- #529 One immutable release candidate, from one coherent reading --------
#
# ADR-0086 makes one authorized set of customer artifacts the external issue
# unit, and ADR-0091 makes the membership of that set per-project configuration
# with only the updated UCM mandatory.  #640 modelled the configuration and #641
# resolved it into executable content; these four relations are what one
# preparation of it leaves behind.
#
# **The mandatory UCM is a column, not a row**, for exactly the reason #640 made
# it one on the profile.  ``release_candidates`` carries the UCM's renderer,
# digest, storage key and size directly, so a candidate with no UCM is
# unrepresentable (the columns are ``not null``) and a candidate with two is
# unrepresentable (there is one set of columns).
# ``release_candidate_artifacts`` admits only what ADR-0091 made a choice, so
# ``updated_ucm`` cannot appear there at all.
#
# **Identity is the digested declaration, not a column somebody remembered to
# fill in.**  ``input_declaration`` holds the exact canonical bytes that bind
# the project, the accepted revision, the previous authorized package or an
# explicit none, the source cutoff, the coverage identity and digest, the issue
# profile row, identity, version and digest, the template and mapping
# registrations and digests, the configured artifact types, every renderer
# identity and version, the product and code revision, and the enabled feature
# flags.  ``content_declaration`` additionally binds the ordered artifact
# identities and their SHA-256 digests.  Both are ``text`` and never ``jsonb``
# for #610's reason — PostgreSQL normalizes key order, whitespace and numbers,
# so the bytes it handed back would no longer be the bytes anybody digested —
# and both carry a check that recomputes SHA-256 over the stored bytes.  A row
# whose declaration does not digest to what it claims cannot be stored.
#
# **The predecessor is a typed reference and nothing else.**
# ``previous_package_id`` points at ``release_packages``, the authorization
# relation #533 populates, through a composite key carrying ``project_id``.  It
# is nullable because "no package has ever been authorized for this project" is
# a real and common state that ADR-0086 requires to be explicit.  There is
# deliberately no column, index or view here that would let a predecessor be
# derived from the newest report by timestamp, from ``external_report_releases``
# (which binds no accepted revision — #635), from the newest render, from the
# newest candidate, or from the adopted baseline.
#
# **A sealed candidate is immutable.** ``enforce_release_record_write`` refuses
# every UPDATE, DELETE and TRUNCATE on all four relations regardless of the
# role attempting it, so ADR-0086's "Corridor never regenerates a sealed
# package in place" is a database invariant and not a convention.  A changed
# artifact, revision or cutoff is a different ``candidate_identity`` and
# therefore a different row.
#
# **A candidate is customer content**, so it carries #531's partition for
# #531's reason: a reader that forgets its ``where`` clause must see nothing
# rather than another customer's issue.

RELEASE_CANDIDATE_TABLES = (
    "release_packages",
    "release_candidates",
    "release_candidate_artifacts",
    "release_preparation_refusals",
)

# Every reason preparation may refuse for, spelled once.  A bounded vocabulary
# rather than free text: a refusal a person has to read a paragraph to classify
# is a refusal nobody counts, and an open string would let a renderer's own
# traceback become the recorded reason.
PREPARATION_REFUSAL_REASONS = (
    "unsupported_issue_configuration",
    "renderer_failed",
    "artifact_missing",
    "storage_failed",
    "digest_mismatch",
    "inputs_changed_while_rendering",
    "mixed_reading",
    "candidate_identity_conflict",
)

_PREPARATION_REFUSAL_REASONS_SQL = ", ".join(
    f"'{reason}'" for reason in PREPARATION_REFUSAL_REASONS
)

RELEASE_READINESS_STATES = ("ready", "ready_with_exceptions", "blocked")

_RELEASE_READINESS_SQL = ", ".join(f"'{state}'" for state in RELEASE_READINESS_STATES)

RELEASE_CANDIDATE_SCHEMA = f"""
-- A candidate binds one profile version by its whole identity, so the row it
-- names and the identity and version it records cannot describe two profiles.
alter table public.project_issue_profiles
    add constraint uq_project_issue_profiles_binding
    unique (id, project_id, profile_identity, profile_version);

-- The authorization relation #533 populates. It is created empty and on
-- purpose: a candidate's predecessor is a typed reference to an authorized
-- package, so the relation has to exist before anything can point at it, and
-- nothing in #529 writes a row here. Every column that would make a package a
-- package -- the accepted revision it covers, the person who authorized it,
-- and when -- is `not null`, so #533 cannot populate it with a release that
-- binds no revision, which is precisely what #635 found wrong with
-- `external_report_releases`.
create table public.release_packages (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    package_identity character varying(160) not null,
    accepted_revision_id bigint not null,
    authorized_by_principal character varying(128) not null,
    authorized_at timestamp with time zone not null,
    recorded_at timestamp with time zone not null default now(),
    constraint uq_release_packages_row unique (id, project_id),
    constraint uq_release_packages_identity
        unique (project_id, package_identity),
    constraint fk_release_packages_revision foreign key
        (accepted_revision_id, project_id)
        references public.project_record_revisions (id, project_id),
    constraint ck_release_packages_identity check (
        length(btrim(package_identity)) > 0
    ),
    constraint ck_release_packages_principal check (
        length(btrim(authorized_by_principal)) > 0
    )
);

create table public.release_candidates (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    -- The SHA-256 of `input_declaration`, and therefore of every bound input.
    candidate_identity character varying(64) not null,
    input_declaration text not null,
    input_schema_version character varying(64) not null,
    -- The SHA-256 of `content_declaration`, which repeats the input identity
    -- and adds the ordered artifact identities and their own digests.
    content_sha256 character varying(64) not null,
    content_declaration text not null,
    accepted_revision_id bigint not null,
    -- Null is ADR-0086's explicit absence: before a project's first authorized
    -- package there is no predecessor, and none is invented.
    previous_package_id bigint,
    source_cutoff timestamp with time zone not null,
    coverage_identity character varying(160) not null,
    coverage_sha256 character varying(64) not null,
    issue_profile_id bigint not null,
    issue_profile_identity character varying(160) not null,
    issue_profile_version integer not null,
    issue_profile_sha256 character varying(64) not null,
    -- The registrations these artifacts render through, named rather than
    -- copied: the digest each one carries stays on the registration that owns
    -- it (#598), and the `input_declaration` above binds it by value so the
    -- candidate identity still changes when a registration does.
    output_template_format_id bigint not null,
    output_template_kind character varying(32)
        generated always as ('output_template') stored,
    field_mapping_format_id bigint not null,
    field_mapping_kind character varying(32)
        generated always as ('field_mapping') stored,
    code_revision character varying(160) not null,
    product_revision character varying(64) not null,
    -- ADR-0091's one mandatory member, held as a property of the candidate.
    -- No UCM and two UCMs are both unrepresentable here.
    ucm_renderer_identity character varying(160) not null,
    ucm_renderer_version character varying(64) not null,
    ucm_content_sha256 character varying(64) not null,
    ucm_storage_key character varying(160) not null,
    ucm_byte_count bigint not null,
    readiness character varying(32) not null,
    prepared_by_principal character varying(128) not null,
    -- The business instant the caller declared, never a clock reading.
    prepared_at timestamp with time zone not null,
    recorded_at timestamp with time zone not null default now(),
    constraint uq_release_candidates_row unique (id, project_id),
    -- One candidate per bound input set. A repeated preparation of the same
    -- inputs converges here rather than writing a second row, and different
    -- bytes under the same identity collide instead of overwriting.
    constraint uq_release_candidates_identity
        unique (project_id, candidate_identity),
    constraint fk_release_candidates_revision foreign key
        (accepted_revision_id, project_id)
        references public.project_record_revisions (id, project_id),
    constraint fk_release_candidates_previous foreign key
        (previous_package_id, project_id)
        references public.release_packages (id, project_id),
    constraint fk_release_candidates_profile foreign key
        (issue_profile_id, project_id, issue_profile_identity,
         issue_profile_version)
        references public.project_issue_profiles
        (id, project_id, profile_identity, profile_version),
    constraint fk_release_candidates_template foreign key
        (output_template_format_id, project_id, output_template_kind)
        references public.project_baseline_formats (id, project_id, format_kind),
    constraint fk_release_candidates_mapping foreign key
        (field_mapping_format_id, project_id, field_mapping_kind)
        references public.project_baseline_formats (id, project_id, format_kind),
    constraint ck_release_candidates_identity_digest check (
        encode(sha256(convert_to(input_declaration, 'utf8')), 'hex')
            = candidate_identity
    ),
    constraint ck_release_candidates_content_digest check (
        encode(sha256(convert_to(content_declaration, 'utf8')), 'hex')
            = content_sha256
    ),
    constraint ck_release_candidates_schema check (
        length(btrim(input_schema_version)) > 0
    ),
    constraint ck_release_candidates_coverage check (
        length(btrim(coverage_identity)) > 0
    ),
    constraint ck_release_candidates_ucm check (
        length(btrim(ucm_renderer_identity)) > 0
        and length(btrim(ucm_renderer_version)) > 0
        and length(btrim(ucm_storage_key)) > 0
        and ucm_byte_count > 0
    ),
    constraint ck_release_candidates_readiness check (
        readiness in ({_RELEASE_READINESS_SQL})
    ),
    constraint ck_release_candidates_principal check (
        length(btrim(prepared_by_principal)) > 0
    ),
    constraint ck_release_candidates_profile_version check (
        issue_profile_version >= 1
    )
);

create index ix_release_candidates_project_prepared
    on public.release_candidates (project_id, prepared_at);

create table public.release_candidate_artifacts (
    id bigserial primary key,
    candidate_id bigint not null,
    project_id bigint not null,
    -- 'updated_ucm' is deliberately not admitted: the mandatory member is the
    -- candidate's own column, so it can be neither dropped nor doubled.
    artifact_type character varying(48) not null,
    renderer_identity character varying(160) not null,
    renderer_version character varying(64) not null,
    content_sha256 character varying(64) not null,
    storage_key character varying(160) not null,
    byte_count bigint not null,
    -- The position this artifact takes in the content digest's ordering, so a
    -- reader can reconstruct the digested sequence from the rows themselves.
    position integer not null,
    constraint uq_release_candidate_artifacts_type
        unique (candidate_id, artifact_type),
    constraint uq_release_candidate_artifacts_position
        unique (candidate_id, position),
    -- Both halves of the key, so an artifact of one project cannot be attached
    -- to another project's candidate.
    constraint fk_release_candidate_artifacts_candidate foreign key
        (candidate_id, project_id)
        references public.release_candidates (id, project_id),
    constraint ck_release_candidate_artifacts_type check (
        artifact_type in ({_CONFIGURED_ARTIFACT_TYPES_SQL})
    ),
    constraint ck_release_candidate_artifacts_renderer check (
        length(btrim(renderer_identity)) > 0
        and length(btrim(renderer_version)) > 0
    ),
    constraint ck_release_candidate_artifacts_bytes check (
        length(btrim(storage_key)) > 0 and byte_count > 0
    ),
    constraint ck_release_candidate_artifacts_position check (position >= 1)
);

create index ix_release_candidate_artifacts_project_id
    on public.release_candidate_artifacts (project_id);

-- The receipt a refused preparation leaves. It is the only thing a failed
-- preparation leaves: there is no candidate row and no artifact row to find,
-- so this is where the reason lives.
create table public.release_preparation_refusals (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    -- Null where the inputs never bound far enough to have an identity.
    candidate_identity character varying(64),
    reason_code character varying(48) not null,
    reason text not null,
    refused_by_principal character varying(128) not null,
    refused_at timestamp with time zone not null,
    recorded_at timestamp with time zone not null default now(),
    constraint ck_release_preparation_refusals_code check (
        reason_code in ({_PREPARATION_REFUSAL_REASONS_SQL})
    ),
    -- Bounded in both senses: one of a closed set of codes, and a sentence a
    -- person reads rather than a captured traceback.
    constraint ck_release_preparation_refusals_reason check (
        length(btrim(reason)) > 0 and length(reason) <= 2000
    ),
    constraint ck_release_preparation_refusals_principal check (
        length(btrim(refused_by_principal)) > 0
    )
);

create index ix_release_preparation_refusals_project
    on public.release_preparation_refusals (project_id, refused_at);

create function public.enforce_release_record_write()
    returns trigger
    language plpgsql
    as $$
        begin
            raise exception 'a prepared release record is immutable: a changed artifact, revision or cutoff is a new candidate, never an edit'
                using errcode='23514';
        end; $$;
"""

RELEASE_CANDIDATE_TRIGGERS = "".join(
    f"""
create trigger trg_{table}_immutable
    before update or delete
    on public.{table}
    for each row
    execute function public.enforce_release_record_write();
create trigger trg_{table}_truncate
    before truncate on public.{table}
    for each statement
    execute function public.enforce_release_record_write();
"""
    for table in RELEASE_CANDIDATE_TABLES
)

RELEASE_CANDIDATE_SCHEMA_DOWN = """
drop table if exists public.release_candidate_artifacts cascade;
drop table if exists public.release_preparation_refusals cascade;
drop table if exists public.release_candidates cascade;
drop table if exists public.release_packages cascade;
drop function if exists public.enforce_release_record_write() cascade;
alter table public.project_issue_profiles
    drop constraint if exists uq_project_issue_profiles_binding;
"""

# A prepared candidate is one customer's issue, so it is partitioned for #531's
# reason. The refusal receipt is partitioned too: the sentence it carries names
# what one project could not produce.
RELEASE_CANDIDATE_PARTITION_POLICIES = f"""
do $$
declare
    v_roles text;
    v_table text;
begin
    select string_agg(quote_ident(rolname), ', ' order by rolname)
      into v_roles
      from pg_roles
     where rolname in ({_UNPARTITIONED_ROLES_SQL});
    foreach v_table in array array['release_packages', 'release_candidates',
                                   'release_candidate_artifacts',
                                   'release_preparation_refusals'] loop
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


def _refuse_unrepresentable_release_candidate_downgrade(bind) -> None:
    """Refuse rather than drop prepared candidates nothing else records.

    The schema this downgrade restores has nowhere to hold a prepared issue,
    its artifact digests, or why a preparation was refused, so every row would
    go silently and the retained bytes in the object store would become
    unreferenced. The same discipline as #512's, #599's, #610's and #640's
    downgrades: state what cannot be carried back, and stop.
    """

    stored = bind.execute(
        sa.text(
            "select count(*) from information_schema.tables "
            " where table_schema = 'public' "
            "   and table_name = 'release_candidates'"
        )
    ).scalar_one()
    if not stored:
        return
    blocked = bind.execute(
        sa.text(
            "select (select count(*) from release_candidates) "
            "     + (select count(*) from release_preparation_refusals) "
            "     + (select count(*) from release_packages)"
        )
    ).scalar_one()
    if blocked:
        raise RuntimeError(
            f"#529 downgrade refuses: {blocked} prepared release record(s) "
            "state what one issue contained and why, and the schema without "
            "them cannot represent it. Nothing is dropped here."
        )


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
    # --- Resolve Delta (#519) ---------------------------------------------
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

    # --- Review Packet resolution (#526) ----------------------------------
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
            f"grant select, insert on public.{table} to {RESOLVE_DELTA_ROLE}"
        )
        op.execute(
            f"grant usage, select on sequence public.{table}_id_seq "
            f"to {RESOLVE_DELTA_ROLE}"
        )
    for body in (
        RECORD_DELTA_FOLLOW_UP_PLAN,
        RECORD_REVIEW_PACKET_RECEIPT,
        REVERSE_REVIEW_PACKET,
    ):
        op.execute(body)
    for name, signature in REVIEW_PACKET_COMMANDS.items():
        op.execute(
            f"alter function public.{name}{signature} owner to {RESOLVE_DELTA_ROLE}"
        )
        op.execute(f"revoke all on function public.{name}{signature} from public")
        # Saving a packet, recording a Follow-up Plan, and undoing the act are
        # attributable human acts, so they join the other decision commands on
        # the web capability alone.
        op.execute(
            f"grant execute on function public.{name}{signature} to corridor_web"
        )

    # --- Spine-native Recorded Verbal origin (#512) -----------------------
    op.execute(RECORDED_VERBAL_ORIGIN_SCHEMA)
    # The backfill runs while `source_segments.statement_id` still exists: it
    # is the only place the legacy attestation can be read from.
    _with_append_only_guards_lifted(op.get_bind(), _backfill_recorded_verbal_origins)
    op.execute(RECORDED_VERBAL_SEGMENT_CUTOVER)
    for table in RECORDED_VERBAL_TABLES:
        # The origin, its legacy mapping, and the backfill receipts are read
        # by the application; only the command writes the first two, and only
        # this transition ever writes the receipts.
        op.execute(f"grant select on public.{table} to {RUNTIME_LOGINS}")
        op.execute(
            f"revoke insert, update, delete, truncate on public.{table} "
            f"from {RUNTIME_LOGINS}"
        )
    for table in RECORDED_VERBAL_APPEND_TABLES:
        op.execute(f"grant select, insert on public.{table} to {SOURCE_APPEND_ROLE}")
    op.execute(
        "grant usage, select on sequence public.recorded_verbal_origins_id_seq "
        f"to {SOURCE_APPEND_ROLE}"
    )
    # The segment command's third parameter is now the spine-native origin.
    op.execute(
        "drop function public.append_source_segments(bigint, bigint, bigint, jsonb)"
    )
    op.execute(APPEND_RECORDED_VERBAL_ORIGIN)
    op.execute(APPEND_SOURCE_SEGMENTS_OVER_ORIGIN)
    for name, signature in RECORDED_VERBAL_COMMANDS.items():
        op.execute(
            f"alter function public.{name}{signature} owner to {SOURCE_APPEND_ROLE}"
        )
        op.execute(f"revoke all on function public.{name}{signature} from public")
        op.execute(
            f"grant execute on function public.{name}{signature} to {RUNTIME_LOGINS}"
        )

    # --- Permanent-state de-duplication (#457) -----------------------------
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
        f"{DEFER_PROPOSED_DELTA_SIGNATURE} owner to {RESOLVE_DELTA_ROLE}"
    )
    op.execute(
        f"revoke all on function public.defer_proposed_delta"
        f"{DEFER_PROPOSED_DELTA_SIGNATURE} from public"
    )
    op.execute(
        f"grant execute on function public.defer_proposed_delta"
        f"{DEFER_PROPOSED_DELTA_SIGNATURE} to corridor_web"
    )

    # --- One delivery ledger for both transports (#599) --------------------
    # After #457, because it renames and re-keys the family that block just
    # constrained, and because the identity it establishes is the one #457
    # proved these rows already satisfy.
    delivery_snapshot = _push_delivery_snapshot(op.get_bind())
    op.execute(UNIFIED_DELIVERY_SCHEMA)
    _reconcile_unified_delivery_family(op.get_bind(), delivery_snapshot)
    for table in UNIFIED_DELIVERY_TABLES:
        # The same terms the #511 block set for the delivery ledger this block
        # renames: an advance and the coverage that makes it safe are records
        # of what happened, so a runtime capability appends one and can never
        # edit or erase one.
        op.execute(f"grant select, insert on public.{table} to {RUNTIME_LOGINS}")
        op.execute(
            f"grant usage, select on sequence public.{table}_id_seq "
            f"to {RUNTIME_LOGINS}"
        )
        op.execute(
            f"revoke update, delete, truncate on public.{table} "
            f"from {RUNTIME_LOGINS}"
        )

    # --- #610 The stored mapping-revision declaration ----------------------
    # Last, because it constrains and extends the #509 registration above: the
    # unique key its composite foreign key resolves against belongs to a table
    # that block creates.
    op.execute(BASELINE_FORMAT_MANIFEST_SCHEMA)
    for table in BASELINE_FORMAT_MANIFEST_TABLES:
        # The same terms the #509 block set for the registration this row
        # belongs to: the application reads a stored mapping revision and
        # writes none of it, and only the record-decision role's command
        # stores one. The row is keyed by the registration's id, so there is
        # no sequence to grant.
        op.execute(f"grant select on public.{table} to {RUNTIME_LOGINS}")
        op.execute(
            f"revoke insert, update, delete, truncate on public.{table} "
            f"from {RUNTIME_LOGINS}"
        )
        op.execute(f"grant select, insert on public.{table} to {OPERATING_MODE_ROLE}")
    op.execute(ATTACH_BASELINE_FORMAT_MANIFEST)
    op.execute(
        f"alter function public.attach_baseline_format_manifest"
        f"{ATTACH_BASELINE_FORMAT_MANIFEST_SIGNATURE} owner to "
        f"{OPERATING_MODE_ROLE}"
    )
    op.execute(
        f"revoke all on function public.attach_baseline_format_manifest"
        f"{ATTACH_BASELINE_FORMAT_MANIFEST_SIGNATURE} from public"
    )
    # Storing the declaration is part of the same attributable human act that
    # registers the revision, so it joins that command on the web capability.
    op.execute(
        f"grant execute on function public.attach_baseline_format_manifest"
        f"{ATTACH_BASELINE_FORMAT_MANIFEST_SIGNATURE} to corridor_web"
    )

    # --- #602 Report Runs bound to the accepted revision -------------------
    # Last, because both bindings resolve against a unique key this block adds
    # to project_record_revisions, and the trigger reads that table. The two
    # report relations keep the grants they already have: a column added to a
    # table the runtime may already insert into needs none of its own.
    op.execute(REPORT_REVISION_BINDING_SCHEMA)

    # --- #605 One stored extractor configuration, and segment citations ----
    # Last, because the run constraint it replaces resolves against a table
    # created here, and because the citation table's composite key resolves
    # against source_segments, which every earlier block in this transition
    # may still be granting and constraining.
    op.execute(EXTRACTOR_CONFIGURATION_REGISTRY_SCHEMA)
    op.execute(EXTRACTION_RUN_CONFIG_REFERENCE_SCHEMA)
    for table in EXTRACTOR_CONFIGURATION_TABLES:
        # A configuration is registered by the runtime that seals it and is
        # never edited afterwards, so the capability appends and reads and
        # holds no other write. The key is the digest, so there is no
        # sequence to grant.
        op.execute(f"grant select, insert on public.{table} to {RUNTIME_LOGINS}")
        op.execute(
            f"revoke update, delete, truncate on public.{table} "
            f"from {RUNTIME_LOGINS}"
        )
    op.execute(EVIDENCE_SEGMENT_CITATION_SCHEMA)
    for table in EVIDENCE_SEGMENT_CITATION_TABLES:
        # A citation is a record of what an Evidence Link names, so a runtime
        # capability appends one and can never edit or erase one.
        op.execute(f"grant select, insert on public.{table} to {RUNTIME_LOGINS}")
        op.execute(
            f"grant usage, select on sequence public.{table}_id_seq "
            f"to {RUNTIME_LOGINS}"
        )
        op.execute(
            f"revoke update, delete, truncate on public.{table} "
            f"from {RUNTIME_LOGINS}"
        )

    # --- #633 The retained report reading is evidence, not a cache ---------
    # Next to last, because it re-describes a column #602's block above created
    # the description for. Comments only: the column, its type and every
    # constraint on it are exactly what #602 left.
    op.execute(REPORT_READING_PAYLOAD_COMMENT)

    # --- #531 Project authorization is a data partition --------------------
    # Last, because the policies it creates reference the spine tables every
    # block above builds, and the commands it creates read the roster.
    op.execute(PROJECT_PARTITION_SCHEMA)
    op.execute(f"grant select on public.project_partition_secrets to {SOURCE_APPEND_ROLE}")
    op.execute(
        f"grant select on public.project_roster_entries to {SOURCE_APPEND_ROLE}"
    )
    for body in (
        SEAL_PROJECT_PARTITION,
        CURRENT_PROJECT_PARTITION,
        OPEN_PROJECT_PARTITION,
        OPEN_MEMBER_PROJECT_PARTITION,
        CLOSE_PROJECT_PARTITION,
    ):
        op.execute(body)
    for name, signature in PARTITION_COMMANDS.items():
        op.execute(
            f"alter function public.{name}{signature} owner to {SOURCE_APPEND_ROLE}"
        )
        # PostgreSQL grants EXECUTE to PUBLIC on every new function (#545), so
        # each command takes PUBLIC back before anything is granted at all.
        op.execute(f"revoke all on function public.{name}{signature} from public")
    for name in PARTITION_COMMANDS_GRANTED:
        op.execute(
            f"grant execute on function public.{name}{PARTITION_COMMANDS[name]} "
            f"to {RUNTIME_LOGINS}"
        )
    op.execute(PROJECT_PARTITION_POLICIES)

    # --- #640 The per-project external-issue profile -----------------------
    # Last, because the profile references the registered output template and
    # field mapping the Adopt Baseline block above creates, and its row-level
    # security calls the partition command the block above that creates.
    op.execute(ISSUE_PROFILE_SCHEMA)
    op.execute(REGISTER_PROJECT_ISSUE_PROFILE)
    for table in ISSUE_PROFILE_TABLES:
        # A new table arrives carrying the schema owner's default privileges,
        # which hand every runtime login full access, so the write half is
        # taken back explicitly and only the command's role keeps it. The
        # application reads what the project is configured to issue and can
        # never change it without the attributable act.
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
    # The command resolves the declared template and mapping against
    # `project_baseline_formats`, which the Adopt Baseline block above already
    # grants this role.
    op.execute(
        f"alter function public.register_project_issue_profile"
        f"{REGISTER_PROJECT_ISSUE_PROFILE_SIGNATURE} owner to "
        f"{OPERATING_MODE_ROLE}"
    )
    for function in (
        f"register_project_issue_profile{REGISTER_PROJECT_ISSUE_PROFILE_SIGNATURE}",
        "enforce_project_issue_profile_write()",
    ):
        op.execute(f"revoke all on function public.{function} from public")
    # Configuring the issue set is an attributable human act, so it joins the
    # other decision commands on the web capability alone.
    op.execute(
        f"grant execute on function public.register_project_issue_profile"
        f"{REGISTER_PROJECT_ISSUE_PROFILE_SIGNATURE} to corridor_web"
    )
    op.execute(ISSUE_PROFILE_PARTITION_POLICIES)

    # --- #529 One immutable release candidate, from one coherent reading ---
    # Last, because a candidate references the issue profile version the block
    # above creates, the registered template and mapping the Adopt Baseline
    # block creates, and the accepted revision the spine creates, and its
    # row-level security calls the partition command #531 creates.
    op.execute(RELEASE_CANDIDATE_SCHEMA)
    op.execute(RELEASE_CANDIDATE_TRIGGERS)
    for table in RELEASE_CANDIDATE_TABLES:
        # A new table arrives carrying the schema owner's default privileges,
        # which hand every runtime login full access including UPDATE and
        # DELETE. #640 had to take that back explicitly and so does this: a
        # sealed candidate is immutable, so no login holds a privilege that
        # would change one, and the immutability trigger above is the second
        # line rather than the only one.
        op.execute(
            f"revoke all on public.{table} from {RUNTIME_LOGINS}"
        )
        op.execute(f"grant select on public.{table} to {RUNTIME_LOGINS}")
    # Preparation is application work: it writes no accepted authority and
    # makes nothing effective, so the runtime capabilities append candidates,
    # artifacts and refusal receipts directly rather than through a
    # `SECURITY DEFINER` command. What they may never do is change one.
    for table in (
        "release_candidates",
        "release_candidate_artifacts",
        "release_preparation_refusals",
    ):
        op.execute(f"grant insert on public.{table} to {RUNTIME_LOGINS}")
        op.execute(
            f"grant usage, select on sequence public.{table}_id_seq "
            f"to {RUNTIME_LOGINS}"
        )
    # `release_packages` is deliberately read-only for every runtime login.
    # Authorizing a package is #533's attributable act, and #533 grants
    # whatever writes that act needs; preparing a candidate must not be able
    # to invent its own predecessor.
    op.execute(
        f"revoke insert, update, delete, truncate on public.release_packages "
        f"from {RUNTIME_LOGINS}"
    )
    op.execute(
        "revoke all on function public.enforce_release_record_write() from public"
    )
    op.execute(RELEASE_CANDIDATE_PARTITION_POLICIES)


def downgrade() -> None:
    """Drop the commands and hand the raw source-table writes back.

    The Support Assessment relation was born in this transition, so the
    downgrade removes it whole rather than opening it to raw writes.
    """

    # --- #529 One immutable release candidate, from one coherent reading ---
    # First, because the upgrade added it last. Every prepared candidate and
    # every refusal receipt would go silently, so this refuses instead.
    _refuse_unrepresentable_release_candidate_downgrade(op.get_bind())
    op.execute(RELEASE_CANDIDATE_SCHEMA_DOWN)

    # --- #640 The per-project external-issue profile -----------------------
    # First, because the upgrade added it last. Every registered profile
    # version would go silently, so this refuses instead of dropping one.
    _refuse_unrepresentable_issue_profile_downgrade(op.get_bind())
    op.execute(
        f"drop function if exists public.register_project_issue_profile"
        f"{REGISTER_PROJECT_ISSUE_PROFILE_SIGNATURE}"
    )
    op.execute(ISSUE_PROFILE_SCHEMA_DOWN)

    # --- #531 Project authorization is a data partition --------------------
    # First, because the upgrade added it last. Nothing is lost: the partition
    # holds no data of its own, and the seal it drops is regenerated whenever
    # the block is applied again.
    op.execute(PROJECT_PARTITION_POLICIES_DOWN)
    for name, signature in PARTITION_COMMANDS.items():
        op.execute(f"drop function if exists public.{name}{signature}")
    op.execute(
        f"revoke select on public.project_roster_entries from {SOURCE_APPEND_ROLE}"
    )
    op.execute(PROJECT_PARTITION_SCHEMA_DOWN)

    # --- #633 The retained report reading is evidence, not a cache ---------
    # Next, because the upgrade added it last but one. Restoring #602's wording
    # is the whole of it: nothing was added, so nothing can be lost, and #602's
    # block further down is what clears the description entirely.
    op.execute(REPORT_READING_PAYLOAD_COMMENT_DOWN)

    # --- #605 One stored extractor configuration, and segment citations ----
    # First among what remains, because the upgrade added it last but one. A
    # run that stores no copy of
    # its configuration, and a citation that names a segment instead of
    # copying its words, are both unrepresentable in the shape this restores,
    # so each refuses rather than losing what it cannot carry back.
    _refuse_unreferenced_evidence_citation_downgrade(op.get_bind())
    op.execute(EVIDENCE_SEGMENT_CITATION_SCHEMA_DOWN)
    _refuse_unreferenced_configuration_downgrade(op.get_bind())
    op.execute(EXTRACTOR_CONFIGURATION_REGISTRY_SCHEMA_DOWN)

    # --- #602 Report Runs bound to the accepted revision -------------------
    # Next, because the upgrade added it last but one. Every binding already
    # recorded would go silently, so this refuses instead of dropping one.
    _refuse_unrepresentable_report_binding_downgrade(op.get_bind())
    op.execute(REPORT_REVISION_BINDING_SCHEMA_DOWN)

    # --- #610 The stored mapping-revision declaration ----------------------
    # Next, because the upgrade added it last but one. Every stored
    # declaration would go silently, so this refuses instead of dropping one.
    _refuse_unrepresentable_manifest_downgrade(op.get_bind())
    op.execute(
        f"drop function if exists public.attach_baseline_format_manifest"
        f"{ATTACH_BASELINE_FORMAT_MANIFEST_SIGNATURE}"
    )
    for table in BASELINE_FORMAT_MANIFEST_TABLES:
        op.execute(
            f"do $$ begin "
            f"if exists (select 1 from pg_tables where schemaname = 'public' "
            f"and tablename = '{table}') then "
            f"revoke select on public.{table} from {RUNTIME_LOGINS}; "
            f"end if; end $$;"
        )
    op.execute(BASELINE_FORMAT_MANIFEST_SCHEMA_DOWN)

    # --- One delivery ledger for both transports (#599) --------------------
    # Next again, third from the end of the upgrade: the family goes back to
    # being the push half alone, and the #457 block below then unwinds the
    # constraints it re-establishes on the restored name.
    _refuse_unrepresentable_delivery_downgrade(op.get_bind())
    for table in UNIFIED_DELIVERY_TABLES:
        op.execute(
            f"do $$ begin "
            f"if exists (select 1 from pg_tables where schemaname = 'public' "
            f"and tablename = '{table}') then "
            f"revoke select, insert on public.{table} from {RUNTIME_LOGINS}; "
            f"end if; end $$;"
        )
    op.execute(UNIFIED_DELIVERY_SCHEMA_DOWN)

    # --- Permanent-state de-duplication (#457) -----------------------------
    # First among what remains, because the upgrade added it last.  Only the
    # constraints and the delivery-identity guard are undone here: the two
    # commands whose bodies this block replaced keep their signatures, and the
    # loops below drop them by that signature exactly as they did before #457.
    op.execute(DEDUPLICATED_IDENTITIES_DOWN)

    # --- Spine-native Recorded Verbal origin (#512) -----------------------
    for name, signature in RECORDED_VERBAL_COMMANDS.items():
        op.execute(f"drop function if exists public.{name}{signature}")
    op.execute(RECORDED_VERBAL_SEGMENT_RESTORE_COLUMN)
    _with_append_only_guards_lifted(
        op.get_bind(), _restore_legacy_recorded_verbal_state
    )
    op.execute(RECORDED_VERBAL_SEGMENT_CUTOVER_DOWN)
    for table in RECORDED_VERBAL_TABLES:
        op.execute(f"revoke select on public.{table} from {RUNTIME_LOGINS}")
    for table in RECORDED_VERBAL_APPEND_TABLES:
        op.execute(f"revoke select, insert on public.{table} from {SOURCE_APPEND_ROLE}")
    op.execute(
        "revoke usage, select on sequence public.recorded_verbal_origins_id_seq "
        f"from {SOURCE_APPEND_ROLE}"
    )
    op.execute(RECORDED_VERBAL_ORIGIN_SCHEMA_DOWN)
    # --- Review Packet resolution (#526) ----------------------------------
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

    # --- Project-bound push intake (#511) ---------------------------------
    op.execute(PUSH_INTAKE_SCHEMA_DOWN)
    # --- Resolve Delta (#519) ---------------------------------------------
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

