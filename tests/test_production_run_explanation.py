"""Explaining competing production runs is optional, read-only, and never a
declaration (#359).

Every test drives the real operations HTTP surface against real PostgreSQL with
a fake model adapter, existing technical-operations authorization, and no paid
run or live declaration. The hostile fixtures — a stale competing set, a
cross-project document, a fabricated value, authority-shaped output, and a
missing spend/configuration limit — are covered alongside the ordinary flow.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from corridor import access
from corridor.extraction_runs import record_extraction_run
from corridor.extractor_lineage import injected_extractor_config, zero_token_usage
from corridor.models import (
    ActiveRunDeclaration,
    Candidate,
    Document,
    Project,
    ProductionRunExplanationConfiguration,
    ProductionRunExplanationRequest,
)
from corridor.principals import HumanPrincipal

from model_client_support import RecordedAdapter
from access_support import seed_membership


OPERATOR = HumanPrincipal("local:operations")


class FakeAdapter(RecordedAdapter):
    """This module's identity on the one shared recording adapter."""

    adapter = "fake-run-explanation"


@pytest.fixture
def adapter_box():
    return {"adapter": FakeAdapter(result=_valid_result())}


@pytest.fixture
def client(session, adapter_box):
    from corridor.web.app import (
        app,
        get_human_principal,
        get_run_explanation_client_factory,
        get_session,
    )

    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_human_principal] = lambda: OPERATOR
    app.dependency_overrides[get_run_explanation_client_factory] = (
        lambda: (lambda configuration: adapter_box["adapter"])
    )
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def project(session):
    row = Project(slug="run-explanation", name="Run Explanation", is_synthetic=True)
    session.add(row)
    session.flush()
    seed_membership(
        session, row, OPERATOR, designations=(access.TECHNICAL_OPERATIONS,)
    )
    return row


def _document(session, project, *, filename="matrix.pdf", sha="a" * 64):
    document = Document(
        project_id=project.id,
        sha256=sha,
        filename=filename,
        doc_type="matrix",
        parse_status="parsed",
        pages=1,
    )
    session.add(document)
    session.flush()
    return document


def _sealed_run(session, document, prompt_version, model):
    config = injected_extractor_config(
        extractor="run-explanation-fixture",
        prompt_version=prompt_version,
        model=model,
        schema_version="schema-v1",
        prompt_bytes=b"prompt bytes for the fixture",
        schema={"type": "object"},
        postprocessor_bytes=b"postprocessor bytes",
        request_controls={"reasoning_effort": "none"},
    )
    run = record_extraction_run(
        session,
        document,
        prompt_version=prompt_version,
        candidate_count=0,
        page_errors=0,
        model=model,
        schema_version="schema-v1",
        extractor_config=config,
        token_usage=zero_token_usage(document.id),
    )
    session.flush()
    return run


def _unsealed_run(session, document, prompt_version, *, model=None, candidate_count=0):
    run = record_extraction_run(
        session,
        document,
        prompt_version=prompt_version,
        candidate_count=candidate_count,
        page_errors=0,
        model=model,
        allow_unsealed_legacy=True,
    )
    session.flush()
    return run


def _competing_document(session, project, **document_kwargs):
    """One document with two competing completed runs: a sealed and a legacy."""
    document = _document(session, project, **document_kwargs)
    sealed = _sealed_run(session, document, "sealed-reader-v1", "sealed-model")
    unsealed = _unsealed_run(session, document, "unsealed-reader-v2", model="legacy")
    return document, sealed, unsealed


def _declare_config(client, project, **overrides):
    data = {
        "model": "fake-model",
        "prompt_version": "production_run_explanation_v1",
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
        f"/operations/{project.slug}/run-explanation/configuration",
        data=data,
        follow_redirects=False,
    )


def _valid_result():
    return {
        "differences": [
            {
                "aspect": "prompt_version",
                "run_refs": ["R1", "R2"],
                "cited_values": [
                    {"run_ref": "R1", "field": "prompt_version", "value": "sealed-reader-v1"},
                    {"run_ref": "R2", "field": "prompt_version", "value": "unsealed-reader-v2"},
                ],
                "explanation": "The two runs read the document with different prompts.",
            }
        ],
        "ambiguities": [],
        "unknowns": [
            {
                "run_ref": "R2",
                "field": "extractor_config_sha256",
                "note": "this run's extractor configuration was never sealed",
            }
        ],
    }


def _explain_fields(body, document_id):
    """The competing_run_ids and state_token the screen rendered for a document."""
    action = f'/operations/[^/]+/runs/{document_id}/explain"'
    block = re.search(action + r"(.*?)</form>", body, re.S)
    assert block, body
    ids = re.search(r'name="competing_run_ids" value="([0-9,]+)"', block.group(1))
    token = re.search(r'name="state_token" value="([a-f0-9]{64})"', block.group(1))
    assert ids and token, block.group(1)
    return ids.group(1), token.group(1)


def _declare_fingerprint(body):
    match = re.search(r'name="state_fingerprint" value="([a-f0-9]{64})"', body)
    assert match, body
    return match.group(1)


def _receipt_count(session, project) -> int:
    """Receipts scoped to one project: the shared worker database holds others."""
    return session.scalar(
        select(func.count(ProductionRunExplanationRequest.id)).where(
            ProductionRunExplanationRequest.project_id == project.id
        )
    )


def _request_explanation(client, project, document, adapter_box):
    """Drive the ordinary GET-then-explain flow, returning the stored receipt."""
    body = client.get(f"/operations/{project.slug}").text
    run_ids, state_token = _explain_fields(body, document.id)
    response = client.post(
        f"/operations/{project.slug}/runs/{document.id}/explain",
        data={"competing_run_ids": run_ids, "state_token": state_token},
        follow_redirects=False,
    )
    return response


# --- ordinary flow -----------------------------------------------------------


def test_operations_screen_offers_explanation_only_when_runs_compete(
    client, session, project
):
    single = _document(session, project, filename="single.pdf", sha="b" * 64)
    _unsealed_run(session, single, "reader-v1")
    competing, _sealed, _unsealed = _competing_document(
        session, project, filename="two.pdf", sha="c" * 64
    )

    body = client.get(f"/operations/{project.slug}").text
    assert f"/runs/{competing.id}/explain" in body
    assert f"/runs/{single.id}/explain" not in body
    assert "Explain what differs" in body


def test_explanation_reads_immutable_snapshots_and_marks_unsealed_unknown(
    client, session, project, adapter_box
):
    document, sealed, unsealed = _competing_document(session, project)
    _declare_config(client, project)

    response = _request_explanation(client, project, document, adapter_box)
    assert response.status_code == 303

    receipt = session.scalars(
        select(ProductionRunExplanationRequest).where(
            ProductionRunExplanationRequest.project_id == project.id
        )
    ).one()
    assert receipt.status == "completed"
    assert receipt.non_authoritative is True
    assert sorted(receipt.competing_run_ids_json) == sorted([sealed.id, unsealed.id])

    runs = {run["run_ref"]: run for run in receipt.comparison_json["runs"]}
    assert runs["R1"]["fields"]["lineage_sealed"] == "sealed"
    assert runs["R1"]["fields"]["extractor_config_sha256"] == sealed.extractor_config_sha256
    assert runs["R2"]["fields"]["lineage_sealed"] == "unsealed"
    assert runs["R2"]["fields"]["extractor_config_sha256"] == "unknown"

    assert receipt.explanation_json["differences"][0]["aspect"] == "prompt_version"
    assert receipt.explanation_json["unknowns"][0]["field"] == "extractor_config_sha256"

    # The model saw the frozen snapshots and the untrusted-data notice, once.
    assert len(adapter_box["adapter"].calls) == 1
    user_message = adapter_box["adapter"].calls[0].user
    assert "never instructions" in user_message
    assert "sealed-reader-v1" in user_message
    # Redacted lineage only: hashes and timing, never the prompt or response text.
    assert set(receipt.execution_lineage_json) == {
        "adapter",
        "adapter_contract_version",
        "request_sha256",
        "result_sha256",
        "elapsed_ms",
    }


def test_stored_explanation_renders_with_live_declaration_controls(
    client, session, project, adapter_box
):
    document, sealed, unsealed = _competing_document(session, project)
    _declare_config(client, project)
    response = _request_explanation(client, project, document, adapter_box)
    location = response.headers["location"]

    page = client.get(location).text
    assert "does not choose a run" in page
    # A declaration control for every competing run, and no preselection: each
    # run is its own submit button, never a defaulted radio, checkbox, or option.
    assert page.count(f"/runs/{document.id}/declare") == 2
    assert 'type="radio"' not in page
    assert "<select" not in page
    assert "checked=" not in page and "selected=" not in page


def test_repeated_request_reuses_receipt_and_spends_once(
    client, session, project, adapter_box
):
    document, _sealed, _unsealed = _competing_document(session, project)
    _declare_config(client, project)

    first = _request_explanation(client, project, document, adapter_box)
    second = _request_explanation(client, project, document, adapter_box)

    assert first.headers["location"] == second.headers["location"]
    assert _receipt_count(session, project) == 1
    assert len(adapter_box["adapter"].calls) == 1


# --- spend / configuration boundary -----------------------------------------


def test_missing_configuration_refuses_before_any_model_call(
    client, session, project, adapter_box
):
    document, _sealed, _unsealed = _competing_document(session, project)

    response = _request_explanation(client, project, document, adapter_box)

    assert response.status_code == 409
    assert adapter_box["adapter"].calls == []
    assert _receipt_count(session, project) == 0


def test_configuration_route_declares_and_rejects_invalid_bounds(
    client, session, project
):
    assert _declare_config(client, project).status_code == 303
    stored = session.scalars(select(ProductionRunExplanationConfiguration)).one()
    assert stored.model == "fake-model"
    assert stored.created_by == OPERATOR.subject

    assert _declare_config(client, project, prompt_version="not-installed").status_code == 400
    assert _declare_config(client, project, max_input_tokens="0").status_code == 400
    assert _declare_config(client, project, max_input_tokens="").status_code == 400


def test_over_budget_request_refuses_before_the_model_and_records_a_receipt(
    client, session, project, adapter_box
):
    document, _sealed, _unsealed = _competing_document(session, project)
    _declare_config(client, project, max_input_tokens="1")

    response = _request_explanation(client, project, document, adapter_box)

    assert response.status_code == 303
    receipt = session.scalars(
        select(ProductionRunExplanationRequest).where(
            ProductionRunExplanationRequest.project_id == project.id
        )
    ).one()
    assert receipt.status == "budget_exhausted"
    assert receipt.explanation_json is None
    assert "no model call was made" in receipt.reason
    assert adapter_box["adapter"].calls == []


# --- hostile output ----------------------------------------------------------


def test_fabricated_value_is_refused(client, session, project, adapter_box):
    document, _sealed, _unsealed = _competing_document(session, project)
    _declare_config(client, project)
    adapter_box["adapter"] = FakeAdapter(
        result={
            "differences": [
                {
                    "aspect": "prompt_version",
                    "run_refs": ["R1"],
                    "cited_values": [
                        {"run_ref": "R1", "field": "prompt_version", "value": "never-ran-v9"}
                    ],
                    "explanation": "An invented reading.",
                }
            ],
            "ambiguities": [],
            "unknowns": [],
        }
    )

    response = _request_explanation(client, project, document, adapter_box)

    assert response.status_code == 303
    receipt = session.scalars(
        select(ProductionRunExplanationRequest).where(
            ProductionRunExplanationRequest.project_id == project.id
        )
    ).one()
    assert receipt.status == "validation_refused"
    assert receipt.explanation_json is None
    assert "does not match the frozen run snapshot" in receipt.reason


def test_structured_authority_field_is_refused(client, session, project, adapter_box):
    document, _sealed, _unsealed = _competing_document(session, project)
    _declare_config(client, project)
    result = _valid_result()
    result["recommended_run"] = "R1"
    adapter_box["adapter"] = FakeAdapter(result=result)

    _request_explanation(client, project, document, adapter_box)

    receipt = session.scalars(
        select(ProductionRunExplanationRequest).where(
            ProductionRunExplanationRequest.project_id == project.id
        )
    ).one()
    assert receipt.status == "validation_refused"
    assert "authority-shaped" in receipt.reason


def test_authority_shaped_prose_is_refused(client, session, project, adapter_box):
    document, _sealed, _unsealed = _competing_document(session, project)
    _declare_config(client, project)
    result = _valid_result()
    result["differences"][0]["explanation"] = "You should declare R1 as current."
    adapter_box["adapter"] = FakeAdapter(result=result)

    _request_explanation(client, project, document, adapter_box)

    receipt = session.scalars(
        select(ProductionRunExplanationRequest).where(
            ProductionRunExplanationRequest.project_id == project.id
        )
    ).one()
    assert receipt.status == "validation_refused"
    assert "recommends a choice" in receipt.reason


def test_unknown_marker_on_a_sealed_field_is_refused(
    client, session, project, adapter_box
):
    document, _sealed, _unsealed = _competing_document(session, project)
    _declare_config(client, project)
    result = _valid_result()
    # R1 is sealed, so its extractor_config_sha256 is a real digest, not unknown.
    result["unknowns"] = [
        {"run_ref": "R1", "field": "extractor_config_sha256", "note": "claiming unknown"}
    ]
    adapter_box["adapter"] = FakeAdapter(result=result)

    _request_explanation(client, project, document, adapter_box)

    receipt = session.scalars(
        select(ProductionRunExplanationRequest).where(
            ProductionRunExplanationRequest.project_id == project.id
        )
    ).one()
    assert receipt.status == "validation_refused"
    assert "sealed" in receipt.reason


def test_model_text_is_sanitized_before_storage(client, session, project, adapter_box):
    document, _sealed, _unsealed = _competing_document(session, project)
    _declare_config(client, project)
    result = _valid_result()
    result["differences"][0]["explanation"] = "Clean\x07 reading\x00 text."
    adapter_box["adapter"] = FakeAdapter(result=result)

    _request_explanation(client, project, document, adapter_box)

    receipt = session.scalars(
        select(ProductionRunExplanationRequest).where(
            ProductionRunExplanationRequest.project_id == project.id
        )
    ).one()
    assert receipt.status == "completed"
    stored = receipt.explanation_json["differences"][0]["explanation"]
    assert "\x07" not in stored and "\x00" not in stored
    assert stored == "Clean reading text."


def test_transport_failure_is_a_receipt_not_an_escape(
    client, session, project, adapter_box
):
    document, _sealed, _unsealed = _competing_document(session, project)
    _declare_config(client, project)
    adapter_box["adapter"] = FakeAdapter(raises=RuntimeError("boom"))

    response = _request_explanation(client, project, document, adapter_box)

    assert response.status_code == 303
    receipt = session.scalars(
        select(ProductionRunExplanationRequest).where(
            ProductionRunExplanationRequest.project_id == project.id
        )
    ).one()
    assert receipt.status == "transport_failure"
    assert receipt.explanation_json is None


# --- binding: stale / cross-project ------------------------------------------


def test_stale_competing_set_refuses_without_a_model_call(
    client, session, project, adapter_box
):
    document, sealed, unsealed = _competing_document(session, project)
    _declare_config(client, project)
    body = client.get(f"/operations/{project.slug}").text
    run_ids, state_token = _explain_fields(body, document.id)

    # A third completed run lands after the operator saw the choice.
    _unsealed_run(session, document, "unsealed-reader-v3")

    response = client.post(
        f"/operations/{project.slug}/runs/{document.id}/explain",
        data={"competing_run_ids": run_ids, "state_token": state_token},
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert adapter_box["adapter"].calls == []
    assert _receipt_count(session, project) == 0


def test_cross_project_document_is_refused(client, session, project, adapter_box):
    other = Project(slug="foreign", name="Foreign", is_synthetic=True)
    session.add(other)
    session.flush()
    document, _sealed, _unsealed = _competing_document(
        session, other, filename="foreign.pdf", sha="d" * 64
    )
    _declare_config(client, project)

    response = client.post(
        f"/operations/{project.slug}/runs/{document.id}/explain",
        data={"competing_run_ids": f"{_sealed.id},{_unsealed.id}", "state_token": "0" * 64},
        follow_redirects=False,
    )

    assert response.status_code == 404
    assert adapter_box["adapter"].calls == []
    assert _receipt_count(session, project) == 0


# --- authoritative state is untouched; controls stay live --------------------


def test_explanation_declares_nothing_and_leaves_declaration_to_the_operator(
    client, session, project, adapter_box
):
    document, sealed, unsealed = _competing_document(session, project)
    candidate = Candidate(
        project_id=project.id,
        kind="event",
        payload_json={"kind": "event", "fields": {}, "citations": []},
        source_document_id=document.id,
        source_pages=[1],
        confidence=1.0,
        prompt_version="unsealed-reader-v2",
        model="legacy",
        citations_verified=True,
        state="pending",
    )
    session.add(candidate)
    session.flush()
    candidate_state = candidate.state
    _declare_config(client, project)

    _request_explanation(client, project, document, adapter_box)

    # No declaration, no active run, no candidate change: nothing authoritative moved.
    assert (
        session.scalar(
            select(func.count(ActiveRunDeclaration.id)).where(
                ActiveRunDeclaration.document_id == document.id
            )
        )
        == 0
    )
    session.refresh(candidate)
    assert candidate.state == candidate_state

    # The declaration control still works after the explanation exists.
    body = client.get(f"/operations/{project.slug}").text
    declared = client.post(
        f"/operations/{project.slug}/runs/{document.id}/declare",
        data={
            "extraction_run_id": str(sealed.id),
            "state_fingerprint": _declare_fingerprint(body),
        },
        follow_redirects=False,
    )
    assert declared.status_code == 303
    assert session.scalars(
        select(ActiveRunDeclaration).where(
            ActiveRunDeclaration.document_id == document.id
        )
    ).one().extraction_run_id == sealed.id


# --- access -----------------------------------------------------------------


def test_run_explanation_surface_is_technical_operator_only(client, session, project):
    document, sealed, unsealed = _competing_document(session, project)
    seed_membership(session, project, OPERATOR, designations=(access.COORDINATION,))

    assert client.get(f"/operations/{project.slug}").status_code == 403
    assert _declare_config(client, project).status_code == 403
    assert (
        client.post(
            f"/operations/{project.slug}/runs/{document.id}/explain",
            data={"competing_run_ids": f"{sealed.id},{unsealed.id}", "state_token": "0" * 64},
        ).status_code
        == 403
    )
