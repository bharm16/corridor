"""CLI boundary for the isolated, digest-pinned SH 99 Admission rehearsal.

The generic M8 CLI proves a captured NHHIP chain and controlled carry-forward
policy; it cannot show the SH 99 Admission residue, exact outcomes, or separate
shared-database approval gate.  This thin command exposes that narrower
operations proof without making a source database a verifier dependency.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

from corridor.sh99_admission_acceptance import (
    SH99AdmissionAcceptanceConfig,
    run_sh99_admission_acceptance,
    verify_sh99_admission_bundle,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _sha256(value: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "must be exactly 64 lowercase hexadecimal characters"
        )
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sh99-admission-acceptance")
    commands = parser.add_subparsers(dest="command", required=True)
    replay = commands.add_parser(
        "replay",
        help="clone the pinned real SH 99 state and run its exact Admission command",
    )
    replay.add_argument("--project-slug", required=True)
    replay.add_argument("--source-database-url", required=True)
    replay.add_argument("--expected-clean-git-revision", required=True)
    replay.add_argument("--output-dir", required=True, type=Path)
    replay.add_argument("--postgres-admin-url", required=True)
    verify = commands.add_parser(
        "verify", help="verify a receipt export without a source database"
    )
    verify.add_argument("bundle_dir", type=Path)
    verify.add_argument("--expected-manifest-sha256", required=True, type=_sha256)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one explicitly bounded operation and emit stable machine JSON."""

    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    try:
        if args.command == "replay":
            summary = run_sh99_admission_acceptance(
                SH99AdmissionAcceptanceConfig(
                    project_slug=args.project_slug,
                    source_database_url=args.source_database_url,
                    expected_clean_git_revision=args.expected_clean_git_revision,
                    output_dir=args.output_dir,
                    postgres_admin_url=args.postgres_admin_url,
                )
            )
            payload = {
                "command": "replay",
                "bundle_dir": str(summary.bundle_dir),
                "manifest_path": str(summary.manifest_path),
                "integrity_manifest_sha256": summary.integrity_manifest_sha256,
                "canonical_content_sha256": summary.canonical_content_sha256,
                "database_name": summary.database_name,
                "shared_database_mutated": False,
            }
        else:
            verified = verify_sh99_admission_bundle(
                args.bundle_dir,
                expected_integrity_manifest_sha256=args.expected_manifest_sha256,
            )
            payload = {
                "command": "verify",
                "bundle_dir": str(args.bundle_dir),
                "valid": verified.valid,
                "integrity_manifest_sha256": verified.integrity_manifest_sha256,
                "canonical_content_sha256": verified.canonical_content_sha256,
            }
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
