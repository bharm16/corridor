"""Retain whole-row organization identity evidence and its replay gate.

ADR-0051 ends silent creation of an External Organization.  The registry's
``aliases`` column remains the current matching projection for compatibility,
while these append-only receipts retain why a source spelling was bound to a
registered party.  The activation table is the ADR-0050 gate for the new
facility/contact/revision/stated-alias automatic tiers; it deliberately starts
empty, therefore inactive.

Revision ID: c345a9f1d2e3
Revises: b4d1e2f3a5c6
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c345a9f1d2e3"
down_revision: Union[str, Sequence[str], None] = "c9e4f2a7b153"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "organization_identity_receipts",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("candidate_id", sa.BigInteger(), nullable=False),
        sa.Column("external_org_id", sa.BigInteger(), nullable=False),
        sa.Column("method", sa.String(length=64), nullable=False),
        sa.Column("scope", sa.String(length=32), server_default="registry", nullable=False),
        sa.Column("stated_wording", sa.Text(), nullable=False),
        sa.Column("evidence_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "facility_classes_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("recorded_by", sa.String(length=128), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=True),
        sa.Column("policy_sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "method in ('human_confirmation', 'automatic_name_alias', "
            "'automatic_facility_class', 'automatic_contact', "
            "'automatic_revision_lineage', 'automatic_stated_alias', "
            "'human_cited_alias_confirmation', 'alias_correction')",
            name="ck_organization_identity_receipt_method",
        ),
        sa.CheckConstraint("scope = 'registry'", name="ck_organization_identity_receipt_scope"),
        sa.CheckConstraint("length(trim(stated_wording)) > 0", name="ck_organization_identity_receipt_wording"),
        sa.CheckConstraint("length(trim(recorded_by)) > 0", name="ck_organization_identity_receipt_actor"),
        sa.CheckConstraint("jsonb_typeof(evidence_json) = 'object'", name="ck_organization_identity_receipt_evidence"),
        sa.CheckConstraint("jsonb_typeof(facility_classes_json) = 'array'", name="ck_organization_identity_receipt_facility_classes"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"]),
        sa.ForeignKeyConstraint(["external_org_id"], ["external_orgs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("candidate_id", "method", name="uq_organization_identity_receipt_candidate_method"),
    )
    op.create_index("ix_organization_identity_receipts_project_id", "organization_identity_receipts", ["project_id"])
    op.create_index("ix_organization_identity_receipts_candidate_id", "organization_identity_receipts", ["candidate_id"])
    op.create_index("ix_organization_identity_receipts_external_org_id", "organization_identity_receipts", ["external_org_id"])
    op.create_table(
        "organization_identity_activations",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("policy_sha256", sa.String(length=64), nullable=False),
        sa.Column("replay_case_count", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(length=160), nullable=False),
        sa.Column("recorded_by", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("action in ('activate', 'suspend')", name="ck_organization_identity_activation_action"),
        sa.CheckConstraint("policy_sha256 ~ '^[0-9a-f]{64}$'", name="ck_organization_identity_activation_sha256"),
        sa.CheckConstraint("length(trim(reason)) > 0", name="ck_organization_identity_activation_reason"),
        sa.CheckConstraint("length(trim(recorded_by)) > 0", name="ck_organization_identity_activation_actor"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_organization_identity_activations_project_id", "organization_identity_activations", ["project_id"])
    op.execute(
        """
        create function reject_organization_identity_mutation()
        returns trigger language plpgsql as $$
        begin
            raise exception 'organization identity history is append-only';
        end;
        $$;
        create trigger organization_identity_receipts_are_immutable
        before update or delete on organization_identity_receipts
        for each row execute function reject_organization_identity_mutation();
        create trigger organization_identity_receipts_reject_truncate
        before truncate on organization_identity_receipts
        for each statement execute function reject_organization_identity_mutation();
        create trigger organization_identity_activations_are_immutable
        before update or delete on organization_identity_activations
        for each row execute function reject_organization_identity_mutation();
        create trigger organization_identity_activations_reject_truncate
        before truncate on organization_identity_activations
        for each statement execute function reject_organization_identity_mutation();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        do $$
        begin
            if exists (select 1 from organization_identity_receipts)
               or exists (select 1 from organization_identity_activations) then
                raise exception 'cannot erase retained organization identity history';
            end if;
        end
        $$;
        drop trigger if exists organization_identity_receipts_are_immutable on organization_identity_receipts;
        drop trigger if exists organization_identity_receipts_reject_truncate on organization_identity_receipts;
        drop trigger if exists organization_identity_activations_are_immutable on organization_identity_activations;
        drop trigger if exists organization_identity_activations_reject_truncate on organization_identity_activations;
        drop function if exists reject_organization_identity_mutation();
        """
    )
    op.drop_index("ix_organization_identity_activations_project_id", table_name="organization_identity_activations")
    op.drop_table("organization_identity_activations")
    op.drop_index("ix_organization_identity_receipts_external_org_id", table_name="organization_identity_receipts")
    op.drop_index("ix_organization_identity_receipts_candidate_id", table_name="organization_identity_receipts")
    op.drop_index("ix_organization_identity_receipts_project_id", table_name="organization_identity_receipts")
    op.drop_table("organization_identity_receipts")
