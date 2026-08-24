"""Make Automatic Carry-Forward normal Corridor-managed processing.

Revision ID: f315b4c6d8e0
Revises: e314a3d8c6f2

Historical project approvals and active pointers remain readable. New receipts
bind the released policy version and digest directly and do not require a
project authorization.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "f315b4c6d8e0"
down_revision: Union[str, Sequence[str], None] = "e314a3d8c6f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.rename_table(
        "active_automatic_carry_forward_policies",
        "retired_automatic_carry_forward_policy_activations",
    )

    op.execute(
        "alter table automatic_carry_forward_receipts "
        "disable trigger automatic_carry_forward_receipts_are_immutable"
    )
    op.add_column(
        "automatic_carry_forward_receipts",
        sa.Column("policy_version", sa.String(length=64)),
    )
    op.add_column(
        "automatic_carry_forward_receipts",
        sa.Column("policy_sha256", sa.String(length=64)),
    )
    op.execute(
        """
        update automatic_carry_forward_receipts receipt
           set policy_version = approval.policy_version,
               policy_sha256 = approval.policy_sha256
          from policy_approvals approval
         where approval.id = receipt.policy_approval_id
        """
    )
    op.alter_column(
        "automatic_carry_forward_receipts", "policy_version", nullable=False
    )
    op.alter_column("automatic_carry_forward_receipts", "policy_sha256", nullable=False)
    op.alter_column(
        "automatic_carry_forward_receipts", "policy_approval_id", nullable=True
    )
    op.create_check_constraint(
        "ck_automatic_carry_forward_receipt_policy_sha256",
        "automatic_carry_forward_receipts",
        "policy_sha256 ~ '^[0-9a-f]{64}$'",
    )
    _replace_receipt_policy_authority()
    op.execute(
        "alter table automatic_carry_forward_receipts "
        "enable trigger automatic_carry_forward_receipts_are_immutable"
    )
    op.execute(
        "alter table automatic_carry_forward_outcomes "
        "disable trigger automatic_carry_forward_outcomes_are_immutable"
    )
    op.drop_index(
        "uq_automatic_carry_forward_outcome_abstained_identity",
        table_name="automatic_carry_forward_outcomes",
    )
    op.alter_column(
        "automatic_carry_forward_outcomes", "policy_approval_id", nullable=True
    )
    _replace_outcome_policy_authority()
    op.execute(
        """
        create unique index uq_automatic_carry_forward_outcome_abstained_identity
        on automatic_carry_forward_outcomes (
            project_id,
            coalesce(policy_approval_id, 0),
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
        "alter table automatic_carry_forward_outcomes "
        "enable trigger automatic_carry_forward_outcomes_are_immutable"
    )


def _replace_receipt_policy_authority() -> None:
    """Keep every existing receipt guard while replacing human authorization.

    The predecessor function is intentionally large because it validates the
    exact comparison, Candidate, Evidence, Admission, and transfer lineage in
    PostgreSQL. Rewriting that proof would risk dropping an unrelated guard, so
    this successor replaces only the three authorization clauses and refuses if
    the released predecessor is not exactly the shape expected.
    """
    connection = op.get_bind()
    definition = connection.scalar(
        sa.text(
            "select pg_get_functiondef("
            "'enforce_automatic_carry_forward_receipt()'::regprocedure)"
        )
    )
    replacements = (
        (
            """                          join policy_approvals policy
                            on policy.id = new.policy_approval_id
                           and policy.project_id = new.project_id
""",
            "",
        ),
        (
            """                           and exists (
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
""",
            "",
        ),
        (
            """                           and jsonb_typeof(
                               act.after_json -> 'policy_approval_id'
                           ) = 'number'
                           and (act.after_json ->> 'policy_approval_id')::bigint =
                               new.policy_approval_id
                           and act.after_json ->> 'policy_sha256' =
                               policy.policy_sha256
""",
            """                           and new.policy_approval_id is null
                           and not (act.after_json ? 'policy_approval_id')
                           and act.after_json ->> 'policy_version' =
                               new.policy_version
                           and act.after_json ->> 'policy_sha256' =
                               new.policy_sha256
""",
        ),
    )
    for old, new in replacements:
        if old not in definition:
            raise RuntimeError(
                "released Carry-Forward receipt guard does not match the expected predecessor"
            )
        definition = definition.replace(old, new, 1)
    connection.exec_driver_sql(definition)


def _replace_outcome_policy_authority() -> None:
    """Bind new outcomes to their released-policy Policy Run, without approval."""
    connection = op.get_bind()
    definition = connection.scalar(
        sa.text(
            "select pg_get_functiondef("
            "'enforce_automatic_carry_forward_outcome()'::regprocedure)"
        )
    )
    replacements = (
        (
            """                          join policy_approvals policy
                            on policy.project_id = new.project_id
                           and policy.id = new.policy_approval_id
                           and policy.family = 'automatic-carry-forward'
""",
            "",
        ),
        (
            """                           and run.policy_approval_id = new.policy_approval_id
""",
            """                           and run.policy_approval_id is null
                           and new.policy_approval_id is null
""",
        ),
        (
            """                                   and receipt.policy_approval_id =
                                       new.policy_approval_id
                                   and receipt.dependency_id = new.dependency_id
""",
            """                                   and receipt.policy_approval_id is null
                                   and receipt.policy_version = run.policy_version
                                   and receipt.policy_sha256 = run.policy_sha256
                                   and receipt.dependency_id = new.dependency_id
""",
        ),
    )
    for old, new in replacements:
        if old not in definition:
            raise RuntimeError(
                "released Carry-Forward outcome guard does not match the expected predecessor"
            )
        definition = definition.replace(old, new, 1)
    connection.exec_driver_sql(definition)


def downgrade() -> None:
    raise RuntimeError(
        "cannot restore project authorization as Automatic Carry-Forward authority"
    )
