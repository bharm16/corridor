"""Migration rehearsal for Evidence Investigator receipts and v2 lineage."""

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

pytestmark = pytest.mark.slow


ROOT = Path(__file__).resolve().parents[1]
PREDECESSOR = "d256f1a8b4c7"
HEAD = "f362a1b2c3d4"


def _upgrade(database_url: str, target: str) -> None:
    completed = subprocess.run(
        ["uv", "run", "alembic", "upgrade", target],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def _columns(connection, table: str) -> list[str]:
    return connection.execute(
        text(
            "select column_name from information_schema.columns "
            "where table_schema = 'public' and table_name = :table "
            "order by ordinal_position"
        ),
        {"table": table},
    ).scalars().all()


def _constraint_columns(connection, constraint_name: str) -> list[str]:
    return connection.execute(
        text(
            """
            select attribute.attname
            from pg_constraint con
            join pg_class relation on relation.oid = con.conrelid
            join unnest(con.conkey) with ordinality as key(attnum, ordinal)
              on true
            join pg_attribute attribute
              on attribute.attrelid = relation.oid
             and attribute.attnum = key.attnum
            where con.conname = :constraint_name
            order by key.ordinal
            """
        ),
        {"constraint_name": constraint_name},
    ).scalars().all()


def test_evidence_investigator_schema_is_one_linear_head_on_a_fresh_database():
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == [HEAD]

    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="issue295_fresh_",
    ) as database:
        database_url = make_url(settings.database_url).set(database=database.name)
        engine = create_engine(database_url)
        try:
            with engine.connect() as connection:
                assert (
                    connection.scalar(text("select version_num from alembic_version"))
                    == HEAD
                )
                assert _columns(connection, "evidence_investigation_runs") == [
                    "id",
                    "public_id",
                    "project_id",
                    "candidate_id",
                    "extraction_run_id",
                    "terminal_status",
                    "reason",
                    "detail",
                    "adapter",
                    "model",
                    "prompt_version",
                    "tool_contract_version",
                    "validator_version",
                    "transport_gate_sha256",
                    "candidate_payload_sha256",
                    "read_fingerprint",
                    "budget_json",
                    "usage_json",
                    "started_at",
                    "completed_at",
                    "adapter_contract_version",
                    "prompt_sha256",
                ]
                assert _columns(connection, "evidence_investigation_shadow_cases") == [
                    "id",
                    "public_id",
                    "project_id",
                    "candidate_id",
                    "extraction_run_id",
                    "candidate_payload_sha256",
                    "read_fingerprint",
                    "model",
                    "prompt_version",
                    "case_json",
                    "registered_evidence_json",
                    "option_population_json",
                    "option_population_sha256",
                    "frozen_at",
                    "prompt_sha256",
                    "adapter_contract_version",
                    "transport_gate_sha256",
                    "budget_json",
                    "tool_contract_version",
                ]
                assert _columns(connection, "evidence_investigation_shadow_outcomes") == [
                    "id",
                    "shadow_case_id",
                    "candidate_disposition",
                    "scope_mode",
                    "selected_dependency_ids_json",
                    "correction",
                    "undo",
                    "unresolved",
                    "outcome_identities_json",
                    "strata_json",
                    "review_seconds",
                    "outcome_sha256",
                    "captured_at",
                    "human_outcome_identity",
                ]
                assert _columns(
                    connection, "evidence_investigation_evaluation_receipts"
                ) == [
                    "id",
                    "public_id",
                    "evaluation_version",
                    "status",
                    "selected_run_ids_json",
                    "identity_json",
                    "metrics_json",
                    "strata_json",
                    "human_scores_json",
                    "gates_json",
                    "limitations_json",
                    "summary_markdown",
                    "receipt_sha256",
                    "evaluated_at",
                ]
                assert _columns(
                    connection, "evidence_investigation_candidate_review_starts"
                ) == [
                    "id",
                    "project_id",
                    "candidate_id",
                    "principal",
                    "observed_at",
                ]
                assert _columns(
                    connection, "statement_suggestion_eligibility_declarations"
                ) == [
                    "id",
                    "project_id",
                    "candidate_id",
                    "contract_version",
                    "declared_at",
                ]
                assert _columns(connection, "statement_suggestion_protections") == [
                    "id",
                    "project_id",
                    "candidate_id",
                    "kind",
                    "observation_contract",
                    "declared_at",
                ]
                assert _columns(connection, "statement_suggestion_protection_ends") == [
                    "id",
                    "protection_id",
                    "ended_at",
                ]
                assert _constraint_columns(
                    connection, "uq_evidence_investigation_shadow_case_identity"
                ) == [
                    "candidate_id",
                    "read_fingerprint",
                    "model",
                    "prompt_version",
                    "prompt_sha256",
                    "adapter_contract_version",
                    "tool_contract_version",
                ]
                for trigger_name in (
                    "evidence_investigation_runs_append_only",
                    "evidence_investigation_step_receipts_append_only",
                    "evidence_investigation_packet_receipts_append_only",
                    "evidence_investigation_shadow_cases_append_only",
                    "evidence_investigation_shadow_executions_append_only",
                    "evidence_investigation_review_observations_append_only",
                    "evidence_investigation_shadow_outcomes_append_only",
                    "evidence_investigation_evaluation_receipts_append_only",
                    "evidence_investigation_candidate_review_starts_append_only",
                    "statement_suggestion_eligibility_declarations_immutable",
                    "statement_suggestion_protections_immutable",
                    "statement_suggestion_protection_ends_immutable",
                    "statement_suggestion_eligibility_declarations_reject_truncate",
                    "statement_suggestion_protections_reject_truncate",
                    "statement_suggestion_protection_ends_reject_truncate",
                ):
                    assert connection.scalar(
                        text(
                            "select exists (select 1 from pg_trigger "
                            "where tgname = :trigger_name)"
                        ),
                        {"trigger_name": trigger_name},
                    )
        finally:
            engine.dispose()


def test_evidence_investigator_v2_seal_preserves_legacy_receipt_rows():
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=ROOT,
        error_cls=RuntimeError,
        database_prefix="issue295_predecessor_",
        migration_revision=PREDECESSOR,
    ) as database:
        database_url = make_url(settings.database_url).set(database=database.name)
        rendered = database_url.render_as_string(hide_password=False)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        insert into projects (id, slug, name, is_synthetic)
                        values (295000, 'issue295-predecessor',
                                'Issue 295 predecessor', true);

                        insert into documents
                            (id, project_id, sha256, filename, doc_type, doc_date,
                             pages, parse_status, registry_id, numbering_scheme)
                        values
                            (295000, 295000, repeat('1', 64), 'issue295.pdf',
                             'minutes', '2026-08-22', 1, 'parsed',
                             'issue295-doc', 'project-unique');

                        insert into candidates
                            (id, project_id, kind, payload_json, source_document_id,
                             extraction_run_id, source_pages, confidence,
                             prompt_version, model, citations_verified, state)
                        values
                            (295001, 295000, 'event',
                             jsonb_build_object(
                                 'kind', 'event',
                                 'fields', jsonb_build_object(
                                     'event_type', 'commitment',
                                     'description', 'Legacy investigation row'
                                 ),
                                 'citations', '[]'::jsonb
                             ),
                             295000, null, array[1], 1.0,
                             'minutes-v1', 'legacy-extractor', true, 'pending');

                        insert into evidence_investigation_runs
                            (id, public_id, project_id, candidate_id,
                             extraction_run_id, terminal_status, reason, detail,
                             adapter, model, prompt_version, tool_contract_version,
                             validator_version, transport_gate_sha256,
                             candidate_payload_sha256, read_fingerprint,
                             budget_json, usage_json, started_at, completed_at)
                        values
                            (295010, '29501010-1010-1010-1010-101010101010',
                             295000, 295001, null, 'human_judgment_needed',
                             null, null, 'direct-responses-v1',
                             'gpt-4.1-mini-legacy', 'evidence-investigator-v1',
                             'evidence-investigator-tools-v1',
                             'evidence-investigator-validator-v1',
                             repeat('a', 64), repeat('b', 64), repeat('c', 64),
                             '{"max_turns": 8}'::jsonb,
                             '{"turns": 1, "input_tokens": 10, "output_tokens": 5}'::jsonb,
                             '2026-08-22 12:00:00+00',
                             '2026-08-22 12:00:03+00');

                        insert into evidence_investigation_shadow_cases
                            (id, public_id, project_id, candidate_id,
                             extraction_run_id, candidate_payload_sha256,
                             read_fingerprint, model, prompt_version, case_json,
                             registered_evidence_json, option_population_json,
                             option_population_sha256, frozen_at)
                        values
                            (295020, '29502020-2020-2020-2020-202020202020',
                             295000, 295001, null, repeat('b', 64),
                             repeat('c', 64), 'gpt-4.1-mini-legacy',
                             'evidence-investigator-v1',
                             '{"schema_version":"evidence-investigator-case-v1"}'::jsonb,
                             '[]'::jsonb, '{}'::jsonb, repeat('d', 64),
                             '2026-08-22 12:01:00+00');
                        """
                    )
                )
            _upgrade(rendered, HEAD)
            with engine.connect() as connection:
                assert (
                    connection.scalar(text("select version_num from alembic_version"))
                    == HEAD
                )
                legacy_run = connection.execute(
                    text(
                        "select adapter_contract_version, prompt_sha256 "
                        "from evidence_investigation_runs where id = 295010"
                    )
                ).mappings().one()
                assert legacy_run["adapter_contract_version"] is None
                assert legacy_run["prompt_sha256"] is None
                legacy_shadow = connection.execute(
                    text(
                        "select prompt_sha256, adapter_contract_version, "
                        "transport_gate_sha256, budget_json, tool_contract_version "
                        "from evidence_investigation_shadow_cases where id = 295020"
                    )
                ).mappings().one()
                assert legacy_shadow["prompt_sha256"] is None
                assert legacy_shadow["adapter_contract_version"] is None
                assert legacy_shadow["transport_gate_sha256"] is None
                assert legacy_shadow["budget_json"] is None
                assert legacy_shadow["tool_contract_version"] is None
                assert _constraint_columns(
                    connection, "uq_evidence_investigation_shadow_case_identity"
                ) == [
                    "candidate_id",
                    "read_fingerprint",
                    "model",
                    "prompt_version",
                    "prompt_sha256",
                    "adapter_contract_version",
                    "tool_contract_version",
                ]
        finally:
            engine.dispose()
