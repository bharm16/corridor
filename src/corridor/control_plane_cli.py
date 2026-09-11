"""Operations-only inputs for deployment, activation and receipt custody (#656).

A customer administration UI would add a new product surface and credential
handling. These bounded commands instead consume reviewed identifier/reference
JSON, refuse defaults, and report no driver text or credentials. They do not
create or destroy databases, grant customer access, or admit customer content.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError

from corridor.control_plane import (
    ControlPlane,
    DestructionReceipt,
    EnvironmentRegistration,
    OnboardingAuthorization,
    OnboardingAuthorizationEvent,
    OnboardingCustody,
)
from corridor.control_plane_schema import initialize_control_plane
from corridor.customer_routing import CustomerIdentity, bind_customer_environment


def required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError("an explicit operations configuration input is missing")
    return value


_CONTRACT = """\
Separate PostgreSQL operations registry and external receipts (#656).
Requires explicit role-specific URLs; never uses a default customer URL.
  make control-plane ARGS="initialize"
  make control-plane ARGS="register --file environment-registration.json"
Limited onboarding authorizations (#827, ADR-0099), issued and withdrawn here
because the control plane is authoritative for them:
  make control-plane ARGS="onboarding-issue --file onboarding-authorization.json"
  make control-plane ARGS="onboarding-event --file withdrawal-request.json"
  make control-plane ARGS="onboarding-show loa-2026-05"
Full input/custody contract: docs/operations/customer-environments.md and
docs/operations/onboarding-authorization.md.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=_CONTRACT,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "initialize", help="install only the separate control-plane schema"
    )
    commands.add_parser(
        "bind-customer", help="attest the customer DB identity using its owner login"
    )
    for name in ("register", "record-destruction", "onboarding-issue", "onboarding-event"):
        commands.add_parser(name).add_argument("--file", type=Path, required=True)
    commands.add_parser("onboarding-show").add_argument("authorization_id")
    for name in ("inspect", "receipts"):
        commands.add_parser(name).add_argument("environment_id")
    state = commands.add_parser("state")
    state.add_argument("environment_id")
    enabled = state.add_mutually_exclusive_group(required=True)
    enabled.add_argument("--enabled", dest="enabled", action="store_true")
    enabled.add_argument("--disabled", dest="enabled", action="store_false")
    held = state.add_mutually_exclusive_group(required=True)
    held.add_argument("--hold", dest="hold", action="store_true")
    held.add_argument("--no-hold", dest="hold", action="store_false")
    state.add_argument("--connector-configuration-ref")
    args = parser.parse_args(argv)
    engine = None
    result: dict[str, Any] | list[dict[str, Any]]
    try:
        url_input = {
            "initialize": "CONTROL_PLANE_DATABASE_URL",
            "bind-customer": "DATABASE_URL",
        }.get(args.command, "CONTROL_PLANE_OPERATIONS_DATABASE_URL")
        engine = create_engine(required_environment(url_input), hide_parameters=True)
        if args.command == "initialize":
            initialize_control_plane(engine)
            result = {"status": "initialized"}
        elif args.command == "bind-customer":
            identity = CustomerIdentity(
                required_environment("CORRIDOR_CUSTOMER_ID"),
                required_environment("CORRIDOR_CUSTOMER_ENVIRONMENT_ID"),
                required_environment("CORRIDOR_DEPLOYMENT_ID"),
            )
            bind_customer_environment(engine, identity)
            result = {"status": "bound", **asdict(identity)}
        elif args.command.startswith("onboarding-"):
            # The restricted operations actor's own custody commands. A
            # coordinator reaches none of this: it is a different database, a
            # different credential and a different party (ADR-0099).
            custody = OnboardingCustody(engine)
            if args.command == "onboarding-issue":
                payload = json.loads(args.file.read_text())
                for field in ("issued_at", "expires_at"):
                    payload[field] = datetime.fromisoformat(payload[field])
                result = asdict(custody.issue(OnboardingAuthorization(**payload)))
            elif args.command == "onboarding-event":
                payload = json.loads(args.file.read_text())
                for field in ("executed_at", "requested_at"):
                    if payload.get(field):
                        payload[field] = datetime.fromisoformat(payload[field])
                result = asdict(
                    custody.record_event(OnboardingAuthorizationEvent(**payload))
                )
            else:
                # The full record the operations audience is owed: the issued
                # terms, and every recorded fact about what happened to them --
                # who required a withdrawal, who executed it, its effective
                # request time and its enforcement state.
                result = {
                    "authorization": asdict(
                        custody.authorization(args.authorization_id)
                    ),
                    "events": [
                        asdict(event)
                        for event in custody.events(args.authorization_id)
                    ],
                }
        else:
            registry = ControlPlane(engine)
            if args.command in {"register", "record-destruction"}:
                payload = json.loads(args.file.read_text())
                if args.command == "register":
                    result = asdict(
                        registry.register(EnvironmentRegistration(**payload))
                    )
                else:
                    payload["observed_at"] = datetime.fromisoformat(
                        payload["observed_at"]
                    )
                    result = asdict(
                        registry.record_destruction(DestructionReceipt(**payload))
                    )
            elif args.command == "state":
                registry.set_state(
                    args.environment_id,
                    enabled=args.enabled,
                    hold=args.hold,
                    connector_configuration_ref=args.connector_configuration_ref,
                )
                result = asdict(registry.inspect(args.environment_id))
            elif args.command == "inspect":
                result = asdict(registry.inspect(args.environment_id))
            else:
                result = [
                    asdict(receipt)
                    for receipt in registry.destruction_receipts(args.environment_id)
                ]
        print(
            json.dumps(result, sort_keys=True, default=lambda value: value.isoformat())
        )
        return 0
    except (SQLAlchemyError, ValueError, TypeError, KeyError, OSError):
        print(
            "control-plane operation refused: check the explicit inputs, binding and operations credential",
            file=sys.stderr,
        )
        return 2
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
