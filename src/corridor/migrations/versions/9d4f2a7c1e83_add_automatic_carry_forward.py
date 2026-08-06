"""add policy-authorized Automatic Carry-Forward

Revision ID: 9d4f2a7c1e83
Revises: 6e1c4a9f2b70
Create Date: 2026-08-06 03:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "9d4f2a7c1e83"
down_revision: Union[str, Sequence[str], None] = "6e1c4a9f2b70"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "automatic_carry_forward_policy_approvals",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("approved_by", sa.Text(), nullable=False),
        sa.Column("policy_json", postgresql.JSONB(), nullable=False),
        sa.Column("policy_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "approved_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(policy_json) = 'object'",
            name="ck_automatic_carry_forward_policy_object",
        ),
        sa.CheckConstraint(
            "policy_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_automatic_carry_forward_policy_sha256",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id",
            "id",
            name="uq_automatic_carry_forward_policy_project_id",
        ),
    )
    op.create_table(
        "active_automatic_carry_forward_policies",
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("policy_approval_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "activated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(
            ["project_id", "policy_approval_id"],
            [
                "automatic_carry_forward_policy_approvals.project_id",
                "automatic_carry_forward_policy_approvals.id",
            ],
            name="fk_active_automatic_carry_forward_policy_project",
        ),
        sa.PrimaryKeyConstraint("project_id"),
        sa.UniqueConstraint("policy_approval_id"),
    )
    op.create_table(
        "automatic_carry_forward_receipts",
        sa.Column("audit_log_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("policy_approval_id", sa.BigInteger(), nullable=False),
        sa.Column("dependency_id", sa.BigInteger(), nullable=False),
        sa.Column("comparison_id", sa.BigInteger(), nullable=False),
        sa.Column("finding_id", sa.BigInteger(), nullable=False),
        sa.Column("predecessor_candidate_id", sa.BigInteger(), nullable=False),
        sa.Column("successor_candidate_id", sa.BigInteger(), nullable=False),
        sa.Column("new_evidence_link_id", sa.BigInteger(), nullable=False),
        sa.Column("origin_admission_audit_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "predecessor_support_transfer_audit_id",
            sa.BigInteger(),
            nullable=True,
        ),
        sa.Column("before_json", postgresql.JSONB(), nullable=False),
        sa.Column("after_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(before_json) = 'object'",
            name="ck_automatic_carry_forward_receipt_before_object",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(after_json) = 'object'",
            name="ck_automatic_carry_forward_receipt_after_object",
        ),
        sa.ForeignKeyConstraint(["audit_log_id"], ["audit_log.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(
            ["project_id", "policy_approval_id"],
            [
                "automatic_carry_forward_policy_approvals.project_id",
                "automatic_carry_forward_policy_approvals.id",
            ],
            name="fk_automatic_carry_forward_receipt_policy_project",
        ),
        sa.ForeignKeyConstraint(["dependency_id"], ["dependencies.id"]),
        sa.ForeignKeyConstraint(
            ["comparison_id"], ["revision_comparison_runs.id"]
        ),
        sa.ForeignKeyConstraint(
            ["finding_id"], ["revision_comparison_findings.id"]
        ),
        sa.ForeignKeyConstraint(
            ["predecessor_candidate_id"], ["candidates.id"]
        ),
        sa.ForeignKeyConstraint(
            ["successor_candidate_id"], ["candidates.id"]
        ),
        sa.ForeignKeyConstraint(
            ["dependency_id", "new_evidence_link_id"],
            ["evidence_links.dependency_id", "evidence_links.id"],
            name="fk_automatic_carry_forward_receipt_dependency_evidence",
        ),
        sa.ForeignKeyConstraint(
            ["origin_admission_audit_id"], ["audit_log.id"]
        ),
        sa.ForeignKeyConstraint(
            ["predecessor_support_transfer_audit_id"], ["audit_log.id"]
        ),
        sa.PrimaryKeyConstraint("audit_log_id"),
        sa.UniqueConstraint(
            "dependency_id",
            "successor_candidate_id",
            name="uq_automatic_carry_forward_dependency_successor",
        ),
        sa.UniqueConstraint(
            "new_evidence_link_id",
            name="uq_automatic_carry_forward_new_evidence",
        ),
    )
    op.create_index(
        "ix_automatic_carry_forward_receipts_dependency_id",
        "automatic_carry_forward_receipts",
        ["dependency_id"],
    )
    op.create_table(
        "automatic_carry_forward_runs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("policy_approval_id", sa.BigInteger(), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("policy_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "abstention_reason_version",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column("carried_count", sa.Integer(), nullable=False),
        sa.Column("abstained_count", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "policy_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_automatic_carry_forward_run_sha256",
        ),
        sa.CheckConstraint(
            "carried_count >= 0 and abstained_count >= 0",
            name="ck_automatic_carry_forward_run_counts",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(
            ["project_id", "policy_approval_id"],
            [
                "automatic_carry_forward_policy_approvals.project_id",
                "automatic_carry_forward_policy_approvals.id",
            ],
            name="fk_automatic_carry_forward_run_policy_project",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id",
            "id",
            name="uq_automatic_carry_forward_run_project_id",
        ),
    )
    op.create_index(
        "ix_automatic_carry_forward_runs_project_id",
        "automatic_carry_forward_runs",
        ["project_id"],
    )
    op.create_table(
        "automatic_carry_forward_outcomes",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("policy_approval_id", sa.BigInteger(), nullable=False),
        sa.Column("dependency_id", sa.BigInteger(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=128), nullable=True),
        sa.Column("reason_version", sa.String(length=64), nullable=True),
        sa.Column("receipt_audit_log_id", sa.BigInteger(), nullable=True),
        sa.Column("comparison_id", sa.BigInteger(), nullable=True),
        sa.Column("finding_id", sa.BigInteger(), nullable=True),
        sa.Column("predecessor_candidate_id", sa.BigInteger(), nullable=True),
        sa.Column("successor_candidate_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "outcome in ('carried', 'abstained')",
            name="ck_automatic_carry_forward_outcome_value",
        ),
        sa.CheckConstraint(
            "("
            "outcome = 'carried' and reason is null and reason_version is null "
            "and receipt_audit_log_id is not null"
            ") or ("
            "outcome = 'abstained' and reason is not null "
            "and reason_version is not null and receipt_audit_log_id is null"
            ")",
            name="ck_automatic_carry_forward_outcome_kind",
        ),
        sa.ForeignKeyConstraint(
            ["comparison_id"], ["revision_comparison_runs.id"]
        ),
        sa.ForeignKeyConstraint(
            ["dependency_id"], ["dependencies.id"]
        ),
        sa.ForeignKeyConstraint(
            ["finding_id"], ["revision_comparison_findings.id"]
        ),
        sa.ForeignKeyConstraint(
            ["predecessor_candidate_id"], ["candidates.id"]
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(
            ["receipt_audit_log_id"],
            ["automatic_carry_forward_receipts.audit_log_id"],
        ),
        sa.ForeignKeyConstraint(
            ["successor_candidate_id"], ["candidates.id"]
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "policy_approval_id"],
            [
                "automatic_carry_forward_policy_approvals.project_id",
                "automatic_carry_forward_policy_approvals.id",
            ],
            name="fk_automatic_carry_forward_outcome_policy_project",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "run_id"],
            [
                "automatic_carry_forward_runs.project_id",
                "automatic_carry_forward_runs.id",
            ],
            name="fk_automatic_carry_forward_outcome_run_project",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id",
            "id",
            name="uq_automatic_carry_forward_outcome_project_id",
        ),
        sa.UniqueConstraint(
            "receipt_audit_log_id",
            name="uq_automatic_carry_forward_outcome_receipt_audit",
        ),
    )
    op.create_index(
        "ix_automatic_carry_forward_outcomes_project_id",
        "automatic_carry_forward_outcomes",
        ["project_id"],
    )
    op.execute(
        """
        create unique index uq_automatic_carry_forward_outcome_abstained_identity
        on automatic_carry_forward_outcomes (
            project_id,
            policy_approval_id,
            dependency_id,
            coalesce(comparison_id, -1),
            coalesce(finding_id, -1),
            coalesce(predecessor_candidate_id, -1),
            coalesce(successor_candidate_id, -1),
            reason,
            reason_version
        )
        where outcome = 'abstained'
        """
    )

    op.execute(
        """
        create function enforce_automatic_carry_forward_policy_approval()
        returns trigger
        language plpgsql
        as $$
        begin
            if tg_op = 'INSERT' then
                return new;
            end if;
            raise exception 'Automatic Carry-Forward policy approvals are immutable'
                using errcode = '23514';
        end;
        $$;

        create trigger automatic_carry_forward_policy_approvals_are_immutable
        before insert or update or delete
        on automatic_carry_forward_policy_approvals
        for each row execute function
            enforce_automatic_carry_forward_policy_approval();

        create trigger automatic_carry_forward_policy_approvals_reject_truncate
        before truncate on automatic_carry_forward_policy_approvals
        for each statement execute function
            enforce_automatic_carry_forward_policy_approval();
        """
    )
    op.execute(
        """
        create function enforce_automatic_carry_forward_receipt()
        returns trigger
        language plpgsql
        as $$
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
                          join automatic_carry_forward_policy_approvals policy
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
        $$;

        create trigger automatic_carry_forward_receipts_are_immutable
        before insert or update or delete on automatic_carry_forward_receipts
        for each row execute function enforce_automatic_carry_forward_receipt();

        create trigger automatic_carry_forward_receipts_reject_truncate
        before truncate on automatic_carry_forward_receipts
        for each statement execute function enforce_automatic_carry_forward_receipt();
        """
    )
    op.execute(
        """
        create function enforce_automatic_carry_forward_run()
        returns trigger
        language plpgsql
        as $$
        declare
            valid_binding boolean;
        begin
            if tg_op = 'INSERT' then
                begin
                    select exists (
                        select 1
                          from automatic_carry_forward_policy_approvals policy
                         where policy.project_id = new.project_id
                           and policy.id = new.policy_approval_id
                           and policy.policy_version = new.policy_version
                           and policy.policy_sha256 = new.policy_sha256
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
                    ) into valid_binding;
                exception
                    when invalid_text_representation
                        or numeric_value_out_of_range then
                        valid_binding := false;
                end;
                if not valid_binding then
                    raise exception 'Automatic Carry-Forward run binding is invalid'
                        using errcode = '23514';
                end if;
                return new;
            end if;

            raise exception 'Automatic Carry-Forward runs are append-only'
                using errcode = '23514';
        end;
        $$;

        create trigger automatic_carry_forward_runs_are_immutable
        before insert or update or delete on automatic_carry_forward_runs
        for each row execute function enforce_automatic_carry_forward_run();

        create trigger automatic_carry_forward_runs_reject_truncate
        before truncate on automatic_carry_forward_runs
        for each statement execute function enforce_automatic_carry_forward_run();

        create function verify_automatic_carry_forward_run_counts(
            target_run_id bigint
        )
        returns void
        language plpgsql
        as $$
        declare
            expected_carried integer;
            expected_abstained integer;
            actual_carried integer;
            actual_abstained integer;
        begin
            select carried_count, abstained_count
              into expected_carried, expected_abstained
              from automatic_carry_forward_runs
             where id = target_run_id;
            if expected_carried is null or expected_abstained is null then
                raise exception
                    'Automatic Carry-Forward runs must reconcile their recorded outcomes'
                    using errcode = '23514';
            end if;
            select
                count(*) filter (where outcome = 'carried'),
                count(*) filter (where outcome = 'abstained')
              into actual_carried, actual_abstained
              from automatic_carry_forward_outcomes
             where run_id = target_run_id;
            if actual_carried <> expected_carried
               or actual_abstained <> expected_abstained then
                raise exception
                    'Automatic Carry-Forward runs must reconcile their recorded outcomes'
                    using errcode = '23514';
            end if;
            return;
        end;
        $$;

        create function require_automatic_carry_forward_run_counts()
        returns trigger
        language plpgsql
        as $$
        begin
            perform verify_automatic_carry_forward_run_counts(new.id);
            return null;
        end;
        $$;

        create function require_automatic_carry_forward_outcome_counts()
        returns trigger
        language plpgsql
        as $$
        begin
            perform verify_automatic_carry_forward_run_counts(new.run_id);
            return null;
        end;
        $$;

        create constraint trigger automatic_carry_forward_runs_must_match_outcomes
        after insert on automatic_carry_forward_runs
        deferrable initially deferred
        for each row execute function require_automatic_carry_forward_run_counts();

        create constraint trigger automatic_carry_forward_outcomes_must_match_runs
        after insert on automatic_carry_forward_outcomes
        deferrable initially deferred
        for each row execute function require_automatic_carry_forward_outcome_counts();

        create function enforce_automatic_carry_forward_outcome()
        returns trigger
        language plpgsql
        as $$
        declare
            valid_binding boolean;
        begin
            if tg_op = 'INSERT' then
                begin
                    select exists (
                        select 1
                          from automatic_carry_forward_runs run
                          join automatic_carry_forward_policy_approvals policy
                            on policy.project_id = new.project_id
                           and policy.id = new.policy_approval_id
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
        $$;

        create trigger automatic_carry_forward_outcomes_are_immutable
        before insert or update or delete on automatic_carry_forward_outcomes
        for each row execute function enforce_automatic_carry_forward_outcome();

        create trigger automatic_carry_forward_outcomes_reject_truncate
        before truncate on automatic_carry_forward_outcomes
        for each statement execute function
            enforce_automatic_carry_forward_outcome();
        """
    )


def downgrade() -> None:
    op.execute(
        "drop trigger if exists automatic_carry_forward_outcomes_must_match_runs "
        "on automatic_carry_forward_outcomes"
    )
    op.execute(
        "drop trigger if exists automatic_carry_forward_runs_must_match_outcomes "
        "on automatic_carry_forward_runs"
    )
    op.execute(
        "drop function if exists require_automatic_carry_forward_outcome_counts()"
    )
    op.execute(
        "drop function if exists require_automatic_carry_forward_run_counts()"
    )
    op.execute(
        "drop function if exists verify_automatic_carry_forward_run_counts(bigint)"
    )
    op.execute(
        "drop trigger if exists automatic_carry_forward_outcomes_reject_truncate "
        "on automatic_carry_forward_outcomes"
    )
    op.execute(
        "drop trigger if exists automatic_carry_forward_outcomes_are_immutable "
        "on automatic_carry_forward_outcomes"
    )
    op.execute("drop function if exists enforce_automatic_carry_forward_outcome()")
    op.execute(
        "drop trigger if exists automatic_carry_forward_runs_reject_truncate "
        "on automatic_carry_forward_runs"
    )
    op.execute(
        "drop trigger if exists automatic_carry_forward_runs_are_immutable "
        "on automatic_carry_forward_runs"
    )
    op.execute("drop function if exists enforce_automatic_carry_forward_run()")
    op.execute(
        "drop trigger if exists automatic_carry_forward_receipts_reject_truncate "
        "on automatic_carry_forward_receipts"
    )
    op.execute(
        "drop trigger if exists automatic_carry_forward_receipts_are_immutable "
        "on automatic_carry_forward_receipts"
    )
    op.execute("drop function if exists enforce_automatic_carry_forward_receipt()")
    op.execute(
        "drop trigger if exists "
        "automatic_carry_forward_policy_approvals_reject_truncate "
        "on automatic_carry_forward_policy_approvals"
    )
    op.execute(
        "drop trigger if exists "
        "automatic_carry_forward_policy_approvals_are_immutable "
        "on automatic_carry_forward_policy_approvals"
    )
    op.execute(
        "drop function if exists "
        "enforce_automatic_carry_forward_policy_approval()"
    )
    op.execute(
        "drop index if exists "
        "uq_automatic_carry_forward_outcome_abstained_identity"
    )
    op.execute("drop index if exists ix_automatic_carry_forward_outcomes_project_id")
    op.execute("drop table if exists automatic_carry_forward_outcomes")
    op.execute("drop index if exists ix_automatic_carry_forward_runs_project_id")
    op.execute("drop table if exists automatic_carry_forward_runs")
    op.drop_index(
        "ix_automatic_carry_forward_receipts_dependency_id",
        table_name="automatic_carry_forward_receipts",
    )
    op.drop_table("automatic_carry_forward_receipts")
    op.drop_table("active_automatic_carry_forward_policies")
    op.drop_table("automatic_carry_forward_policy_approvals")
