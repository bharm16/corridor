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
)
from corridor.legacy_ledger_archive import plan_retirement, retire_legacy_ledger
from corridor.m8_acceptance_database import provision_disposable_postgres
from corridor.models import (
    AuditLog,
    Candidate,
    Dependency,
    DependencyEvent,
    Document,
    EvidenceLink,
    ExternalOrg,
    Project,
)


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
    connection, expected_216_data: dict[str, list[dict]]
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
        "operative_support",
        "policy_runs",
        "projects",
        "reconfirmation_receipts",
    }
    for table_name in unchanged_tables:
        assert current[table_name] == expected_216_data[table_name]

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
        assert connection.scalar(text(f"select count(*) from {table_name}")) == (
            len(expected_rows)
        )

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


def test_populated_rehearsal_checks_each_revision_and_atomic_refusal():
    """Every post-#216 step preserves integrity; a lossy downgrade is atomic."""
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

            with engine.connect() as connection:
                head_snapshot = _capture_database_data(connection)
                head_schema = (
                    _capture_columns_constraints_triggers_fingerprint(connection)
                )

            refused = _run_alembic(
                rendered_database_url,
                "downgrade",
                _PRE_STATEMENT_REVISION,
                expect_success=False,
            )
            assert "cannot downgrade statement scope or timing" in (
                refused.stderr + refused.stdout
            )

            with engine.connect() as connection:
                _assert_migration_state(
                    connection,
                    expected_revision=revisions[-1],
                    expected_216_data=base_snapshot.content,
                )
                assert _capture_database_data(connection) == head_snapshot
                assert (
                    _capture_columns_constraints_triggers_fingerprint(connection)
                    == head_schema
                )
        finally:
            engine.dispose()


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


def test_external_statement_migration_preserves_verbal_slips_and_seals_cited_rows():
    """A safe #216 Verbal downgrades, upgrades, and remains append-only."""
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

        _run_alembic(database_url, "downgrade", _PRE_STATEMENT_REVISION)
        legacy_engine = create_engine(database_url)
        try:
            with legacy_engine.begin() as connection:
                connection.execute(
                    text(
                        "alter table dependency_events disable trigger "
                        "verbal_dependency_events_are_immutable"
                    )
                )
                connection.execute(
                    text(
                        "update dependency_events set event_type = 'slip' "
                        "where id = :event_id"
                    ),
                    {"event_id": ids["verbal"]},
                )
                connection.execute(
                    text(
                        "alter table dependency_events enable trigger "
                        "verbal_dependency_events_are_immutable"
                    )
                )
        finally:
            legacy_engine.dispose()

        _run_alembic(database_url, "upgrade", "head")
        with session_factory() as session:
            verbal = session.get(DependencyEvent, ids["verbal"])
            assert verbal is not None
            assert verbal.event_type == "commitment"
            assert verbal.new_timing.precision == "day"
            assert verbal.new_timing.start_date == verbal.new_timing.end_date == date(
                2025, 6, 1
            )

        # This is the safe #216-compatible downgrade that must not be stopped
        # by the inherited Verbal append-only trigger.
        _run_alembic(database_url, "downgrade", _PRE_STATEMENT_REVISION)
        legacy_engine = create_engine(database_url)
        try:
            with legacy_engine.connect() as connection:
                assert connection.scalar(
                    text(
                        "select committed_date from dependency_events "
                        "where id = :event_id"
                    ),
                    {"event_id": ids["verbal"]},
                ) == date(2025, 6, 1)
        finally:
            legacy_engine.dispose()

        _run_alembic(database_url, "upgrade", "head")
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
                select(EvidenceLink.id).where(EvidenceLink.event_id == cited.id)
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
            "update evidence_links set event_id = null, "
            "dependency_id = :dependency_id, satisfies_requirement = false "
            "where id = :evidence_id",
            {
                "dependency_id": ids["dependency"],
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
