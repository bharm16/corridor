"""CLI boundary for the isolated, digest-pinned SH 99 Admission rehearsal.

The generic M8 CLI proves a captured NHHIP chain and controlled carry-forward
policy; it cannot show the SH 99 Admission residue, exact outcomes, or separate
shared-database approval gate.  This thin command exposes that narrower
operations proof and the later shared-operation seal without making a source
database a verifier dependency. A separate seal CLI was rejected because both
receipts must use one exact replay and database-free verification boundary.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

from corridor.sh99_admission_acceptance import (
    SH99AdmissionAcceptanceConfig,
    SH99SharedAdmissionSealConfig,
    run_sh99_admission_acceptance,
    run_sh99_shared_admission_seal,
    verify_sh99_admission_bundle,
    verify_sh99_shared_admission_seal_bundle,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _sha256(value: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "must be exactly 64 lowercase hexadecimal characters"
        )
    return value


def _active_run(value: str) -> tuple[int, int]:
    try:
        document, run = value.split(":", 1)
        parsed = (int(document), int(run))
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be DOCUMENT_ID:EXTRACTION_RUN_ID") from exc
    if any(item < 1 for item in parsed):
        raise argparse.ArgumentTypeError("must contain positive integer identifiers")
    return parsed


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
    seal = commands.add_parser(
        "seal",
        help="seal the exact ordinary shared Admission path on a disposable clone",
    )
    seal.add_argument("--project-slug", required=True)
    seal.add_argument("--source-database-url", required=True)
    seal.add_argument("--expected-clean-git-revision", required=True)
    seal.add_argument("--output-dir", required=True, type=Path)
    seal.add_argument("--postgres-admin-url", required=True)
    seal.add_argument("--expected-acceptance-receipt-id", required=True, type=int)
    seal.add_argument(
        "--expected-acceptance-receipt-sha256", required=True, type=_sha256
    )
    seal.add_argument("--expected-activation-id", required=True, type=int)
    seal.add_argument(
        "--expected-active-run", required=True, action="append", type=_active_run
    )
    seal.add_argument("--expected-candidate-id", required=True, type=int)
    verify = commands.add_parser(
        "verify", help="verify a receipt export without a source database"
    )
    verify.add_argument("bundle_dir", type=Path)
    verify.add_argument("--expected-manifest-sha256", required=True, type=_sha256)
    verify_seal = commands.add_parser(
        "verify-seal", help="verify a shared-operation seal without a database"
    )
    verify_seal.add_argument("bundle_dir", type=Path)
    verify_seal.add_argument(
        "--expected-manifest-sha256", required=True, type=_sha256
    )
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
        elif args.command == "seal":
            summary = run_sh99_shared_admission_seal(
                SH99SharedAdmissionSealConfig(
                    project_slug=args.project_slug,
                    source_database_url=args.source_database_url,
                    expected_clean_git_revision=args.expected_clean_git_revision,
                    output_dir=args.output_dir,
                    postgres_admin_url=args.postgres_admin_url,
                    expected_acceptance_receipt_id=args.expected_acceptance_receipt_id,
                    expected_acceptance_receipt_sha256=(
                        args.expected_acceptance_receipt_sha256
                    ),
                    expected_activation_id=args.expected_activation_id,
                    expected_active_runs=tuple(args.expected_active_run),
                    expected_candidate_id=args.expected_candidate_id,
                )
            )
            payload = {
                "command": "seal",
                "bundle_dir": str(summary.bundle_dir),
                "manifest_path": str(summary.manifest_path),
                "integrity_manifest_sha256": summary.integrity_manifest_sha256,
                "canonical_content_sha256": summary.canonical_content_sha256,
                "database_name": summary.database_name,
                "shared_database_mutated": False,
            }
        elif args.command == "verify":
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
        else:
            verified = verify_sh99_shared_admission_seal_bundle(
                args.bundle_dir,
                expected_integrity_manifest_sha256=args.expected_manifest_sha256,
            )
            payload = {
                "command": "verify-seal",
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
