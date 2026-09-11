"""Publish or independently verify a bounded Product Test Run receipt.

The existing product-proving command and receipt identities remain unchanged.
This is a software exercise, not Contract Acceptance of construction work.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from corridor import digests
from corridor.product_proving_database import (
    ProductProvingDatabaseBaselineConfig,
    SharedDevelopmentRestoreConfig,
    capture_product_proving_database_baseline,
    restore_shared_development_database,
    verify_product_proving_database_baseline,
)
from corridor.product_proving_run import (
    CandidateSetComparison,
    ExpectedPreflight,
    ExtractionConfiguration,
    ObservedPreflight,
    ProductProvingFailureCapture,
    publish_product_proving_failure_bundle,
    verify_product_proving_bundle,
    verify_product_proving_failure_bundle,
)
from corridor.product_proving_frontend_capture import verify_frontend_pass_bundle
from corridor.product_proving_restore import publish_product_proving_restore_bundle
from corridor.product_proving_session import (
    ObservedProductProvingPublicationConfig,
    publish_observed_product_proving_session,
)




def _sha256(value: str) -> str:
    if not digests.is_digest(value):
        raise argparse.ArgumentTypeError(
            "must be exactly 64 lowercase hexadecimal characters"
        )
    return value


_CONTRACT = """\
Publish a Product Test Run only from two independently sealed live-frontend pass bundles and the
exact restored database baseline; arbitrary success capture JSON is not accepted:
  make product-proving ARGS="publish-observed --database-baseline-dir=<dir> --database-baseline-manifest-sha256=<sha> --pass-1-dir=<dir> --pass-1-manifest-sha256=<sha> --restore-1-dir=<dir> --restore-1-manifest-sha256=<sha> --pass-2-dir=<dir> --pass-2-manifest-sha256=<sha> --restore-2-dir=<dir> --restore-2-manifest-sha256=<sha> --source-database-url=<url> --output-dir=<new-dir>"
  make product-proving ARGS="verify <bundle-dir> --expected-manifest-sha256=<sha>"
Capture and clone-verify the exact local development database before a pass;
restore requires an explicit exact-target opt-in and re-verifies every public
schema object, table, and sequence after replacing the database:
  make product-proving ARGS="database-capture --source-database-url=<url> --postgres-admin-url=<url> --expected-clean-git-revision=<sha> --expected-migration-head=<head> --output-dir=<new-dir>"
  make product-proving ARGS="database-restore <bundle-dir> --source-database-url=<url> --postgres-admin-url=<url> --expected-source-database-name=corridor --expected-clean-git-revision=<sha> --expected-migration-head=<head> --expected-manifest-sha256=<sha> --pass-bundle-dir=<dir> --pass-bundle-manifest-sha256=<sha> --restore-receipt-output-dir=<new-dir> --allow-shared-development-restore"
A terminal failure uses `publish-failure` and `verify-failure`; it can never
be read through the successful two-pass verifier.
"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="product-proving-run",
        description="Publish or verify a bounded Product Test Run and its exact receipts.",
        epilog=_CONTRACT,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    publish = commands.add_parser(
        "publish-observed", help="publish two observed test passes and exact restoration receipts"
    )
    publish.add_argument("--database-baseline-dir", type=Path, required=True)
    publish.add_argument("--database-baseline-manifest-sha256", type=_sha256, required=True)
    publish.add_argument("--pass-1-dir", type=Path, required=True)
    publish.add_argument("--pass-1-manifest-sha256", type=_sha256, required=True)
    publish.add_argument("--pass-2-dir", type=Path, required=True)
    publish.add_argument("--pass-2-manifest-sha256", type=_sha256, required=True)
    publish.add_argument("--restore-1-dir", type=Path, required=True)
    publish.add_argument("--restore-1-manifest-sha256", type=_sha256, required=True)
    publish.add_argument("--restore-2-dir", type=Path, required=True)
    publish.add_argument("--restore-2-manifest-sha256", type=_sha256, required=True)
    publish.add_argument("--source-database-url", required=True)
    publish.add_argument("--repo-root", type=Path, default=Path.cwd())
    publish.add_argument("--output-dir", type=Path, required=True)
    publish_failure = commands.add_parser(
        "publish-failure", help="publish a terminal failed Product Test Run receipt"
    )
    publish_failure.add_argument("--capture-json", type=Path, required=True)
    publish_failure.add_argument("--output-dir", type=Path, required=True)
    verify = commands.add_parser("verify", help="verify a successful Product Test Run bundle")
    verify.add_argument("bundle_dir", type=Path)
    verify.add_argument("--expected-manifest-sha256", type=_sha256, required=True)
    verify_failure = commands.add_parser(
        "verify-failure", help="verify a failed Product Test Run bundle"
    )
    verify_failure.add_argument("bundle_dir", type=Path)
    verify_failure.add_argument(
        "--expected-manifest-sha256", type=_sha256, required=True
    )
    database_capture = commands.add_parser("database-capture")
    database_capture.add_argument("--source-database-url", required=True)
    database_capture.add_argument("--postgres-admin-url", required=True)
    database_capture.add_argument("--expected-clean-git-revision", required=True)
    database_capture.add_argument("--expected-migration-head", required=True)
    database_capture.add_argument("--output-dir", type=Path, required=True)
    database_capture.add_argument("--repo-root", type=Path, default=Path.cwd())
    database_verify = commands.add_parser("database-verify")
    database_verify.add_argument("bundle_dir", type=Path)
    database_verify.add_argument(
        "--expected-manifest-sha256", type=_sha256, required=True
    )
    database_restore = commands.add_parser("database-restore")
    database_restore.add_argument("bundle_dir", type=Path)
    database_restore.add_argument("--source-database-url", required=True)
    database_restore.add_argument("--postgres-admin-url", required=True)
    database_restore.add_argument("--expected-source-database-name", required=True)
    database_restore.add_argument("--expected-clean-git-revision", required=True)
    database_restore.add_argument("--expected-migration-head", required=True)
    database_restore.add_argument(
        "--expected-manifest-sha256", type=_sha256, required=True
    )
    database_restore.add_argument("--repo-root", type=Path, default=Path.cwd())
    database_restore.add_argument("--pass-bundle-dir", type=Path, required=True)
    database_restore.add_argument(
        "--pass-bundle-manifest-sha256", type=_sha256, required=True
    )
    database_restore.add_argument(
        "--restore-receipt-output-dir", type=Path, required=True
    )
    database_restore.add_argument(
        "--allow-shared-development-restore", action="store_true"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "publish-observed":
            bundle = publish_observed_product_proving_session(
                ObservedProductProvingPublicationConfig(
                    database_baseline_dir=args.database_baseline_dir,
                    database_baseline_manifest_sha256=(
                        args.database_baseline_manifest_sha256
                    ),
                    pass_one_dir=args.pass_1_dir,
                    pass_one_manifest_sha256=args.pass_1_manifest_sha256,
                    pass_two_dir=args.pass_2_dir,
                    pass_two_manifest_sha256=args.pass_2_manifest_sha256,
                    restore_one_dir=args.restore_1_dir,
                    restore_one_manifest_sha256=args.restore_1_manifest_sha256,
                    restore_two_dir=args.restore_2_dir,
                    restore_two_manifest_sha256=args.restore_2_manifest_sha256,
                    source_database_url=args.source_database_url,
                    repo_root=args.repo_root,
                    output_dir=args.output_dir,
                )
            )
            payload = {
                "command": "publish-observed",
                "bundle_dir": str(bundle.bundle_dir),
                "integrity_manifest_sha256": bundle.integrity_manifest_sha256,
                "canonical_content_sha256": bundle.canonical_content_sha256,
                "status": "passed",
            }
        elif args.command == "publish-failure":
            raw = json.loads(args.capture_json.read_bytes())
            bundle = publish_product_proving_failure_bundle(
                args.output_dir, _failure_capture(raw)
            )
            payload = {
                "command": "publish-failure",
                "bundle_dir": str(bundle.bundle_dir),
                "integrity_manifest_sha256": bundle.integrity_manifest_sha256,
                "canonical_content_sha256": bundle.canonical_content_sha256,
                "status": "failed",
            }
        elif args.command == "verify":
            verified = verify_product_proving_bundle(
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
        elif args.command == "verify-failure":
            verified = verify_product_proving_failure_bundle(
                args.bundle_dir,
                expected_integrity_manifest_sha256=args.expected_manifest_sha256,
            )
            payload = {
                "command": "verify-failure",
                "bundle_dir": str(args.bundle_dir),
                "valid": verified.valid,
                "integrity_manifest_sha256": verified.integrity_manifest_sha256,
                "canonical_content_sha256": verified.canonical_content_sha256,
                "status": "failed",
            }
        elif args.command == "database-capture":
            summary = capture_product_proving_database_baseline(
                ProductProvingDatabaseBaselineConfig(
                    source_database_url=args.source_database_url,
                    postgres_admin_url=args.postgres_admin_url,
                    expected_checkout_revision=args.expected_clean_git_revision,
                    expected_migration_head=args.expected_migration_head,
                    output_dir=args.output_dir,
                    repo_root=args.repo_root,
                )
            )
            payload = {
                "command": "database-capture",
                "bundle_dir": str(summary.bundle_dir),
                "manifest_sha256": summary.manifest_sha256,
                "dump_sha256": summary.dump_sha256,
                "state_sha256": summary.state_sha256,
                "schema_sha256": summary.schema_sha256,
                "table_count": summary.table_count,
                "sequence_count": summary.sequence_count,
            }
        elif args.command == "database-verify":
            verified = verify_product_proving_database_baseline(
                args.bundle_dir,
                expected_manifest_sha256=args.expected_manifest_sha256,
            )
            payload = {
                "command": "database-verify",
                "bundle_dir": str(args.bundle_dir),
                "valid": True,
                "manifest_sha256": verified.manifest_sha256,
                "dump_sha256": verified.dump_sha256,
                "state_sha256": verified.fingerprint.state_sha256,
                "schema_sha256": verified.fingerprint.schema_sha256,
            }
        else:
            baseline = verify_product_proving_database_baseline(
                args.bundle_dir,
                expected_manifest_sha256=args.expected_manifest_sha256,
            )
            frontend_pass = verify_frontend_pass_bundle(
                args.pass_bundle_dir,
                expected_integrity_manifest_sha256=(
                    args.pass_bundle_manifest_sha256
                ),
            )
            summary = restore_shared_development_database(
                SharedDevelopmentRestoreConfig(
                    source_database_url=args.source_database_url,
                    postgres_admin_url=args.postgres_admin_url,
                    expected_source_database_name=args.expected_source_database_name,
                    expected_checkout_revision=args.expected_clean_git_revision,
                    expected_migration_head=args.expected_migration_head,
                    bundle_dir=args.bundle_dir,
                    expected_manifest_sha256=args.expected_manifest_sha256,
                    repo_root=args.repo_root,
                    allow_shared_development_restore=(
                        args.allow_shared_development_restore
                    ),
                )
            )
            restore_receipt = publish_product_proving_restore_bundle(
                args.restore_receipt_output_dir,
                frontend_pass=frontend_pass,
                baseline=baseline,
                restore=summary,
            )
            payload = {
                "command": "database-restore",
                "source_database_name": summary.source_database_name,
                "restore_operation_id": summary.operation_id,
                "previous_state_sha256": summary.previous_state_sha256,
                "restored_state_sha256": summary.restored_state_sha256,
                "restored_schema_sha256": baseline.fingerprint.schema_sha256,
                "manifest_sha256": summary.manifest_sha256,
                "dump_sha256": summary.dump_sha256,
                "restore_receipt_dir": str(restore_receipt.bundle_dir),
                "restore_receipt_manifest_sha256": (
                    restore_receipt.integrity_manifest_sha256
                ),
            }
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


def _failure_capture(raw: dict) -> ProductProvingFailureCapture:
    return ProductProvingFailureCapture(
        expected=_expected(raw["expected"]),
        observed=_observed(raw["observed"]),
        pass_number=raw["pass_number"],
        phase=raw["phase"],
        errors=tuple(raw["errors"]),
        extraction_comparisons=tuple(
            _comparison(comparison)
            for comparison in raw["extraction_comparisons"]
        ),
        extraction_run_receipts=tuple(raw["extraction_run_receipts"]),
        admission_started=raw["admission_started"],
        source_database_mutated=raw["source_database_mutated"],
        operations_elapsed_seconds=raw["operations_elapsed_seconds"],
        baseline_dump_sha256=raw["baseline_dump_sha256"],
        baseline_state_manifest_sha256=raw["baseline_state_manifest_sha256"],
        restored_baseline_fingerprint=raw["restored_baseline_fingerprint"],
    )


def _expected(raw: dict) -> ExpectedPreflight:
    return ExpectedPreflight(
        source_revision=raw["source_revision"],
        origin_main_revision=raw["origin_main_revision"],
        migration_head=raw["migration_head"],
        policy_digests=raw["policy_digests"],
        documents={int(key): value for key, value in raw["documents"].items()},
        baseline_runs={
            int(key): value for key, value in raw["baseline_runs"].items()
        },
        milestone_sources=raw["milestone_sources"],
        baseline_fingerprint=raw["baseline_fingerprint"],
    )


def _observed(raw: dict) -> ObservedPreflight:
    return ObservedPreflight(
        source_revision=raw["source_revision"],
        origin_main_revision=raw["origin_main_revision"],
        clean_worktree=raw["clean_worktree"],
        migration_head=raw["migration_head"],
        policy_digests=raw["policy_digests"],
        documents={int(key): value for key, value in raw["documents"].items()},
        baseline_runs={
            int(key): value for key, value in raw["baseline_runs"].items()
        },
        milestone_sources=raw["milestone_sources"],
        baseline_fingerprint=raw["baseline_fingerprint"],
    )


def _comparison(raw: dict) -> CandidateSetComparison:
    return CandidateSetComparison(
        document_id=raw["document_id"],
        baseline_run_id=raw["baseline_run_id"],
        fresh_run_id=raw["fresh_run_id"],
        baseline_configuration=ExtractionConfiguration(
            **raw["baseline_configuration"]
        ),
        fresh_configuration=ExtractionConfiguration(**raw["fresh_configuration"]),
        added=tuple(raw["added"]),
        missing=tuple(raw["missing"]),
        matched_sha256=tuple(raw["matched_sha256"]),
    )


if __name__ == "__main__":
    raise SystemExit(main())
