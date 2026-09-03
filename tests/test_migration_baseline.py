"""Permanent database baseline and supported-upgrade contract."""

from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256
import os
from pathlib import Path
import subprocess

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from corridor.config import settings
from corridor.facts import _fact_digest, _statement_timing_structured
from corridor.m8_acceptance_database import provision_disposable_postgres
from corridor.materializer import (
    materialize_quoted_statement_wording,
    materialize_typed_satellite,
)
from corridor.models import SourceSegment
from corridor.statement_values import StatementTiming
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
    "0ceb6beb7b07bdb1348642d05d342df8ce79f2c53079ec1b1cb15069d0bb10a4"
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

    b2d5f8a1c4e7 transforms privileges and adds relations, not data: it
    takes the raw source-table writes back from the runtime capabilities and
    hands the source-append role its commands (#492), it creates the
    Support Assessment tables that only the fifth command writes (#530), and
    it establishes the baseline/delta operating mode with its immutable
    adoption receipt and the guards that refuse a legacy accepted-value write
    for an adopted project (#520).  A database standing at the supported
    revision must cross that transition in both directions.
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


# --- The #512 recorded-verbal backfill, on its exact transformed rows -------

VERBAL_WORDS = "Equistar will submit the signed exhibit by March 2025."
VERBAL_RECORDER = "local:dana-fields"
# Supplied logical times. The recorded time this transition must preserve is
# written explicitly, so the assertion is exact and reads no wall clock.
VERBAL_RECORDED_AT = datetime(2025, 3, 3, 14, 30, tzinfo=timezone.utc)
VERBAL_CONVERSATION_DATE = date(2025, 3, 3)
SECOND_VERBAL_WORDS = "Oncor will set the pole the week of April 7, 2025."
SECOND_VERBAL_RECORDED_AT = datetime(2025, 4, 1, 9, 15, tzinfo=timezone.utc)
SECOND_VERBAL_CONVERSATION_DATE = date(2025, 4, 1)


def _seed_dual_written_verbal(
    session,
    *,
    slug: str,
    words: str,
    recorded_at: datetime,
    conversation_date: date,
    dependency_count: int,
    unreconcilable_fact_type: str | None = None,
) -> dict:
    """Write one verbal exactly as the #451 dual-write left it at the released head."""

    project_id = session.scalar(
        text(
            "insert into projects (slug, name, is_synthetic) "
            "values (:slug, :name, true) returning id"
        ),
        {"slug": slug, "name": slug},
    )
    dependency_ids = [
        session.scalar(
            text(
                "insert into dependencies (project_id, ref_code, dep_type, title) "
                "values (:project_id, :ref_code, 'utility_relocation', :title) "
                "returning id"
            ),
            {
                "project_id": project_id,
                "ref_code": f"DEP-{index + 1}",
                "title": f"{slug} constraint {index + 1}",
            },
        )
        for index in range(dependency_count)
    ]
    org_id = session.scalar(
        text(
            "insert into external_orgs (name, org_type) "
            "values (:name, 'utility') returning id"
        ),
        {"name": f"{slug} utility"},
    )
    lineage_id = session.scalar(
        text(
            "insert into commitment_lineages (project_id) values (:project_id) "
            "returning id"
        ),
        {"project_id": project_id},
    )
    statement_id = session.scalar(
        text(
            "insert into dependency_events ("
            "project_id, commitment_lineage_id, event_type, source_kind, "
            "affected_external_org_id, stated_external_org_id, attribution_state, "
            "stated_party, description, created_by, event_date, created_at"
            ") values ("
            ":project_id, :lineage_id, 'commitment', 'verbal', :org_id, :org_id, "
            "'resolved', :party, :words, :recorder, :conversation_date, "
            ":recorded_at) returning id"
        ),
        {
            "project_id": project_id,
            "lineage_id": lineage_id,
            "org_id": org_id,
            "party": f"{slug} utility",
            "words": words,
            "recorder": VERBAL_RECORDER,
            "conversation_date": conversation_date,
            "recorded_at": recorded_at,
        },
    )
    session.execute(
        text(
            "insert into dependency_event_timings ("
            "event_id, kind, text, precision, start_date, end_date"
            ") values (:event_id, 'new', 'March 3, 2025', 'day', "
            "date '2025-03-03', date '2025-03-03')"
        ),
        {"event_id": statement_id},
    )
    segment_id = session.scalar(
        text(
            "insert into source_segments ("
            "project_id, statement_id, kind, exact_text, content_sha256, ordinal"
            ") values ("
            ":project_id, :statement_id, 'recorded_verbal_statement', :words, "
            ":digest, 1) returning id"
        ),
        {
            "project_id": project_id,
            "statement_id": statement_id,
            "words": words,
            "digest": sha256(words.encode("utf-8")).hexdigest(),
        },
    )
    subject_key = f"lineage:{lineage_id}"
    timings = (("new", StatementTiming.day("March 3, 2025", date(2025, 3, 3))),)
    expected = _verbal_fact_digests(
        segment_id=segment_id,
        project_id=project_id,
        words=words,
        subject_key=subject_key,
        run_identity={"statement_id": statement_id},
        timings=timings,
        dependency_ids=tuple(dependency_ids),
    )
    if unreconcilable_fact_type is not None:
        # A stored identity the recipe cannot reproduce, written as it would
        # have to arrive: `facts` refuses every later UPDATE.
        expected = {**expected, unreconcilable_fact_type: "0" * 64}

    fact_ids = {}
    fact_ids["statement_wording"] = session.scalar(
        text(
            "insert into facts ("
            "project_id, fact_type, subject_kind, subject_key, text_value, "
            "transformation, recorded_by, content_sha256"
            ") values ("
            ":project_id, 'statement_wording', 'statement_candidate', :subject_key, "
            ":words, 'exact_prose_span_v1', :recorder, :digest) returning id"
        ),
        {
            "project_id": project_id,
            "subject_key": subject_key,
            "words": words,
            "recorder": VERBAL_RECORDER,
            "digest": expected["statement_wording"],
        },
    )
    for role in ("value_source", "attribution_source"):
        session.execute(
            text(
                "insert into fact_sources ("
                "project_id, fact_id, source_segment_id, role, ordinal"
                ") values (:project_id, :fact_id, :segment_id, :role, 1)"
            ),
            {
                "project_id": project_id,
                "fact_id": fact_ids["statement_wording"],
                "segment_id": segment_id,
                "role": role,
            },
        )

    fact_ids["statement_timing"] = session.scalar(
        text(
            "insert into facts ("
            "project_id, fact_type, subject_kind, subject_key, transformation, "
            "recorded_by, content_sha256"
            ") values ("
            ":project_id, 'statement_timing', 'statement_candidate', :subject_key, "
            "'typed_statement_timing_v1', :recorder, :digest) returning id"
        ),
        {
            "project_id": project_id,
            "subject_key": subject_key,
            "recorder": VERBAL_RECORDER,
            "digest": expected["statement_timing"],
        },
    )
    session.execute(
        text(
            "insert into fact_sources ("
            "project_id, fact_id, source_segment_id, role, ordinal"
            ") values (:project_id, :fact_id, :segment_id, 'value_source', 1)"
        ),
        {
            "project_id": project_id,
            "fact_id": fact_ids["statement_timing"],
            "segment_id": segment_id,
        },
    )
    for role, timing in timings:
        session.execute(
            text(
                "insert into fact_statement_timings ("
                "project_id, fact_id, timing_role, text, precision, "
                "start_date, end_date"
                ") values ("
                ":project_id, :fact_id, :role, :timing_text, :precision, "
                ":start_date, :end_date)"
            ),
            {
                "project_id": project_id,
                "fact_id": fact_ids["statement_timing"],
                "role": role,
                "timing_text": timing.text,
                "precision": timing.precision,
                "start_date": timing.start_date,
                "end_date": timing.end_date,
            },
        )

    fact_ids["applies_to"] = session.scalar(
        text(
            "insert into facts ("
            "project_id, fact_type, subject_kind, subject_key, transformation, "
            "recorded_by, content_sha256"
            ") values ("
            ":project_id, 'applies_to', 'statement_candidate', :subject_key, "
            "'structured_reference_set_v1', :recorder, :digest) returning id"
        ),
        {
            "project_id": project_id,
            "subject_key": subject_key,
            "recorder": VERBAL_RECORDER,
            "digest": expected["applies_to"],
        },
    )
    session.execute(
        text(
            "insert into fact_sources ("
            "project_id, fact_id, source_segment_id, role, ordinal"
            ") values (:project_id, :fact_id, :segment_id, 'value_source', 1)"
        ),
        {
            "project_id": project_id,
            "fact_id": fact_ids["applies_to"],
            "segment_id": segment_id,
        },
    )
    for slot, dependency_id in enumerate(dependency_ids, start=1):
        session.execute(
            text(
                "insert into fact_applies_to ("
                "project_id, fact_id, dependency_id, ordinal"
                ") values (:project_id, :fact_id, :dependency_id, :ordinal)"
            ),
            {
                "project_id": project_id,
                "fact_id": fact_ids["applies_to"],
                "dependency_id": dependency_id,
                "ordinal": slot,
            },
        )

    return {
        "project_id": project_id,
        "statement_id": statement_id,
        "segment_id": segment_id,
        "subject_key": subject_key,
        "words": words,
        "recorded_at": recorded_at,
        "conversation_date": conversation_date,
        "timings": timings,
        "dependency_ids": tuple(dependency_ids),
        "fact_ids": fact_ids,
        "prior_digests": expected,
    }


def _verbal_fact_digests(
    *,
    segment_id: int,
    project_id: int,
    words: str,
    subject_key: str,
    run_identity: dict,
    timings,
    dependency_ids,
) -> dict[str, str]:
    """The three Fact identity digests, from the application's own recipe.

    The migration carries a frozen copy of that recipe; computing the expected
    values here proves the copy still agrees with `corridor.facts`, on both the
    digest the backfill must reproduce and the one it must write.
    """

    segment = SourceSegment(
        project_id=project_id,
        document_id=None,
        recorded_verbal_origin_id=0,
        kind="recorded_verbal_statement",
        exact_text=words,
        content_sha256=sha256(words.encode("utf-8")).hexdigest(),
        ordinal=1,
    )
    segment.id = segment_id
    return {
        "statement_wording": _fact_digest(
            run_identity=run_identity,
            subject_kind="statement_candidate",
            subject_key=subject_key,
            value=materialize_quoted_statement_wording(segment, words),
        ),
        "statement_timing": _fact_digest(
            run_identity=run_identity,
            subject_kind="statement_candidate",
            subject_key=subject_key,
            value=materialize_typed_satellite("statement_timing", segment),
            structured_value=_statement_timing_structured(timings),
        ),
        "applies_to": _fact_digest(
            run_identity=run_identity,
            subject_kind="statement_candidate",
            subject_key=subject_key,
            value=materialize_typed_satellite("applies_to", segment),
            structured_value={"dependency_ids": list(dependency_ids)},
        ),
    }


def test_the_recorded_verbal_backfill_reconciles_one_to_one(tmp_path):
    """ADR-0081 stage 1, proved on the exact rows the transition transformed.

    Two dual-written verbals cross the transition. Each must gain exactly one
    origin carrying the recorder and the original recorded time, exactly one
    compatibility mapping, exactly one receipt, and three Fact identity digests
    replaced from a receipt that names both values. Nothing else moves, and the
    downgrade puts every one of them back.
    """

    configured = make_url(settings.database_url)
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="corridor_baseline_verbal_",
        migration_revision=SUPPORTED_HEAD,
    ) as database:
        database_url = configured.set(database=database.name)
        with database.session_factory.begin() as session:
            first = _seed_dual_written_verbal(
                session,
                slug="verbal-backfill-one",
                words=VERBAL_WORDS,
                recorded_at=VERBAL_RECORDED_AT,
                conversation_date=VERBAL_CONVERSATION_DATE,
                dependency_count=2,
            )
            second = _seed_dual_written_verbal(
                session,
                slug="verbal-backfill-two",
                words=SECOND_VERBAL_WORDS,
                recorded_at=SECOND_VERBAL_RECORDED_AT,
                conversation_date=SECOND_VERBAL_CONVERSATION_DATE,
                dependency_count=0,
            )

        upgraded = _alembic(database_url, "upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stderr

        with database.session_factory() as session:
            receipts = session.execute(
                text(
                    "select receipt.legacy_statement_id, receipt.source_segment_id, "
                    "       receipt.origin_id, receipt.project_id, "
                    "       receipt.fact_count, receipt.migration_revision, "
                    "       receipt.executed_by, "
                    "       origin.recorded_by, origin.recorded_at, "
                    "       origin.conversation_date, origin.exact_text, "
                    "       origin.content_sha256, origin.corrects_origin_id, "
                    "       mapping.statement_id as mapped_statement_id, "
                    "       segment.recorded_verbal_origin_id as segment_origin_id "
                    "  from recorded_verbal_origin_backfill_receipts receipt "
                    "  join recorded_verbal_origins origin "
                    "    on origin.id = receipt.origin_id "
                    "  join recorded_verbal_origin_statements mapping "
                    "    on mapping.origin_id = receipt.origin_id "
                    "  join source_segments segment "
                    "    on segment.id = receipt.source_segment_id "
                    " order by receipt.legacy_statement_id"
                )
            ).all()

            assert len(receipts) == 2
            for receipt, seeded in zip(receipts, (first, second)):
                assert receipt.legacy_statement_id == seeded["statement_id"]
                assert receipt.source_segment_id == seeded["segment_id"]
                assert receipt.mapped_statement_id == seeded["statement_id"]
                assert receipt.segment_origin_id == receipt.origin_id
                assert receipt.project_id == seeded["project_id"]
                assert receipt.fact_count == 3
                assert receipt.migration_revision == CURRENT_HEAD
                assert receipt.executed_by == "migration:b2d5f8a1c4e7/512"
                # The recorder attested; the migration only moved the row.
                assert receipt.recorded_by == VERBAL_RECORDER
                assert receipt.recorded_at == seeded["recorded_at"]
                assert receipt.conversation_date == seeded["conversation_date"]
                assert receipt.exact_text == seeded["words"]
                assert receipt.content_sha256 == sha256(
                    seeded["words"].encode("utf-8")
                ).hexdigest()
                assert receipt.corrects_origin_id is None

            # One origin, one mapping, one receipt per verbal; nothing else.
            counts = session.execute(
                text(
                    "select (select count(*) from recorded_verbal_origins) as origins, "
                    "  (select count(*) from recorded_verbal_origin_statements) "
                    "    as mappings, "
                    "  (select count(*) from "
                    "    recorded_verbal_origin_backfill_receipts) as receipts, "
                    "  (select count(*) from recorded_verbal_origin_fact_digests) "
                    "    as digests, "
                    "  (select count(*) from source_segments "
                    "    where kind = 'recorded_verbal_statement' "
                    "      and recorded_verbal_origin_id is null) as unpointed"
                )
            ).one()
            assert (
                counts.origins,
                counts.mappings,
                counts.receipts,
                counts.digests,
                counts.unpointed,
            ) == (2, 2, 2, 6, 0)

            for receipt, seeded in zip(receipts, (first, second)):
                after = _verbal_fact_digests(
                    segment_id=seeded["segment_id"],
                    project_id=seeded["project_id"],
                    words=seeded["words"],
                    subject_key=seeded["subject_key"],
                    run_identity={"recorded_verbal_origin_id": receipt.origin_id},
                    timings=seeded["timings"],
                    dependency_ids=seeded["dependency_ids"],
                )
                for fact_type, fact_id in seeded["fact_ids"].items():
                    stored = session.execute(
                        text(
                            "select fact.content_sha256 as digest, "
                            "       change.prior_content_sha256 as prior, "
                            "       change.content_sha256 as recorded, "
                            "       change.receipt_id as receipt_id, "
                            "       change.project_id as project_id "
                            "  from facts fact "
                            "  join recorded_verbal_origin_fact_digests change "
                            "    on change.fact_id = fact.id "
                            " where fact.id = :fact_id"
                        ),
                        {"fact_id": fact_id},
                    ).one()
                    assert stored.digest == after[fact_type]
                    assert stored.recorded == after[fact_type]
                    assert stored.prior == seeded["prior_digests"][fact_type]
                    assert stored.project_id == seeded["project_id"]

        downgraded = _alembic(database_url, "downgrade", SUPPORTED_HEAD)
        assert downgraded.returncode == 0, downgraded.stderr

        with database.session_factory() as session:
            for seeded in (first, second):
                restored = session.execute(
                    text(
                        "select statement_id, kind, exact_text "
                        "  from source_segments where id = :segment_id"
                    ),
                    {"segment_id": seeded["segment_id"]},
                ).one()
                assert restored.statement_id == seeded["statement_id"]
                assert restored.kind == "recorded_verbal_statement"
                assert restored.exact_text == seeded["words"]
                for fact_type, fact_id in seeded["fact_ids"].items():
                    digest = session.scalar(
                        text("select content_sha256 from facts where id = :fact_id"),
                        {"fact_id": fact_id},
                    )
                    assert digest == seeded["prior_digests"][fact_type]
            assert session.scalar(
                text(
                    "select count(*) from information_schema.tables "
                    " where table_schema = 'public' "
                    "   and table_name like 'recorded_verbal%'"
                )
            ) == 0


def test_the_recorded_verbal_backfill_refuses_an_unreconcilable_fact(tmp_path):
    """A Fact whose stored identity cannot be reproduced aborts the transition.

    The backfill derives the replacement digest from the same reconstruction
    that must first reproduce the stored one. A row it cannot reproduce is a
    row it does not understand, so it refuses rather than writing a digest that
    would silently redefine the Fact.
    """

    configured = make_url(settings.database_url)
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="corridor_baseline_verbal_bad_",
        migration_revision=SUPPORTED_HEAD,
    ) as database:
        database_url = configured.set(database=database.name)
        with database.session_factory.begin() as session:
            seeded = _seed_dual_written_verbal(
                session,
                slug="verbal-backfill-broken",
                words=VERBAL_WORDS,
                recorded_at=VERBAL_RECORDED_AT,
                conversation_date=VERBAL_CONVERSATION_DATE,
                dependency_count=1,
                unreconcilable_fact_type="statement_wording",
            )

        completed = _alembic(database_url, "upgrade", "head")

        assert completed.returncode != 0
        assert "#512 backfill refuses" in completed.stderr
        assert _migration_head(database.session_factory) == SUPPORTED_HEAD
        with database.session_factory() as session:
            assert session.scalar(
                text(
                    "select statement_id from source_segments where id = :segment_id"
                ),
                {"segment_id": seeded["segment_id"]},
            ) == seeded["statement_id"]
            assert session.scalar(
                text(
                    "select count(*) from information_schema.tables "
                    " where table_schema = 'public' "
                    "   and table_name like 'recorded_verbal%'"
                )
            ) == 0


DEDUPLICATION_WORDS = "Oncor will relocate the pole before 3 March 2025."


def _seed_identified_fact(session, *, slug: str, digest: str | None) -> dict:
    """One project-scoped Fact at the supported head, with or without identity.

    ``facts.content_sha256`` is nullable there behind a partial unique index,
    which is exactly what #457 makes permanent state: a Fact with no digest is
    a Fact with no identity, and two of them are indistinguishable.
    """

    project_id = session.scalar(
        text(
            "insert into projects (slug, name, is_synthetic) "
            "values (:slug, :name, true) returning id"
        ),
        {"slug": slug, "name": slug},
    )
    document_id = session.scalar(
        text(
            "insert into documents (project_id, sha256, filename, doc_type) "
            "values (:project_id, :digest, 'minutes.pdf', 'minutes') returning id"
        ),
        {
            "project_id": project_id,
            "digest": sha256(slug.encode("utf-8")).hexdigest(),
        },
    )
    segment_id = session.scalar(
        text(
            "insert into source_segments ("
            "project_id, document_id, kind, exact_text, content_sha256, ordinal, "
            "page_no, start_offset, end_offset"
            ") values ("
            ":project_id, :document_id, 'prose_span', :words, :digest, 1, "
            "1, 0, :end_offset) returning id"
        ),
        {
            "project_id": project_id,
            "document_id": document_id,
            "words": DEDUPLICATION_WORDS,
            "digest": sha256(DEDUPLICATION_WORDS.encode("utf-8")).hexdigest(),
            "end_offset": len(DEDUPLICATION_WORDS),
        },
    )
    subject_key = f"candidate:{segment_id}"
    fact_id = session.scalar(
        text(
            "insert into facts ("
            "project_id, fact_type, subject_kind, subject_key, text_value, "
            "transformation, recorded_by, content_sha256"
            ") values ("
            ":project_id, 'statement_wording', 'statement_candidate', "
            ":subject_key, :words, 'exact_prose_span_v1', 'local:test', :digest"
            ") returning id"
        ),
        {
            "project_id": project_id,
            "subject_key": subject_key,
            "words": DEDUPLICATION_WORDS,
            "digest": digest,
        },
    )
    session.execute(
        text(
            "insert into fact_sources ("
            "project_id, fact_id, source_segment_id, role, ordinal"
            ") values (:project_id, :fact_id, :segment_id, 'value_source', 1)"
        ),
        {"project_id": project_id, "fact_id": fact_id, "segment_id": segment_id},
    )
    # A revision is written only by the role that owns accepted authority; a
    # guard trigger refuses every other writer, this seed included.
    session.execute(text("set local role corridor_fact_decision_writer"))
    revision_id = session.scalar(
        text(
            "insert into project_record_revisions ("
            "project_id, command_type, human_principal, released_policy, "
            "idempotency_key"
            ") values ("
            ":project_id, 'record_human_fact_decision', 'local:coordinator', "
            "null, :key) returning id"
        ),
        {"project_id": project_id, "key": f"decide:{fact_id}"},
    )
    session.execute(text("reset role"))
    return {
        "project_id": project_id,
        "document_id": document_id,
        "segment_id": segment_id,
        "subject_key": subject_key,
        "fact_id": fact_id,
        "revision_id": revision_id,
        "digest": digest,
        "idempotency_key": f"decide:{fact_id}",
    }


def test_the_deduplication_transition_carries_identified_rows_across_unchanged():
    """#457, proved on the exact rows the transition constrains.

    The block writes no row: it turns each family's derived identity into a
    constraint. So the proof is that an identified Fact and its revision cross
    unchanged, that the identity is afterwards a property of the table rather
    than of the command — an exact copy is refused — and that the downgrade
    leaves both rows exactly as they were seeded.
    """

    configured = make_url(settings.database_url)
    digest = sha256(f"fact:{DEDUPLICATION_WORDS}".encode("utf-8")).hexdigest()
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="corridor_baseline_dedup_",
        migration_revision=SUPPORTED_HEAD,
    ) as database:
        database_url = configured.set(database=database.name)
        with database.session_factory.begin() as session:
            seeded = _seed_identified_fact(
                session, slug="dedup-identified", digest=digest
            )

        upgraded = _alembic(database_url, "upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stderr

        with database.session_factory() as session:
            fact = session.execute(
                text(
                    "select project_id, fact_type, subject_kind, subject_key, "
                    "       text_value, transformation, recorded_by, content_sha256 "
                    "  from facts where id = :fact_id"
                ),
                {"fact_id": seeded["fact_id"]},
            ).one()
            assert fact.project_id == seeded["project_id"]
            assert fact.fact_type == "statement_wording"
            assert fact.subject_kind == "statement_candidate"
            assert fact.subject_key == seeded["subject_key"]
            assert fact.text_value == DEDUPLICATION_WORDS
            assert fact.transformation == "exact_prose_span_v1"
            assert fact.recorded_by == "local:test"
            assert fact.content_sha256 == digest

            revision = session.execute(
                text(
                    "select project_id, command_type, human_principal, "
                    "       released_policy, idempotency_key "
                    "  from project_record_revisions where id = :revision_id"
                ),
                {"revision_id": seeded["revision_id"]},
            ).one()
            assert revision.project_id == seeded["project_id"]
            assert revision.command_type == "record_human_fact_decision"
            assert revision.human_principal == "local:coordinator"
            assert revision.released_policy is None
            assert revision.idempotency_key == seeded["idempotency_key"]

            # The identity is now the table's, not the command's.
            assert session.execute(
                text(
                    "select conname, contype from pg_constraint "
                    " where conname in ('uq_facts_content_sha256', "
                    "   'ck_project_record_revisions_idempotency_key', "
                    "   'uq_delta_groups_source_change', "
                    "   'uq_fact_decisions_revision_fact', "
                    "   'uq_delta_deferrals_occurrence', "
                    "   'uq_push_deliveries_envelope') order by conname"
                )
            ).all() == [
                ("ck_project_record_revisions_idempotency_key", "c"),
                ("uq_delta_deferrals_occurrence", "u"),
                ("uq_delta_groups_source_change", "u"),
                ("uq_fact_decisions_revision_fact", "u"),
                ("uq_facts_content_sha256", "u"),
                ("uq_push_deliveries_envelope", "u"),
            ]
            assert session.scalar(
                text(
                    "select attnotnull from pg_attribute "
                    " where attrelid = 'public.facts'::regclass "
                    "   and attname = 'content_sha256'"
                )
            ) is True

        with database.session_factory() as session:
            with pytest.raises(DBAPIError, match="uq_facts_content_sha256"):
                session.execute(
                    text(
                        "insert into facts (project_id, fact_type, subject_kind, "
                        "  subject_key, text_value, transformation, recorded_by, "
                        "  content_sha256) "
                        "select project_id, fact_type, subject_kind, subject_key, "
                        "  text_value, transformation, recorded_by, content_sha256 "
                        "  from facts where id = :fact_id"
                    ),
                    {"fact_id": seeded["fact_id"]},
                )

        downgraded = _alembic(database_url, "downgrade", SUPPORTED_HEAD)
        assert downgraded.returncode == 0, downgraded.stderr

        with database.session_factory() as session:
            restored = session.execute(
                text(
                    "select content_sha256, subject_key, text_value "
                    "  from facts where id = :fact_id"
                ),
                {"fact_id": seeded["fact_id"]},
            ).one()
            assert restored.content_sha256 == digest
            assert restored.subject_key == seeded["subject_key"]
            assert restored.text_value == DEDUPLICATION_WORDS
            assert session.scalar(
                text(
                    "select count(*) from pg_constraint "
                    " where conname in ('uq_facts_content_sha256', "
                    "   'ck_project_record_revisions_idempotency_key', "
                    "   'uq_delta_deferrals_occurrence', "
                    "   'uq_push_deliveries_envelope')"
                )
            ) == 0
            assert session.scalar(
                text(
                    "select indexdef from pg_indexes "
                    " where indexname = 'uq_facts_content_sha256'"
                )
            ) == (
                "CREATE UNIQUE INDEX uq_facts_content_sha256 ON public.facts "
                "USING btree (content_sha256) WHERE (content_sha256 IS NOT NULL)"
            )


def test_the_deduplication_transition_refuses_a_fact_with_no_identity():
    """A row the new identity cannot represent aborts the transition.

    Which of two indistinguishable rows is the record is a semantic question,
    and no migration has the authority to answer it: the transition counts what
    it cannot represent, names the family, and leaves every row alone.
    """

    configured = make_url(settings.database_url)
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="corridor_baseline_dedup_bad_",
        migration_revision=SUPPORTED_HEAD,
    ) as database:
        database_url = configured.set(database=database.name)
        with database.session_factory.begin() as session:
            seeded = _seed_identified_fact(
                session, slug="dedup-unidentified", digest=None
            )

        completed = _alembic(database_url, "upgrade", "head")

        assert completed.returncode != 0
        assert "#457 de-duplication refuses" in completed.stderr
        assert "1 Source Facts with no identity digest" in completed.stderr
        assert "Nothing is merged or dropped here" in completed.stderr
        assert _migration_head(database.session_factory) == SUPPORTED_HEAD
        with database.session_factory() as session:
            assert session.scalar(
                text("select content_sha256 from facts where id = :fact_id"),
                {"fact_id": seeded["fact_id"]},
            ) is None
            assert session.scalar(
                text(
                    "select count(*) from pg_constraint "
                    " where conname = 'uq_push_deliveries_envelope'"
                )
            ) == 0
