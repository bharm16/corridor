"""#690 The preparation a worker actually runs, and the authorities it reads.

#675 recorded a coordinator's request that this project's next issue be
prepared and #529 built the three phases that prepare one, and nothing
between them ever ran: `run_preparation_request` had no caller outside a
test, so a coordinator who pressed **Confirm coverage and prepare issue**
left a request no deployed process would execute and an Issue section that
read "Preparing this issue" for ever.  This block is the schema half of the
bridge.  The three relations it adds are each an answer to "which exact
retained authority did this preparation use", because the supervisor that
runs a request may resolve authorities and may never compose a second
interpretation of the issue.

**One occurrence names one request.**  Due Work occurrences are otherwise
coalesced from a cadence, and a cadence slot cannot say *which* request it
is for; one opaque occurrence processing fifty requests would also give the
fifty one shared lease, one shared retry budget and one shared receipt.
`release_preparation_publications` binds one occurrence to one request, both
ways unique, so the runtime's claim, lease and retry semantics apply to a
single request and a reclaimed occurrence resumes that request and no other.

**One request binds exactly one report-preparation reading.**
`release_preparation_readings` is that binding.  It is emphatically *not*
"the latest completed receipt for this project": the newest internal reading
moves whenever the weekly pass runs, so a request that resolved it late
would prepare a different window from the one it was submitted for, and a
failed preparation could advance the next reading's floor.  The row records
the receipt by identity and its result digest, freezes the window's
**ceilings** at the moment of binding, and records the **floors** taken from
the reading bound to the previous *authorized* package -- zero for a first
issue.  ADR-0086 is explicit that only an authorized package advances the
external comparison baseline, so a candidate that was merely prepared,
blocked or refused moves no floor.  The result itself stays owned by
`due_work_receipts.handler_result_json`; copying it here would copy state
another row already owns, which is the defect #598's ratchet refuses.

**A registered output template names its exact bytes.**
`project_baseline_format_objects` is the storage binding a registration was
missing.  `project_baseline_formats` stores a *digest*, and a replacement
registration could name a digest whose bytes were never retained: initial
adoption only works by accident, because the adopted workbook is staged and
registered as a Document before it becomes the as-adopted output template.
Storage reconciliation derives its expected objects from Documents,
Processing Artifacts, page renders and token layers, so such a template
would be absent from the store and invisible to the reconciler as well.
The row carries the registration's own identity, version and digest through
a composite foreign key, so a binding that disagrees with what was
registered is unrepresentable rather than merely unlikely, and the storage
key is a check-constrained function of the digest rather than a path
somebody chose.  A preparation whose template object cannot be proved fails
with a bounded reason and never falls back to another template.

All three are customer content and all three carry #531's partition for
#531's reason.  The two append-only records carry #529's immutability
trigger; the object binding is immutable through its grants, exactly as
#610's stored mapping declaration is.
"""

from __future__ import annotations

import sqlalchemy as sa

from corridor.migrations.source_append_commands.project_partition import (
    _UNPARTITIONED_ROLES_SQL,
)
from corridor.migrations.source_append_commands.roles import RUNTIME_LOGINS


PREPARATION_SUPERVISOR_APPEND_ONLY_TABLES = (
    "release_preparation_readings",
    "release_preparation_publications",
)

PREPARATION_SUPERVISOR_TABLES = (
    "project_baseline_format_objects",
) + PREPARATION_SUPERVISOR_APPEND_ONLY_TABLES

REPORT_PREPARATION_HANDLER_KEY = "report_preparation"

PREPARATION_SUPERVISOR_SCHEMA = f"""
-- The exact retained bytes one output-template registration was registered
-- over. Keyed by the registration, so a registration has at most one, and
-- carrying the registration's identity, version and digest so a binding that
-- names different content cannot be stored beside it.
create table public.project_baseline_format_objects (
    format_id bigint primary key,
    project_id bigint not null references public.projects (id),
    format_identity character varying(160) not null,
    format_version character varying(64) not null,
    content_sha256 character varying(64) not null,
    -- Derived from the digest, never chosen: a storage key a caller composed
    -- would let a registration point at bytes with another digest entirely.
    storage_key character varying(160) not null,
    byte_count bigint not null,
    file_suffix character varying(32) not null,
    retained_by_principal character varying(128) not null,
    retained_at timestamp with time zone not null,
    recorded_at timestamp with time zone not null default now(),
    constraint fk_project_baseline_format_objects_registration foreign key
        (format_id, project_id, format_identity, format_version,
         content_sha256)
        references public.project_baseline_formats
        (id, project_id, format_identity, format_version, content_sha256),
    constraint ck_project_baseline_format_objects_digest check (
        content_sha256 ~ '^[0-9a-f]{{64}}$'
    ),
    constraint ck_project_baseline_format_objects_key check (
        storage_key = substr(content_sha256, 1, 2) || '/' || content_sha256
            || file_suffix
    ),
    constraint ck_project_baseline_format_objects_suffix check (
        file_suffix = '' or file_suffix ~ '^[.][A-Za-z0-9.]{{1,16}}$'
    ),
    constraint ck_project_baseline_format_objects_bytes check (byte_count > 0),
    constraint ck_project_baseline_format_objects_principal check (
        length(btrim(retained_by_principal)) > 0
    )
);

create index ix_project_baseline_format_objects_project
    on public.project_baseline_format_objects (project_id, format_id);

-- The one completed report-preparation reading one request prepares under.
create table public.release_preparation_readings (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    request_id bigint not null,
    receipt_id bigint not null references public.due_work_receipts (id),
    handler_key character varying(64) not null,
    result_schema_version character varying(64) not null,
    result_sha256 character varying(64) not null,
    accepted_revision_id bigint not null,
    source_cutoff timestamp with time zone not null,
    -- The authorized package this window is measured from, or an explicit
    -- none for a project's first issue. Never the newest candidate, the
    -- newest Report Run, or the most recent failed preparation.
    previous_package_id bigint,
    prior_delta_floor bigint not null,
    prior_disposition_floor bigint not null,
    through_delta_id bigint not null,
    through_disposition_id bigint not null,
    bound_at timestamp with time zone not null,
    recorded_at timestamp with time zone not null default now(),
    constraint uq_release_preparation_readings_row unique (id, project_id),
    -- Exactly one, so a retry of the same request reuses the reading it was
    -- bound to rather than picking up whatever has completed since.
    constraint uq_release_preparation_readings_request unique (request_id),
    constraint fk_release_preparation_readings_request foreign key
        (request_id, project_id)
        references public.release_preparation_requests (id, project_id),
    constraint fk_release_preparation_readings_revision foreign key
        (accepted_revision_id, project_id)
        references public.project_record_revisions (id, project_id),
    constraint fk_release_preparation_readings_previous foreign key
        (previous_package_id, project_id)
        references public.release_packages (id, project_id),
    constraint ck_release_preparation_readings_digest check (
        result_sha256 ~ '^[0-9a-f]{{64}}$'
    ),
    constraint ck_release_preparation_readings_handler check (
        handler_key = '{REPORT_PREPARATION_HANDLER_KEY}'
    ),
    constraint ck_release_preparation_readings_schema check (
        length(btrim(result_schema_version)) > 0
    ),
    constraint ck_release_preparation_readings_floors check (
        prior_delta_floor >= 0 and prior_disposition_floor >= 0
    ),
    -- A watermark pair, never a pair of timestamps (#488): the window is
    -- (floor, ceiling] on append-only identifiers, so a ceiling below its own
    -- floor is not a window this schema can hold.
    constraint ck_release_preparation_readings_window check (
        through_delta_id >= prior_delta_floor
        and through_disposition_id >= prior_disposition_floor
    )
);

create index ix_release_preparation_readings_project
    on public.release_preparation_readings (project_id, id);

create index ix_release_preparation_readings_receipt
    on public.release_preparation_readings (receipt_id);

-- One Due Work occurrence, one preparation request.
create table public.release_preparation_publications (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    request_id bigint not null,
    occurrence_id bigint not null references public.due_work_occurrences (id),
    published_at timestamp with time zone not null,
    recorded_at timestamp with time zone not null default now(),
    constraint uq_release_preparation_publications_row unique (id, project_id),
    constraint uq_release_preparation_publications_request
        unique (request_id),
    constraint uq_release_preparation_publications_occurrence
        unique (occurrence_id),
    constraint fk_release_preparation_publications_request foreign key
        (request_id, project_id)
        references public.release_preparation_requests (id, project_id)
);

create index ix_release_preparation_publications_project
    on public.release_preparation_publications (project_id, id);
"""

PREPARATION_SUPERVISOR_TRIGGERS = "".join(
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
    for table in PREPARATION_SUPERVISOR_APPEND_ONLY_TABLES
)

PREPARATION_SUPERVISOR_SCHEMA_DOWN = """
drop table if exists public.release_preparation_publications cascade;
drop table if exists public.release_preparation_readings cascade;
drop table if exists public.project_baseline_format_objects cascade;
"""

_PREPARATION_SUPERVISOR_TABLES_SQL = ", ".join(
    f"'{table}'" for table in PREPARATION_SUPERVISOR_TABLES
)

# A registered template's bytes, the reading one issue was prepared under and
# the occurrence that carried it all name one customer's work, so all three
# carry #531's partition on the same terms as #675's three.
PREPARATION_SUPERVISOR_PARTITION_POLICIES = f"""
do $$
declare
    v_roles text;
    v_table text;
begin
    select string_agg(quote_ident(rolname), ', ' order by rolname)
      into v_roles
      from pg_roles
     where rolname in ({_UNPARTITIONED_ROLES_SQL});
    foreach v_table in array array[{_PREPARATION_SUPERVISOR_TABLES_SQL}] loop
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


def _refuse_unrepresentable_preparation_supervisor_downgrade(bind) -> None:
    """Refuse rather than drop the authorities a preparation was bound to.

    The schema this downgrade restores has nowhere to hold the exact reading a
    request prepared under, the occurrence that carried it, or the bytes a
    registered output template stands on. Dropping them would leave a prepared
    candidate unable to say which window it measured or which template it was
    rendered from. The same discipline as #529's, #533's, #640's and #675's
    downgrades: state what cannot be carried back, and stop.
    """

    stored = bind.execute(
        sa.text(
            "select count(*) from information_schema.tables "
            " where table_schema = 'public' "
            "   and table_name = 'release_preparation_readings'"
        )
    ).scalar_one()
    if not stored:
        return
    blocked = bind.execute(
        sa.text(
            "select (select count(*) from release_preparation_readings) "
            "     + (select count(*) from release_preparation_publications) "
            "     + (select count(*) from project_baseline_format_objects)"
        )
    ).scalar_one()
    if blocked:
        raise RuntimeError(
            f"#690 downgrade refuses: {blocked} bound reading, publication or "
            "retained output-template object state which exact authorities "
            "one preparation used, and the schema without them cannot "
            "represent it. Nothing is dropped here."
        )


def upgrade(op) -> None:
    # Last, because a bound reading references the preparation request #675
    # creates, the accepted revision the spine creates and the authorized
    # package #533 creates; a publication references a Due Work occurrence;
    # a retained template object references the registration #509 creates;
    # and all three call the partition command #531 creates and #657 and #676
    # re-declare. It follows #680's revoke deliberately: these three are
    # customer content and are partitioned, so they belong on the side of that
    # boundary the pilot keeps rather than the side it takes away.
    op.execute(PREPARATION_SUPERVISOR_SCHEMA)
    op.execute(PREPARATION_SUPERVISOR_TRIGGERS)
    for table in PREPARATION_SUPERVISOR_TABLES:
        # A new table arrives carrying the schema owner's default privileges,
        # which hand every runtime login full access including UPDATE and
        # DELETE. #531, #640, #529, #533, #675 and #680 each had to take that
        # back explicitly and so does this. Binding a reading to a request,
        # publishing an occurrence for one, and retaining the bytes a template
        # was registered over are all application work that writes no accepted
        # authority and makes nothing effective, so a runtime capability
        # appends these rows directly and can never change one.
        op.execute(f"revoke all on public.{table} from {RUNTIME_LOGINS}")
        op.execute(f"grant select, insert on public.{table} to {RUNTIME_LOGINS}")
    for table in PREPARATION_SUPERVISOR_APPEND_ONLY_TABLES:
        # The object binding is keyed by the registration's own id, so only
        # these two have a sequence to grant.
        op.execute(
            f"grant usage, select on sequence public.{table}_id_seq "
            f"to {RUNTIME_LOGINS}"
        )
    op.execute(PREPARATION_SUPERVISOR_PARTITION_POLICIES)


def downgrade(op) -> None:
    # First, because the upgrade added it last. A bound reading is the only
    # record of which window a preparation measured and which retained object
    # its template stood on, so this refuses rather than dropping it.
    _refuse_unrepresentable_preparation_supervisor_downgrade(op.get_bind())
    op.execute(PREPARATION_SUPERVISOR_SCHEMA_DOWN)
