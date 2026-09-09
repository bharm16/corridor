"""Clock-independent report eligibility in the existing transition (#645).

Historical archives contain no Report Run membership or ID watermark. None
can be backfilled from retained evidence in the supported schema. NULL is
intentional: timestamps are not proof. New reports retain the exact archive
relationship so comparison can restart after an unknown historical boundary.
"""

import sqlalchemy as sa


def upgrade(op):
    op.execute("alter table legacy_ledger_archives add column retirement_report_run_watermark_id bigint check(retirement_report_run_watermark_id >= 0)")
    op.execute("alter table report_runs add column retirement_archive_id bigint references legacy_ledger_archives(id)")
    op.execute("create index ix_report_runs_retirement_archive on report_runs(project_id,retirement_archive_id,id)")
    op.execute("""
        create function bind_report_retirement() returns trigger
        language plpgsql security definer set search_path to 'public' as $$
        declare archive_id bigint;
        begin
            if tg_op='UPDATE' then
                if new.id is distinct from old.id or new.project_id is distinct from old.project_id
                    or new.retirement_archive_id is distinct from old.retirement_archive_id then
                    raise exception 'report retirement identity is immutable' using errcode='23514';
                end if;
                return new;
            end if;
            select id into archive_id from legacy_ledger_archives where project_id=new.project_id;
            if new.retirement_archive_id is not null and new.retirement_archive_id is distinct from archive_id then
                raise exception 'report retirement binding differs from project archive' using errcode='23514';
            end if;
            new.retirement_archive_id := archive_id;
            return new;
        end; $$;
        create trigger bind_report_retirement before insert or update on report_runs
            for each row execute function bind_report_retirement();
        revoke all on function bind_report_retirement() from public;
    """)


def downgrade(op):
    if op.get_bind().scalar(sa.text("select exists(select 1 from legacy_ledger_archives where retirement_report_run_watermark_id is not null) or exists(select 1 from report_runs where retirement_archive_id is not null)")):
        raise RuntimeError("report retirement boundaries cannot be represented by the predecessor")
    op.execute("drop trigger bind_report_retirement on report_runs")
    op.execute("drop function bind_report_retirement()")
    op.execute("alter table report_runs drop column retirement_archive_id")
    op.execute("alter table legacy_ledger_archives drop column retirement_report_run_watermark_id")
