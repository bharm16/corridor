"""#605 One stored extractor configuration, and cited segments.

Two copies removed, both by the same move: store the value once and keep a
reference to it.

**The extractor configuration.**  Every sealed ``extraction_runs`` row
carries its whole ``extractor_config_json`` receipt — prompt version, model,
schema, three source digests, request controls and the runtime's Python and
dependency-lock identity.  Every run of one deployed extractor seals a
byte-identical object, which is why the digest column beside it exists at
all.  So the receipt is written once per run and is the same object each
time: a value masquerading as a possession.  ``extractor_configurations``
stores it once, keyed by that digest, and the run keeps only the reference
it already had.

The registry is immutable.  A run that names a configuration is asserting
what it actually ran; a configuration that could be edited afterwards would
let that assertion quietly become false, so a trigger refuses every update
and delete rather than trusting a convention.

The backfill preserves exact legacy configuration and invents none.  Every
distinct digest already stored is inserted with the exact receipt that was
stored under it, and the foreign key is added afterwards, so a legacy run
keeps referencing precisely what it ran.  A run written before the seal
existed has ``extractor_config_sha256 is null`` and stays that way: null is
a checked, explicit "not known", and giving it today's deployed
configuration would be a fabrication that reads exactly like a measurement.
The inline ``extractor_config_json`` copy is not dropped either — the
constraint below simply stops requiring it, and requires instead that a copy
still present is *identical* to the registry row, so no reader loses a
receipt and no copy can drift from the row it duplicates.

The receipt's shape rule moves into ``extractor_configuration_receipt_is_valid``
so the registry and the run constraint share one definition of it, rather
than the second restating the first — the same defect, one level up.

**Evidence citations.**  ADR-0068 decided that a Source Segment owns its
exact text once and that consumers reference it.  ``evidence_links.quote``
is the copy that decision supersedes.  ``evidence_link_sources`` is the
relationship that replaces it: link, segment, ordinal, and nothing else —
no text column, so a citation written through it cannot carry a second copy
of the words.  It mirrors ``fact_sources`` and ``support_assessment_sources``
rather than inventing a shape, and its composite keys make a citation of
another document's or another project's segment unrepresentable.

``evidence_links.quote`` stays, and stays readable.  Proving that a cited
segment's text and a legacy quote say the same thing is a separate piece of
work with its own corpus; until that proof exists, rewriting the column in
bulk would be replacing evidence with something merely believed equivalent.

The table takes ordinary grants rather than joining the source-append
command family.  Those commands exist to enforce what constraints cannot —
the digest of stored text, locator identity, project scope of an untyped
reference.  This row stores no text, holds no locator, and its scope is a
composite foreign key, so a ``SECURITY DEFINER`` wrapper would add ceremony
and no guarantee.
"""

from __future__ import annotations

import sqlalchemy as sa

from corridor.migrations.source_append_commands.roles import RUNTIME_LOGINS


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


def upgrade(op) -> None:
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


def downgrade(op) -> None:
    # First among what remains, because the upgrade added it last but one. A
    # run that stores no copy of
    # its configuration, and a citation that names a segment instead of
    # copying its words, are both unrepresentable in the shape this restores,
    # so each refuses rather than losing what it cannot carry back.
    _refuse_unreferenced_evidence_citation_downgrade(op.get_bind())
    op.execute(EVIDENCE_SEGMENT_CITATION_SCHEMA_DOWN)
    _refuse_unreferenced_configuration_downgrade(op.get_bind())
    op.execute(EXTRACTOR_CONFIGURATION_REGISTRY_SCHEMA_DOWN)
