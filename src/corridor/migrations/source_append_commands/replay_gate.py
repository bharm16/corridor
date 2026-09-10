"""One activation ledger for every ADR-0050 replay gate (card 8).

ADR-0050's gate is not per-family. The pass is void when the rule's own
fingerprint or its schema changes, a deliberate human suspension beats every
passing test, and only an attributable human act lifts one — the same three
sentences for the automatic location link rule, the corroborated
unreadable-cell admission class, the whole-row organization identity tiers and
the unknown-scope Event Admission class. Four expansions nevertheless grew four
tables with the same seven columns, and two of them grew the same nine lines of
``activation_status`` character for character.

This family folds those four relations into ``policy_activations``, keyed by
policy family and rule fingerprint. ``corridor.replay_gate`` is the one reader
and writer; the four model classes are typed views onto this relation
(single-table inheritance on ``family``), so a query written for one family
cannot read another family's rows and an operator screen reads every family in
one statement.

**Two columns are per-family shape, and the checks say so.** Three families bind
a pass to ``policy_sha256``, the digest of their canonical policy. The Event
Admission family binds a pass to an immutable clone-based acceptance receipt
instead, so it carries ``acceptance_receipt_id`` and no digest.
``ck_policy_activation_sha256`` and ``ck_policy_activation_receipt`` make each
family's other shape unrepresentable rather than merely unwritten, and the
receipt guard trigger still refuses an Event Admission activation whose receipt
is not the passing receipt for that project and policy version.

**Row identity.** Four independent sequences cannot merge without renumbering
something. The Event Admission ledger is the only one whose row ids are pinned
outside the database (the SH99 shared-admission seal names activation 140), so
its rows keep their ids exactly and the other three families are re-keyed above
them in ``created_at`` order. The downgrade reverses that: Event Admission rows
return under their own ids, the rest are re-keyed into their own sequences in
ledger order, so a round trip loses no row and no attribution.

**Grants.** A table created here arrives with the schema owner's default
privileges, which hand every runtime login full access. The four relations this
replaces were revoked from ``corridor_web`` by #680 — no enabled live-pilot
route reads an activation ledger — so the consolidated relation takes that
privilege straight back rather than inheriting a boundary nobody decided
(``corridor.access.NOT_YET_PARTITIONED_RELATIONS`` records the same answer
once instead of four times).
"""

from __future__ import annotations

from corridor.migrations.source_append_commands.roles import RUNTIME_LOGINS


# The four relations this family replaces, and the family each becomes. The
# order is the order rows are carried forward in, after the receipt-bound family
# has kept its own ids.
REPLACED_LEDGERS = (
    ("schedule_link_activations", "schedule_link"),
    ("organization_identity_activations", "organization_identity"),
    ("unreadable_cell_admission_activations", "unreadable_cell_admission"),
)
RECEIPT_BOUND_LEDGER = ("event_admission_activations", "event_admission")

POLICY_ACTIVATION_SCHEMA = """
create table public.policy_activations (
    id bigserial primary key,
    project_id bigint not null references public.projects (id),
    -- The policy family this entry governs. It is the discriminator the four
    -- typed views select on, so a family cannot read another family's history.
    family character varying(48) not null,
    action character varying(16) not null,
    policy_version character varying(64) not null,
    -- The digest of the canonical policy a fingerprint-bound pass stands on.
    policy_sha256 character varying(64),
    -- How many recorded human decisions the replay compared. Null for a
    -- suspension, which proves nothing, and for the receipt-bound family, whose
    -- receipt carries the replayed population.
    replay_case_count integer,
    -- The immutable acceptance receipt a receipt-bound pass is bound to.
    acceptance_receipt_id bigint
        references public.event_admission_acceptance_receipts (id),
    reason character varying(160) not null,
    recorded_by character varying(128) not null,
    created_at timestamp with time zone not null default now(),
    constraint ck_policy_activation_action
        check (action in ('activate', 'suspend')),
    constraint ck_policy_activation_family
        check (length(btrim(family)) > 0),
    constraint ck_policy_activation_reason
        check (length(btrim(reason)) > 0),
    constraint ck_policy_activation_actor
        check (length(btrim(recorded_by)) > 0),
    -- A digest family carries a well-formed digest; the receipt-bound family
    -- carries none. Written as a `case` rather than as two `or`ed clauses,
    -- because `policy_sha256 ~ '...'` is null for a null digest and a check
    -- that evaluates to null passes: the two-clause form admitted exactly
    -- the row it was written to refuse.
    constraint ck_policy_activation_sha256 check (
        case when family = 'event_admission'
             then policy_sha256 is null
             else policy_sha256 is not null
                  and policy_sha256 ~ '^[0-9a-f]{64}$'
        end
    ),
    -- Only the receipt-bound family names a receipt, and it always does.
    constraint ck_policy_activation_receipt check (
        (family = 'event_admission') = (acceptance_receipt_id is not null)
    ),
    -- A suspension proves nothing, so it never carries a case count.
    constraint ck_policy_activation_case_count check (
        action <> 'suspend' or replay_case_count is null
    )
);

create index ix_policy_activations_project_id
    on public.policy_activations (project_id);
-- The newest entry for one family in one project is the whole status read.
create index ix_policy_activations_family
    on public.policy_activations (project_id, family, id desc);

create function public.reject_policy_activation_mutation() returns trigger
    language plpgsql
    as $$
        begin
            raise exception 'policy activation history is append-only';
        end;
        $$;

create function public.require_passing_policy_activation_receipt()
    returns trigger
    language plpgsql
    as $$
        begin
            if new.family <> 'event_admission' then
                return new;
            end if;
            if not exists (
                select 1
                from event_admission_acceptance_receipts receipt
                where receipt.id = new.acceptance_receipt_id
                  and receipt.project_id = new.project_id
                  and receipt.policy_version = new.policy_version
                  and receipt.status = 'passed'
            ) then
                raise exception 'Event Admission activation requires its passing exact receipt';
            end if;
            return new;
        end;
        $$;

create trigger policy_activations_are_immutable
    before update or delete on public.policy_activations
    for each row execute function public.reject_policy_activation_mutation();
create trigger policy_activations_reject_truncate
    before truncate on public.policy_activations
    for each statement
    execute function public.reject_policy_activation_mutation();
create trigger policy_activations_require_passing_receipt
    before insert on public.policy_activations
    for each row
    execute function public.require_passing_policy_activation_receipt();
"""

POLICY_ACTIVATION_SCHEMA_DOWN = """
drop table if exists public.policy_activations cascade;
drop function if exists public.require_passing_policy_activation_receipt() cascade;
drop function if exists public.reject_policy_activation_mutation() cascade;
"""

# The four relations, restored exactly as the supported predecessor holds them.
RESTORED_LEDGERS_SCHEMA = """
create table public.event_admission_activations (
    id bigint not null,
    project_id integer not null,
    acceptance_receipt_id bigint not null,
    action character varying(16) not null,
    policy_version character varying(64) not null,
    reason character varying(128) not null,
    recorded_by character varying(128) not null,
    created_at timestamp with time zone default now() not null,
    constraint ck_event_admission_activation_action
        check (action in ('activate', 'suspend')),
    constraint ck_event_admission_activation_actor
        check (length(btrim(recorded_by)) > 0),
    constraint ck_event_admission_activation_reason
        check (length(btrim(reason)) > 0)
);
create sequence public.event_admission_activations_id_seq
    as bigint start with 1 increment by 1 no minvalue no maxvalue cache 1;
alter sequence public.event_admission_activations_id_seq
    owned by public.event_admission_activations.id;
alter table only public.event_admission_activations
    alter column id
    set default nextval('public.event_admission_activations_id_seq'::regclass);
alter table only public.event_admission_activations
    add constraint event_admission_activations_pkey primary key (id);
alter table only public.event_admission_activations
    add constraint event_admission_activations_acceptance_receipt_id_fkey
    foreign key (acceptance_receipt_id)
    references public.event_admission_acceptance_receipts (id);
alter table only public.event_admission_activations
    add constraint event_admission_activations_project_id_fkey
    foreign key (project_id) references public.projects (id);
create index ix_event_admission_activation_project
    on public.event_admission_activations (project_id, id);

create table public.organization_identity_activations (
    id bigint not null,
    project_id bigint not null,
    action character varying(16) not null,
    policy_version character varying(64) not null,
    policy_sha256 character varying(64) not null,
    replay_case_count integer,
    reason character varying(160) not null,
    recorded_by character varying(128) not null,
    created_at timestamp with time zone default now() not null,
    constraint ck_organization_identity_activation_action
        check (action in ('activate', 'suspend')),
    constraint ck_organization_identity_activation_actor
        check (length(btrim(recorded_by)) > 0),
    constraint ck_organization_identity_activation_reason
        check (length(btrim(reason)) > 0),
    constraint ck_organization_identity_activation_sha256
        check (policy_sha256 ~ '^[0-9a-f]{64}$')
);
create sequence public.organization_identity_activations_id_seq
    as bigint start with 1 increment by 1 no minvalue no maxvalue cache 1;
alter sequence public.organization_identity_activations_id_seq
    owned by public.organization_identity_activations.id;
alter table only public.organization_identity_activations
    alter column id set default
    nextval('public.organization_identity_activations_id_seq'::regclass);
alter table only public.organization_identity_activations
    add constraint organization_identity_activations_pkey primary key (id);
alter table only public.organization_identity_activations
    add constraint organization_identity_activations_project_id_fkey
    foreign key (project_id) references public.projects (id);
create index ix_organization_identity_activations_project_id
    on public.organization_identity_activations (project_id);

create table public.schedule_link_activations (
    id bigint not null,
    project_id bigint not null,
    action character varying(16) not null,
    policy_version character varying(64) not null,
    policy_sha256 character varying(64) not null,
    replay_case_count integer,
    reason character varying(160) not null,
    recorded_by character varying(128) not null,
    created_at timestamp with time zone default now() not null,
    constraint ck_schedule_link_activation_action
        check (action in ('activate', 'suspend')),
    constraint ck_schedule_link_activation_actor
        check (length(btrim(recorded_by)) > 0),
    constraint ck_schedule_link_activation_reason
        check (length(btrim(reason)) > 0),
    constraint ck_schedule_link_activation_sha256
        check (policy_sha256 ~ '^[0-9a-f]{64}$')
);
create sequence public.schedule_link_activations_id_seq
    as bigint start with 1 increment by 1 no minvalue no maxvalue cache 1;
alter sequence public.schedule_link_activations_id_seq
    owned by public.schedule_link_activations.id;
alter table only public.schedule_link_activations
    alter column id set default
    nextval('public.schedule_link_activations_id_seq'::regclass);
alter table only public.schedule_link_activations
    add constraint schedule_link_activations_pkey primary key (id);
alter table only public.schedule_link_activations
    add constraint schedule_link_activations_project_id_fkey
    foreign key (project_id) references public.projects (id);
create index ix_schedule_link_activations_project_id
    on public.schedule_link_activations (project_id);

create table public.unreadable_cell_admission_activations (
    id bigint not null,
    project_id bigint not null,
    action character varying(16) not null,
    policy_version character varying(64) not null,
    policy_sha256 character varying(64) not null,
    replay_case_count integer,
    reason character varying(160) not null,
    recorded_by character varying(128) not null,
    created_at timestamp with time zone default now() not null,
    constraint ck_unreadable_cell_admission_action
        check (action in ('activate', 'suspend')),
    constraint ck_unreadable_cell_admission_actor
        check (length(btrim(recorded_by)) > 0),
    constraint ck_unreadable_cell_admission_reason
        check (length(btrim(reason)) > 0),
    constraint ck_unreadable_cell_admission_sha
        check (policy_sha256 ~ '^[0-9a-f]{64}$')
);
create sequence public.unreadable_cell_admission_activations_id_seq
    as bigint start with 1 increment by 1 no minvalue no maxvalue cache 1;
alter sequence public.unreadable_cell_admission_activations_id_seq
    owned by public.unreadable_cell_admission_activations.id;
alter table only public.unreadable_cell_admission_activations
    alter column id set default
    nextval('public.unreadable_cell_admission_activations_id_seq'::regclass);
alter table only public.unreadable_cell_admission_activations
    add constraint unreadable_cell_admission_activations_pkey primary key (id);
alter table only public.unreadable_cell_admission_activations
    add constraint unreadable_cell_admission_activations_project_id_fkey
    foreign key (project_id) references public.projects (id);
create index ix_unreadable_cell_admission_activations_project_id
    on public.unreadable_cell_admission_activations (project_id);

create trigger event_admission_activations_are_immutable
    before delete or update on public.event_admission_activations
    for each row
    execute function public.refuse_event_admission_acceptance_mutation();
create trigger event_admission_activations_require_passing_receipt
    before insert on public.event_admission_activations
    for each row
    execute function public.require_passing_event_admission_activation();
create trigger organization_identity_activations_are_immutable
    before delete or update on public.organization_identity_activations
    for each row
    execute function public.reject_organization_identity_mutation();
create trigger organization_identity_activations_reject_truncate
    before truncate on public.organization_identity_activations
    for each statement
    execute function public.reject_organization_identity_mutation();
create trigger schedule_link_activations_are_immutable
    before delete or update on public.schedule_link_activations
    for each row
    execute function public.reject_schedule_linking_mutation();
create trigger schedule_link_activations_reject_truncate
    before truncate on public.schedule_link_activations
    for each statement
    execute function public.reject_schedule_linking_mutation();
create trigger unreadable_cell_admission_activations_are_immutable
    before delete or update on public.unreadable_cell_admission_activations
    for each row
    execute function
        public.reject_unreadable_cell_admission_activations_mutation();
create trigger unreadable_cell_admission_activations_reject_truncate
    before truncate on public.unreadable_cell_admission_activations
    for each statement
    execute function
        public.reject_unreadable_cell_admission_activations_mutation();
"""


def _carry_rows_forward(op) -> None:
    """Move every recorded activation and suspension into the one relation.

    The receipt-bound family keeps its own row ids, because they are the only
    ledger ids named outside the database. The other three are re-keyed above
    them in ``created_at`` order, which is the order they were appended in.
    Nothing is summarised or dropped: an attribution that cannot be carried
    forward is a lost human act.
    """

    op.execute(
        "insert into public.policy_activations ("
        " id, project_id, family, action, policy_version, policy_sha256,"
        " replay_case_count, acceptance_receipt_id, reason, recorded_by,"
        " created_at) "
        "select id, project_id, 'event_admission', action, policy_version,"
        " null, null, acceptance_receipt_id, reason, recorded_by, created_at "
        "from public.event_admission_activations order by id"
    )
    op.execute(
        "select setval('public.policy_activations_id_seq',"
        " greatest(coalesce((select max(id) from public.policy_activations), 0), 1),"
        " (select max(id) is not null from public.policy_activations))"
    )
    for table, family in REPLACED_LEDGERS:
        op.execute(
            "insert into public.policy_activations ("
            " project_id, family, action, policy_version, policy_sha256,"
            " replay_case_count, acceptance_receipt_id, reason, recorded_by,"
            " created_at) "
            f"select project_id, '{family}', action, policy_version,"
            " policy_sha256, replay_case_count, null, reason, recorded_by,"
            f" created_at from public.{table} order by created_at, id"
        )


def _carry_rows_back(op) -> None:
    """Restore the four relations from the one, in ledger order."""

    op.execute(
        "insert into public.event_admission_activations ("
        " id, project_id, acceptance_receipt_id, action, policy_version,"
        " reason, recorded_by, created_at) "
        "select id, project_id, acceptance_receipt_id, action,"
        " policy_version, left(reason, 128), recorded_by, created_at "
        "from public.policy_activations where family = 'event_admission' "
        "order by id"
    )
    op.execute(
        "select setval('public.event_admission_activations_id_seq',"
        " greatest(coalesce("
        "  (select max(id) from public.event_admission_activations), 0), 1),"
        " (select max(id) is not null"
        "  from public.event_admission_activations))"
    )
    for table, family in REPLACED_LEDGERS:
        op.execute(
            f"insert into public.{table} ("
            " project_id, action, policy_version, policy_sha256,"
            " replay_case_count, reason, recorded_by, created_at) "
            "select project_id, action, policy_version, policy_sha256,"
            " replay_case_count, reason, recorded_by, created_at "
            f"from public.policy_activations where family = '{family}' "
            "order by id"
        )


def upgrade(op) -> None:
    op.execute(POLICY_ACTIVATION_SCHEMA)
    # The receipt guard would refuse the carried rows on the way in: the receipt
    # they name is genuine, and re-proving it row by row while moving history is
    # a check on the move, not on the decision. Disable it for the move only.
    op.execute(
        "alter table public.policy_activations "
        "disable trigger policy_activations_require_passing_receipt"
    )
    _carry_rows_forward(op)
    op.execute(
        "alter table public.policy_activations "
        "enable trigger policy_activations_require_passing_receipt"
    )
    # `cascade` takes each relation's own triggers, indexes and sequence. The
    # two guard *functions* the dropped triggers called stay: the downgrade
    # re-attaches them, and three of the four are still in use by the receipt and
    # derivation relations beside them.
    for table, _family in (RECEIPT_BOUND_LEDGER, *REPLACED_LEDGERS):
        op.execute(f"drop table public.{table} cascade")
    # No enabled live-pilot route reads an activation ledger (#680), and a new
    # relation arrives holding the schema owner's default privileges, so the web
    # capability's access is taken back explicitly. The worker keeps the append
    # it had on all four.
    op.execute("revoke all on public.policy_activations from corridor_web")
    op.execute("revoke all on sequence public.policy_activations_id_seq from corridor_web")
    op.execute("grant select, insert on public.policy_activations to corridor_worker")
    op.execute(
        "grant usage, select on sequence public.policy_activations_id_seq "
        "to corridor_worker"
    )


def downgrade(op) -> None:
    op.execute(RESTORED_LEDGERS_SCHEMA)
    op.execute(
        "alter table public.event_admission_activations "
        "disable trigger event_admission_activations_require_passing_receipt"
    )
    _carry_rows_back(op)
    op.execute(
        "alter table public.event_admission_activations "
        "enable trigger event_admission_activations_require_passing_receipt"
    )
    for table, _family in (RECEIPT_BOUND_LEDGER, *REPLACED_LEDGERS):
        op.execute(
            f"grant select, insert, update, delete on public.{table} "
            f"to {RUNTIME_LOGINS}"
        )
        op.execute(
            f"grant usage, select on sequence public.{table}_id_seq "
            f"to {RUNTIME_LOGINS}"
        )
    op.execute(POLICY_ACTIVATION_SCHEMA_DOWN)
