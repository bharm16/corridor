"""CLI for the digest-pinned, disposable SH 99 coordinator rehearsal."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

from corridor import digests
from corridor.sh99_coordinator_rehearsal import (
    SH99CoordinatorRehearsalConfig,
    run_sh99_coordinator_rehearsal,
    verify_coordinator_rehearsal_bundle,
)


_MIGRATION_REVISION = re.compile(r"^[0-9a-f]{12}$")


def _sha256(value: str) -> str:
    if not digests.is_digest(value):
        raise argparse.ArgumentTypeError("must be exactly 64 lowercase hexadecimal characters")
    return value


def _non_negative_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a non-negative number of seconds") from exc
    if seconds < 0:
        raise argparse.ArgumentTypeError("must be a non-negative number of seconds")
    return seconds


def _migration_revision(value: str) -> str:
    if _MIGRATION_REVISION.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "must be exactly 12 lowercase hexadecimal characters"
        )
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sh99-coordinator-rehearsal")
    commands = parser.add_subparsers(dest="command", required=True)
    replay = commands.add_parser(
        "replay",
        help="clone pinned SH 99 state and exercise the existing coordinator interface",
    )
    replay.add_argument("--project-slug", required=True)
    replay.add_argument("--source-database-url", required=True)
    replay.add_argument("--postgres-admin-url", required=True)
    replay.add_argument("--expected-clean-git-revision", required=True)
    replay.add_argument(
        "--expected-source-migration-head", required=True, type=_migration_revision
    )
    replay.add_argument(
        "--expected-target-migration-head", required=True, type=_migration_revision
    )
    replay.add_argument("--shared-admission-receipt-path", required=True, type=Path)
    replay.add_argument("--expected-shared-admission-receipt-sha256", required=True, type=_sha256)
    replay.add_argument("--approved-shared-state-receipt", required=True)
    replay.add_argument("--shared-backfill-elapsed-seconds", required=True, type=_non_negative_seconds)
    replay.add_argument("--output-dir", required=True, type=Path)
    replay.add_argument("--coordinator-subject", default="local:sh99-coordinator")
    replay.add_argument("--coordinator-display-name", default="SH 99 Coordinator")
    verify = commands.add_parser(
        "verify", help="verify a closed coordinator bundle without a source database"
    )
    verify.add_argument("bundle_dir", type=Path)
    verify.add_argument("--expected-manifest-sha256", required=True, type=_sha256)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one bounded rehearsal or verify one previously published bundle."""

    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    try:
        if args.command == "replay":
            summary = run_sh99_coordinator_rehearsal(
                SH99CoordinatorRehearsalConfig(
                    project_slug=args.project_slug,
                    source_database_url=args.source_database_url,
                    postgres_admin_url=args.postgres_admin_url,
                    expected_clean_git_revision=args.expected_clean_git_revision,
                    expected_source_migration_head=args.expected_source_migration_head,
                    expected_target_migration_head=args.expected_target_migration_head,
                    shared_admission_receipt_path=args.shared_admission_receipt_path,
                    expected_shared_admission_receipt_sha256=(
                        args.expected_shared_admission_receipt_sha256
                    ),
                    approved_shared_state_receipt=args.approved_shared_state_receipt,
                    shared_backfill_elapsed_seconds=args.shared_backfill_elapsed_seconds,
                    output_dir=args.output_dir,
                    coordinator_subject=args.coordinator_subject,
                    coordinator_display_name=args.coordinator_display_name,
                )
            )
            payload = {
                "command": "replay",
                "bundle_dir": str(summary.bundle.bundle_dir),
                "integrity_manifest_sha256": summary.bundle.integrity_manifest_sha256,
                "canonical_content_sha256": summary.bundle.canonical_content_sha256,
                "database_name": summary.database_name,
                "status": summary.status,
            }
        else:
            verified = verify_coordinator_rehearsal_bundle(
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
