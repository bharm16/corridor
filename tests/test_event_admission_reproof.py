"""Staleness decision and gate-7 declaration for scheduled Event Admission re-proof.

These rollback-scoped tests cover the parts that need no committed transaction: the
staleness read that recovery keys on (the same effective-policy selection ordinary
processing uses) and the gate-7 declaration that must be validated and retained before
the handler may run. The committed occurrence/claim/replay/activation behavior lives in
``tests/test_event_admission_reproof_runtime.py`` on the harness-owned database.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from corridor.db import Session, engine
from corridor.due_work import (
    DueWorkRefusal,
    EventAdmissionReproofDeclaration,
    HANDLER_EVENT_ADMISSION_REPROOF,
    configure_event_admission_reproof,
    _validate_stored_schedule,
)
from corridor.event_admission import (
    EVENT_ADMISSION_POLICY_VERSION,
    UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
    UNKNOWN_SCOPE_POLICY_VERSION,
    _current_migration_head,
    _current_source_revision,
)
from corridor.event_admission_acceptance import (
    RECEIPT_VERSION,
    SELECTION_RULE,
    _receipt_promotion_gates,
    activate_passing_acceptance,
    record_acceptance_receipt,
    suspend_unknown_scope_admission,
)
from corridor.event_admission_reproof import evaluate_reproof_need
from corridor.migrations.policy import SUPPORTED_FROM_REVISION
from corridor.models import DueWorkSchedule, Project
from corridor import policy
from corridor.event_admission import canonical_event_admission_policy


def acceptance_receipt(session, project, *, source_revision, migration_head, eligible):
    """A minimal internally-consistent pass/fail acceptance receipt for one project.

    Recording it exercises the real gate and identity validation; only the disposable
    clone provisioning is skipped. ``eligible`` toggles the nonzero-eligible gate.
    """

    receipt = {
        "schema_version": RECEIPT_VERSION,
        "source_revision": source_revision,
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
            "predecessor": SUPPORTED_FROM_REVISION,
            "head": migration_head,
            "status": "passed",
            "fresh_head": migration_head,
            "fresh_status": "passed",
        },
        "authority_statement": "Only the deterministic unknown-scope class is authorized.",
    }
    receipt["gates"] = _receipt_promotion_gates(receipt)
    return receipt


@pytest.fixture
def session():
    connection = engine.connect()
    transaction = connection.begin()
    db = Session(bind=connection)
    yield db
    db.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def project(session):
    project = Project(
        slug=f"reproof-unit-{uuid4().hex}",
        name="Re-proof Unit",
        is_synthetic=True,
        project_side_parties=["LJA"],
    )
    session.add(project)
    session.flush([project])
    return project


def _declaration(project_id: int, now: datetime) -> EventAdmissionReproofDeclaration:
    return EventAdmissionReproofDeclaration.released_hourly(
        project_id=project_id,
        configuration_version="event-admission-reproof-v1",
        policy_version=UNKNOWN_SCOPE_POLICY_VERSION,
        reason_version=UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
        selection_rule=SELECTION_RULE,
        starts_at=now.replace(minute=0, second=0, microsecond=0),
    )


NOW = datetime(2026, 8, 30, 7, 0, tzinfo=timezone.utc)


# --- staleness evaluation -------------------------------------------------------


def test_never_proved_class_is_not_stale(session, project):
    need = evaluate_reproof_need(session, project.id)
    assert need.proof_status == "no_applicable_proof"
    assert need.is_stale is False
    assert need.effective_policy_version == EVENT_ADMISSION_POLICY_VERSION


def test_current_passing_and_active_class_is_not_stale(session, project):
    stored = record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision=_current_source_revision(),
        migration_head=_current_migration_head(session),
        receipt_json=acceptance_receipt(
            session,
            project,
            source_revision=_current_source_revision(),
            migration_head=_current_migration_head(session),
            eligible=True,
        ),
    )
    assert activate_passing_acceptance(session, stored.id) is not None

    need = evaluate_reproof_need(session, project.id)
    assert need.proof_status == "passed_current"
    assert need.is_stale is False
    assert need.effective_status == "active"
    assert need.effective_policy_version == UNKNOWN_SCOPE_POLICY_VERSION


def test_passing_but_stale_bound_identities_is_stale(session, project):
    # A passing receipt pinned to a source revision that is not the deployed one is
    # exactly the "previously proved, now stale" state recovery keys on.
    record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision="0" * 40,
        migration_head=_current_migration_head(session),
        receipt_json=acceptance_receipt(
            session,
            project,
            source_revision="0" * 40,
            migration_head=_current_migration_head(session),
            eligible=True,
        ),
    )
    need = evaluate_reproof_need(session, project.id)
    assert need.proof_status == "stale_bound_identities"
    assert need.is_stale is True
    # The safe fallback while stale is the predecessor policy, not the opt-in rules.
    assert need.effective_policy_version == EVENT_ADMISSION_POLICY_VERSION


def test_standing_suspension_over_a_stale_receipt_is_still_stale(session, project):
    # Prove+activate on a current receipt, then a newer passing receipt goes stale;
    # a standing suspension does not hide the staleness from recovery.
    current = record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision=_current_source_revision(),
        migration_head=_current_migration_head(session),
        receipt_json=acceptance_receipt(
            session,
            project,
            source_revision=_current_source_revision(),
            migration_head=_current_migration_head(session),
            eligible=True,
        ),
    )
    assert activate_passing_acceptance(session, current.id) is not None
    suspend_unknown_scope_admission(
        session,
        project_id=project.id,
        reason="paused for audit",
        recorded_by="local:operations",
    )
    record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision="1" * 40,
        migration_head=_current_migration_head(session),
        receipt_json=acceptance_receipt(
            session,
            project,
            source_revision="1" * 40,
            migration_head=_current_migration_head(session),
            eligible=True,
        ),
    )
    need = evaluate_reproof_need(session, project.id)
    assert need.proof_status == "stale_bound_identities"
    assert need.is_stale is True
    assert need.effective_status == "suspended"


def test_failed_newest_proof_is_not_recovered_automatically(session, project):
    record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision=_current_source_revision(),
        migration_head=_current_migration_head(session),
        receipt_json=acceptance_receipt(
            session,
            project,
            source_revision=_current_source_revision(),
            migration_head=_current_migration_head(session),
            eligible=False,
        ),
    )
    need = evaluate_reproof_need(session, project.id)
    assert need.proof_status == "failed_newest_proof"
    assert need.is_stale is False


# --- gate-7 declaration ---------------------------------------------------------


def test_valid_declaration_persists_one_enabled_schedule(session, project):
    schedule = configure_event_admission_reproof(
        session, _declaration(project.id, NOW), now=NOW
    )
    assert schedule.handler_key == HANDLER_EVENT_ADMISSION_REPROOF
    assert schedule.disabled_at is None
    assert schedule.scope_json == {
        "project_id": project.id,
        "policy_version": UNKNOWN_SCOPE_POLICY_VERSION,
        "reason_version": UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
        "selection_rule": SELECTION_RULE,
    }
    # The retained configuration round-trips through the stored-schedule validator.
    _validate_stored_schedule(schedule)
    assert schedule.configuration_json["resources"]["model_token_budget"] == 0
    assert schedule.configuration_json["resources"]["clone_budget"] == 3


def test_a_second_configuration_disables_the_prior_schedule(session, project):
    first = configure_event_admission_reproof(
        session, _declaration(project.id, NOW), now=NOW
    )
    second = configure_event_admission_reproof(
        session,
        replace(
            _declaration(project.id, NOW),
            configuration_version="event-admission-reproof-v2",
        ),
        now=NOW,
    )
    session.flush()
    assert second.id != first.id
    refreshed = session.get(DueWorkSchedule, first.id)
    assert refreshed.disabled_at is not None
    assert second.disabled_at is None


@pytest.mark.parametrize(
    "override, match",
    [
        ({"model_token_budget": 1}, "resource declaration is invalid"),
        ({"notification_budget": 1}, "resource declaration is invalid"),
        ({"clone_budget": 0}, "resource declaration is invalid"),
        ({"clone_budget": 99}, "resource declaration is invalid"),
        ({"concurrency_limit": 2}, "resource declaration is invalid"),
        ({"cadence": "daily"}, "hourly UTC"),
        ({"policy_version": "Bad Version!"}, "policy version"),
        ({"selection_rule": "NOT A RULE"}, "selection rule"),
    ],
)
def test_invalid_declaration_is_refused_without_writing_a_schedule(
    session, project, override, match
):
    with pytest.raises(DueWorkRefusal, match=match):
        configure_event_admission_reproof(
            session, replace(_declaration(project.id, NOW), **override), now=NOW
        )
    assert (
        session.scalars(
            select(DueWorkSchedule).where(DueWorkSchedule.project_id == project.id)
        ).all()
        == []
    )
