"""Store released External Report bytes and context once.

Revision ID: d430a1b2c3d4
Revises: 444758f7b4a7
"""

from alembic import op


revision = "d430a1b2c3d4"
down_revision = "444758f7b4a7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Let new receipts refer to immutable artifact-owned content."""

    op.execute(
        "alter table external_report_releases "
        "add column content_storage varchar(16) not null default 'legacy', "
        "alter column pdf_bytes drop not null, "
        "alter column record_context_json drop not null"
    )
    op.execute(
        "alter table external_report_releases "
        "alter column content_storage set default 'artifact'"
    )
    op.execute(
        "alter table external_report_releases "
        "drop constraint ck_external_report_releases_nonempty_pdf, "
        "add constraint ck_external_report_releases_nonempty_pdf "
        "check (pdf_bytes is null or octet_length(pdf_bytes) > 5), "
        "drop constraint ck_external_report_releases_context_object, "
        "add constraint ck_external_report_releases_context_object "
        "check (record_context_json is null "
        "or jsonb_typeof(record_context_json) = 'object'), "
        "add constraint ck_external_report_releases_content_owner "
        "check ((content_storage = 'legacy' and pdf_bytes is not null "
        "and record_context_json is not null) or "
        "(content_storage = 'artifact' and artifact_id is not null "
        "and pdf_bytes is null and evaluation_context_json is null "
        "and record_context_json is null))"
    )
    op.execute(
        """
        create function enforce_external_report_release_content_owner()
        returns trigger
        language plpgsql
        as $$
        begin
            if new.content_storage <> 'artifact'
               or new.artifact_id is null
               or new.pdf_bytes is not null
               or new.evaluation_context_json is not null
               or new.record_context_json is not null then
                raise exception
                    'new External Report release requires one artifact content owner without copied bytes or context'
                    using errcode = '23514';
            end if;
            return new;
        end;
        $$
        """
    )
    op.execute(
        "create trigger external_report_releases_require_artifact_content_owner "
        "before insert on external_report_releases for each row "
        "execute function enforce_external_report_release_content_owner()"
    )


def downgrade() -> None:
    """The supported released-head transition is intentionally one-way."""

    raise RuntimeError("released-report artifact-reference downgrade is unsupported")
