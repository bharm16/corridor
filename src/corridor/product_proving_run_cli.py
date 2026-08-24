"""Publish or independently verify a bounded Product Proving Run receipt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

from corridor.product_proving_run import (
    CandidateSetComparison,
    ExpectedPreflight,
    ObservedPreflight,
    ProductProvingCapture,
    ProductProvingPass,
    publish_product_proving_bundle,
    verify_product_proving_bundle,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _sha256(value: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "must be exactly 64 lowercase hexadecimal characters"
        )
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="product-proving-run")
    commands = parser.add_subparsers(dest="command", required=True)
    publish = commands.add_parser("publish")
    publish.add_argument("--capture-json", type=Path, required=True)
    publish.add_argument("--pass-1-approved-export", type=Path, required=True)
    publish.add_argument("--pass-2-approved-export", type=Path, required=True)
    publish.add_argument("--output-dir", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("bundle_dir", type=Path)
    verify.add_argument("--expected-manifest-sha256", type=_sha256, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "publish":
            raw = json.loads(args.capture_json.read_bytes())
            capture = _capture(
                raw,
                pass_one_pdf=args.pass_1_approved_export.read_bytes(),
                pass_two_pdf=args.pass_2_approved_export.read_bytes(),
            )
            bundle = publish_product_proving_bundle(args.output_dir, capture)
            payload = {
                "command": "publish",
                "bundle_dir": str(bundle.bundle_dir),
                "integrity_manifest_sha256": bundle.integrity_manifest_sha256,
                "canonical_content_sha256": bundle.canonical_content_sha256,
                "status": "passed",
            }
        else:
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
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


def _capture(
    raw: dict,
    *,
    pass_one_pdf: bytes,
    pass_two_pdf: bytes,
) -> ProductProvingCapture:
    return ProductProvingCapture(
        expected=_expected(raw["expected"]),
        observed=_observed(raw["observed"]),
        pass_one=_pass(raw["pass_one"], pass_one_pdf),
        pass_two=_pass(raw["pass_two"], pass_two_pdf),
        final_baseline_fingerprint=raw["final_baseline_fingerprint"],
        simulated_practitioner=raw["simulated_practitioner"],
        same_project_manual_report_compared=raw[
            "same_project_manual_report_compared"
        ],
        revision_processing_included=raw["revision_processing_included"],
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


def _pass(raw: dict, pdf_bytes: bytes) -> ProductProvingPass:
    return ProductProvingPass(
        pass_number=raw["pass_number"],
        restored_baseline_fingerprint=raw["restored_baseline_fingerprint"],
        extraction_comparisons=tuple(
            CandidateSetComparison(**comparison)
            for comparison in raw["extraction_comparisons"]
        ),
        extraction_failures=tuple(raw["extraction_failures"]),
        admission_completed=raw["admission_completed"],
        residual_candidate_ids=tuple(raw["residual_candidate_ids"]),
        residual_outcomes={
            int(key): value for key, value in raw["residual_outcomes"].items()
        },
        frontend_kind=raw["frontend_kind"],
        frontend_actions=tuple(raw["frontend_actions"]),
        invalid_action_refused=raw["invalid_action_refused"],
        invalid_action_write_set=raw["invalid_action_write_set"],
        correction_preserved_predecessor=raw["correction_preserved_predecessor"],
        work_decision_change_preserved_predecessor=raw[
            "work_decision_change_preserved_predecessor"
        ],
        report_pdf_sha256=raw["report_pdf_sha256"],
        approved_export_sha256=raw["approved_export_sha256"],
        approved_export_bytes=pdf_bytes,
        report_provenance_classes=tuple(raw["report_provenance_classes"]),
        write_set=raw["write_set"],
        operations_elapsed_seconds=raw["operations_elapsed_seconds"],
        practitioner_elapsed_seconds=raw["practitioner_elapsed_seconds"],
        non_blocking_friction=tuple(raw["non_blocking_friction"]),
        workarounds=tuple(raw["workarounds"]),
    )


if __name__ == "__main__":
    raise SystemExit(main())
