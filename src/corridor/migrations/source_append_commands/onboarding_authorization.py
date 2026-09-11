"""The limited onboarding authorization, enforced where the act commits (#827).

Folded into this transition for the same window reason as the blocks around
it: ``corridor.migrations.policy`` allows one unreleased transition and this is
it.

ADR-0099 decides that the work which must precede authoritative activation runs
under a **limited onboarding authorization**, and that the control plane is
authoritative for it. The control plane is a different database (ADR-0083), so
what lives here is not the authorization: it is the **grant** the customer
environment holds, recorded by a restricted operations actor, and the
enforcement the ADR asks for -- "the database adoption command must verify a
server-trusted, project-bound authorization, not accept a browser-supplied
Boolean saying onboarding is permitted".

Four relations, and each exists because a different sentence of the decision
could otherwise be satisfied by a flag.

``project_onboarding_grants`` is the grant itself, immutable, written only by
``record_onboarding_grant`` and executable only by the operations capability.
``corridor_web`` holds no execute on that command and no write on the relation,
which is what stops a project coordinator self-authorizing: the party that
wants customer data processed is not the party that may permit it. A reissue is
a new row at a higher ``grant_version``; nothing is ever updated.

``project_onboarding_grant_events`` is what happened to a grant afterwards,
append-only. It carries three different facts the decision insists are
different: a withdrawal **requested** by a verified customer representative, a
withdrawal **enforced** in this database, and an enforcement that **failed**.
It also carries ``governing_authorization_superseded`` -- the governing
customer-authorization version a grant names can be replaced, and when it is,
new protected writes require explicit revalidation and a current grant rather
than silently inheriting broader or narrower permission from the replacement.
And it carries ``revalidated``: the pilot has no offline grace for starting a
new protected onboarding act, so a positive validity result is good for a
stated finite window and no longer.

``project_onboarding_previews`` is the retained preview. The flow ADR-0099
implies and #893 already paid for is: authorized receipt, bounded compatibility
read, **retained** preview, coordinator approval, short atomic commit. A
preview retained here is one an approval request can verify by identity without
opening a workbook. ``adoptable`` is computed **by the database** from
``project_operating_mode`` at the moment the preview is retained, so a project
that has already adopted cannot be handed an adoptable preview even by a caller
holding a preview permission -- a remaining preview permission never overrides
the one-way adopted state.

``project_onboarding_acts`` is the retained proof that a permitted onboarding
act committed while the grant was valid. ADR-0099 rejects requiring an
unexpired authorization at activation and requires instead validity at the
moment of the act plus retained proof of it; that proof is this row, and it is
immutable, so the answers recorded in a completed adoption are history rather
than a form somebody can revisit. Its ``(project_id, operation, request_key)``
unique is the exact-retry key, and ``material_sha256`` is the canonical binding
of the submission that key names: the same key with a different payload is
conflicting reuse, not a retry. Its ``(project_id, operation)`` unique is what
refuses a second adoption under a new key.

What is deliberately **not** here:

- **No ``consumed`` column.** ADR-0099 says consumption can be derived from the
  retained receipt, and a second mutable flag is not necessarily required.
  ``project_onboarding_acts`` is that receipt: a committed ``adopt_baseline``
  act *is* the consumption of the adoption permission, and because the relation
  is immutable and uniquely keyed, a concurrent second attempt cannot consume
  it twice. One of the two transactions loses the unique and sees the other's
  committed result.
- **No control-plane content.** The customer's workbook stays here and the
  cross-customer authorization stays in the control plane; what crosses is
  identifiers, versions and digests.
- **No typed processing hold.** #919 owns that. The commands below refuse
  conservatively through the gate that exists, and name the seam rather than
  inventing a second one.

The downgrade drops the relations whole and refuses where a grant or an act
survives, because the supported predecessor has no shape that could carry the
proof an activation later reads.
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

GRANT_TABLE = "project_onboarding_grants"
EVENT_TABLE = "project_onboarding_grant_events"
PREVIEW_TABLE = "project_onboarding_previews"
ACT_TABLE = "project_onboarding_acts"

ONBOARDING_TABLES = (GRANT_TABLE, EVENT_TABLE, PREVIEW_TABLE, ACT_TABLE)

#: The closed set of operations a limited onboarding authorization may permit.
#: It is ADR-0099's "what it allows" list, one name per activity, plus the
#: bounded setup permission the decision's consequences require for the step
#: after adoption (#828) so consuming the adoption permission cannot strand a
#: coordinator between "baseline adopted" and "project ready for activation".
ONBOARDING_OPERATIONS = (
    "reach_project",
    "receive_source",
    "inspect_compatibility",
    "prepare_mapping",
    "review_baseline_questions",
    "adopt_baseline",
    "approve_issue_profile",
)

#: The operations that are themselves committing acts, so they leave a retained
#: proof row. Every other permitted operation is preparatory and consumes
#: nothing (ADR-0099's consumption table).
ONBOARDING_ACT_OPERATIONS = ("adopt_baseline", "approve_issue_profile")

#: What can be recorded about a grant after it is issued.
ONBOARDING_EVENT_KINDS = (
    "revalidated",
    "withdrawal_requested",
    "withdrawal_enforced",
    "withdrawal_enforcement_failed",
    "governing_authorization_superseded",
)

#: The coordinator questions that cannot be left open before an adoption
#: commits. The application owns the reading that raises them
#: (``corridor.baseline_adoption``); the revision is source bytes and may not
#: import it, so this is the frozen copy and
#: ``tests/test_onboarding_authorization.py`` is what keeps it honest.
BLOCKING_QUESTION_KINDS = (
    "adopted_scope",
    "likely_distinct_facilities",
    "conflicting_plan_basis",
)

_BLOCKING_KINDS_SQL = ", ".join(f"'{name}'" for name in BLOCKING_QUESTION_KINDS)
_OPERATIONS_SQL = ", ".join(f"'{name}'" for name in ONBOARDING_OPERATIONS)
_ACT_OPERATIONS_SQL = ", ".join(f"'{name}'" for name in ONBOARDING_ACT_OPERATIONS)
_EVENT_KINDS_SQL = ", ".join(f"'{name}'" for name in ONBOARDING_EVENT_KINDS)


ONBOARDING_AUTHORIZATION_SCHEMA = f"""
create table public.{GRANT_TABLE} (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    authorization_id character varying(128) not null,
    grant_version integer not null,
    customer character varying(128) not null,
    environment character varying(128) not null,
    permitted_operations text[] not null,
    source_scope character varying(256) not null,
    governing_authorization_identity character varying(128) not null,
    governing_authorization_version character varying(64) not null,
    evidence_identity character varying(256) not null,
    evidence_sha256 character varying(64) not null,
    issued_at timestamp with time zone not null,
    expires_at timestamp with time zone not null,
    issued_by_actor character varying(128) not null,
    recorded_by_actor character varying(128) not null,
    recorded_at timestamp with time zone not null default now(),
    constraint uq_{GRANT_TABLE}_project_id unique (project_id, id),
    constraint uq_{GRANT_TABLE}_version
        unique (project_id, authorization_id, grant_version),
    constraint ck_{GRANT_TABLE}_window check (expires_at > issued_at),
    constraint ck_{GRANT_TABLE}_version check (grant_version >= 1),
    constraint ck_{GRANT_TABLE}_operations check (
        array_length(permitted_operations, 1) >= 1
        and permitted_operations <@ array[{_OPERATIONS_SQL}]::text[]
    ),
    constraint ck_{GRANT_TABLE}_evidence check (
        evidence_sha256 ~ '^[0-9a-f]{{64}}$'
        and length(btrim(evidence_identity)) > 0
        and length(btrim(governing_authorization_identity)) > 0
        and length(btrim(governing_authorization_version)) > 0
    ),
    constraint ck_{GRANT_TABLE}_actors check (
        length(btrim(issued_by_actor)) > 0
        and length(btrim(recorded_by_actor)) > 0
        and length(btrim(authorization_id)) > 0
        and length(btrim(customer)) > 0
        and length(btrim(environment)) > 0
        and length(btrim(source_scope)) > 0
    )
);

create index ix_{GRANT_TABLE}_project_id on public.{GRANT_TABLE} (project_id);

create table public.{EVENT_TABLE} (
    id bigserial primary key,
    project_id bigint not null,
    grant_id bigint not null,
    kind character varying(48) not null,
    requested_by character varying(256),
    requested_at timestamp with time zone,
    executed_by_actor character varying(128) not null,
    executed_at timestamp with time zone not null,
    reason text,
    detail text,
    recorded_at timestamp with time zone not null default now(),
    constraint uq_{EVENT_TABLE}_project_id unique (project_id, id),
    constraint fk_{EVENT_TABLE}_grant
        foreign key (project_id, grant_id)
        references public.{GRANT_TABLE} (project_id, id),
    constraint ck_{EVENT_TABLE}_kind check (kind in ({_EVENT_KINDS_SQL})),
    constraint ck_{EVENT_TABLE}_actor check (
        length(btrim(executed_by_actor)) > 0
    ),
    -- A withdrawal the customer required records who required it and when, and
    -- that request time is the effective time operations answers for. An
    -- enforcement record without a request behind it would describe a
    -- withdrawal nobody asked for.
    constraint ck_{EVENT_TABLE}_withdrawal_request check (
        kind <> 'withdrawal_requested'
        or (
            requested_by is not null
            and length(btrim(requested_by)) > 0
            and requested_at is not null
            and reason is not null
            and length(btrim(reason)) > 0
        )
    ),
    constraint ck_{EVENT_TABLE}_failure check (
        kind <> 'withdrawal_enforcement_failed'
        or (detail is not null and length(btrim(detail)) > 0)
    ),
    constraint ck_{EVENT_TABLE}_supersession check (
        kind <> 'governing_authorization_superseded'
        or (reason is not null and length(btrim(reason)) > 0)
    )
);

create index ix_{EVENT_TABLE}_grant on public.{EVENT_TABLE} (project_id, grant_id);

create table public.{PREVIEW_TABLE} (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    grant_id bigint not null,
    source_sha256 character varying(64) not null,
    filename character varying(512) not null,
    source_identity character varying(256) not null,
    mapping_identity character varying(256) not null,
    mapping_version character varying(64) not null,
    binding_fingerprint character varying(64) not null,
    payload jsonb not null,
    operations_resolved boolean not null,
    blocking_question_count integer not null,
    adoptable boolean not null,
    prepared_by_actor character varying(128) not null,
    prepared_at timestamp with time zone not null,
    recorded_at timestamp with time zone not null default now(),
    constraint uq_{PREVIEW_TABLE}_project_id unique (project_id, id),
    constraint uq_{PREVIEW_TABLE}_fingerprint
        unique (project_id, binding_fingerprint),
    constraint fk_{PREVIEW_TABLE}_grant
        foreign key (project_id, grant_id)
        references public.{GRANT_TABLE} (project_id, id),
    constraint ck_{PREVIEW_TABLE}_digests check (
        source_sha256 ~ '^[0-9a-f]{{64}}$'
        and binding_fingerprint ~ '^[0-9a-f]{{64}}$'
    ),
    constraint ck_{PREVIEW_TABLE}_counts check (blocking_question_count >= 0),
    constraint ck_{PREVIEW_TABLE}_actor check (
        length(btrim(prepared_by_actor)) > 0
    )
);

create index ix_{PREVIEW_TABLE}_project_id on public.{PREVIEW_TABLE} (project_id);

create table public.{ACT_TABLE} (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    grant_id bigint not null,
    authorization_id character varying(128) not null,
    grant_version integer not null,
    operation character varying(48) not null,
    request_key character varying(160) not null,
    material_sha256 character varying(64) not null,
    principal character varying(128) not null,
    committed_at timestamp with time zone not null,
    validity jsonb not null,
    result jsonb not null,
    recorded_at timestamp with time zone not null default now(),
    constraint uq_{ACT_TABLE}_project_id unique (project_id, id),
    -- The exact-retry key. PostgreSQL's five identity checks are payload and
    -- authority checks; this is what identifies the user's own submission.
    constraint uq_{ACT_TABLE}_request
        unique (project_id, operation, request_key),
    -- One committing onboarding act of each kind per project. A second
    -- adoption under a new key has nowhere to land.
    constraint uq_{ACT_TABLE}_once unique (project_id, operation),
    constraint fk_{ACT_TABLE}_grant
        foreign key (project_id, grant_id)
        references public.{GRANT_TABLE} (project_id, id),
    constraint ck_{ACT_TABLE}_operation check (
        operation in ({_ACT_OPERATIONS_SQL})
    ),
    constraint ck_{ACT_TABLE}_digest check (
        material_sha256 ~ '^[0-9a-f]{{64}}$'
    ),
    constraint ck_{ACT_TABLE}_text check (
        length(btrim(request_key)) > 0 and length(btrim(principal)) > 0
    )
);

create index ix_{ACT_TABLE}_project_id on public.{ACT_TABLE} (project_id);
"""


# A grant and an act are written only by their own command, and never updated.
# The same shape #520 gave the adoption receipt, for the same reason: an
# authority derived from a row anybody can edit is not an authority.
ONBOARDING_AUTHORIZATION_GUARDS = f"""
create function public.enforce_onboarding_record_write() returns trigger
    language plpgsql
    as $$
        begin
            if tg_op <> 'INSERT' then
                raise exception 'onboarding authorization records are immutable: a change is a new attributable record, never an edit of what somebody permitted or did'
                    using errcode='23514';
            end if;
            if current_user <> '{RECORD_DECISION_ROLE}' then
                raise exception 'onboarding authorization records require their typed command'
                    using errcode='23514';
            end if;
            return new;
        end; $$;
"""

_GUARD_TRIGGERS = "\n".join(
    f"""
create trigger trg_{table}_write
    before insert or update or delete on public.{table}
    for each row execute function public.enforce_onboarding_record_write();
create trigger trg_{table}_truncate
    before truncate on public.{table}
    for each statement execute function public.enforce_onboarding_record_write();
"""
    for table in ONBOARDING_TABLES
)

ONBOARDING_AUTHORIZATION_TRIGGERS = _GUARD_TRIGGERS + f"""
-- The coordinator's designation, proved on the row the way #839 proves it, so
-- the roster is consulted rather than the principal string the caller passed.
create trigger trg_{ACT_TABLE}_designation
    before insert on public.{ACT_TABLE}
    for each row execute function public.enforce_coordination_designation('principal');
"""


RECORD_ONBOARDING_GRANT = f"""
create function public.record_onboarding_grant(
    p_project_id bigint,
    p_authorization_id character varying,
    p_grant_version integer,
    p_customer character varying,
    p_environment character varying,
    p_permitted_operations text[],
    p_source_scope character varying,
    p_governing_identity character varying,
    p_governing_version character varying,
    p_evidence_identity character varying,
    p_evidence_sha256 character varying,
    p_issued_at timestamp with time zone,
    p_expires_at timestamp with time zone,
    p_issued_by_actor character varying,
    p_recorded_by_actor character varying
) returns jsonb
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            prior {GRANT_TABLE}%ROWTYPE;
            grant_id bigint;
        begin
            select * into prior from {GRANT_TABLE}
             where project_id = p_project_id
               and authorization_id = p_authorization_id
               and grant_version = p_grant_version;
            if found then
                if prior.permitted_operations <> p_permitted_operations
                   or prior.expires_at is distinct from p_expires_at
                   or prior.governing_authorization_version
                       is distinct from p_governing_version then
                    raise exception 'onboarding_grant:version_bound_to_other_terms this authorization version already names different terms; reissue at a higher version'
                        using errcode='23514';
                end if;
                return jsonb_build_object('grant_id', prior.id, 'created', false);
            end if;
            if exists (
                select 1 from {GRANT_TABLE}
                 where project_id = p_project_id
                   and authorization_id = p_authorization_id
                   and grant_version >= p_grant_version
            ) then
                raise exception 'onboarding_grant:version_went_backwards a later version of this authorization is already recorded'
                    using errcode='23514';
            end if;
            insert into {GRANT_TABLE} (
                project_id, authorization_id, grant_version, customer,
                environment, permitted_operations, source_scope,
                governing_authorization_identity, governing_authorization_version,
                evidence_identity, evidence_sha256, issued_at, expires_at,
                issued_by_actor, recorded_by_actor
            ) values (
                p_project_id, p_authorization_id, p_grant_version, p_customer,
                p_environment, p_permitted_operations, p_source_scope,
                p_governing_identity, p_governing_version, p_evidence_identity,
                p_evidence_sha256, p_issued_at, p_expires_at,
                p_issued_by_actor, p_recorded_by_actor
            ) returning id into grant_id;
            return jsonb_build_object('grant_id', grant_id, 'created', true);
        end; $$;
"""

RECORD_ONBOARDING_GRANT_SIGNATURE = (
    "(bigint, character varying, integer, character varying, character varying, "
    "text[], character varying, character varying, character varying, "
    "character varying, character varying, timestamp with time zone, "
    "timestamp with time zone, character varying, character varying)"
)


RECORD_ONBOARDING_GRANT_EVENT = f"""
create function public.record_onboarding_grant_event(
    p_project_id bigint,
    p_grant_id bigint,
    p_kind character varying,
    p_requested_by character varying,
    p_requested_at timestamp with time zone,
    p_executed_by_actor character varying,
    p_executed_at timestamp with time zone,
    p_reason text,
    p_detail text
) returns jsonb
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            event_id bigint;
        begin
            if not exists (
                select 1 from {GRANT_TABLE}
                 where project_id = p_project_id and id = p_grant_id
            ) then
                raise exception 'onboarding_grant:unknown_grant this project holds no such onboarding authorization'
                    using errcode='23514';
            end if;
            insert into {EVENT_TABLE} (
                project_id, grant_id, kind, requested_by, requested_at,
                executed_by_actor, executed_at, reason, detail
            ) values (
                p_project_id, p_grant_id, p_kind, p_requested_by, p_requested_at,
                p_executed_by_actor, p_executed_at, p_reason, p_detail
            ) returning id into event_id;
            return jsonb_build_object('event_id', event_id);
        end; $$;
"""

RECORD_ONBOARDING_GRANT_EVENT_SIGNATURE = (
    "(bigint, bigint, character varying, character varying, "
    "timestamp with time zone, character varying, timestamp with time zone, "
    "text, text)"
)


# One reading of a grant's standing, used by every command below and by the
# application, so a Python reader and the commands can never disagree about
# whether onboarding writes are still permitted.
ONBOARDING_GRANT_STANDING = f"""
create function public.onboarding_grant_standing(
    p_project_id bigint,
    p_operation character varying,
    p_at timestamp with time zone,
    p_revalidation_window_seconds integer
) returns jsonb
    language plpgsql stable security definer
    set search_path to 'public'
    as $$
        declare
            held {GRANT_TABLE}%ROWTYPE;
            withdrawal {EVENT_TABLE}%ROWTYPE;
            superseded {EVENT_TABLE}%ROWTYPE;
            last_valid timestamp with time zone;
        begin
            select * into held from {GRANT_TABLE}
             where project_id = p_project_id
               and p_operation = any(permitted_operations)
             order by grant_version desc, id desc
             limit 1;
            if not found then
                return jsonb_build_object(
                    'permitted', false, 'reason', 'no_onboarding_authorization'
                );
            end if;

            select * into withdrawal from {EVENT_TABLE}
             where project_id = p_project_id
               and grant_id = held.id
               and kind in ('withdrawal_requested', 'withdrawal_enforced')
             order by id desc
             limit 1;
            if found then
                return jsonb_build_object(
                    'permitted', false,
                    'reason', 'onboarding_authorization_withdrawn',
                    'grant_id', held.id,
                    'withdrawn_at', withdrawal.executed_at
                );
            end if;

            select * into superseded from {EVENT_TABLE}
             where project_id = p_project_id
               and grant_id = held.id
               and kind = 'governing_authorization_superseded'
             order by id desc
             limit 1;
            if found then
                return jsonb_build_object(
                    'permitted', false,
                    'reason', 'governing_authorization_superseded',
                    'grant_id', held.id
                );
            end if;

            if p_at >= held.expires_at then
                return jsonb_build_object(
                    'permitted', false,
                    'reason', 'onboarding_authorization_expired',
                    'grant_id', held.id,
                    'expires_at', held.expires_at
                );
            end if;
            if p_at < held.issued_at then
                return jsonb_build_object(
                    'permitted', false,
                    'reason', 'onboarding_authorization_not_yet_in_force',
                    'grant_id', held.id
                );
            end if;

            -- No offline grace: a positive validity result is good for the
            -- stated window and then has to be established again.
            select greatest(
                       held.issued_at,
                       coalesce(max(executed_at), held.issued_at)
                   )
              into last_valid
              from {EVENT_TABLE}
             where project_id = p_project_id
               and grant_id = held.id
               and kind = 'revalidated';
            if last_valid is null then
                last_valid := held.issued_at;
            end if;
            if p_at > last_valid
                     + make_interval(secs => p_revalidation_window_seconds) then
                return jsonb_build_object(
                    'permitted', false,
                    'reason', 'onboarding_authorization_revalidation_required',
                    'grant_id', held.id,
                    'validated_at', last_valid
                );
            end if;

            return jsonb_build_object(
                'permitted', true,
                'grant_id', held.id,
                'authorization_id', held.authorization_id,
                'grant_version', held.grant_version,
                'operation', p_operation,
                'issued_at', held.issued_at,
                'expires_at', held.expires_at,
                'validated_at', last_valid,
                'governing_authorization_identity',
                    held.governing_authorization_identity,
                'governing_authorization_version',
                    held.governing_authorization_version,
                'evidence_identity', held.evidence_identity,
                'evidence_sha256', held.evidence_sha256,
                'source_scope', held.source_scope
            );
        end; $$;
"""

ONBOARDING_GRANT_STANDING_SIGNATURE = (
    "(bigint, character varying, timestamp with time zone, integer)"
)


RETAIN_ONBOARDING_PREVIEW = f"""
create function public.retain_onboarding_preview(
    p_project_id bigint,
    p_source_sha256 character varying,
    p_filename character varying,
    p_source_identity character varying,
    p_mapping_identity character varying,
    p_mapping_version character varying,
    p_binding_fingerprint character varying,
    p_payload jsonb,
    p_operations_resolved boolean,
    p_blocking_question_count integer,
    p_prepared_by_actor character varying,
    p_prepared_at timestamp with time zone,
    p_revalidation_window_seconds integer
) returns jsonb
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            standing jsonb;
            prior {PREVIEW_TABLE}%ROWTYPE;
            preview_id bigint;
            may_adopt boolean;
        begin
            standing := public.onboarding_grant_standing(
                p_project_id, 'inspect_compatibility', p_prepared_at,
                p_revalidation_window_seconds
            );
            if not (standing ->> 'permitted')::boolean then
                raise exception 'onboarding_preview:% this project may not be inspected for onboarding right now', standing ->> 'reason'
                    using errcode='42501';
            end if;

            -- The database decides adoptability, not the caller. A preview
            -- permission that outlives the adoption cannot produce a second
            -- adoptable baseline, because the one-way operating mode is read
            -- here and recorded on the row.
            may_adopt := public.project_operating_mode(p_project_id) = 'legacy';

            select * into prior from {PREVIEW_TABLE}
             where project_id = p_project_id
               and binding_fingerprint = p_binding_fingerprint;
            if found then
                return jsonb_build_object(
                    'preview_id', prior.id,
                    'created', false,
                    'adoptable', prior.adoptable
                );
            end if;

            insert into {PREVIEW_TABLE} (
                project_id, grant_id, source_sha256, filename, source_identity,
                mapping_identity, mapping_version, binding_fingerprint, payload,
                operations_resolved, blocking_question_count, adoptable,
                prepared_by_actor, prepared_at
            ) values (
                p_project_id, (standing ->> 'grant_id')::bigint, p_source_sha256,
                p_filename, p_source_identity, p_mapping_identity,
                p_mapping_version, p_binding_fingerprint, p_payload,
                p_operations_resolved, p_blocking_question_count, may_adopt,
                p_prepared_by_actor, p_prepared_at
            ) returning id into preview_id;
            return jsonb_build_object(
                'preview_id', preview_id, 'created', true, 'adoptable', may_adopt
            );
        end; $$;
"""

RETAIN_ONBOARDING_PREVIEW_SIGNATURE = (
    "(bigint, character varying, character varying, character varying, "
    "character varying, character varying, character varying, jsonb, boolean, "
    "integer, character varying, timestamp with time zone, integer)"
)


# The checks, with nothing written. A caller asks this before doing the work an
# act needs, so an exact retry returns the original receipt without repeating
# it, and a refusal is the bounded one the screen prints rather than a
# constraint violation after a hundred writes. `commit_onboarding_act` runs
# every one of them again at the end, so this is a courtesy, never the
# authority.
CHECK_ONBOARDING_ACT = f"""
create function public.check_onboarding_act(
    p_project_id bigint,
    p_operation character varying,
    p_request_key character varying,
    p_material_sha256 character varying,
    p_at timestamp with time zone,
    p_preview_fingerprint character varying,
    p_answers jsonb,
    p_revalidation_window_seconds integer
) returns jsonb
    language plpgsql stable security definer
    set search_path to 'public'
    as $$
        declare
            prior {ACT_TABLE}%ROWTYPE;
            standing jsonb;
            retained {PREVIEW_TABLE}%ROWTYPE;
        begin
            if p_request_key is null or length(btrim(p_request_key)) = 0 then
                raise exception 'onboarding_act:missing_request_key this submission carries no request key, so an exact retry could not be told from a second act'
                    using errcode='23514';
            end if;

            -- The exact retry, answered before any authority is read. ADR-0099
            -- makes this retrieval of a prior result, not the exercise of an
            -- expired permission, so neither the grant's liveness nor today's
            -- proposed preview is consulted. The caller has already proved
            -- current identity and read access to get here.
            select * into prior from {ACT_TABLE}
             where project_id = p_project_id
               and operation = p_operation
               and request_key = p_request_key;
            if found then
                if prior.material_sha256 is distinct from p_material_sha256 then
                    raise exception 'onboarding_act:conflicting_reuse this request key is already bound to a different submission'
                        using errcode='23514';
                end if;
                return jsonb_build_object(
                    'replay', true,
                    'act_id', prior.id,
                    'result', prior.result,
                    'committed_at', prior.committed_at
                );
            end if;

            if exists (
                select 1 from {ACT_TABLE}
                 where project_id = p_project_id and operation = p_operation
            ) then
                raise exception 'onboarding_act:already_performed this project has already completed that onboarding act; a change to it is a later supported act, not another one of these'
                    using errcode='23514';
            end if;

            standing := public.onboarding_grant_standing(
                p_project_id, p_operation, p_at, p_revalidation_window_seconds
            );
            if not (standing ->> 'permitted')::boolean then
                raise exception 'onboarding_act:% this onboarding act is not permitted right now', standing ->> 'reason'
                    using errcode='42501';
            end if;

            if p_operation = 'adopt_baseline' then
                if public.project_operating_mode(p_project_id) <> 'legacy' then
                    raise exception 'onboarding_act:already_adopted this project already adopted a baseline; replacing it is a later record change'
                        using errcode='23514';
                end if;
                select * into retained from {PREVIEW_TABLE}
                 where project_id = p_project_id
                   and binding_fingerprint = p_preview_fingerprint;
                if not found then
                    raise exception 'onboarding_act:preview_not_retained the preview being adopted is not one this project retained'
                        using errcode='23514';
                end if;
                if not retained.adoptable then
                    raise exception 'onboarding_act:preview_not_adoptable that preview was retained for verification, not as an adoptable baseline'
                        using errcode='23514';
                end if;
                if not retained.operations_resolved then
                    raise exception 'onboarding_act:operations_unresolved Corridor operations has not resolved this workbook yet'
                        using errcode='23514';
                end if;
                -- Every question the retained reading raises that cannot be
                -- left open has an answer in this submission. Counting is not
                -- enough: the answer has to be to the question that was asked,
                -- so both are matched on kind and subject.
                if exists (
                    select 1
                      from jsonb_array_elements(retained.payload -> 'questions') as q
                     where q ->> 'kind' in ({_BLOCKING_KINDS_SQL})
                       and not exists (
                           select 1
                             from jsonb_array_elements(
                                 coalesce(p_answers, '[]'::jsonb)
                             ) as a
                            where a ->> 'kind' = q ->> 'kind'
                              and a ->> 'subject' = q ->> 'subject'
                       )
                ) then
                    raise exception 'onboarding_act:blocking_question_unresolved a question that has to be decided before adoption is still open'
                        using errcode='23514';
                end if;
            end if;

            return jsonb_build_object('replay', false, 'standing', standing);
        end; $$;
"""

CHECK_ONBOARDING_ACT_SIGNATURE = (
    "(bigint, character varying, character varying, character varying, "
    "timestamp with time zone, character varying, jsonb, integer)"
)


COMMIT_ONBOARDING_ACT = f"""
create function public.commit_onboarding_act(
    p_project_id bigint,
    p_operation character varying,
    p_request_key character varying,
    p_material_sha256 character varying,
    p_principal character varying,
    p_at timestamp with time zone,
    p_preview_fingerprint character varying,
    p_answers jsonb,
    p_revalidation_window_seconds integer,
    p_result jsonb
) returns jsonb
    language plpgsql security definer
    set search_path to 'public'
    as $$
        declare
            prior {ACT_TABLE}%ROWTYPE;
            standing jsonb;
            retained {PREVIEW_TABLE}%ROWTYPE;
            act_id bigint;
        begin
            if p_request_key is null or length(btrim(p_request_key)) = 0 then
                raise exception 'onboarding_act:missing_request_key this submission carries no request key, so an exact retry could not be told from a second act'
                    using errcode='23514';
            end if;

            -- The exact retry, answered before any authority is read. ADR-0099
            -- makes this retrieval of a prior result, not the exercise of an
            -- expired permission, so neither the grant's liveness nor today's
            -- proposed preview is consulted. The caller has already proved
            -- current identity and read access to get here.
            select * into prior from {ACT_TABLE}
             where project_id = p_project_id
               and operation = p_operation
               and request_key = p_request_key;
            if found then
                if prior.material_sha256 is distinct from p_material_sha256 then
                    raise exception 'onboarding_act:conflicting_reuse this request key is already bound to a different submission'
                        using errcode='23514';
                end if;
                return jsonb_build_object(
                    'act_id', prior.id,
                    'created', false,
                    'result', prior.result,
                    'committed_at', prior.committed_at
                );
            end if;

            if exists (
                select 1 from {ACT_TABLE}
                 where project_id = p_project_id and operation = p_operation
            ) then
                raise exception 'onboarding_act:already_performed this project has already completed that onboarding act; a change to it is a later supported act, not another one of these'
                    using errcode='23514';
            end if;

            standing := public.onboarding_grant_standing(
                p_project_id, p_operation, p_at, p_revalidation_window_seconds
            );
            if not (standing ->> 'permitted')::boolean then
                raise exception 'onboarding_act:% this onboarding act is not permitted right now', standing ->> 'reason'
                    using errcode='42501';
            end if;

            if p_operation = 'adopt_baseline' then
                -- The operating mode is deliberately *not* re-read here. This
                -- command runs last, in the same transaction as the adoption
                -- it attributes, so by now the project's own act has already
                -- moved it out of `legacy`. `check_onboarding_act` owns that
                -- precondition, before the writes; what refuses a second
                -- adoption at this point is `uq_project_onboarding_acts_once`,
                -- which is also what makes a concurrent attempt converge on
                -- one adoption rather than write two.
                select * into retained from {PREVIEW_TABLE}
                 where project_id = p_project_id
                   and binding_fingerprint = p_preview_fingerprint;
                if not found then
                    raise exception 'onboarding_act:preview_not_retained the preview being adopted is not one this project retained'
                        using errcode='23514';
                end if;
                if not retained.adoptable then
                    raise exception 'onboarding_act:preview_not_adoptable that preview was retained for verification, not as an adoptable baseline'
                        using errcode='23514';
                end if;
                if not retained.operations_resolved then
                    raise exception 'onboarding_act:operations_unresolved Corridor operations has not resolved this workbook yet'
                        using errcode='23514';
                end if;
                -- Every question the retained reading raises that cannot be
                -- left open has an answer in this submission. Counting is not
                -- enough: the answer has to be to the question that was asked,
                -- so both are matched on kind and subject.
                if exists (
                    select 1
                      from jsonb_array_elements(retained.payload -> 'questions') as q
                     where q ->> 'kind' in ({_BLOCKING_KINDS_SQL})
                       and not exists (
                           select 1
                             from jsonb_array_elements(
                                 coalesce(p_answers, '[]'::jsonb)
                             ) as a
                            where a ->> 'kind' = q ->> 'kind'
                              and a ->> 'subject' = q ->> 'subject'
                       )
                ) then
                    raise exception 'onboarding_act:blocking_question_unresolved a question that has to be decided before adoption is still open'
                        using errcode='23514';
                end if;
            end if;

            insert into {ACT_TABLE} (
                project_id, grant_id, authorization_id, grant_version,
                operation, request_key, material_sha256, principal,
                committed_at, validity, result
            ) values (
                p_project_id, (standing ->> 'grant_id')::bigint,
                standing ->> 'authorization_id',
                (standing ->> 'grant_version')::integer,
                p_operation, p_request_key, p_material_sha256, p_principal,
                p_at, standing, p_result
            ) returning id into act_id;
            return jsonb_build_object(
                'act_id', act_id, 'created', true, 'result', p_result
            );
        end; $$;
"""

COMMIT_ONBOARDING_ACT_SIGNATURE = (
    "(bigint, character varying, character varying, character varying, "
    "character varying, timestamp with time zone, character varying, jsonb, "
    "integer, jsonb)"
)


ONBOARDING_COMMANDS = {
    "record_onboarding_grant": RECORD_ONBOARDING_GRANT_SIGNATURE,
    "record_onboarding_grant_event": RECORD_ONBOARDING_GRANT_EVENT_SIGNATURE,
    "onboarding_grant_standing": ONBOARDING_GRANT_STANDING_SIGNATURE,
    "retain_onboarding_preview": RETAIN_ONBOARDING_PREVIEW_SIGNATURE,
    "check_onboarding_act": CHECK_ONBOARDING_ACT_SIGNATURE,
    "commit_onboarding_act": COMMIT_ONBOARDING_ACT_SIGNATURE,
}

#: Who may execute what. The custody rule of ADR-0099 lives in this mapping: a
#: coordinator's own session (``corridor_web``) can commit the act it is
#: authorized for and can retain a preview, and it cannot issue, reissue or
#: annotate the authorization that permits either. Recording a grant and its
#: events is the restricted operations actor's, so the party who wants customer
#: data processed is never the party who permits it.
ONBOARDING_COMMAND_GRANTS = {
    "record_onboarding_grant": ("corridor_worker",),
    "record_onboarding_grant_event": ("corridor_worker",),
    "onboarding_grant_standing": ("corridor_web", "corridor_worker"),
    "retain_onboarding_preview": ("corridor_web", "corridor_worker"),
    "check_onboarding_act": ("corridor_web", "corridor_worker"),
    "commit_onboarding_act": ("corridor_web",),
}


ONBOARDING_PARTITION_POLICIES = "\n".join(
    f"""
do $$
declare
    v_roles text;
    v_table text := '{table}';
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
    for table in ONBOARDING_TABLES
)

ONBOARDING_AUTHORIZATION_SCHEMA_DOWN = "\n".join(
    [
        *(
            f"drop function if exists public.{name}{signature};"
            for name, signature in ONBOARDING_COMMANDS.items()
        ),
        "drop function if exists public.enforce_onboarding_record_write() cascade;",
        *(
            f"drop table if exists public.{table} cascade;"
            for table in reversed(ONBOARDING_TABLES)
        ),
    ]
)


def upgrade(op) -> None:
    # After `coverage_preparation`, whose `enforce_coordination_designation`
    # this block's act trigger calls, and after `operating_mode`, whose
    # `project_operating_mode` both commands read. Nothing later in the
    # revision names these relations.
    op.execute(ONBOARDING_AUTHORIZATION_SCHEMA)
    op.execute(ONBOARDING_AUTHORIZATION_GUARDS)
    op.execute(ONBOARDING_AUTHORIZATION_TRIGGERS)
    for table in ONBOARDING_TABLES:
        # A new table arrives with the schema owner's default privileges, which
        # hand every runtime login full access; the write half is taken back and
        # only the commands' role keeps it.
        op.execute(f"revoke all on public.{table} from {RUNTIME_LOGINS}")
        op.execute(f"grant select on public.{table} to {RUNTIME_LOGINS}")
        op.execute(f"grant select, insert on public.{table} to {RECORD_DECISION_ROLE}")
        op.execute(
            f"grant usage, select on sequence public.{table}_id_seq "
            f"to {RECORD_DECISION_ROLE}"
        )
    op.execute(RECORD_ONBOARDING_GRANT)
    op.execute(RECORD_ONBOARDING_GRANT_EVENT)
    op.execute(ONBOARDING_GRANT_STANDING)
    op.execute(RETAIN_ONBOARDING_PREVIEW)
    op.execute(CHECK_ONBOARDING_ACT)
    op.execute(COMMIT_ONBOARDING_ACT)
    for name, signature in ONBOARDING_COMMANDS.items():
        op.execute(
            f"alter function public.{name}{signature} owner to {RECORD_DECISION_ROLE}"
        )
        op.execute(f"revoke all on function public.{name}{signature} from public")
        for login in ONBOARDING_COMMAND_GRANTS[name]:
            op.execute(
                f"grant execute on function public.{name}{signature} to {login}"
            )
    op.execute(ONBOARDING_PARTITION_POLICIES)


def downgrade(op) -> None:
    # Before `coverage_preparation` and `operating_mode` unwind the function and
    # the mode this block calls, mirroring the upgrade's order. The supported
    # predecessor has no relation that could carry the retained proof an
    # activation later reads, so refuse rather than drop it silently.
    for table in (GRANT_TABLE, ACT_TABLE):
        if op.get_bind().scalar(
            sa.text(f"select exists (select 1 from public.{table})")
        ):
            raise RuntimeError(
                "retained onboarding authorization records cannot be "
                "represented by the supported predecessor"
            )
    for table in ONBOARDING_TABLES:
        op.execute(
            f"do $$ begin "
            f"if exists (select 1 from pg_tables where schemaname = 'public' "
            f"and tablename = '{table}') then "
            f"revoke select on public.{table} from {RUNTIME_LOGINS}; "
            f"end if; end $$;"
        )
    op.execute(ONBOARDING_AUTHORIZATION_SCHEMA_DOWN)
