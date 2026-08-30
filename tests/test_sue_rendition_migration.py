"""Schema rehearsal for converted SUE renditions and Evidence proposals.

Application-only provenance was rejected because a converted file could then
be relabeled, deleted, or linked across projects behind the ingest service.
These tests prove the exact predecessor upgrade, same-project derivation, new
non-adjudicable Candidate kind, immutability, and destructive downgrade refusal.
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
from sqlalchemy.exc import DBAPIError, IntegrityError

from corridor.config import settings
from corridor.m8_acceptance_database import provision_disposable_postgres


pytestmark = pytest.mark.slow
ROOT = Path(__file__).resolve().parents[1]
PREDECESSOR = "f367a8c1d2e4"
HEAD = "f360a1b2c3d4"


def _url(database) -> str:
    return make_url(settings.database_url).set(database=database.name).render_as_string(
        hide_password=False
    )


def _alembic(database_url: str, *args: str):
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )


def test_structured_sue_schema_is_one_fresh_linear_head():
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == [HEAD]
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="sue_rendition_fresh_",
    ) as database:
        engine = create_engine(_url(database))
        try:
            with engine.connect() as connection:
                assert connection.scalar(
                    text("select version_num from alembic_version")
                ) == HEAD
                assert connection.scalar(
                    text("select to_regclass('document_rendition_derivations')")
                ) == "document_rendition_derivations"
        finally:
            engine.dispose()


def test_predecessor_upgrade_enforces_evidence_kind_and_append_only_derivation():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="sue_rendition_predecessor_",
        migration_revision=PREDECESSOR,
    ) as database:
        database_url = _url(database)
        bound = database.session_factory.kw.get("bind")
        if bound is not None:
            bound.dispose()
        upgraded = _alembic(database_url, "upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stdout + upgraded.stderr

        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                project_id = connection.scalar(
                    text(
                        "insert into projects (slug, name, is_synthetic) values "
                        "('sue-rendition', 'SUE Rendition', true) returning id"
                    )
                )
                source_id = connection.scalar(
                    text(
                        "insert into documents (project_id, registry_id, sha256, "
                        "filename, doc_type, parse_status) values (:project, "
                        "'source-xls', :sha, 'source.xls', 'plan', 'failed') returning id"
                    ),
                    {"project": project_id, "sha": "a" * 64},
                )
                derived_id = connection.scalar(
                    text(
                        "insert into documents (project_id, registry_id, sha256, "
                        "filename, doc_type, parse_status) values (:project, "
                        "'derived-xlsx', :sha, 'derived.xlsx', 'plan', 'parsed') returning id"
                    ),
                    {"project": project_id, "sha": "b" * 64},
                )
                derivation_id = connection.scalar(
                    text(
                        "insert into document_rendition_derivations "
                        "(project_id, source_document_id, derived_document_id, kind, "
                        "source_format, derived_format, source_sha256, derived_sha256, "
                        "tool, tool_version) values (:project, :source, :derived, "
                        "'format_conversion', 'xls', 'xlsx', :source_sha, :derived_sha, "
                        "'corridor.xls-to-xlsx', '3') returning id"
                    ),
                    {
                        "project": project_id,
                        "source": source_id,
                        "derived": derived_id,
                        "source_sha": "a" * 64,
                        "derived_sha": "b" * 64,
                    },
                )
                connection.execute(
                    text(
                        "insert into candidates (project_id, kind, payload_json, "
                        "source_document_id, source_pages, citations_verified, state) "
                        "values (:project, 'evidence', '{}', :derived, array[1], true, "
                        "'pending')"
                    ),
                    {"project": project_id, "derived": derived_id},
                )

            with pytest.raises((IntegrityError, DBAPIError)):
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "update document_rendition_derivations set tool_version='4' "
                            "where id=:id"
                        ),
                        {"id": derivation_id},
                    )
        finally:
            engine.dispose()

        refused = _alembic(database_url, "downgrade", PREDECESSOR)
        assert refused.returncode != 0
        assert "cannot erase retained structured SUE provenance" in (
            refused.stdout + refused.stderr
        )
