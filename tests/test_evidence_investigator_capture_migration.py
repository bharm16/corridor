"""Fresh and predecessor rehearsals for cutoff-correct outcome capture storage.

A stamped development database can report the expected head while missing the
append-only triggers that keep a frozen case's captured association immutable.
These tests prove the fresh schema objects and that a populated predecessor
upgrade both preserves prior rows and refuses to erase retained captures.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from corridor.config import settings
from corridor.m8_acceptance_database import provision_disposable_postgres


pytestmark = [pytest.mark.slow, pytest.mark.migration]

ROOT = Path(__file__).resolve().parents[1]
PREDECESSOR = "e361f1a2b3c4"
HEAD = "b7d3f9a1c2e5"


def _run_alembic(database_url: str, *args: str):
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )


def _database_url(database) -> str:
    return (
        make_url(settings.database_url)
        .set(database=database.name)
        .render_as_string(hide_password=False)
    )


def test_capture_schema_is_one_fresh_linear_head():
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == [HEAD]

    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="capture_fresh_",
        reuse_migrated_template=True,
    ) as database:
        engine = create_engine(_database_url(database))
        try:
            with engine.connect() as connection:
                assert (
                    connection.scalar(text("select version_num from alembic_version"))
                    == HEAD
                )
                tables = set(
                    connection.scalars(
                        text(
                            "select table_name from information_schema.tables "
                            "where table_schema='public' "
                            "and table_name like 'evidence_investigation_capture_%'"
                        )
                    ).all()
                )
                assert tables == {
                    "evidence_investigation_capture_contracts",
                    "evidence_investigation_capture_results",
                }
                triggers = set(
                    connection.scalars(
                        text(
                            "select tgname from pg_trigger where not tgisinternal "
                            "and tgname like 'evidence_investigation_capture_%'"
                        )
                    ).all()
                )
                assert {
                    "evidence_investigation_capture_contracts_are_immutable",
                    "evidence_investigation_capture_results_are_immutable",
                }.issubset(triggers)
        finally:
            engine.dispose()


def test_predecessor_upgrade_preserves_rows_and_protects_retained_captures():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="capture_predecessor_",
        reuse_migrated_template=True,
        migration_revision=PREDECESSOR,
    ) as database:
        database_url = _database_url(database)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                project_id = connection.scalar(
                    text(
                        "insert into projects (slug, name, is_synthetic) "
                        "values ('capture-predecessor', 'Capture predecessor', true) "
                        "returning id"
                    )
                )
        finally:
            engine.dispose()
        bound = database.session_factory.kw.get("bind")
        if bound is not None:
            bound.dispose()

        upgraded = _run_alembic(database_url, "upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr

        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                assert (
                    connection.scalar(
                        text("select count(*) from projects where id=:id"),
                        {"id": project_id},
                    )
                    == 1
                )
                connection.execute(
                    text(
                        "insert into evidence_investigation_capture_contracts ("
                        "public_id, project_id, cohort_id, contract_sha256, model, "
                        "prompt_version, prompt_sha256, adapter_contract_version, "
                        "tool_contract_version, validator_version, baseline_identity, "
                        "window_start, cutoff_at, protection_end, history_retained_from, "
                        "missing_label_policy, member_case_public_ids_json, "
                        "contract_json, declared_by) values ("
                        "'contract-pred', :project_id, 'cohort-pred', :sha, 'model-x', "
                        "'prompt-x', :psha, 'adapter-x', 'tool-x', 'validator-x', "
                        "'baseline-x', now(), now(), now(), now(), 'remain_missing', "
                        "'[]'::jsonb, '{}'::jsonb, 'local:capture-tester')"
                    ),
                    {"project_id": project_id, "sha": "c" * 64, "psha": "d" * 64},
                )
            with engine.begin() as connection:
                with pytest.raises(Exception):
                    connection.execute(
                        text(
                            "update evidence_investigation_capture_contracts "
                            "set declared_by = 'rewritten' where contract_sha256 = :sha"
                        ),
                        {"sha": "c" * 64},
                    )
        finally:
            engine.dispose()

        refused = _run_alembic(database_url, "downgrade", PREDECESSOR)
        assert refused.returncode != 0
        assert "cannot erase retained evidence_investigation_capture_contracts" in (
            refused.stdout + refused.stderr
        )
