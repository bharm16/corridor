"""Bundle writing and verification for M8 acceptance exports."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any

from corridor.m8_acceptance_publication import publish_directory_once


@dataclass(frozen=True)
class VerificationResult:
    valid: bool
    integrity_manifest_sha256: str
    canonical_content_sha256: str


def write_bundle(
    output_dir: Path,
    *,
    environment: dict[str, Any],
    real_chain: dict[str, Any],
    controlled_lane: dict[str, Any],
    assertions,
    canonical_content: dict[str, Any],
    bundle_schema_version: str,
    bundle_files: Sequence[str],
    error_cls: type[Exception],
    corrupt_bundle_error_cls: type[Exception],
    canonical_json: Callable[[Any], bytes],
    sha256: Callable[[bytes], str],
    json_sha256: Callable[[Any], str],
) -> tuple[Path, str, str]:
    exports = {
        "environment.json": environment,
        "real-chain.json": real_chain,
        "controlled-lane.json": controlled_lane,
        "assertions.json": {
            "claim_boundary": canonical_content["claim_boundary"],
            "assertions": [asdict(item) for item in assertions],
        },
        "canonical-content.json": canonical_content,
    }
    canonical_sha256 = json_sha256(canonical_content)
    try:
        published_dir = publish_directory_once(
            output_dir,
            temp_prefix="corridor-m8-bundle",
            build=lambda stage_dir: _stage_bundle(
                stage_dir,
                exports=exports,
                canonical_sha256=canonical_sha256,
                bundle_schema_version=bundle_schema_version,
                bundle_files=bundle_files,
                canonical_json=canonical_json,
                sha256=sha256,
            ),
        )
    except FileExistsError as exc:
        raise error_cls("bundle output directory already exists") from exc
    manifest_path = published_dir / "manifest.json"
    manifest_sha256 = sha256(manifest_path.read_bytes())
    verified = verify_bundle(
        published_dir,
        expected_integrity_manifest_sha256=manifest_sha256,
        bundle_schema_version=bundle_schema_version,
        bundle_files=bundle_files,
        corrupt_bundle_error_cls=corrupt_bundle_error_cls,
        sha256=sha256,
        json_sha256=json_sha256,
    )
    if verified.canonical_content_sha256 != canonical_sha256:
        raise error_cls("new acceptance bundle failed self-verification")
    return manifest_path, manifest_sha256, canonical_sha256


def verify_bundle(
    bundle_dir: Path,
    *,
    expected_integrity_manifest_sha256: str,
    bundle_schema_version: str,
    bundle_files: Sequence[str],
    corrupt_bundle_error_cls: type[Exception],
    sha256: Callable[[bytes], str],
    json_sha256: Callable[[Any], str],
) -> VerificationResult:
    """Verify the export set against a caller-held manifest identity pin."""

    bundle_dir = Path(bundle_dir)
    manifest_path = bundle_dir / "manifest.json"
    if re.fullmatch(r"[0-9a-f]{64}", expected_integrity_manifest_sha256) is None:
        raise corrupt_bundle_error_cls("expected manifest digest is invalid")
    if manifest_path.is_symlink():
        raise corrupt_bundle_error_cls("manifest.json must not be a symlink")
    try:
        manifest_bytes = manifest_path.read_bytes()
    except OSError as exc:
        raise corrupt_bundle_error_cls("manifest.json is absent or invalid") from exc
    actual_manifest_sha256 = sha256(manifest_bytes)
    if actual_manifest_sha256 != expected_integrity_manifest_sha256:
        raise corrupt_bundle_error_cls(
            "manifest digest does not match the caller-provided pin"
        )
    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError as exc:
        raise corrupt_bundle_error_cls("manifest.json is absent or invalid") from exc
    if manifest.get("schema_version") != bundle_schema_version:
        raise corrupt_bundle_error_cls("manifest.json has an unsupported schema")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != set(bundle_files):
        raise corrupt_bundle_error_cls("manifest.json does not name the exact export set")
    try:
        actual_entries = {path.name: path for path in bundle_dir.iterdir()}
    except OSError as exc:
        raise corrupt_bundle_error_cls("bundle directory is absent or invalid") from exc
    if set(actual_entries) != {"manifest.json", *bundle_files}:
        raise corrupt_bundle_error_cls("bundle contains an unmanifested export")
    if any(
        path.is_symlink() or not path.is_file()
        for path in actual_entries.values()
    ):
        raise corrupt_bundle_error_cls("bundle contains an unmanifested export")

    root = bundle_dir.resolve()
    for relative_path, expected in sorted(files.items()):
        pure = PurePosixPath(relative_path)
        if (
            pure.is_absolute()
            or ".." in pure.parts
            or pure.as_posix() != relative_path
            or len(pure.parts) != 1
        ):
            raise corrupt_bundle_error_cls(f"unsafe manifest path {relative_path!r}")
        path = bundle_dir / relative_path
        if path.is_symlink():
            raise corrupt_bundle_error_cls(f"{relative_path} must not be a symlink")
        try:
            path.resolve().relative_to(root)
            value = path.read_bytes()
        except (OSError, ValueError) as exc:
            raise corrupt_bundle_error_cls(f"{relative_path} is absent or unsafe") from exc
        if not isinstance(expected, dict):
            raise corrupt_bundle_error_cls(f"{relative_path} manifest entry is invalid")
        if expected.get("bytes") != len(value) or expected.get("sha256") != sha256(
            value
        ):
            raise corrupt_bundle_error_cls(f"{relative_path} does not match its digest")

    try:
        canonical = json.loads((bundle_dir / "canonical-content.json").read_bytes())
    except json.JSONDecodeError as exc:
        raise corrupt_bundle_error_cls("canonical-content.json is invalid") from exc
    canonical_sha256 = json_sha256(canonical)
    if manifest.get("canonical_content_sha256") != canonical_sha256:
        raise corrupt_bundle_error_cls("canonical-content.json identity does not match")
    return VerificationResult(
        valid=True,
        integrity_manifest_sha256=actual_manifest_sha256,
        canonical_content_sha256=canonical_sha256,
    )


def _stage_bundle(
    output_dir: Path,
    *,
    exports: dict[str, Any],
    canonical_sha256: str,
    bundle_schema_version: str,
    bundle_files: Sequence[str],
    canonical_json: Callable[[Any], bytes],
    sha256: Callable[[bytes], str],
) -> None:
    file_manifest: dict[str, dict[str, Any]] = {}
    for relative_path in bundle_files:
        value = canonical_json(exports[relative_path]) + b"\n"
        target = output_dir / relative_path
        target.write_bytes(value)
        file_manifest[relative_path] = {
            "bytes": len(value),
            "sha256": sha256(value),
        }
    manifest = {
        "schema_version": bundle_schema_version,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "canonical_content_sha256": canonical_sha256,
        "files": file_manifest,
    }
    (output_dir / "manifest.json").write_bytes(canonical_json(manifest) + b"\n")
