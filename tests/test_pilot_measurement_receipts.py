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


def test_real_source_and_delta_producers_bind_fresh_native_receipts(session, adopted, store, tmp_path, monkeypatch):
    import json
    from sqlalchemy import func, select
    from corridor.analytics import capture_events
    from corridor.later_revision import capture_later_revision
    from corridor.source_revision_declaration import RevisionDeclaration
    from corridor.principals import HumanPrincipal
    from corridor.source_delivery import (DeliveryBinding, DeliveryObservation, envelope_for_delivery,
        record_delivery)
    from corridor.source_intake import validate_and_stage

    from later_revision_support import BASELINE_ROWS, workbook_bytes
    from test_pilot_measurement import BINDING

    configure(session, adopted)
    inventory = effective_issue_inventory(session, adopted.project.id, CUTOFF)
    identity = observed_database_identity(session)
    deployment = replace(BINDING, code_revision="producer-first", database_identity=identity,
                         source_configuration={"identity": "fixture-folder", "version": "1"},
                         connector_configuration={"transport": "pull", "channel": "fixture-folder"},
                         issue_profile_identity=inventory.profile_identity,
                         issue_profile_version=inventory.profile_version, issue_profile_sha256=inventory.content_sha256,
                         template_identity=f"{inventory.output_template.identity}:{inventory.output_template.version}",
                         mapping_identity=f"{inventory.field_mapping.identity}:{inventory.field_mapping.version}")
    configuration = tmp_path / "measurement-binding.json"
    configuration.write_text(json.dumps(deployment.as_dict()))
    monkeypatch.setenv("CORRIDOR_ANALYTICS_BINDING_FILE", str(configuration))
    monkeypatch.setenv("CORRIDOR_CODE_REVISION", "producer-first")
    incoming = [list(row) for row in BASELINE_ROWS]
    incoming[0][3] = "18 in"
    body = workbook_bytes(tmp_path / "incoming.xlsx", incoming)
    project_id = adopted.project.id
    delivery_binding = DeliveryBinding(customer="fixture-customer", project_id=project_id,
                                       project_slug=adopted.project.slug, transport="pull",
                                       channel="fixture-folder", configuration_identity="fixture-folder", configuration_version="1")
    session.commit()
    with capture_events() as captured:
        started = session.scalar(select(func.now()))
        staged = validate_and_stage(body, "incoming.xlsx", customer_id="fixture-customer", project_id=project_id)
        observation = DeliveryObservation(external_identity="fixture-ucm", external_version="revision-2",
                                          content_digest=staged.sha256, bytes_reference=str(staged.stored_path),
                                          metadata={"filename": "incoming.xlsx", "byte_count": len(body), "source_class": "matrix"})
        delivered = record_delivery(session, delivery_binding, observation, disposition="stored",
                                    service_identity="fixture-connector", run_identity="first-arrival")
        with session.begin_nested():
            result = capture_later_revision(session, project=adopted.project, staged=staged,
                                            envelope=envelope_for_delivery(session, delivered.delivery_id),
                                            declaration=RevisionDeclaration(declared_by=HumanPrincipal("local:coordinator")))
        session.commit()
    window = period(project_id=project_id, start=started, end=started + timedelta(hours=1), declared_at=started,
                    binding=deployment, database_identity=identity,
                    issue_profile_identity=inventory.profile_identity, issue_profile_version=inventory.profile_version,
                    issue_profile_sha256=inventory.content_sha256, domain_receipts_complete=True)
    receipts = read_domain_receipts(session, [window], events=captured.events)
    targets = [e for e in receipts if e.occurred_at >= started
               and e.family in {EventFamily.SOURCE_ARRIVAL, EventFamily.SOURCE_CAPTURE,
                               EventFamily.PROPOSED_DELTA_CREATION}]
    assert len(targets) == 3
    assert all(e.payload.get("binding_event_ids") for e in targets)
    assert all("unavailable_binding_fields" not in e.payload for e in targets)
    assert all(e.binding.code_revision == "producer-first" for e in targets)
    measured = derive_measurement([window], [*captured.events, *receipts])["periods"][0]
    assert measured["cohort_status"] == "bound"
    assert measured["coverage"][0]["captured_within_window"] == 1
    assert len(result.delta_ids) == 1

    # A later deployment reuses the same source, document and delta. Its
    # observations are retries, never a second donor for the original binds.
    second_started = session.scalar(select(func.now()))
    second_deployment = replace(deployment, code_revision="producer-second")
    configuration.write_text(json.dumps(second_deployment.as_dict()))
    monkeypatch.setenv("CORRIDOR_CODE_REVISION", "producer-second")
    with capture_events() as later_events:
        reused = record_delivery(session, delivery_binding, observation, disposition="stored",
                                 service_identity="fixture-connector", run_identity="retry")
        replay = capture_later_revision(session, project=adopted.project, staged=staged,
                                        envelope=envelope_for_delivery(session, reused.delivery_id),
                                        declaration=RevisionDeclaration(declared_by=HumanPrincipal("local:coordinator")))
        assert replay.delta_ids == result.delta_ids
        refused = record_delivery(session, delivery_binding,
                                   replace(observation, external_identity="refused-delivery", bytes_reference=""),
                                   disposition="terminally_refused", refusal_reason="fixture refusal",
                                   service_identity="fixture-connector", run_identity="refusal")
        rollback = session.begin_nested()
        ghost_rows = [list(row) for row in incoming]
        ghost_rows[0][3] = "24 in"
        ghost_body = workbook_bytes(tmp_path / "rolled-back.xlsx", ghost_rows)
        ghost_staged = validate_and_stage(ghost_body, "rolled-back.xlsx", customer_id="fixture-customer", project_id=project_id)
        ghost_observation = replace(observation, external_version="rolled-back", content_digest=ghost_staged.sha256,
                                    bytes_reference=str(ghost_staged.stored_path))
        ghost_delivery = record_delivery(session, delivery_binding, ghost_observation, disposition="stored",
                                         service_identity="fixture-connector", run_identity="rolled-back")
        ghost = capture_later_revision(session, project=adopted.project, staged=ghost_staged,
                                       envelope=envelope_for_delivery(session, ghost_delivery.delivery_id),
                                       declaration=RevisionDeclaration(declared_by=HumanPrincipal("local:coordinator")))
        rollback.rollback()
        session.commit()
    for family in (EventFamily.SOURCE_ARRIVAL, EventFamily.SOURCE_CAPTURE, EventFamily.PROPOSED_DELTA_CREATION):
        assert any(e.payload.get("outcome") == "replayed" for e in later_events.by_family(family))
    assert any(e.payload.get("outcome") == "created" for e in later_events.by_family(EventFamily.PROPOSED_DELTA_CREATION))
    first_window = replace(window, end=second_started)
    second_window = replace(window, period_id="later-producer", start=second_started, declared_at=second_started,
                            end=second_started + timedelta(hours=1), binding=second_deployment)
    all_events = [*captured.events, *later_events.events]
    exported = read_domain_receipts(session, [first_window, second_window], events=all_events)
    by_receipt = {(e.family, e.payload["receipt_id"]): e for e in exported}
    for original in targets:
        current = by_receipt[original.family, original.payload["receipt_id"]]
        assert current.payload["binding_event_ids"] == original.payload["binding_event_ids"]
        assert current.binding.code_revision == "producer-first"
    for delta_id in result.delta_ids:
        native = by_receipt[EventFamily.PROPOSED_DELTA_CREATION, delta_id]
        assert native.payload["source_class"] == "matrix"
        assert native.payload["source_family"].startswith("ucm_workbook:")
    assert (EventFamily.SOURCE_CAPTURE, ghost.document_id) not in by_receipt
    assert all((EventFamily.PROPOSED_DELTA_CREATION, i) not in by_receipt for i in ghost.delta_ids)
    assert by_receipt[EventFamily.SOURCE_ARRIVAL, refused.delivery_id].binding.code_revision == "producer-second"
    report = derive_measurement([first_window, second_window], [*all_events, *exported])
    assert all(p["cohort_status"] == "bound" for p in report["periods"])
    assert report["periods"][1]["captured_source_arrivals"] == 0
    unknown = read_domain_receipts(session, [first_window, second_window], events=later_events.events)
    old_capture = next(e for e in unknown if e.family == EventFamily.SOURCE_CAPTURE
                       and e.payload["document_id"] == result.document_id)
    assert old_capture.payload["outcome"] == "unavailable"
    assert old_capture.payload["attempt_outcomes"] == ["replayed"]
    assert "binding_event_ids" not in old_capture.payload


def test_metadata_only_registration_is_not_a_durable_capture(session, project, store, tmp_path, monkeypatch):
    import json
    from sqlalchemy import func, select
    from corridor.analytics import capture_events
    from corridor.ingest import ingest_document
    from corridor.source_delivery import DeliveryBinding, DeliveryObservation, record_delivery
    from corridor.source_intake import validate_and_stage
    from later_revision_support import BASELINE_ROWS, workbook_bytes
    from test_pilot_measurement import BINDING

    identity = observed_database_identity(session)
    binding = replace(BINDING, database_identity=identity,
                      source_configuration={"identity": "fixture-folder", "version": "1"},
                      connector_configuration={"transport": "pull", "channel": "fixture-folder"})
    config = tmp_path / "binding.json"
    config.write_text(json.dumps(binding.as_dict()))
    monkeypatch.setenv("CORRIDOR_ANALYTICS_BINDING_FILE", str(config))
    session.commit()
    with capture_events() as captured:
        started = session.scalar(select(func.now()))
        staged = validate_and_stage(workbook_bytes(tmp_path / "missing.xlsx", BASELINE_ROWS), "missing.xlsx")
        delivered = record_delivery(session, DeliveryBinding("fixture-customer", project.id, project.slug,
                                    "pull", "fixture-folder", "fixture-folder", "1"),
                                    DeliveryObservation("lost-source", "v1", staged.sha256, str(staged.stored_path),
                                                        metadata={"source_class": "matrix"}),
                                    disposition="stored", service_identity="fixture", run_identity="lost-source")
        staged.stored_path.unlink()  # Simulate loss of this fixture's already-staged bytes.
        document = ingest_document(session, project_id=project.id, path=staged.stored_path,
                                   doc_type="matrix", images_dir=tmp_path / "images", expected_sha256=staged.sha256,
                                   source_delivery_id=delivered.delivery_id)
        session.commit()
    assert document.parse_status == "failed"
    window = period(project_id=project.id, database_identity=identity, binding=binding, start=started,
                    declared_at=started, end=started + timedelta(hours=1), domain_receipts_complete=True)
    receipts = read_domain_receipts(session, [window], events=captured.events)
    capture = next(e for e in receipts if e.family == EventFamily.SOURCE_CAPTURE)
    assert capture.payload["outcome"] == "unavailable"
    report = derive_measurement([window], [*captured.events, *receipts])["periods"][0]
    assert report["captured_source_arrivals"] == 0
    assert report["coverage"][0]["captured_within_window"] == 0


def test_competing_worker_replay_cannot_claim_the_winning_delta_binding(session, project):
    from concurrent.futures import ThreadPoolExecutor
    from queue import Queue
    import time
    from sqlalchemy import func, select
    from corridor.analytics import capture_events
    from corridor.proposed_deltas import ExistingSubjectTarget, ProposedDeltaValues, create_proposed_delta_group
    from test_pilot_measurement import BINDING

    project_id = project.id
    identity = observed_database_identity(session)
    session.commit()
    engine = session.get_bind()
    worker = create_engine(engine.url.set(username="corridor_worker", password="corridor_worker"))
    values = (ProposedDeltaValues(change_type="modify", target=ExistingSubjectTarget("concurrent-subject", "size"),
                                  accepted_value="12 in", proposed_value="18 in"),)
    first_binding = replace(BINDING, code_revision="winning-code", database_identity=identity)
    retry_binding = replace(first_binding, code_revision="retrying-code")
    pids = Queue()

    def competing_call():
        with Session(worker) as other:
            pids.put(other.scalar(select(func.pg_backend_pid())))
            result = create_proposed_delta_group(other, project_id=project_id, source_family="matrix",
                                                 source_revision="concurrent-version", deltas=values,
                                                 analytics_binding=retry_binding)
            ids = tuple(r.id for r in result)
            other.commit()
            return ids

    try:
        with capture_events() as captured, Session(worker) as first:
            first_pid = first.scalar(select(func.pg_backend_pid()))
            written = create_proposed_delta_group(first, project_id=project_id, source_family="matrix",
                                                  source_revision="concurrent-version", deltas=values,
                                                  analytics_binding=first_binding)
            original_ids = tuple(r.id for r in written)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(competing_call)
                blocked = False
                try:
                    other_pid = pids.get(timeout=5)
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        if first_pid in session.scalar(select(func.pg_blocking_pids(other_pid))):
                            blocked = True
                            break
                        time.sleep(0.02)
                finally:
                    first.commit()  # Release the real unique-key wait even if the test fails.
                assert future.result(timeout=5) == original_ids
            assert blocked, "the second call must reach the uncommitted competing insert"
        events = captured.by_family(EventFamily.PROPOSED_DELTA_CREATION)
        assert [(e.payload["outcome"], e.binding.code_revision) for e in events] == [
            ("created", "winning-code"), ("replayed", "retrying-code")]
        receipt = next(e for e in read_domain_receipts(session, [period(project_id=project_id, database_identity=identity)],
                                                       events=events)
                       if e.family == EventFamily.PROPOSED_DELTA_CREATION)
        assert receipt.payload["binding_event_ids"] == [events[0].event_id]
        assert receipt.binding.code_revision == "winning-code"
    finally:
        worker.dispose()


# --- "This logged event and this database receipt describe the same act" ----
#
# Each family declares how its logged event corroborates its receipt. These
# cases need no database: the events come from the family constructors and the
# rows are model instances built in memory.

from datetime import datetime, timezone

from corridor import models as m
from corridor.analytics import (
    AnalyticsEvent, child_decision_event, coverage_confirmation_event, default_binding,
    delta_supersession_event, follow_up_plan_creation_event, packet_save_event, packet_surfacing_event,
    preparation_attempt_finished_event, preparation_attempt_started_event, preparation_request_event,
    proposed_delta_creation_event, release_authorization_event, release_candidate_preparation_event,
    source_arrival_event, source_capture_event,
)
from corridor.pilot_measurement_receipts import same_act

AT = datetime(2026, 9, 10, 12, 30, tzinfo=timezone.utc)
LATER = datetime(2026, 9, 10, 12, 31, tzinfo=timezone.utc)
FIXTURE_BINDING = default_binding(code_revision="git:fixture")


def decision_event(**changes):
    fields = dict(occurred_at=AT, project_id=7, delta_id=31, action="apply", effect_kind="field_change",
                  outcome="saved", refusal_reason=None, revision_id=12, packet_owned=True,
                  support_assessment_count=0)
    fields.update(changes)
    return child_decision_event(FIXTURE_BINDING, **fields)


def test_child_decision_corroborates_its_disposition_in_either_action_vocabulary():
    row = m.DeltaDisposition(id=1, delta_id=31, disposition="apply")
    receipt = {"delta_id": 31, "action": "apply", "outcome": "resolved"}
    assert same_act(decision_event(), receipt, row, AT)
    assert same_act(decision_event(outcome="resolved"), receipt, row, AT)
    assert not same_act(decision_event(outcome="refused"), receipt, row, AT)
    assert not same_act(decision_event(delta_id=32), receipt, row, AT)
    assert not same_act(decision_event(action="keep_current"), receipt, row, AT)
    assert not same_act(decision_event(), receipt, row, LATER)
    # A row logged before the record's vocabulary was declared spelled the act
    # "accept" and its outcome "status"; the named translation still reads it.
    historical = AnalyticsEvent(family="child_decision", binding=FIXTURE_BINDING, occurred_at=AT,
                                payload={"project_id": 7, "delta_id": 31, "action": "accept", "status": "saved"})
    assert same_act(historical, receipt, row, AT)
    assert not same_act(historical, {**receipt, "action": "keep_current"}, row, AT)


def test_child_decision_defer_requires_the_deferred_outcome():
    row = m.DeltaDeferral(id=2, delta_id=31)
    receipt = {"delta_id": 31, "action": "defer", "outcome": "deferred"}
    assert same_act(decision_event(action="defer", outcome="deferred"), receipt, row, AT)
    assert not same_act(decision_event(action="defer", outcome="saved"), receipt, row, AT)


def test_packet_save_corroborates_only_a_saved_receipt():
    row = m.DeltaReviewPacketReceipt(id=4)
    receipt = {"outcome": "saved"}
    saved = packet_save_event(FIXTURE_BINDING, occurred_at=AT, project_id=7, receipt_id=4, revision_id=12,
                              grouping_rule_version="v2", grouping_key_kind="source_revision", grouping_key="k",
                              observed_accepted_revision_id=11, child_count=1, outcome_counts={},
                              outcome="saved", refusal_reason=None)
    assert same_act(saved, receipt, row, AT)
    refused = packet_save_event(FIXTURE_BINDING, occurred_at=AT, project_id=7, receipt_id=None, revision_id=None,
                                grouping_rule_version="v2", grouping_key_kind="source_revision", grouping_key="k",
                                observed_accepted_revision_id=10, child_count=1, outcome_counts={},
                                outcome="refused", refusal_reason="stale_revision")
    assert not same_act(refused, receipt, row, AT)
    assert not same_act(saved, receipt, m.DeltaReviewPacketReceipt(id=5), AT)


def test_release_families_read_the_outcome_from_their_status_label():
    candidate = m.ReleaseCandidate(id=5, candidate_identity="candidate:1", readiness="ready", prepared_at=AT)
    prepared = release_candidate_preparation_event(
        FIXTURE_BINDING, occurred_at=AT, surface="worker", outcome="ready", principal_subject="p", project_id=7,
        source_cutoff=AT.isoformat(), accepted_revision_id=11, previous_package_id=None, issue_profile_id=3,
        issue_profile_version=1, coverage_identity="c", configured_artifact_types=["updated_ucm"],
        candidate_identity="candidate:1", content_sha256="b" * 64, readiness="ready", blocker_count=0,
        exception_count=0, refusal_code=None,
    )
    # The candidate's own instant is prepared_at, whatever instant the reader passes.
    assert same_act(prepared, {}, candidate, LATER)
    assert not same_act(replace(prepared, occurred_at=LATER), {}, candidate, LATER)
    assert not same_act(replace(prepared, metric_labels={"surface": "worker", "status": "blocked"}), {}, candidate, AT)
    refused = replace(prepared, payload={**prepared.payload, "refusal_code": "no_coverage"})
    assert not same_act(refused, {}, candidate, AT)

    package = m.ReleasePackage(id=6, package_identity="package:1", authorized_by_principal="p")
    authorized = release_authorization_event(
        FIXTURE_BINDING, occurred_at=AT, surface="issue_screen", status="authorized", principal_subject="p",
        project_id=7, candidate_id=5, candidate_identity="candidate:1", accepted_revision_id=11,
        issue_profile_version=1, source_cutoff=AT.isoformat(), package_identity="package:1", issue_number=1,
        refusal_code=None,
    )
    assert same_act(authorized, {}, package, AT)
    assert not same_act(replace(authorized, metric_labels={"surface": "issue_screen", "status": "replayed"}),
                        {}, package, AT)
    assert not same_act(authorized, {}, m.ReleasePackage(id=7, package_identity="package:1",
                                                          authorized_by_principal="someone-else"), AT)


def test_preparation_families_corroborate_request_confirmation_and_both_attempt_shapes():
    profile = {"issue_profile_identity": "i", "issue_profile_version": 1, "issue_profile_sha256": "a" * 64}
    request_row = m.ReleasePreparationRequest(id=6, requested_by_principal="p")
    requested = preparation_request_event(FIXTURE_BINDING, occurred_at=AT, project_id=7, receipt_id=6,
                                          principal_subject="p", request_id=6, coverage_declaration_id=8,
                                          outcome="requested", **profile)
    assert same_act(requested, {}, request_row, AT)
    assert not same_act(replace(requested, payload={**requested.payload, "outcome": "replayed"}), {}, request_row, AT)

    declaration = m.IssueCoverageDeclaration(id=8, confirmed_by_principal="p")
    confirmed = coverage_confirmation_event(FIXTURE_BINDING, occurred_at=AT, project_id=7, receipt_id=8,
                                            principal_subject="p", coverage_declaration_id=8, reading_sha256="c" * 64,
                                            annotation_count=2, unchanged_declaration_reused=False, **profile)
    assert same_act(confirmed, {}, declaration, AT)
    reused = replace(confirmed, payload={**confirmed.payload, "unchanged_declaration_reused": True})
    assert not same_act(reused, {}, declaration, AT)

    reading = m.ReleasePreparationReading(id=1, request_id=6, bound_at=AT)
    started = preparation_attempt_started_event(FIXTURE_BINDING, occurred_at=AT, project_id=7, request_id=6,
                                                coverage_declaration_id=8, principal_subject="p",
                                                started_at=AT.isoformat(), **profile)
    assert same_act(started, {}, reading, AT)
    attempt = m.ReleasePreparationAttempt(id=2, request_id=6, outcome="failed", started_at=AT)
    finished = preparation_attempt_finished_event(FIXTURE_BINDING, occurred_at=LATER, project_id=7, request_id=6,
                                                  attempt_id=2, outcome="failed", candidate_id=None,
                                                  refusal_code="renderer_failed", started_at=AT.isoformat(),
                                                  coverage_declaration_id=8, principal_subject="p", **profile)
    # A refusal code disqualifies every family except the attempt that recorded it.
    assert same_act(finished, {}, attempt, LATER)
    assert not same_act(started, {}, attempt, AT)
    assert not same_act(replace(finished, payload={**finished.payload, "outcome": "prepared"}), {}, attempt, LATER)


def test_delta_and_source_families_corroborate_their_rows():
    created = proposed_delta_creation_event(FIXTURE_BINDING, occurred_at=AT, project_id=7, source_family="matrix",
                                            source_revision="r", document_id=5, delta_id=31, outcome="created",
                                            complete_enumerative_source=True, row_accounting_sealed=False)
    assert same_act(created, {}, m.ProposedDelta(id=31), AT)
    assert not same_act(created, {}, m.ProposedDelta(id=32), AT)
    assert not same_act(replace(created, payload={**created.payload, "outcome": "replayed"}), {}, m.ProposedDelta(id=31), AT)

    plan = follow_up_plan_creation_event(FIXTURE_BINDING, occurred_at=AT, project_id=7, delta_id=31,
                                         follow_up_plan_id=9, revision_id=12, grouping_rule_version="v2",
                                         has_return_date=False, responsible_kind="organization", evidence_count=0)
    assert same_act(plan, {}, m.DeltaFollowUpPlan(id=9, delta_id=31), AT)
    assert not same_act(plan, {}, m.DeltaFollowUpPlan(id=9, delta_id=30), AT)

    supersession = delta_supersession_event(FIXTURE_BINDING, occurred_at=AT, project_id=7, prior_delta_id=31,
                                            superseding_delta_id=32, source_reading_id=5, source_revision="r",
                                            comparison_rule_version="email-v3")
    row = m.DeltaSupersession(id=1, prior_delta_id=31, superseding_delta_id=32, source_reading_id=5,
                              minutes_capture_id=None)
    assert same_act(supersession, {}, row, AT)
    assert not same_act(supersession, {}, replace_attr(row, superseding_delta_id=33), AT)

    arrival = source_arrival_event(FIXTURE_BINDING, filename="in.xlsx", content_sha256="d" * 64, byte_count=1,
                                   source_delivery_id=3, occurred_at=AT, outcome="recorded", disposition="stored")
    assert same_act(arrival, {}, m.SourceDelivery(id=3, disposition="stored"), AT)
    assert not same_act(arrival, {}, m.SourceDelivery(id=3, disposition="terminally_refused"), AT)
    staged = replace(arrival, payload={**arrival.payload, "outcome": "staged"})
    assert not same_act(staged, {}, m.SourceDelivery(id=3, disposition="stored"), AT)

    capture = source_capture_event(FIXTURE_BINDING, storage_key="k", content_sha256="d" * 64, byte_count=1,
                                   document_id=5, occurred_at=AT, outcome="captured")
    assert same_act(capture, {}, m.Document(id=5), AT)
    assert same_act(replace(capture, payload={**capture.payload, "outcome": "unavailable"}), {}, m.Document(id=5), AT)
    assert not same_act(replace(capture, payload={**capture.payload, "outcome": "replayed"}), {}, m.Document(id=5), AT)

    shown = packet_surfacing_event(FIXTURE_BINDING, occurred_at=AT, principal_subject="p", **{
        key: None for key in (
            "project_id", "item_key", "grouping_key_kind", "grouping_key", "grouping_rule_version", "band",
            "attention_reasons", "held_out_reason", "child_count", "ready_count", "held_out_count",
            "unchanged_count", "customer_artifacts", "artifact_rule_version", "observed_accepted_revision_id",
            "cutoff", "issue_profile_id", "issue_profile_identity", "issue_profile_version",
            "issue_profile_sha256", "issue_profile_problems", "consequence_rule_version", "consequence_level",
            "child_consequences")}, )
    assert not same_act(shown, {}, m.ProposedDelta(id=31), AT)


def replace_attr(row, **changes):
    for name, value in changes.items():
        setattr(row, name, value)
    return row
