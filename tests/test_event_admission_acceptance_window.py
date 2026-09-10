"""The supported upgrade window an Event Admission acceptance run must rehearse.

The acceptance run used to name its predecessor revision as a literal. #548
consolidated that revision away, and the only test that executed the path
skipped unless ``docker compose ps`` reported a running service from the
repository root — which CI never has, because PostgreSQL comes from the runner
image there. The run therefore kept asking Alembic for a revision the
executable graph no longer holds, on one developer machine, for a whole
consolidation (#639).

These tests execute the rehearsal itself against the configured PostgreSQL
admin URL, with no Compose probe in front of them: where PostgreSQL is
unreachable they fail, as ``tests/test_boot.py`` and ``tests/conftest.py``
already do. They also fix the four states a receipt naming a retired
predecessor holds — integrity-valid, historical, not replayable, not current —
so consolidation can never be read as corruption, and a retired predecessor can
never be read as current proof.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from corridor import policy
from corridor.config import settings
from corridor.event_admission import (
    UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
    UNKNOWN_SCOPE_POLICY_VERSION,
    _current_migration_head,
    _current_source_revision,
    acceptance_receipt_integrity_valid,
    canonical_event_admission_policy,
    read_event_admission_policy_status,
)
from corridor import event_admission_acceptance
from corridor.event_admission_acceptance import (
    RECEIPT_VERSION,
    RETIRED_PREDECESSOR_REFUSAL,
    SELECTION_RULE,
    _receipt_promotion_gates,
    _rehearse_predecessor_upgrade,
    activate_passing_acceptance,
    lift_unknown_scope_admission,
    migration_rehearsal_refusal,
    record_acceptance_receipt,
    suspend_unknown_scope_admission,
)
from corridor.migrations.policy import CURRENT_HEAD, SUPPORTED_FROM_REVISION
from corridor.models import Project

# The predecessor the historical receipts name. #548 consolidated it into the
# baseline builder, so it is retained source bytes in `migrations/versions`
# that Alembic does not load. It is written here on purpose: this is the one
# place that proves what such a receipt is, and it must not track the window.
RETIRED_PREDECESSOR = "a257c9e6f204"


@pytest.fixture
def project(session):
    project = Project(
        slug=f"acceptance-window-{uuid4().hex}",
        name="Acceptance Window",
        is_synthetic=True,
        project_side_parties=["LJA"],
    )
    session.add(project)
    session.flush([project])
    return project


def _receipt(session, project, *, predecessor: str) -> dict:
    """One internally consistent receipt whose only variable is its predecessor."""

    receipt = {
        "schema_version": RECEIPT_VERSION,
        "source_revision": _current_source_revision(),
        "migration_head": _current_migration_head(session),
        "selection_rule": SELECTION_RULE,
        "policy_version": UNKNOWN_SCOPE_POLICY_VERSION,
        "policy_sha256": policy.canonical_sha256(
            canonical_event_admission_policy(project, UNKNOWN_SCOPE_POLICY_VERSION)
        ),
        "reason_version": UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
        "opt_in": {
            "metrics": {
                "admission_count": 1,
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
            "admissions": [
                {"candidate_id": 1, "commitment_lineage_id": 1, "statement_event_id": 1}
            ],
            "work_items": [
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
            ],
        },
        "migration_rehearsal": {
            "predecessor": predecessor,
            "head": _current_migration_head(session),
            "status": "passed",
            "fresh_head": _current_migration_head(session),
            "fresh_status": "passed",
        },
        "authority_statement": "Only the deterministic unknown-scope class is authorized.",
    }
    receipt["gates"] = _receipt_promotion_gates(receipt)
    return receipt


def _record(session, project, *, predecessor: str):
    receipt_json = _receipt(session, project, predecessor=predecessor)
    return record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision=receipt_json["source_revision"],
        migration_head=receipt_json["migration_head"],
        receipt_json=receipt_json,
    )


# --- The window the run actually rehearses ----------------------------------


@pytest.mark.slow
def test_a_current_acceptance_run_rehearses_the_recorded_supported_window():
    """The range comes from migrations/policy.py, and it executes.

    With the retired literal in place this raises ``disposable database
    migration failed: ... Can't locate revision identified by 'a257c9e6f204'``,
    which is exactly the reported defect (#639).
    """

    rehearsal = _rehearse_predecessor_upgrade(
        settings.database_url, expected_head=CURRENT_HEAD
    )

    assert rehearsal == {
        "predecessor": SUPPORTED_FROM_REVISION,
        "head": CURRENT_HEAD,
        "status": "passed",
    }


@pytest.mark.slow
def test_a_revision_outside_the_window_is_refused_not_silently_substituted(
    monkeypatch,
):
    """No retired revision may stand in for the supported one."""

    monkeypatch.setattr(
        event_admission_acceptance, "SUPPORTED_FROM_REVISION", RETIRED_PREDECESSOR
    )

    with pytest.raises(ValueError) as refused:
        _rehearse_predecessor_upgrade(
            settings.database_url, expected_head=CURRENT_HEAD
        )

    # Verbatim the reported symptom: the executable graph does not hold it.
    assert f"Can't locate revision identified by '{RETIRED_PREDECESSOR}'" in str(
        refused.value
    )


def test_a_rehearsal_to_a_head_the_policy_does_not_record_is_refused():
    with pytest.raises(ValueError, match="migrations/policy.py records"):
        _rehearse_predecessor_upgrade(
            settings.database_url, expected_head="not-the-recorded-head"
        )


# --- What a receipt naming a retired predecessor is --------------------------


def test_a_retired_predecessor_is_named_as_unreplayable_not_as_corruption():
    """The reason is the window, and it is distinct from a failed rehearsal."""

    rehearsal = {
        "predecessor": RETIRED_PREDECESSOR,
        "head": CURRENT_HEAD,
        "status": "passed",
        "fresh_head": CURRENT_HEAD,
        "fresh_status": "passed",
    }

    assert migration_rehearsal_refusal(rehearsal) == RETIRED_PREDECESSOR_REFUSAL
    assert (
        migration_rehearsal_refusal({**rehearsal, "predecessor": SUPPORTED_FROM_REVISION})
        is None
    )


def test_a_receipt_naming_a_retired_predecessor_is_history_and_not_current_proof(
    session, project
):
    """Integrity-valid, historical, not replayable, not current — all four."""

    stored = _record(session, project, predecessor=RETIRED_PREDECESSOR)

    # Integrity-valid: the bytes and the indexed envelope still agree.
    assert acceptance_receipt_integrity_valid(stored) is True
    # Historical: it still says what it said, unrewritten.
    assert (
        stored.receipt_json["migration_rehearsal"]["predecessor"]
        == RETIRED_PREDECESSOR
    )
    # Not replayable, by name rather than by crash.
    assert (
        migration_rehearsal_refusal(stored.receipt_json["migration_rehearsal"])
        == RETIRED_PREDECESSOR_REFUSAL
    )
    assert stored.receipt_json["gates"]["fresh_and_predecessor_migrations_passed"] is False
    # Not current: it cannot activate, and it does not raise doing nothing.
    assert stored.status == "failed"
    assert activate_passing_acceptance(session, stored.id) is None

    status = read_event_admission_policy_status(session, project.id)

    assert status.status == "failed_newest_proof"
    assert status.proof_status == "failed_newest_proof"
    assert status.latest_receipt_integrity_valid is True


def test_activation_requires_a_receipt_that_rehearsed_the_supported_window(
    session, project
):
    retired = _record(session, project, predecessor=RETIRED_PREDECESSOR)
    assert activate_passing_acceptance(session, retired.id) is None

    current = _record(session, project, predecessor=SUPPORTED_FROM_REVISION)
    activation = activate_passing_acceptance(session, current.id)

    assert activation is not None
    assert read_event_admission_policy_status(session, project.id).status == "active"


def test_lifting_a_suspension_requires_a_new_current_receipt_not_a_retired_one(
    session, project
):
    activated = _record(session, project, predecessor=SUPPORTED_FROM_REVISION)
    assert activate_passing_acceptance(session, activated.id) is not None
    suspend_unknown_scope_admission(
        session,
        project_id=project.id,
        reason="receipt reproduction paused",
        recorded_by="local:operations",
    )

    _record(session, project, predecessor=RETIRED_PREDECESSOR)
    with pytest.raises(ValueError, match="current passing proof is required"):
        lift_unknown_scope_admission(
            session, project_id=project.id, recorded_by="local:human-lift"
        )

    reproved = _record(session, project, predecessor=SUPPORTED_FROM_REVISION)
    lifted = lift_unknown_scope_admission(
        session, project_id=project.id, recorded_by="local:human-lift"
    )

    assert lifted.acceptance_receipt_id == reproved.id
    assert read_event_admission_policy_status(session, project.id).status == "active"
