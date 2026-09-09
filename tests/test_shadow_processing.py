"""Synthetic shadow capture through real worker/web logins and native deltas."""
from datetime import date, datetime, timezone
from hashlib import sha256
import os

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from corridor.compatibility_intake import run_compatibility_intake
from corridor.config import settings
from corridor.field_mapping_manifest import DEMO_EXTERNAL_REFERENCES
from corridor.models import Project, ProposedDelta
from corridor.native_provider_boundary import CustomerAuthorization
from corridor.shadow_processing import ShadowRefused, provision_shadow_project, run_shadow_ucm, verify_runtime
from later_revision_support import BASELINE_ROWS, CUSTOMER, PRINCIPAL, adopt, deliver, workbook_bytes

NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)
DELETE = date(2026, 12, 1)


def authorization(body, slug):
    return CustomerAuthorization("synthetic-shadow-authorization", CUSTOMER,
        frozenset({slug}), frozenset({"ucm"}), frozenset({"shadow-processing"}),
        frozenset({"compatibility", "shadow"}), frozenset({sha256(body).hexdigest()}),
        0, 0, 0, "deterministic-no-model", "0" * 64, True, "local:authorizer", "2026-09-09")


@pytest.fixture
def shadow(runtime_database, tmp_path):
    baseline = workbook_bytes(tmp_path / "baseline.xlsx", BASELINE_ROWS)
    changed = [list(row) for row in BASELINE_ROWS]
    changed[0][3] = "18 in"
    body = workbook_bytes(tmp_path / "later.xlsx", changed)
    with runtime_database.session_factory.begin() as owner:
        project = Project(slug="shadow-ucm", name="Synthetic shadow", is_synthetic=True)
        owner.add(project)
        owner.flush()
        adopt(owner, project, baseline, tmp_path)
        project_id = project.id
        staged, envelope = deliver(owner, project, body)
        provision_shadow_project(owner, project_id=project.id, customer=CUSTOMER,
            environment="synthetic-shadow", operator=PRINCIPAL.subject)
    approved = authorization(body, "shadow-ucm")
    compatibility = run_compatibility_intake(body, "later.xlsx", authorization=approved,
        customer=CUSTOMER, project="shadow-ucm", operator=PRINCIPAL.subject,
        environment="synthetic-compatibility", deletion_date=DELETE,
        external_references=DEMO_EXTERNAL_REFERENCES)
    engines = {}
    for role in ("corridor_worker", "corridor_web"):
        url = make_url(settings.database_url).set(database=runtime_database.name,
            username=role, password=os.environ.get(f"{role.upper()}_DB_PASSWORD", role))
        engines[role] = create_engine(url)
    yield runtime_database, engines, project_id, staged, envelope, approved, compatibility.receipt
    for engine in engines.values():
        engine.dispose()


def test_actual_worker_capture_freezes_native_identity_and_replays(shadow):
    database, engines, project_id, staged, envelope, approved, compatibility = shadow
    def run(session):
        return run_shadow_ucm(session, project=session.get(Project, project_id),
            staged=staged, envelope=envelope, compatibility_receipt=compatibility,
            authorization=approved, customer=CUSTOMER, environment="synthetic-shadow",
            source_configuration="manual-ucm-v1", principal=PRINCIPAL,
            deletion_date=DELETE, now=NOW)
    with Session(engines["corridor_worker"]) as worker, worker.begin():
        output = run(worker)
        native = worker.scalars(select(ProposedDelta).where(ProposedDelta.project_id == project_id)).all()
        assert [row.id for row in native] == [row["id"] for row in output["deltas"]]
        assert output["deltas"][0]["proposed_value"] == "18 in"
        assert output["deltas"][0]["lifecycle"]["status"] == "open"
        assert output["source_delivery_watermark"] >= output["delivery_id"]
    with Session(engines["corridor_worker"]) as worker, worker.begin():
        assert run(worker) == output
    with database.session_factory.begin() as owner:
        assert owner.scalar(text("select count(*) from shadow_runs")) == 1
        assert owner.scalar(text("select count(*) from project_record_revisions where project_id=:id"), {"id": project_id}) == 1


def test_shadow_cannot_reach_membership_release_or_record_commands(shadow):
    database, engines, project_id, *_ = shadow
    with Session(engines["corridor_worker"]) as worker:
        verify_runtime(worker, project_id=project_id, customer=CUSTOMER, environment="synthetic-shadow")
        for statement in (
            "select public.adopt_project_baseline(1, 'a', 'b', 'c', 'd', 'e', 1)",
            "select public.authorize_release_package(1, 1, 'local:coordinator', now(), 'key')",
            "insert into release_candidates(project_id) values (:id)",
            "insert into release_preparation_requests(project_id) values (:id)",
            "insert into release_packages(project_id) values (:id)",
        ):
            with pytest.raises(DBAPIError, match="permission denied"), worker.begin_nested():
                worker.execute(text(statement), {"id": project_id})
    with database.session_factory.begin() as owner:
        with pytest.raises(DBAPIError, match="shadow project"), owner.begin_nested():
            owner.execute(text("insert into project_roster_entries(project_id,principal_subject,active) values (:id,'local:coordinator',true)"), {"id": project_id})
        with pytest.raises(ShadowRefused, match="actual corridor_worker"):
            verify_runtime(owner, project_id=project_id, customer=CUSTOMER, environment="synthetic-shadow")
    with Session(engines["corridor_web"]) as web:
        assert web.scalar(text("select count(*) from projects where id=:id"), {"id": project_id}) == 0
        with pytest.raises(DBAPIError), web.begin_nested():
            web.execute(text("select open_project_partition('local:coordinator',:id)"), {"id": project_id})


def test_changed_receipt_cannot_produce_shadow_facts(shadow):
    database, engines, project_id, staged, envelope, approved, receipt = shadow
    receipt.stored_path.write_bytes(b'{}')
    with Session(engines["corridor_worker"]) as worker, worker.begin():
        with pytest.raises(ShadowRefused, match="digest changed"):
            run_shadow_ucm(worker, project=worker.get(Project, project_id), staged=staged,
                envelope=envelope, compatibility_receipt=receipt, authorization=approved,
                customer=CUSTOMER, environment="synthetic-shadow", source_configuration="manual-v1",
                principal=PRINCIPAL, deletion_date=DELETE, now=NOW)
        assert worker.scalar(text("select count(*) from shadow_runs")) == 0
        assert worker.scalar(text("select count(*) from proposed_deltas")) == 0
