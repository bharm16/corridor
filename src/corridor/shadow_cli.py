"""Operator entry points for isolated shadow provisioning, UCM capture and export.

Only provisioning accepts the separate schema-owner URL. Capture/export require
an explicit corridor_worker URL and verify its actual database capability. The
operator supplies existing signed authorization and compatibility receipts; this
adapter creates neither and never adopts a baseline or activates customer data.
"""
from __future__ import annotations

import argparse
from datetime import date
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import tempfile

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from corridor.compatibility_intake import CompatibilityReceipt
from corridor.models import Project, SourceDelivery
from corridor.native_provider_boundary import CustomerAuthorization
from corridor.principals import HumanPrincipal
from corridor.push_intake import PushCredential, PushPayload, accept_delivery, bind_credential, register_push_credential
from corridor.shadow_capabilities import ShadowRefused, verify_runtime
from corridor.shadow_processing import provision_shadow_project, run_shadow_ucm, validate_shadow_input
from corridor.shadow_receipts import read_shadow_run
from corridor.source_delivery import envelope_for_delivery
from corridor.source_intake import validate_and_stage
from corridor.storage import staged_file


def _authorization(path):
    value = json.loads(path.read_bytes())
    if value.pop("kind", None) != "customer-authorization":
        raise ShadowRefused("a retained customer authorization is required")
    for key in ("projects", "source_classes", "purposes", "stages", "source_sha256s"):
        value[key] = frozenset(value[key])
    return CustomerAuthorization(**value)


def _compatibility(path, expected_sha256):
    body = path.read_bytes()
    if sha256(body).hexdigest() != expected_sha256:
        raise ShadowRefused("compatibility receipt digest differs")
    value = json.loads(body)
    return CompatibilityReceipt(value["kind"], value["outcome"], value["source"]["content_sha256"],
        expected_sha256, "", path)


def _write_export(path, output):
    body = json.dumps({"canonicalization": "postgresql-jsonb-text-v1", "payload_text": output.payload_text,
        "output_sha256": output.output_sha256}, sort_keys=True).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".shadow-export-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != body:
                raise ShadowRefused("export destination already contains different bytes")
    finally:
        temporary.unlink(missing_ok=True)
    return sha256(body).hexdigest()


def _parser():
    parser = argparse.ArgumentParser(prog="shadow-processing")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("provision", "run", "export"):
        sub = commands.add_parser(name)
        sub.add_argument("--database-url-env", required=True,
            help="name of the environment variable holding this command's explicit database URL")
        sub.add_argument("--project-id", required=True, type=int)
        sub.add_argument("--customer", required=True)
        sub.add_argument("--environment", required=True)
        if name in {"provision", "run"}:
            sub.add_argument("--operator", required=True)
        if name == "provision":
            sub.add_argument("--intake-secret-env", help="optionally register a new project-bound webhook secret")
        if name == "run":
            source = sub.add_mutually_exclusive_group(required=True)
            source.add_argument("--input", type=Path)
            source.add_argument("--delivery-id", type=int)
            sub.add_argument("--intake-secret-env", help="required with --input; existing project-bound webhook secret")
            sub.add_argument("--transport-delivery-id", help="stable transport identity when uploading --input")
            sub.add_argument("--compatibility-receipt", type=Path, required=True)
            sub.add_argument("--compatibility-sha256", required=True)
            sub.add_argument("--authorization", type=Path, required=True)
            sub.add_argument("--source-configuration", required=True)
            sub.add_argument("--deletion-date", type=date.fromisoformat, required=True)
            sub.add_argument("--complete", action="store_true")
            sub.add_argument("--sealed", action="store_true")
        if name == "export":
            sub.add_argument("--identity", required=True)
            sub.add_argument("--output", type=Path, required=True)
    return parser


def _run(session, args, project):
    authorization = _authorization(args.authorization)
    compatibility = _compatibility(args.compatibility_receipt, args.compatibility_sha256)
    if args.input:
        if not args.intake_secret_env or not os.environ.get(args.intake_secret_env):
            raise ShadowRefused("an existing project-bound intake credential is required")
        body = args.input.read_bytes()
        source_sha256 = sha256(body).hexdigest()
    else:
        delivery = session.get(SourceDelivery, args.delivery_id)
        if delivery is None or delivery.project_id != project.id or delivery.customer != args.customer:
            raise ShadowRefused("delivery is outside the declared shadow project")
        source_sha256 = delivery.content_sha256
    validate_shadow_input(compatibility_receipt=compatibility, authorization=authorization,
        source_sha256=source_sha256, customer=args.customer, project_slug=project.slug,
        environment=args.environment, deletion_date=args.deletion_date)
    if args.input:
        binding = bind_credential(session, PushCredential("webhook", os.environ[args.intake_secret_env]))
        if (binding.customer, binding.project_id) != (args.customer, project.id):
            raise ShadowRefused("intake credential is outside the declared shadow project")
        receipt = accept_delivery(session, binding, PushPayload(body, args.input.name,
            transport_delivery_id=args.transport_delivery_id))
        envelope = receipt.envelope
        staged = validate_and_stage(body, args.input.name)
    else:
        envelope = envelope_for_delivery(session, delivery.id)
        path = staged_file(source_sha256)
        if path is None:
            raise ShadowRefused("stored source bytes are unavailable")
        staged = validate_and_stage(path.read_bytes(), path.name)
    payload = run_shadow_ucm(session, project=project, staged=staged, envelope=envelope,
        compatibility_receipt=compatibility, authorization=authorization, customer=args.customer,
        environment=args.environment, source_configuration=args.source_configuration,
        principal=HumanPrincipal(args.operator), deletion_date=args.deletion_date,
        is_complete_enumerative_source=args.complete, row_accounting_sealed=args.sealed)
    output = read_shadow_run(session, payload["identity"])
    return {"outcome": "frozen", "identity": payload["identity"], "output_sha256": output.output_sha256}


def main(argv=None):
    args = _parser().parse_args(argv)
    engine = None
    try:
        # Secrets are resolved from environment variables, never echoed or
        # accepted as command-line URL arguments that shell history records.
        url = make_url(os.environ[args.database_url_env])
        if url.drivername != "postgresql+psycopg" or not all((url.username, url.password, url.host, url.port, url.database)):
            raise ShadowRefused("an explicit PostgreSQL URL is required")
        if args.command != "provision" and url.username != "corridor_worker":
            raise ShadowRefused("capture/export require the corridor_worker URL")
        if args.command == "provision" and url.username.startswith("corridor_"):
            raise ShadowRefused("provisioning requires the separate schema-owner URL")
        engine = create_engine(url, hide_parameters=True)
        with Session(engine) as session, session.begin():
            if args.command != "provision":
                verify_runtime(session, project_id=args.project_id, customer=args.customer, environment=args.environment)
            project = session.get(Project, args.project_id)
            if project is None:
                raise ShadowRefused("shadow project does not exist")
            if args.command == "provision":
                operator = HumanPrincipal(args.operator)
                provision_shadow_project(session, project_id=project.id, customer=args.customer,
                    environment=args.environment, operator=operator.subject)
                if args.intake_secret_env:
                    material = os.environ[args.intake_secret_env]
                    # Reuse an exact previously bound credential; never repoint it.
                    try:
                        bound = bind_credential(session, PushCredential("webhook", material))
                    except ValueError:
                        register_push_credential(session, customer=args.customer, project=project,
                            channel="webhook", material=material)
                    else:
                        if (bound.customer, bound.project_id) != (args.customer, project.id):
                            raise ShadowRefused("existing intake credential belongs elsewhere")
                outcome = {"outcome": "provisioned", "project_id": project.id, "environment": args.environment}
            elif args.command == "run":
                outcome = _run(session, args, project)
            else:
                output = read_shadow_run(session, args.identity)
                if output.payload["project_id"] != project.id:
                    raise ShadowRefused("export run is outside the declared project")
                artifact_sha256 = _write_export(args.output, output)
                outcome = {"outcome": "exported", "path": str(args.output.resolve()),
                    "artifact_sha256": artifact_sha256, "output_sha256": output.output_sha256}
        print(json.dumps(outcome))
        return 0
    except (ShadowRefused, SQLAlchemyError, OSError, ValueError, TypeError, KeyError, AttributeError):
        print("shadow command refused: role, scope, source authorization, or retained evidence is invalid", file=sys.stderr)
        return 2
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
