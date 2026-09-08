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
