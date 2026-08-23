"""Fresh and predecessor rehearsals for Dependency Abstention inputs.

The outcome table was already append-only. This successor preserves every
historical Abstention while giving new runs an exact input fingerprint; editing
a stamped predecessor was rejected because deployed databases already carry it.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from corridor.config import settings
from corridor.m8_acceptance_database import provision_disposable_postgres


ROOT = Path(__file__).resolve().parents[1]
PREDECESSOR = "d257f2b9c537"
HEAD = "e314a3d8c6f2"


def _upgrade(database_url: str, target: str) -> None:
    completed = subprocess.run(
        ["uv", "run", "alembic", "upgrade", target],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def _columns(connection) -> list[str]:
    return connection.execute(
        text(
            "select column_name from information_schema.columns "
            "where table_schema = 'public' "
            "and table_name = 'dependency_admission_outcomes' "
            "order by ordinal_position"
        )
    ).scalars().all()


def _constraints(connection) -> set[str]:
    return set(
        connection.execute(
            text(
                "select conname from pg_constraint "
                "where conrelid = 'dependency_admission_outcomes'::regclass"
            )
        ).scalars()
    )


def test_dependency_abstention_inputs_are_on_one_fresh_linear_head():
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == [HEAD]

    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="issue314_dependency_fresh_",
    ) as database:
        with database.session_factory().connection() as connection:
            assert connection.scalar(text("select version_num from alembic_version")) == HEAD
            assert _columns(connection)[-2:] == [
                "eligibility_json",
                "eligibility_sha256",
            ]
            assert {
                "ck_dependency_admission_outcome_eligibility_sha256",
                "ck_dependency_admission_outcome_eligibility_shape",
            }.issubset(_constraints(connection))


def test_predecessor_upgrade_preserves_historical_dependency_abstention():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="issue314_dependency_predecessor_",
        migration_revision=PREDECESSOR,
    ) as database:
        database_url = make_url(settings.database_url).set(database=database.name)
        rendered = database_url.render_as_string(hide_password=False)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                assert "eligibility_json" not in _columns(connection)
                connection.execute(
                    text(
                        "insert into projects (id, slug, name, is_synthetic) "
                        "values (314001, 'issue314-predecessor', "
                        "'Issue 314 predecessor', true)"
                    )
                )
                connection.execute(
                    text(
                        "insert into documents "
                        "(id, project_id, sha256, filename, doc_type, "
                        "numbering_scheme, parse_status) values "
                        "(314002, 314001, :sha, 'predecessor.pdf', "
                        "'matrix', 'project-unique', 'parsed')"
                    ),
                    {"sha": "a" * 64},
                )
                connection.execute(
                    text(
                        "insert into candidates "
                        "(id, project_id, kind, payload_json, source_document_id, "
                        "source_pages, prompt_version, citations_verified, state) "
                        "values (314003, 314001, 'dependency', "
                        "'{\"kind\":\"dependency\",\"fields\":{}}', "
                        "314002, array[1], 'historical', true, 'pending')"
                    )
                )
                connection.execute(
                    text(
                        "insert into policy_runs "
                        "(id, project_id, family, policy_approval_id, policy_version, "
                        "policy_sha256, abstention_reason_version, applied_count, "
                        "abstained_count) values "
                        "(314004, 314001, 'dependency-admission', null, "
                        "'dependency-admission-v1', :sha, "
                        "'dependency-admission-abstentions-v3', 0, 1)"
                    ),
                    {"sha": "b" * 64},
                )
                connection.execute(
                    text(
                        "insert into dependency_admission_outcomes "
                        "(id, policy_run_id, family, candidate_id, outcome, reason) "
                        "values (314005, 314004, 'dependency-admission', 314003, "
                        "'abstained', 'revisions_disagree_on_party')"
                    )
                )
            engine.dispose()
            _upgrade(rendered, HEAD)
            engine = create_engine(database_url)
            with engine.connect() as connection:
                assert connection.scalar(text("select version_num from alembic_version")) == HEAD
                assert _columns(connection)[-2:] == [
                    "eligibility_json",
                    "eligibility_sha256",
                ]
                assert {
                    "ck_dependency_admission_outcome_eligibility_sha256",
                    "ck_dependency_admission_outcome_eligibility_shape",
                }.issubset(_constraints(connection))
                row = connection.execute(
                    text(
                        "select id, outcome, reason, eligibility_json, "
                        "eligibility_sha256 from dependency_admission_outcomes "
                        "where id = 314005"
                    )
                ).mappings().one()
                assert dict(row) == {
                    "id": 314005,
                    "outcome": "abstained",
                    "reason": "revisions_disagree_on_party",
                    "eligibility_json": None,
                    "eligibility_sha256": None,
                }
        finally:
            engine.dispose()
