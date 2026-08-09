"""join the policy families' approval and run tables

Revision ID: f2c8d94ab371
Revises: e9a4b7c2d158
Create Date: 2026-08-09 00:50:00.000000

ADR-0028. The approval and run shapes were identical across the three
policy families, and the copies had already drifted: only Carry-Forward's
runs and outcomes carried the deferred counts-reconciliation triggers.
One approvals table and one runs table now name their family; each
family keeps its own outcome table, whose run reference carries the
family in a composite key so a cross-family pointer is a constraint
violation. The counts check becomes shared, dispatching on family.

Data moved: one Carry-Forward approval and one run (the admission tables
were empty). The id remapping is recorded by the migration itself in the
moved rows' order.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "f2c8d94ab371"
down_revision: Union[str, Sequence[str], None] = "e9a4b7c2d158"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

FAMILY_CHECK = (
    "family in ('automatic-carry-forward', 'event-admission', "
    "'dependency-admission')"
)

ACF = "automatic-carry-forward"


def upgrade() -> None:
    op.create_table(
        "policy_approvals",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey("projects.id"),
            nullable=False,
        ),
        sa.Column("family", sa.String(length=32), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("approved_by", sa.Text(), nullable=False),
        sa.Column("policy_json", JSONB(), nullable=False),
        sa.Column("policy_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "approved_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "project_id", "id", name="uq_policy_approvals_project_id"
        ),
        sa.UniqueConstraint("family", "id", name="uq_policy_approvals_family_id"),
        sa.UniqueConstraint(
            "project_id",
            "family",
            "id",
            name="uq_policy_approvals_project_family_id",
        ),
        sa.CheckConstraint(FAMILY_CHECK, name="ck_policy_approvals_family"),
        sa.CheckConstraint(
            "jsonb_typeof(policy_json) = 'object'",
            name="ck_policy_approvals_object",
        ),
        sa.CheckConstraint(
            "policy_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_policy_approvals_sha256",
        ),
    )
    op.create_table(
        "policy_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "project_id",
            sa.BigInteger(),
            sa.ForeignKey("projects.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("family", sa.String(length=32), nullable=False),
        sa.Column("policy_approval_id", sa.BigInteger(), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("policy_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "abstention_reason_version", sa.String(length=64), nullable=False
        ),
        sa.Column("applied_count", sa.Integer(), nullable=False),
        sa.Column("abstained_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("family", "id", name="uq_policy_runs_family_id"),
        sa.UniqueConstraint(
            "project_id", "family", "id", name="uq_policy_runs_project_family_id"
        ),
        sa.CheckConstraint(FAMILY_CHECK, name="ck_policy_runs_family"),
        sa.CheckConstraint(
            "policy_sha256 ~ '^[0-9a-f]{64}$'", name="ck_policy_runs_sha256"
        ),
        sa.CheckConstraint(
            "applied_count >= 0 and abstained_count >= 0",
            name="ck_policy_runs_counts",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "family", "policy_approval_id"],
            [
                "policy_approvals.project_id",
                "policy_approvals.family",
                "policy_approvals.id",
            ],
            name="fk_policy_runs_approval_project_family",
        ),
    )

    # Immutability, one function per shared table.
    for table in ("policy_approvals", "policy_runs"):
        op.execute(
            f"""
            create function enforce_{table}()
            returns trigger
            language plpgsql
            as $$
            begin
                if tg_op = 'INSERT' then
                    return new;
                end if;
                raise exception '{table} are immutable'
                    using errcode = '23514';
            end;
            $$;

            create trigger {table}_are_immutable
            before update or delete
            on {table}
            for each row execute function enforce_{table}();

            create trigger {table}_reject_truncate
            before truncate on {table}
            for each statement execute function enforce_{table}();
            """
        )

    # Drop the old ACF triggers so the two rows can move, and the old
    # counts machinery with them.
    op.execute(
        """
        drop trigger automatic_carry_forward_runs_must_match_outcomes
            on automatic_carry_forward_runs;
        drop trigger automatic_carry_forward_outcomes_must_match_runs
            on automatic_carry_forward_outcomes;
        drop function require_automatic_carry_forward_run_counts();
        drop function require_automatic_carry_forward_outcome_counts();
        drop function verify_automatic_carry_forward_run_counts(bigint);
        """
    )

    # Move the Carry-Forward rows, recording the id remapping.
    op.execute(
        f"""
        create temporary table _approval_map on commit drop as
        select id as old_id,
               nextval('policy_approvals_id_seq') as new_id
        from automatic_carry_forward_policy_approvals order by id;

        insert into policy_approvals
            (id, project_id, family, policy_version, approved_by,
             policy_json, policy_sha256, approved_at)
        select m.new_id, a.project_id, '{ACF}', a.policy_version,
               a.approved_by, a.policy_json, a.policy_sha256, a.approved_at
        from automatic_carry_forward_policy_approvals a
        join _approval_map m on m.old_id = a.id
        order by a.id;

        create temporary table _run_map on commit drop as
        select id as old_id,
               nextval('policy_runs_id_seq') as new_id
        from automatic_carry_forward_runs order by id;

        insert into policy_runs
            (id, project_id, family, policy_approval_id, policy_version,
             policy_sha256, abstention_reason_version, applied_count,
             abstained_count, created_at)
        select rm.new_id, r.project_id, '{ACF}', am.new_id,
               r.policy_version, r.policy_sha256,
               r.abstention_reason_version, r.carried_count,
               r.abstained_count, r.created_at
        from automatic_carry_forward_runs r
        join _run_map rm on rm.old_id = r.id
        join _approval_map am on am.old_id = r.policy_approval_id
        order by r.id;
        """
    )

    # The pointer follows its approval.
    op.execute(
        f"""
        alter table active_automatic_carry_forward_policies
            drop constraint fk_active_automatic_carry_forward_policy_project;
        alter table active_automatic_carry_forward_policies
            add column family varchar(32) not null default '{ACF}';
        alter table active_automatic_carry_forward_policies
            add constraint ck_active_automatic_carry_forward_policy_family
            check (family = '{ACF}');
        update active_automatic_carry_forward_policies p
            set policy_approval_id = m.new_id
            from _approval_map m where m.old_id = p.policy_approval_id;
        alter table active_automatic_carry_forward_policies
            add constraint fk_active_automatic_carry_forward_policy_project
            foreign key (project_id, family, policy_approval_id)
            references policy_approvals (project_id, family, id);
        """
    )

    # Receipts and outcomes follow, family pinned in their keys. Both
    # tables are immutable by trigger; the remap updates would refuse, so
    # the triggers lift for exactly this statement window. Both hold zero
    # rows today; the updates are for correctness if that ever changes.
    op.execute(
        f"""
        alter table automatic_carry_forward_receipts
            disable trigger automatic_carry_forward_receipts_are_immutable;
        alter table automatic_carry_forward_outcomes
            disable trigger automatic_carry_forward_outcomes_are_immutable;

        alter table automatic_carry_forward_receipts
            drop constraint fk_automatic_carry_forward_receipt_policy_project;
        alter table automatic_carry_forward_receipts
            add column family varchar(32) not null default '{ACF}';
        alter table automatic_carry_forward_receipts
            add constraint ck_automatic_carry_forward_receipt_family
            check (family = '{ACF}');
        update automatic_carry_forward_receipts r
            set policy_approval_id = m.new_id
            from _approval_map m where m.old_id = r.policy_approval_id;
        alter table automatic_carry_forward_receipts
            add constraint fk_automatic_carry_forward_receipt_policy_project
            foreign key (project_id, family, policy_approval_id)
            references policy_approvals (project_id, family, id);

        alter table automatic_carry_forward_outcomes
            drop constraint fk_automatic_carry_forward_outcome_run_project;
        alter table automatic_carry_forward_outcomes
            drop constraint fk_automatic_carry_forward_outcome_policy_project;
        alter table automatic_carry_forward_outcomes
            add column family varchar(32) not null default '{ACF}';
        alter table automatic_carry_forward_outcomes
            add constraint ck_automatic_carry_forward_outcome_family
            check (family = '{ACF}');
        update automatic_carry_forward_outcomes o
            set run_id = m.new_id
            from _run_map m where m.old_id = o.run_id;
        update automatic_carry_forward_outcomes o
            set policy_approval_id = m.new_id
            from _approval_map m where m.old_id = o.policy_approval_id;
        alter table automatic_carry_forward_outcomes
            add constraint fk_automatic_carry_forward_outcome_run_project
            foreign key (project_id, family, run_id)
            references policy_runs (project_id, family, id);
        alter table automatic_carry_forward_outcomes
            add constraint fk_automatic_carry_forward_outcome_policy_project
            foreign key (project_id, family, policy_approval_id)
            references policy_approvals (project_id, family, id);

        alter table automatic_carry_forward_receipts
            enable trigger automatic_carry_forward_receipts_are_immutable;
        alter table automatic_carry_forward_outcomes
            enable trigger automatic_carry_forward_outcomes_are_immutable;
        """
    )

    # The admission outcome tables (both empty) point at the shared runs.
    for table, family, old_column in (
        ("event_admission_outcomes", "event-admission", "event_admission_run_id"),
        (
            "dependency_admission_outcomes",
            "dependency-admission",
            "dependency_admission_run_id",
        ),
    ):
        op.execute(
            f"""
            alter table {table}
                disable trigger {table}_are_immutable;
            alter table {table}
                drop constraint {table}_{old_column}_fkey;
            alter table {table} rename column {old_column} to policy_run_id;
            alter table {table}
                add column family varchar(32) not null default '{family}';
            alter table {table}
                add constraint ck_{table}_family
                check (family = '{family}');
            alter table {table}
                add constraint fk_{table}_run_family
                foreign key (family, policy_run_id)
                references policy_runs (family, id);
            alter table {table}
                enable trigger {table}_are_immutable;
            """
        )

    # Every family now carries the counts reconciliation Carry-Forward
    # alone used to have, dispatched on family and deferred to commit.
    op.execute(
        """
        create function verify_policy_run_counts(target_run_id bigint)
        returns void
        language plpgsql
        as $$
        declare
            run_family text;
            expected_applied integer;
            expected_abstained integer;
            actual_applied integer;
            actual_abstained integer;
        begin
            select family, applied_count, abstained_count
              into run_family, expected_applied, expected_abstained
              from policy_runs
             where id = target_run_id;
            if run_family is null then
                raise exception
                    'policy runs must reconcile their recorded outcomes'
                    using errcode = '23514';
            end if;
            if run_family = 'automatic-carry-forward' then
                select
                    count(*) filter (where outcome = 'carried'),
                    count(*) filter (where outcome = 'abstained')
                  into actual_applied, actual_abstained
                  from automatic_carry_forward_outcomes
                 where run_id = target_run_id;
            elsif run_family = 'event-admission' then
                select
                    count(*) filter (where outcome = 'admitted'),
                    count(*) filter (where outcome = 'abstained')
                  into actual_applied, actual_abstained
                  from event_admission_outcomes
                 where policy_run_id = target_run_id;
            else
                select
                    count(*) filter (where outcome = 'admitted'),
                    count(*) filter (where outcome = 'abstained')
                  into actual_applied, actual_abstained
                  from dependency_admission_outcomes
                 where policy_run_id = target_run_id;
            end if;
            if actual_applied <> expected_applied
               or actual_abstained <> expected_abstained then
                raise exception
                    'policy runs must reconcile their recorded outcomes'
                    using errcode = '23514';
            end if;
            return;
        end;
        $$;

        create function require_policy_run_counts()
        returns trigger
        language plpgsql
        as $$
        begin
            perform verify_policy_run_counts(new.id);
            return null;
        end;
        $$;

        create function require_policy_outcome_counts_by_run_id()
        returns trigger
        language plpgsql
        as $$
        begin
            perform verify_policy_run_counts(new.run_id);
            return null;
        end;
        $$;

        create function require_policy_outcome_counts_by_policy_run_id()
        returns trigger
        language plpgsql
        as $$
        begin
            perform verify_policy_run_counts(new.policy_run_id);
            return null;
        end;
        $$;

        create constraint trigger policy_runs_must_match_outcomes
        after insert on policy_runs
        deferrable initially deferred
        for each row execute function require_policy_run_counts();

        create constraint trigger automatic_carry_forward_outcomes_must_match_runs
        after insert on automatic_carry_forward_outcomes
        deferrable initially deferred
        for each row execute function require_policy_outcome_counts_by_run_id();

        create constraint trigger event_admission_outcomes_must_match_runs
        after insert on event_admission_outcomes
        deferrable initially deferred
        for each row
        execute function require_policy_outcome_counts_by_policy_run_id();

        create constraint trigger dependency_admission_outcomes_must_match_runs
        after insert on dependency_admission_outcomes
        deferrable initially deferred
        for each row
        execute function require_policy_outcome_counts_by_policy_run_id();
        """
    )

    # The receipt and outcome validation functions embed joins against
    # the old tables; they are recreated against the shared ones with
    # the family pinned, before those tables drop.
    op.execute(
        """
CREATE OR REPLACE FUNCTION public.enforce_automatic_carry_forward_receipt()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
        declare
            valid_binding boolean;
        begin
            if tg_op = 'INSERT' then
                begin
                    select exists (
                        select 1
                          from audit_log act
                          join dependencies dependency
                            on dependency.id = new.dependency_id
                          join policy_approvals policy
                            on policy.id = new.policy_approval_id
                           and policy.project_id = new.project_id
                          join revision_comparison_runs comparison
                            on comparison.id = new.comparison_id
                           and comparison.project_id = new.project_id
                          join revision_comparison_findings finding
                            on finding.id = new.finding_id
                           and finding.revision_comparison_run_id = comparison.id
                          join candidates predecessor
                            on predecessor.id = new.predecessor_candidate_id
                           and predecessor.project_id = new.project_id
                          join candidates successor
                            on successor.id = new.successor_candidate_id
                           and successor.project_id = new.project_id
                          join evidence_links evidence
                            on evidence.id = new.new_evidence_link_id
                           and evidence.dependency_id = new.dependency_id
                          join audit_log admission
                            on admission.id = new.origin_admission_audit_id
                          left join audit_log prior_transfer
                            on prior_transfer.id =
                               new.predecessor_support_transfer_audit_id
                         where act.id = new.audit_log_id
                           and dependency.project_id = new.project_id
                           and act.action = 'automatic_carry_forward'
                           and act.entity_type = 'dependency'
                           and act.entity_id = new.dependency_id
                           and act.actor = 'corridor:automatic-carry-forward'
                           and act.human_principal is null
                           and act.before_json = new.before_json
                           and act.after_json = new.after_json
                           and exists (
                               select 1
                                 from audit_log auth_entry
                                where auth_entry.entity_type = 'project'
                                  and auth_entry.entity_id = new.project_id
                                  and auth_entry.action =
                                      'authorize_automatic_carry_forward'
                                  and auth_entry.actor = policy.approved_by
                                  and auth_entry.human_principal =
                                      policy.approved_by
                                  and auth_entry.after_json =
                                      jsonb_build_object(
                                          'policy_approval_id', policy.id,
                                          'policy_version',
                                              policy.policy_version,
                                          'policy_sha256',
                                              policy.policy_sha256
                                      )
                           )
                           and comparison.predecessor_document_id =
                               predecessor.source_document_id
                           and comparison.successor_document_id =
                               successor.source_document_id
                           and comparison.predecessor_extraction_run_id =
                               predecessor.extraction_run_id
                           and comparison.successor_extraction_run_id =
                               successor.extraction_run_id
                           and new.predecessor_candidate_id = any(
                               finding.predecessor_candidate_ids
                           )
                           and new.successor_candidate_id = any(
                               finding.successor_candidate_ids
                           )
                           and finding.state = 'unchanged'
                           and finding.predecessor_candidate_ids =
                               array[new.predecessor_candidate_id]::bigint[]
                           and finding.successor_candidate_ids =
                               array[new.successor_candidate_id]::bigint[]
                           and evidence.document_id = successor.source_document_id
                           and evidence.verified
                           and successor.citations_verified
                           and jsonb_typeof(
                               successor.payload_json -> 'citations'
                           ) = 'array'
                           and jsonb_array_length(
                               successor.payload_json -> 'citations'
                           ) = 1
                           and jsonb_typeof(
                               successor.payload_json -> 'citations' -> 0
                           ) = 'object'
                           and (
                               successor.payload_json -> 'citations' -> 0 ->
                                   'verified'
                           ) = 'true'::jsonb
                           and jsonb_typeof(
                               successor.payload_json -> 'citations' -> 0 ->
                                   'document_id'
                           ) = 'number'
                           and (
                               successor.payload_json -> 'citations' -> 0 ->>
                                   'document_id'
                           )::bigint = successor.source_document_id
                           and jsonb_typeof(
                               successor.payload_json -> 'citations' -> 0 ->
                                   'page'
                           ) = 'number'
                           and (
                               successor.payload_json -> 'citations' -> 0 ->>
                                   'page'
                           )::integer = evidence.page_no
                           and evidence.page_no > 0
                           and nullif(
                               successor.payload_json -> 'citations' -> 0 ->>
                                   'quote',
                               ''
                           ) is not null
                           and evidence.quote = (
                               successor.payload_json -> 'citations' -> 0 ->>
                                   'quote'
                           )
                           and 1 = (
                               select count(*)
                                 from jsonb_array_elements(
                                     comparison.successor_inputs_json
                                 ) as exact_input(value)
                                where jsonb_typeof(
                                    exact_input.value -> 'candidate_id'
                                ) = 'number'
                                  and (
                                      exact_input.value ->> 'candidate_id'
                                  )::bigint = new.successor_candidate_id
                           )
                           and exists (
                               select 1
                                 from jsonb_array_elements(
                                     comparison.successor_inputs_json
                                 ) as exact_input(value)
                                where (
                                    exact_input.value ->> 'candidate_id'
                                )::bigint = new.successor_candidate_id
                                  and (
                                      exact_input.value -> 'citations_verified'
                                  ) = 'true'::jsonb
                                  and jsonb_typeof(
                                      exact_input.value -> 'payload_json' ->
                                          'citations'
                                  ) = 'array'
                                  and jsonb_array_length(
                                      exact_input.value -> 'payload_json' ->
                                          'citations'
                                  ) = 1
                                  and (
                                      exact_input.value -> 'payload_json' ->
                                          'citations' -> 0 -> 'verified'
                                  ) = 'true'::jsonb
                                  and (
                                      exact_input.value -> 'payload_json' ->
                                          'citations' -> 0 ->> 'document_id'
                                  )::bigint = evidence.document_id
                                  and (
                                      exact_input.value -> 'payload_json' ->
                                          'citations' -> 0 ->> 'page'
                                  )::integer = evidence.page_no
                                  and (
                                      exact_input.value -> 'payload_json' ->
                                          'citations' -> 0 ->> 'quote'
                                  ) = evidence.quote
                           )
                           and admission.entity_type = 'dependency'
                           and admission.entity_id = new.dependency_id
                           and admission.action in (
                               'accept_candidate', 'merge_candidate'
                           )
                           and admission.human_principal is not null
                           and admission.actor = admission.human_principal
                           and admission.id < act.id
                           and jsonb_typeof(
                               admission.after_json -> 'candidate_id'
                           ) = 'number'
                           and (
                               (
                                   new.predecessor_support_transfer_audit_id
                                       is null
                                   and (
                                       admission.after_json ->> 'candidate_id'
                                   )::bigint = new.predecessor_candidate_id
                               )
                               or (
                                   new.predecessor_support_transfer_audit_id
                                       is not null
                                   and prior_transfer.entity_type = 'dependency'
                                   and prior_transfer.entity_id = new.dependency_id
                                   and prior_transfer.action in (
                                       'reconfirm_operative_support',
                                       'automatic_carry_forward'
                                   )
                                   and admission.id < prior_transfer.id
                                   and prior_transfer.id < act.id
                                   and jsonb_typeof(
                                       prior_transfer.after_json ->
                                           'successor_candidate_id'
                                   ) = 'number'
                                   and (
                                       prior_transfer.after_json ->>
                                           'successor_candidate_id'
                                   )::bigint = new.predecessor_candidate_id
                                   and jsonb_typeof(
                                       prior_transfer.after_json ->
                                           'origin_admission_audit_id'
                                   ) = 'number'
                                   and (
                                       prior_transfer.after_json ->>
                                           'origin_admission_audit_id'
                                   )::bigint = new.origin_admission_audit_id
                                   and (
                                       (
                                           prior_transfer.action =
                                               'reconfirm_operative_support'
                                           and exists (
                                               select 1
                                                 from reconfirmation_receipts prior
                                                where prior.audit_log_id =
                                                    prior_transfer.id
                                                  and prior.dependency_id =
                                                    new.dependency_id
                                                  and prior.successor_candidate_id =
                                                    new.predecessor_candidate_id
                                                  and prior.before_json =
                                                    prior_transfer.before_json
                                                  and prior.after_json =
                                                    prior_transfer.after_json
                                           )
                                       )
                                       or (
                                           prior_transfer.action =
                                               'automatic_carry_forward'
                                           and exists (
                                               select 1
                                                 from automatic_carry_forward_receipts prior
                                                where prior.audit_log_id =
                                                    prior_transfer.id
                                                  and prior.project_id = new.project_id
                                                  and prior.dependency_id =
                                                    new.dependency_id
                                                  and prior.successor_candidate_id =
                                                    new.predecessor_candidate_id
                                                  and prior.origin_admission_audit_id =
                                                    new.origin_admission_audit_id
                                                  and prior.before_json =
                                                    prior_transfer.before_json
                                                  and prior.after_json =
                                                    prior_transfer.after_json
                                           )
                                       )
                                   )
                               )
                           )
                           and jsonb_typeof(
                               act.after_json -> 'policy_approval_id'
                           ) = 'number'
                           and (act.after_json ->> 'policy_approval_id')::bigint =
                               new.policy_approval_id
                           and act.after_json ->> 'policy_sha256' =
                               policy.policy_sha256
                           and jsonb_typeof(
                               act.after_json -> 'comparison_id'
                           ) = 'number'
                           and (act.after_json ->> 'comparison_id')::bigint =
                               new.comparison_id
                           and jsonb_typeof(act.after_json -> 'finding_id') =
                               'number'
                           and (act.after_json ->> 'finding_id')::bigint =
                               new.finding_id
                           and jsonb_typeof(
                               act.after_json -> 'predecessor_candidate_id'
                           ) = 'number'
                           and (act.after_json ->>
                                'predecessor_candidate_id')::bigint =
                               new.predecessor_candidate_id
                           and jsonb_typeof(
                               act.after_json -> 'successor_candidate_id'
                           ) = 'number'
                           and (act.after_json ->>
                                'successor_candidate_id')::bigint =
                               new.successor_candidate_id
                           and jsonb_typeof(
                               act.after_json -> 'new_evidence_link_id'
                           ) = 'number'
                           and (act.after_json ->>
                                'new_evidence_link_id')::bigint =
                               new.new_evidence_link_id
                           and jsonb_typeof(
                               act.after_json -> 'origin_admission_audit_id'
                           ) = 'number'
                           and (act.after_json ->>
                                'origin_admission_audit_id')::bigint =
                               new.origin_admission_audit_id
                           and (
                               (
                                   new.predecessor_support_transfer_audit_id
                                   is null
                                   and act.after_json ?
                                       'predecessor_support_transfer_audit_id'
                                   and act.after_json ->
                                       'predecessor_support_transfer_audit_id'
                                       = 'null'::jsonb
                               )
                               or (
                                   jsonb_typeof(
                                       act.after_json ->
                                       'predecessor_support_transfer_audit_id'
                                   ) = 'number'
                                   and (act.after_json ->>
                                        'predecessor_support_transfer_audit_id'
                                       )::bigint =
                                       new.predecessor_support_transfer_audit_id
                               )
                           )
                    ) into valid_binding;
                exception
                    when invalid_text_representation
                        or numeric_value_out_of_range then
                        valid_binding := false;
                end;
                if not valid_binding then
                    raise exception 'Automatic Carry-Forward receipt binding is invalid'
                        using errcode = '23514';
                end if;
                return new;
            end if;

            raise exception 'Automatic Carry-Forward receipts are append-only'
                using errcode = '23514';
        end;
        $function$

;

CREATE OR REPLACE FUNCTION public.enforce_automatic_carry_forward_outcome()
 RETURNS trigger
 LANGUAGE plpgsql
AS $function$
        declare
            valid_binding boolean;
        begin
            if tg_op = 'INSERT' then
                begin
                    select exists (
                        select 1
                          from policy_runs run
                          join policy_approvals policy
                            on policy.project_id = new.project_id
                           and policy.id = new.policy_approval_id
                           and policy.family = 'automatic-carry-forward'
                          join dependencies dependency
                            on dependency.id = new.dependency_id
                          left join revision_comparison_runs comparison
                            on comparison.id = new.comparison_id
                          left join revision_comparison_findings finding
                            on finding.id = new.finding_id
                          left join candidates predecessor
                            on predecessor.id = new.predecessor_candidate_id
                          left join candidates successor
                            on successor.id = new.successor_candidate_id
                          left join automatic_carry_forward_receipts receipt
                            on receipt.audit_log_id = new.receipt_audit_log_id
                         where run.id = new.run_id
                           and run.family = 'automatic-carry-forward'
                           and run.project_id = new.project_id
                           and run.policy_approval_id = new.policy_approval_id
                           and dependency.project_id = new.project_id
                           and (
                               new.comparison_id is null
                               or comparison.project_id = new.project_id
                           )
                           and (
                               new.finding_id is null
                               or (
                                   new.comparison_id is not null
                                   and finding.revision_comparison_run_id =
                                       new.comparison_id
                               )
                           )
                           and (
                               new.predecessor_candidate_id is null
                               or predecessor.project_id = new.project_id
                           )
                           and (
                               new.successor_candidate_id is null
                               or successor.project_id = new.project_id
                           )
                           and (
                               (
                                   new.outcome = 'carried'
                                   and new.receipt_audit_log_id is not null
                                   and receipt.project_id = new.project_id
                                   and receipt.policy_approval_id =
                                       new.policy_approval_id
                                   and receipt.dependency_id = new.dependency_id
                                   and receipt.comparison_id is not distinct from
                                       new.comparison_id
                                   and receipt.finding_id is not distinct from
                                       new.finding_id
                                   and receipt.predecessor_candidate_id is not distinct from
                                       new.predecessor_candidate_id
                                   and receipt.successor_candidate_id is not distinct from
                                       new.successor_candidate_id
                               )
                               or (
                                   new.outcome = 'abstained'
                                   and new.receipt_audit_log_id is null
                                   and new.reason = any (
                                       array[
                                           'comparison_policy_unapproved',
                                           'review_stale',
                                           'dependency_unavailable',
                                           'comparison_not_one_to_one',
                                           'comparison_integrity_failure',
                                           'predecessor_finding_unavailable',
                                           'comparison_changed',
                                           'comparison_dropped',
                                           'comparison_ambiguous',
                                           'comparison_unmatched',
                                           'comparison_input_unavailable',
                                           'successor_fields_not_exact',
                                           'successor_not_actionable',
                                           'successor_candidate_changed',
                                           'successor_provenance_unsafe',
                                           'unsupported_operative_support_role',
                                           'readiness_source_changed',
                                           'readiness_history_untrusted',
                                           'supersession_registry_unavailable',
                                           'multi_hop_supersession',
                                           'successor_active_run_invalid',
                                           'predecessor_active_run_unavailable',
                                           'multiple_exact_comparisons',
                                           'admission_scope_identity_mismatch',
                                           'partial_scope_transfer',
                                           'support_scope_changed',
                                           'predecessor_support_provenance_unsafe',
                                           'admission_run_mismatch',
                                           'admission_fields_changed',
                                           'admission_history_corrupt',
                                           'admission_link_unavailable',
                                           'admission_link_ambiguous',
                                           'admission_document_mismatch',
                                           'admission_project_mismatch',
                                           'admission_not_attributable',
                                           'admission_state_inconsistent',
                                           'reconfirmation_history_corrupt',
                                           'reconfirmation_lineage_cycle',
                                           'reconfirmation_lineage_chronology_invalid',
                                           'reconfirmation_identity_mismatch',
                                           'reconfirmation_lineage_mismatch',
                                           'reconfirmation_lineage_changed',
                                           'reconfirmation_candidate_changed',
                                           'reconfirmation_provenance_unsafe',
                                           'reconfirmation_evidence_mismatch',
                                           'successor_candidate_already_reconfirmed',
                                           'successor_candidate_link_ambiguous',
                                           'awaiting_extraction',
                                           'extraction_failed',
                                           'awaiting_active_run',
                                           'awaiting_comparison',
                                           'comparison_selection_ambiguous',
                                           'comparison_ready',
                                           'blocked',
                                           'unclassified_unsafe'
                                       ]::text[]
                                   )
                                   and new.reason_version =
                                       'automatic-carry-forward-abstentions-v1'
                               )
                           )
                    ) into valid_binding;
                exception
                    when invalid_text_representation
                        or numeric_value_out_of_range then
                        valid_binding := false;
                end;
                if not valid_binding then
                    raise exception 'Automatic Carry-Forward outcome binding is invalid'
                        using errcode = '23514';
                end if;
                return new;
            end if;

            raise exception 'Automatic Carry-Forward outcomes are append-only'
                using errcode = '23514';
        end;
        $function$

;

        """
    )

    # The six replaced tables and their guard functions.
    op.execute(
        """
        drop table automatic_carry_forward_runs;
        drop table automatic_carry_forward_policy_approvals;
        drop table event_admission_runs;
        drop table event_admission_policy_approvals;
        drop table dependency_admission_runs;
        drop table dependency_admission_policy_approvals;
        drop function enforce_automatic_carry_forward_policy_approval();
        drop function enforce_automatic_carry_forward_run();
        drop function enforce_event_admission_policy_approvals();
        drop function enforce_event_admission_runs();
        drop function enforce_dependency_admission_policy_approvals();
        drop function enforce_dependency_admission_runs();
        """
    )


def downgrade() -> None:
    raise NotImplementedError(
        "ADR-0028's join is not mechanically reversible: recreating six "
        "receipt tables and re-deriving their id spaces is restore-from-"
        "backup territory, and pretending otherwise here would be a trap"
    )
