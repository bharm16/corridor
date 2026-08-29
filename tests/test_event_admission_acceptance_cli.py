"""Operator CLI for event-admission replay status and explicit human control."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from corridor.db import Session, engine
from corridor.event_admission import (
    EVENT_ADMISSION_POLICY_VERSION,
    UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
    UNKNOWN_SCOPE_POLICY_VERSION,
    _current_migration_head,
    _current_source_revision,
    canonical_event_admission_policy,
)
from corridor.event_admission_acceptance import (
    EventAdmissionAcceptanceResult,
    RECEIPT_VERSION,
    SELECTION_RULE,
    activate_passing_acceptance,
    record_acceptance_receipt,
    suspend_unknown_scope_admission,
)
from corridor.event_admission_acceptance_cli import main
from corridor.models import Project
from corridor import policy


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    db = Session(bind=connection)
    yield db
    db.close()
    transaction.rollback()
    connection.close()


class _OpenSession:
    def __init__(self, session):
        self.session = session

    def __call__(self):
        session = self.session

        class Context:
            def __enter__(self):
                return session

            def __exit__(self, *_):
                return False

        return Context()


def _json_output(capsys) -> dict:
    captured = capsys.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


@pytest.fixture
def project(session):
    project = Project(
        slug="event-admission-acceptance-cli",
        name="Event Admission Acceptance CLI",
        is_synthetic=True,
        project_side_parties=["LJA"],
    )
    session.add(project)
    session.flush([project])
    return project


def _activation_receipt(session, project, *, eligible: bool) -> dict:
    migration_head = _current_migration_head(session)
    assert migration_head is not None
    receipt = {
        "schema_version": RECEIPT_VERSION,
        "source_revision": _current_source_revision(),
        "migration_head": migration_head,
        "selection_rule": SELECTION_RULE,
        "policy_version": UNKNOWN_SCOPE_POLICY_VERSION,
        "policy_sha256": policy.canonical_sha256(
            canonical_event_admission_policy(project, UNKNOWN_SCOPE_POLICY_VERSION)
        ),
        "reason_version": UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
        "opt_in": {
            "metrics": {
                "admission_count": 1 if eligible else 0,
                "false_party_attribution": 0,
                "false_dependency_scope": 0,
                "project_side_masquerade": 0,
                "cross_project_references": 0,
                "unauthorized_work_decisions": 0,
                "duplicates": 0,
                "protected_dependency_delta": 0,
                "protected_report_delta": 0,
                "invalid_evidence_or_receipts": 0,
            },
            "admissions": (
                [{"candidate_id": 1, "commitment_lineage_id": 1, "statement_event_id": 1}]
                if eligible
                else []
            ),
            "work_items": (
                [
                    {
                        "commitment_lineage_id": 1,
                        "statement_event_id": 1,
                        "source_candidate_id": 1,
                        "dependency_id": None,
                        "attention_reasons": [
                            "unknown_scope",
                            "missing_internal_owner",
                            "missing_next_action",
                        ],
                    }
                ]
                if eligible
                else []
            ),
        },
        "migration_rehearsal": {
            "predecessor": "a257c9e6f204",
            "head": migration_head,
            "status": "passed",
            "fresh_head": migration_head,
            "fresh_status": "passed",
        },
        "authority_statement": (
            "Only the deterministic unknown-scope Commitment class is authorized; "
            "models receive no write authority."
        ),
    }
    from corridor.event_admission_acceptance import _receipt_promotion_gates

    receipt["gates"] = _receipt_promotion_gates(receipt)
    return receipt


def test_replay_reports_passing_proof_separately_from_suspension_veto(
    monkeypatch, capsys
):
    monkeypatch.setattr(
        "corridor.event_admission_acceptance_cli.run_event_admission_acceptance",
        lambda _config: EventAdmissionAcceptanceResult(
            receipt_id=41,
            status="passed",
            activated=False,
            receipt_sha256="a" * 64,
            source_revision="b" * 40,
            migration_head="c257e1a8b426",
            clone_database_names=("proof_predecessor", "proof_opt_in"),
        ),
    )

    assert (
        main(
            [
                "replay",
                "--project-slug",
                "suspended-project",
                "--source-database-url",
                "postgresql://source",
                "--postgres-admin-url",
                "postgresql://admin",
                "--expected-clean-git-revision",
                "b" * 40,
            ]
        )
        == 0
    )
    payload = _json_output(capsys)
    assert payload["status"] == "passed"
    assert payload["activated"] is False
    assert payload["receipt_id"] == 41


def test_status_reports_suspension_and_permitted_operations(session, project, capsys):
    passed = record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision=_current_source_revision(),
        migration_head=_current_migration_head(session),
        receipt_json=_activation_receipt(session, project, eligible=True),
    )
    assert activate_passing_acceptance(session, passed.id) is not None
    suspend_unknown_scope_admission(
        session,
        project_id=project.id,
        reason="receipt reproduction paused",
        recorded_by="local:operations",
    )

    assert (
        main(
            [
                "status",
                "--project-slug",
                project.slug,
            ],
            session_factory=_OpenSession(session),
        )
        == 0
    )
    payload = _json_output(capsys)

    assert payload["status"] == "suspended"
    assert payload["proof_status"] == "passed_current"
    assert payload["effective_policy_version"] == EVENT_ADMISSION_POLICY_VERSION
    assert payload["allowed_operations"] == ["lift", "replay"]
    assert payload["latest_action"]["action"] == "suspend"
    assert payload["latest_action"]["acceptance_receipt_id"] == passed.id
    assert (
        payload["latest_action"]["policy_version"]
        == UNKNOWN_SCOPE_POLICY_VERSION
    )


def test_lift_requires_a_human_principal_and_reactivates(session, project, capsys):
    passed = record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision=_current_source_revision(),
        migration_head=_current_migration_head(session),
        receipt_json=_activation_receipt(session, project, eligible=True),
    )
    assert activate_passing_acceptance(session, passed.id) is not None
    suspend_unknown_scope_admission(
        session,
        project_id=project.id,
        reason="receipt reproduction paused",
        recorded_by="local:operations",
    )

    assert (
        main(
            [
                "lift",
                "--project-slug",
                project.slug,
                "--recorded-by",
                "local:human-lift",
            ],
            session_factory=_OpenSession(session),
        )
        == 0
    )
    payload = _json_output(capsys)

    assert payload["command"] == "lift"
    assert payload["policy_version"] == UNKNOWN_SCOPE_POLICY_VERSION
    assert payload["recorded_by"] == "local:human-lift"

    assert (
        main(
            [
                "lift",
                "--project-slug",
                project.slug,
                "--recorded-by",
                "corridor:event-admission-activation",
            ],
            session_factory=_OpenSession(session),
        )
        == 1
    )
    captured = capsys.readouterr()
    assert "human principal" in captured.err


def test_suspend_refuses_machine_identity_then_records_the_human_act(
    session, project, capsys
):
    passed = record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision=_current_source_revision(),
        migration_head=_current_migration_head(session),
        receipt_json=_activation_receipt(session, project, eligible=True),
    )
    assert activate_passing_acceptance(session, passed.id) is not None

    assert (
        main(
            [
                "suspend",
                "--project-slug",
                project.slug,
                "--reason",
                "receipt reproduction paused",
                "--recorded-by",
                "corridor:event-admission-activation",
            ],
            session_factory=_OpenSession(session),
        )
        == 1
    )
    assert "human principal" in capsys.readouterr().err

    assert (
        main(
            [
                "suspend",
                "--project-slug",
                project.slug,
                "--reason",
                "receipt reproduction paused",
                "--recorded-by",
                "local:operations",
            ],
            session_factory=_OpenSession(session),
        )
        == 0
    )
    payload = _json_output(capsys)
    assert payload["command"] == "suspend"
    assert payload["recorded_by"] == "local:operations"


def test_status_reports_failed_newest_proof(session, project, capsys):
    receipt = record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision=_current_source_revision(),
        migration_head=_current_migration_head(session),
        receipt_json=_activation_receipt(session, project, eligible=False),
    )

    assert (
        main(
            ["status", "--project-slug", project.slug],
            session_factory=_OpenSession(session),
        )
        == 0
    )
    payload = _json_output(capsys)

    assert payload["status"] == "failed_newest_proof"
    assert payload["latest_receipt"]["id"] == receipt.id
    assert payload["allowed_operations"] == ["replay"]
