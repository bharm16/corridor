"""#610 The stored mapping-revision declaration (ADR-0076, ADR-0083).

#597 made a versioned semantic-mapping manifest the authority a template is
read through, and recorded only its identity, version and digest on the
registration.  That proves *which* revision a render was performed under and
not *what* that revision declared, so reproducing a past render depended on
whoever declared it still holding the declaration.  For a record whose claim
is auditability that is a gap, not a convenience: a digest nobody can resolve
is weaker evidence than it looks.

``project_baseline_format_manifests`` stores the declaration itself, as the
exact canonical bytes the digest is taken over — the same shape as a Source
Segment, which retains exact text beside its digest rather than the digest
alone.  A ``jsonb`` column was rejected for that reason: PostgreSQL
normalizes key order, whitespace and numbers, so the bytes it gave back would
no longer be the bytes anyone digested, and the digest could not be checked
against them at all.

Two constraints make a stored declaration that disagrees with its
registration unrepresentable rather than merely unlikely.  The check
constraint recomputes SHA-256 over the stored bytes and requires the row's
own ``content_sha256``; the composite foreign key requires that identity,
version and digest to be the registration's.  Together the stored bytes
digest to exactly what the registration records, and no command, trigger or
review has to be trusted for it.

The row is keyed by the registration's own id, so a stored declaration cannot
outlive or precede the act that registered it, and a registration with no row
here has no stored declaration — an explicit absence, never an empty
manifest.  ``attach_baseline_format_manifest`` is the one writer, owned by
the record-decision role like every other write to this family, and a replay
converges on the row already stored instead of writing a second one.
"""

from __future__ import annotations

import sqlalchemy as sa

from corridor.migrations.source_append_commands.roles import (
    RECORD_DECISION_ROLE,
    RUNTIME_LOGINS,
)


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


def upgrade(op) -> None:
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
        op.execute(f"grant select, insert on public.{table} to {RECORD_DECISION_ROLE}")
    op.execute(ATTACH_BASELINE_FORMAT_MANIFEST)
    op.execute(
        f"alter function public.attach_baseline_format_manifest"
        f"{ATTACH_BASELINE_FORMAT_MANIFEST_SIGNATURE} owner to "
        f"{RECORD_DECISION_ROLE}"
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


def downgrade(op) -> None:
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
