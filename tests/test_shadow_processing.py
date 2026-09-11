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
from corridor.field_mapping_manifest import DEMO_EXTERNAL_REFERENCES, MappingDeclaration
from corridor.models import Project, ProposedDelta
from corridor.native_provider_boundary import CustomerAuthorization
from corridor.shadow_processing import ShadowRefused, provision_shadow_project, run_shadow_ucm, verify_runtime
from harness_support import as_role
from later_revision_support import BASELINE_ROWS, CUSTOMER, PRINCIPAL, adopt, deliver, workbook_bytes

NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)
DELETE = date(2026, 12, 1)


def authorization(body, slug):
    return CustomerAuthorization(
        record_id="synthetic-shadow-authorization", customer=CUSTOMER,
        projects=frozenset({slug}), source_classes=frozenset({"ucm"}), purposes=frozenset({"shadow-processing"}),
        stages=frozenset({"compatibility", "shadow"}), source_sha256s=frozenset({sha256(body).hexdigest()}),
        max_calls=0, max_pages=0, max_total_tokens=0, posture_identity="deterministic-no-model",
        posture_digest="0" * 64, retention_disclosed=True, signed_by="local:authorizer", signed_on="2026-09-09")


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
        external_references=MappingDeclaration(external_references=DEMO_EXTERNAL_REFERENCES).external_reference_headings)
    engines = {}
    for role in ("corridor_worker", "corridor_web"):
        url = make_url(settings.database_url).set(database=runtime_database.name,
            username=role, password=os.environ.get(f"{role.upper()}_DB_PASSWORD", role))
        engines[role] = create_engine(url)
    yield runtime_database, engines, project_id, staged, envelope, approved, compatibility.receipt
    for engine in engines.values():
        engine.dispose()


@pytest.mark.parametrize("complete", [False, True])
def test_actual_worker_capture_freezes_native_identity_and_replays(shadow, complete):
    database, engines, project_id, staged, envelope, approved, compatibility = shadow
    def run(session):
        return run_shadow_ucm(session, project=session.get(Project, project_id),
            staged=staged, envelope=envelope, compatibility_receipt=compatibility,
            authorization=approved, customer=CUSTOMER, environment="synthetic-shadow",
            source_configuration="manual-ucm-v1", principal=PRINCIPAL,
            deletion_date=DELETE, now=NOW,
            is_complete_enumerative_source=complete, row_accounting_sealed=complete)
    with Session(engines["corridor_worker"]) as worker, worker.begin():
        output = run(worker)
        native = worker.scalars(select(ProposedDelta).where(ProposedDelta.project_id == project_id)).all()
        assert [row.id for row in native] == [row["id"] for row in output["deltas"]]
        assert output["deltas"][0]["proposed_value"] == "18 in"
        assert output["deltas"][0]["lifecycle"]["status"] == "open"
        assert output["source_delivery_watermark"] >= output["delivery_id"]
        assert output["is_complete_enumerative_source"] is complete
        assert output["row_accounting_sealed"] is complete
        assert output["accounting"]["sheet_name"] == "Utility Conflicts"
        assert len(output["accounting"]["rows"]) == 3
        assert output["accounting"]["removals"] == []
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


def test_shadow_deployment_source_append_rechecks_actual_worker(shadow, monkeypatch):
    from corridor.source_append import append_source_segments
    database, engines, project_id, *_ = shadow
    monkeypatch.setattr(settings, "deployment_data_class", "shadow")
    monkeypatch.setattr(settings, "customer_id", CUSTOMER)
    monkeypatch.setattr(settings, "customer_environment_id", "synthetic-shadow")
    with Session(engines["corridor_worker"]) as worker:
        assert append_source_segments(worker, project_id=project_id, document_id=None,
            recorded_verbal_origin_id=None, segments=[]) == ()
    with database.session_factory() as owner:
        with pytest.raises(ShadowRefused, match="actual corridor_worker"):
            append_source_segments(owner, project_id=project_id, document_id=None,
                recorded_verbal_origin_id=None, segments=[])


def test_database_seal_overrides_forged_chronology_and_freezes_native_scope(shadow):
    import json
    from dataclasses import asdict
    from corridor.shadow_receipts import read_shadow_run
    database, engines, project_id, staged, envelope, approved, compatibility = shadow
    with Session(engines["corridor_worker"]) as worker, worker.begin():
        output = run_shadow_ucm(worker, project=worker.get(Project, project_id), staged=staged,
            envelope=envelope, compatibility_receipt=compatibility, authorization=approved,
            customer=CUSTOMER, environment="synthetic-shadow", source_configuration="seal-first",
            principal=PRINCIPAL, deletion_date=DELETE, now=NOW)
        exported = read_shadow_run(worker, output["identity"])
        assert exported.payload == output
        assert sha256(exported.payload_text.encode()).hexdigest() == exported.output_sha256
        assert json.loads(exported.payload_text) == output
    # This comparison input already exists before the attempted forged freeze.
    with database.session_factory.begin() as owner:
        project = owner.get(Project, project_id)
        from corridor.push_intake import PushCredential, PushPayload, accept_delivery, bind_credential
        binding = bind_credential(owner, PushCredential("webhook", f"secret-{project.slug}"))
        reference = accept_delivery(owner, binding, PushPayload(
            staged.stored_path.read_bytes(), staged.filename,
            transport_delivery_id="already-seen-comparison-revision")).envelope
        reference_id = owner.scalar(text("select id from source_deliveries where idempotency_key=:key and disposition='stored'"),
            {"key": reference.idempotency_key})
        reference_at = owner.scalar(text("select received_at from source_deliveries where id=:id"), {"id": reference_id})
    forged = output | {"identity": "f" * 64, "source_configuration": "seal-second",
        "frozen_at": "1900-01-01T00:00:00+00:00", "source_delivery_watermark": 0,
        "canonicalization": "caller-json", "source_provenance": [], "groups": [], "fact_ids": []}
    with Session(engines["corridor_worker"]) as worker, worker.begin():
        sealed = worker.execute(text("insert into shadow_runs(identity,project_id,payload,output_sha256,recorded_at) values (:identity,:project,cast(:payload as jsonb),'forged','1900-01-01') returning payload,output_sha256,recorded_at"),
            {"identity": forged["identity"], "project": project_id, "payload": json.dumps(forged)}).one()
        assert sealed.payload["source_delivery_watermark"] >= reference_id
        assert datetime.fromisoformat(sealed.payload["frozen_at"]) >= reference_at
        assert sealed.recorded_at == datetime.fromisoformat(sealed.payload["frozen_at"])
        assert sealed.output_sha256 != "forged"
        assert sealed.payload["source_provenance"]
        assert sealed.payload["fact_ids"] == output["fact_ids"]
        assert sealed.payload["groups"]
        assert sealed.payload["canonicalization"] == "postgresql-jsonb-text-v1"
        exported = read_shadow_run(worker, forged["identity"])
        assert json.loads(asdict(exported)["payload_text"]) == sealed.payload
        for problem in ("project_id", "document_id", "delivery_id", "delta_id", "delta_value"):
            corrupt = json.loads(json.dumps(output))
            corrupt["identity"] = "e" * 64
            corrupt["source_configuration"] = "seal-invalid"
            if problem == "delta_id":
                corrupt["deltas"][0]["id"] = -1
            elif problem == "delta_value":
                corrupt["deltas"][0]["proposed_value"] = "invented"
            else:
                corrupt[problem] = -1
            with pytest.raises(DBAPIError, match="shadow"), worker.begin_nested():
                worker.execute(text("insert into shadow_runs(identity,project_id,payload,output_sha256) values (:identity,:project,cast(:payload as jsonb),'forged')"),
                    {"identity": corrupt["identity"], "project": project_id, "payload": json.dumps(corrupt)})

    # A caller cannot hold an older MVCC snapshot, watch another committed
    # delivery arrive, and then have the DB certify its stale watermark.
    with engines["corridor_worker"].connect().execution_options(isolation_level="REPEATABLE READ") as connection:
        with Session(bind=connection) as stale, stale.begin():
            before = stale.scalar(text("select max(id) from source_deliveries"))
            with database.session_factory.begin() as owner:
                project = owner.get(Project, project_id)
                binding = bind_credential(owner, PushCredential("webhook", f"secret-{project.slug}"))
                later = accept_delivery(owner, binding, PushPayload(staged.stored_path.read_bytes(),
                    staged.filename, transport_delivery_id="committed-after-stale-snapshot"))
                assert later.delivery_id > before
            stale_payload = output | {"identity": "d" * 64, "source_configuration": "stale-snapshot"}
            with pytest.raises(DBAPIError, match="read committed"), stale.begin_nested():
                stale.execute(text("insert into shadow_runs(identity,project_id,payload,output_sha256) values (:identity,:project,cast(:payload as jsonb),'forged')"),
                    {"identity": stale_payload["identity"], "project": project_id, "payload": json.dumps(stale_payload)})


@pytest.mark.parametrize("grant_kind", ["decision_command", "release_update"])
def test_shadow_runtime_rechecks_authority_reachable_through_set_role(shadow, grant_kind):
    """NOINHERIT does not prevent SET ROLE, nor the target's inherited ACLs."""
    from uuid import uuid4
    database, engines, project_id, *_ = shadow
    suffix = uuid4().hex
    intermediary = f"shadow_bridge_{suffix}"
    authority = f"shadow_grant_{suffix}"
    signature = None
    created = False
    try:
        with database.session_factory.begin() as owner:
            owner.execute(text(f'create role "{intermediary}" nologin noinherit'))
            owner.execute(text(f'create role "{authority}" nologin noinherit'))
            # The login may assume the ordinary intermediary, whose effective
            # privileges include a second role it cannot itself SET ROLE into.
            owner.execute(text(f'grant "{authority}" to "{intermediary}" with inherit true'))
            owner.execute(text(f'grant "{authority}" to "{intermediary}" with set false'))
            owner.execute(text(f'grant "{intermediary}" to corridor_worker with inherit false'))
            owner.execute(text(f'grant "{intermediary}" to corridor_worker with set true'))
            command = owner.execute(text("""
                select p.oid, p.oid::regprocedure::text as signature from pg_proc p
                join pg_namespace n on n.oid=p.pronamespace
                where n.nspname='public' and p.proname='authorize_release_package'
            """)).one()
            command_oid, signature = command
        created = True
        with Session(engines["corridor_worker"]) as worker:
            verify_runtime(worker, project_id=project_id, customer=CUSTOMER, environment="synthetic-shadow")
        # Simulate privilege drift after provisioning, without changing any
        # existing role's global attributes or touching another database's ACLs.
        with database.session_factory.begin() as owner:
            statement = (f'grant execute on function {signature} to "{authority}"'
                         if grant_kind == "decision_command" else
                         f'grant update on public.release_candidates to "{authority}"')
            owner.execute(text(statement))
        with Session(engines["corridor_worker"]) as worker:
            privilege = ("select has_function_privilege(current_user,:oid,'EXECUTE')"
                         if grant_kind == "decision_command" else
                         "select has_table_privilege(current_user,'public.release_candidates','UPDATE')")
            assert worker.scalar(text(privilege), {"oid": command_oid}) is False
            assert worker.scalar(text("select pg_has_role(current_user,:role,'SET')"), {"role": intermediary}) is True
            assert worker.scalar(text("select pg_has_role(current_user,:role,'SET')"), {"role": authority}) is False
            with as_role(worker, intermediary):
                assert worker.scalar(text(privilege), {"oid": command_oid}) is True
            with pytest.raises(ShadowRefused, match="accepted-record or release authority"):
                verify_runtime(worker, project_id=project_id, customer=CUSTOMER, environment="synthetic-shadow")
        with database.session_factory.begin() as owner:
            owner.execute(text(f'grant "{intermediary}" to corridor_worker with set false'))
        with Session(engines["corridor_worker"]) as worker:
            # Mere membership with neither SET nor INHERIT does not confer
            # these capabilities and must not become a false authority finding.
            assert worker.scalar(text("select pg_has_role(current_user,:role,'MEMBER')"), {"role": authority}) is True
            verify_runtime(worker, project_id=project_id, customer=CUSTOMER, environment="synthetic-shadow")
        with database.session_factory.begin() as owner:
            owner.execute(text(f'grant "{intermediary}" to corridor_worker with inherit true'))
        with Session(engines["corridor_worker"]) as worker:
            with pytest.raises(ShadowRefused, match="accepted-record or release authority"):
                verify_runtime(worker, project_id=project_id, customer=CUSTOMER, environment="synthetic-shadow")
    finally:
        if created:
            with database.session_factory.begin() as owner:
                owner.execute(text(f'revoke "{intermediary}" from corridor_worker'))
                owner.execute(text(f'revoke "{authority}" from "{intermediary}"'))
                if signature is not None:
                    owner.execute(text(f'revoke execute on function {signature} from "{authority}"'))
                owner.execute(text(f'revoke update on public.release_candidates from "{authority}"'))
                owner.execute(text(f'drop role if exists "{intermediary}"'))
                owner.execute(text(f'drop role if exists "{authority}"'))


def test_web_temp_shadow_registry_cannot_reveal_the_shadow_project(shadow):
    """The login owns TEMP, but cannot substitute the definer's registry."""
    _database, engines, project_id, *_ = shadow
    with Session(engines["corridor_web"]) as web:
        web.execute(text("create temporary table shadow_projects(project_id bigint) on commit drop"))
        web.execute(text("grant select on pg_temp.shadow_projects to public"))
        assert web.scalar(text("select public.shadow_project_visible(:project)"), {"project": project_id}) is False
        assert web.scalar(text("select count(*) from public.projects where id=:project"), {"project": project_id}) == 0


@pytest.mark.parametrize("statement", [
    "insert into public.project_roster_entries(project_id,principal_subject,display_name,active) values (:project,'local:temp-registry-reader','Temporary registry reader',true)",
    "insert into public.release_preparation_requests(project_id) values (:project)",
    "insert into public.release_candidates(project_id) values (:project)",
])
def test_web_temp_shadow_registry_cannot_bypass_customer_surface_guard(shadow, statement):
    """Every protected customer surface refuses before other row validation."""
    _database, engines, project_id, *_ = shadow
    with Session(engines["corridor_web"]) as web:
        web.execute(text("create temporary table shadow_projects(project_id bigint) on commit drop"))
        web.execute(text("grant select on pg_temp.shadow_projects to public"))
        with pytest.raises(DBAPIError, match="shadow project cannot enter customer coordination or release"), web.begin_nested():
            web.execute(text(statement), {"project": project_id})
