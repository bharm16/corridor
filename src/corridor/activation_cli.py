"""Validate or freeze explicit activation evidence without deployment side effects.

The operator names the configuration and evidence manifest. This command neither
scans a live image nor enables routing, and validation writes no receipt. The
freeze command publishes the same checked manifest in separate receipt custody.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from corridor.activation import ActivationConfiguration, ActivationRefused, EvidenceArtifact, activate, validate_activation_evidence
from corridor.principals import HumanPrincipal


def main(argv=None):
    parser = argparse.ArgumentParser(prog="activation")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "freeze"):
        sub = commands.add_parser(command)
        sub.add_argument("--configuration", type=Path, required=True)
        sub.add_argument("--evidence", type=Path, required=True,
            help="JSON gate -> {path, sha256}; relative artifact paths resolve beside this manifest")
        sub.add_argument("--operator", required=True)
        sub.add_argument("--revision", required=True)
        if command == "freeze":
            sub.add_argument("--custody", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        operator = HumanPrincipal(args.operator)
        configuration = ActivationConfiguration(**json.loads(args.configuration.read_bytes()))
        manifest = json.loads(args.evidence.read_bytes())
        artifacts = {}
        for gate, reference in manifest.items():
            path = Path(reference["path"])
            if not path.is_absolute():
                path = args.evidence.parent / path
            artifacts[gate] = EvidenceArtifact(path, reference["sha256"])
        common = dict(evidence=artifacts, operator=operator.subject, revision=args.revision)
        if args.command == "validate":
            payload = validate_activation_evidence(configuration, **common)
            print(json.dumps({"outcome": "validated", "configuration_digest": configuration.identity,
                "revision": args.revision, "gates": sorted(payload["evidence"])}))
        else:
            receipt = activate(configuration, custody=args.custody, **common)
            print(json.dumps({"outcome": "frozen", "configuration_digest": configuration.identity,
                "receipt_path": str(receipt.path.resolve()), "receipt_sha256": receipt.sha256}))
        return 0
    except (ActivationRefused, OSError, ValueError, TypeError, KeyError, AttributeError):
        print("activation refused: configuration or prerequisite evidence is missing, invalid, or stale", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
