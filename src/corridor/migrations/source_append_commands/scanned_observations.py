"""The processing observation an Unconfirmed reading is bound to (#809).

#804 wired ingest to record every Textract-only cell as an Unconfirmed reading
(ADR-0094, ADR-0064): a ``unreadable_cell_resolutions`` row holding the
document, the page, the cell key, the value and a policy version, and nothing
that says *which observation of the provider* produced the value. Everything
that did say so lived outside the database — the adapter's identity file in
the cache scope, the raw OCR receipt on disk, the cost receipt — each of them
a Class B file that retention may delete, none of them a row a resolution
could name. A reading whose provenance is a file is a reading whose provenance
expires.

This family adds the one durable owner that was missing and binds the
resolution to it.

**``scanned_page_observations``** is one row per processing observation: one
``analyze_page`` binding ingest consumed for one page of one Document
rendition. It carries what the binding carries and nothing the record already
owns — the Document and its rendition digest, the page, the authorization
record the request was matched against, the cache-scope digest (the request
boundary, the operation and feature types, the request configuration and the
adapter and rasterizer identities, which is the reader/configuration identity
in one digest), the digest of the raster that was sent, the digest of the raw
response exactly as retained, the digest of the normalized reading, and the
provider's own model version and request id. Every digest references the
retained evidence rather than copying it: the raw response is found by its
digest in the scope directory while it lives, and provable from its digest
after. The identity of an observation is those references together, so the
same page re-read out of the same cached response converges on the row it
already has, and a different response, raster, scope or record is a new
observation beside it.

**The resolution binds by id.** ``unreadable_cell_resolutions`` gains
``observation_id``, the observation a scanned reading was read out of;
``source_region_id``, the routed region of the recorded routing decision the
cell fell in; and ``observation_unbound_reason``, the one declared reason a
scanned reading may carry no observation. ``run_id`` keeps its meaning — the
reading harness's run — and stays null on the scanned route; a row is read by
a harness run or by a provider observation, never both, and the check says so.

**What was rejected, and why.** An ``extraction_run_id`` foreign key was the
obvious column and the wrong one: ingest passes the *source digest* to the
adapter under the name ``extraction_run`` (``ingest.py``), no Extraction Run
exists on this route, and inventing a placeholder run to satisfy a foreign key
would have made the run table lie about what ran. A binding to the token
layer manifest was rejected because the manifest is a Class B artifact's
manifest, not the observation: it records the reading's digests but not the
raster, the scope or the record, and it is de-duplicated on layer content
rather than on the observation that produced it. Copying the receipt's
``provenance`` map onto every cell row was rejected as the value-copying
``tests/test_architecture.py`` ratchets against.

**Backfill.** A scanned reading that already exists — ``origin = 'harness'``
with no ``run_id``, which is the shape #804 wrote and no other writer does —
predates any observation row, and the only exact retained evidence about its
observation is that there is none. Those rows are marked
``predates_observation_binding`` rather than bound to a guess. Nothing else is
touched: the relation's append-only guard is disabled for that one statement
and re-enabled, as the activation ledger move disabled its receipt guard for
the move alone. The downgrade refuses while an observation exists: the supported
predecessor has no place for it, and dropping it would return the row to the
state this family exists to end.

**Grants and partition.** The relation is project-scoped and holds one
customer's readings, so it answers the partition the way the record tables do
(#657): the policy name matches ``p_%_project_partition`` so the live check
finds it, and it is recorded in ``access.PARTITIONED_RELATIONS``. Ingest
writes it as the worker capability, so the worker keeps the append; the web
capability reads its partition and writes nothing, exactly as it can write
nothing to the resolutions it binds.
"""

from __future__ import annotations

import sqlalchemy as sa

from corridor.migrations.source_append_commands.project_partition import (
    _UNPARTITIONED_ROLES_SQL,
)
from corridor.migrations.source_append_commands.roles import RUNTIME_LOGINS

OBSERVATION_TABLE = "scanned_page_observations"

# The one reason a scanned reading may carry no observation: it was written
# before this family existed, and no retained evidence binds it to one.
PREDATES_OBSERVATION_BINDING = "predates_observation_binding"

SCANNED_OBSERVATION_SCHEMA = """
create table public.scanned_page_observations (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    document_id bigint not null references public.documents (id),
    -- The rendition the adapter was asked about, as the binding names it.
    rendition_sha256 character varying(64) not null,
    page_no integer not null,
    -- The authorization record the request was matched against, by its id.
    authorization_record_id character varying(128) not null,
    -- The cache scope: the request boundary, the operation and feature
    -- types, the request configuration and the adapter and rasterizer
    -- identities, in one digest. This is the reader/configuration identity.
    scope_digest character varying(64) not null,
    -- The raster that was sent, the response exactly as retained, and the
    -- normalized reading, each by digest.
    raster_sha256 character varying(64) not null,
    raw_response_sha256 character varying(64) not null,
    reading_sha256 character varying(64) not null,
    -- What the provider reported about itself and about this request. Null
    -- when the response did not say.
    provider_model_version character varying(64),
    provider_request_id character varying(128),
    observed_at timestamp with time zone not null,
    created_at timestamp with time zone not null default now(),
    constraint ck_scanned_page_observation_page check (page_no > 0),
    constraint ck_scanned_page_observation_record
        check (length(btrim(authorization_record_id)) > 0),
    constraint ck_scanned_page_observation_rendition
        check (rendition_sha256 ~ '^[0-9a-f]{64}$'),
    constraint ck_scanned_page_observation_scope
        check (scope_digest ~ '^[0-9a-f]{64}$'),
    constraint ck_scanned_page_observation_raster
        check (raster_sha256 ~ '^[0-9a-f]{64}$'),
    constraint ck_scanned_page_observation_response
        check (raw_response_sha256 ~ '^[0-9a-f]{64}$'),
    constraint ck_scanned_page_observation_reading
        check (reading_sha256 ~ '^[0-9a-f]{64}$'),
    -- One observation is its references together: the same page read out of
    -- the same retained response under the same scope and record is the same
    -- observation, and anything else is a new one beside it.
    constraint uq_scanned_page_observation_identity unique (
        document_id, page_no, authorization_record_id, scope_digest,
        raster_sha256, raw_response_sha256, reading_sha256
    )
);
create index ix_scanned_page_observations_project_id
    on public.scanned_page_observations (project_id);

alter table public.unreadable_cell_resolutions
    add column observation_id bigint
        references public.scanned_page_observations (id),
    add column source_region_id character varying(64),
    add column observation_unbound_reason character varying(48);
create index ix_unreadable_cell_resolutions_observation_id
    on public.unreadable_cell_resolutions (observation_id);
"""

# The scanned readings that already exist: #804's shape, and no other
# writer's. The only exact retained evidence about their observation is that
# no row records one.
BACKFILL_UNBOUND_READINGS = f"""
update public.unreadable_cell_resolutions
   set observation_unbound_reason = '{PREDATES_OBSERVATION_BINDING}'
 where origin = 'harness' and run_id is null;
"""

# A row is read by a harness run or by a provider observation, never both; a
# scanned reading (a harness-origin row with no run) is bound to an
# observation or declares the one reason it is not; and the reason is never
# carried beside a binding it would contradict.
OBSERVATION_CHECK = f"""
alter table public.unreadable_cell_resolutions
    add constraint ck_unreadable_cell_resolution_observation check (
        (run_id is null or observation_id is null)
        and (observation_id is null or observation_unbound_reason is null)
        and (observation_unbound_reason is null
             or observation_unbound_reason = '{PREDATES_OBSERVATION_BINDING}')
        and (origin <> 'harness'
             or run_id is not null
             or observation_id is not null
             or observation_unbound_reason is not null)
    );
"""

SCANNED_OBSERVATION_PARTITION_POLICIES = f"""
do $$
declare
    v_roles text;
    v_table text := '{OBSERVATION_TABLE}';
begin
    select string_agg(quote_ident(rolname), ', ' order by rolname)
      into v_roles
      from pg_roles
     where rolname in ({_UNPARTITIONED_ROLES_SQL});
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
end $$;
"""

SCANNED_OBSERVATION_SCHEMA_DOWN = """
alter table public.unreadable_cell_resolutions
    drop constraint ck_unreadable_cell_resolution_observation;
alter table public.unreadable_cell_resolutions
    drop column observation_unbound_reason,
    drop column source_region_id,
    drop column observation_id;
drop table public.scanned_page_observations;
"""


def upgrade(op) -> None:
    # After the retained-outgoing-requests block and before the sibling
    # transitions: it alters `unreadable_cell_resolutions`, which the baseline
    # created, and creates one relation nothing later in the revision names.
    op.execute(SCANNED_OBSERVATION_SCHEMA)
    # The relation is append-only, and the guard would refuse the one declared
    # reason this family sets on rows that predate it. Setting a reason is not
    # a rewrite of a reading; disable the guard for that statement alone.
    op.execute(
        "alter table public.unreadable_cell_resolutions "
        "disable trigger unreadable_cell_resolutions_are_immutable"
    )
    op.execute(BACKFILL_UNBOUND_READINGS)
    op.execute(
        "alter table public.unreadable_cell_resolutions "
        "enable trigger unreadable_cell_resolutions_are_immutable"
    )
    op.execute(OBSERVATION_CHECK)
    # A new table arrives carrying the schema owner's default privileges,
    # which hand every runtime login full access. Ingest writes it as the
    # worker; the web capability reads its partition and writes nothing, as it
    # writes nothing to the resolutions this relation binds.
    op.execute(f"revoke all on public.{OBSERVATION_TABLE} from {RUNTIME_LOGINS}")
    op.execute(f"grant select on public.{OBSERVATION_TABLE} to {RUNTIME_LOGINS}")
    op.execute(f"grant insert on public.{OBSERVATION_TABLE} to corridor_worker")
    op.execute(
        f"grant usage, select on sequence public.{OBSERVATION_TABLE}_id_seq "
        "to corridor_worker"
    )
    op.execute(SCANNED_OBSERVATION_PARTITION_POLICIES)


def downgrade(op) -> None:
    # First among the feature reversals, mirroring the upgrade's last feature
    # block. The supported predecessor cannot name an observation, so a
    # database holding one refuses to go down rather than return its readings
    # to the unbound state this family ends.
    if op.get_bind().scalar(
        sa.text(f"select exists (select 1 from public.{OBSERVATION_TABLE})")
    ):
        raise RuntimeError(
            "scanned page observations cannot be represented by the supported "
            "predecessor"
        )
    op.execute(SCANNED_OBSERVATION_SCHEMA_DOWN)
