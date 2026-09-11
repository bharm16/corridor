"""#675 The confirmed coverage declaration, and the preparation it asks for.

#536 promised Review -> Follow-up -> Issue as one executable path and shipped
with no way to make a candidate at all, so a project whose candidate was
stale had a section that told the coordinator what was needed and offered
nothing.  ADR-0086 makes "one declared coverage state" a shared input of
every artifact in an issue, and #529 took it as an in-memory value its caller
supplied: any caller could therefore prepare an issue under a coverage nobody
had confirmed.  These three relations close both gaps at once.

**The machine owns the facts and the person owns the declaration.**
``derived_reading`` holds the exact canonical bytes Corridor derived from the
effective issue profile, the persisted Source Delivery ledger, the processing
receipts and the declared cutoff, digested by
``derived_reading_digest``; ``declaration`` repeats that digest and adds only
what a person may add, digested by ``declaration_digest``.  Both digests are
recomputed by a check constraint over the stored bytes, so a declaration that
claims a reading nobody derived cannot be stored, and the row proves which
reading was in front of the person who confirmed it.  Both are ``text`` and
never ``jsonb`` for #610's reason: PostgreSQL normalises key order and
whitespace, so the bytes it handed back would no longer be the bytes anybody
digested.

**The boundary is an append-only watermark, never a clock comparison.**
#641 closed without ADR-0085's "source outside the current issue cutoff" limb
and said exactly why: a Proposed Delta has no trustworthy source-arrival
instant and ``created_at`` is server-assigned.  A delivery has one, and it is
a row in an append-only ledger, so this block gives ``documents`` the
``source_delivery_id`` that lets a Proposed Delta dereference the delivery
that produced its Source Fact -- through its own delta group's document --
and gives the declaration a ``through_source_delivery_id`` watermark.
Membership in an issue is then an identity comparison against that watermark
and never a timestamp comparison against ``created_at``.  The link is
nullable in both directions on purpose: the corpus path registers documents
that arrived through no transport, and a reading that cannot dereference a
delivery says so rather than guessing one.

**Status is derived, and there is no mutable close object.**
``release_preparation_requests`` records what the coordinator was looking at
when they asked, and ``release_preparation_attempts`` records what each
finished attempt produced.  Neither carries a status column.  An attempt row
is appended when the attempt *finishes*, carrying both its declared instants,
so the relations stay append-only and #529's immutability trigger covers them
whole; "preparing" is the derived answer for a request no attempt has
finished yet.  ADR-0085 refused a stored packet lifecycle and #537 refused a
stored cross-project queue for the same reason, and a weekly-close row would
be the third attempt at it.

**All three are customer content**, so all three carry #531's partition for
#531's reason, and all three carry #529's immutability trigger: a confirmed
declaration, a submitted request and a finished attempt are all records of
something that happened, and none of them is edited afterwards.

**Who may append is proved here too, and not only in the Python that calls
in** (#839).  The maintainer settled the division on 2026-09-10: *Project
Coordination may confirm coverage and request preparation.  External Release
may authorize.  Read-only membership confers neither.*  #533 already proved its
half inside PostgreSQL, because an application check is one a second caller can
forget, and these two relations had no such proof at all -- any project member
could confirm the coverage an issue was prepared under and ask for the
preparation.  ``enforce_coordination_designation`` is that proof:
``SECURITY DEFINER`` with a fixed ``search_path``, it re-reads the active
roster entry and its ``can_coordinate`` flag as the schema's own owner and
raises ``42501``, so the roster is what is consulted and never the principal
string the caller passed.

It is a trigger and not a fifth ``SECURITY DEFINER`` command, deliberately.
These appends stay the runtime capability's own for the reason recorded below
-- they write no accepted authority and make nothing effective -- and what
#839 adds is a rule over them, not a new writer.  A trigger also holds for
*every* inserter, including the schema owner a migration or a fixture connects
as, which a command only reachable by grant would not; the same choice #520
made when it put the adopted-project refusal on ``dependencies`` rather than in
the Python that happens to call it.  ``release_preparation_attempts`` carries
no such trigger and must not: an attempt is what a background worker records
about its own run, and no person confirms it.
"""

from __future__ import annotations

import sqlalchemy as sa

from corridor.migrations.source_append_commands.project_partition import (
    _UNPARTITIONED_ROLES_SQL,
)
from corridor.migrations.source_append_commands.release_candidate import (
    _PREPARATION_REFUSAL_REASONS_SQL,
)
from corridor.migrations.source_append_commands.roles import RUNTIME_LOGINS


COVERAGE_PREPARATION_TABLES = (
    "issue_coverage_declarations",
    "release_preparation_requests",
    "release_preparation_attempts",
)

PREPARATION_ATTEMPT_OUTCOMES = ("prepared", "refused", "failed")

_PREPARATION_ATTEMPT_OUTCOMES_SQL = ", ".join(
    f"'{outcome}'" for outcome in PREPARATION_ATTEMPT_OUTCOMES
)

COVERAGE_PREPARATION_SCHEMA = f"""
-- A delivery is project-scoped by identity, so a document of one customer can
-- never name a delivery of another.
alter table public.source_deliveries
    add constraint uq_source_deliveries_row unique (id, project_id);

-- The one provenance link #641 lacked. Nullable: a document registered through
-- the corpus path arrived through no transport at all, and a coverage reading
-- that cannot dereference a delivery must be able to say so.
alter table public.documents
    add column source_delivery_id bigint;

alter table public.documents
    add constraint fk_documents_source_delivery foreign key
        (source_delivery_id, project_id)
        references public.source_deliveries (id, project_id);

create index ix_documents_source_delivery_id
    on public.documents (source_delivery_id);

create table public.issue_coverage_declarations (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    issue_profile_id bigint not null,
    issue_profile_identity character varying(160) not null,
    issue_profile_version integer not null,
    -- The human-readable cutoff the reading was taken at, frozen beside the
    -- watermark. It is what a coordinator reads; it is not what decides
    -- membership.
    cutoff_at timestamp with time zone not null,
    -- The exact append-only ledger boundary this issue includes. Null is the
    -- honest watermark of a project that has taken no delivery at all, and is
    -- never read as "everything".
    through_source_delivery_id bigint,
    derived_reading text not null,
    derived_reading_digest character varying(64) not null,
    declaration text not null,
    declaration_digest character varying(64) not null,
    coverage_identity character varying(160) not null,
    confirmed_by_principal character varying(128) not null,
    confirmed_at timestamp with time zone not null,
    idempotency_key character varying(160) not null,
    recorded_at timestamp with time zone not null default now(),
    constraint uq_issue_coverage_declarations_row unique (id, project_id),
    constraint uq_issue_coverage_declarations_key
        unique (project_id, idempotency_key),
    constraint uq_issue_coverage_declarations_identity
        unique (project_id, declaration_digest),
    constraint fk_issue_coverage_declarations_profile foreign key
        (issue_profile_id, project_id, issue_profile_identity,
         issue_profile_version)
        references public.project_issue_profiles
        (id, project_id, profile_identity, profile_version),
    constraint fk_issue_coverage_declarations_delivery foreign key
        (through_source_delivery_id, project_id)
        references public.source_deliveries (id, project_id),
    constraint ck_issue_coverage_declarations_reading_digest check (
        encode(sha256(convert_to(derived_reading, 'utf8')), 'hex')
            = derived_reading_digest
    ),
    constraint ck_issue_coverage_declarations_declaration_digest check (
        encode(sha256(convert_to(declaration, 'utf8')), 'hex')
            = declaration_digest
    ),
    constraint ck_issue_coverage_declarations_identity_text check (
        length(btrim(coverage_identity)) > 0
    ),
    constraint ck_issue_coverage_declarations_principal check (
        length(btrim(confirmed_by_principal)) > 0
    ),
    constraint ck_issue_coverage_declarations_version check (
        issue_profile_version >= 1
    )
);

create index ix_issue_coverage_declarations_project
    on public.issue_coverage_declarations (project_id, cutoff_at);

-- The candidate names the declaration it was prepared under, by identity. The
-- coverage identity and digest beside it say what the coverage *was*; this
-- says whose confirmation of which derived reading authorised preparing under
-- it, which is a different question and the one #675 exists to answer.
alter table public.release_candidates
    add column coverage_declaration_id bigint not null;

alter table public.release_candidates
    add constraint fk_release_candidates_coverage foreign key
        (coverage_declaration_id, project_id)
        references public.issue_coverage_declarations (id, project_id);

create table public.release_preparation_requests (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    accepted_revision_id bigint not null,
    issue_profile_id bigint not null,
    issue_profile_identity character varying(160) not null,
    issue_profile_version integer not null,
    coverage_declaration_id bigint not null,
    source_cutoff timestamp with time zone not null,
    requested_by_principal character varying(128) not null,
    requested_at timestamp with time zone not null,
    idempotency_key character varying(160) not null,
    recorded_at timestamp with time zone not null default now(),
    constraint uq_release_preparation_requests_row unique (id, project_id),
    constraint uq_release_preparation_requests_key
        unique (project_id, idempotency_key),
    constraint fk_release_preparation_requests_revision foreign key
        (accepted_revision_id, project_id)
        references public.project_record_revisions (id, project_id),
    constraint fk_release_preparation_requests_profile foreign key
        (issue_profile_id, project_id, issue_profile_identity,
         issue_profile_version)
        references public.project_issue_profiles
        (id, project_id, profile_identity, profile_version),
    constraint fk_release_preparation_requests_coverage foreign key
        (coverage_declaration_id, project_id)
        references public.issue_coverage_declarations (id, project_id),
    constraint ck_release_preparation_requests_principal check (
        length(btrim(requested_by_principal)) > 0
    ),
    constraint ck_release_preparation_requests_key check (
        length(btrim(idempotency_key)) > 0
    ),
    constraint ck_release_preparation_requests_version check (
        issue_profile_version >= 1
    )
);

create index ix_release_preparation_requests_project
    on public.release_preparation_requests (project_id, id);

create table public.release_preparation_attempts (
    id bigserial primary key,
    request_id bigint not null,
    project_id bigint not null,
    outcome character varying(32) not null,
    candidate_id bigint,
    refusal_code character varying(48),
    reason text,
    started_at timestamp with time zone not null,
    finished_at timestamp with time zone not null,
    recorded_at timestamp with time zone not null default now(),
    constraint uq_release_preparation_attempts_row unique (id, project_id),
    constraint fk_release_preparation_attempts_request foreign key
        (request_id, project_id)
        references public.release_preparation_requests (id, project_id),
    constraint fk_release_preparation_attempts_candidate foreign key
        (candidate_id, project_id)
        references public.release_candidates (id, project_id),
    constraint ck_release_preparation_attempts_outcome check (
        outcome in ({_PREPARATION_ATTEMPT_OUTCOMES_SQL})
    ),
    -- #529's three outcomes, made unrepresentable in each other's shape: a
    -- prepared attempt names its candidate and no reason, and a refused or
    -- failed one names a bounded reason and no candidate. A row describing a
    -- half-prepared candidate is not a row this schema can hold.
    constraint ck_release_preparation_attempts_result check (
        (outcome = 'prepared' and candidate_id is not null
             and refusal_code is null and reason is null)
        or (outcome = 'refused' and candidate_id is null
             and refusal_code is not null and reason is not null)
        or (outcome = 'failed' and candidate_id is null
             and refusal_code is null and reason is not null)
    ),
    constraint ck_release_preparation_attempts_code check (
        refusal_code is null
        or refusal_code in ({_PREPARATION_REFUSAL_REASONS_SQL})
    ),
    constraint ck_release_preparation_attempts_reason check (
        reason is null
        or (length(btrim(reason)) > 0 and length(reason) <= 2000)
    ),
    constraint ck_release_preparation_attempts_span check (
        finished_at >= started_at
    )
);

create index ix_release_preparation_attempts_request
    on public.release_preparation_attempts (request_id, id);

create index ix_release_preparation_attempts_project
    on public.release_preparation_attempts (project_id, id);
"""

COVERAGE_PREPARATION_TRIGGERS = "".join(
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
    for table in COVERAGE_PREPARATION_TABLES
)

# The designation proof (#839). It reads the column naming the person out of
# the row it is about, so one function serves both relations and neither can
# name a different column than the one its own trigger declares.
#
# `security definer` for the reason `authorize_release_package` is: the answer
# must not depend on what the inserting login happens to be granted on the
# roster, or on whether a project partition was declared first. `search_path`
# is fixed so the relation it reads cannot be shadowed. PostgreSQL checks
# EXECUTE on a trigger function when the trigger is created and not when it
# fires, so taking PUBLIC's default execute back costs the triggers nothing and
# leaves nobody able to call the proof directly (#492, #545).
COORDINATION_DESIGNATION_GUARD = """
create function public.enforce_coordination_designation()
    returns trigger
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            v_principal text;
        begin
            v_principal := to_jsonb(NEW) ->> TG_ARGV[0];
            if v_principal is null or length(btrim(v_principal)) = 0 then
                raise exception 'this record names the person performing it'
                    using errcode='23514';
            end if;
            if not exists (
                select 1
                  from public.project_roster_entries
                 where project_id = NEW.project_id
                   and principal_subject = v_principal
                   and active
                   and can_coordinate
            ) then
                raise exception
                    'principal % holds no project-coordination designation for project %',
                    v_principal, NEW.project_id
                    using errcode='42501';
            end if;
            return NEW;
        end; $$;
"""

# The two relations a person appends to. `release_preparation_attempts` is
# deliberately absent: a worker records what its own run produced, and nobody
# confirms an attempt.
COORDINATION_DESIGNATION_TRIGGERS = """
create trigger trg_issue_coverage_declarations_designated
    before insert on public.issue_coverage_declarations
    for each row
    execute function public.enforce_coordination_designation(
        'confirmed_by_principal'
    );
create trigger trg_release_preparation_requests_designated
    before insert on public.release_preparation_requests
    for each row
    execute function public.enforce_coordination_designation(
        'requested_by_principal'
    );
"""

COVERAGE_PREPARATION_SCHEMA_DOWN = """
drop function if exists public.enforce_coordination_designation() cascade;
drop table if exists public.release_preparation_attempts cascade;
drop table if exists public.release_preparation_requests cascade;
alter table public.release_candidates
    drop constraint if exists fk_release_candidates_coverage;
alter table public.release_candidates
    drop column if exists coverage_declaration_id;
drop table if exists public.issue_coverage_declarations cascade;
alter table public.documents
    drop constraint if exists fk_documents_source_delivery;
drop index if exists public.ix_documents_source_delivery_id;
alter table public.documents drop column if exists source_delivery_id;
alter table public.source_deliveries
    drop constraint if exists uq_source_deliveries_row;
"""

# A confirmed coverage declaration names one customer's sources by name, a
# request names what that customer was about to be sent, and an attempt names
# why it could not be. All three are partitioned for #531's reason.
COVERAGE_PREPARATION_PARTITION_POLICIES = f"""
do $$
declare
    v_roles text;
    v_table text;
begin
    select string_agg(quote_ident(rolname), ', ' order by rolname)
      into v_roles
      from pg_roles
     where rolname in ({_UNPARTITIONED_ROLES_SQL});
    foreach v_table in array array['issue_coverage_declarations',
                                   'release_preparation_requests',
                                   'release_preparation_attempts'] loop
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


def _refuse_unrepresentable_coverage_declaration_downgrade(bind) -> None:
    """Refuse rather than drop what a person confirmed and a worker attempted.

    The schema this downgrade restores has nowhere to hold a confirmed coverage
    declaration, the request it authorised, or what the attempt at it produced,
    and dropping the candidate's coverage reference would leave every prepared
    candidate unable to say whose confirmation it was prepared under. The same
    discipline as #529's, #533's and #640's downgrades: state what cannot be
    carried back, and stop.
    """

    stored = bind.execute(
        sa.text(
            "select count(*) from information_schema.tables "
            " where table_schema = 'public' "
            "   and table_name = 'issue_coverage_declarations'"
        )
    ).scalar_one()
    if not stored:
        return
    blocked = bind.execute(
        sa.text(
            "select (select count(*) from issue_coverage_declarations) "
            "     + (select count(*) from release_preparation_requests) "
            "     + (select count(*) from release_preparation_attempts)"
        )
    ).scalar_one()
    if blocked:
        raise RuntimeError(
            f"#675 downgrade refuses: {blocked} confirmed coverage and "
            "preparation record(s) state what one issue was prepared under and "
            "who confirmed it, and the schema without them cannot represent "
            "it. Nothing is dropped here."
        )


def upgrade(op) -> None:
    # Last, because a declaration references the issue profile version #640
    # creates and the delivery ledger #599 renames, a request references the
    # accepted revision the spine creates and the declaration above, an attempt
    # references the candidate #529 creates, and every one of the three calls
    # the partition command #531 creates and re-declares under #657 and #676.
    op.execute(COVERAGE_PREPARATION_SCHEMA)
    op.execute(COVERAGE_PREPARATION_TRIGGERS)
    # #839's designation proof, before the grants below hand anybody INSERT:
    # the rule exists for the first row this schema can hold, not from the
    # first row somebody remembered to check.
    op.execute(COORDINATION_DESIGNATION_GUARD)
    op.execute(
        "revoke all on function public.enforce_coordination_designation() "
        "from public"
    )
    op.execute(COORDINATION_DESIGNATION_TRIGGERS)
    for table in COVERAGE_PREPARATION_TABLES:
        # A new table arrives carrying the schema owner's default privileges,
        # which hand every runtime login full access including UPDATE and
        # DELETE. #531, #640, #529 and #533 each had to take that back
        # explicitly and so does this: a confirmed declaration, a submitted
        # request and a finished attempt are all records of something that
        # happened, so no login holds a privilege that would change one, and
        # the immutability trigger above is the second line rather than the
        # only one.
        op.execute(f"revoke all on public.{table} from {RUNTIME_LOGINS}")
        op.execute(f"grant select on public.{table} to {RUNTIME_LOGINS}")
        # Confirming coverage, requesting a preparation, and recording what an
        # attempt produced are all application work that writes no accepted
        # authority and makes nothing effective, so the runtime capabilities
        # append these rows directly rather than through a `SECURITY DEFINER`
        # command, exactly as #529 decided for the candidate itself. What they
        # may never do is change one -- and, since #839, what they may never do
        # is append the first two on behalf of somebody the roster does not
        # designate to coordinate this project.
        op.execute(f"grant insert on public.{table} to {RUNTIME_LOGINS}")
        op.execute(
            f"grant usage, select on sequence public.{table}_id_seq "
            f"to {RUNTIME_LOGINS}"
        )
    # The one column #675 adds to an existing relation. `documents` is written
    # by the intake and corpus paths that already hold INSERT on it, so the
    # link needs no grant of its own; it is named here because a reader of this
    # block should not have to infer that nothing was widened.
    op.execute(COVERAGE_PREPARATION_PARTITION_POLICIES)


def downgrade(op) -> None:
    # First, because the upgrade added it last. Every confirmed declaration and
    # every preparation record would go silently, and the candidate would lose
    # the confirmation it was prepared under, so this refuses instead.
    _refuse_unrepresentable_coverage_declaration_downgrade(op.get_bind())
    op.execute(COVERAGE_PREPARATION_SCHEMA_DOWN)
