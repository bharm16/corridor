"""#533 One authorized package, and the receipt that binds it.

ADR-0086 makes one attributable human authorization of one prepared set the
external issue unit, and requires one package receipt that binds every
artifact and digest to the accepted revision, the predecessor or an explicit
none, the cutoff, the coverage state, the template and mapping identities,
the releaser and the release time.  #529 created ``release_packages`` empty
so a candidate could name a predecessor; this block is what may write it.

**The receipt/candidate binding is composite, not three columns that drift.**
``release_packages (candidate_id, project_id, accepted_revision_id)``
references ``release_candidates (id, project_id, accepted_revision_id)``, so
the receipt states the revision it released *explicitly* and, in the same
key, proves that it is the revision the candidate was prepared from.  A
receipt whose revision disagrees with its candidate's is not a row this
schema can hold.  #635 asked for exactly this: ``external_report_releases``
binds no accepted revision at all, so "has this revision been issued?" had no
answer, and none may be inferred from a timestamp.

**The predecessor is a chain, never a clock.**  ``previous_package_id`` is an
explicit typed reference; ``sequence_number`` is its position, carried as the
predecessor's own number plus one and bound to the predecessor row by a
composite key, exactly as #640 bound an issue-profile succession.  One root
per project, one successor per package: the first release has no predecessor
and invents none, and a later release has exactly one.  Nothing here orders
releases by ``authorized_at``, and a release authorized at an earlier
declared instant than its predecessor is still its successor -- ordering by
the recorded time would put the clock back in charge of the baseline.

**The artifact enumeration is copied by the database, not supplied.**  The
command inserts ``release_package_artifacts`` with ``insert ... select`` from
the candidate's own rows and copies the mandatory UCM from the candidate's
own columns, so the receipt's enumeration cannot disagree with the candidate
it seals.  The mandatory UCM is columns rather than a row for #529's reason:
a package with no UCM and a package with two are both unrepresentable.

**Authority is proved inside PostgreSQL.**  #531 delivered the
external-release designation and enforced it at the web route, which is an
application check: a second caller that forgets it releases anyway.
``authorize_release_package`` re-proves the active roster entry *and* its
``can_release_externally`` flag as the command's own owner, and no runtime
login holds insert on either package relation, so the command is the only
door.  A forged principal buys nothing: the roster is what is consulted, not
the string the caller passed.

**A package is customer content**, so both relations carry #531's partition
for #531's reason, and both carry #529's immutability trigger: a sealed
receipt is never edited, and a different set is a different package.
"""

from __future__ import annotations

import sqlalchemy as sa

from corridor.migrations.source_append_commands.issue_profile import (
    _CONFIGURED_ARTIFACT_TYPES_SQL,
)
from corridor.migrations.source_append_commands.operating_mode import (
    OPERATING_MODE_ROLE,
)
from corridor.migrations.source_append_commands.project_partition import (
    _UNPARTITIONED_ROLES_SQL,
)
from corridor.migrations.source_append_commands.roles import RUNTIME_LOGINS


RELEASE_PACKAGE_TABLES = ("release_packages", "release_package_artifacts")

RELEASE_AUTHORIZATION_SCHEMA = f"""
-- The half of the composite binding that lives on the candidate. Without it
-- the receipt could name a candidate and a revision that have nothing to do
-- with each other.
alter table public.release_candidates
    add constraint uq_release_candidates_revision_row
    unique (id, project_id, accepted_revision_id);

alter table public.release_packages
    -- The prepared set this receipt seals. `not null`: a package with no
    -- candidate would be a release of nothing anybody reviewed.
    add column candidate_id bigint not null,
    -- The candidate's own identity and content digest, copied so the receipt
    -- states what it sealed without a join, and constrained by the composite
    -- key below so the copy cannot drift.
    add column candidate_identity character varying(64) not null,
    add column content_sha256 character varying(64) not null,
    -- ADR-0086's explicit absence. Null is the first package's honest answer.
    add column previous_package_id bigint,
    add column previous_sequence_number integer,
    add column sequence_number integer not null,
    add column source_cutoff timestamp with time zone not null,
    add column coverage_identity character varying(160) not null,
    add column coverage_sha256 character varying(64) not null,
    add column issue_profile_id bigint not null,
    add column issue_profile_identity character varying(160) not null,
    add column issue_profile_version integer not null,
    add column issue_profile_sha256 character varying(64) not null,
    add column output_template_format_id bigint not null,
    add column output_template_kind character varying(32)
        generated always as ('output_template') stored,
    add column field_mapping_format_id bigint not null,
    add column field_mapping_kind character varying(32)
        generated always as ('field_mapping') stored,
    -- ADR-0091's one mandatory member, held as a property of the package.
    add column ucm_renderer_identity character varying(160) not null,
    add column ucm_renderer_version character varying(64) not null,
    add column ucm_content_sha256 character varying(64) not null,
    add column ucm_storage_key character varying(160) not null,
    add column ucm_byte_count bigint not null,
    -- The exact canonical bytes `package_identity` digests. `text`, never
    -- `jsonb`, for #610's reason: PostgreSQL normalizes key order, whitespace
    -- and numbers, so what it handed back would not be what anybody digested.
    add column receipt_declaration text not null,
    add column receipt_schema_version character varying(64) not null,
    -- One receipt per candidate: the candidate cannot be attached to two
    -- divergent releases, and a replay converges on the row that exists.
    add constraint uq_release_packages_candidate unique (candidate_id),
    add constraint uq_release_packages_sequence
        unique (id, project_id, sequence_number),
    -- One successor per package, so the release history is a chain and never
    -- a fork.
    add constraint uq_release_packages_successor unique (previous_package_id),
    -- The composite binding. The receipt states its revision explicitly, and
    -- this proves it is the revision the candidate was prepared from.
    add constraint fk_release_packages_candidate foreign key
        (candidate_id, project_id, accepted_revision_id)
        references public.release_candidates (id, project_id, accepted_revision_id),
    add constraint fk_release_packages_previous foreign key
        (previous_package_id, project_id, previous_sequence_number)
        references public.release_packages (id, project_id, sequence_number),
    add constraint fk_release_packages_profile foreign key
        (issue_profile_id, project_id, issue_profile_identity,
         issue_profile_version)
        references public.project_issue_profiles
        (id, project_id, profile_identity, profile_version),
    add constraint fk_release_packages_template foreign key
        (output_template_format_id, project_id, output_template_kind)
        references public.project_baseline_formats (id, project_id, format_kind),
    add constraint fk_release_packages_mapping foreign key
        (field_mapping_format_id, project_id, field_mapping_kind)
        references public.project_baseline_formats (id, project_id, format_kind),
    -- The identity is the digest of the receipt bytes, so a receipt that does
    -- not digest to what it claims cannot be stored.
    add constraint ck_release_packages_receipt_digest check (
        encode(sha256(convert_to(receipt_declaration, 'utf8')), 'hex')
            = package_identity
    ),
    add constraint ck_release_packages_receipt_schema check (
        length(btrim(receipt_schema_version)) > 0
    ),
    add constraint ck_release_packages_coverage check (
        length(btrim(coverage_identity)) > 0
    ),
    add constraint ck_release_packages_ucm check (
        length(btrim(ucm_renderer_identity)) > 0
        and length(btrim(ucm_renderer_version)) > 0
        and length(btrim(ucm_storage_key)) > 0
        and ucm_byte_count > 0
    ),
    add constraint ck_release_packages_profile_version check (
        issue_profile_version >= 1
    ),
    -- Number one opens the chain with no predecessor; every later number
    -- follows its predecessor's by exactly one. A fictitious earlier release
    -- is unrepresentable in both directions.
    add constraint ck_release_packages_succession check (
        (previous_package_id is null and sequence_number = 1
            and previous_sequence_number is null)
        or (previous_package_id is not null
            and previous_sequence_number is not null
            and sequence_number = previous_sequence_number + 1)
    );

-- One release lineage per project: a second root would be a second issue
-- series with no ordering between them.
create unique index uq_release_packages_root
    on public.release_packages (project_id)
    where previous_package_id is null;

create table public.release_package_artifacts (
    id bigserial primary key,
    package_id bigint not null,
    project_id bigint not null,
    -- 'updated_ucm' is deliberately not admitted: the mandatory member is the
    -- package's own columns, so it can be neither dropped nor doubled.
    artifact_type character varying(48) not null,
    renderer_identity character varying(160) not null,
    renderer_version character varying(64) not null,
    content_sha256 character varying(64) not null,
    storage_key character varying(160) not null,
    byte_count bigint not null,
    position integer not null,
    constraint uq_release_package_artifacts_type
        unique (package_id, artifact_type),
    constraint uq_release_package_artifacts_position
        unique (package_id, position),
    constraint fk_release_package_artifacts_package foreign key
        (package_id, project_id)
        references public.release_packages (id, project_id),
    constraint ck_release_package_artifacts_type check (
        artifact_type in ({_CONFIGURED_ARTIFACT_TYPES_SQL})
    ),
    constraint ck_release_package_artifacts_renderer check (
        length(btrim(renderer_identity)) > 0
        and length(btrim(renderer_version)) > 0
    ),
    constraint ck_release_package_artifacts_bytes check (
        length(btrim(storage_key)) > 0 and byte_count > 0
    ),
    constraint ck_release_package_artifacts_position check (position >= 1)
);

create index ix_release_package_artifacts_project_id
    on public.release_package_artifacts (project_id);
"""

# The one writer. Everything it stores about the sealed set is read from the
# candidate inside this function, so no caller can hand it an artifact list, a
# revision, or a predecessor that the prepared candidate does not carry.
AUTHORIZE_RELEASE_PACKAGE = """
create function public.authorize_release_package(
    p_project_id bigint,
    p_candidate_id bigint,
    p_principal character varying,
    p_authorized_at timestamp with time zone,
    p_receipt_declaration text
) returns jsonb
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            candidate release_candidates%ROWTYPE;
            existing release_packages%ROWTYPE;
            head release_packages%ROWTYPE;
            declared jsonb;
            declared_digest character varying(64);
            declared_schema character varying(64);
            newest_revision bigint;
            head_id bigint;
            new_id bigint;
            new_sequence integer;
            expected jsonb;
            stated jsonb;
        begin
            if p_principal is null or length(btrim(p_principal)) = 0 then
                raise exception 'a release names the person authorizing it'
                    using errcode='23514';
            end if;
            if p_authorized_at is null then
                raise exception 'a release is recorded at an explicitly supplied business instant, never a clock reading'
                    using errcode='23514';
            end if;
            -- #531's designation, re-proved here as this command's owner. The
            -- web route may check it too; this is the check that cannot be
            -- forgotten by a second caller or forged by the caller's string.
            if not exists (
                select 1
                  from public.project_roster_entries
                 where project_id = p_project_id
                   and principal_subject = p_principal
                   and active
                   and can_release_externally
            ) then
                raise exception
                    'principal % holds no external-release designation for project %',
                    p_principal, p_project_id
                    using errcode='42501';
            end if;

            select * into candidate from release_candidates
             where id = p_candidate_id and project_id = p_project_id;
            if not found then
                raise exception 'candidate % is not a prepared candidate of project %', p_candidate_id, p_project_id
                    using errcode='23514';
            end if;

            -- Idempotent replay. The unique key on `candidate_id` is what
            -- makes two divergent releases of one candidate unrepresentable;
            -- this returns the release that exists rather than colliding.
            select * into existing from release_packages
             where candidate_id = p_candidate_id;
            if found then
                return jsonb_build_object(
                    'package_id', existing.id,
                    'package_identity', existing.package_identity,
                    'sequence_number', existing.sequence_number,
                    'created', false
                );
            end if;

            if candidate.readiness = 'blocked' then
                raise exception 'this candidate is blocked and cannot be authorized; clearing what blocks it needs a newly prepared candidate'
                    using errcode='23514';
            end if;

            select max(id) into newest_revision from project_record_revisions
             where project_id = p_project_id;
            if coalesce(newest_revision, 0) is distinct from
                candidate.accepted_revision_id then
                raise exception 'the accepted record moved to revision % after this candidate was prepared from revision %; prepare a fresh candidate', newest_revision, candidate.accepted_revision_id
                    using errcode='23514';
            end if;

            -- The chain head, found by the absence of a successor and never by
            -- the newest `authorized_at`. It must still be the predecessor the
            -- candidate was prepared against.
            select p.* into head from release_packages p
             where p.project_id = p_project_id
               and not exists (
                   select 1 from release_packages s
                    where s.previous_package_id = p.id
               );
            head_id := case when found then head.id else null end;
            if head_id is distinct from candidate.previous_package_id then
                raise exception 'a package was authorized after this candidate was prepared, so its comparison baseline is no longer the current one; prepare a fresh candidate'
                    using errcode='23514';
            end if;

            declared_digest := encode(
                sha256(convert_to(p_receipt_declaration, 'utf8')), 'hex'
            );
            declared := p_receipt_declaration::jsonb;
            declared_schema := declared->>'schema_version';
            if declared_schema is null
               or length(btrim(declared_schema)) = 0 then
                raise exception 'a release receipt names the declaration schema it was written under'
                    using errcode='23514';
            end if;
            -- The receipt bytes are the caller's, so what they say about the
            -- sealed set is checked against what the database read, not taken
            -- on trust. Everything else the receipt records is a column below,
            -- constrained by a key or a foreign key of its own.
            if declared->>'candidate_identity'
                is distinct from candidate.candidate_identity
               or declared->>'content_sha256'
                is distinct from candidate.content_sha256
               or (declared->>'candidate_id')::bigint
                is distinct from candidate.id
               or (declared->>'accepted_revision_id')::bigint
                is distinct from candidate.accepted_revision_id
               or declared->>'authorized_by_principal' is distinct from p_principal
            then
                raise exception 'the release receipt does not describe the candidate it seals'
                    using errcode='23514';
            end if;
            select jsonb_agg(entry order by entry->>'position')
              into expected
              from (
                select jsonb_build_object(
                           'position', '1',
                           'artifact_type', 'updated_ucm',
                           'content_sha256', candidate.ucm_content_sha256
                       ) as entry
                union all
                select jsonb_build_object(
                           'position', a.position::text,
                           'artifact_type', a.artifact_type,
                           'content_sha256', a.content_sha256
                       )
                  from release_candidate_artifacts a
                 where a.candidate_id = candidate.id
              ) rows;
            select jsonb_agg(entry order by entry->>'position')
              into stated
              from (
                select jsonb_build_object(
                           'position', value->>'position',
                           'artifact_type', value->>'artifact_type',
                           'content_sha256', value->>'content_sha256'
                       ) as entry
                  from jsonb_array_elements(
                      coalesce(declared->'artifacts', '[]'::jsonb)
                  )
              ) rows;
            if expected is distinct from stated then
                raise exception 'the release receipt does not enumerate the artifacts this candidate holds'
                    using errcode='23514';
            end if;

            new_sequence := case when head_id is null
                                 then 1 else head.sequence_number + 1 end;
            new_id := nextval('release_packages_id_seq');
            insert into release_packages (
                id, project_id, package_identity, accepted_revision_id,
                authorized_by_principal, authorized_at,
                candidate_id, candidate_identity, content_sha256,
                previous_package_id, previous_sequence_number, sequence_number,
                source_cutoff, coverage_identity, coverage_sha256,
                issue_profile_id, issue_profile_identity, issue_profile_version,
                issue_profile_sha256, output_template_format_id,
                field_mapping_format_id, ucm_renderer_identity,
                ucm_renderer_version, ucm_content_sha256, ucm_storage_key,
                ucm_byte_count, receipt_declaration, receipt_schema_version
            ) values (
                new_id, p_project_id, declared_digest,
                candidate.accepted_revision_id, p_principal, p_authorized_at,
                candidate.id, candidate.candidate_identity,
                candidate.content_sha256,
                head_id,
                case when head_id is null then null else head.sequence_number end,
                new_sequence,
                candidate.source_cutoff, candidate.coverage_identity,
                candidate.coverage_sha256, candidate.issue_profile_id,
                candidate.issue_profile_identity,
                candidate.issue_profile_version, candidate.issue_profile_sha256,
                candidate.output_template_format_id,
                candidate.field_mapping_format_id,
                candidate.ucm_renderer_identity, candidate.ucm_renderer_version,
                candidate.ucm_content_sha256, candidate.ucm_storage_key,
                candidate.ucm_byte_count, p_receipt_declaration, declared_schema
            );
            -- Copied by the database from the candidate's own rows. Nothing
            -- the caller supplies decides what the receipt enumerates.
            insert into release_package_artifacts (
                package_id, project_id, artifact_type, renderer_identity,
                renderer_version, content_sha256, storage_key, byte_count,
                position
            )
            select new_id, p_project_id, a.artifact_type, a.renderer_identity,
                   a.renderer_version, a.content_sha256, a.storage_key,
                   a.byte_count, a.position
              from release_candidate_artifacts a
             where a.candidate_id = candidate.id;
            return jsonb_build_object(
                'package_id', new_id,
                'package_identity', declared_digest,
                'sequence_number', new_sequence,
                'created', true
            );
        end; $$;
"""

AUTHORIZE_RELEASE_PACKAGE_SIGNATURE = (
    "(bigint, bigint, character varying, timestamp with time zone, text)"
)

RELEASE_PACKAGE_TRIGGERS = """
create trigger trg_release_package_artifacts_immutable
    before update or delete
    on public.release_package_artifacts
    for each row
    execute function public.enforce_release_record_write();
create trigger trg_release_package_artifacts_truncate
    before truncate on public.release_package_artifacts
    for each statement
    execute function public.enforce_release_record_write();
"""

# A sealed package is one customer's issue, so its enumeration carries #531's
# partition exactly as the candidate's does.
RELEASE_PACKAGE_PARTITION_POLICIES = f"""
do $$
declare
    v_roles text;
    v_table text;
begin
    select string_agg(quote_ident(rolname), ', ' order by rolname)
      into v_roles
      from pg_roles
     where rolname in ({_UNPARTITIONED_ROLES_SQL});
    foreach v_table in array array['release_package_artifacts'] loop
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

RELEASE_AUTHORIZATION_SCHEMA_DOWN = """
drop table if exists public.release_package_artifacts cascade;
drop index if exists uq_release_packages_root;
alter table public.release_packages
    -- The foreign keys go first: each depends on a unique index below it, and
    -- PostgreSQL runs the subcommands of one `alter table` in the order given.
    drop constraint if exists fk_release_packages_candidate,
    drop constraint if exists fk_release_packages_previous,
    drop constraint if exists fk_release_packages_profile,
    drop constraint if exists fk_release_packages_template,
    drop constraint if exists fk_release_packages_mapping,
    drop constraint if exists uq_release_packages_candidate,
    drop constraint if exists uq_release_packages_sequence,
    drop constraint if exists uq_release_packages_successor,
    drop constraint if exists ck_release_packages_receipt_digest,
    drop constraint if exists ck_release_packages_receipt_schema,
    drop constraint if exists ck_release_packages_coverage,
    drop constraint if exists ck_release_packages_ucm,
    drop constraint if exists ck_release_packages_profile_version,
    drop constraint if exists ck_release_packages_succession,
    drop column if exists candidate_id,
    drop column if exists candidate_identity,
    drop column if exists content_sha256,
    drop column if exists previous_package_id,
    drop column if exists previous_sequence_number,
    drop column if exists sequence_number,
    drop column if exists source_cutoff,
    drop column if exists coverage_identity,
    drop column if exists coverage_sha256,
    drop column if exists issue_profile_id,
    drop column if exists issue_profile_identity,
    drop column if exists issue_profile_version,
    drop column if exists issue_profile_sha256,
    drop column if exists output_template_format_id,
    drop column if exists output_template_kind,
    drop column if exists field_mapping_format_id,
    drop column if exists field_mapping_kind,
    drop column if exists ucm_renderer_identity,
    drop column if exists ucm_renderer_version,
    drop column if exists ucm_content_sha256,
    drop column if exists ucm_storage_key,
    drop column if exists ucm_byte_count,
    drop column if exists receipt_declaration,
    drop column if exists receipt_schema_version;
alter table public.release_candidates
    drop constraint if exists uq_release_candidates_revision_row;
"""


def _refuse_unrepresentable_release_package_downgrade(bind) -> None:
    """Refuse rather than drop the receipts that say what a customer received.

    The schema this downgrade restores keeps ``release_packages`` but strips
    every column that makes a receipt a receipt -- the candidate it sealed, the
    artifact digests, the predecessor, the cutoff, the coverage state. An
    authorized package would survive as a name with nothing behind it, which is
    the shape #635 objected to in ``external_report_releases``. The same
    discipline as #529's downgrade: state what cannot be carried back, and stop.
    """

    stored = bind.execute(
        sa.text(
            "select count(*) from information_schema.columns "
            " where table_schema = 'public' "
            "   and table_name = 'release_packages' "
            "   and column_name = 'candidate_id'"
        )
    ).scalar_one()
    if not stored:
        return
    blocked = bind.execute(
        sa.text("select count(*) from release_packages")
    ).scalar_one()
    if blocked:
        raise RuntimeError(
            f"#533 downgrade refuses: {blocked} authorized package receipt(s) "
            "record what one customer was issued and against which accepted "
            "revision, and the schema without those columns cannot represent "
            "it. Nothing is dropped here."
        )


def upgrade(op) -> None:
    # Last, because the receipt binds the candidate the block above creates,
    # through a composite key that needs that table's new unique constraint,
    # and because its command proves #531's designation against the roster.
    op.execute(RELEASE_AUTHORIZATION_SCHEMA)
    op.execute(RELEASE_PACKAGE_TRIGGERS)
    op.execute(AUTHORIZE_RELEASE_PACKAGE)
    # The schema owner's default privileges hand every new table to the runtime
    # logins including UPDATE and DELETE. #531, #640 and #529 each had to take
    # that back explicitly and so does this: a sealed receipt is immutable, and
    # the trigger above is the second line rather than the only one.
    op.execute(
        f"revoke all on public.release_package_artifacts from {RUNTIME_LOGINS}"
    )
    op.execute(
        f"grant select on public.release_package_artifacts to {RUNTIME_LOGINS}"
    )
    # `release_packages` stays read-only to every runtime login, exactly as
    # #529 left it. Authorizing an issue is an attributable human act, so the
    # only writer is the `SECURITY DEFINER` command, and the runtime cannot
    # reach either package relation with an INSERT of its own.
    op.execute(
        "revoke insert, update, delete, truncate on public.release_packages "
        f"from {RUNTIME_LOGINS}"
    )
    op.execute(
        "revoke insert, update, delete, truncate on "
        f"public.release_package_artifacts from {RUNTIME_LOGINS}"
    )
    for table in RELEASE_PACKAGE_TABLES:
        op.execute(
            f"grant select, insert on public.{table} to {OPERATING_MODE_ROLE}"
        )
        op.execute(
            f"grant usage, select on sequence public.{table}_id_seq "
            f"to {OPERATING_MODE_ROLE}"
        )
    # Everything the command reads to prove what it is about to seal: the
    # roster entry carrying #531's designation, the candidate and its artifact
    # rows, and the revision history the staleness check compares against.
    for readable in (
        "project_roster_entries",
        "release_candidates",
        "release_candidate_artifacts",
        "project_record_revisions",
    ):
        op.execute(
            f"grant select on public.{readable} to {OPERATING_MODE_ROLE}"
        )
    op.execute(
        f"alter function public.authorize_release_package"
        f"{AUTHORIZE_RELEASE_PACKAGE_SIGNATURE} owner to {OPERATING_MODE_ROLE}"
    )
    op.execute(
        f"revoke all on function public.authorize_release_package"
        f"{AUTHORIZE_RELEASE_PACKAGE_SIGNATURE} from public"
    )
    # Only the web capability. A background run carries no person's release
    # authority, so the worker is deliberately not granted this one.
    op.execute(
        f"grant execute on function public.authorize_release_package"
        f"{AUTHORIZE_RELEASE_PACKAGE_SIGNATURE} to corridor_web"
    )
    op.execute(RELEASE_PACKAGE_PARTITION_POLICIES)


def downgrade(op) -> None:
    # First, because the upgrade added it last. Every receipt would lose the
    # candidate, artifact digests, predecessor and cutoff that make it one, so
    # this refuses instead of stripping them.
    _refuse_unrepresentable_release_package_downgrade(op.get_bind())
    op.execute(
        f"drop function if exists public.authorize_release_package"
        f"{AUTHORIZE_RELEASE_PACKAGE_SIGNATURE}"
    )
    op.execute(RELEASE_AUTHORIZATION_SCHEMA_DOWN)
