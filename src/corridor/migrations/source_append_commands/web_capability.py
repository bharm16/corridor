"""#680 The live-pilot web capability boundary.

#657 classified all 191 relations `corridor_web` can read and left 130 of
them classified `NOT_YET_PARTITIONED` — project-scoped, unpolicied, and
still directly selectable. An inventory of holes is not a boundary: a
direct-id select as the real web login still answered
`select * from work_decisions where id = 41` with whatever project owned
row 41.

The rejected fix was one loop putting row-level security on all 127
`project_id` relations. It would refuse machine ingress, the customer-wide
registries, the partition's own authorization inputs, the frozen legacy
routes and every parentless child relation — and it would refuse them
*silently*, because every web test overrides the session with the schema
owner's, and the schema owner bypasses row-level security. The suite would
have stayed green while the product stopped working.

So this block draws the boundary where the product is instead.
`corridor.web_boundary` names the fourteen routes the live pilot serves and
the relations each of them was observed to reach. Four of those relations
were unpartitioned and are partitioned here; every other unpartitioned
relation is taken away from `corridor_web` entirely. A route that quietly
starts reading one now fails loudly rather than returning another
customer's rows.

**Why these four, and why `documents` only now.** `documents` and
`source_deliveries` are read by every project surface and by the source
register the adopted week shows. #657 recorded a real objection to
partitioning them: the transport-authenticated ingress paths read and write
them carrying no person's membership, so they declare no partition and a
policy would refuse a working ingress. That objection is removed rather
than overruled — `/intake/inbound` now takes the operations capability's
session, and `corridor_worker` holds the unpartitioned policy every one of
these blocks writes. `external_report_artifacts` and
`external_report_releases` are the issue trail `/record/{slug}` reads, and
nothing outside a project surface touches them.

**What is deliberately *not* revoked.** `audit_log`, `external_orgs` and
`extractor_configurations` stay: they are the whole customer database's, not
one project's, and #657's classification says so with its reasons. The six
authorization inputs stay for the reason partitioning them was never
possible — the partition is derived from them. Nothing here touches
`corridor_worker`, the three command-owner roles, or the opt-in
`corridor_legacy_dev` login: the boundary is the *human web capability's*,
and machine work keeps what it had.
"""

from __future__ import annotations

from corridor.migrations.source_append_commands.project_partition import (
    _UNPARTITIONED_ROLES_SQL,
)


# Four are read by an enabled pilot route. Two more are here for a different
# reason: #511 gives the *human* capability a designed authority over them —
# revoking a push credential is a person's act, and the checkpoint an advance
# reached is inserted through the same boundary — so denying them would take
# back an authority this ticket has no business taking. Both carry a
# `project_id`, so they get the partition instead and keep their grants.
WEB_PARTITIONED_TABLES = (
    "connector_checkpoint_advances",
    "documents",
    "external_report_artifacts",
    "external_report_releases",
    "push_intake_credentials",
    "source_deliveries",
)

_WEB_PARTITIONED_TABLES_SQL = ", ".join(
    f"'{table}'" for table in WEB_PARTITIONED_TABLES
)

WEB_PARTITION_POLICIES = f"""
do $$
declare
    v_roles text;
    v_table text;
begin
    select string_agg(quote_ident(rolname), ', ' order by rolname)
      into v_roles
      from pg_roles
     where rolname in ({_UNPARTITIONED_ROLES_SQL});
    foreach v_table in array array[{_WEB_PARTITIONED_TABLES_SQL}] loop
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

WEB_PARTITION_POLICIES_DOWN = f"""
do $$
declare
    v_table text;
begin
    foreach v_table in array array[{_WEB_PARTITIONED_TABLES_SQL}] loop
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

# The frozen legacy accepted relations and the subject-resolution decisions:
# `corridor_web` only ever held SELECT here, because #492 already took the
# writes away. The reading is what is left, and the pilot needs none of it.
WEB_DENIED_READ_ONLY = (
    "dependencies",
    "dispute_history_resolutions",
    "dispute_settlements",
    "operative_support",
    "subject_resolution_decisions",
    "work_decisions",
)

# Connector bookkeeping with no project column of its own: which deliveries one
# checkpoint advance covered. The human web role held the schema owner's
# default append here and no human surface has ever written it — the connector
# poller does, as `corridor_worker`. Its two parents are partitioned, it cannot
# be, and machine ingress is not the human capability's work, so this one is
# revoked rather than policed.
WEB_DENIED_APPEND = (
    "connector_checkpoint_advance_deliveries",
)

# Everything the schema owner's `ALTER DEFAULT PRIVILEGES` handed the web
# login on creation: SELECT, INSERT, UPDATE and DELETE, on tables no
# enabled pilot route reads. #531, #640, #529, #533 and #675 each had to
# take that default back one relation at a time; this takes it back for
# every relation the classification still calls unpartitioned.
WEB_DENIED_DEFAULT_PRIVILEGES = (
    "active_extraction_runs",
    "active_run_declarations",
    "assertions",
    "assignment_notification_attempts",
    "assignment_notification_dispatches",
    "assignment_notification_feedback",
    "assignment_notifications",
    "automatic_carry_forward_outcomes",
    "automatic_carry_forward_receipts",
    "candidate_dispositions",
    "cohort_receipts",
    "commitment_lineages",
    "condition_resolutions",
    "coordination_summary_configurations",
    "coordination_summary_requests",
    "dependency_admission_outcomes",
    "dependency_dismissals",
    "dependency_event_evidence",
    "dependency_event_migration_receipts",
    "dependency_event_scope_decisions",
    "dependency_event_scopes",
    "dependency_event_timings",
    "dependency_evidence_sufficiencies",
    "discovered_references",
    "doc_pages",
    "document_notification_attempts",
    "document_notification_dispatches",
    "document_notifications",
    "document_quarantines",
    "document_rendition_derivations",
    "documentation_field_confirmations",
    "due_action_notification_attempts",
    "due_action_notification_dispatches",
    "due_action_notifications",
    "due_work_occurrences",
    "due_work_receipts",
    "due_work_schedules",
    "event_admission_acceptance_receipts",
    "event_admission_outcomes",
    "event_cohort_receipts",
    "evidence_investigation_candidate_review_starts",
    "evidence_investigation_capture_contracts",
    "evidence_investigation_capture_results",
    "evidence_investigation_evaluation_receipts",
    "evidence_investigation_packet_receipts",
    "evidence_investigation_review_observations",
    "evidence_investigation_runs",
    "evidence_investigation_shadow_cases",
    "evidence_investigation_shadow_executions",
    "evidence_investigation_shadow_outcomes",
    "evidence_investigation_step_receipts",
    "evidence_links",
    "extraction_failure_diagnosis_configurations",
    "extraction_failure_diagnosis_requests",
    "extraction_measurement_case_states",
    "extraction_run_candidates",
    "extraction_runs",
    "follow_up_plan_receipts",
    "follow_up_plan_reversals",
    "inbound_messages",
    "inbound_route_triage",
    "inbound_thread_readings",
    "inbound_threads",
    "intake_project_identifiers",
    "key_date_draft_receipts",
    "key_date_draft_row_receipts",
    "legacy_ledger_archives",
    "milestone_registrations",
    "milestones",
    "organization_identity_receipts",
    "page_processing_failures",
    "page_render_derivatives",
    "policy_activations",
    "policy_approvals",
    "policy_runs",
    "processing_artifacts",
    "production_run_explanation_configurations",
    "production_run_explanation_requests",
    "project_check_configurations",
    "reconfirmation_receipts",
    "report_runs",
    "retention_holds",
    "retention_manifest_items",
    "retention_manifests",
    "retention_references",
    "retired_automatic_carry_forward_policy_activations",
    "retired_dependency_statuses",
    "revision_change_explanation_configurations",
    "revision_change_explanation_requests",
    "revision_comparison_findings",
    "revision_comparison_runs",
    "revision_reconciliation_requests",
    "schedule_governing_derivations",
    "schedule_link_receipts",
    "scheduled_report_publications",
    "source_fetch_attempts",
    "source_intake_draft_configurations",
    "source_intake_draft_requests",
    "stated_by_people",
    "statement_coordination_receipts",
    "statement_coordination_reversal_effects",
    "statement_coordination_reversals",
    "statement_suggestion_eligibility_declarations",
    "statement_suggestion_protection_ends",
    "statement_suggestion_protections",
    "subject_candidate_suggestions",
    "subject_resolution_attempts",
    "subject_resolution_candidates",
    "token_layers",
    "unreadable_cell_reading_profiles",
    "unreadable_cell_reading_runs",
    "unreadable_cell_reading_steps",
    "unreadable_cell_resolutions",
    "work_decision_milestone_impacts",
)

WEB_DENIED_RELATIONS = (
    WEB_DENIED_READ_ONLY + WEB_DENIED_APPEND + WEB_DENIED_DEFAULT_PRIVILEGES
)

_WEB_DENIED_SQL = ", ".join(f"'{table}'" for table in sorted(WEB_DENIED_RELATIONS))

# Three relations carried a grant of SELECT to PUBLIC, left by the command role
# that created them: `fact_decisions`, `project_record_revisions` and
# `subject_resolution_decisions`. A revoke aimed at one login does not touch a
# PUBLIC grant, so the first draft of this block appeared to work and left
# `subject_resolution_decisions` readable by `corridor_web` — and by every
# other role in the database, present and future. #680 answered that with a
# one-relation list here, which fixed the relation it named and left the rule
# unstated; the #693 block below states the rule instead and sweeps all three
# out of the catalog, so this block no longer names any of them.
# `corridor_legacy_dev` keeps its reading through the explicit
# `grant select on all tables` the baseline gives it, not through PUBLIC.

# `revoke all` rather than `revoke select`: a capability that keeps INSERT on a
# relation it may not read is still a capability on that relation, and a
# privilege-by-privilege revoke leaves behind exactly the column grants nobody
# thinks to name. The sequences go with their
# tables — an owned sequence the web login can still read and advance is a row
# count and a write path that outlived the table it belongs to.
WEB_CAPABILITY_REVOKE = f"""
do $$
declare
    v_table text;
    v_sequence text;
begin
    foreach v_table in array array[{_WEB_DENIED_SQL}] loop
        execute format('revoke all on public.%I from corridor_web', v_table);
    end loop;
    for v_sequence in
        select s.relname
          from pg_class s
          join pg_depend d
            on d.objid = s.oid and d.classid = 'pg_class'::regclass
          join pg_class t on t.oid = d.refobjid
          join pg_namespace n on n.oid = t.relnamespace
         where s.relkind = 'S'
           and n.nspname = 'public'
           and t.relname in ({_WEB_DENIED_SQL})
    loop
        execute format(
            'revoke all on sequence public.%I from corridor_web', v_sequence
        );
    end loop;
end $$;
"""

# The downgrade hands back exactly what each relation held, not a blanket
# grant: six of them only ever carried SELECT, and re-granting INSERT there
# would use the downgrade to widen a capability #492 had already narrowed.
WEB_CAPABILITY_RESTORE = f"""
do $$
declare
    v_table text;
    v_sequence text;
begin
    foreach v_table in array array[{", ".join(f"'{t}'" for t in WEB_DENIED_READ_ONLY)}] loop
        execute format('grant select on public.%I to corridor_web', v_table);
    end loop;
    foreach v_table in array array[{", ".join(f"'{t}'" for t in WEB_DENIED_APPEND)}] loop
        execute format(
            'grant select, insert on public.%I to corridor_web', v_table
        );
    end loop;
    foreach v_table in array array[{", ".join(f"'{t}'" for t in WEB_DENIED_DEFAULT_PRIVILEGES)}] loop
        execute format(
            'grant select, insert, update, delete on public.%I to corridor_web',
            v_table
        );
    end loop;
    for v_sequence in
        select s.relname
          from pg_class s
          join pg_depend d
            on d.objid = s.oid and d.classid = 'pg_class'::regclass
          join pg_class t on t.oid = d.refobjid
          join pg_namespace n on n.oid = t.relnamespace
         where s.relkind = 'S'
           and n.nspname = 'public'
           and t.relname in ({_WEB_DENIED_SQL})
    loop
        execute format(
            'grant select, usage on sequence public.%I to corridor_web',
            v_sequence
        );
    end loop;
end $$;
"""


def upgrade(op) -> None:
    # Last, because it partitions relations blocks above create and revokes
    # privileges on every relation any of them left with the schema owner's
    # default grant. The policies come before the revoke so a relation that
    # is both partitioned and denied would be a contradiction the next
    # statement raises rather than a silent state.
    op.execute(WEB_PARTITION_POLICIES)
    op.execute(WEB_CAPABILITY_REVOKE)


def downgrade(op) -> None:
    # First, because the upgrade added it last. The restore hands back the
    # exact privileges each relation held rather than a blanket grant, and
    # the policies come off after it so no window exists where a relation is
    # readable again and still partitioned against a partition no caller
    # declared.
    op.execute(WEB_CAPABILITY_RESTORE)
    op.execute(WEB_PARTITION_POLICIES_DOWN)
