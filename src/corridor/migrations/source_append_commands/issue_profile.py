"""#640 The per-project external-issue profile (ADR-0091, ADR-0086).

ADR-0091 made the externally issued set per-project configuration with only
the updated UCM mandatory, and recorded in the same breath that the
configuration "is not modelled".  Nothing implemented it, so #536 shipped
ADR-0085's three consequence levels deliberately underived — a level is a
projection onto the next issue's declared content, and there was no declared
content to project onto — and #529 carried a fixed four-artifact list that
ADR-0091 had already retired.  These two tables are that configuration.

**The mandatory member is a column, not a row.**  ``updated_ucm`` is the one
artifact with no configuration switch, so it is a property of the profile and
not an entry in the configured set: a profile with no UCM is unrepresentable
because ``ucm_renderer_identity`` is ``not null``, and a second UCM entry is
unrepresentable because ``updated_ucm`` is not a value the artifact table's
type check admits.  A nullable row in a set-membership table would have made
both states insertable and left "always" to a Python branch.  The artifact
table therefore holds exactly what ADR-0091 made configurable.

**Two layers, no third.**  A profile names artifact types and the renderer
revision each is produced by; what a given renderer revision contains — which
accepted fields, alerts, follow-up content and disclosures — is that
renderer's own declaration.  There is no per-field column here and no place to
add one, which is the point: an inventory assembled field by field per
customer is a report builder, not a configured issue set.

**History is a chain, and the chain is the timing rule.**  Each version names
its predecessor through a composite foreign key that carries the predecessor's
project, identity, version and effective instant, and
``ck_project_issue_profiles_succession`` requires the version to be exactly
one higher and the effective instant to be strictly later.  So the monotonic,
non-backdatable, single-lineage, append-only history is four constraints
rather than a Python comparison anybody can forget: a caller cannot register a
version effective at or before the currently effective one, and therefore
cannot retroactively change what an earlier reporting cutoff was configured to
issue.  Strictly later, not at-or-later, is deliberate — equality would leave
one instant whose configured answer could still be rewritten.
``uq_project_issue_profiles_successor`` refuses a fork, and the partial unique
index refuses a second lineage in one project.

**The digest covers the whole configuration.**  ``declaration`` holds the
exact canonical bytes the digest is taken over, as ``text`` and never
``jsonb``, for #610's reason: PostgreSQL normalizes key order, whitespace and
numbers, so the bytes it gave back would no longer be the bytes anyone
digested.  ``ck_project_issue_profiles_digest`` recomputes SHA-256 over the
stored bytes, so a declaration that does not digest to what the row claims
cannot be stored at all.  Every other column is *derived from* those bytes by
the one command, so no column can disagree with the digest.

**The template and mapping are references, not copies.**  A profile names the
registered output template and field mapping (#509, #597) its artifacts render
through, and the composite foreign keys carry ``project_id`` and a generated
constant kind — so a profile naming another project's registration, or naming
a field mapping where a template belongs, is unrepresentable rather than
refused by a lookup somebody could skip.

**Who configures.**  The command is owned by the record-decision role and
granted to the web capability alone; ``issue_profile.register_issue_profile``
proves the Project Coordination designation before calling it.  The external
releaser deliberately holds no authority here: that person authorizes a
prepared package (#533), and release approval must not become package design.
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


def upgrade(op) -> None:
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
            f"grant select, insert on public.{table} to {RECORD_DECISION_ROLE}"
        )
        op.execute(
            f"grant usage, select on sequence public.{table}_id_seq "
            f"to {RECORD_DECISION_ROLE}"
        )
    # The command resolves the declared template and mapping against
    # `project_baseline_formats`, which the Adopt Baseline block above already
    # grants this role.
    op.execute(
        f"alter function public.register_project_issue_profile"
        f"{REGISTER_PROJECT_ISSUE_PROFILE_SIGNATURE} owner to "
        f"{RECORD_DECISION_ROLE}"
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


def downgrade(op) -> None:
    # First, because the upgrade added it last. Every registered profile
    # version would go silently, so this refuses instead of dropping one.
    _refuse_unrepresentable_issue_profile_downgrade(op.get_bind())
    op.execute(
        f"drop function if exists public.register_project_issue_profile"
        f"{REGISTER_PROJECT_ISSUE_PROFILE_SIGNATURE}"
    )
    op.execute(ISSUE_PROFILE_SCHEMA_DOWN)
