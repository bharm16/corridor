"""Permanent database baseline and supported-upgrade contract."""

from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
import subprocess

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from corridor.config import settings
from corridor.m8_acceptance_database import provision_disposable_postgres
from alembic.config import Config
from alembic.script import ScriptDirectory

from corridor.migrations import policy
from corridor.product_proving_database import fingerprint_database_url
from corridor.report_release import retrieve_released_external_report


ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "src" / "corridor" / "migrations" / "baseline_versions"
SCHEMA_BUILDER = "b7d3f9a1c2e5"
SPREADSHEET_HEAD = "0ca809014df7"
FACT_HEAD = "444758f7b4a7"
RELEASE_HEAD = "d430a1b2c3d4"
APPEND_HEAD = "1142da5be661"
PAGE_INVENTORY_HEAD = "1d2e3f4a5b6c"
IMMUTABLE_PROPOSAL_HEAD = "961bd259310f"
PROSE_HEAD = "437e8c9a0b1d"
DECISION_HEAD = "20c7d970be63"
CURRENT_RECORD_HEAD = "8fc4c747b2d9"
STRUCTURED_FACT_HEAD = "7e1b2c3d4f50"
SUBJECT_RESOLUTION_HEAD = "453a1b2c3d4e"
PROSE_ACCOUNTING_HEAD = "452c7d8e9f10"
RENDER_HEAD = "2e3f4a5b6c7d"
CLASS_B_RETENTION_HEAD = "7a3e91c4d8b2"
TOKEN_LAYER_HEAD = "3f4a5b6c7d8e"
HUMAN_DECISION_HEAD = "4a5b6c7d8e9f"
VERBAL_SEGMENT_HEAD = "5b6c7d8e9f01"
STATEMENT_TIMING_HEAD = "6c7d8e9f0a12"
COORDINATE_COMMAND_HEAD = "7d8e9f0a1b23"
SUPPORTED_HEAD = "a1c4e7b0d2f3"
CURRENT_HEAD = "b2d5f8a1c4e7"
EXPECTED_SCHEMA_SHA256 = (
    "72a4a1d613bfb46f7d4ec78f6b5cb671fa13d0447474b75bfe9f3da2fe1556a7"
)

pytestmark = [pytest.mark.slow, pytest.mark.migration]


def test_the_executable_migration_window_matches_the_recorded_policy():
    """Assert the revision graph, not a list of filenames.

    The guard this replaces enumerated the permitted filenames, so every
    migration-bearing change added its name and passed. It could not fail as
    the chain grew, which is how 2 executable revisions became 23 after #423
    bounded them.
    """

    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option(
        "script_location", "src/corridor/migrations"
    )
    script = ScriptDirectory.from_config(config)

    heads = script.get_heads()
    assert list(heads) == [policy.CURRENT_HEAD], (
        "the executable graph must have exactly one head, and it must be the "
        f"revision the policy records: {heads}"
    )

    revisions = list(script.walk_revisions())
    by_id = {revision.revision: revision for revision in revisions}
    assert policy.SCHEMA_BUILDER in by_id, "the fresh-install builder must exist"
    assert policy.SUPPORTED_FROM_REVISION in by_id, (
        "the supported revision must exist in the executable graph"
    )

    # With the chain consolidated the supported revision *is* the head, so
    # there is nothing after it to walk; `iterate_revisions` excludes its
    # lower bound and returns an empty set.
    reachable = {
        revision.revision
        for revision in script.iterate_revisions(
            policy.CURRENT_HEAD, policy.SUPPORTED_FROM_REVISION
        )
    }
    if policy.SUPPORTED_FROM_REVISION != policy.CURRENT_HEAD:
        assert policy.CURRENT_HEAD in reachable

    # No executable revision sits outside the builder's line: history before
    # the supported revision is source bytes, not an upgrade path.
    unreachable = {
        revision.revision
        for revision in revisions
        if revision.revision not in reachable
        and revision.revision
        not in {policy.SCHEMA_BUILDER, policy.SUPPORTED_FROM_REVISION}
        and revision.revision != policy.COMPATIBILITY_MARKER
    }
    assert unreachable == set(), (
        f"unrelated executable history remains: {sorted(unreachable)}"
    )

    # The ratchet. `UNRELEASED_EDGES` may fall and must never rise; a change
    # needing another revision folds into the current unreleased transition,
    # or consolidates the chain and lowers the recorded number.
    # `iterate_revisions` excludes its lower bound, so the reachable set is
    # exactly the transitions after the supported revision.
    edges = len(reachable)
    assert edges <= max(policy.UNRELEASED_EDGES, policy.UNRELEASED_EDGE_TARGET), (
        f"the executable chain grew to {edges} transitions after "
        f"{policy.SUPPORTED_FROM_REVISION}, above the permitted "
        f"{max(policy.UNRELEASED_EDGES, policy.UNRELEASED_EDGE_TARGET)}. Fold "
        "the change into the current unreleased transition, or consolidate "
        "and lower the policy."
    )
    assert edges >= policy.UNRELEASED_EDGES, (
        f"the chain is down to {edges} transitions; lower "
        f"UNRELEASED_EDGES in the migration policy to hold the gain."
    )
    assert policy.UNRELEASED_EDGE_TARGET == 1, (
        "ADR-0065's window is one supported transition; the target does not move"
    )


def test_fresh_database_matches_the_released_schema_exactly():
    configured = make_url(settings.database_url)
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="corridor_baseline_fresh_",
    ) as database:
        database_url = configured.set(database=database.name).render_as_string(
            hide_password=False
        )
        fingerprint = fingerprint_database_url(database_url)
        security = _statement_retirement_security(database.session_factory)
        source_append = _source_append_security(database.session_factory)

    assert database.migration_head == CURRENT_HEAD
    assert fingerprint.schema_sha256 == EXPECTED_SCHEMA_SHA256
    assert source_append == SOURCE_APPEND_SECURED
    assert security == {
        "can_login": False,
        "inherits": False,
        "function_owner": "corridor_statement_retirement",
        "function_public_execute": False,
        "tables": {
            "commitment_lineages": ("DELETE", "SELECT"),
            "dependency_event_evidence": ("DELETE", "SELECT"),
            "dependency_event_scope_decisions": ("DELETE", "SELECT"),
            "dependency_event_scopes": ("DELETE", "SELECT"),
            "dependency_event_timings": ("DELETE", "SELECT"),
            "dependency_events": ("DELETE", "SELECT"),
            "evidence_links": ("DELETE", "SELECT"),
            "legacy_ledger_archives": ("DELETE", "SELECT"),
            "projects": ("DELETE", "SELECT"),
            "work_decisions": ("SELECT",),
        },
    }


def test_the_supported_database_upgrades_to_the_current_head_and_back():
    """The one supported transition, proved on its exact transformed rows.

    b2d5f8a1c4e7 transforms privileges and adds one relation, not data: it
    takes the raw source-table writes back from the runtime capabilities and
    hands the source-append role its commands (#492), and it creates the
    Support Assessment tables that only the fifth command writes (#530).  A
    database standing at the supported revision must cross that transition
    in both directions.
    """

    configured = make_url(settings.database_url)
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="corridor_baseline_transition_",
        migration_revision=SUPPORTED_HEAD,
    ) as database:
        database_url = configured.set(database=database.name)
        assert database.migration_head == SUPPORTED_HEAD
        assert _source_append_security(database.session_factory) == SOURCE_APPEND_OPEN

        upgraded = _alembic(database_url, "upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stderr
        assert _migration_head(database.session_factory) == CURRENT_HEAD
        assert _source_append_security(database.session_factory) == SOURCE_APPEND_SECURED

        downgraded = _alembic(database_url, "downgrade", SUPPORTED_HEAD)
        assert downgraded.returncode == 0, downgraded.stderr
        assert _migration_head(database.session_factory) == SUPPORTED_HEAD
        assert _source_append_security(database.session_factory) == SOURCE_APPEND_OPEN


def test_downgrade_across_the_consolidated_baseline_is_unsupported():
    configured = make_url(settings.database_url)
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="corridor_baseline_downgrade_",
    ) as database:
        database_url = configured.set(database=database.name)
        # The consolidated baseline is the whole executable graph, so the
        # only downgrade target left is `base`.
        completed = _alembic(database_url, "downgrade", "base")

    assert completed.returncode != 0
    assert (
        "consolidated schema baseline downgrade is unsupported"
        in completed.stderr
    )


def _project_row(session_factory):
    with session_factory() as session:
        return session.execute(
            text(
                "select slug, name, is_synthetic from projects "
                "where slug = 'baseline-bridge'"
            )
        ).one()


def _seed_legacy_release_and_artifact(session, project_id: int) -> None:
    pdf_bytes = b"%PDF-1.7\nlegacy release\n%%EOF"
    digest = sha256(pdf_bytes).hexdigest()
    artifact_id = session.scalar(
        text(
            "insert into external_report_artifacts ("
            "project_id, artifact_name, format, pdf_bytes, pdf_sha256, "
            "evaluated_on, ruleset_version, evaluation_context_json, "
            "provenance_mode, record_context_json"
            ") values ("
            ":project_id, 'artifact-backed.pdf', 'pdf', :pdf_bytes, :digest, "
            "date '2026-08-31', 'v0.4', '{}'::jsonb, "
            "'all-supported-sources', '{}'::jsonb"
            ") returning id"
        ),
        {"project_id": project_id, "pdf_bytes": pdf_bytes, "digest": digest},
    )
    assert artifact_id is not None
    session.execute(
        text(
            "insert into external_report_releases ("
            "project_id, artifact_name, format, pdf_bytes, pdf_sha256, "
            "evaluated_on, ruleset_version, evaluation_context_json, "
            "provenance_mode, record_context_json, released_by, released_by_display"
            ") values ("
            ":project_id, 'legacy-release.pdf', 'pdf', :pdf_bytes, :digest, "
            "date '2026-08-31', 'v0.4', '{}'::jsonb, "
            "'all-supported-sources', '{\"dependencies\": []}'::jsonb, "
            "'local:legacy', 'Legacy Releaser'"
            ")"
        ),
        {"project_id": project_id, "pdf_bytes": pdf_bytes, "digest": digest},
    )


def _legacy_release_row(session_factory):
    with session_factory() as session:
        return session.execute(
            text(
                "select artifact_id, artifact_name, pdf_bytes, pdf_sha256, "
                "evaluation_context_json, record_context_json, released_by "
                "from external_report_releases "
                "where artifact_name = 'legacy-release.pdf'"
            )
        ).one()


def _assert_legacy_release_reader(session_factory, project_id: int) -> None:
    with session_factory() as session:
        release_id = session.scalar(
            text(
                "select id from external_report_releases "
                "where artifact_name = 'legacy-release.pdf'"
            )
        )
        release = retrieve_released_external_report(
            session, project_id, int(release_id)
        )
        assert release.content_storage == "legacy"
        assert release.artifact_id is None
        assert release.pdf_bytes == b"%PDF-1.7\nlegacy release\n%%EOF"
        assert release.record_context_json == {"dependencies": []}
        assert release.digest_is_valid is True


def _insert_artifact_reference_release(session_factory) -> None:
    with session_factory.begin() as session:
        project_id = session.scalar(
            text("select id from projects where slug = 'baseline-bridge'")
        )
        artifact_id = session.scalar(
            text(
                "select id from external_report_artifacts "
                "where artifact_name = 'artifact-backed.pdf'"
            )
        )
        artifact_digest = session.scalar(
            text(
                "select pdf_sha256 from external_report_artifacts where id = :artifact_id"
            ),
            {"artifact_id": artifact_id},
        )
        release_id = session.scalar(
            text(
                "insert into external_report_releases ("
                "project_id, artifact_id, artifact_name, format, pdf_sha256, "
                "evaluated_on, ruleset_version, provenance_mode, "
                "released_by, released_by_display"
                ") values ("
                ":project_id, :artifact_id, 'artifact-backed.pdf', 'pdf', :digest, "
                "date '2026-08-31', 'v0.4', 'all-supported-sources', "
                "'local:new', 'New Releaser'"
                ") returning id"
            ),
            {
                "project_id": project_id,
                "artifact_id": artifact_id,
                "digest": artifact_digest,
            },
        )
        stored = session.execute(
            text(
                "select pdf_bytes, evaluation_context_json, record_context_json "
                "from external_report_releases where id = :release_id"
            ),
            {"release_id": release_id},
        ).one()
        assert tuple(stored) == (None, None, None)
        duplicated_artifact_id = session.scalar(
            text(
                "insert into external_report_artifacts ("
                "project_id, artifact_name, format, pdf_bytes, pdf_sha256, "
                "evaluated_on, ruleset_version, evaluation_context_json, "
                "provenance_mode, record_context_json"
                ") select project_id, 'duplicate-owner.pdf', format, pdf_bytes, "
                "pdf_sha256, evaluated_on, ruleset_version, evaluation_context_json, "
                "provenance_mode, record_context_json "
                "from external_report_artifacts where id = :artifact_id "
                "returning id"
            ),
            {"artifact_id": artifact_id},
        )
        with pytest.raises(Exception, match="content owner"):
            with session.begin_nested():
                session.execute(
                    text(
                        "insert into external_report_releases ("
                        "project_id, artifact_id, artifact_name, format, pdf_bytes, "
                        "pdf_sha256, evaluated_on, ruleset_version, "
                        "evaluation_context_json, provenance_mode, "
                        "record_context_json, released_by, released_by_display"
                        ") select :project_id, :artifact_id, 'duplicate-owner.pdf', "
                        "format, pdf_bytes, pdf_sha256, evaluated_on, ruleset_version, "
                        "evaluation_context_json, provenance_mode, record_context_json, "
                        "'local:invalid-copy', 'Invalid Copy' "
                        "from external_report_artifacts where id = :artifact_id"
                    ),
                    {
                        "project_id": project_id,
                        "artifact_id": duplicated_artifact_id,
                    },
                )
        with pytest.raises(Exception, match="content_owner"):
            with session.begin_nested():
                session.execute(
                    text(
                        "insert into external_report_releases ("
                        "project_id, artifact_name, format, pdf_sha256, "
                        "evaluated_on, ruleset_version, provenance_mode, "
                        "released_by, released_by_display"
                        ") values ("
                        ":project_id, 'missing-content-owner.pdf', 'pdf', :digest, "
                        "date '2026-08-31', 'v0.4', 'all-supported-sources', "
                        "'local:invalid', 'Invalid Releaser'"
                        ")"
                    ),
                    {"project_id": project_id, "digest": artifact_digest},
                )


def _migration_head(session_factory) -> str:
    with session_factory() as session:
        return str(session.scalar(text("select version_num from alembic_version")))


def _source_segment_rows(session_factory) -> list[tuple]:
    with session_factory() as session:
        return list(
            session.execute(
                text(
                    "select id, project_id, document_id, kind, exact_text, cell_range "
                    "from source_segments order by id"
                )
            ).all()
        )


def _fact_rows(session_factory) -> list[tuple]:
    with session_factory() as session:
        return list(
            session.execute(
                text(
                    "select id, document_id, extraction_run_id, fact_type, "
                    "subject_key, text_value from facts order by id"
                )
            ).all()
        )


SOURCE_TABLES = (
    "source_segments",
    "facts",
    "fact_sources",
    "fact_applies_to",
    "fact_closure_results",
    "fact_closure_sources",
    "fact_statement_timings",
    "extracted_proposals",
    "extracted_proposal_facts",
    "source_fact_append_receipts",
)
# Born in b2d5f8a1c4e7 (#530): absent before it, appended only through the
# command after it.
SUPPORT_TABLES = (
    "support_assessments",
    "support_assessment_sources",
)
SOURCE_APPEND_COMMANDS = (
    "append_source_segments",
    "append_fact",
    "append_extracted_proposal",
    "append_source_fact_receipt",
    "append_support_assessment",
)
# Before b2d5f8a1c4e7: the runtime capabilities write the source tables raw,
# no append command exists, and the Support Assessment relation does not
# exist (None, as distinct from a table nobody may write).
SOURCE_APPEND_OPEN = {
    "runtime_insert": {
        **{table: ("corridor_web", "corridor_worker") for table in SOURCE_TABLES},
        **{table: None for table in SUPPORT_TABLES},
    },
    "runtime_select": {
        **{table: ("corridor_web", "corridor_worker") for table in SOURCE_TABLES},
        **{table: None for table in SUPPORT_TABLES},
    },
    "commands": {},
}
# After it: nobody writes them raw, both capabilities read them all, and
# every command is owned by the source-append role, hidden from PUBLIC, and
# callable by both capabilities.
SOURCE_APPEND_SECURED = {
    "runtime_insert": {table: () for table in SOURCE_TABLES + SUPPORT_TABLES},
    "runtime_select": {
        table: ("corridor_web", "corridor_worker")
        for table in SOURCE_TABLES + SUPPORT_TABLES
    },
    "commands": {
        command: {
            "owner": "corridor_source_append",
            "public_execute": False,
            "execute": ("corridor_web", "corridor_worker"),
        }
        for command in SOURCE_APPEND_COMMANDS
    },
}


def _runtime_table_privilege(session, table: str, privilege: str):
    """Which runtime capabilities hold one privilege, or None if no such table."""

    if session.scalar(text("select to_regclass(:table)"), {"table": table}) is None:
        return None
    return tuple(
        session.scalars(
            text(
                "select r from unnest(array['corridor_web', 'corridor_worker']) as r "
                "where has_table_privilege(r, :table, :privilege) order by r"
            ),
            {"table": table, "privilege": privilege},
        ).all()
    )


def _source_append_security(session_factory) -> dict:
    with session_factory() as session:
        runtime_insert = {
            table: _runtime_table_privilege(session, table, "insert")
            for table in SOURCE_TABLES + SUPPORT_TABLES
        }
        runtime_select = {
            table: _runtime_table_privilege(session, table, "select")
            for table in SOURCE_TABLES + SUPPORT_TABLES
        }
        commands = {}
        for name in SOURCE_APPEND_COMMANDS:
            row = session.execute(
                text(
                    "select owner.rolname, "
                    "has_function_privilege('public', function.oid, 'execute'), "
                    "has_function_privilege('corridor_web', function.oid, 'execute'), "
                    "has_function_privilege('corridor_worker', function.oid, 'execute') "
                    "from pg_proc function "
                    "join pg_namespace namespace on namespace.oid = function.pronamespace "
                    "join pg_roles owner on owner.oid = function.proowner "
                    "where namespace.nspname = 'public' and function.proname = :name"
                ),
                {"name": name},
            ).first()
            if row is None:
                continue
            commands[name] = {
                "owner": str(row[0]),
                "public_execute": bool(row[1]),
                "execute": tuple(
                    role
                    for role, granted in (
                        ("corridor_web", row[2]),
                        ("corridor_worker", row[3]),
                    )
                    if granted
                ),
            }
    return {
        "runtime_insert": runtime_insert,
        "runtime_select": runtime_select,
        "commands": commands,
    }


def _statement_retirement_security(session_factory) -> dict:
    with session_factory() as session:
        can_login, inherits = session.execute(
            text(
                "select rolcanlogin, rolinherit from pg_roles "
                "where rolname = 'corridor_statement_retirement'"
            )
        ).one()
        function_owner = session.scalar(
            text(
                "select owner.rolname from pg_proc function "
                "join pg_namespace namespace on namespace.oid = function.pronamespace "
                "join pg_roles owner on owner.oid = function.proowner "
                "where namespace.nspname = 'public' "
                "and function.proname = 'purge_external_party_statement_rows'"
            )
        )
        public_execute = session.scalar(
            text(
                "select has_function_privilege("
                "'public', "
                "'public.purge_external_party_statement_rows(bigint,text)', "
                "'execute')"
            )
        )
        rows = session.execute(
            text(
                "select table_name, privilege_type "
                "from information_schema.role_table_grants "
                "where table_schema = 'public' "
                "and grantee = 'corridor_statement_retirement' "
                "order by table_name, privilege_type"
            )
        ).all()
    tables: dict[str, list[str]] = {}
    for table_name, privilege in rows:
        tables.setdefault(str(table_name), []).append(str(privilege))
    return {
        "can_login": bool(can_login),
        "inherits": bool(inherits),
        "function_owner": str(function_owner),
        "function_public_execute": bool(public_execute),
        "tables": {name: tuple(values) for name, values in tables.items()},
    }


def _alembic(database_url, command: str, target: str):
    environment = {
        **os.environ,
        "DATABASE_URL": database_url.render_as_string(hide_password=False),
    }
    return subprocess.run(
        ["uv", "run", "alembic", command, target],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
