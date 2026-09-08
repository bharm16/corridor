"""Measure preparation through its public durable workflow, without another write."""

from datetime import timedelta
from dataclasses import replace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from corridor.analytics import EventFamily
from corridor.issue_profile import effective_issue_inventory
from corridor.pilot_measurement import derive_measurement
from corridor.pilot_measurement_receipts import read_domain_receipts, observed_database_identity
from corridor.release_preparation_worker import record_failed_attempt

from test_pilot_measurement import period
from test_release_preparation import (
    store, adopted, configure, declared, ask,
    REQUESTED_AT, STARTED_AT, FINISHED_AT, CUTOFF,
)
# These HTTP fixtures use this module's attested, harness-owned database.
from test_packet_review_screen import project, client


@pytest.fixture
def session(runtime_database):
    from corridor.customer_routing import CustomerIdentity, bind_customer_environment

    engine = runtime_database.session_factory.kw["bind"]
    bind_customer_environment(engine, CustomerIdentity("fixture-customer", "fixture", "measurement-proof"))
    with runtime_database.session_factory() as scoped:
        yield scoped
        scoped.rollback()


def test_preparation_receipt_export_retains_failure_and_unchanged_declaration_retry(session, adopted):
    configure(session, adopted)
    declaration = declared(session, adopted)
    first = ask(session, adopted, declaration)
    record_failed_attempt(session, request_id=first.id, project_id=adopted.project.id,
                          reason="fixture renderer failed", started_at=STARTED_AT,
                          finished_at=FINISHED_AT)
    second = ask(session, adopted, declaration, idempotency_key="retry-after-failure",
                 requested_at=FINISHED_AT + timedelta(minutes=1))
    inventory = effective_issue_inventory(session, adopted.project.id, CUTOFF)
    window = period(project_id=adopted.project.id, start=REQUESTED_AT.replace(hour=0),
                    end=FINISHED_AT + timedelta(days=1), declared_at=REQUESTED_AT.replace(hour=0),
                    issue_profile_identity=inventory.profile_identity,
                    issue_profile_version=inventory.profile_version,
                    issue_profile_sha256=inventory.content_sha256,
                    database_identity=observed_database_identity(session),
                    source_population_complete=False)
    receipts = read_domain_receipts(session, [window])
    attempts = [e for e in receipts if e.family == EventFamily.PREPARATION_ATTEMPT]
    assert len(attempts) == 1
    assert attempts[0].occurred_at == FINISHED_AT
    assert attempts[0].payload["started_at"] == STARTED_AT.isoformat()
    assert attempts[0].payload["coverage_declaration_id"] == declaration.id
    assert attempts[0].payload["receipt"]["table"] == "release_preparation_attempts"
    report = derive_measurement([window], receipts)["periods"][0]
    preparation = {p["request_id"]: p for p in report["preparations"]}
    assert preparation[first.id]["outcome"] == "failed"
    assert preparation[first.id]["request_to_candidate_seconds"] is None
    assert preparation[first.id]["unchanged_declaration_reused"] is False
    assert preparation[second.id]["outcome"] == "open"
    assert preparation[second.id]["unchanged_declaration_reused"] is True
    assert not session.new and not session.dirty and not session.deleted


def test_export_reads_attestation_with_worker_capability_and_refuses_customer_relabeling(session, adopted):
    session.commit()
    engine = session.get_bind()
    worker = create_engine(engine.url.set(username="corridor_worker", password="corridor_worker"))
    try:
        with Session(worker) as reading:
            assert reading.scalar(text("select current_user")) == "corridor_worker"
            identity = observed_database_identity(reading)
            window = period(project_id=adopted.project.id, database_identity=identity,
                            source_population_complete=False)
            receipts = read_domain_receipts(reading, [window])
            assert all(e.binding.database_identity == identity for e in receipts)
            with pytest.raises(ValueError, match="customer/environment differs"):
                read_domain_receipts(reading, [replace(window, customer_id="another-customer")])
            with pytest.raises(ValueError, match="connected database origin"):
                read_domain_receipts(reading, [replace(window, database_identity="another-database")])
    finally:
        worker.dispose()


@pytest.mark.parametrize("outcome", ["keep_current", "unsupported"])
def test_packets_first_shown_after_save_enter_the_denominator(session, project, client, outcome):
    from corridor.analytics import capture_events
    from packet_review_support import Rendition, append_deltas, modify, subject, support
    from test_packet_review_screen import NOW, _revision

    revision = _revision(session, project, changes=1)
    with capture_events() as captured:
        assert client.get(f"/review/{project.slug}").status_code == 200
        first = captured.by_family(EventFamily.PACKET_SURFACING)[0]
        original_delta = first.payload["child_consequences"][0]["delta_id"]
        later = Rendition(session, project, "later-ucm.xlsx")
        fact, segment = later.capture(fact_type="station_from", value="3002+00", subject_key=subject(2))
        support(session, project, fact, segment)
        (new_delta,) = append_deltas(session, project, later, source_revision="2026-10", values=[
            modify(subject_key=subject(2), field_name="station_from", accepted_value="1002+00",
                   proposed_value="3002+00", baseline_revision=revision.revision_of[subject(2)])
        ])
        response = client.post(f"/review/{project.slug}", data={
            "item_key": first.payload["item_key"], "outcome": outcome, "child": [str(original_delta)],
        })
    assert response.status_code in {200, 400, 409, 422}
    window = period(project_id=project.id, start=NOW.replace(hour=0), end=NOW.replace(hour=0) + timedelta(days=1),
                    declared_at=NOW.replace(hour=0), binding=first.binding,
                    database_identity=first.binding.database_identity)
    report = derive_measurement([window], captured.events)["periods"][0]
    assert report["packet_denominator"] == 2
    assert new_delta.id in {i for packet in report["packets"] for i in packet["child_ids"]}


def test_later_retry_or_refusal_cannot_supply_an_older_requests_runtime_binding(session, adopted, monkeypatch):
    from corridor.analytics import capture_events

    configure(session, adopted)
    declaration = declared(session, adopted)
    monkeypatch.setenv("CORRIDOR_CODE_REVISION", "code-first")
    with capture_events() as captured:
        first = ask(session, adopted, declaration)
        monkeypatch.setenv("CORRIDOR_CODE_REVISION", "code-second")
        second = ask(session, adopted, declaration, idempotency_key="second-request",
                     requested_at=REQUESTED_AT + timedelta(minutes=3))
    first_event, second_event = captured.by_family(EventFamily.PREPARATION_REQUEST)
    refused = replace(second_event, event_id="refused-old-request", occurred_at=first.requested_at,
                      payload={**second_event.payload, "request_id": first.id,
                               "receipt_id": first.id, "outcome": "refused"})
    window = period(project_id=adopted.project.id, start=REQUESTED_AT.replace(hour=0),
                    end=REQUESTED_AT.replace(hour=0) + timedelta(days=1),
                    declared_at=REQUESTED_AT.replace(hour=0),
                    database_identity=observed_database_identity(session))
    unknown = read_domain_receipts(session, [window], events=[second_event, refused])
    old = next(e for e in unknown if e.family == EventFamily.PREPARATION_REQUEST
               and e.payload["request_id"] == first.id)
    assert "binding_event_ids" not in old.payload
    assert "code_revision" in old.payload["unavailable_binding_fields"]
    exact = read_domain_receipts(session, [window], events=[first_event, second_event, refused])
    requests = {e.payload["request_id"]: e for e in exact if e.family == EventFamily.PREPARATION_REQUEST}
    assert requests[first.id].payload["binding_event_ids"] == [first_event.event_id]
    assert requests[second.id].payload["binding_event_ids"] == [second_event.event_id]
    assert requests[first.id].binding.code_revision == "code-first"
    assert requests[second.id].binding.code_revision == "code-second"


@pytest.mark.parametrize("identity_style", ["canonical", "linked"])
def test_native_model_usage_reconciles_with_actual_billing_once(session, project, identity_style):
    from corridor.analytics import AnalyticsEvent
    from corridor.models import ExtractionRun
    from packet_review_support import Rendition
    from test_pilot_measurement import BINDING

    source = Rendition(session, project, "billed-model-input.xlsx")
    run = ExtractionRun(document_id=source.document.id, prompt_version="pilot-measurement-fixture-v1",
                        model="fixture-model", outcome="completed", candidate_count=0, page_errors=0)
    session.add(run)
    session.flush()
    origin = observed_database_identity(session)
    window = period(project_id=project.id, database_identity=origin)
    bill = AnalyticsEvent(
        family=EventFamily.PROVIDER_USAGE, binding=replace(BINDING, database_identity=origin),
        event_id="actual-provider-bill", occurred_at=run.completed_at,
        payload={"project_id": project.id, "usage_id": f"extraction_run:{run.id}" if identity_style == "canonical" else "provider-call:1",
                 **({"extraction_run_id": run.id} if identity_style == "linked" else {}),
                 "model": "fixture-model", "source_class": source.document.doc_type,
                 "prompt_version": run.prompt_version, "policy_version": "retained-bill-policy-v1",
                 "mode": "actual_call", "purpose": "production", "provider": "fixture-provider",
                 "actual_cost_usd": "0.125", "evidence_reference": "provider-bill:1"},
    )
    receipts = read_domain_receipts(session, [window], events=[bill])
    result = derive_measurement([window], [*receipts, bill])["periods"][0]["provider_cost"]
    assert result["actual_cost_usd"] == "0.125"
    assert result["counts"]["actual_call"] == 1
    assert result["unavailable_entries"] == []
    assert result["breakdown"][0]["native_receipts"] == [{"table": "extraction_runs", "id": run.id}]
    repeated = replace(bill, event_id="copied-billing-event")
    duplicate = derive_measurement([window], [*receipts, bill, repeated])["periods"][0]["provider_cost"]
    assert duplicate["actual_cost_usd"] == "0.125"
    assert duplicate["counts"]["actual_call"] == 1
    conflicting = replace(bill, event_id="wrong-model", payload={**bill.payload, "model": "another-model"})
    with pytest.raises(ValueError, match="contradicts retained model"):
        derive_measurement([window], [*receipts, conflicting])
    unknown = replace(bill, event_id="purpose-missing", payload={**bill.payload, "purpose": "unavailable"})
    assert derive_measurement([window], [*receipts, unknown])["periods"][0]["provider_cost"]["actual_cost_usd"] is None
    historical = replace(bill, event_id="historical", payload={**bill.payload,
                                                              "mode": "historical_experiment", "purpose": "experiment"})
    history_cost = derive_measurement([window], [*receipts, historical])["periods"][0]["provider_cost"]
    assert history_cost["historical_experiment_cost_usd"] == "0.125"
    assert history_cost["actual_cost_usd"] == "0.00"
