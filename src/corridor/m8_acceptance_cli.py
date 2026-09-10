"""Explicit operator boundary for M8 capture, replay, and verification.

``capture`` is the only command permitted to construct a model client, and it
does so only after the operator passes ``--live-model`` plus exact model and
Git pins.  The ordinary ``replay`` and ``verify`` paths operate solely on
captured bytes and never construct a model client.

This replaces the prior subjective human gate and injected Python-only seam:
operators now get one explicit paid-capture command and model-free commands
whose inputs, outcomes, and failure status are machine-checkable.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
import sys

from corridor.m8_acceptance import (
    AcceptanceCaptureConfig,
    AcceptanceError,
    AcceptanceRunConfig,
    capture_m8_fixture,
    run_m8_acceptance,
    verify_m8_acceptance_bundle,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _sha256(value: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "must be exactly 64 lowercase hexadecimal characters"
        )
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="m8-acceptance",
        description=(
            "Capture, replay, or verify the M8 software acceptance test. This is not Contract "
            "Acceptance of construction work."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    capture = commands.add_parser(
        "capture",
        help="explicitly capture fresh exact inputs through the live model",
    )
    capture.add_argument(
        "--live-model",
        action="store_true",
        help="opt in to paid live extraction; absent means capture fails closed",
    )
    capture.add_argument("--source-lock", required=True, type=Path)
    capture.add_argument("--output-dir", required=True, type=Path)
    capture.add_argument("--postgres-admin-url", required=True)
    capture.add_argument("--prompt-version", required=True)
    capture.add_argument("--expected-model", required=True)
    capture.add_argument("--schema-version", required=True)
    capture.add_argument("--expected-clean-git-revision", required=True)

    replay = commands.add_parser(
        "replay",
        help="run the ordinary model-free acceptance replay from captured bytes",
    )
    replay.add_argument("--fixture", required=True, type=Path)
    replay.add_argument("--transformations", required=True, type=Path)
    replay.add_argument("--output-dir", required=True, type=Path)
    replay.add_argument("--postgres-admin-url", required=True)
    replay.add_argument(
        "--expected-fixture-sha256",
        required=True,
        type=_sha256,
    )
    replay.add_argument(
        "--expected-transformations-sha256",
        required=True,
        type=_sha256,
    )
    replay.add_argument("--expected-clean-git-revision")

    verify = commands.add_parser(
        "verify",
        help="verify an exported acceptance bundle without replaying it",
    )
    verify.add_argument("bundle_dir", type=Path)
    verify.add_argument(
        "--expected-manifest-sha256",
        required=True,
        type=_sha256,
        help="trusted SHA-256 pin for manifest.json",
    )
    return parser


def _capture_payload(summary) -> dict:
    return {
        "command": "capture",
        "fixture_path": str(summary.fixture_path),
        "fixture_sha256": summary.fixture_sha256,
        "database_name": summary.database_name,
        "run_count": summary.run_count,
        "candidate_count": summary.candidate_count,
    }


def _replay_payload(summary) -> dict:
    return {
        "command": "replay",
        "bundle_dir": str(summary.bundle_dir),
        "manifest_path": str(summary.manifest_path),
        "integrity_manifest_sha256": summary.integrity_manifest_sha256,
        "canonical_content_sha256": summary.canonical_content_sha256,
        "fixture_sha256": summary.fixture_sha256,
        "assertions": [asdict(assertion) for assertion in summary.assertions],
        "carried_count": summary.carried_count,
        "abstention_counts": dict(sorted(summary.abstention_counts.items())),
        "database_name": summary.database_name,
    }


def _verification_payload(bundle_dir: Path, result) -> dict:
    return {
        "command": "verify",
        "bundle_dir": str(bundle_dir),
        "valid": result.valid,
        "integrity_manifest_sha256": result.integrity_manifest_sha256,
        "canonical_content_sha256": result.canonical_content_sha256,
    }


def _print_json(payload: dict) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


@dataclass(frozen=True)
class ProductionExtractor:
    """The extractor a live capture must match, and the read it performs.

    Passed in rather than reached for, so a test states which extractor
    identity it is exercising instead of rewriting a module attribute. The
    default is the deployed one and is resolved only on the live path.
    """

    prompt_version: str
    schema_version: str
    extract_document: Callable[..., list]


def production_extractor() -> ProductionExtractor:
    """The deployed Matrix route: the selected native reader (#766)."""

    # Imports stay inside the explicit live path.  Model-free replay neither
    # constructs a client nor needs to know which extractor production uses.
    from corridor.native_matrix import PROMPT_VERSION, SCHEMA_VERSION
    from corridor.pipeline import extraction_route

    def extract_document(session, document, *, client=None):
        return extraction_route(document, client=client).extract(session, document)

    return ProductionExtractor(
        prompt_version=PROMPT_VERSION,
        schema_version=SCHEMA_VERSION,
        extract_document=extract_document,
    )


def _capture_with_live_model(
    config: AcceptanceCaptureConfig,
    *,
    extractor: ProductionExtractor | None = None,
):
    """Run capture through one pinned production client, closed on every exit."""

    extractor = extractor if extractor is not None else production_extractor()

    if config.prompt_version != extractor.prompt_version:
        raise AcceptanceError(
            f"pinned prompt version {config.prompt_version!r} does not match "
            f"production PROMPT_VERSION {extractor.prompt_version!r}"
        )
    if config.schema_version != extractor.schema_version:
        raise AcceptanceError(
            f"pinned schema version {config.schema_version!r} does not match "
            f"production SCHEMA_VERSION {extractor.schema_version!r}"
        )

    from corridor.llm import OpenAIClient

    client = OpenAIClient()
    try:
        if client.model != config.expected_model:
            raise AcceptanceError(
                f"configured live model {client.model!r} does not match "
                f"--expected-model {config.expected_model!r}"
            )

        def extract(session, document):
            return extractor.extract_document(session, document, client=client)

        return capture_m8_fixture(config, extract=extract)
    finally:
        client.close()


def main(
    argv: list[str] | None = None,
    *,
    extractor: ProductionExtractor | None = None,
) -> int:
    """Route one explicit acceptance operation and print stable JSON.

    In particular, ``replay`` does not accept or derive a model client.
    ``extractor`` names the extractor a live capture must match; it defaults
    to the deployed one.
    """

    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)

    exit_status = 0
    try:
        if args.command == "capture":
            if not args.live_model:
                raise AcceptanceError(
                    "capture is fail-closed without explicit --live-model opt-in"
                )
            summary = _capture_with_live_model(
                AcceptanceCaptureConfig(
                    source_lock_path=args.source_lock,
                    output_dir=args.output_dir,
                    postgres_admin_url=args.postgres_admin_url,
                    prompt_version=args.prompt_version,
                    expected_model=args.expected_model,
                    schema_version=args.schema_version,
                    expected_clean_git_revision=args.expected_clean_git_revision,
                ),
                extractor=extractor,
            )
            payload = _capture_payload(summary)
        elif args.command == "replay":
            summary = run_m8_acceptance(
                AcceptanceRunConfig(
                    fixture_path=args.fixture,
                    transformations_path=args.transformations,
                    output_dir=args.output_dir,
                    postgres_admin_url=args.postgres_admin_url,
                    expected_fixture_sha256=args.expected_fixture_sha256,
                    expected_transformations_sha256=(
                        args.expected_transformations_sha256
                    ),
                    expected_clean_git_revision=args.expected_clean_git_revision,
                )
            )
            payload = _replay_payload(summary)
            if any(not assertion.passed for assertion in summary.assertions):
                exit_status = 1
        else:
            result = verify_m8_acceptance_bundle(
                args.bundle_dir,
                expected_integrity_manifest_sha256=(
                    args.expected_manifest_sha256
                ),
            )
            payload = _verification_payload(args.bundle_dir, result)
    except (RuntimeError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1

    _print_json(payload)
    return exit_status


if __name__ == "__main__":
    raise SystemExit(main())
