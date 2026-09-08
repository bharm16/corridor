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
)
from corridor.control_plane_schema import initialize_control_plane
from corridor.customer_routing import CustomerIdentity, bind_customer_environment


def required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError("an explicit operations configuration input is missing")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "initialize", help="install only the separate control-plane schema"
    )
    commands.add_parser(
        "bind-customer", help="attest the customer DB identity using its owner login"
    )
    for name in ("register", "record-destruction"):
        commands.add_parser(name).add_argument("--file", type=Path, required=True)
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
