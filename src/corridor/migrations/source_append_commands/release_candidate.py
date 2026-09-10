"""#529 One immutable release candidate, from one coherent reading.

ADR-0086 makes one authorized set of customer artifacts the external issue
unit, and ADR-0091 makes the membership of that set per-project configuration
with only the updated UCM mandatory.  #640 modelled the configuration and #641
resolved it into executable content; these four relations are what one
preparation of it leaves behind.

**The mandatory UCM is a column, not a row**, for exactly the reason #640 made
it one on the profile.  ``release_candidates`` carries the UCM's renderer,
digest, storage key and size directly, so a candidate with no UCM is
unrepresentable (the columns are ``not null``) and a candidate with two is
unrepresentable (there is one set of columns).
``release_candidate_artifacts`` admits only what ADR-0091 made a choice, so
``updated_ucm`` cannot appear there at all.

**Identity is the digested declaration, not a column somebody remembered to
fill in.**  ``input_declaration`` holds the exact canonical bytes that bind
the project, the accepted revision, the previous authorized package or an
explicit none, the source cutoff, the coverage identity and digest, the issue
profile row, identity, version and digest, the template and mapping
registrations and digests, the configured artifact types, every renderer
identity and version, the product and code revision, and the enabled feature
flags.  ``content_declaration`` additionally binds the ordered artifact
identities and their SHA-256 digests.  Both are ``text`` and never ``jsonb``
for #610's reason — PostgreSQL normalizes key order, whitespace and numbers,
so the bytes it handed back would no longer be the bytes anybody digested —
and both carry a check that recomputes SHA-256 over the stored bytes.  A row
whose declaration does not digest to what it claims cannot be stored.

**The predecessor is a typed reference and nothing else.**
``previous_package_id`` points at ``release_packages``, the authorization
relation #533 populates, through a composite key carrying ``project_id``.  It
is nullable because "no package has ever been authorized for this project" is
a real and common state that ADR-0086 requires to be explicit.  There is
deliberately no column, index or view here that would let a predecessor be
derived from the newest report by timestamp, from ``external_report_releases``
(which binds no accepted revision — #635), from the newest render, from the
newest candidate, or from the adopted baseline.

**A sealed candidate is immutable.** ``enforce_release_record_write`` refuses
every UPDATE, DELETE and TRUNCATE on all four relations regardless of the
role attempting it, so ADR-0086's "Corridor never regenerates a sealed
package in place" is a database invariant and not a convention.  A changed
artifact, revision or cutoff is a different ``candidate_identity`` and
therefore a different row.

**A candidate is customer content**, so it carries #531's partition for
#531's reason: a reader that forgets its ``where`` clause must see nothing
rather than another customer's issue.
"""

from __future__ import annotations

import sqlalchemy as sa

from corridor.migrations.source_append_commands.issue_profile import (
    _CONFIGURED_ARTIFACT_TYPES_SQL,
)
from corridor.migrations.source_append_commands.project_partition import (
    _UNPARTITIONED_ROLES_SQL,
)
from corridor.migrations.source_append_commands.roles import RUNTIME_LOGINS


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


def upgrade(op) -> None:
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


def downgrade(op) -> None:
    # First, because the upgrade added it last. Every prepared candidate and
    # every refusal receipt would go silently, so this refuses instead.
    _refuse_unrepresentable_release_candidate_downgrade(op.get_bind())
    op.execute(RELEASE_CANDIDATE_SCHEMA_DOWN)
