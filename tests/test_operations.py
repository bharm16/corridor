"""The technical-operations screen for production-run and policy recovery (#344).

These tests stay on the ordinary HTTP surface against PostgreSQL.  The test
principal is injected only at the HTTP identity seam; the application still
resolves the membership and technical-operations designation on every route.
"""

from __future__ import annotations

import re
from hashlib import sha256

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from corridor import access
from corridor import policy
from corridor.db import Session, engine
from corridor.event_admission import (
    UNKNOWN_SCOPE_ABSTENTION_REASON_VERSION,
    UNKNOWN_SCOPE_POLICY_VERSION,
    _current_migration_head,
    _current_source_revision,
    canonical_event_admission_policy,
)
from corridor.event_admission_acceptance import (
    RECEIPT_VERSION,
    SELECTION_RULE,
    _receipt_promotion_gates,
    activate_passing_acceptance,
    record_acceptance_receipt,
)
from corridor.extraction_runs import record_extraction_run
from corridor.models import (
    ActiveExtractionRun,
    ActiveRunDeclaration,
    Document,
    Project,
    RecordInclusionRequest,
)
from corridor.principals import HumanPrincipal
from corridor.web.app import app, get_human_principal, get_session

from access_support import seed_membership


OPERATOR = HumanPrincipal("local:operations")


@pytest.fixture
def session():
    connection = engine.connect()
    trans = connection.begin()
    current = Session(bind=connection)
    yield current
    current.close()
    trans.rollback()
    connection.close()


@pytest.fixture
def client(session):
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: OPERATOR
    with TestClient(app) as current:
        yield current
    app.dependency_overrides.clear()


def _project(session, slug: str) -> Project:
    project = Project(slug=slug, name=slug.title(), is_synthetic=True)
    session.add(project)
    session.flush()
    return project


def _document_with_runs(session, project: Project) -> tuple[Document, list[int]]:
    document = Document(
        project_id=project.id,
        sha256=sha256(f"{project.id}:source.pdf".encode()).hexdigest(),
        filename="utility-conflict-matrix.pdf",
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush()
    runs = [
        record_extraction_run(
            session,
            document,
            prompt_version=f"matrix-v{ordinal}",
            candidate_count=0,
            page_errors=0,
            model="test-model",
            schema_version="matrix-shape-v1",
            allow_unsealed_legacy=True,
        )
        for ordinal in (1, 2)
    ]
    session.flush()
    return document, [run.id for run in runs]


def _offered_state(body: str) -> str:
    match = re.search(r'name="state_fingerprint" value="([a-f0-9]{64})"', body)
    assert match, body
    return match.group(1)


def _passing_acceptance_receipt(session, project: Project) -> dict:
    """One valid, minimal receipt so HTTP exercises the real policy writer."""
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
            "predecessor": "a257c9e6f204",
            "head": migration_head,
            "status": "passed",
            "fresh_head": migration_head,
            "fresh_status": "passed",
        },
        "authority_statement": "Only the deterministic unknown-scope Commitment class is authorized.",
    }
    receipt["gates"] = _receipt_promotion_gates(receipt)
    return receipt


def test_screen_requires_the_technical_operator_designation_and_hides_foreign_project(
    client, session
):
    home = _project(session, "operations-home")
    foreign = _project(session, "operations-foreign")
    seed_membership(session, home, OPERATOR, designations=[access.COORDINATION])

    assert client.get(f"/operations/{home.slug}").status_code == 403
    assert client.get(f"/operations/{foreign.slug}").status_code == 404

    seed_membership(session, home, OPERATOR, designations=[access.TECHNICAL_OPERATIONS])
    response = client.get(f"/operations/{home.slug}")
    assert response.status_code == 200
    assert "Processing operations" in response.text
    assert "No generic retry is available" in response.text


def test_operator_declaration_is_attributable_stale_safe_and_hands_off_record_inclusion(
    client, session
):
    project = _project(session, "operations-declare")
    seed_membership(session, project, OPERATOR, designations=[access.TECHNICAL_OPERATIONS])
    document, run_ids = _document_with_runs(session, project)
    pending_before = session.get(RecordInclusionRequest, project.id)
    assert pending_before is not None
    dirty_before = pending_before.dirty_seq

    view = client.get(f"/operations/{project.slug}")
    assert view.status_code == 200
    assert "utility-conflict-matrix.pdf" in view.text
    assert "legacy configuration unavailable" in view.text.lower()
    state_fingerprint = _offered_state(view.text)

    declared = client.post(
        f"/operations/{project.slug}/runs/{document.id}/declare",
        data={
            "extraction_run_id": str(run_ids[1]),
            "state_fingerprint": state_fingerprint,
        },
        follow_redirects=False,
    )
    assert declared.status_code == 303
    assert declared.headers["location"].startswith(f"/operations/{project.slug}")

    session.expire_all()
    assert session.get(ActiveExtractionRun, document.id).extraction_run_id == run_ids[1]
    history = session.scalars(
        select(ActiveRunDeclaration).where(
            ActiveRunDeclaration.document_id == document.id
        )
    ).all()
    assert len(history) == 1
    assert history[0].declared_by == OPERATOR.subject
    assert session.get(RecordInclusionRequest, project.id).dirty_seq == dirty_before + 1

    # The original page is no longer an authoritative offer.  It cannot append
    # another declaration, processing occurrence, or project fact.
    stale = client.post(
        f"/operations/{project.slug}/runs/{document.id}/declare",
        data={
            "extraction_run_id": str(run_ids[1]),
            "state_fingerprint": state_fingerprint,
        },
        follow_redirects=False,
    )
    assert stale.status_code == 409
    assert len(
        session.scalars(
            select(ActiveRunDeclaration).where(
                ActiveRunDeclaration.document_id == document.id
            )
        ).all()
    ) == 1


def test_policy_status_and_unsupported_suspend_are_honest_and_side_effect_free(
    client, session
):
    project = _project(session, "operations-policy")
    seed_membership(session, project, OPERATOR, designations=[access.TECHNICAL_OPERATIONS])

    view = client.get(f"/operations/{project.slug}")
    assert view.status_code == 200
    assert "No applicable proof" in view.text
    refused = client.post(
        f"/operations/{project.slug}/unknown-scope/suspend",
        data={"reason": "investigate"},
        follow_redirects=False,
    )
    assert refused.status_code == 409
    assert "not active" in refused.text


def test_operator_screen_suspends_and_lifts_only_through_the_shared_policy_writer(
    client, session
):
    project = _project(session, "operations-suspension")
    seed_membership(session, project, OPERATOR, designations=[access.TECHNICAL_OPERATIONS])
    receipt = record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision=_current_source_revision(),
        migration_head=_current_migration_head(session),
        receipt_json=_passing_acceptance_receipt(session, project),
    )
    assert activate_passing_acceptance(session, receipt.id) is not None

    active = client.get(f"/operations/{project.slug}")
    suspended = client.post(
        f"/operations/{project.slug}/unknown-scope/suspend",
        data={"reason": "investigate drift", "state_fingerprint": _offered_state(active.text)},
        follow_redirects=False,
    )
    assert suspended.status_code == 303

    view = client.get(f"/operations/{project.slug}")
    assert "A passing proof remains suspended. It is not an active policy." in view.text
    lifted = client.post(
        f"/operations/{project.slug}/unknown-scope/lift",
        data={"state_fingerprint": _offered_state(view.text)},
        follow_redirects=False,
    )
    assert lifted.status_code == 303
    assert "Effective status:</b> active" in client.get(
        f"/operations/{project.slug}"
    ).text


def test_both_suspension_race_outcomes_refuse_the_stale_form_whole(client, session):
    """A competing act through the shared writer wins; the stale form appends nothing."""
    from corridor.event_admission_acceptance import (
        lift_unknown_scope_admission,
        suspend_unknown_scope_admission,
    )
    from corridor.models import EventAdmissionActivation

    project = _project(session, "operations-race")
    seed_membership(session, project, OPERATOR, designations=[access.TECHNICAL_OPERATIONS])
    receipt = record_acceptance_receipt(
        session,
        project_id=project.id,
        source_revision=_current_source_revision(),
        migration_head=_current_migration_head(session),
        receipt_json=_passing_acceptance_receipt(session, project),
    )
    assert activate_passing_acceptance(session, receipt.id) is not None

    def _history() -> list[str]:
        return [
            row.action
            for row in session.scalars(
                select(EventAdmissionActivation)
                .where(EventAdmissionActivation.project_id == project.id)
                .order_by(EventAdmissionActivation.id)
            )
        ]

    # Race one: the page offered Suspend while the policy was active, but a
    # competing operator's suspension commits first through the shared writer.
    active_view = client.get(f"/operations/{project.slug}")
    suspend_unknown_scope_admission(
        session,
        project_id=project.id,
        reason="competing operator got there first",
        recorded_by="local:competing-operator",
    )
    stale_suspend = client.post(
        f"/operations/{project.slug}/unknown-scope/suspend",
        data={
            "reason": "mine too",
            "state_fingerprint": _offered_state(active_view.text),
        },
        follow_redirects=False,
    )
    assert stale_suspend.status_code == 409
    assert _history() == ["activate", "suspend"]

    # Race two: the page offered Lift while suspended, but a competing lift
    # commits first.  The stale lift is refused whole rather than appending a
    # second activation on top of the already-current one.
    suspended_view = client.get(f"/operations/{project.slug}")
    lift_unknown_scope_admission(
        session, project_id=project.id, recorded_by="local:competing-operator"
    )
    stale_lift = client.post(
        f"/operations/{project.slug}/unknown-scope/lift",
        data={"state_fingerprint": _offered_state(suspended_view.text)},
        follow_redirects=False,
    )
    assert stale_lift.status_code == 409
    assert _history() == ["activate", "suspend", "activate"]
