"""Exercise post-#216 statement migrations on disposable PostgreSQL databases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from corridor.config import settings
from corridor.external_statements import (
    CitedStatementEvidence,
    StatementScope,
    StatementTiming,
    record_external_party_statement,
    record_statement_scope_decision,
)
from corridor.legacy_ledger_archive import plan_retirement, retire_legacy_ledger
from corridor.m8_acceptance_database import provision_disposable_postgres
from corridor.models import (
    AuditLog,
    Candidate,
    Dependency,
    DependencyEvent,
    DependencyEventEvidence,
    DependencyEventScope,
    DependencyEventScopeDecision,
    DependencyEventTiming,
    DocPage,
    Document,
    EvidenceLink,
    ExternalOrg,
    Project,
)
from corridor.principals import HumanPrincipal
import corridor.adjudicate as adjudicate_module
import corridor.models as models_module


_ROOT = Path(__file__).resolve().parents[1]
_PRE_STATEMENT_REVISION = "e216f5a4b3c2"
_SNAPSHOT_TABLES = (
    "projects",
    "external_orgs",
    "documents",
    "dependencies",
    "candidates",
    "dependency_events",
    "evidence_links",
    "operative_support",
    "audit_log",
    "policy_runs",
    "event_admission_outcomes",
    "reconfirmation_receipts",
    "event_cohort_receipts",
)
_STATEMENT_SHAPE_REVISION = "a217e4f3a2b1"
_ATTRIBUTABLE_STORAGE_REVISION = "b223f5a4c3d2"
_SCOPE_DECISION_REVISION = "c224a6b4d3e2"
_EVENT_EVIDENCE_ROLE_REVISION = "d225a7c4e3f2"
_LEGACY_STATEMENT_BACKFILL_REVISION = "e226a8d4f3c2"
_EVENT_EVIDENCE_MIGRATION_REVISION = "f227b9e4d3c2"
_CONTRACT_STATEMENT_REVISION = "a230c4d3e2f1"
_RETIREMENT_ROLE_REPAIR_REVISION = "b230e4f5a6b7"


@dataclass(frozen=True)
class _RevisionSchemaExpectation:
    constraints: frozenset[str]
    triggers: frozenset[str]


_REVISION_SCHEMA_EXPECTATIONS = {
    _PRE_STATEMENT_REVISION: _RevisionSchemaExpectation(
        constraints=frozenset(
            {
                "ck_verbal_events_require_call_facts",
                "dependency_events_dependency_id_fkey",
                "event_admission_outcomes_dependency_event_id_fkey",
                "evidence_links_event_id_fkey",
                "fk_operative_support_dependency_evidence",
                "reconfirmation_receipts_audit_log_id_fkey",
                "reconfirmation_receipts_dependency_id_fkey",
                "reconfirmation_receipts_successor_candidate_id_fkey",
            }
        ),
        triggers=frozenset(
            {
                "event_admission_outcomes_are_immutable",
                "event_admission_outcomes_must_match_runs",
                "event_admission_outcomes_reject_truncate",
                "event_cohort_receipts_are_immutable",
                "event_cohort_receipts_reject_truncate",
                "policy_runs_are_immutable",
                "policy_runs_must_match_outcomes",
                "policy_runs_reject_truncate",
                "reconfirmation_receipts_are_immutable",
                "reconfirmation_receipts_reject_truncate",
                "verbal_dependency_events_are_immutable",
                "verbal_dependency_events_reject_truncate",
            }
        ),
    ),
    _STATEMENT_SHAPE_REVISION: _RevisionSchemaExpectation(
        constraints=frozenset(
            {
                "ck_dependency_event_timing_bounds",
                "ck_dependency_event_timing_kind",
                "ck_dependency_event_timing_precision",
                "ck_dependency_events_scope_mode",
                "ck_evidence_links_event_ownership",
                "dependency_event_scope_links_match_shape",
                "dependency_event_scope_shape_is_valid",
                "dependency_event_scopes_dependency_id_fkey",
                "dependency_event_scopes_event_id_fkey",
                "dependency_event_timings_event_id_fkey",
                "dependency_event_timings_match_statement",
                "dependency_evidence_sufficiencies_dependency_id_fkey",
                "dependency_evidence_sufficiencies_evidence_link_id_fkey",
                "event_admission_outcomes_dependency_event_id_fkey",
                "fk_dependency_events_affected_external_org",
                "fk_dependency_events_project",
                "fk_dependency_events_stated_external_org",
                "fk_operative_support_evidence_link",
            }
        ),
        triggers=frozenset(
            {
                "dependency_event_scope_link_is_valid",
                "dependency_event_scope_links_match_shape",
                "dependency_event_scope_shape_is_valid",
                "dependency_event_timings_match_statement",
                "event_admission_outcomes_are_immutable",
                "event_admission_outcomes_must_match_runs",
                "event_admission_outcomes_reject_truncate",
                "event_cohort_receipts_are_immutable",
                "event_cohort_receipts_reject_truncate",
                "external_party_statement_events_are_immutable",
                "external_party_statement_events_reject_truncate",
                "external_party_statement_evidence_is_immutable",
                "external_party_statement_evidence_reject_truncate",
                "external_party_statement_scopes_are_immutable",
                "external_party_statement_scopes_reject_truncate",
                "external_party_statement_timings_are_immutable",
                "external_party_statement_timings_reject_truncate",
                "policy_runs_are_immutable",
                "policy_runs_must_match_outcomes",
                "policy_runs_reject_truncate",
                "reconfirmation_receipts_are_immutable",
                "reconfirmation_receipts_reject_truncate",
            }
        ),
    ),
}

_REVISION_SCHEMA_EXPECTATIONS[_ATTRIBUTABLE_STORAGE_REVISION] = (
    _RevisionSchemaExpectation(
        constraints=_REVISION_SCHEMA_EXPECTATIONS[
            _STATEMENT_SHAPE_REVISION
        ].constraints
        | frozenset(
            {
                "ck_dependency_events_attribution",
                "dependency_event_migration_receipts_event_id_fkey",
                "dependency_event_migration_receipts_pkey",
                "dependency_event_timing_cardinality_is_valid",
                "dependency_event_timing_rows_match_event",
            }
        ),
        triggers=_REVISION_SCHEMA_EXPECTATIONS[_STATEMENT_SHAPE_REVISION].triggers
        | frozenset(
            {
                "dependency_event_migration_receipts_are_immutable",
                "dependency_event_migration_receipts_reject_truncate",
                "dependency_event_timing_cardinality_is_valid",
                "dependency_event_timing_rows_match_event",
            }
        ),
    )
)

_REVISION_SCHEMA_EXPECTATIONS[_SCOPE_DECISION_REVISION] = (
    _RevisionSchemaExpectation(
        constraints=(
            _REVISION_SCHEMA_EXPECTATIONS[_ATTRIBUTABLE_STORAGE_REVISION]
            .constraints
            - frozenset(
                {
                    "dependency_event_scope_links_match_shape",
                    "dependency_event_scope_shape_is_valid",
                    "dependency_event_timings_match_statement",
                }
            )
            | frozenset(
                {
                    "ck_dependency_event_scope_decisions_actor",
                    "ck_dependency_event_scope_decisions_mode",
                    "dependency_event_scope_decisi_supersedes_scope_decision_id_fkey",
                    "dependency_event_scope_decision_links_match_shape",
                    "dependency_event_scope_decision_shape_is_valid",
                    "dependency_event_scope_decisions_event_id_fkey",
                    "dependency_event_scope_decisions_pkey",
                    "fk_dependency_event_scopes_scope_decision",
                    "uq_dependency_event_scope_decision_supersedes",
                    "uq_dependency_event_scopes_decision_dependency",
                }
            )
        ),
        triggers=(
            _REVISION_SCHEMA_EXPECTATIONS[_ATTRIBUTABLE_STORAGE_REVISION]
            .triggers
            - frozenset(
                {
                    "dependency_event_scope_link_is_valid",
                    "dependency_event_scope_links_match_shape",
                    "dependency_event_scope_shape_is_valid",
                    "dependency_event_timings_match_statement",
                    "external_party_statement_scopes_are_immutable",
                    "external_party_statement_scopes_reject_truncate",
                }
            )
            | frozenset(
                {
                    "dependency_event_scope_decision_link_is_valid",
                    "dependency_event_scope_decision_actor_is_valid",
                    "dependency_event_scope_decision_links_match_shape",
                    "dependency_event_scope_decision_shape_is_valid",
                    "dependency_event_scope_decisions_are_immutable",
                    "dependency_event_scope_decisions_reject_truncate",
                    "dependency_event_scope_links_are_immutable",
                    "dependency_event_scope_links_reject_truncate",
                    "dependency_events_receive_initial_scope_decision",
                    "verbal_statement_timings_match_shape",
                    "verbal_statements_match_shape",
                }
            )
        ),
    )
)

_REVISION_SCHEMA_EXPECTATIONS[_EVENT_EVIDENCE_ROLE_REVISION] = (
    _RevisionSchemaExpectation(
        constraints=_REVISION_SCHEMA_EXPECTATIONS[_SCOPE_DECISION_REVISION].constraints
        | frozenset(
            {
                "ck_dependency_event_evidence_actor",
                "dependency_event_evidence_event_id_fkey",
                "dependency_event_evidence_evidence_link_id_fkey",
                "dependency_event_evidence_pkey",
                "fk_dependency_evidence_sufficiencies_scope_link",
                "fk_operative_support_scope_link",
                "uq_dependency_evidence_sufficiency_scope_evidence",
            }
        ),
        triggers=_REVISION_SCHEMA_EXPECTATIONS[_SCOPE_DECISION_REVISION].triggers
        | frozenset(
            {
                "dependency_event_evidence_is_immutable",
                "dependency_event_evidence_is_valid",
                "dependency_event_evidence_reject_truncate",
                "dependency_evidence_sufficiency_scope_is_valid",
                "operative_event_evidence_scope_is_valid",
            }
        ),
    )
)
_REVISION_SCHEMA_EXPECTATIONS[_LEGACY_STATEMENT_BACKFILL_REVISION] = (
    _REVISION_SCHEMA_EXPECTATIONS[_EVENT_EVIDENCE_ROLE_REVISION]
)
_REVISION_SCHEMA_EXPECTATIONS[_EVENT_EVIDENCE_MIGRATION_REVISION] = (
    _REVISION_SCHEMA_EXPECTATIONS[_EVENT_EVIDENCE_ROLE_REVISION]
)
_REVISION_SCHEMA_EXPECTATIONS[_CONTRACT_STATEMENT_REVISION] = (
    _RevisionSchemaExpectation(
        constraints=(
            _REVISION_SCHEMA_EXPECTATIONS[_EVENT_EVIDENCE_MIGRATION_REVISION]
            .constraints
            - frozenset(
                {
                    "ck_evidence_links_event_ownership",
                    "evidence_links_event_id_fkey",
                }
            )
        ),
        triggers=(
            _REVISION_SCHEMA_EXPECTATIONS[_EVENT_EVIDENCE_MIGRATION_REVISION]
            .triggers
            - frozenset({"evidence_links_receive_event_evidence"})
            | frozenset(
                {
                    "dependency_event_evidence_has_one_owner",
                    "evidence_links_have_one_owner",
                }
            )
        ),
    )
)
_REVISION_SCHEMA_EXPECTATIONS[_RETIREMENT_ROLE_REPAIR_REVISION] = (
    _REVISION_SCHEMA_EXPECTATIONS[_CONTRACT_STATEMENT_REVISION]
)


@dataclass(frozen=True)
class _StatementSnapshot:
    content: dict[str, list[dict]]
    fixture_cases: frozenset[str]
    sha256: str


def _seed_216_statement_history(connection) -> None:
    """Populate only facts representable at completed #216."""
    connection.execute(
        text(
            """
            insert into projects
                (id, slug, name, is_synthetic, created_at, project_side_parties)
            values
                (222000, 'a222-statement-rehearsal',
                 'A222 statement rehearsal', true,
                 '2026-08-11 12:00:00+00', '[]'::jsonb);

            insert into external_orgs (id, name, org_type, aliases)
            values (222000, 'Equistar', 'utility', array['Equistar Chemicals']);

            insert into documents
                (id, project_id, sha256, filename, doc_type, doc_date, pages,
                 parse_status, created_at, registry_id, numbering_scheme)
            values
                (222000, 222000, repeat('1', 64), 'a222-minutes.pdf',
                 'minutes', '2025-01-16', 3, 'parsed',
                 '2026-08-11 12:00:00+00', 'a222-minutes',
                 'project-unique');

            insert into dependencies
                (id, project_id, ref_code, dep_type, title, external_org_id,
                 status, committed_date, evidence_required, created_at)
            values
                (222001, 222000, 'DEP-A222-CITED', 'utility_relocation',
                 'Cited statement Dependency', 222000, 'identified',
                 '2025-08-01', 'Completion evidence',
                 '2026-08-11 12:00:00+00'),
                (222002, 222000, 'DEP-A222-VERBAL', 'utility_relocation',
                 'Verbal statement Dependency', 222000, 'identified',
                 '2025-07-01', null, '2026-08-11 12:00:00+00'),
                (222003, 222000, 'DEP-A222-CLOSURE', 'utility_relocation',
                 'Closed-work Dependency', 222000, 'closed', null,
                 'Completion evidence', '2026-08-11 12:00:00+00'),
                (222004, 222000, 'DEP-A222-SCALAR', 'utility_relocation',
                 'Scalar-only committed date', 222000, 'identified',
                 '2025-09-01', null, '2026-08-11 12:00:00+00');

            insert into candidates
                (id, project_id, kind, payload_json, source_document_id,
                 source_pages, confidence, prompt_version, model,
                 citations_verified, state, merged_into, adjudicated_at,
                 created_at)
            values
                (222001, 222000, 'dependency',
                 jsonb_build_object(
                     'kind', 'dependency',
                     'fields', jsonb_build_object('utility_id', 'A222-1'),
                     'citations', '[]'::jsonb
                 ),
                 222000, array[1], 1.0, 'legacy-v1', 'legacy-model', true,
                 'accepted', 222001, '2026-08-11 12:00:00+00',
                 '2026-08-11 12:00:00+00'),
                (222002, 222000, 'event',
                 jsonb_build_object(
                     'kind', 'event',
                     'fields', jsonb_build_object('event_type', 'commitment'),
                     'citations', '[]'::jsonb
                 ),
                 222000, array[2], 1.0, 'legacy-v1', 'legacy-model', true,
                 'accepted', 222001, '2026-08-11 12:00:00+00',
                 '2026-08-11 12:00:00+00');

            insert into dependency_events
                (id, dependency_id, event_type, event_date, description,
                 created_by, created_at, committed_date, source_kind,
                 stated_party)
            values
                (222101, 222001, 'commitment', '2025-01-16',
                 'Equistar committed to June 1.',
                 'corridor:event-admission', '2026-08-11 12:00:00+00',
                 '2025-06-01', 'cited', 'Equistar'),
                (222102, 222002, 'commitment', '2025-02-01',
                 'Equistar committed by phone to July 1.',
                 'local:a222-recorder', '2026-08-11 12:00:00+00',
                 '2025-07-01', 'verbal', 'Equistar'),
                (222103, 222001, 'slip', '2025-03-01',
                 'Equistar moved completion to August 1.',
                 'corridor:event-admission', '2026-08-11 12:00:00+00',
                 '2025-08-01', 'cited', 'Equistar'),
                (222104, 222003, 'closure', '2025-04-01',
                 'Equistar reported the work complete.',
                 'corridor:event-admission', '2026-08-11 12:00:00+00',
                 null, 'cited', 'Equistar');

            insert into evidence_links
                (id, dependency_id, event_id, document_id, page_no, quote,
                 verified, satisfies_requirement, created_at)
            values
                (222201, 222001, 222101, 222000, 1,
                 'Equistar committed to June 1.', true, true,
                 '2026-08-11 12:00:00+00'),
                (222202, 222001, 222103, 222000, 2,
                 'Equistar moved completion to August 1.', true, false,
                 '2026-08-11 12:00:00+00'),
                (222203, 222003, 222104, 222000, 3,
                 'Equistar reported the work complete.', true, true,
                 '2026-08-11 12:00:00+00'),
                (222204, 222004, null, 222000, 1,
                 'Legacy scalar date support.', true, false,
                 '2026-08-11 12:00:00+00');

            insert into operative_support
                (id, dependency_id, evidence_link_id, role, field_name,
                 designated_by, designated_at)
            values
                (222301, 222001, 222201, 'publication', 'committed_date',
                 'local:a222-reviewer', '2026-08-11 12:00:00+00');

            insert into audit_log
                (id, actor, human_principal, action, entity_type, entity_id,
                 before_json, after_json, ts)
            values
                (222401, 'event-admission', null, 'admit_event',
                 'dependency_event', 222101, null,
                 jsonb_build_object('candidate_id', 222002),
                 '2026-08-11 12:00:00+00'),
                (222402, 'a222-reviewer', 'local:a222-reviewer',
                 'reconfirm_operative_support', 'dependency', 222001,
                 jsonb_build_object('evidence_link_id', 222201),
                 jsonb_build_object(
                     'evidence_link_id', 222201,
                     'successor_candidate_id', 222001
                 ),
                 '2026-08-11 12:00:00+00');

            insert into reconfirmation_receipts
                (audit_log_id, dependency_id, successor_candidate_id,
                 before_json, after_json, created_at)
            values
                (222402, 222001, 222001,
                 jsonb_build_object('evidence_link_id', 222201),
                 jsonb_build_object(
                     'evidence_link_id', 222201,
                     'successor_candidate_id', 222001
                 ),
                 '2026-08-11 12:00:00+00');

            insert into policy_runs
                (id, project_id, family, policy_approval_id, policy_version,
                 policy_sha256, abstention_reason_version, applied_count,
                 abstained_count, created_at)
            values
                (222501, 222000, 'event-admission', null, 'a222-policy-v1',
                 repeat('2', 64), 'a222-abstentions-v1', 1, 0,
                 '2026-08-11 12:00:00+00');

            insert into event_admission_outcomes
                (id, policy_run_id, family, candidate_id, outcome, reason,
                 dependency_event_id, created_at)
            values
                (222502, 222501, 'event-admission', 222002, 'admitted', null,
                 222101, '2026-08-11 12:00:00+00');

            insert into event_cohort_receipts
                (id, project_id, rule_version, input_run_ids, members,
                 member_count, content_sha256, created_at)
            values
                (222601, 222000, 'a222-cohort-v1', '[]'::jsonb,
                 jsonb_build_array('DEP-A222-CITED'), 1, repeat('3', 64),
                 '2026-08-11 12:00:00+00');
            """
        )
    )


def _seed_representable_216_verbal(connection) -> None:
    connection.execute(
        text(
            """
            insert into projects
                (id, slug, name, is_synthetic, created_at, project_side_parties)
            values
                (222900, 'a222-round-trip', 'A222 round trip', true,
                 '2026-08-11 12:00:00+00', '[]'::jsonb);

            insert into external_orgs (id, name, org_type, aliases)
            values (222900, 'Round Trip Party', 'utility', array[]::text[]);

            insert into dependencies
                (id, project_id, ref_code, dep_type, title, external_org_id,
                 status, committed_date, created_at)
            values
                (222901, 222900, 'DEP-A222-ROUND-TRIP',
                 'utility_relocation', 'Round-trip Dependency', 222900,
                 'identified', '2025-07-01',
                 '2026-08-11 12:00:00+00');

            insert into dependency_events
                (id, dependency_id, event_type, event_date, description,
                 created_by, created_at, committed_date, source_kind,
                 stated_party)
            values
                (222902, 222901, 'commitment', '2025-02-01',
                 'Round Trip Party committed by phone to July 1.',
                 'local:a222-recorder', '2026-08-11 12:00:00+00',
                 '2025-07-01', 'verbal', 'Round Trip Party');
            """
        )
    )


def _capture_216_statement_snapshot(connection) -> _StatementSnapshot:
    content = _capture_statement_data(connection)

    cases = set()
    event_rows = content["dependency_events"]
    cases.update(row["source_kind"] for row in event_rows)
    cases.update(row["event_type"] for row in event_rows)
    if connection.scalar(
        text(
            """
            select exists (
                select 1 from dependencies dependency
                where dependency.committed_date is not null
                  and not exists (
                      select 1 from dependency_events event
                      where event.dependency_id = dependency.id
                  )
            )
            """
        )
    ):
        cases.add("scalar-only-committed-date")
    if any(row["event_id"] is not None for row in content["evidence_links"]):
        cases.add("event-evidence")
    if content["operative_support"]:
        cases.add("dependency-evidence-role")
    if content["audit_log"]:
        cases.add("audit")
    if content["event_admission_outcomes"]:
        cases.add("admission-outcome")
    if content["reconfirmation_receipts"] and content["event_cohort_receipts"]:
        cases.add("immutable-receipt")

    canonical = json.dumps(
        content,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return _StatementSnapshot(
        content=content,
        fixture_cases=frozenset(cases),
        sha256=hashlib.sha256(canonical).hexdigest(),
    )


def _run_alembic(
    database_url: str,
    command: str,
    revision: str,
    *,
    expect_success: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [
            "uv",
            "run",
            "alembic",
            "-c",
            str(_ROOT / "alembic.ini"),
            command,
            revision,
        ],
        cwd=_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        check=False,
    )
    if expect_success:
        assert completed.returncode == 0, completed.stderr or completed.stdout
    else:
        assert completed.returncode != 0, "downgrade unexpectedly succeeded"
    return completed


def _remove_late_a217_immutability_guard(connection) -> None:
    """Reproduce databases that applied a217 before its source was amended."""
    connection.execute(
        text(
            """
            drop trigger if exists external_party_statement_events_are_immutable
                on dependency_events;
            drop trigger if exists external_party_statement_events_reject_truncate
                on dependency_events;
            drop trigger if exists external_party_statement_scopes_are_immutable
                on dependency_event_scopes;
            drop trigger if exists external_party_statement_scopes_reject_truncate
                on dependency_event_scopes;
            drop trigger if exists external_party_statement_timings_are_immutable
                on dependency_event_timings;
            drop trigger if exists external_party_statement_timings_reject_truncate
                on dependency_event_timings;
            drop trigger if exists external_party_statement_evidence_is_immutable
                on evidence_links;
            drop trigger if exists external_party_statement_evidence_reject_truncate
                on evidence_links;
            drop function if exists reject_external_party_statement_child_mutation();
            drop function if exists reject_external_party_statement_evidence_mutation();
            drop function if exists reject_external_party_statement_mutation();
            drop function if exists reject_external_party_statement_truncate();
            revoke all privileges on table projects, legacy_ledger_archives,
                dependency_events, dependency_event_scopes, dependency_event_timings,
                evidence_links from corridor_statement_retirement;
            """
        )
    )


def _statement_revision_path() -> tuple[str, ...]:
    config = Config(str(_ROOT / "alembic.ini"))
    scripts = ScriptDirectory.from_config(config)
    heads = scripts.get_heads()
    assert len(heads) == 1, (
        f"statement migration rehearsal requires one head: {heads}"
    )

    ordered = list(
        reversed(list(scripts.iterate_revisions(heads[0], _PRE_STATEMENT_REVISION)))
    )
    expected_parent = _PRE_STATEMENT_REVISION
    revisions = []
    for migration in ordered:
        assert migration.down_revision == expected_parent, (
            "statement migrations must remain one linear revision path: "
            f"{migration.revision} revises {migration.down_revision!r}, "
            f"expected {expected_parent}"
        )
        revisions.append(migration.revision)
        expected_parent = migration.revision
    return tuple(revisions)


def _capture_statement_data(connection) -> dict[str, list[dict]]:
    content = {}
    for table_name in _SNAPSHOT_TABLES:
        order_column = (
            "audit_log_id" if table_name == "reconfirmation_receipts" else "id"
        )
        content[table_name] = connection.scalar(
            text(
                f"""
                select coalesce(jsonb_agg(to_jsonb(snapshot_row)), '[]'::jsonb)
                from (
                    select * from {table_name} order by {order_column}
                ) snapshot_row
                """
            )
        )
    return content


def _capture_database_data(connection) -> dict[str, list[dict]]:
    table_names = connection.scalars(
        text(
            """
            select tablename
            from pg_tables
            where schemaname = 'public'
            order by tablename
            """
        )
    ).all()
    content = {}
    for table_name in table_names:
        quoted_table = connection.dialect.identifier_preparer.quote(table_name)
        content[table_name] = connection.scalar(
            text(
                f"""
                select coalesce(
                    jsonb_agg(
                        to_jsonb(snapshot_row)
                        order by to_jsonb(snapshot_row)::text
                    ),
                    '[]'::jsonb
                )
                from {quoted_table} snapshot_row
                """
            )
        )
    return content


def _capture_columns_constraints_triggers_fingerprint(connection) -> str:
    columns = connection.execute(
        text(
            """
            select table_name, ordinal_position, column_name, data_type,
                   is_nullable, coalesce(column_default, '')
            from information_schema.columns
            where table_schema = 'public'
            order by table_name, ordinal_position
            """
        )
    ).all()
    constraints = connection.execute(
        text(
            """
            select relation.relname, constraint_row.conname,
                   constraint_row.contype, constraint_row.convalidated,
                   pg_get_constraintdef(constraint_row.oid, true)
            from pg_constraint constraint_row
            join pg_class relation on relation.oid = constraint_row.conrelid
            join pg_namespace namespace on namespace.oid = relation.relnamespace
            where namespace.nspname = 'public'
            order by relation.relname, constraint_row.conname
            """
        )
    ).all()
    triggers = connection.execute(
        text(
            """
            select relation.relname, trigger_row.tgname, trigger_row.tgenabled,
                   pg_get_triggerdef(trigger_row.oid, true)
            from pg_trigger trigger_row
            join pg_class relation on relation.oid = trigger_row.tgrelid
            join pg_namespace namespace on namespace.oid = relation.relnamespace
            where namespace.nspname = 'public' and not trigger_row.tgisinternal
            order by relation.relname, trigger_row.tgname
            """
        )
    ).all()
    canonical = json.dumps(
        {
            "columns": [list(row) for row in columns],
            "constraints": [list(row) for row in constraints],
            "triggers": [list(row) for row in triggers],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _assert_a217_statement_data(
    connection,
    expected_216_data: dict[str, list[dict]],
    *,
    event_roles_migrated: bool = False,
    event_role_columns_added: bool = False,
) -> None:
    current = _capture_statement_data(connection)
    unchanged_tables = {
        "audit_log",
        "candidates",
        "dependencies",
        "documents",
        "event_admission_outcomes",
        "event_cohort_receipts",
        "external_orgs",
        "policy_runs",
        "projects",
        "reconfirmation_receipts",
    }
    if not event_roles_migrated and not event_role_columns_added:
        unchanged_tables.add("operative_support")
    for table_name in unchanged_tables:
        assert current[table_name] == expected_216_data[table_name]
    if event_role_columns_added:
        assert [
            {key: value for key, value in row.items() if key != "scope_link_id"}
            for row in current["operative_support"]
        ] == expected_216_data["operative_support"]

    dependencies = {
        row["id"]: row for row in expected_216_data["dependencies"]
    }
    for legacy_event in expected_216_data["dependency_events"]:
        dependency = dependencies[legacy_event["dependency_id"]]
        migrated = connection.scalar(
            text(
                """
                select to_jsonb(migrated_event)
                from (
                    select id, event_type, project_id, affected_external_org_id,
                           stated_external_org_id, scope_mode, timing_direction,
                           source_kind, stated_party, event_date, description,
                           created_by, created_at
                    from dependency_events
                    where id = :event_id
                ) migrated_event
                """
            ),
            {"event_id": legacy_event["id"]},
        )
        assert migrated == {
            "id": legacy_event["id"],
            "event_type": (
                "commitment"
                if legacy_event["event_type"] == "slip"
                else legacy_event["event_type"]
            ),
            "project_id": dependency["project_id"],
            "affected_external_org_id": dependency["external_org_id"],
            "stated_external_org_id": None,
            "scope_mode": "selected",
            "timing_direction": None,
            "source_kind": legacy_event["source_kind"],
            "stated_party": legacy_event["stated_party"],
            "event_date": legacy_event["event_date"],
            "description": legacy_event["description"],
            "created_by": legacy_event["created_by"],
            "created_at": legacy_event["created_at"],
        }
        scope_ids = connection.scalars(
            text(
                """
                select dependency_id
                from dependency_event_scopes
                where event_id = :event_id
                order by dependency_id
                """
            ),
            {"event_id": legacy_event["id"]},
        ).all()
        assert scope_ids == [legacy_event["dependency_id"]]

        timings = connection.execute(
            text(
                """
                select kind, text, precision, start_date, end_date
                from dependency_event_timings
                where event_id = :event_id
                order by kind
                """
            ),
            {"event_id": legacy_event["id"]},
        ).mappings().all()
        committed_date = legacy_event["committed_date"]
        if committed_date is None:
            assert timings == []
        elif legacy_event["source_kind"] == "verbal":
            exact_day = date.fromisoformat(committed_date)
            assert [dict(row) for row in timings] == [
                {
                    "kind": "new",
                    "text": committed_date,
                    "precision": "day",
                    "start_date": exact_day,
                    "end_date": exact_day,
                }
            ]
        else:
            assert [dict(row) for row in timings] == [
                {
                    "kind": "new",
                    "text": committed_date,
                    "precision": "legacy_unknown",
                    "start_date": None,
                    "end_date": None,
                }
            ]

    expected_sufficiencies = set()
    for legacy_evidence in expected_216_data["evidence_links"]:
        migrated = connection.scalar(
            text(
                """
                select to_jsonb(migrated_evidence)
                from (
                    select id, dependency_id, event_id, document_id, page_no,
                           quote, verified, satisfies_requirement, created_at
                    from evidence_links
                    where id = :evidence_id
                ) migrated_evidence
                """
            ),
            {"evidence_id": legacy_evidence["id"]},
        )
        expected = dict(legacy_evidence)
        if legacy_evidence["event_id"] is not None:
            if legacy_evidence["satisfies_requirement"]:
                expected_sufficiencies.add(
                    (legacy_evidence["dependency_id"], legacy_evidence["id"])
                )
            expected["dependency_id"] = None
            expected["satisfies_requirement"] = False
        assert migrated == expected

    actual_sufficiencies = set(
        connection.execute(
            text(
                """
                select dependency_id, evidence_link_id
                from dependency_evidence_sufficiencies
                order by dependency_id, evidence_link_id
                """
            )
        ).tuples()
    )
    assert actual_sufficiencies == expected_sufficiencies


def _assert_scope_decision_data(
    connection,
    expected_216_data: dict[str, list[dict]],
    *,
    scalar_backfilled: bool = False,
    event_roles_migrated: bool = False,
    event_role_columns_added: bool = False,
) -> None:
    """Assert the #224–#227 representation from independent legacy facts."""
    _assert_a217_statement_data(
        connection,
        expected_216_data,
        event_roles_migrated=event_roles_migrated,
        event_role_columns_added=event_role_columns_added,
    )
    legacy_events = expected_216_data["dependency_events"]
    decisions = connection.execute(
        text(
            """
            select decision.event_id, decision.scope_mode, decision.decided_by,
                   decision.supersedes_scope_decision_id, scope.dependency_id,
                   scope.recorded_by
            from dependency_event_scope_decisions decision
            left join dependency_event_scopes scope
              on scope.scope_decision_id = decision.id
            where decision.event_id = any(:event_ids)
            order by decision.event_id, scope.dependency_id
            """
        ),
        {"event_ids": [event["id"] for event in legacy_events]},
    ).mappings().all()
    assert [dict(row) for row in decisions] == [
        {
            "event_id": event["id"],
            "scope_mode": "selected",
            "decided_by": "corridor:statement-migration-v1",
            "supersedes_scope_decision_id": None,
            "dependency_id": event["dependency_id"],
            "recorded_by": "corridor:statement-migration-v1",
        }
        for event in legacy_events
    ]
    if not scalar_backfilled:
        return

    legacy_dependency_ids = {
        event["dependency_id"] for event in legacy_events
    }
    scalar_dependencies = [
        dependency
        for dependency in expected_216_data["dependencies"]
        if dependency["committed_date"] is not None
        and dependency["id"] not in legacy_dependency_ids
    ]
    scalar = connection.execute(
        text(
            """
            select event.event_type, event.project_id,
                   event.affected_external_org_id, event.stated_external_org_id,
                   event.attribution_state, event.source_kind, event.stated_party,
                   event.event_date, event.description, event.created_by,
                   timing.kind, timing.text, timing.precision,
                   timing.start_date, timing.end_date, decision.scope_mode,
                   decision.decided_by, scope.dependency_id, scope.recorded_by
            from dependency_events event
            join dependency_event_timings timing on timing.event_id = event.id
            join dependency_event_scope_decisions decision on decision.event_id = event.id
            join dependency_event_scopes scope on scope.scope_decision_id = decision.id
            where event.created_by = 'corridor:statement-migration-v1'
              and event.description like
                'Legacy scalar-only Committed Date migrated for Dependency %'
            order by scope.dependency_id
            """
        )
    ).mappings().all()
    assert [dict(row) for row in scalar] == [
        {
            "event_type": "commitment",
            "project_id": dependency["project_id"],
            "affected_external_org_id": dependency["external_org_id"],
            "stated_external_org_id": None,
            "attribution_state": "unresolved",
            "source_kind": "cited",
            "stated_party": None,
            "event_date": None,
            "description": (
                "Legacy scalar-only Committed Date migrated for Dependency "
                f"{dependency['id']}"
            ),
            "created_by": "corridor:statement-migration-v1",
            "kind": "new",
            "text": dependency["committed_date"],
            "precision": "legacy_unknown",
            "start_date": None,
            "end_date": None,
            "scope_mode": "selected",
            "decided_by": "corridor:statement-migration-v1",
            "dependency_id": dependency["id"],
            "recorded_by": "corridor:statement-migration-v1",
        }
        for dependency in scalar_dependencies
    ]


def _assert_event_evidence_migration(
    connection, expected_216_data: dict[str, list[dict]]
) -> None:
    _assert_scope_decision_data(
        connection,
        expected_216_data,
        scalar_backfilled=True,
        event_roles_migrated=True,
        event_role_columns_added=True,
    )
    event_evidence = connection.execute(
        text(
            """
            select evidence.evidence_link_id, evidence.event_id,
                   link.document_id, link.page_no, link.quote, link.verified,
                   link.dependency_id, link.satisfies_requirement
            from dependency_event_evidence evidence
            join evidence_links link on link.id = evidence.evidence_link_id
            order by evidence.evidence_link_id
            """
        )
    ).mappings().all()
    legacy_event_evidence = [
        evidence
        for evidence in expected_216_data["evidence_links"]
        if evidence["event_id"] is not None
    ]
    assert [dict(row) for row in event_evidence] == [
        {
            "evidence_link_id": evidence["id"],
            "event_id": evidence["event_id"],
            "document_id": evidence["document_id"],
            "page_no": evidence["page_no"],
            "quote": evidence["quote"],
            "verified": evidence["verified"],
            "dependency_id": None,
            "satisfies_requirement": False,
        }
        for evidence in legacy_event_evidence
    ]
    roles = connection.execute(
        text(
            """
            select sufficiency.dependency_id, sufficiency.evidence_link_id,
                   scope.event_id, scope.dependency_id as scope_dependency_id,
                   support.id as publication_support_id,
                   support.scope_link_id = sufficiency.scope_link_id
                       as publication_scope_matches
                from dependency_evidence_sufficiencies sufficiency
            join dependency_event_scopes scope on scope.id = sufficiency.scope_link_id
            left join operative_support support
                 on support.evidence_link_id = sufficiency.evidence_link_id
                 and support.dependency_id = sufficiency.dependency_id
                order by sufficiency.id
                """
            )
        ).mappings().all()
    expected_support_by_evidence = {
        support["evidence_link_id"]: support
        for support in expected_216_data["operative_support"]
    }
    assert [dict(row) for row in roles] == [
        {
            "dependency_id": evidence["dependency_id"],
            "evidence_link_id": evidence["id"],
            "event_id": evidence["event_id"],
            "scope_dependency_id": evidence["dependency_id"],
            "publication_support_id": (
                expected_support_by_evidence[evidence["id"]]["id"]
                if evidence["id"] in expected_support_by_evidence
                else None
            ),
            "publication_scope_matches": (
                True if evidence["id"] in expected_support_by_evidence else None
            ),
        }
        for evidence in legacy_event_evidence
        if evidence["satisfies_requirement"]
    ]


def _assert_contracted_statement_data(
    connection, expected_216_data: dict[str, list[dict]]
) -> None:
    """Assert the contract keeps one structural owner and one readiness role."""
    columns = {
        (row["table_name"], row["column_name"])
        for row in connection.execute(
            text(
                """
                select table_name, column_name
                from information_schema.columns
                where table_schema = 'public'
                """
            )
        ).mappings()
    }
    assert ("evidence_links", "event_id") not in columns
    assert ("evidence_links", "satisfies_requirement") not in columns
    assert ("dependencies", "committed_date") in columns

    legacy_events = expected_216_data["dependency_events"]
    mappings = connection.execute(
        text(
            """
            select mapping.evidence_link_id, mapping.event_id
            from dependency_event_evidence mapping
            order by mapping.evidence_link_id
            """
        )
    ).mappings().all()
    assert [dict(row) for row in mappings] == [
        {"evidence_link_id": evidence["id"], "event_id": evidence["event_id"]}
        for evidence in expected_216_data["evidence_links"]
        if evidence["event_id"] is not None
    ]

    # The #216 fixture establishes exact days only for its Verbal history.
    # Cited scalars and scalar-only rows are now legacy-unknown statements,
    # so the compatibility date must clear rather than fabricate a day.
    expected_dates = {
        dependency["id"]: next(
            (
                event["committed_date"]
                for event in sorted(
                    legacy_events,
                    key=lambda item: (item["event_date"] or "", item["id"]),
                    reverse=True,
                )
                if event["dependency_id"] == dependency["id"]
                and event["source_kind"] == "verbal"
                and event["committed_date"] is not None
            ),
            None,
        )
        for dependency in expected_216_data["dependencies"]
    }
    projected = dict(
        connection.execute(
            text("select id, committed_date::text from dependencies order by id")
        ).all()
    )
    assert projected == expected_dates

    direct_roles = connection.execute(
        text(
            """
            select sufficiency.dependency_id, sufficiency.evidence_link_id
            from dependency_evidence_sufficiencies sufficiency
            where sufficiency.scope_link_id is null
            order by sufficiency.id
            """
        )
    ).all()
    assert direct_roles == []


def _assert_statement_data_at_revision(
    connection,
    *,
    expected_revision: str,
    expected_216_data: dict[str, list[dict]],
) -> None:
    if expected_revision == _PRE_STATEMENT_REVISION:
        assert _capture_statement_data(connection) == expected_216_data
        return
    if expected_revision == _STATEMENT_SHAPE_REVISION:
        _assert_a217_statement_data(connection, expected_216_data)
        return
    if expected_revision == _ATTRIBUTABLE_STORAGE_REVISION:
        _assert_a217_statement_data(connection, expected_216_data)
        rows = connection.execute(
            text(
                """
                select event.id, event.attribution_state,
                       event.stated_external_org_id,
                       receipt.original_event =
                           (to_jsonb(event) - 'attribution_state') as exact_receipt
                from dependency_events event
                join dependency_event_migration_receipts receipt
                  on receipt.event_id = event.id
                order by event.id
                """
            )
        ).mappings().all()
        assert len(rows) == len(expected_216_data["dependency_events"])
        assert all(row["exact_receipt"] for row in rows)
        assert all(
            row["attribution_state"]
            == ("resolved" if row["stated_external_org_id"] is not None else "unresolved")
            for row in rows
        )
        return
    if expected_revision == _SCOPE_DECISION_REVISION:
        _assert_a217_statement_data(connection, expected_216_data)
        assert connection.scalar(
            text("select count(*) from dependency_event_scope_decisions")
        ) == 0
        assert connection.scalar(
            text(
                "select count(*) from dependency_event_scopes "
                "where scope_decision_id is not null or recorded_by is not null"
            )
        ) == 0
        return
    if expected_revision == _EVENT_EVIDENCE_ROLE_REVISION:
        _assert_a217_statement_data(
            connection, expected_216_data, event_role_columns_added=True
        )
        assert connection.scalar(
            text("select count(*) from dependency_event_evidence")
        ) == 0
        assert connection.scalar(
            text(
                "select count(*) from dependency_evidence_sufficiencies "
                "where scope_link_id is not null"
            )
        ) == 0
        assert connection.scalar(
            text("select count(*) from operative_support where scope_link_id is not null")
        ) == 0
        return
    if expected_revision == _LEGACY_STATEMENT_BACKFILL_REVISION:
        _assert_scope_decision_data(
            connection,
            expected_216_data,
            scalar_backfilled=True,
            event_role_columns_added=True,
        )
        return
    if expected_revision == _EVENT_EVIDENCE_MIGRATION_REVISION:
        _assert_event_evidence_migration(connection, expected_216_data)
        return
    if expected_revision in {
        _CONTRACT_STATEMENT_REVISION,
        _RETIREMENT_ROLE_REPAIR_REVISION,
    }:
        _assert_contracted_statement_data(connection, expected_216_data)
        return
    raise AssertionError(
        "statement migration rehearsal needs explicit data expectations for "
        f"revision {expected_revision}"
    )


def _assert_migration_state(
    connection,
    *,
    expected_revision: str,
    expected_216_data: dict[str, list[dict]],
) -> None:
    assert connection.scalar(text("select version_num from alembic_version")) == (
        expected_revision
    )
    for table_name, expected_rows in expected_216_data.items():
        expected_count = len(expected_rows)
        if (
            table_name == "dependency_events"
            and expected_revision
            in {
                _LEGACY_STATEMENT_BACKFILL_REVISION,
                _EVENT_EVIDENCE_MIGRATION_REVISION,
                _CONTRACT_STATEMENT_REVISION,
                _RETIREMENT_ROLE_REPAIR_REVISION,
            }
        ):
            legacy_dependency_ids = {
                event["dependency_id"]
                for event in expected_216_data["dependency_events"]
            }
            expected_count += sum(
                dependency["committed_date"] is not None
                and dependency["id"] not in legacy_dependency_ids
                for dependency in expected_216_data["dependencies"]
            )
        assert connection.scalar(text(f"select count(*) from {table_name}")) == expected_count

    invalid_constraints = connection.execute(
        text(
            """
            select relation.relname, constraint_row.conname
            from pg_constraint constraint_row
            join pg_class relation on relation.oid = constraint_row.conrelid
            join pg_namespace namespace on namespace.oid = relation.relnamespace
            where namespace.nspname = 'public'
              and constraint_row.contype in ('c', 'f', 'p', 'u', 'x')
              and not constraint_row.convalidated
            order by relation.relname, constraint_row.conname
            """
        )
    ).all()
    assert invalid_constraints == []

    constraint_names = set(
        connection.scalars(
            text(
                """
                select constraint_row.conname
                from pg_constraint constraint_row
                join pg_class relation on relation.oid = constraint_row.conrelid
                join pg_namespace namespace on namespace.oid = relation.relnamespace
                where namespace.nspname = 'public'
                """
            )
        )
    )
    trigger_names = set(
        connection.scalars(
            text(
                """
                select trigger_row.tgname
                from pg_trigger trigger_row
                join pg_class relation on relation.oid = trigger_row.tgrelid
                join pg_namespace namespace on namespace.oid = relation.relnamespace
                where namespace.nspname = 'public'
                  and not trigger_row.tgisinternal
                  and trigger_row.tgenabled in ('O', 'A')
                """
            )
        )
    )
    schema_expectation = _REVISION_SCHEMA_EXPECTATIONS.get(expected_revision)
    assert schema_expectation is not None, (
        "statement migration rehearsal needs explicit schema expectations for "
        f"revision {expected_revision}"
    )
    assert schema_expectation.constraints <= constraint_names
    assert schema_expectation.triggers <= trigger_names

    disabled_triggers = connection.execute(
        text(
            """
            select relation.relname, trigger_row.tgname
            from pg_trigger trigger_row
            join pg_class relation on relation.oid = trigger_row.tgrelid
            join pg_namespace namespace on namespace.oid = relation.relnamespace
            where namespace.nspname = 'public'
              and not trigger_row.tgisinternal
              and trigger_row.tgenabled not in ('O', 'A')
            order by relation.relname, trigger_row.tgname
            """
        )
    ).all()
    assert disabled_triggers == []

    _assert_statement_data_at_revision(
        connection,
        expected_revision=expected_revision,
        expected_216_data=expected_216_data,
    )


def _upgrade_and_assert_revisions(
    database_url: str,
    engine,
    revisions: tuple[str, ...],
    expected_216_data: dict[str, list[dict]],
) -> None:
    for revision in revisions:
        _run_alembic(database_url, "upgrade", revision)
        with engine.connect() as connection:
            _assert_migration_state(
                connection,
                expected_revision=revision,
                expected_216_data=expected_216_data,
            )


def _assert_rejected(
    session_factory, statement: str, parameters: dict[str, int]
) -> None:
    with session_factory() as session, pytest.raises(DBAPIError, match="append-only"):
        session.execute(text(statement), parameters)


def _assert_unprivileged_writer_cannot_purge(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    do $$
                    begin
                        if not exists (
                            select 1 from pg_roles where rolname = 'a217_unprivileged_writer'
                        ) then
                            create role a217_unprivileged_writer nologin noinherit;
                        end if;
                    end;
                    $$;
                    grant usage on schema public to a217_unprivileged_writer;
                    """
                )
            )
            assert connection.scalar(
                text(
                    "select has_function_privilege("
                    "'a217_unprivileged_writer', "
                    "'public.purge_external_party_statement_rows(bigint, text)', "
                    "'execute')"
                )
            ) is False
        with engine.begin() as connection:
            connection.execute(text("set local role a217_unprivileged_writer"))
            with pytest.raises(DBAPIError, match="permission denied"):
                connection.execute(
                    text(
                        "select public.purge_external_party_statement_rows("
                        "0, 'retirement')"
                    )
                )
    finally:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "revoke all privileges on schema public "
                    "from a217_unprivileged_writer"
                )
            )
            connection.execute(text("drop role if exists a217_unprivileged_writer"))
        engine.dispose()


def test_statement_rehearsal_starts_at_completed_216_schema():
    """The rehearsal database has no post-#216 migration residue."""
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=_ROOT,
        error_cls=RuntimeError,
        database_prefix="a222_statement_",
        migration_revision=_PRE_STATEMENT_REVISION,
    ) as database:
        assert database.postgres_version.startswith("16.")
        assert database.current_revision == _PRE_STATEMENT_REVISION


def test_contract_head_removes_legacy_statement_authority_but_keeps_projection():
    """The live schema has one structured authority and one date projection."""
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=_ROOT,
        error_cls=RuntimeError,
        database_prefix="a230_contract_",
    ) as database:
        database_url = make_url(settings.database_url).set(database=database.name)
        engine = create_engine(database_url)
        try:
            with engine.connect() as connection:
                columns = {
                    (row["table_name"], row["column_name"])
                    for row in connection.execute(
                        text(
                            """
                            select table_name, column_name
                            from information_schema.columns
                            where table_schema = 'public'
                            """
                        )
                    ).mappings()
                }
                assert ("dependency_events", "dependency_id") not in columns
                assert ("dependency_events", "committed_date") not in columns
                assert ("evidence_links", "event_id") not in columns
                assert ("evidence_links", "satisfies_requirement") not in columns
                assert ("dependencies", "committed_date") in columns
        finally:
            engine.dispose()

    model_source = Path(models_module.__file__).read_text()
    adjudication_source = Path(adjudicate_module.__file__).read_text()
    assert "def event_id(" not in model_source
    assert "def satisfies_requirement(" not in model_source
    assert "committed_date=committed_date" not in adjudication_source


def test_populated_216_fixture_records_every_historical_statement_case():
    """The fixed snapshot names every legacy shape later revisions must preserve."""
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=_ROOT,
        error_cls=RuntimeError,
        database_prefix="a222_statement_",
        migration_revision=_PRE_STATEMENT_REVISION,
    ) as database:
        engine = create_engine(
            make_url(settings.database_url).set(database=database.name)
        )
        try:
            with engine.begin() as connection:
                _seed_216_statement_history(connection)
                snapshot = _capture_216_statement_snapshot(connection)
        finally:
            engine.dispose()

    assert snapshot.fixture_cases == frozenset(
        {
            "admission-outcome",
            "audit",
            "cited",
            "closure",
            "commitment",
            "dependency-evidence-role",
            "event-evidence",
            "immutable-receipt",
            "scalar-only-committed-date",
            "slip",
            "verbal",
        }
    )
    assert snapshot.sha256 == (
        "87855687ec19adeccbca8ab1be7a0edfa6dbe19ded1a3924afc48d0ebea6c33b"
    )


def test_populated_rehearsal_checks_receipts_round_trip_and_atomic_refusal():
    """Legacy rows round-trip; a later unrepresentable statement refuses."""
    revisions = _statement_revision_path()
    assert revisions

    with provision_disposable_postgres(
        settings.database_url,
        repo_root=_ROOT,
        error_cls=RuntimeError,
        database_prefix="a222_statement_",
        migration_revision=_PRE_STATEMENT_REVISION,
    ) as database:
        database_url = make_url(settings.database_url).set(database=database.name)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                _seed_216_statement_history(connection)
                base_snapshot = _capture_216_statement_snapshot(connection)
                _assert_migration_state(
                    connection,
                    expected_revision=_PRE_STATEMENT_REVISION,
                    expected_216_data=base_snapshot.content,
                )

            rendered_database_url = database_url.render_as_string(
                hide_password=False
            )
            _upgrade_and_assert_revisions(
                rendered_database_url,
                engine,
                revisions,
                base_snapshot.content,
            )

            for statement in (
                "update dependency_event_migration_receipts "
                "set original_event = '{}'::jsonb where event_id = 222101",
                "delete from dependency_event_migration_receipts "
                "where event_id = 222101",
                "truncate dependency_event_migration_receipts",
            ):
                with pytest.raises(DBAPIError, match="append-only"):
                    with engine.begin() as connection:
                        connection.execute(text(statement))

            with engine.connect() as connection:
                head_snapshot = _capture_database_data(connection)
                head_schema = _capture_columns_constraints_triggers_fingerprint(
                    connection
                )
            refused = _run_alembic(
                rendered_database_url,
                "downgrade",
                _STATEMENT_SHAPE_REVISION,
                expect_success=False,
            )
            assert "cannot downgrade statement contract" in (
                refused.stderr + refused.stdout
            )
            with engine.connect() as connection:
                assert connection.scalar(text("select version_num from alembic_version")) == (
                    revisions[-1]
                )
                assert _capture_database_data(connection) == head_snapshot
                assert (
                    _capture_columns_constraints_triggers_fingerprint(connection)
                    == head_schema
                )
        finally:
            engine.dispose()


def test_attributable_storage_repairs_a217_applied_before_late_guards():
    """The next revision repairs, rather than assumes, amended a217 source."""
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=_ROOT,
        error_cls=RuntimeError,
        database_prefix="a223_historical_a217_",
        migration_revision=_PRE_STATEMENT_REVISION,
    ) as database:
        database_url = make_url(settings.database_url).set(database=database.name)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                _seed_216_statement_history(connection)
                base_snapshot = _capture_216_statement_snapshot(connection)

            rendered_database_url = database_url.render_as_string(
                hide_password=False
            )
            _run_alembic(
                rendered_database_url, "upgrade", _STATEMENT_SHAPE_REVISION
            )
            with engine.begin() as connection:
                _remove_late_a217_immutability_guard(connection)

            _run_alembic(
                rendered_database_url, "upgrade", _ATTRIBUTABLE_STORAGE_REVISION
            )
            with engine.connect() as connection:
                _assert_migration_state(
                    connection,
                    expected_revision=_ATTRIBUTABLE_STORAGE_REVISION,
                    expected_216_data=base_snapshot.content,
                )
                assert connection.scalar(
                    text(
                        "select has_table_privilege("
                        "'corridor_statement_retirement', "
                        "'dependency_events', 'delete')"
                    )
                ) is True
        finally:
            engine.dispose()


def test_event_role_migration_refuses_ambiguous_preexisting_scope_history():
    """A role predating #225 cannot be guessed onto a corrected scope."""
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=_ROOT,
        error_cls=RuntimeError,
        database_prefix="a227_ambiguous_scope_",
        migration_revision=_SCOPE_DECISION_REVISION,
    ) as database:
        database_url = make_url(settings.database_url).set(
            database=database.name
        ).render_as_string(hide_password=False)
        with database.session_factory() as session:
            project = Project(
                slug="a227-ambiguous-scope",
                name="A227 ambiguous scope",
                is_synthetic=True,
            )
            party = ExternalOrg(name="Equistar")
            session.add_all((project, party))
            session.flush()
            document = Document(
                project_id=project.id,
                sha256=hashlib.sha256(b"a227 ambiguous scope").hexdigest(),
                filename="a227-ambiguous-scope.pdf",
                doc_type="minutes",
                parse_status="parsed",
            )
            dependency = Dependency(
                project_id=project.id,
                ref_code="DEP-A227-1",
                dep_type="utility_relocation",
                title="Equistar relocation",
                external_org_id=party.id,
                status="identified",
            )
            session.add_all((document, dependency))
            session.flush()
            # This fixture intentionally targets the completed #224 schema.
            # Current writers are already contracted, so seed the historical
            # dual representation directly rather than pretending modern code
            # can write a schema that no longer exists.
            event_id = session.scalar(
                text(
                    """
                    insert into dependency_events
                        (project_id, affected_external_org_id,
                         stated_external_org_id, attribution_state, scope_mode,
                         event_type, source_kind, stated_party, event_date,
                         description, created_by)
                    values
                        (:project_id, :party_id, :party_id, 'resolved',
                         'selected', 'commitment', 'cited', 'Equistar',
                         '2025-01-16',
                         'Equistar will complete relocation by June 1.',
                         'corridor:event-admission')
                    returning id
                    """
                ),
                {"project_id": project.id, "party_id": party.id},
            )
            decision_id = session.scalar(
                text(
                    """
                    select id from dependency_event_scope_decisions
                    where event_id = :event_id
                    """
                ),
                {"event_id": event_id},
            )
            session.scalar(
                text(
                    """
                    insert into dependency_event_scopes
                        (event_id, scope_decision_id, dependency_id, recorded_by)
                    values
                        (:event_id, :decision_id, :dependency_id,
                         'corridor:event-admission')
                    returning id
                    """
                ),
                {
                    "event_id": event_id,
                    "decision_id": decision_id,
                    "dependency_id": dependency.id,
                },
            )
            session.execute(
                text(
                    """
                    insert into dependency_event_timings
                        (event_id, kind, text, precision, start_date, end_date)
                    values (:event_id, 'new', 'June 1', 'day',
                            '2025-06-01', '2025-06-01')
                    """
                ),
                {"event_id": event_id},
            )
            evidence_id = session.scalar(
                text(
                    """
                    insert into evidence_links
                        (dependency_id, event_id, document_id, page_no, quote,
                         verified, satisfies_requirement)
                    values (null, :event_id, :document_id, 1,
                            'Equistar will complete by June 1.', true, false)
                    returning id
                    """
                ),
                {"event_id": event_id, "document_id": document.id},
            )
            session.execute(
                text(
                    "insert into dependency_evidence_sufficiencies "
                    "(dependency_id, evidence_link_id) values (:dependency_id, :link_id)"
                ),
                {"dependency_id": dependency.id, "link_id": evidence_id},
            )
            record_statement_scope_decision(
                session,
                event_id=event_id,
                scope=StatementScope.selected((dependency.id,)),
                actor=HumanPrincipal("local:scope-corrector"),
            )
            session.commit()

        _run_alembic(database_url, "upgrade", _LEGACY_STATEMENT_BACKFILL_REVISION)
        refused = _run_alembic(
            database_url,
            "upgrade",
            _EVENT_EVIDENCE_MIGRATION_REVISION,
            expect_success=False,
        )
        assert "ambiguous historical Dependency scope" in (
            refused.stderr + refused.stdout
        )


def test_representable_216_history_round_trips_each_revision_exactly():
    """A compatible legacy history returns byte-for-byte through every step."""
    revisions = _statement_revision_path()
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=_ROOT,
        error_cls=RuntimeError,
        database_prefix="a222_statement_",
        migration_revision=_PRE_STATEMENT_REVISION,
    ) as database:
        database_url = make_url(settings.database_url).set(database=database.name)
        engine = create_engine(database_url)
        try:
            with engine.begin() as connection:
                _seed_representable_216_verbal(connection)
                original = _capture_statement_data(connection)

            rendered_database_url = database_url.render_as_string(
                hide_password=False
            )
            _upgrade_and_assert_revisions(
                rendered_database_url,
                engine,
                revisions,
                original,
            )

            downgrade_targets = tuple(
                reversed((_PRE_STATEMENT_REVISION, *revisions[:-1]))
            )
            for revision in downgrade_targets:
                _run_alembic(
                    rendered_database_url,
                    "downgrade",
                    revision,
                )
                with engine.connect() as connection:
                    _assert_migration_state(
                        connection,
                        expected_revision=revision,
                        expected_216_data=original,
                    )

            with engine.connect() as connection:
                assert _capture_statement_data(connection) == original
        finally:
            engine.dispose()


def test_contracted_statements_refuse_legacy_downgrade_and_seal_cited_rows():
    """Current attributable statements cannot be rewritten as legacy Slips."""
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=_ROOT,
        error_cls=RuntimeError,
        database_prefix="a217_statement_",
    ) as database:
        database_url = make_url(settings.database_url).set(
            database=database.name
        ).render_as_string(hide_password=False)
        session_factory = database.session_factory
        _assert_unprivileged_writer_cannot_purge(database_url)

        with session_factory() as session:
            project = Project(
                slug="a217-statement-migration",
                name="A217 statement migration",
                is_synthetic=True,
            )
            party = ExternalOrg(name="Equistar")
            session.add_all([project, party])
            session.flush()
            document = Document(
                project_id=project.id,
                sha256=hashlib.sha256(b"a217 statement migration").hexdigest(),
                filename="a217-statement-migration.pdf",
                doc_type="minutes",
                parse_status="parsed",
            )
            session.add(document)
            session.flush()
            session.add(
                DocPage(
                    document_id=document.id,
                    page_no=1,
                    text="Equistar will complete by June 1.",
                )
            )
            candidate = Candidate(
                project_id=project.id,
                kind="dependency",
                payload_json={
                    "kind": "dependency",
                    "fields": {"utility_id": "A217-1"},
                    "citations": [],
                },
                source_document_id=document.id,
                source_pages=[1],
                confidence=1.0,
                prompt_version="legacy-v1",
                model="legacy-model",
                citations_verified=True,
                state="accepted",
            )
            dependency = Dependency(
                project_id=project.id,
                ref_code="DEP-A217-1",
                dep_type="utility_relocation",
                title="Equistar relocation",
                external_org_id=party.id,
                status="identified",
            )
            session.add_all([candidate, dependency])
            session.flush()
            session.add(
                AuditLog(
                    actor="agent",
                    action="accept_candidate",
                    entity_type="dependency",
                    entity_id=dependency.id,
                    after_json={"candidate_id": candidate.id},
                )
            )
            verbal = record_external_party_statement(
                session,
                project_id=project.id,
                affected_external_org_id=party.id,
                stated_party="Equistar",
                stated_external_org_id=party.id,
                source_kind="verbal",
                event_date=date(2025, 1, 16),
                description="Equistar said it will complete by June 1.",
                new_timing=StatementTiming.day("June 1", date(2025, 6, 1)),
                scope=StatementScope.selected((dependency.id,)),
                created_by="local:a217-recorder",
            )
            ids = {
                "project": project.id,
                "party": party.id,
                "document": document.id,
                "dependency": dependency.id,
                "verbal": verbal.id,
            }
            session.commit()

        engine = create_engine(database_url)
        try:
            with engine.connect() as connection:
                before_data = _capture_database_data(connection)
                before_schema = _capture_columns_constraints_triggers_fingerprint(
                    connection
                )
            refused = _run_alembic(
                database_url,
                "downgrade",
                _PRE_STATEMENT_REVISION,
                expect_success=False,
            )
            assert "cannot downgrade statement contract" in (
                refused.stderr + refused.stdout
            )
            with engine.connect() as connection:
                assert connection.scalar(text("select version_num from alembic_version")) == (
                    _RETIREMENT_ROLE_REPAIR_REVISION
                )
                assert _capture_database_data(connection) == before_data
                assert (
                    _capture_columns_constraints_triggers_fingerprint(connection)
                    == before_schema
                )

        finally:
            engine.dispose()

        with session_factory() as session:
            cited = record_external_party_statement(
                session,
                project_id=ids["project"],
                affected_external_org_id=ids["party"],
                stated_party="Equistar",
                stated_external_org_id=ids["party"],
                source_kind="cited",
                event_date=date(2025, 1, 16),
                description="Equistar will complete by June 1.",
                new_timing=StatementTiming.day("June 1", date(2025, 6, 1)),
                scope=StatementScope.selected((ids["dependency"],)),
                created_by="corridor:event-admission",
                evidence=CitedStatementEvidence(
                    document_id=ids["document"],
                    page_no=1,
                    quote="Equistar will complete by June 1.",
                ),
            )
            ids["cited"] = cited.id
            ids["timing"] = cited.new_timing.id
            ids["scope"] = cited.scope_links[0].id
            ids["evidence"] = session.scalar(
                select(EvidenceLink.id)
                .join(
                    DependencyEventEvidence,
                    DependencyEventEvidence.evidence_link_id == EvidenceLink.id,
                )
                .where(DependencyEventEvidence.event_id == cited.id)
            )
            session.commit()

        _assert_rejected(
            session_factory,
            "update dependency_events set description = 'rewritten' where id = :event_id",
            {"event_id": ids["cited"]},
        )
        _assert_rejected(
            session_factory,
            "delete from dependency_event_timings where id = :timing_id",
            {"timing_id": ids["timing"]},
        )
        _assert_rejected(
            session_factory,
            "delete from dependency_event_scopes where id = :scope_id",
            {"scope_id": ids["scope"]},
        )
        _assert_rejected(
            session_factory,
            "update evidence_links set quote = 'rewritten' where id = :evidence_id",
            {"evidence_id": ids["evidence"]},
        )
        _assert_rejected(
            session_factory,
            "delete from dependency_event_evidence "
            "where evidence_link_id = :evidence_id",
            {
                "evidence_id": ids["evidence"],
            },
        )

        with session_factory() as session:
            plan = plan_retirement(session, ids["project"])
            retire_legacy_ledger(
                session,
                ids["project"],
                expected_sha256=plan.content_sha256,
                expected_dependency_count=1,
            )
            assert session.scalar(text("select current_user")) == "corridor"
            session.commit()
            assert session.scalars(
                select(Dependency).where(Dependency.project_id == ids["project"])
            ).all() == []
            assert session.scalars(
                select(DependencyEvent).where(
                    DependencyEvent.project_id == ids["project"]
                )
            ).all() == []


def test_contract_downgrade_refuses_multiscope_before_legacy_ddl():
    """A later multi-Dependency scope cannot be collapsed into one old event."""
    with provision_disposable_postgres(
        settings.database_url,
        repo_root=_ROOT,
        error_cls=RuntimeError,
        database_prefix="a230_multiscope_",
    ) as database:
        database_url = make_url(settings.database_url).set(
            database=database.name
        ).render_as_string(hide_password=False)
        session_factory = database.session_factory
        with session_factory() as session:
            project = Project(
                slug="a230-multiscope",
                name="A230 multi-scope",
                is_synthetic=True,
            )
            party = ExternalOrg(name="Equistar")
            session.add_all((project, party))
            session.flush()
            first = Dependency(
                project_id=project.id,
                ref_code="DEP-A230-1",
                dep_type="utility_relocation",
                title="Equistar first relocation",
                external_org_id=party.id,
                status="identified",
            )
            second = Dependency(
                project_id=project.id,
                ref_code="DEP-A230-2",
                dep_type="utility_relocation",
                title="Equistar second relocation",
                external_org_id=party.id,
                status="identified",
            )
            session.add_all((first, second))
            session.flush()
            # This shape is intentionally as close as possible to the
            # reversible legacy form: unresolved attribution, one exact day,
            # selected scope, and no correction. Only scope cardinality makes
            # it impossible to collapse faithfully.
            event = DependencyEvent(
                project_id=project.id,
                affected_external_org_id=party.id,
                stated_external_org_id=None,
                attribution_state="unresolved",
                scope_mode="selected",
                event_type="commitment",
                source_kind="cited",
                stated_party=None,
                event_date=None,
                description="Equistar will complete both relocations by June 1.",
                created_by="corridor:event-admission",
            )
            session.add(event)
            session.flush()
            decision = session.scalar(
                select(DependencyEventScopeDecision).where(
                    DependencyEventScopeDecision.event_id == event.id
                )
            )
            assert decision is not None
            session.add_all(
                (
                    DependencyEventTiming(
                        event_id=event.id,
                        kind="new",
                        text="June 1",
                        precision="day",
                        start_date=date(2025, 6, 1),
                        end_date=date(2025, 6, 1),
                    ),
                    DependencyEventScope(
                        event_id=event.id,
                        scope_decision_id=decision.id,
                        dependency_id=first.id,
                        recorded_by="corridor:event-admission",
                    ),
                    DependencyEventScope(
                        event_id=event.id,
                        scope_decision_id=decision.id,
                        dependency_id=second.id,
                        recorded_by="corridor:event-admission",
                    ),
                )
            )
            session.commit()

        engine = create_engine(database_url)
        try:
            with engine.connect() as connection:
                before_data = _capture_database_data(connection)
                before_schema = _capture_columns_constraints_triggers_fingerprint(
                    connection
                )
            refused = _run_alembic(
                database_url,
                "downgrade",
                _PRE_STATEMENT_REVISION,
                expect_success=False,
            )
            assert "cannot downgrade statement contract" in (
                refused.stderr + refused.stdout
            )
            with engine.connect() as connection:
                assert connection.scalar(text("select version_num from alembic_version")) == (
                    _RETIREMENT_ROLE_REPAIR_REVISION
                )
                assert _capture_database_data(connection) == before_data
                assert (
                    _capture_columns_constraints_triggers_fingerprint(connection)
                    == before_schema
                )
        finally:
            engine.dispose()
