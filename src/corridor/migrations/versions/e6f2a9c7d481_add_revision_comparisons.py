"""add immutable exact-run Revision Comparisons

Revision ID: e6f2a9c7d481
Revises: d7a1c4e9b205
Create Date: 2026-08-05 19:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e6f2a9c7d481"
down_revision: Union[str, Sequence[str], None] = "d7a1c4e9b205"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "extraction_runs",
        sa.Column("candidate_inputs_json", postgresql.JSONB(), nullable=True),
    )
    # A completed legacy zero-row run is exact by definition. A nonzero
    # legacy run stays null: its live Candidates may already have been edited,
    # so backfilling them would mislabel current review state as extractor-time
    # input. Fresh extraction is the only honest repair.
    op.execute(
        """
        update extraction_runs
           set candidate_inputs_json = '[]'::jsonb
         where candidate_count = 0
        """
    )
    op.execute(
        """
        create function reject_extraction_run_receipt_mutation()
        returns trigger
        language plpgsql
        as $$
        declare
            demo_project boolean;
        begin
            if tg_op = 'DELETE' then
                select exists (
                    select 1
                      from documents
                      join projects on projects.id = documents.project_id
                     where documents.id = old.document_id
                       and projects.is_synthetic
                       and projects.slug = 'corridor-demo'
                ) into demo_project;
                if demo_project then
                    return old;
                end if;
            end if;
            raise exception 'Extraction Run receipts are immutable'
                using errcode = '23514';
        end;
        $$;

        create trigger extraction_run_receipts_are_immutable
        before update or delete on extraction_runs
        for each row execute function reject_extraction_run_receipt_mutation();

        create trigger extraction_run_receipts_reject_truncate
        before truncate on extraction_runs
        for each statement execute function reject_extraction_run_receipt_mutation();
        """
    )
    op.execute(
        """
        create function enforce_candidate_run_lineage()
        returns trigger
        language plpgsql
        as $$
        declare
            demo_project boolean;
            owned_by_run boolean;
        begin
            if tg_op = 'TRUNCATE' then
                raise exception 'Candidate run lineage is immutable'
                    using errcode = '23514';
            end if;

            if tg_op = 'DELETE' then
                select exists (
                    select 1
                      from projects
                     where projects.id = old.project_id
                       and projects.is_synthetic
                       and projects.slug = 'corridor-demo'
                ) into demo_project;
                if demo_project then
                    return old;
                end if;
                raise exception 'Candidate run lineage is immutable'
                    using errcode = '23514';
            end if;

            if tg_op = 'INSERT' then
                if new.extraction_run_id is not null then
                    raise exception
                        'Candidate must attach to its run after input capture'
                        using errcode = '23514';
                end if;
                return new;
            end if;

            if new.id is distinct from old.id
               or new.project_id is distinct from old.project_id
               or new.source_document_id is distinct from old.source_document_id
               or new.kind is distinct from old.kind
               or new.source_pages is distinct from old.source_pages
               or new.confidence is distinct from old.confidence
               or new.prompt_version is distinct from old.prompt_version
               or new.model is distinct from old.model
               or new.created_at is distinct from old.created_at then
                raise exception 'Candidate run lineage is immutable'
                    using errcode = '23514';
            end if;

            if new.extraction_run_id is distinct from old.extraction_run_id then
                if old.extraction_run_id is not null
                   or new.extraction_run_id is null then
                    raise exception 'Candidate run lineage is immutable'
                        using errcode = '23514';
                end if;
                select exists (
                    select 1
                      from extraction_runs
                      join documents
                        on documents.id = extraction_runs.document_id
                     where extraction_runs.id = new.extraction_run_id
                       and extraction_runs.document_id = new.source_document_id
                       and documents.project_id = new.project_id
                       and extraction_runs.prompt_version = new.prompt_version
                       and extraction_runs.model is not distinct from new.model
                       and extraction_runs.candidate_inputs_json @>
                           jsonb_build_array(
                               jsonb_build_object(
                                   'candidate_id', new.id,
                                   'project_id', new.project_id,
                                   'source_document_id', new.source_document_id,
                                   'prompt_version', new.prompt_version,
                                   'model', new.model
                               )
                           )
                ) into owned_by_run;
                if not owned_by_run then
                    raise exception
                        'Candidate is absent from its immutable run inputs'
                        using errcode = '23514';
                end if;
            end if;
            return new;
        end;
        $$;

        create trigger candidate_run_lineage_is_immutable
        before insert or update or delete on candidates
        for each row execute function enforce_candidate_run_lineage();

        create trigger candidates_reject_truncate
        before truncate on candidates
        for each statement execute function enforce_candidate_run_lineage();
        """
    )
    op.create_table(
        "revision_comparison_runs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", sa.BigInteger(), nullable=False),
        sa.Column("predecessor_document_id", sa.BigInteger(), nullable=False),
        sa.Column("successor_document_id", sa.BigInteger(), nullable=False),
        sa.Column("predecessor_extraction_run_id", sa.BigInteger(), nullable=False),
        sa.Column("successor_extraction_run_id", sa.BigInteger(), nullable=False),
        sa.Column("predecessor_schema_version", sa.String(length=64), nullable=True),
        sa.Column("successor_schema_version", sa.String(length=64), nullable=True),
        sa.Column("predecessor_prompt_version", sa.String(length=64), nullable=False),
        sa.Column("successor_prompt_version", sa.String(length=64), nullable=False),
        sa.Column("predecessor_model", sa.String(length=64), nullable=True),
        sa.Column("successor_model", sa.String(length=64), nullable=True),
        sa.Column("matcher_version", sa.String(length=64), nullable=False),
        sa.Column("matcher_config", postgresql.JSONB(), nullable=False),
        sa.Column("predecessor_inputs_json", postgresql.JSONB(), nullable=False),
        sa.Column("successor_inputs_json", postgresql.JSONB(), nullable=False),
        sa.Column("finding_count", sa.Integer(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "predecessor_document_id <> successor_document_id",
            name="ck_revision_comparison_distinct_documents",
        ),
        sa.CheckConstraint(
            "finding_count >= 0", name="ck_revision_comparison_finding_count"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(
            ["project_id", "predecessor_document_id"],
            ["documents.project_id", "documents.id"],
            name="fk_revision_comparison_predecessor_project",
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "successor_document_id"],
            ["documents.project_id", "documents.id"],
            name="fk_revision_comparison_successor_project",
        ),
        sa.ForeignKeyConstraint(
            ["predecessor_document_id", "predecessor_extraction_run_id"],
            ["extraction_runs.document_id", "extraction_runs.id"],
            name="fk_revision_comparison_predecessor_run",
        ),
        sa.ForeignKeyConstraint(
            ["successor_document_id", "successor_extraction_run_id"],
            ["extraction_runs.document_id", "extraction_runs.id"],
            name="fk_revision_comparison_successor_run",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "revision_comparison_findings",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("revision_comparison_run_id", sa.BigInteger(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "added",
                "dropped",
                "unchanged",
                "changed",
                "ambiguous",
                "unmatched",
                name="revision_comparison_state",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column(
            "predecessor_candidate_ids",
            postgresql.ARRAY(sa.BigInteger()),
            nullable=False,
        ),
        sa.Column(
            "successor_candidate_ids",
            postgresql.ARRAY(sa.BigInteger()),
            nullable=False,
        ),
        sa.Column("match_score", sa.Float(), nullable=True),
        sa.Column("field_changes", postgresql.JSONB(), nullable=False),
        sa.Column("matcher_detail", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint(
            "match_score is null or (match_score >= 0 and match_score <= 1)",
            name="ck_revision_comparison_match_score",
        ),
        sa.CheckConstraint(
            "(state = 'added' and cardinality(predecessor_candidate_ids) = 0 "
            "and cardinality(successor_candidate_ids) = 1) or "
            "(state = 'dropped' and cardinality(predecessor_candidate_ids) = 1 "
            "and cardinality(successor_candidate_ids) = 0) or "
            "(state = 'unmatched' and "
            "((cardinality(predecessor_candidate_ids) = 1 "
            "and cardinality(successor_candidate_ids) = 0) or "
            "(cardinality(predecessor_candidate_ids) = 0 "
            "and cardinality(successor_candidate_ids) = 1))) or "
            "(state in ('unchanged', 'changed') "
            "and cardinality(predecessor_candidate_ids) = 1 "
            "and cardinality(successor_candidate_ids) = 1) or "
            "(state = 'ambiguous' "
            "and cardinality(predecessor_candidate_ids) > 0 "
            "and cardinality(successor_candidate_ids) > 0)",
            name="ck_revision_comparison_finding_shape",
        ),
        sa.ForeignKeyConstraint(
            ["revision_comparison_run_id"], ["revision_comparison_runs.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("revision_comparison_run_id", "ordinal"),
    )
    op.execute(
        """
        create function enforce_revision_comparison_run_mutation()
        returns trigger
        language plpgsql
        as $$
        declare
            existing_count integer;
        begin
            if tg_op = 'INSERT' then
                if new.sealed_at is not null then
                    raise exception
                        'Revision Comparison must begin unsealed'
                        using errcode = '23514';
                end if;
                return new;
            end if;

            if tg_op = 'DELETE' then
                raise exception 'Revision Comparison receipts are append-only'
                    using errcode = '23514';
            end if;

            if old.sealed_at is null
               and new.sealed_at is not null
               and (to_jsonb(new) - 'sealed_at') = (to_jsonb(old) - 'sealed_at') then
                select count(*) into existing_count
                  from revision_comparison_findings
                 where revision_comparison_run_id = old.id;
                if existing_count <> old.finding_count then
                    raise exception
                        'Revision Comparison cannot seal with % of % findings',
                        existing_count,
                        old.finding_count
                        using errcode = '23514';
                end if;
                return new;
            end if;

            raise exception 'Revision Comparison receipts are append-only'
                using errcode = '23514';
        end;
        $$;

        create trigger revision_comparison_runs_are_immutable
        before insert or update or delete on revision_comparison_runs
        for each row execute function enforce_revision_comparison_run_mutation();
        """
    )
    op.execute(
        """
        create function enforce_revision_comparison_finding_mutation()
        returns trigger
        language plpgsql
        as $$
        declare
            expected_count integer;
            sealed timestamptz;
            existing_count integer;
        begin
            if tg_op <> 'INSERT' then
                raise exception 'Revision Comparison findings are append-only'
                    using errcode = '23514';
            end if;

            select finding_count, sealed_at into expected_count, sealed
              from revision_comparison_runs
             where id = new.revision_comparison_run_id;
            select count(*) into existing_count
              from revision_comparison_findings
             where revision_comparison_run_id = new.revision_comparison_run_id;
            if expected_count is null or sealed is not null
               or existing_count >= expected_count then
                raise exception 'Revision Comparison finding set is sealed'
                    using errcode = '23514';
            end if;
            return new;
        end;
        $$;

        create trigger revision_comparison_findings_are_immutable
        before insert or update or delete on revision_comparison_findings
        for each row execute function enforce_revision_comparison_finding_mutation();
        """
    )
    op.execute(
        """
        create function require_sealed_revision_comparison()
        returns trigger
        language plpgsql
        as $$
        declare
            sealed timestamptz;
            expected_count integer;
            existing_count integer;
        begin
            select sealed_at, finding_count into sealed, expected_count
              from revision_comparison_runs
             where id = new.id;
            select count(*) into existing_count
              from revision_comparison_findings
             where revision_comparison_run_id = new.id;
            if sealed is null or existing_count <> expected_count then
                raise exception 'Revision Comparison must commit sealed and complete'
                    using errcode = '23514';
            end if;
            return null;
        end;
        $$;

        create constraint trigger revision_comparison_runs_must_commit_sealed
        after insert on revision_comparison_runs
        deferrable initially deferred
        for each row execute function require_sealed_revision_comparison();
        """
    )
    op.execute(
        """
        create function reject_revision_comparison_truncate()
        returns trigger
        language plpgsql
        as $$
        begin
            raise exception 'Revision Comparison receipts are append-only'
                using errcode = '23514';
        end;
        $$;

        create trigger revision_comparison_runs_reject_truncate
        before truncate on revision_comparison_runs
        for each statement execute function reject_revision_comparison_truncate();

        create trigger revision_comparison_findings_reject_truncate
        before truncate on revision_comparison_findings
        for each statement execute function reject_revision_comparison_truncate();
        """
    )


def downgrade() -> None:
    op.execute("drop trigger if exists candidates_reject_truncate on candidates")
    op.execute(
        "drop trigger if exists candidate_run_lineage_is_immutable on candidates"
    )
    op.execute("drop function if exists enforce_candidate_run_lineage()")
    op.execute(
        "drop trigger if exists extraction_run_receipts_reject_truncate "
        "on extraction_runs"
    )
    op.execute(
        "drop trigger if exists revision_comparison_findings_reject_truncate "
        "on revision_comparison_findings"
    )
    op.execute(
        "drop trigger if exists revision_comparison_runs_reject_truncate "
        "on revision_comparison_runs"
    )
    op.execute("drop function if exists reject_revision_comparison_truncate()")
    op.execute(
        "drop trigger if exists revision_comparison_runs_must_commit_sealed "
        "on revision_comparison_runs"
    )
    op.execute("drop function if exists require_sealed_revision_comparison()")
    op.execute(
        "drop trigger if exists revision_comparison_findings_are_immutable "
        "on revision_comparison_findings"
    )
    op.execute("drop function if exists enforce_revision_comparison_finding_mutation()")
    op.execute(
        "drop trigger if exists revision_comparison_runs_are_immutable "
        "on revision_comparison_runs"
    )
    op.execute("drop function if exists enforce_revision_comparison_run_mutation()")
    op.drop_table("revision_comparison_findings")
    op.drop_table("revision_comparison_runs")
    op.execute(
        "drop trigger if exists extraction_run_receipts_are_immutable "
        "on extraction_runs"
    )
    op.execute("drop function if exists reject_extraction_run_receipt_mutation()")
    op.drop_column("extraction_runs", "candidate_inputs_json")
