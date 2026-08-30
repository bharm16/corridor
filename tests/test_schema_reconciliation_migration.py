"""Rehearse convergence of historical and freshly replayed public schemas."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from corridor.config import settings
from corridor.m8_acceptance_database import provision_disposable_postgres
from corridor.product_proving_database import _fingerprint_public_schema


pytestmark = pytest.mark.slow

ROOT = Path(__file__).resolve().parents[1]
PREDECESSOR = "b317c5d7e9f2"
HEAD = "b7d3f9a1c2e5"


def _upgrade(database_url: str, target: str) -> None:
    completed = subprocess.run(
        ["uv", "run", "alembic", "upgrade", target],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def _downgrade(database_url: str, target: str) -> None:
    completed = subprocess.run(
        ["uv", "run", "alembic", "downgrade", target],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def _schema(database_url: str):
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            return _fingerprint_public_schema(connection)
    finally:
        engine.dispose()


def _assert_reconciled(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            sufficiency_id = connection.execute(
                text(
                    "select is_identity, column_default "
                    "from information_schema.columns "
                    "where table_schema = 'public' "
                    "and table_name = 'dependency_evidence_sufficiencies' "
                    "and column_name = 'id'"
                )
            ).mappings().one()
            assert sufficiency_id.is_identity == "NO"
            assert sufficiency_id.column_default == (
                "nextval('dependency_evidence_sufficiencies_id_seq'::regclass)"
            )

            evaluation = connection.execute(
                text(
                    "select is_nullable from information_schema.columns "
                    "where table_schema = 'public' "
                    "and table_name = 'external_report_releases' "
                    "and column_name = 'evaluation_context_json'"
                )
            ).scalar_one()
            assert evaluation == "YES"

            constraints = dict(
                connection.execute(
                    text(
                        "select conname, pg_get_constraintdef(oid, true) "
                        "from pg_constraint where conrelid in ("
                        "'dependency_events'::regclass, "
                        "'external_report_releases'::regclass)"
                    )
                ).all()
            )
            assert "event_source_kind" not in constraints
            assert "ck_dependency_events_source_kind" in constraints
            assert "evaluation_context_json IS NULL" in constraints[
                "ck_external_report_releases_evaluation_object"
            ]

            guard = connection.scalar(
                text(
                    "select pg_get_functiondef(oid) from pg_proc "
                    "where pronamespace = 'public'::regnamespace "
                    "and proname = 'reject_verbal_dependency_event_mutation'"
                )
            )
            assert "current_user = 'corridor_statement_retirement'" in guard
            assert connection.scalar(
                text(
                    "select count(*) from pg_proc "
                    "where pronamespace = 'public'::regnamespace "
                    "and proname = 'reject_verbal_statement_child_mutation'"
                )
            ) == 0
            assert connection.scalar(
                text(
                    "select count(*) from pg_trigger where tgname in ("
                    "'verbal_dependency_event_scopes_are_immutable', "
                    "'verbal_dependency_event_timings_are_immutable')"
                )
            ) == 0
    finally:
        engine.dispose()


def _install_historical_drift(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "insert into projects (id, slug, name, is_synthetic) values "
                    "(318001, 'schema-reconciliation', 'Schema reconciliation', true)"
                )
            )
            connection.execute(
                text(
                    "insert into documents "
                    "(id, project_id, sha256, filename, doc_type, parse_status) values "
                    "(318002, 318001, :sha, 'schema.pdf', 'minutes', 'parsed')"
                ),
                {"sha": "3" * 64},
            )
            connection.execute(
                text(
                    "insert into dependencies "
                    "(id, project_id, ref_code, dep_type, title) values "
                    "(318003, 318001, 'DEP-318003', "
                    "'utility_relocation', 'Utility')"
                )
            )
            connection.execute(
                text(
                    "insert into evidence_links "
                    "(id, dependency_id, document_id, page_no, quote, verified) values "
                    "(318004, 318003, 318002, 1, 'Historical Evidence.', true)"
                )
            )
            connection.execute(
                text(
                    "insert into dependency_evidence_sufficiencies "
                    "(id, dependency_id, evidence_link_id) values "
                    "(41, 318003, 318004)"
                )
            )
            connection.execute(
                text(
                    "alter table dependency_evidence_sufficiencies "
                    "alter column id drop default; "
                    "alter sequence dependency_evidence_sufficiencies_id_seq "
                    "owned by none; "
                    "drop sequence dependency_evidence_sufficiencies_id_seq; "
                    "alter table dependency_evidence_sufficiencies "
                    "alter column id add generated by default as identity "
                    "(start with 7000); "
                    "select setval("
                    "pg_get_serial_sequence("
                    "'dependency_evidence_sufficiencies', 'id'), "
                    "7000, true); "

                    "alter table external_report_releases "
                    "alter column evaluation_context_json set not null; "
                    "alter table external_report_releases drop constraint "
                    "ck_external_report_releases_evaluation_object; "
                    "alter table external_report_releases add constraint "
                    "ck_external_report_releases_evaluation_object "
                    "check (jsonb_typeof(evaluation_context_json) = 'object'); "

                    "alter table dependency_events drop constraint event_source_kind;"
                )
            )
            connection.execute(
                text(
                    """
                    create or replace function reject_verbal_dependency_event_mutation()
                    returns trigger language plpgsql as $$
                    begin
                        if tg_op = 'TRUNCATE' then
                            raise exception 'verbal dependency events are append-only'
                                using errcode = '23514';
                        end if;
                        if tg_op = 'DELETE' and old.source_kind = 'verbal' then
                            raise exception 'verbal dependency events are append-only'
                                using errcode = '23514';
                        end if;
                        if tg_op = 'UPDATE' and (
                            old.source_kind = 'verbal' or new.source_kind = 'verbal'
                        ) then
                            raise exception 'verbal dependency events are append-only'
                                using errcode = '23514';
                        end if;
                        return case when tg_op = 'DELETE' then old else new end;
                    end;
                    $$;

                    create function reject_verbal_statement_child_mutation()
                    returns trigger language plpgsql as $$
                    declare target_event_id bigint; target_source text;
                    begin
                        target_event_id := case
                            when tg_op = 'DELETE' then old.event_id else new.event_id
                        end;
                        select source_kind into target_source
                        from dependency_events where id = target_event_id;
                        if target_source = 'verbal' then
                            raise exception 'verbal dependency events are append-only'
                                using errcode = '23514';
                        end if;
                        return case when tg_op = 'DELETE' then old else new end;
                    end;
                    $$;

                    create trigger verbal_dependency_event_scopes_are_immutable
                    before update or delete on dependency_event_scopes
                    for each row execute function
                        reject_verbal_statement_child_mutation();
                    create trigger verbal_dependency_event_timings_are_immutable
                    before update or delete on dependency_event_timings
                    for each row execute function
                        reject_verbal_statement_child_mutation();
                    """
                )
            )
    finally:
        engine.dispose()


def test_schema_reconciliation_is_one_fresh_linear_head():
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == [HEAD]

    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="schema_reconciliation_fresh_",
        migration_revision=HEAD,
    ) as database:
        database_url = make_url(settings.database_url).set(database=database.name)
        rendered = database_url.render_as_string(hide_password=False)
        _assert_reconciled(rendered)


def test_historical_predecessor_converges_and_rehearses_downgrade_reupgrade():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="schema_reconciliation_predecessor_reference_",
        migration_revision=PREDECESSOR,
    ) as predecessor:
        predecessor_url = make_url(settings.database_url).set(
            database=predecessor.name
        )
        predecessor_schema = _schema(
            predecessor_url.render_as_string(hide_password=False)
        )

    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="schema_reconciliation_head_reference_",
        migration_revision=HEAD,
    ) as fresh:
        fresh_url = make_url(settings.database_url).set(database=fresh.name)
        head_schema = _schema(fresh_url.render_as_string(hide_password=False))

    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="schema_reconciliation_historical_",
        migration_revision=PREDECESSOR,
    ) as historical:
        historical_url = make_url(settings.database_url).set(database=historical.name)
        rendered = historical_url.render_as_string(hide_password=False)
        _install_historical_drift(rendered)

        _upgrade(rendered, HEAD)
        _assert_reconciled(rendered)
        assert _schema(rendered) == head_schema

        engine = create_engine(rendered)
        try:
            with engine.connect() as connection:
                assert connection.scalar(
                    text(
                        "select dependency_id from "
                        "dependency_evidence_sufficiencies where id = 41"
                    )
                ) == 318003
                assert connection.execute(
                    text(
                        "select last_value, is_called from "
                        "dependency_evidence_sufficiencies_id_seq"
                    )
                ).one() == (7000, True)
                assert connection.scalar(
                    text(
                        "select nextval(pg_get_serial_sequence("
                        "'dependency_evidence_sufficiencies', 'id'))"
                    )
                ) == 7001
        finally:
            engine.dispose()

        _downgrade(rendered, PREDECESSOR)
        assert _schema(rendered) == predecessor_schema
        _upgrade(rendered, HEAD)
        _assert_reconciled(rendered)
        assert _schema(rendered) == head_schema
