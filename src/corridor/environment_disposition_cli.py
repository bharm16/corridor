"""Operator entry point for AWS export, disposition and recovery (#514).

The first implementation exposed library seams only. This CLI connects those
seams using explicit AWS profiles and database credential environment references,
writes private artifacts atomically, and requires an explicit authorization
flag before constructing any AWS clients. It never looks up secret values,
initializes a customer database, or activates a customer route.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from types import SimpleNamespace

from corridor.aws_environment_disposition import AwsStackEnvironmentDestroyer, observe_stack_inventory
from corridor.disposition_contracts import json_digest as digest
from corridor.control_plane import ControlPlane
from corridor.disposition_evidence import disposition_gate_payload
from corridor.environment_disposition import (
    AwsDispositionResources, DispositionRefused, ReferentialRetention, RetentionSchedule,
    execute_environment_disposition, plan_environment_disposition,
)
from corridor.environment_export import export_environment_archive, transfer_export_custody
from corridor.environment_rehearsal import AwsRestoreRehearsal, RestoreRehearsal, sql_restore_probe
from corridor.principals import HumanPrincipal
from corridor.receipts import write_private_snapshot


_COMMANDS = ("inventory", "export", "custody", "plan", "execute", "status",
             "rehearsal-restore", "rehearsal-verify", "rehearsal-cleanup", "gate")


def _environment_reference(name):
    if not name or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or not os.environ.get(name):
        raise DispositionRefused("a populated database-URL environment-variable reference is required")
    return os.environ[name]


def _write_private_json(path, payload):
    write_private_snapshot(Path(path), json.dumps(payload, sort_keys=True, indent=2,
        default=lambda value: value.isoformat() if isinstance(value, datetime) else str(value)) + "\n")


def _provider_clients(resources, *, source_profile, custody_profile, authorized):
    if not authorized or not source_profile:
        raise DispositionRefused("AWS commands require --authorize-aws-535 and an explicit --aws-profile")
    import boto3
    from botocore.config import Config
    session = boto3.Session(profile_name=source_profile, region_name=resources.region)
    config = Config(connect_timeout=5, read_timeout=30, retries={"mode": "standard", "total_max_attempts": 3})
    clients = {name: session.client(name, config=config) for name in (
        "sts", "rds", "s3", "kms", "cloudformation", "ecs", "logs", "secretsmanager", "ecr", "ec2")}
    regions = [row["RegionName"] for row in clients["ec2"].describe_regions(AllRegions=False)["Regions"]]
    clients["regional"] = {region: {name: session.client(name, region_name=region, config=config)
                                    for name in ("sts", "rds", "backup")} for region in regions}
    if custody_profile:
        custody = boto3.Session(profile_name=custody_profile, region_name=resources.region)
        clients["custody_s3"] = custody.client("s3", config=config)
    return clients


def _close_clients(clients):
    seen = set()
    def close(value):
        if isinstance(value, dict):
            for child in value.values():
                close(child)
        elif id(value) not in seen:
            seen.add(id(value))
            value.close()
    close(clients)


def _run(arguments, configuration, control_plane, clients):
    values = dict(configuration["resources"])
    values["kms_key_arns"] = tuple(values["kms_key_arns"])
    resources = AwsDispositionResources(**values)
    command = arguments.command
    result = dict(configuration)
    if command == "status":
        return {"environment_id": resources.environment_id,
                "plans": [asdict(row) for row in control_plane.disposition_plans(resources.environment_id)],
                "destruction_receipts": [asdict(row) for row in control_plane.destruction_receipts(resources.environment_id)],
                "rehearsal_receipts": control_plane.disposition_rehearsal_receipts(resources.environment_id, arguments.operation_id)
                    if arguments.operation_id else []}
    if command == "gate":
        return disposition_gate_payload(control_plane, plan_id=configuration["disposition_plan"]["plan_id"],
            operation_id=arguments.operation_id, rehearsal_operation_id=configuration["rehearsal"]["operation_id"],
            configuration_identity=configuration["activation_configuration_identity"],
            inventory_digest=configuration["disposition_inventory_digest"])
    if not arguments.principal:
        raise DispositionRefused("provider operations require an explicit named --principal")
    principal = HumanPrincipal(arguments.principal)
    bucket = resources.object_namespace_bucket
    registration = control_plane.inspect(resources.environment_id)
    resources.require_registration(registration)
    destroyer = AwsStackEnvironmentDestroyer(live_activation="live-aws-535" if arguments.authorize_aws_535 else None,
        resources=resources, approved_resource_sha256=resources.sha256, clients=clients)
    clock = SimpleNamespace(now=lambda: datetime.now(timezone.utc))
    def freeze_guard():
        destroyer.require_frozen(control_plane.inspect(resources.environment_id))
    if command == "inventory":
        destroyer.require_bound(registration)
        inventory = observe_stack_inventory(clients, resources,
            application_stack_id=configuration["application_stack_id"], data_stack_id=configuration["data_stack_id"])
        result["resources"] = asdict(replace(resources, whole_environment=inventory))
    elif command == "export":
        if not arguments.archive_output or not arguments.pgpass_file or not arguments.database_username or not arguments.postgres_ca_file:
            raise DispositionRefused("export requires --archive-output, --pgpass-file, --database-username and --postgres-ca-file")
        archive = export_environment_archive(resources=resources, inventory=resources.whole_environment, clients=clients,
            output_path=arguments.archive_output, pgpass_file=arguments.pgpass_file, database_username=arguments.database_username,
            postgres_tls_root_cert=arguments.postgres_ca_file, principal=principal, before_export=freeze_guard,
            unavailability_disclosure=Path(arguments.disclosure_file).read_bytes() if arguments.disclosure_file else b"")
        result["archive"] = {key: archive[key] for key in ("path", "sha256")}
    elif command == "custody":
        if "custody_s3" not in clients:
            raise DispositionRefused("custody transfer requires an explicit separate --custody-profile")
        destroyer.require_bound(registration)
        destination = configuration["custody_destination"]
        if destination["owner"] == resources.account_id:
            raise DispositionRefused("custody destination must have a separate owner")
        custody = transfer_export_custody(client=clients["custody_s3"], archive_path=configuration["archive"]["path"],
            bucket=destination["bucket"], key=destination["key"], owner=destination["owner"],
            environment_id=resources.environment_id, inventory_sha256=digest(resources.whole_environment), principal=principal)
        result["resources"] = asdict(replace(resources, whole_environment={**resources.whole_environment, "custody": custody}))
    elif command == "plan":
        if "custody_s3" not in clients:
            raise DispositionRefused("planning requires --custody-profile for export verification")
        manifest = plan_environment_disposition(control_plane, environment_id=resources.environment_id,
            schedules=[RetentionSchedule(row["label"], datetime.fromisoformat(row["retain_until"])) for row in configuration["retention_schedules"]],
            referential=ReferentialRetention(**configuration["referential_retention"]), principal=principal, as_of=clock.now(),
            provider_resources=resources, provider_observer=destroyer)
        result["disposition_plan"] = {"plan_id": manifest.plan_id, "content_sha256": manifest.content_sha256, "status": manifest.status}
    elif command == "execute":
        if not arguments.operation_id or "custody_s3" not in clients:
            raise DispositionRefused("execution requires stable --operation-id and --custody-profile")
        plan = configuration["disposition_plan"]
        outcome = execute_environment_disposition(control_plane, plan_id=plan["plan_id"], expected_sha256=plan["content_sha256"],
            destroyer=destroyer, operation_id=arguments.operation_id, recorded_by=principal.subject,
            referential=ReferentialRetention(**configuration["referential_retention"]), clock=clock)
        result["execution"] = asdict(outcome)
    else:
        specification = dict(configuration["rehearsal"])
        specification["restore_time"] = datetime.fromisoformat(specification["restore_time"])
        specification["security_group_ids"] = tuple(specification["security_group_ids"])
        rehearsal = AwsRestoreRehearsal(destroyer=destroyer, control_plane=control_plane,
            specification=RestoreRehearsal(**specification), clock=clock, before_mutation=freeze_guard)
        if command == "rehearsal-restore":
            result["rehearsal_observation"] = rehearsal.restore()
        elif command == "rehearsal-cleanup":
            result["rehearsal_observation"] = rehearsal.cleanup()
        else:
            import psycopg
            from sqlalchemy.engine import make_url
            from corridor.object_storage import S3ObjectStore
            url = make_url(_environment_reference(arguments.restored_database_url_env)).set(drivername="postgresql")
            store = S3ObjectStore(bucket=bucket, client=clients["s3"])
            with psycopg.connect(url.render_as_string(hide_password=False)) as connection:
                result["rehearsal_observation"] = rehearsal.verify(lambda target: sql_restore_probe(connection,
                    target=target, queries=configuration["rehearsal_queries"], object_store=store,
                    object_digests=specification["expected_object_digests"]))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=_COMMANDS)
    parser.add_argument("--configuration", required=True, help="JSON configuration with resource identities, never credential values")
    parser.add_argument("--output", required=True, help="private JSON result/configuration artifact")
    parser.add_argument("--control-plane-url-env", default="CONTROL_PLANE_OPERATIONS_DATABASE_URL")
    parser.add_argument("--restored-database-url-env")
    parser.add_argument("--aws-profile")
    parser.add_argument("--custody-profile")
    parser.add_argument("--authorize-aws-535", action="store_true", help="explicitly authorize the selected command's AWS calls")
    parser.add_argument("--principal")
    parser.add_argument("--operation-id")
    parser.add_argument("--archive-output")
    parser.add_argument("--pgpass-file")
    parser.add_argument("--database-username")
    parser.add_argument("--postgres-ca-file")
    parser.add_argument("--disclosure-file")
    arguments = parser.parse_args(argv)
    from sqlalchemy import create_engine
    engine = None
    clients = {}
    try:
        configuration = json.loads(Path(arguments.configuration).read_text())
        values = dict(configuration["resources"])
        values["kms_key_arns"] = tuple(values["kms_key_arns"])
        resources = AwsDispositionResources(**values)
        engine = create_engine(_environment_reference(arguments.control_plane_url_env), echo=False, hide_parameters=True)
        if arguments.command not in {"status", "gate"}:
            clients = _provider_clients(resources, source_profile=arguments.aws_profile,
                custody_profile=arguments.custody_profile, authorized=arguments.authorize_aws_535)
        payload = _run(arguments, configuration, ControlPlane(engine), clients)
        _write_private_json(arguments.output, payload)
        state = payload.get("execution", {}).get("status") or payload.get("rehearsal_observation", {}).get("outcome")
        suffix = f" ({state})" if state else ""
        print(f"Wrote {arguments.command} result to {arguments.output}{suffix}")
        return 3 if state in {"partial", "pending", "refused"} else 0
    except DispositionRefused as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        # SQL/SDK exception strings may carry connection or credential context.
        # Keep those bytes out of stdout, logs and evidence artifacts.
        print(f"{type(exc).__name__}: operation failed; no success artifact was written", file=sys.stderr)
        return 1
    finally:
        _close_clients(clients)
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
