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

The transition then binds every new Unconfirmed reading to the processing
observation that produced it (#809), once more because the window holds one
unreleased transition.  #804 recorded a Textract-only cell as an
``unreadable_cell_resolutions`` row with no durable link to the provider
observation behind it; the request identity, the submitted raster, the response
and reading digests and the authorization record lived only in Class B files.
``scanned_page_observations`` is that observation as a row — one per
``analyze_page`` binding ingest consumed, identified by its references rather
than by a copy of any of them — and the resolution names it by id, beside the
routed region the cell fell in.  ``run_id`` keeps meaning the reading harness's
run and stays null on the scanned route; the rejected alternative was an
``extraction_run_id`` column, which would have needed a placeholder Extraction
Run to point at.  Readings that already existed are marked as predating the
binding rather than bound to a guess, and the downgrade refuses while an
observation exists.

Every family above except this revision's own append commands now lives in
``corridor.migrations.source_append_commands`` — one module per family, each
opening with the block comment that used to sit above its constants, each
carrying its own SQL, its helpers, its ``upgrade``/``downgrade`` pair and its
downgrade guard.  Alembic lists ``*.py`` in the configured version location and
does not descend into directories, so this file remains the whole of the
executable revision and that package is never scanned as migration history.
``upgrade`` and ``downgrade`` below compose the families; the order is the
order, and the comment beside each call is the reason it stands where it does.
"""

from __future__ import annotations

from hashlib import sha256
import json

from alembic import op

from corridor.migrations.source_append_commands import (
    baseline_format_manifest,
    baseline_record,
    capture_correction,
    coverage_preparation,
    delta_deduplication,
    environment_binding,
    extractor_configuration,
    follow_up_plan_closure,
    issue_profile,
    native_segments,
    operating_mode,
    outgoing_requests,
    partition_declaration,
    partition_seal,
    preparation_supervisor,
    product_upload_delivery,
    project_partition,
    public_privileges,
    push_intake,
    recorded_verbal,
    release_candidate,
    release_package,
    report_reading_payload,
    report_revision_binding,
    resolve_delta,
    review_packets,
    scanned_observations,
    source_revision_declaration,
    spend_authorization,
    unified_delivery,
    web_capability,
)

# The Fact identity recipe frozen below reads the family's own role map.
from corridor.migrations.source_append_commands.recorded_verbal import (
    MATERIALIZED_SOURCE_LINKS,
)
from corridor.migrations.source_append_commands.roles import (
    ASSESSMENTS_SQL,
    COMMANDS,
    DELTA_TABLES,
    EVIDENCE_ROLES_SQL,
    REFERENCED_TABLES,
    RUNTIME_LOGINS,
    SOURCE_APPEND_ROLE,
    SOURCE_TABLES,
    SUPPORT_TABLES,
)

# `APPEND_NATIVE_SOURCE_SEGMENTS` is handed to `corridor.migrations.email_spine`
# below.  The rest are re-exports rather than uses: `tests/test_architecture.py`
# loads this file by path and compares the frozen #680 and #693 boundary copies
# the revision carries against `corridor.web_boundary`, so those names must keep
# resolving here.  Every other name a family owns is read from that family's own
# module.
from corridor.migrations.source_append_commands.native_segments import (
    APPEND_NATIVE_SOURCE_SEGMENTS,
)
from corridor.migrations.source_append_commands.public_privileges import (
    PUBLIC_PRIVILEGE_RESTORE,
    PUBLIC_PRIVILEGE_REVOKE,
    PUBLIC_RELATION_PRIVILEGE_ALLOWLIST,
    PUBLIC_RELATION_PRIVILEGES_AT_THIS_REVISION,
    _text_array_sql,
)
from corridor.migrations.source_append_commands import replay_gate
from corridor.migrations.source_append_commands.web_capability import (
    WEB_DENIED_PARTITIONED,
    WEB_DENIED_RELATIONS,
    WEB_PARTITIONED_TABLES,
)


revision = "b2d5f8a1c4e7"
down_revision = "a1c4e7b0d2f3"
branch_labels = None
depends_on = None


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


# The Fact identity recipe, frozen at this revision.  `corridor.facts`
# computes the same bytes; the backfill reproduces each stored digest with it
# before it replaces one, so a recipe that has drifted refuses instead of
# rewriting.
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
            for role in MATERIALIZED_SOURCE_LINKS[fact_type]
        ],
    }
    return sha256(
        json.dumps(digest_input, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


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
    operating_mode.upgrade(op)

    # --- Adopt Baseline (#509) --------------------------------------------
    baseline_record.upgrade(op)

    # --- Project-bound push intake (#511) ---------------------------------
    push_intake.upgrade(op)

    # --- Resolve Delta (#519) ---------------------------------------------
    resolve_delta.upgrade(op)

    # --- Review Packet resolution (#526) ----------------------------------
    review_packets.upgrade(op)

    # --- Spine-native Recorded Verbal origin (#512) -----------------------
    recorded_verbal.upgrade(op, _frozen_fact_digest, revision)

    # --- Permanent-state de-duplication (#457) -----------------------------
    delta_deduplication.upgrade(op)

    # --- One delivery ledger for both transports (#599) --------------------
    unified_delivery.upgrade(op)

    # --- #610 The stored mapping-revision declaration ----------------------
    baseline_format_manifest.upgrade(op)

    # --- #602 Report Runs bound to the accepted revision -------------------
    report_revision_binding.upgrade(op)

    # --- #605 One stored extractor configuration, and segment citations ----
    extractor_configuration.upgrade(op)

    # --- #633 The retained report reading is evidence, not a cache ---------
    report_reading_payload.upgrade(op)

    # --- #531 Project authorization is a data partition --------------------
    project_partition.upgrade(op)

    # --- #640 The per-project external-issue profile -----------------------
    issue_profile.upgrade(op)

    # --- #529 One immutable release candidate, from one coherent reading ---
    release_candidate.upgrade(op)

    # --- #533 One authorized package, and the receipt that binds it --------
    release_package.upgrade(op)

    # --- #657 One transaction holds one scope, and the record is partitioned -
    partition_declaration.upgrade(op)

    # --- #676 A seal binds a scope to the transaction that earned it -------
    partition_seal.upgrade(op)

    # --- #675 The confirmed coverage declaration, and the preparation it asks
    coverage_preparation.upgrade(op)

    # --- One activation ledger for every ADR-0050 replay gate -------------
    # Before the web-capability block, because that block revokes by name on
    # the relations that exist when it runs: this family's one relation is what
    # it denies, and the four it replaces are gone by then.
    replay_gate.upgrade(op)

    # --- #680 The live-pilot web capability boundary -------------------
    web_capability.upgrade(op)

    # --- #690 The preparation a worker actually runs ----------------------
    preparation_supervisor.upgrade(op)

    # --- Native PDF readings and pipeline qualification (#736, #737) -----
    native_segments.upgrade(op)

    # --- The bound customer environment (#656) ---------------------------
    environment_binding.upgrade(op)

    # --- #652 Retained outgoing requests ----------------------------------
    outgoing_requests.upgrade(op)

    # --- #809 The observation an Unconfirmed reading was read out of -------
    # After every block that creates a relation it could bind to and before
    # the sibling transitions: it alters `unreadable_cell_resolutions`, which
    # the baseline created, and nothing later in the revision names its table.
    scanned_observations.upgrade(op)

    # --- #811 One spend authorization the five assistant configurations name -
    # After the observation binding and before the sibling transitions: it
    # alters the five configuration relations the baseline created and creates
    # one relation nothing later in the revision names.
    spend_authorization.upgrade(op)

    # --- #823 A product upload is a delivery, and its confirmation ---------
    # After the spend authorization and before the sibling transitions: it
    # re-states one `unified_delivery` constraint and creates one relation
    # nothing later in the revision names.
    product_upload_delivery.upgrade(op)

    # --- #825 What a coordinator declared about one delivery --------------
    # After the product upload delivery and before the sibling transitions: it
    # names `source_deliveries` and the project composite key the blocks above
    # established, and creates one relation nothing later in the revision
    # names.
    source_revision_declaration.upgrade(op)

    # --- #835 How a Follow-up Plan stops being an outside ask -------------
    # After the source revision declaration and before the sibling
    # transitions. It is the last block that may run here: its composite
    # foreign key names `delta_follow_up_plans`, which `review_packets`
    # creates, and its designation trigger is #839's function, which
    # `coverage_preparation` creates -- so it follows both, and creates one
    # relation nothing later in the revision names.
    follow_up_plan_closure.upgrade(op)

    # --- #836 What a coordinator reported about one capture ---------------
    # After `review_packets`, whose `proposed_deltas` project-scoped unique its
    # composite foreign key names, and after the spine relations the baseline
    # created, whose `(project_id, document_id, id)` uniques carry the rest of
    # the binding. It creates one relation nothing later in the revision names.
    capture_correction.upgrade(op)

    from corridor.migrations import email_spine

    email_spine.upgrade(op, APPEND_NATIVE_SOURCE_SEGMENTS, APPEND_FACT)
    from corridor.migrations import project_contacts

    project_contacts.upgrade(op)
    from corridor.migrations import minutes_spine

    minutes_spine.upgrade(op)
    from corridor.migrations import impact_derivations

    impact_derivations.upgrade(op)
    from corridor.migrations import retirement_watermark

    retirement_watermark.upgrade(op)
    from corridor import shadow_schema

    shadow_schema.install(op)
    from corridor.migrations import legacy_history, coordination_history, support_history

    legacy_history.upgrade(op)
    coordination_history.upgrade(op)
    support_history.upgrade(op)

    # --- #693 No application relation carries a privilege granted to PUBLIC
    public_privileges.upgrade(op)


def downgrade() -> None:
    """Drop the commands and hand the raw source-table writes back.

    The Support Assessment relation was born in this transition, so the
    downgrade removes it whole rather than opening it to raw writes.
    """

    from corridor.migrations import email_spine

    from corridor.migrations import project_contacts

    from corridor.migrations import minutes_spine

    from corridor.migrations import impact_derivations

    from corridor.migrations import retirement_watermark

    from corridor import shadow_schema

    from corridor.migrations import legacy_history, coordination_history, support_history

    support_history.downgrade(op)
    coordination_history.downgrade(op)
    legacy_history.downgrade(op)
    shadow_schema.uninstall(op)
    retirement_watermark.downgrade(op)
    impact_derivations.downgrade(op)
    minutes_spine.downgrade(op)
    project_contacts.downgrade(op)
    email_spine.downgrade(op)

    # --- #836 What a coordinator reported about one capture ---------------
    # First among the feature reversals, because the upgrade added it last,
    # and before the Proposed Delta and spine relations its composite foreign
    # keys name unwind.
    capture_correction.downgrade(op)

    # --- #835 How a Follow-up Plan stops being an outside ask -------------
    # Next, because the upgrade added it second from the end, and before the
    # plan relation its composite foreign key names unwinds.
    follow_up_plan_closure.downgrade(op)

    # --- #825 What a coordinator declared about one delivery --------------
    # Next, because the upgrade added it second from the end, and before the
    # delivery ledger its composite foreign key names unwinds.
    source_revision_declaration.downgrade(op)

    # --- #823 A product upload is a delivery, and its confirmation ---------
    # Next, mirroring its place second from the end of the upgrade, and before
    # `unified_delivery` unwinds the family it constrains.
    product_upload_delivery.downgrade(op)

    # --- #811 One spend authorization the five assistant configurations name -
    # Next, mirroring its place second from the end of the upgrade.
    spend_authorization.downgrade(op)

    # --- #809 The observation an Unconfirmed reading was read out of -------
    scanned_observations.downgrade(op)

    # --- The bound customer environment (#656) ---------------------------
    environment_binding.downgrade(op)

    # --- Native PDF readings and pipeline qualification (#736, #737) -----
    native_segments.downgrade(op)

    # --- #693 No application relation carries a privilege granted to PUBLIC
    public_privileges.downgrade(op)

    # --- #652 Retained outgoing requests ----------------------------------
    outgoing_requests.downgrade(op)

    # --- #690 The preparation a worker actually runs ----------------------
    preparation_supervisor.downgrade(op)

    # --- #680 The live-pilot web capability boundary -------------------
    web_capability.downgrade(op)

    # --- One activation ledger for every ADR-0050 replay gate -------------
    # After the web-capability restore, which hands the privilege back on the
    # one relation before this family takes it apart into four again.
    replay_gate.downgrade(op)

    # --- #675 The confirmed coverage declaration, and the preparation it asks
    coverage_preparation.downgrade(op)

    # --- #676 A seal binds a scope to the transaction that earned it -------
    partition_seal.downgrade(op)

    # --- #657 One transaction holds one scope, and the record is partitioned -
    partition_declaration.downgrade(op)

    # --- #533 One authorized package, and the receipt that binds it --------
    release_package.downgrade(op)

    # --- #529 One immutable release candidate, from one coherent reading ---
    release_candidate.downgrade(op)

    # --- #640 The per-project external-issue profile -----------------------
    issue_profile.downgrade(op)

    # --- #531 Project authorization is a data partition --------------------
    project_partition.downgrade(op)

    # --- #633 The retained report reading is evidence, not a cache ---------
    report_reading_payload.downgrade(op)

    # --- #605 One stored extractor configuration, and segment citations ----
    extractor_configuration.downgrade(op)

    # --- #602 Report Runs bound to the accepted revision -------------------
    report_revision_binding.downgrade(op)

    # --- #610 The stored mapping-revision declaration ----------------------
    baseline_format_manifest.downgrade(op)

    # --- One delivery ledger for both transports (#599) --------------------
    unified_delivery.downgrade(op)

    # --- Permanent-state de-duplication (#457) -----------------------------
    delta_deduplication.downgrade(op)

    # --- Spine-native Recorded Verbal origin (#512) -----------------------
    recorded_verbal.downgrade(op)

    # --- Review Packet resolution (#526) ----------------------------------
    review_packets.downgrade(op)

    # --- Project-bound push intake (#511) ---------------------------------
    push_intake.downgrade(op)

    # --- Resolve Delta (#519) ---------------------------------------------
    resolve_delta.downgrade(op)

    # --- Adopt Baseline (#509) --------------------------------------------
    baseline_record.downgrade(op)

    # --- Baseline/delta operating mode (#520) -----------------------------
    operating_mode.downgrade(op)

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
