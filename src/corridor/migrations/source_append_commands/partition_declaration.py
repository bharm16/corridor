"""#657 One transaction holds one scope, and every relation is classified.

#531 moved the project boundary into PostgreSQL for four spine relations and
#640, #529 and #533 each carried their own along.  Two things were still
open, and this block closes both.

**One transaction, one scope (#662).**  Nothing stopped a transaction from
declaring project A, reading, then declaring project B and reading again.
#654 measured that and recorded it as the behaviour: every successful
declaration re-seals the setting, so the scope simply moved.  That makes the
authorization context of a unit of work a moving target — a coordinator
surface, a batch, or a half-refactored reader can mix two projects' rows
inside one atomic read and no rule anywhere objects.  So a declaration now
also seals *what was declared*: the scope kind, the principal, and (for a
single project) the project.  A second declaration of the same thing is
idempotent; anything else raises ``25000`` and a new transaction is the
boundary.  ``close_project_partition`` deliberately does not clear the
declaration: giving up the reading is not permission to take up another
person's or another project's.

The guard is a *consistency* rule, not a second forgery defence.  A caller
that hand-clears the declaration setting still has to pass the membership
proof to obtain any scope at all, so clearing it buys exactly what today
already allows.  What it does buy — and what the seal on the declaration is
for — is that a *tampered* declaration fails closed rather than opening the
gate: an unverifiable declaration refuses every further declaration in that
transaction.

**Coverage.**  ``corridor_web`` can read 188 relations.  127 carry a
``project_id``; eleven of them were partitioned.  The remaining 116 answered
a direct-id lookup — ``select * from fact_decisions where id = 41`` — with
another customer project's row, which is the same hole #531 closed for
``source_segments`` and no smaller.  This block partitions the record and
decision families: the Proposed Delta lifecycle, Record Inclusion and the
Fact decisions and supports, the accepted revision and everything Adopt
Baseline registers, the Recorded Verbal origins, and the two legacy accepted
relations the product still reads.

It deliberately does **not** blanket every remaining relation, because some
of them are read where no person's partition exists and partitioning them
would break a working path this ticket may not edit.  Those are named,
with the reason, in ``corridor.access``; the classification is the
deliverable, and ``tests/test_architecture.py`` refuses a new relation that
is not in it.

**The view.**  ``current_project_record`` selects from ``facts`` and
``fact_decisions``.  It is owned by the schema owner, so row-level security
on those tables was evaluated as *the view's owner*, who bypasses it: the
view handed ``corridor_web`` every project's accepted record while the
tables under it refused.  ``security_invoker`` makes the view read as its
caller, which is the only setting under which a partitioned base table means
anything through a view.
"""

from __future__ import annotations

from corridor.migrations.source_append_commands.project_partition import (
    create_or_replace,
    OPEN_MEMBER_PROJECT_PARTITION,
    OPEN_PROJECT_PARTITION,
    _UNPARTITIONED_ROLES_SQL,
)
from corridor.migrations.source_append_commands.roles import (
    RUNTIME_LOGINS,
    SOURCE_APPEND_ROLE,
)


# Every relation that gains the partition here. Grouped as the families are
# reasoned about, then applied in one pass.
PARTITIONED_RECORD_TABLES = (
    # The Proposed Delta lifecycle (#518, #519, #526).
    "delta_groups",
    "delta_dispositions",
    "delta_deferrals",
    "delta_record_decisions",
    "delta_decision_supports",
    "delta_supersessions",
    "delta_follow_up_plans",
    "delta_follow_up_plan_evidence",
    "delta_review_packet_receipts",
    "delta_review_packet_children",
    "delta_review_packet_supports",
    "delta_review_packet_reversals",
    # Record Inclusion, the Fact decisions, and what supports them (#530).
    "fact_decisions",
    "fact_dispositions",
    "fact_sources",
    "fact_applies_to",
    "fact_closure_results",
    "fact_closure_sources",
    "fact_statement_timings",
    "extracted_proposal_facts",
    "record_inclusion_requests",
    "support_assessments",
    "support_assessment_sources",
    # The accepted record and everything Adopt Baseline registers (#509, #610).
    "project_record_revisions",
    "project_baseline_adoptions",
    "project_baseline_sources",
    "project_baseline_source_rows",
    "project_baseline_formats",
    "project_baseline_format_manifests",
    # The spine-native origin of a Recorded Verbal Statement (#512).
    "recorded_verbal_origins",
    "recorded_verbal_origin_statements",
    "recorded_verbal_origin_fact_digests",
    "recorded_verbal_origin_backfill_receipts",
    # Appended evidence and receipts that name one project's words.
    "evidence_link_sources",
    "source_fact_append_receipts",
    # The two legacy accepted relations the product still reads (ADR-0081).
    "candidates",
    "dependency_events",
)

_PARTITIONED_RECORD_TABLES_SQL = ", ".join(
    f"'{table}'" for table in PARTITIONED_RECORD_TABLES
)

PARTITIONED_RECORD_POLICIES = f"""
do $$
declare
    v_roles text;
    v_table text;
begin
    select string_agg(quote_ident(rolname), ', ' order by rolname)
      into v_roles
      from pg_roles
     where rolname in ({_UNPARTITIONED_ROLES_SQL});
    foreach v_table in array array[{_PARTITIONED_RECORD_TABLES_SQL}] loop
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

PARTITIONED_RECORD_POLICIES_DOWN = f"""
do $$
declare
    v_table text;
begin
    foreach v_table in array array[{_PARTITIONED_RECORD_TABLES_SQL}] loop
        execute format(
            'drop policy if exists %I on public.%I',
            'p_' || v_table || '_project_partition', v_table
        );
        execute format(
            'drop policy if exists %I on public.%I',
            'p_' || v_table || '_unpartitioned', v_table
        );
        execute format(
            'alter table public.%I disable row level security', v_table
        );
    end loop;
end $$;
"""

# A view owned by the schema owner evaluates row-level security as its owner,
# and the schema owner bypasses it. Without this the accepted record was
# readable across every project through the view while the tables under it
# refused — the partition was real and the projection through it was not.
CURRENT_RECORD_VIEW_SECURITY_INVOKER = """
alter view public.current_project_record set (security_invoker = true);
"""

CURRENT_RECORD_VIEW_SECURITY_INVOKER_DOWN = """
alter view public.current_project_record reset (security_invoker);
"""

# The declared scope, sealed exactly as the effective scope is. Only the two
# proving commands call this, and like `seal_project_partition` it is never
# granted: a caller that could seal a declaration could declare any scope it
# liked to be the one this transaction already holds.
SEAL_PARTITION_DECLARATION = """
create function public.seal_partition_declaration(p_declaration text)
returns void
language plpgsql
security definer
as $$
declare
    v_secret text;
begin
    select secret into strict v_secret
      from public.project_partition_secrets
     where id = 1;
    perform set_config(
        'corridor.project_partition_declaration', p_declaration, true
    );
    perform set_config(
        'corridor.project_partition_declaration_seal',
        encode(
            sha256((v_secret || ':declaration:' || p_declaration)::bytea), 'hex'
        ),
        true
    );
end;
$$;
"""

# Null means this transaction has declared nothing yet. A custom setting reads
# null only until something in the session touches it and empty afterwards,
# because `set_config(..., true)` reverts to the empty default at transaction
# end — so both readings mean the same thing and a pooled connection starts
# every transaction with no declaration, which is exactly the boundary #662
# asks for.
CURRENT_PARTITION_DECLARATION = """
create function public.current_partition_declaration()
returns text
language plpgsql
stable
security definer
as $$
declare
    v_declaration text := nullif(
        current_setting('corridor.project_partition_declaration', true), ''
    );
    v_seal text := current_setting(
        'corridor.project_partition_declaration_seal', true
    );
    v_secret text;
begin
    if v_declaration is null then
        return null;
    end if;
    select secret into strict v_secret
      from public.project_partition_secrets
     where id = 1;
    if v_seal is distinct from
        encode(
            sha256((v_secret || ':declaration:' || v_declaration)::bytea), 'hex'
        )
    then
        raise exception
            'the project-authorization scope declared on this transaction is '
            'not one the database sealed'
            using errcode = '25000';
    end if;
    return v_declaration;
end;
$$;
"""

# The whole of #662, in one place so the two commands cannot drift apart.
REQUIRE_PARTITION_DECLARATION = """
create function public.require_partition_declaration(p_declaration text)
returns void
language plpgsql
security definer
as $$
declare
    v_declared text := public.current_partition_declaration();
begin
    if v_declared is null or v_declared = p_declaration then
        return;
    end if;
    raise exception
        'this transaction already declared the project-authorization scope '
        '%; declaring % needs a new transaction',
        v_declared, p_declaration
        using errcode = '25000';
end;
$$;
"""

# The guard runs *after* the membership proof on purpose. It then refuses only
# what would otherwise have succeeded, so a caller asking for a project it is
# not on keeps hearing the specific answer #531 gave it — and #654's promise
# that such a refusal leaves the caller holding its earlier scope is unchanged
# rather than reworded. Both refusals preserve that scope; they differ only in
# which sentence is true.
OPEN_PROJECT_PARTITION_657 = """
create or replace function public.open_project_partition(
    p_principal_subject text,
    p_project_id bigint
)
returns bigint
language plpgsql
security definer
as $$
begin
    if not exists (
        select 1
          from public.project_roster_entries
         where project_id = p_project_id
           and principal_subject = p_principal_subject
           and active
    ) then
        raise exception
            'principal % holds no active membership of project %',
            p_principal_subject, p_project_id
            using errcode = '42501';
    end if;
    perform public.require_partition_declaration(
        'project:' || p_principal_subject || ':' || p_project_id::text
    );
    perform public.seal_project_partition(p_project_id::text);
    perform public.seal_partition_declaration(
        'project:' || p_principal_subject || ':' || p_project_id::text
    );
    return p_project_id;
end;
$$;
"""

# The declaration names the kind and the principal and not the resolved id
# list, because the cross-project reading's identity is "this person's
# projects" and a roster that changes mid-transaction must not turn a repeat
# call into a refusal.
OPEN_MEMBER_PROJECT_PARTITION_657 = """
create or replace function public.open_member_project_partition(
    p_principal_subject text
)
returns bigint[]
language plpgsql
security definer
as $$
declare
    v_ids bigint[];
begin
    perform public.require_partition_declaration(
        'member:' || p_principal_subject
    );
    select coalesce(array_agg(project_id order by project_id), array[]::bigint[])
      into v_ids
      from public.project_roster_entries
     where principal_subject = p_principal_subject
       and active;
    perform public.seal_project_partition(array_to_string(v_ids, ','));
    perform public.seal_partition_declaration(
        'member:' || p_principal_subject
    );
    return v_ids;
end;
$$;
"""

PARTITION_DECLARATION_COMMANDS = {
    "seal_partition_declaration": "(text)",
    "current_partition_declaration": "()",
    "require_partition_declaration": "(text)",
}

# Only the reading command is granted. Sealing a declaration and asserting one
# are the halves of the mechanism the proving commands call, exactly as
# `seal_project_partition` is.
PARTITION_DECLARATION_COMMANDS_GRANTED = ("current_partition_declaration",)

# The pre-#657 bodies, restored by `create or replace` so the owner and the
# grants #531 set survive the downgrade untouched.
OPEN_PROJECT_PARTITION_657_DOWN = create_or_replace(OPEN_PROJECT_PARTITION)
OPEN_MEMBER_PROJECT_PARTITION_657_DOWN = create_or_replace(
    OPEN_MEMBER_PROJECT_PARTITION
)


def upgrade(op) -> None:
    # Last, because it replaces two commands #531 creates, adds row-level
    # security to relations every block above builds, and re-declares a view
    # over two of them.
    for body in (
        SEAL_PARTITION_DECLARATION,
        CURRENT_PARTITION_DECLARATION,
        REQUIRE_PARTITION_DECLARATION,
    ):
        op.execute(body)
    for name, signature in PARTITION_DECLARATION_COMMANDS.items():
        # The same owner as the partition commands they belong to: the seal
        # secret is readable by that role and by nothing else.
        op.execute(
            f"alter function public.{name}{signature} owner to {SOURCE_APPEND_ROLE}"
        )
        # PostgreSQL grants EXECUTE to PUBLIC on every new function (#545), so
        # each command takes PUBLIC back before anything is granted at all.
        op.execute(f"revoke all on function public.{name}{signature} from public")
    for name in PARTITION_DECLARATION_COMMANDS_GRANTED:
        op.execute(
            f"grant execute on function public.{name}"
            f"{PARTITION_DECLARATION_COMMANDS[name]} to {RUNTIME_LOGINS}"
        )
    # `create or replace` keeps the owner and the grants #531 set, so the two
    # proving commands gain the guard and change nothing else about who may
    # call them.
    op.execute(OPEN_PROJECT_PARTITION_657)
    op.execute(OPEN_MEMBER_PROJECT_PARTITION_657)
    op.execute(PARTITIONED_RECORD_POLICIES)
    op.execute(CURRENT_RECORD_VIEW_SECURITY_INVOKER)


def downgrade(op) -> None:
    # First, because the upgrade added it last. Nothing is lost: the guard and
    # the policies hold no data of their own, and the two commands go back to
    # the bodies #531 gave them.
    op.execute(CURRENT_RECORD_VIEW_SECURITY_INVOKER_DOWN)
    op.execute(PARTITIONED_RECORD_POLICIES_DOWN)
    op.execute(OPEN_PROJECT_PARTITION_657_DOWN)
    op.execute(OPEN_MEMBER_PROJECT_PARTITION_657_DOWN)
    for name, signature in PARTITION_DECLARATION_COMMANDS.items():
        op.execute(f"drop function if exists public.{name}{signature}")
