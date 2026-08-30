"""Individual sign-in identity, sessions, and project-scoped designations.

Adds the storage for #331: a global email->principal identity, the single-use
magic-link secrets and browser sessions (both stored only as hashes), an
append-only backoff ledger, and the four distinct write designations on each
project membership.

No historical person->account association is invented: the new identity table
starts empty, and existing audit and decision principals keep their own subject
untouched.  The designation columns default to false, so an existing membership
gains no authority until it is explicitly, attributably designated.

Revision ID: b6d24f1e8a37
Revises: a364b7c9e2f1
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b6d24f1e8a37"
down_revision: Union[str, Sequence[str], None] = "a364b7c9e2f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Membership carries authority explicitly; nothing is implied by being on
    # the roster.  Existing rows fail closed with every designation off.
    for column in (
        "can_coordinate",
        "can_review_documentation",
        "can_release_externally",
        "is_technical_operator",
    ):
        op.add_column(
            "project_roster_entries",
            sa.Column(
                column,
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )

    op.create_table(
        "person_identities",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("email_normalized", sa.Text(), nullable=False),
        sa.Column("principal_subject", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(trim(email_normalized)) > 0",
            name="ck_person_identity_email",
        ),
        sa.CheckConstraint(
            "length(trim(principal_subject)) > 0",
            name="ck_person_identity_principal",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email_normalized", name="uq_person_identity_email"),
        sa.UniqueConstraint(
            "principal_subject", name="uq_person_identity_principal"
        ),
    )

    op.create_table(
        "sign_in_tokens",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("email_normalized", sa.Text(), nullable=False),
        sa.Column("token_sha256", sa.String(length=64), nullable=False),
        sa.Column("redirect_path", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(token_sha256) = 64", name="ck_sign_in_token_hash"
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name="ck_sign_in_token_expiry"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_sha256", name="uq_sign_in_token_hash"),
    )

    op.create_table(
        "web_sessions",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("session_sha256", sa.String(length=64), nullable=False),
        sa.Column("csrf_sha256", sa.String(length=64), nullable=False),
        sa.Column("principal_subject", sa.String(length=128), nullable=False),
        sa.Column("email_normalized", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(session_sha256) = 64", name="ck_web_session_hash"
        ),
        sa.CheckConstraint(
            "length(csrf_sha256) = 64", name="ck_web_session_csrf"
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name="ck_web_session_expiry"
        ),
        sa.CheckConstraint(
            "length(trim(principal_subject)) > 0",
            name="ck_web_session_principal",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_sha256", name="uq_web_session_hash"),
    )

    op.create_table(
        "sign_in_attempts",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("scope_kind", sa.String(length=32), nullable=False),
        sa.Column("scope_value", sa.Text(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_sign_in_attempt_scope",
        "sign_in_attempts",
        ["scope_kind", "scope_value", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_sign_in_attempt_scope", table_name="sign_in_attempts")
    op.drop_table("sign_in_attempts")
    op.drop_table("web_sessions")
    op.drop_table("sign_in_tokens")
    op.drop_table("person_identities")
    for column in (
        "is_technical_operator",
        "can_release_externally",
        "can_review_documentation",
        "can_coordinate",
    ):
        op.drop_column("project_roster_entries", column)
