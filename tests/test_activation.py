"""Activation freezes exact fixture evidence and refuses changed deployment inputs."""
from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json

import pytest

from corridor.web_boundary import PILOT_ROUTES

from corridor.activation import (ActivationConfiguration, ActivationRefused, BASE_GATES,
    EvidenceArtifact, activate, processing_authorized, route_manifest_digest)
from harness_support import as_role

NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)


@pytest.fixture
def configuration():
    return ActivationConfiguration("fixture-env", "fixture-customer", 1, "ucm-v1", "manual-upload",
        511, 606, "us-east-2", "code-1", "db-1", "image-1", "governance-1", "security-1",
        "a" * 64, "true", "corridor_web", route_manifest_digest(), "fixture-deployment")


def evidence(tmp_path, configuration, **overrides):
    result = {}
    for gate in BASE_GATES:
        payload = {"gate": gate, "outcome": "passed", "configuration": configuration.identity,
            "observed_at": NOW.isoformat()}
        if gate == "web_boundary":
            payload |= {"contract": "live-pilot-web-boundary-v1", "actual_login": "corridor_web",
                "actual_database_role": "corridor_web", "route_manifest_digest": route_manifest_digest(),
                "database_binding": {"customer": configuration.customer, "environment": configuration.environment,
                    "deployment_id": configuration.deployment_id},
                "boundary_state": "enforced",
                "observations": [{"method": method, "template": route, "status": 200}
                    for method, route in PILOT_ROUTES] + [{"method": "GET", "template": "/disabled", "status": 404}]}
        if gate == "pdf_image_audit":
            payload |= {"image_digest": configuration.image_digest,
                "built_image_audit": {
                    "schema_version": "corridor.built-image-engine-audit.v1",
                    "image": {"image_id": configuration.image_digest},
                    "revision": configuration.code_revision, "working_tree_dirty": False,
                    "clear_of_retired_engines": True, "findings": [],
                    "audited": {
                        "environments": {"application": {"present": []}, "render_worker": {"present": []}},
                        "executables": {"tesseract": None}, "system_packages": {"tesseract-ocr": None},
                        "native_matrix_runtime": {"code_revision": configuration.code_revision},
                        "notices": {"manifest_present": True, "missing_files": [], "declared_files": 1,
                            "files_on_disk": 1, "packages": ["pypdfium2"],
                            "installed_in_image": {"pypdfium2": [{"dist_info": "fixture-pdfium.dist-info",
                                "licence_files": ["fixture-LICENSE"]}]}}
                    }
                }}
        if gate == "disposition":
            payload |= {"inventory_digest": configuration.disposition_inventory_digest,
                "external_receipt_reference": "fixture-control-plane/receipt",
                "rehearsal_receipt_sha256": "b" * 64}
        payload |= overrides.get(gate, {})
        body = json.dumps(payload).encode()
        path = tmp_path / f"{gate}.json"
        path.write_bytes(body)
        result[gate] = EvidenceArtifact(path, sha256(body).hexdigest())
    return result


def test_successful_revision_replays_and_configuration_change_disables_it(tmp_path, configuration):
    artifacts = evidence(tmp_path, configuration)
    kwargs = dict(evidence=artifacts, operator="local:operator", revision="activation-1",
        custody=tmp_path / "custody", now=NOW)
    receipt = activate(configuration, **kwargs)
    assert activate(configuration, **kwargs) == receipt
    assert processing_authorized(configuration, receipt)
    assert not processing_authorized(replace(configuration, security_revision="security-2"), receipt)
    with pytest.raises(ActivationRefused, match="stale"):
        activate(replace(configuration, source_configuration="ucm-v2"), **kwargs)


@pytest.mark.parametrize("gate", sorted(BASE_GATES))
def test_missing_or_failed_gate_writes_no_activation(tmp_path, configuration, gate):
    artifacts = evidence(tmp_path, configuration, **{gate: {"outcome": "failed"}})
    custody = tmp_path / "custody"
    with pytest.raises(ActivationRefused, match="prerequisite"):
        activate(configuration, evidence=artifacts, operator="local:operator", revision="one", custody=custody, now=NOW)
    assert not custody.exists()
    del artifacts[gate]
    with pytest.raises(ActivationRefused, match="missing prerequisites"):
        activate(configuration, evidence=artifacts, operator="local:operator", revision="one", custody=custody, now=NOW)


@pytest.mark.parametrize("mode", ["", "false", "FALSE", "unrecognized"])
def test_disabled_or_unknown_web_boundary_refuses_before_receipt(tmp_path, configuration, mode):
    config = replace(configuration, boundary_mode=mode)
    with pytest.raises(ActivationRefused):
        activate(config, evidence=evidence(tmp_path, config), operator="local:operator",
            revision="one", custody=tmp_path / "custody", now=NOW)
    assert not (tmp_path / "custody").exists()


def test_disposition_requires_actual_external_custody_and_matching_inventory(tmp_path, configuration):
    with pytest.raises(ActivationRefused, match="disposition"):
        activate(configuration, evidence=evidence(tmp_path, configuration,
            disposition={"inventory_digest": "other"}), operator="local:operator",
            revision="one", custody=tmp_path / "custody", now=NOW)


def test_tampered_evidence_and_conditional_provider_gate_are_refused(tmp_path, configuration):
    artifacts = evidence(tmp_path, configuration)
    artifacts["adopted_baseline"].path.write_text('{}')
    with pytest.raises(ActivationRefused, match="digest changed"):
        activate(configuration, evidence=artifacts, operator="local:operator", revision="one",
            custody=tmp_path / "custody", now=NOW)
    model = replace(configuration, model_provider_posture="fixture-approved-provider")
    with pytest.raises(ActivationRefused, match="model_provider_governance"):
        activate(model, evidence=evidence(tmp_path, model), operator="local:operator", revision="model",
            custody=tmp_path / "custody", now=NOW)


def test_boundary_collector_requires_actual_login_and_deployed_enforcement(
    runtime_database, configuration,
):
    """The HTTP adapter is synthetic; PostgreSQL runs as the real web login."""
    import os
    from types import SimpleNamespace
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.orm import Session
    from corridor.activation import collect_boundary_smoke
    from corridor.config import settings

    cases = [{"method": method, "template": route, "url": route,
        "expected_status": 200} for method, route in PILOT_ROUTES]
    cases.append({"method": "GET", "template": "/fixture-disabled", "url": "/fixture-disabled",
        "expected_status": 404})
    boundary_state = "enforced"
    def request(method, url, **kwargs):
        return SimpleNamespace(status_code=404 if url == "/fixture-disabled" else 200,
            text="fixture response", json=lambda: {"checks": [{"component": "live_pilot_web_boundary",
                "healthy": True, "detail": boundary_state}]})
    with runtime_database.session_factory() as owner:
        with pytest.raises(ActivationRefused, match="actual corridor_web"):
            collect_boundary_smoke(owner, configuration=configuration, request=request, cases=cases, now=NOW)
    with runtime_database.session_factory.begin() as owner:
        owner.execute(text("insert into customer_environment_binding(singleton,customer_id,environment_id,deployment_id) values (true,:customer,:environment,:deployment)"),
            {"customer": configuration.customer, "environment": configuration.environment, "deployment": configuration.deployment_id})
    url = make_url(settings.database_url).set(database=runtime_database.name,
        username="corridor_web", password=os.environ.get("CORRIDOR_WEB_DB_PASSWORD", "corridor_web"))
    web_engine = create_engine(url)
    try:
        with Session(web_engine) as web:
            observed = collect_boundary_smoke(web, configuration=configuration, request=request, cases=cases, now=NOW)
            assert observed["actual_login"] == "corridor_web"
            assert len(observed["observations"]) == len(PILOT_ROUTES) + 1
            with pytest.raises(ActivationRefused, match="actual database"):
                collect_boundary_smoke(web, configuration=replace(configuration, deployment_id="other"),
                    request=request, cases=cases, now=NOW)
            boundary_state = "not_declared"
            with pytest.raises(ActivationRefused, match="does not report an enforced"):
                collect_boundary_smoke(web, configuration=configuration, request=request, cases=cases, now=NOW)
    finally:
        web_engine.dispose()


def test_incomplete_route_smoke_cannot_be_promoted_to_activation(tmp_path, configuration):
    artifacts = evidence(tmp_path, configuration, web_boundary={"observations": [
        {"method": "GET", "template": "/", "status": 200}]})
    with pytest.raises(ActivationRefused, match="actual deployment smoke"):
        activate(configuration, evidence=artifacts, operator="local:operator", revision="one",
            custody=tmp_path / "custody", now=NOW)


@pytest.mark.parametrize("change", [
    {"evidence": {}}, {"evidence": None}, {"operator": ""}, {"revision": "  "},
    {"activated_at": "invalid"}, {"activated_at": "2026-09-09T00:00:00"},
    {"activated_at": "2999-09-09T00:00:00+00:00"},
])
def test_runtime_refuses_incomplete_activation_receipt(tmp_path, configuration, change):
    receipt = activate(configuration, evidence=evidence(tmp_path, configuration),
        operator="local:operator", revision="one", custody=tmp_path / "custody", now=NOW)
    payload = receipt.read() | change
    body = json.dumps(payload).encode()
    path = tmp_path / "incomplete.json"
    path.write_bytes(body)
    assert not processing_authorized(configuration, EvidenceArtifact(path, sha256(body).hexdigest()))


def test_runtime_refuses_invented_gate_digests(tmp_path, configuration):
    receipt = activate(configuration, evidence=evidence(tmp_path, configuration),
        operator="local:operator", revision="one", custody=tmp_path / "custody", now=NOW)
    payload = receipt.read()
    payload["evidence"]["customer_authorization"] = "passed"
    body = json.dumps(payload).encode()
    path = tmp_path / "invented.json"
    path.write_bytes(body)
    assert not processing_authorized(configuration, EvidenceArtifact(path, sha256(body).hexdigest()))


@pytest.mark.parametrize("defect", ["different-image", "retired-engine", "missing-notices", "clear-flag-only"])
def test_ucm_only_activation_requires_matching_built_image_observations(tmp_path, configuration, defect):
    assert configuration.processes_pdf is False
    artifacts = evidence(tmp_path, configuration)
    payload = artifacts["pdf_image_audit"].read()
    if defect == "different-image":
        payload["built_image_audit"]["image"]["image_id"] = "sha256:other"
    elif defect == "retired-engine":
        payload["built_image_audit"]["audited"]["environments"]["render_worker"]["present"] = ["retired-fixture-engine"]
    elif defect == "missing-notices":
        payload["built_image_audit"]["audited"]["notices"]["missing_files"] = ["fixture-LICENSE"]
    else:
        payload["built_image_audit"] = {"clear_of_retired_engines": True}
    body = json.dumps(payload).encode()
    path = artifacts["pdf_image_audit"].path
    path.write_bytes(body)
    artifacts["pdf_image_audit"] = EvidenceArtifact(path, sha256(body).hexdigest())
    with pytest.raises(ActivationRefused, match="image audit"):
        activate(configuration, evidence=artifacts, operator="local:operator", revision="one",
            custody=tmp_path / "custody", now=NOW)
    assert not (tmp_path / "custody").exists()


def test_operator_validate_writes_nothing_and_freeze_publishes_receipt(tmp_path, configuration, capsys):
    from dataclasses import asdict
    from corridor.activation_cli import main
    from pathlib import Path
    configuration_path = tmp_path / "configuration.json"
    configuration_path.write_text(json.dumps(asdict(configuration)))
    artifacts = evidence(tmp_path, configuration)
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps({gate: {"path": artifact.path.name, "sha256": artifact.sha256}
        for gate, artifact in artifacts.items()}))
    common = ["--configuration", str(configuration_path), "--evidence", str(evidence_path),
        "--operator", "local:operator", "--revision", "cli-revision"]
    before = set(tmp_path.iterdir())
    assert main(["validate", *common]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "validated"
    assert set(tmp_path.iterdir()) == before
    assert main(["freeze", *common, "--custody", str(tmp_path / "custody")]) == 0
    frozen = json.loads(capsys.readouterr().out)
    assert frozen["outcome"] == "frozen"
    receipt = EvidenceArtifact(Path(frozen["receipt_path"]), frozen["receipt_sha256"])
    assert processing_authorized(configuration, receipt)


@pytest.mark.parametrize("grant_kind", ["table_select", "column_select", "ownership"])
def test_boundary_smoke_checks_set_role_and_inherited_read_capabilities(runtime_database, configuration, grant_kind):
    """The actual login can assume an intermediary with non-SET inherited ACLs."""
    import os
    from types import SimpleNamespace
    from uuid import uuid4

    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.orm import Session

    from corridor.activation import collect_boundary_smoke
    from corridor.config import settings

    suffix = uuid4().hex
    intermediary = f"boundary_bridge_{suffix}"
    authority = f"boundary_reader_{suffix}"
    relation = f"boundary_revoked_{suffix}"
    cases = [{"method": method, "template": route, "url": route, "expected_status": 200}
        for method, route in PILOT_ROUTES]
    cases.append({"method": "GET", "template": "/fixture-disabled", "url": "/fixture-disabled", "expected_status": 404})
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url))
        return SimpleNamespace(status_code=404 if url == "/fixture-disabled" else 200,
            text="fixture response", json=lambda: {"checks": [{"component": "live_pilot_web_boundary",
                "healthy": True, "detail": "enforced"}]})

    web_engine = create_engine(make_url(settings.database_url).set(database=runtime_database.name,
        username="corridor_web", password=os.environ.get("CORRIDOR_WEB_DB_PASSWORD", "corridor_web")))
    created = False
    try:
        with runtime_database.session_factory.begin() as owner:
            owner.execute(text("insert into customer_environment_binding(singleton,customer_id,environment_id,deployment_id) values (true,:customer,:environment,:deployment)"),
                {"customer": configuration.customer, "environment": configuration.environment, "deployment": configuration.deployment_id})
            owner.execute(text(f'create role "{intermediary}" nologin noinherit'))
            owner.execute(text(f'create role "{authority}" nologin noinherit'))
            owner.execute(text(f'create table public."{relation}" (secret text)'))
            owner.execute(text(f'insert into public."{relation}" values (\'retained fixture value\')'))
            # Default privileges may grant web access to a new owner table;
            # this relation is explicitly revoked before the drift exercise.
            owner.execute(text(f'revoke all on public."{relation}" from public, corridor_web'))
            owner.execute(text(f'grant "{authority}" to "{intermediary}" with inherit true'))
            owner.execute(text(f'grant "{authority}" to "{intermediary}" with set false'))
            owner.execute(text(f'grant "{intermediary}" to corridor_web with inherit false'))
            owner.execute(text(f'grant "{intermediary}" to corridor_web with set true'))
        created = True
        with Session(web_engine) as web:
            assert collect_boundary_smoke(web, configuration=configuration, request=request, cases=cases, now=NOW)["outcome"] == "passed"
        with runtime_database.session_factory.begin() as owner:
            if grant_kind == "ownership":
                owner.execute(text(f'alter table public."{relation}" owner to "{authority}"'))
            else:
                privilege = "select(secret)" if grant_kind == "column_select" else "select"
                owner.execute(text(f'grant {privilege} on public."{relation}" to "{authority}"'))
        with Session(web_engine) as web:
            assert web.scalar(text("select has_table_privilege(current_user,:relation,'SELECT')"), {"relation": relation}) is False
            assert web.scalar(text("select has_any_column_privilege(current_user,:relation,'SELECT')"), {"relation": relation}) is False
            assert web.scalar(text("select pg_has_role(session_user,:role,'SET')"), {"role": authority}) is False
            with as_role(web, intermediary):
                assert web.scalar(text(f'select secret from public."{relation}"')) == "retained fixture value"
            calls.clear()
            with pytest.raises(ActivationRefused, match="revoked relation"):
                collect_boundary_smoke(web, configuration=configuration, request=request, cases=cases, now=NOW)
            assert calls == []
        with runtime_database.session_factory.begin() as owner:
            owner.execute(text(f'grant "{intermediary}" to corridor_web with set false'))
        with Session(web_engine) as web:
            # MEMBER alone, with neither INHERIT nor SET, conveys no read.
            assert web.scalar(text("select pg_has_role(session_user,:role,'MEMBER')"), {"role": authority}) is True
            assert collect_boundary_smoke(web, configuration=configuration, request=request, cases=cases, now=NOW)["outcome"] == "passed"
        with runtime_database.session_factory.begin() as owner:
            owner.execute(text(f'grant "{intermediary}" to corridor_web with inherit true'))
        with Session(web_engine) as web:
            with pytest.raises(ActivationRefused, match="revoked relation"):
                collect_boundary_smoke(web, configuration=configuration, request=request, cases=cases, now=NOW)
    finally:
        web_engine.dispose()
        if created:
            with runtime_database.session_factory.begin() as owner:
                owner.execute(text(f'drop table public."{relation}"'))
                owner.execute(text(f'revoke "{intermediary}" from corridor_web'))
                owner.execute(text(f'revoke "{authority}" from "{intermediary}"'))
                owner.execute(text(f'drop role "{intermediary}"'))
                owner.execute(text(f'drop role "{authority}"'))
