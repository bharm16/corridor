"""Explaining a verified newer-document change is optional, read-only, and never
a decision (#360).

Every test drives the real coordination HTTP surface against real PostgreSQL
with a fake model adapter, the existing customer-access gate, and no paid run or
support update. The explanation sits beside the deterministic newer-document
question derived by the released routing policy (#348); the hostile fixtures — a
fabricated value, a relabelled dropped/unmatched distinction, a changed
conclusion narrated as unchanged, a fabricated failed-extraction cause,
authority-shaped output, a stale question, a cross-project record, and a missing
spend/configuration limit — are covered alongside the ordinary flow.
"""

from __future__ import annotations

from datetime import date
import re
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from corridor import access
from corridor.adjudicate import accept_candidate
from corridor.extraction_runs import declare_active_run, record_extraction_run
from corridor.models import (
    Candidate,
    DocPage,
    Document,
    ExternalOrg,
    Project,
    RevisionChangeExplanationConfiguration,
    RevisionChangeExplanationRequest,
)
from corridor.principals import HumanPrincipal
from corridor.revision_comparison import create_revision_comparison
from corridor.supersession import SupersessionDeclaration, register_supersessions
from corridor.support_update_routing import (
    changed_source_context,
    customer_consequences_by_dependency,
)

from model_client_support import RecordedAdapter
from access_support import seed_membership


OPERATOR = HumanPrincipal("local:revision-change-explanation")
REVIEWER = HumanPrincipal("local:revision-change-routing-reviewer")


class FakeAdapter(RecordedAdapter):
    """This module's identity on the one shared recording adapter."""

    adapter = "fake-revision-change-explanation"


@pytest.fixture
def adapter_box():
    return {"adapter": FakeAdapter(result=None)}


@pytest.fixture
def client(session, adapter_box):
    from corridor.web.app import (
        app,
        get_human_principal,
        get_revision_change_explanation_client_factory,
        get_session,
    )

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: OPERATOR
    app.dependency_overrides[get_revision_change_explanation_client_factory] = (
        lambda: (lambda configuration: adapter_box["adapter"])
    )
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# --- scenario builder (mirrors tests/test_support_update_routing.py) ---------


def _document(session, project, *, registry_id, sha, filename, page_text):
    document = Document(
        project_id=project.id,
        registry_id=registry_id,
        sha256=sha * 64,
        filename=filename,
        doc_type="other" if registry_id == "INDEX" else "matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush([document])
    session.add(
        DocPage(
            document_id=document.id,
            page_no=1,
            text=page_text,
            image_path=f"/tmp/{filename}.png",
        )
    )
    session.flush()
    return document


def _fields(*, utility_id="FOC1-1", station_from="100+00", baseline=None):
    fields = {
        "utility_id": utility_id,
        "external_org": "AT&T",
        "utility_type": "Telecom",
        "station_from": station_from,
    }
    if baseline is not None:
        fields["baseline"] = baseline
    return fields


def _quote(fields):
    return " ".join(
        value
        for value in (
            fields["utility_id"],
            fields["external_org"],
            fields["utility_type"],
            fields["station_from"],
            fields.get("baseline"),
        )
        if value
    )


def _candidate(project, document, fields):
    citation = {
        "document_id": document.id,
        "page": 1,
        "quote": _quote(fields),
        "verified": True,
    }
    return Candidate(
        project_id=project.id,
        kind="dependency",
        payload_json={
            "kind": "dependency",
            "fields": dict(fields),
            "citations": [dict(citation)],
        },
        source_document_id=document.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version="matrix-v1",
        model="test-model",
        citations_verified=True,
    )


def _completed_run(session, document, *candidates):
    run = record_extraction_run(
        session,
        document,
        prompt_version="matrix-v1",
        candidate_count=len(candidates),
        page_errors=0,
        candidates=candidates,
        model="test-model",
        schema_version="candidate-v1",
        allow_unsealed_legacy=True,
    )
    declare_active_run(session, document.id, run.id, principal=REVIEWER)
    session.flush()
    return run


def _seed(session, *, successor_rows=({},), predecessor_baseline=None):
    """Seed one superseded, admitted Constraint and its successor revision.

    ``successor_rows`` follows tests/test_support_update_routing.py: ``{}`` an
    unchanged row, ``{"utility_type": ...}`` a changed row, and ``()`` a dropped
    row (the completed successor has zero rows).
    """
    project = Project(
        slug=f"revision-change-{uuid4().hex}",
        name="Revision Change Explanation",
        is_synthetic=True,
    )
    session.add(project)
    session.flush([project])
    seed_membership(
        session,
        project,
        OPERATOR,
        designations=(access.COORDINATION, access.TECHNICAL_OPERATIONS),
    )
    if session.scalar(select(ExternalOrg).where(ExternalOrg.name == "AT&T")) is None:
        session.add(ExternalOrg(name="AT&T", aliases=[]))
        session.flush()

    predecessor_fields = _fields(baseline=predecessor_baseline)
    predecessor = _document(
        session,
        project,
        registry_id="REV-A",
        sha="a",
        filename="revision-a.pdf",
        page_text=_quote(predecessor_fields),
    )
    successor = _document(
        session,
        project,
        registry_id="REV-B",
        sha="b",
        filename="revision-b.pdf",
        page_text="FOC1-1 AT&T Telecom 100+00 200+00 IH-69 "
        + (predecessor_baseline or ""),
    )
    _document(
        session,
        project,
        registry_id="INDEX",
        sha="c",
        filename="index.pdf",
        page_text="REV-A superseded by REV-B on 2026-08-01",
    )

    predecessor_candidate = _candidate(project, predecessor, predecessor_fields)
    predecessor_run = _completed_run(session, predecessor, predecessor_candidate)
    dependency = accept_candidate(session, predecessor_candidate, principal=REVIEWER)
    dependency.evidence_required = "approved relocation closeout"
    register_supersessions(
        session,
        [
            SupersessionDeclaration(
                predecessor_registry_id="REV-A",
                successor_registry_id="REV-B",
                replacement_date=date(2026, 8, 1),
                source_registry_id="INDEX",
                source_page=1,
            )
        ],
        project_id=project.id,
    )

    successor_candidates = []
    for overrides in successor_rows:
        fields = _fields(baseline=predecessor_baseline)
        fields.update(overrides)
        successor_candidates.append(_candidate(project, successor, fields))
    successor_run = _completed_run(session, successor, *successor_candidates)

    create_revision_comparison(
        session,
        predecessor_extraction_run_id=predecessor_run.id,
        successor_extraction_run_id=successor_run.id,
    )
    session.flush()
    return {"project": project, "dependency": dependency}


# --- helpers to drive the surface --------------------------------------------


def _canon(value) -> str:
    return "unknown" if value is None else str(value)


def _consequence(session, project, dependency_id):
    return customer_consequences_by_dependency(session, project.id)[dependency_id]


def _valid_result(session, project, dependency_id):
    consequence = _consequence(session, project, dependency_id)
    context = changed_source_context(session, consequence)
    return {
        "supported_changes": [
            {
                "field": change.field,
                "before_value": _canon(change.before),
                "after_value": _canon(change.after),
                "explanation": (
                    f"The current revision records {change.field} as "
                    f"{_canon(change.after)}, where the replaced revision recorded "
                    f"{_canon(change.before)}."
                ),
            }
            for change in context.changed_values
        ],
        "preserved_alternatives": [],
        "preserved_distinctions": [],
        "completeness_limits": [
            {
                "side": "successor",
                "note": "Both revisions were fully read, so every compared row is present.",
            }
        ],
        "unknowns": [],
    }


def _explain_fields(body):
    block = re.search(r'/explain-revision-change"(.*?)</form>', body, re.S)
    assert block, body
    comparison = re.search(r'name="comparison_id" value="(\d+)"', block.group(1))
    finding = re.search(r'name="finding_id" value="(\d+)"', block.group(1))
    token = re.search(r'name="state_token" value="([a-f0-9]{64})"', block.group(1))
    assert comparison and finding and token, block.group(1)
    return comparison.group(1), finding.group(1), token.group(1)


def _declare_config(client, project, **overrides):
    data = {
        "model": "fake-model",
        "prompt_version": "revision_change_explanation_v1",
        "max_input_tokens": "50000",
        "max_output_tokens": "2000",
        "timeout_seconds": "30",
        "max_requests": "1",
        "retry_policy": "none",
        "retention_policy": "class_b_30_days",
        "observation_context": "internal_working_view",
    }
    data.update(overrides)
    return client.post(
        f"/operations/{project.slug}/revision-change-explanation/configuration",
        data=data,
        follow_redirects=False,
    )


def _request_explanation(client, project, dependency_id):
    body = client.get(f"/ledger/{project.slug}/{dependency_id}").text
    comparison_id, finding_id, state_token = _explain_fields(body)
    return client.post(
        f"/ledger/{project.slug}/{dependency_id}/explain-revision-change",
        data={
            "comparison_id": comparison_id,
            "finding_id": finding_id,
            "state_token": state_token,
        },
        follow_redirects=False,
    )


def _receipt_count(session, project) -> int:
    return session.scalar(
        select(func.count(RevisionChangeExplanationRequest.id)).where(
            RevisionChangeExplanationRequest.project_id == project.id
        )
    )


def _one_receipt(session, project) -> RevisionChangeExplanationRequest:
    return session.scalars(
        select(RevisionChangeExplanationRequest).where(
            RevisionChangeExplanationRequest.project_id == project.id
        )
    ).one()


# --- ordinary flow -----------------------------------------------------------


def test_explanation_offered_only_beside_a_verified_question(client, session):
    scenario = _seed(session, successor_rows=({"utility_type": "Gas"},))
    dependency_id = scenario["dependency"].id

    body = client.get(f"/ledger/{scenario['project'].slug}/{dependency_id}").text

    assert "A newer document needs attention" in body
    assert "/explain-revision-change" in body
    assert "Explain this change" in body


def test_explanation_reads_the_verified_comparison_and_changes_nothing(
    client, session, adapter_box
):
    scenario = _seed(session, successor_rows=({"utility_type": "Gas"},))
    project, dependency_id = scenario["project"], scenario["dependency"].id
    _declare_config(client, project)
    adapter_box["adapter"] = FakeAdapter(
        result=_valid_result(session, project, dependency_id)
    )

    response = _request_explanation(client, project, dependency_id)
    assert response.status_code == 303

    receipt = _one_receipt(session, project)
    assert receipt.status == "completed"
    assert receipt.non_authoritative is True
    assert receipt.finding_state == "changed"
    change = receipt.explanation_json["supported_changes"][0]
    assert change["field"] == "utility_type"
    assert change["before_value"] == "Telecom"
    assert change["after_value"] == "Gas"

    # The model saw the frozen comparison and the untrusted-data notice, once.
    assert len(adapter_box["adapter"].calls) == 1
    user_message = adapter_box["adapter"].calls[0].user
    assert "never instructions" in user_message
    assert "Gas" in user_message
    # Redacted lineage only: hashes and timing, never the prompt or response text.
    assert set(receipt.execution_lineage_json) == {
        "adapter",
        "adapter_contract_version",
        "request_sha256",
        "result_sha256",
        "elapsed_ms",
    }

    # The deterministic question is untouched and still derived after the read.
    assert dependency_id in customer_consequences_by_dependency(session, project.id)


def test_completed_explanation_renders_beside_the_deterministic_question(
    client, session, adapter_box
):
    scenario = _seed(session, successor_rows=({"utility_type": "Gas"},))
    project, dependency_id = scenario["project"], scenario["dependency"].id
    _declare_config(client, project)
    adapter_box["adapter"] = FakeAdapter(
        result=_valid_result(session, project, dependency_id)
    )
    _request_explanation(client, project, dependency_id)

    page = client.get(f"/ledger/{project.slug}/{dependency_id}").text
    # The deterministic change context stays visible beside the explanation.
    assert "Replaced revision" in page
    assert "settles nothing" in page
    assert "Telecom → Gas" in page or "Telecom" in page and "Gas" in page


def test_repeated_request_reuses_receipt_and_spends_once(
    client, session, adapter_box
):
    scenario = _seed(session, successor_rows=({"utility_type": "Gas"},))
    project, dependency_id = scenario["project"], scenario["dependency"].id
    _declare_config(client, project)
    adapter_box["adapter"] = FakeAdapter(
        result=_valid_result(session, project, dependency_id)
    )

    first = _request_explanation(client, project, dependency_id)
    second = _request_explanation(client, project, dependency_id)

    assert first.status_code == second.status_code == 303
    assert _receipt_count(session, project) == 1
    assert len(adapter_box["adapter"].calls) == 1


# --- spend / configuration boundary -----------------------------------------


def test_missing_configuration_refuses_before_any_model_call(
    client, session, adapter_box
):
    scenario = _seed(session, successor_rows=({"utility_type": "Gas"},))
    project, dependency_id = scenario["project"], scenario["dependency"].id
    adapter_box["adapter"] = FakeAdapter(
        result=_valid_result(session, project, dependency_id)
    )

    response = _request_explanation(client, project, dependency_id)

    assert response.status_code == 409
    assert adapter_box["adapter"].calls == []
    assert _receipt_count(session, project) == 0


def test_configuration_route_declares_and_rejects_invalid_bounds(client, session):
    scenario = _seed(session, successor_rows=({"utility_type": "Gas"},))
    project = scenario["project"]

    assert _declare_config(client, project).status_code == 303
    stored = session.scalars(
        select(RevisionChangeExplanationConfiguration).where(
            RevisionChangeExplanationConfiguration.project_id == project.id
        )
    ).one()
    assert stored.model == "fake-model"
    assert stored.created_by == OPERATOR.subject

    assert _declare_config(client, project, prompt_version="nope").status_code == 400
    assert _declare_config(client, project, max_input_tokens="0").status_code == 400
    assert _declare_config(client, project, max_input_tokens="").status_code == 400


def test_over_budget_request_refuses_before_the_model_and_records_a_receipt(
    client, session, adapter_box
):
    scenario = _seed(session, successor_rows=({"utility_type": "Gas"},))
    project, dependency_id = scenario["project"], scenario["dependency"].id
    _declare_config(client, project, max_input_tokens="1")
    adapter_box["adapter"] = FakeAdapter(
        result=_valid_result(session, project, dependency_id)
    )

    response = _request_explanation(client, project, dependency_id)

    assert response.status_code == 303
    receipt = _one_receipt(session, project)
    assert receipt.status == "budget_exhausted"
    assert receipt.explanation_json is None
    assert "no model call was made" in receipt.reason
    assert adapter_box["adapter"].calls == []


# --- hostile output ----------------------------------------------------------


def test_fabricated_value_is_refused(client, session, adapter_box):
    scenario = _seed(session, successor_rows=({"utility_type": "Gas"},))
    project, dependency_id = scenario["project"], scenario["dependency"].id
    _declare_config(client, project)
    result = _valid_result(session, project, dependency_id)
    result["supported_changes"][0]["after_value"] = "Fabricated"
    adapter_box["adapter"] = FakeAdapter(result=result)

    _request_explanation(client, project, dependency_id)

    receipt = _one_receipt(session, project)
    assert receipt.status == "validation_refused"
    assert receipt.explanation_json is None
    assert "does not match the verified comparison" in receipt.reason


def test_structured_authority_field_is_refused(client, session, adapter_box):
    scenario = _seed(session, successor_rows=({"utility_type": "Gas"},))
    project, dependency_id = scenario["project"], scenario["dependency"].id
    _declare_config(client, project)
    result = _valid_result(session, project, dependency_id)
    result["recommended_action"] = "settle the discrepancy"
    adapter_box["adapter"] = FakeAdapter(result=result)

    _request_explanation(client, project, dependency_id)

    receipt = _one_receipt(session, project)
    assert receipt.status == "validation_refused"
    assert "authority-shaped" in receipt.reason


def test_decision_shaped_prose_is_refused(client, session, adapter_box):
    scenario = _seed(session, successor_rows=({"utility_type": "Gas"},))
    project, dependency_id = scenario["project"], scenario["dependency"].id
    _declare_config(client, project)
    result = _valid_result(session, project, dependency_id)
    result["supported_changes"][0]["explanation"] = (
        "You should update the support to the new value."
    )
    adapter_box["adapter"] = FakeAdapter(result=result)

    _request_explanation(client, project, dependency_id)

    receipt = _one_receipt(session, project)
    assert receipt.status == "validation_refused"
    assert "recommends a decision" in receipt.reason


def test_changed_conclusion_as_unchanged_transfer_is_refused(
    client, session, adapter_box
):
    scenario = _seed(session, successor_rows=({"utility_type": "Gas"},))
    project, dependency_id = scenario["project"], scenario["dependency"].id
    _declare_config(client, project)
    result = _valid_result(session, project, dependency_id)
    result["supported_changes"][0]["explanation"] = (
        "The recorded conclusion is unchanged by this revision."
    )
    adapter_box["adapter"] = FakeAdapter(result=result)

    _request_explanation(client, project, dependency_id)

    receipt = _one_receipt(session, project)
    assert receipt.status == "validation_refused"
    assert "unchanged support transfer" in receipt.reason


def test_relabelled_dropped_distinction_is_refused(client, session, adapter_box):
    scenario = _seed(session, successor_rows=())  # honestly completed zero-row successor
    project, dependency_id = scenario["project"], scenario["dependency"].id
    _declare_config(client, project)
    # The finding is 'dropped'; relabelling the earlier-document row 'unmatched'
    # would recast a real drop as an unresolved match — refused.
    result = {
        "supported_changes": [],
        "preserved_alternatives": [],
        "preserved_distinctions": [
            {"row_ref": "P1", "distinction": "unmatched", "note": "no counterpart."}
        ],
        "completeness_limits": [],
        "unknowns": [],
    }
    adapter_box["adapter"] = FakeAdapter(result=result)

    _request_explanation(client, project, dependency_id)

    receipt = _one_receipt(session, project)
    assert receipt.finding_state == "dropped"
    assert receipt.status == "validation_refused"
    assert "relabels the verified finding" in receipt.reason


def test_fabricated_failed_extraction_cause_is_refused(client, session, adapter_box):
    scenario = _seed(session, successor_rows=())
    project, dependency_id = scenario["project"], scenario["dependency"].id
    _declare_config(client, project)
    result = {
        "supported_changes": [],
        "preserved_alternatives": [],
        "preserved_distinctions": [
            {
                "row_ref": "P1",
                "distinction": "dropped",
                "note": "This row disappeared because the extraction failed.",
            }
        ],
        "completeness_limits": [],
        "unknowns": [],
    }
    adapter_box["adapter"] = FakeAdapter(result=result)

    _request_explanation(client, project, dependency_id)

    receipt = _one_receipt(session, project)
    assert receipt.status == "validation_refused"
    assert "failed-extraction cause" in receipt.reason


def test_model_text_is_sanitized_before_storage(client, session, adapter_box):
    scenario = _seed(session, successor_rows=({"utility_type": "Gas"},))
    project, dependency_id = scenario["project"], scenario["dependency"].id
    _declare_config(client, project)
    result = _valid_result(session, project, dependency_id)
    result["supported_changes"][0]["explanation"] = "Clean\x07 change\x00 text."
    adapter_box["adapter"] = FakeAdapter(result=result)

    _request_explanation(client, project, dependency_id)

    receipt = _one_receipt(session, project)
    assert receipt.status == "completed"
    stored = receipt.explanation_json["supported_changes"][0]["explanation"]
    assert "\x07" not in stored and "\x00" not in stored
    assert stored == "Clean change text."


def test_transport_failure_is_a_receipt_not_an_escape(client, session, adapter_box):
    scenario = _seed(session, successor_rows=({"utility_type": "Gas"},))
    project, dependency_id = scenario["project"], scenario["dependency"].id
    _declare_config(client, project)
    adapter_box["adapter"] = FakeAdapter(raises=RuntimeError("boom"))

    response = _request_explanation(client, project, dependency_id)

    assert response.status_code == 303
    receipt = _one_receipt(session, project)
    assert receipt.status == "transport_failure"
    assert receipt.explanation_json is None


# --- binding: stale / cross-project ------------------------------------------


def test_stale_state_token_refuses_without_a_model_call(client, session, adapter_box):
    scenario = _seed(session, successor_rows=({"utility_type": "Gas"},))
    project, dependency_id = scenario["project"], scenario["dependency"].id
    _declare_config(client, project)
    body = client.get(f"/ledger/{project.slug}/{dependency_id}").text
    comparison_id, finding_id, _token = _explain_fields(body)

    response = client.post(
        f"/ledger/{project.slug}/{dependency_id}/explain-revision-change",
        data={
            "comparison_id": comparison_id,
            "finding_id": finding_id,
            "state_token": "0" * 64,
        },
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert adapter_box["adapter"].calls == []
    assert _receipt_count(session, project) == 0


def test_mismatched_finding_refuses_as_stale(client, session, adapter_box):
    scenario = _seed(session, successor_rows=({"utility_type": "Gas"},))
    project, dependency_id = scenario["project"], scenario["dependency"].id
    _declare_config(client, project)
    body = client.get(f"/ledger/{project.slug}/{dependency_id}").text
    comparison_id, finding_id, state_token = _explain_fields(body)

    response = client.post(
        f"/ledger/{project.slug}/{dependency_id}/explain-revision-change",
        data={
            "comparison_id": comparison_id,
            "finding_id": str(int(finding_id) + 999),
            "state_token": state_token,
        },
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert adapter_box["adapter"].calls == []
    assert _receipt_count(session, project) == 0


def test_cross_project_record_is_refused(client, session, adapter_box):
    scenario = _seed(session, successor_rows=({"utility_type": "Gas"},))
    project = scenario["project"]
    _declare_config(client, project)
    foreign = _seed(session, successor_rows=({"utility_type": "Gas"},))
    foreign_dependency_id = foreign["dependency"].id

    response = client.post(
        f"/ledger/{project.slug}/{foreign_dependency_id}/explain-revision-change",
        data={"comparison_id": "1", "finding_id": "1", "state_token": "0" * 64},
        follow_redirects=False,
    )

    assert response.status_code == 404
    assert adapter_box["adapter"].calls == []
    assert _receipt_count(session, project) == 0


# --- access ------------------------------------------------------------------


def test_explain_action_requires_customer_coordination(client, session, adapter_box):
    scenario = _seed(session, successor_rows=({"utility_type": "Gas"},))
    project, dependency_id = scenario["project"], scenario["dependency"].id
    # Exactly technical operations, without customer coordination: the explain
    # action is refused like every other coordination mutation (#331), and no
    # model is called.
    seed_membership(
        session, project, OPERATOR, designations=(access.TECHNICAL_OPERATIONS,)
    )

    assert (
        client.post(
            f"/ledger/{project.slug}/{dependency_id}/explain-revision-change",
            data={"comparison_id": "1", "finding_id": "1", "state_token": "0" * 64},
        ).status_code
        == 403
    )
    assert adapter_box["adapter"].calls == []
    assert _receipt_count(session, project) == 0


def test_non_member_cannot_reach_the_explain_action(client, session):
    scenario = _seed(session, successor_rows=({"utility_type": "Gas"},))
    project, dependency_id = scenario["project"], scenario["dependency"].id
    other = Project(slug=f"foreign-{uuid4().hex}", name="Foreign", is_synthetic=True)
    session.add(other)
    session.flush([other])
    # OPERATOR is not a member of ``other``; a non-member sees the same 404 as a
    # missing project, so project existence never leaks.
    assert (
        client.post(
            f"/ledger/{other.slug}/{dependency_id}/explain-revision-change",
            data={"comparison_id": "1", "finding_id": "1", "state_token": "0" * 64},
        ).status_code
        == 404
    )


def test_configuration_route_requires_technical_operations(client, session):
    scenario = _seed(session, successor_rows=({"utility_type": "Gas"},))
    project = scenario["project"]
    seed_membership(session, project, OPERATOR, designations=(access.COORDINATION,))

    assert _declare_config(client, project).status_code == 403
