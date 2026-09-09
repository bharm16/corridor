"""Operator commands use real database capabilities and retained source receipts."""
from hashlib import sha256
import json
import os

from sqlalchemy.engine import make_url
from sqlalchemy import text

from corridor.config import settings
from corridor.shadow_cli import main
from test_shadow_processing import shadow as shadow, CUSTOMER, PRINCIPAL, DELETE


def test_operator_provision_run_and_exact_export(shadow, tmp_path, monkeypatch, capsys):
    database, _engines, project_id, _staged, envelope, authorization, compatibility = shadow
    owner_url = make_url(settings.database_url).set(database=database.name)
    worker_url = owner_url.set(username="corridor_worker",
        password=os.environ.get("CORRIDOR_WORKER_DB_PASSWORD", "corridor_worker"))
    monkeypatch.setenv("SHADOW_TEST_OWNER_URL", owner_url.render_as_string(hide_password=False))
    monkeypatch.setenv("SHADOW_TEST_WORKER_URL", worker_url.render_as_string(hide_password=False))
    monkeypatch.setenv("SHADOW_TEST_INTAKE_SECRET", "secret-shadow-ucm")
    common = ["--project-id", str(project_id), "--customer", CUSTOMER, "--environment", "synthetic-shadow"]
    assert main(["provision", "--database-url-env", "SHADOW_TEST_OWNER_URL", *common,
        "--operator", PRINCIPAL.subject, "--intake-secret-env", "SHADOW_TEST_INTAKE_SECRET"]) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "provisioned"
    authorization_path = tmp_path / "authorization.json"
    authorization_path.write_text(json.dumps(authorization.as_dict()))
    with database.session_factory() as owner:
        delivery_id = owner.scalar(text("select id from source_deliveries where idempotency_key=:key and disposition='stored'"),
            {"key": envelope.idempotency_key})
    run = ["run", "--database-url-env", "SHADOW_TEST_WORKER_URL", *common,
        "--operator", PRINCIPAL.subject, "--delivery-id", str(delivery_id),
        "--compatibility-receipt", str(compatibility.stored_path),
        "--compatibility-sha256", compatibility.receipt_sha256,
        "--authorization", str(authorization_path), "--source-configuration", "operator-ucm-v1",
        "--deletion-date", DELETE.isoformat(), "--complete", "--sealed"]
    assert main(run) == 0
    first = json.loads(capsys.readouterr().out)
    assert main(run) == 0
    assert json.loads(capsys.readouterr().out) == first
    destination = tmp_path / "frozen-shadow.json"
    export = ["export", "--database-url-env", "SHADOW_TEST_WORKER_URL", *common,
        "--identity", first["identity"], "--output", str(destination)]
    assert main(export) == 0
    json.loads(capsys.readouterr().out)
    retained = json.loads(destination.read_bytes())
    assert sha256(retained["payload_text"].encode()).hexdigest() == retained["output_sha256"]
    assert json.loads(retained["payload_text"])["identity"] == first["identity"]
    assert main(export) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "exported"


def test_capture_rejects_owner_url_before_database_or_source_access(monkeypatch, capsys):
    monkeypatch.setenv("SHADOW_TEST_OWNER_URL", "postgresql+psycopg://owner:fixture@localhost:5433/missing")
    assert main(["run", "--database-url-env", "SHADOW_TEST_OWNER_URL", "--project-id", "1",
        "--customer", "fixture", "--environment", "fixture", "--operator", "local:operator",
        "--delivery-id", "1", "--compatibility-receipt", "/nonexistent/receipt.json",
        "--compatibility-sha256", "a" * 64, "--authorization", "/nonexistent/authorization.json",
        "--source-configuration", "fixture", "--deletion-date", "2999-01-01"]) == 2
    assert "refused" in capsys.readouterr().err
