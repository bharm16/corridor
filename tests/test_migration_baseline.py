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
from corridor.product_proving_database import fingerprint_database_url
from corridor.report_release import retrieve_released_external_report


ROOT = Path(__file__).resolve().parents[1]
VERSIONS = ROOT / "src" / "corridor" / "migrations" / "baseline_versions"
SCHEMA_BUILDER = "b7d3f9a1c2e5"
BASELINE_MARKER = "c0a1d0b5e11e"
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
PREDECESSOR_HEAD = VERBAL_SEGMENT_HEAD
CURRENT_HEAD = "6c7d8e9f0a12"
EXPECTED_SCHEMA_SHA256 = (
    "71b5872a005204c351f8ad8391da4368b79aa45dde78d1b33ff17b6d85b94744"
)

pytestmark = [pytest.mark.slow, pytest.mark.migration]


def test_migration_inventory_is_one_builder_marker_and_two_linear_successors():
    assert {path.name for path in VERSIONS.glob("*.py")} == {
        f"{SCHEMA_BUILDER}_current_schema_baseline.py",
        f"{BASELINE_MARKER}_establish_current_baseline.py",
        f"{SPREADSHEET_HEAD}_add_spreadsheet_source_segments.py",
        f"{FACT_HEAD}_add_typed_facts.py",
        f"{RELEASE_HEAD}_release_references_artifact.py",
        f"{APPEND_HEAD}_add_scoped_source_fact_append.py",
        f"{PAGE_INVENTORY_HEAD}_add_pdf_page_inventory.py",
        f"{IMMUTABLE_PROPOSAL_HEAD}_add_immutable_proposals.py",
        f"{PROSE_HEAD}_add_prose_spans_and_statement_facts.py",
        f"{DECISION_HEAD}_add_fact_decisions.py",
        f"{CURRENT_RECORD_HEAD}_add_current_project_record_view.py",
        f"{STRUCTURED_FACT_HEAD}_add_structured_cell_facts.py",
        f"{SUBJECT_RESOLUTION_HEAD}_add_subject_resolution_registry.py",
        f"{PROSE_ACCOUNTING_HEAD}_add_prose_completeness_accounting.py",
        f"{RENDER_HEAD}_add_page_render_derivatives.py",
        f"{CLASS_B_RETENTION_HEAD}_add_class_b_retention.py",
        f"{TOKEN_LAYER_HEAD}_add_token_layers.py",
        f"{HUMAN_DECISION_HEAD}_add_human_fact_decision.py",
        f"{VERBAL_SEGMENT_HEAD}_add_recorded_verbal_segment.py",
        f"{CURRENT_HEAD}_add_statement_timing_fact.py",
    }


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

    assert database.migration_head == CURRENT_HEAD
    assert fingerprint.schema_sha256 == EXPECTED_SCHEMA_SHA256
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


def test_supported_predecessor_adds_empty_append_receipts_without_changing_rows():
    configured = make_url(settings.database_url)
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="corridor_baseline_bridge_",
        migration_revision=PREDECESSOR_HEAD,
    ) as database:
        database_url = configured.set(database=database.name)
        with database.session_factory.begin() as session:
            project_id = session.scalar(
                text(
                    "insert into projects (slug, name, is_synthetic) "
                    "values ('baseline-bridge', 'Baseline Bridge', true) "
                    "returning id"
                )
            )
            assert project_id is not None
        before = _project_row(database.session_factory)

        completed = _alembic(database_url, "upgrade", "head")

        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert _migration_head(database.session_factory) == CURRENT_HEAD
        assert _project_row(database.session_factory) == before
        assert _source_segment_rows(database.session_factory) == []
        assert _fact_rows(database.session_factory) == []
        # The statement_timing migration lets a human-gated verbal Fact be
        # document-less and adds the typed fact_statement_timings satellite.
        with database.session_factory() as session:
            document_id_nullable = session.scalar(
                text(
                    "select is_nullable from information_schema.columns "
                    "where table_name = 'facts' and column_name = 'document_id'"
                )
            )
            assert document_id_nullable == "YES"
            type_check = session.scalar(
                text(
                    "select pg_get_constraintdef(oid) from pg_constraint "
                    "where conname = 'ck_facts_type'"
                )
            )
            assert "statement_timing" in type_check
            assert session.scalar(
                text("select to_regclass('public.fact_statement_timings')")
            )


def test_supported_predecessor_creates_immutable_scoped_append_receipt():
    configured = make_url(settings.database_url)
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="corridor_segment_bridge_",
        migration_revision=PREDECESSOR_HEAD,
    ) as database:
        with database.session_factory.begin() as session:
            project_id = session.scalar(
                text(
                    "insert into projects (slug, name, is_synthetic) "
                    "values ('segment-bridge', 'Segment Bridge', true) "
                    "returning id"
                )
            )
            document_id = session.scalar(
                text(
                    "insert into documents "
                    "(project_id, sha256, filename, doc_type, numbering_scheme, "
                    "pages, parse_status) values "
                    "(:project_id, :sha256, 'matrix.xlsx', 'matrix', "
                    "'project-unique', 1, 'parsed') returning id"
                ),
                {"project_id": project_id, "sha256": "a" * 64},
            )

        with database.session_factory.begin() as session:
            segment_id = session.scalar(
                text(
                    "insert into source_segments "
                    "(project_id, document_id, kind, exact_text, content_sha256, "
                    "ordinal, sheet_name, cell_range) values "
                    "(:project_id, :document_id, 'spreadsheet_cell', 'UC-1', "
                    ":digest, 1, 'Conflicts', 'A2') returning id"
                ),
                {
                    "project_id": project_id,
                    "document_id": document_id,
                    "digest": "1c4fc7e2bdaf4b219c00ce662b927dc5d7e17091e467df61b9582d7fb359a39e",
                },
            )

        database_url = configured.set(database=database.name)
        completed = _alembic(database_url, "upgrade", "head")
        assert completed.returncode == 0, completed.stdout + completed.stderr

        with database.session_factory.begin() as session:
            candidate_id = session.scalar(
                text(
                    "insert into candidates "
                    "(project_id, kind, payload_json, source_document_id, source_pages, "
                    "prompt_version, citations_verified) values "
                    "(:project_id, 'dependency', '{\"fields\": {}}'::jsonb, "
                    ":document_id, array[1], 'migration_fixture_v1', true) returning id"
                ),
                {"project_id": project_id, "document_id": document_id},
            )
            run_id = session.scalar(
                text(
                    "insert into extraction_runs "
                    "(document_id, prompt_version, outcome, candidate_count, page_errors) "
                    "values (:document_id, 'migration_fixture_v1', 'completed', 1, 0) "
                    "returning id"
                ),
                {"document_id": document_id},
            )
            fact_id = session.scalar(
                text(
                    "insert into facts "
                    "(project_id, document_id, extraction_run_id, fact_type, "
                    "subject_kind, subject_key, text_value, transformation, recorded_by, "
                    "content_sha256) "
                    "values (:project_id, :document_id, :run_id, 'station_from', "
                    "'source_row', 'Conflicts!2', 'UC-1', 'trim_cell_text_v1', "
                    "'extractor:migration_fixture_v1', :content_sha256) returning id"
                ),
                {
                    "project_id": project_id,
                    "document_id": document_id,
                    "run_id": run_id,
                    "content_sha256": "b" * 64,
                },
            )
            receipt_id = session.scalar(
                text(
                    "insert into source_fact_append_receipts "
                    "(project_id, document_id, extraction_run_id, idempotency_key, "
                    "content_sha256) values (:project_id, :document_id, :run_id, "
                    "'migration:fixture', :content_sha256) returning id"
                ),
                {
                    "project_id": project_id,
                    "document_id": document_id,
                    "run_id": run_id,
                    "content_sha256": "c" * 64,
                },
            )
            session.execute(
                text(
                    "insert into fact_sources "
                    "(project_id, document_id, fact_id, source_segment_id, role, ordinal) "
                    "values (:project_id, :document_id, :fact_id, :segment_id, "
                    "'value_source', 1)"
                ),
                {
                    "project_id": project_id,
                    "document_id": document_id,
                    "fact_id": fact_id,
                    "segment_id": segment_id,
                },
            )
            proposal_id = session.scalar(
                text(
                    "insert into extracted_proposals "
                    "(project_id, document_id, extraction_run_id, candidate_id, kind, "
                    "subject_key, candidate_metadata_json) "
                    "values (:project_id, :document_id, :run_id, :candidate_id, "
                    "'dependency', 'Conflicts!2', cast(:metadata as jsonb)) returning id"
                ),
                {
                    "project_id": project_id,
                    "document_id": document_id,
                    "run_id": run_id,
                    "candidate_id": candidate_id,
                    "metadata": '{"state":"pending","source_pages":[1]}',
                },
            )
            session.execute(
                text(
                    "insert into extracted_proposal_facts "
                    "(project_id, document_id, extraction_run_id, proposal_id, fact_id, ordinal) "
                    "values (:project_id, :document_id, :run_id, :proposal_id, :fact_id, 1)"
                ),
                {
                    "project_id": project_id,
                    "document_id": document_id,
                    "run_id": run_id,
                    "proposal_id": proposal_id,
                    "fact_id": fact_id,
                },
            )

        assert _source_segment_rows(database.session_factory) == [
            (segment_id, project_id, document_id, "spreadsheet_cell", "UC-1", "A2")
        ]
        assert _fact_rows(database.session_factory) == [
            (fact_id, document_id, run_id, "station_from", "Conflicts!2", "UC-1")
        ]
        with pytest.raises(DBAPIError, match="Extracted Proposal spine is immutable"):
            with database.session_factory.begin() as session:
                session.execute(
                    text("update extracted_proposals set subject_key = 'x' where id = :id"),
                    {"id": proposal_id},
                )
        assert _fact_rows(database.session_factory) == [
            (fact_id, document_id, run_id, "station_from", "Conflicts!2", "UC-1")
        ]


def test_downgrade_that_would_drop_the_statement_timing_fact_is_unsupported():
    configured = make_url(settings.database_url)
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="corridor_baseline_downgrade_",
    ) as database:
        database_url = configured.set(database=database.name)
        completed = _alembic(database_url, "downgrade", PREDECESSOR_HEAD)

    assert completed.returncode != 0
    assert "statement timing fact migration downgrade is unsupported" in completed.stderr


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
